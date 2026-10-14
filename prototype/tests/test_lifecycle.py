"""Lease states, admission, and cancellation, in process on a FakeClock.

held --begin_drain--> draining --release--> released --acquire--> held (generation + 1).
A draining lease stays live and renewable; a released or expired one fences its own
generation. Process level drain behavior lives in test_drain.py.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.errors import Cancelled, Draining, FencedError, LeaseDenied
from gameshark.lease import LeaseManager
from gameshark.lifecycle import Phase
from gameshark.store import connect
from gameshark.worker import run_forever
from tests.helpers import AGENT, pr_event

TTL = 10.0


@pytest.fixture
def lease(paths, clock):
    conn = connect(paths["runtime"])
    yield LeaseManager(conn, AGENT, clock)
    conn.close()


def test_drain_keeps_lease_live_and_renewable(lease, clock):
    gen = lease.acquire("host-a", TTL)
    assert lease.state() == "held"
    lease.begin_drain("host-a", gen)
    lease.begin_drain("host-a", gen)                      # idempotent
    assert lease.state() == "draining"

    clock.advance(TTL - 1)
    lease.renew("host-a", gen, TTL)                       # renewal continues while draining
    clock.advance(TTL - 1)
    with pytest.raises(LeaseDenied):                      # successor refused while valid
        lease.acquire("host-b", TTL)
    assert lease.state() == "draining"


def test_release_fences_old_generation_without_successor(lease):
    gen = lease.acquire("host-a", TTL)
    lease.begin_drain("host-a", gen)
    lease.release("host-a", gen)
    assert lease.state() == "released" and lease.current_generation() == gen

    with pytest.raises(FencedError, match="released"):
        lease.conn.execute("BEGIN IMMEDIATE")
        try:
            lease.guard_in_txn(gen)
        finally:
            lease.conn.execute("ROLLBACK")
    for attempt in (lambda: lease.renew("host-a", gen, TTL),
                    lambda: lease.begin_drain("host-a", gen)):
        with pytest.raises(FencedError):
            attempt()
    assert lease.acquire("host-b", TTL) == gen + 1
    assert lease.state() == "held"


def test_expired_lease_refuses_renewal(lease, clock):
    gen = lease.acquire("host-a", TTL)
    clock.advance(TTL + 1)
    with pytest.raises(FencedError, match="expired"):
        lease.renew("host-a", gen, TTL)
    assert lease.acquire("host-a", TTL) == gen + 1         # reclaim bumps the fence


def test_same_owner_reclaiming_draining_lease_bumps_generation(lease):
    gen = lease.acquire("host-a", TTL)
    assert lease.acquire("host-a", TTL) == gen            # plain renew keeps it
    lease.begin_drain("host-a", gen)
    assert lease.acquire("host-a", TTL) == gen + 1        # the draining incarnation is fenced
    assert lease.state() == "held"


def test_stopped_supervisor_commits_nothing_more(make_supervisor):
    sup = make_supervisor()
    gen = sup.generation
    sup.stop()
    assert sup.lifecycle.phase is Phase.STOPPED
    with pytest.raises(FencedError):
        sup._fenced(lambda conn: conn.execute("DELETE FROM events"))
    with pytest.raises(FencedError):
        sup.journal._write("k", "pending", {"adapter": "pr"}, None)
    assert sup.lease.current_generation() == gen
    sup.close()


def test_draining_refuses_new_work_before_it_persists(make_supervisor):
    sup = make_supervisor()
    assert sup.lifecycle.phase is Phase.SERVING
    assert sup.lifecycle.begin_drain(deadline=0.0)
    with pytest.raises(Draining):
        sup.submit(pr_event())
    assert sup.router.status(pr_event()["event_id"]) is None   # stays with its source
    sup.close()


def test_cancel_leaves_event_pending_for_successor(make_supervisor):
    sup = make_supervisor()
    sup.lifecycle.token.cancel("drain deadline reached")
    with pytest.raises(Cancelled):
        sup._submit_inflight(pr_event())
    with pytest.raises(Cancelled):
        sup.router._handle(pr_event())                     # dispatch never starts
    sup.close()

    successor = make_supervisor(owner_token="stark-host")  # same host restart
    assert successor.router.status(pr_event()["event_id"]) is None
    assert successor.submit(pr_event()) == "acked"
    successor.close()


def test_run_forever_drains_on_stop_and_releases(make_supervisor):
    sup = make_supervisor()
    stop = threading.Event()
    stop.set()
    logs: list[str] = []
    assert run_forever(sup, stop, 0.0, log=logs.append) == 0
    assert any("draining generation" in line for line in logs), logs
    assert sup.lease.state() == "released"
    assert sup.lifecycle.phase is Phase.STOPPED
    sup.close()


def test_v02_lease_table_migrates_in_place(tmp_path):
    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE lease (agent_id TEXT PRIMARY KEY, owner_token TEXT, "
                "generation INTEGER NOT NULL DEFAULT 0, expires_at REAL NOT NULL DEFAULT 0)")
    raw.execute("INSERT INTO lease VALUES ('stark', 'host-a', 7, 0)")
    raw.commit()
    raw.close()
    conn = connect(db)
    row = conn.execute("SELECT generation, state FROM lease").fetchone()
    assert tuple(row) == (7, "held")
    conn.close()
