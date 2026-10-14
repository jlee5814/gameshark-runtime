"""Fencing after takeover: a stale owner detects the loss, exits, and commits nothing.

Regression for the PR #1 finding on LeaseManager.renew: a double rollback replaced
FencedError with sqlite3.OperationalError, so run_forever logged and retried forever
instead of exiting. Each takeover here is a second owner acquiring the lease after the
first owner's TTL lapsed on a shared FakeClock.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.errors import FencedError
from gameshark.github_client import FakeGitHubClient
from gameshark.lease import LeaseManager
from gameshark.store import connect
from gameshark.supervisor import AgentSupervisor
from gameshark.worker import apply_controls, enqueue_control, run_forever
from sims.github_monitor import GitHubMonitorAdapter
from tests.helpers import AGENT, PR_RESP, pr_event

REPO, PR = "acme/widget", 42
TTL = 1.0


def take_over(paths, clock, owner: str = "host-2") -> int:
    """Another host acquires the lapsed lease without running recovery yet."""
    clock.advance(TTL + 1)
    conn = connect(paths["runtime"])
    try:
        return LeaseManager(conn, AGENT, clock).acquire(owner, 30.0)
    finally:
        conn.close()


def lease_row(paths) -> tuple:
    conn = connect(paths["runtime"])
    try:
        r = conn.execute("SELECT owner_token, generation, expires_at FROM lease").fetchone()
        return tuple(r)
    finally:
        conn.close()


def snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {t: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {t}")) for t in tables}


def build_monitor(paths, clock, owner: str, client: FakeGitHubClient,
                  sup_cls=AgentSupervisor) -> AgentSupervisor:
    sup = sup_cls(db_path=paths["runtime"], agent_id=AGENT, owner_token=owner, clock=clock,
                  lease_ttl_s=TTL)
    sup.add_adapter(GitHubMonitorAdapter(client))
    sup.start()
    return sup


def test_renew_after_takeover_raises_fenced_error(make_supervisor, paths, clock):
    sup1 = make_supervisor(owner_token="host-1", lease_ttl_s=TTL)
    assert take_over(paths, clock) == sup1.generation + 1

    with pytest.raises(FencedError):
        sup1.lease.renew(sup1.owner_token, sup1.generation, TTL)
    assert not sup1.conn.in_transaction
    assert sup1.lease.current_generation() == sup1.generation + 1   # connection still usable
    sup1.close()


def test_write_txn_keeps_original_exception(tmp_path):
    from gameshark.store import write_txn

    conn = connect(tmp_path / "txn.db")

    # The body already ended the transaction: no second rollback, no masking.
    with pytest.raises(ValueError, match="original"), write_txn(conn):
        conn.execute("ROLLBACK")
        raise ValueError("original")
    assert not conn.in_transaction

    # A failing statement rolls back once and surfaces its own error.
    with pytest.raises(sqlite3.OperationalError, match="no such table"), write_txn(conn):
        conn.execute("INSERT INTO lease(agent_id, generation) VALUES('x', 1)")
        conn.execute("SELECT * FROM missing_table")
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM lease").fetchone()[0] == 0
    conn.close()


def test_run_forever_exits_4_after_takeover(make_supervisor, paths, clock):
    sup1 = make_supervisor(owner_token="host-1", lease_ttl_s=TTL)
    take_over(paths, clock)
    after_takeover = lease_row(paths)

    logs: list[str] = []
    code = run_forever(sup1, threading.Event(), 0.0, log=logs.append, max_ticks=20)

    assert code == 4
    assert any("lost ownership" in line for line in logs)
    assert not any("tick failed" in line for line in logs)
    assert lease_row(paths) == after_takeover       # stale release left the new lease alone
    sup1.close()


def test_stale_owner_cannot_commit(make_supervisor, paths, clock):
    client = FakeGitHubClient()
    sup1 = make_supervisor(owner_token="host-1", lease_ttl_s=TTL, start=False)
    monitor = GitHubMonitorAdapter(client)
    sup1.add_adapter(monitor)
    sup1.start()
    monitor.accept_responsibility(REPO, PR)
    client.add_review("r1", ordinal=100, state="approved")
    enqueue_control(sup1.conn, AGENT, "github", "watch", {"repo": REPO, "pr": 7})

    take_over(paths, clock)
    before = snapshot(sup1.conn)

    attempts = {
        "renew": lambda: sup1.lease.renew(sup1.owner_token, sup1.generation, TTL),
        "submit": lambda: sup1.submit(pr_event()),
        "poll": sup1.poll,
        "apply_controls": lambda: apply_controls(sup1),
        "journal": lambda: sup1.journal._write("k", "pending", {"adapter": "pr"}, None),
        "responsibility": lambda: sup1.responsibilities.transition(PR_RESP, "done"),
        "companion": lambda: monitor._notify_effect(
            REPO, PR, "new_review", "review:r1", "s", "r").do(sup1.generation),
    }
    for name, attempt in attempts.items():
        with pytest.raises(FencedError):
            attempt()
        assert not sup1.conn.in_transaction, name

    assert snapshot(sup1.conn) == before
    sup1.close()


class TakeoverAtIntent(AgentSupervisor):
    """Loses the lease after the fenced intent checkpoint, before the effect dispatches."""

    takeover = None

    def _maybe_crash(self, label: str) -> None:
        if label == "B_after_intent" and self.takeover is not None:
            self.takeover()
            self.takeover = None


def test_stale_notification_rejected_then_recovered_once(paths, clock):
    client = FakeGitHubClient()
    client.add_review("r1", ordinal=100, state="approved")
    sup1 = build_monitor(paths, clock, "host-1", client, sup_cls=TakeoverAtIntent)
    sup1.registry.get("github").accept_responsibility(REPO, PR)
    sup1.takeover = lambda: take_over(paths, clock)

    with pytest.raises(FencedError):
        sup1.poll()
    count = "SELECT COUNT(*) FROM notifications"
    assert sup1.conn.execute(count).fetchone()[0] == 0
    stale = sup1.conn.execute("SELECT status, generation FROM effects").fetchall()
    assert [tuple(r) for r in stale] == [("pending", sup1.generation)]
    sup1.close()

    sup2 = build_monitor(paths, clock, "host-2", client)          # recovery dispatches it
    assert sup2.conn.execute(count).fetchone()[0] == 1
    done = sup2.conn.execute("SELECT status, generation FROM effects").fetchall()
    assert [tuple(r) for r in done] == [("done", sup2.generation)]
    sup2.poll()
    assert sup2.conn.execute(count).fetchone()[0] == 1
    sup2.close()
