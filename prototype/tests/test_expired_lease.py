"""Expiry fencing: an owner whose lease lapsed commits nothing, even with no successor.

Regression for issue #10. The guard checked generation and release but not expires_at,
so an expired owner kept committing until its next heartbeat or a successor's acquire.
Every case here runs on a shared FakeClock so expiry lands at an exact instant.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.errors import FencedError
from gameshark.github_client import FakeGitHubClient
from gameshark.lease import LeaseManager
from gameshark.store import connect, write_txn
from sims.github_monitor import GitHubMonitorAdapter
from tests.helpers import AGENT, PR_OP, PR_RESP, pr_event
from tests.test_lease_fencing import snapshot

REPO, PR = "acme/widget", 42
TTL = 10.0


def mark(conn) -> None:
    conn.execute("INSERT INTO monitor_seen VALUES(?,?,?,?,?)", (AGENT, REPO, PR, "x", 0.0))


def seen(sup) -> int:
    return sup.conn.execute("SELECT COUNT(*) FROM monitor_seen").fetchone()[0]


def test_expired_owner_with_no_successor_commits_nothing(make_supervisor, clock):
    client = FakeGitHubClient()
    sup = make_supervisor(owner_token="host-1", lease_ttl_s=TTL, start=False)
    monitor = GitHubMonitorAdapter(client)
    sup.add_adapter(monitor)
    sup.start()
    monitor.accept_responsibility(REPO, PR)
    sup.responsibilities.upsert(PR_RESP, "pr-review", {"repo": REPO, "pr": PR})

    clock.advance(TTL + 1)                                   # lapsed; nobody took over
    before = snapshot(sup.conn)
    attempts = {
        "fenced": lambda: sup._fenced(mark),
        "submit": lambda: sup.submit(pr_event()),
        "journal": lambda: sup.journal._write("k", "pending", {"adapter": "pr"}, None),
        "event_status": lambda: sup.router._set_status("ev", "acked"),
        "responsibility": lambda: sup.responsibilities.transition(PR_RESP, "done"),
        "companion": lambda: monitor._notify_effect(
            REPO, PR, "new_review", "review:r1", "s", "r").do(sup.generation),
    }
    for name, attempt in attempts.items():
        with pytest.raises(FencedError, match="expired"):
            attempt()
        assert not sup.conn.in_transaction, name

    assert snapshot(sup.conn) == before
    assert sup.lease.current_generation() == sup.generation   # no successor exists
    sup.close()


def test_expiry_boundary_rejects_at_expires_at(make_supervisor, clock):
    sup = make_supervisor(lease_ttl_s=TTL)
    clock.advance(TTL - 0.5)
    sup._fenced(mark)                                        # still live: commits
    assert seen(sup) == 1

    clock.advance(0.5)                                       # now == expires_at exactly
    before = snapshot(sup.conn)
    with pytest.raises(FencedError, match="expired"):
        sup._fenced(lambda conn: conn.execute("DELETE FROM monitor_seen"))
    assert snapshot(sup.conn) == before
    sup.close()


@pytest.mark.parametrize("generation", [0, 1])
def test_missing_lease_row_fails_closed(tmp_path, clock, generation):
    conn = connect(tmp_path / "rt.db")
    lease = LeaseManager(conn, AGENT, clock)
    before = snapshot(conn)
    with pytest.raises(FencedError, match="no lease"), write_txn(conn):
        lease.guard_in_txn(generation)
        mark(conn)
    assert snapshot(conn) == before
    conn.close()


def test_renewed_lease_commits_past_original_ttl(make_supervisor, clock):
    sup = make_supervisor(lease_ttl_s=TTL)
    for _ in range(3):
        clock.advance(TTL - 1)
        sup.lease.renew(sup.owner_token, sup.generation, TTL)
    sup._fenced(mark)                                        # 27 s in, three renewals
    assert seen(sup) == 1
    sup.close()


def test_draining_owner_commits_while_renewed_then_fences_on_expiry(make_supervisor, clock):
    sup = make_supervisor(lease_ttl_s=TTL)
    sup.lease.begin_drain(sup.owner_token, sup.generation)
    clock.advance(TTL - 1)
    sup.lease.renew(sup.owner_token, sup.generation, TTL)    # the heartbeat keeps draining
    clock.advance(TTL - 1)
    sup._fenced(mark)                                        # past the first TTL, still live
    assert sup.lease.state() == "draining" and seen(sup) == 1

    clock.advance(1)                                         # heartbeat stalled: expired
    before = snapshot(sup.conn)
    with pytest.raises(FencedError, match="expired"):
        sup._fenced(mark)
    assert snapshot(sup.conn) == before
    sup.close()


def test_released_draining_owner_commits_nothing(make_supervisor, clock):
    sup = make_supervisor(lease_ttl_s=TTL)
    sup.lease.begin_drain(sup.owner_token, sup.generation)
    sup.lease.release(sup.owner_token, sup.generation)
    before = snapshot(sup.conn)
    with pytest.raises(FencedError):
        sup._fenced(mark)
    assert snapshot(sup.conn) == before
    sup.close()


def test_old_generation_fenced_after_takeover(make_supervisor, paths, clock):
    sup1 = make_supervisor(owner_token="host-1", lease_ttl_s=TTL)
    clock.advance(TTL + 1)
    sup2 = make_supervisor(owner_token="host-2", lease_ttl_s=TTL)
    assert sup2.generation == sup1.generation + 1

    before = snapshot(sup1.conn)
    with pytest.raises(FencedError, match="stale generation"):
        sup1._fenced(mark)
    assert snapshot(sup1.conn) == before
    sup2._fenced(mark)                                       # the successor still commits
    sup1.close()
    sup2.close()


def test_late_completion_fails_instead_of_recording_success(make_supervisor, paths, clock):
    sup = make_supervisor(lease_ttl_s=TTL)
    remote = sup.registry.get("pr").remote
    post = remote.post_comment

    def slow_post(*args, **kwargs):
        clock.advance(TTL + 1)                               # the call outlives the lease
        return post(*args, **kwargs)

    remote.post_comment = slow_post
    with pytest.raises(FencedError, match="expired"):
        sup.submit(pr_event())
    assert not sup.conn.in_transaction

    # Intent stays pending and the event unacknowledged, so a successor reconciles it.
    assert sup.effect_status(PR_OP) == "pending"
    assert sup.router.status(pr_event()["event_id"]) == "pending"
    assert sup.responsibility_state(PR_RESP) == "active"
    sup.close()

    sup2 = make_supervisor(owner_token="host-2", lease_ttl_s=TTL)
    assert sup2.effect_status(PR_OP) == "done"
    assert sup2.responsibility_state(PR_RESP) == "done"
    sup2.close()


def test_expiry_during_transaction_commits_before_any_takeover(make_supervisor, clock):
    """The guaranteed boundary: the guard instant, not the COMMIT instant.

    The guard runs inside BEGIN IMMEDIATE and SQLite holds the write lock until COMMIT,
    so no acquire interleaves. A write whose guard passed commits even if the clock
    crosses expires_at before COMMIT; it linearizes before any successor, whose acquire
    then bumps the generation and fences the old owner.
    """
    sup1 = make_supervisor(owner_token="host-1", lease_ttl_s=TTL)

    def crosses_expiry(conn):
        clock.advance(TTL + 1)
        mark(conn)

    sup1._fenced(crosses_expiry)
    assert seen(sup1) == 1

    sup2 = make_supervisor(owner_token="host-2", lease_ttl_s=TTL)
    assert sup2.generation == sup1.generation + 1
    with pytest.raises(FencedError):
        sup1._fenced(lambda conn: conn.execute("DELETE FROM monitor_seen"))
    assert seen(sup2) == 1
    sup1.close()
    sup2.close()
