"""Crash and recovery tests A through I, plus hardening cases.

Each restart builds a fresh supervisor on the same database files. Recovery is a pure
function of durable state. The real subprocess test proves this at the OS level.
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.errors import CrashInjected, FencedError, LeaseDenied
from tests.helpers import (
    PR_OP,
    PR_RESP,
    game_event,
    game_op,
    game_remote,
    github_remote,
    pr_event,
)


# ----------------------------------------------------------------- A
def test_a_crash_before_intent(make_supervisor, paths):
    sup1 = make_supervisor(crash_at={"A_before_intent"})
    with pytest.raises(CrashInjected):
        sup1.submit(pr_event())
    assert sup1.effect_status(PR_OP) is None          # no intent recorded
    assert github_remote(paths).comment_count(PR_OP) == 0
    sup1.close()

    sup2 = make_supervisor()                           # restart replays the pending event
    assert github_remote(paths).comment_count(PR_OP) == 1
    assert sup2.effect_status(PR_OP) == "done"
    assert sup2.responsibility_state(PR_RESP) == "done"
    sup2.close()


# ----------------------------------------------------------------- B
def test_b_crash_after_intent_before_dispatch(make_supervisor, paths):
    sup1 = make_supervisor(crash_at={"B_after_intent"})
    with pytest.raises(CrashInjected):
        sup1.submit(pr_event())
    assert sup1.effect_status(PR_OP) == "pending"
    assert github_remote(paths).comment_count(PR_OP) == 0
    sup1.close()

    sup2 = make_supervisor()                           # reconcile: probe ABSENT -> dispatch once
    assert github_remote(paths).comment_count(PR_OP) == 1
    assert sup2.effect_status(PR_OP) == "done"
    sup2.close()


# ----------------------------------------------------------------- C
def test_c_crash_after_remote_before_journal(make_supervisor, paths):
    sup1 = make_supervisor(crash_at={"C_after_remote"})
    with pytest.raises(CrashInjected):
        sup1.submit(pr_event())
    assert github_remote(paths).comment_count(PR_OP) == 1   # remote already landed
    assert sup1.effect_status(PR_OP) == "pending"           # local journal not yet done
    sup1.close()

    sup2 = make_supervisor()                           # reconcile: probe FOUND -> no duplicate
    assert github_remote(paths).comment_count(PR_OP) == 1
    assert sup2.effect_status(PR_OP) == "done"
    sup2.close()


# ----------------------------------------------------------------- D
def test_d_crash_after_journal_done(make_supervisor, paths):
    sup1 = make_supervisor(crash_at={"D_after_done"})
    with pytest.raises(CrashInjected):
        sup1.submit(pr_event())
    assert sup1.effect_status(PR_OP) == "done"
    assert github_remote(paths).comment_count(PR_OP) == 1
    sup1.close()

    sup2 = make_supervisor()                           # event replays; effect already done
    assert github_remote(paths).comment_count(PR_OP) == 1
    assert sup2.router.status(pr_event()["event_id"]) == "acked"
    sup2.close()


# ----------------------------------------------------------------- E
def test_e_two_outstanding_responsibilities(make_supervisor, paths):
    sup1 = make_supervisor()
    sup1.submit(game_event(turn=1))                    # recurring duty makes move 1
    sup1.crash_at.add("C_after_remote")
    with pytest.raises(CrashInjected):
        sup1.submit(pr_event())                        # pr effect pending after remote landed
    assert sup1.responsibility_state("game:m1") == "active"
    assert sup1.effect_status(PR_OP) == "pending"
    sup1.close()

    sup2 = make_supervisor()                           # both responsibilities resume
    assert github_remote(paths).comment_count(PR_OP) == 1
    assert sup2.responsibility_state(PR_RESP) == "done"
    assert sup2.responsibility_state("game:m1") == "active"
    sup2.submit(game_event(turn=2))                    # recurring duty continues
    assert game_remote(paths).move_count(game_op(turn=1)) == 1
    assert game_remote(paths).move_count(game_op(turn=2)) == 1
    sup2.close()


# ----------------------------------------------------------------- F
def test_f_two_concurrent_supervisors(make_supervisor):
    sup1 = make_supervisor(owner_token="host-1")
    sup2 = make_supervisor(owner_token="host-2", start=False)
    with pytest.raises(LeaseDenied):
        sup2.start()
    sup1.close()
    sup2.close()


# ----------------------------------------------------------------- G
def test_g_stale_worker_cannot_commit(make_supervisor, paths, clock):
    sup1 = make_supervisor(owner_token="host-A", lease_ttl_s=10)
    clock.advance(20)                                  # sup1 lease expires
    sup2 = make_supervisor(owner_token="host-B", lease_ttl_s=10)   # takeover bumps the fence
    assert sup2.generation == sup1.generation + 1

    with pytest.raises(FencedError):
        sup1.submit(pr_event())                        # stale generation rejected locally
    assert github_remote(paths).comment_count(PR_OP) == 0
    sup1.close()
    sup2.close()


# ----------------------------------------------------------------- H
def test_h_ambiguous_blocks_and_escalates(make_supervisor, paths):
    sup1 = make_supervisor(crash_at={"C_after_remote"})
    with pytest.raises(CrashInjected):
        sup1.submit(pr_event())
    assert github_remote(paths).comment_count(PR_OP) == 1
    sup1.close()

    sup2 = make_supervisor(github_ambiguous=True)      # probe cannot confirm on recovery
    assert sup2.effect_status(PR_OP) == "blocked"
    assert sup2.responsibility_state(PR_RESP) == "blocked"
    assert sup2.router.status(pr_event()["event_id"]) == "dead"
    assert github_remote(paths, ambiguous=True).comment_count(PR_OP) == 1   # not duplicated
    sup2.close()


# ----------------------------------------------------------------- I
def test_i_model_replacement_keeps_identity(make_supervisor, paths):
    sup1 = make_supervisor()
    uuid1 = sup1.identity.uuid
    sup1.submit(game_event(turn=1))
    sup1.stop()
    sup1.close()

    sup2 = make_supervisor(owner_token="stark-host-v2")   # new session, same agent
    assert sup2.identity.uuid == uuid1
    # Adapter reconnect reuses the same identity (never resets it).
    assert sup2._make_ctx("game").identity_uuid == uuid1
    sup2.submit(game_event(turn=8))
    assert game_remote(paths).move_count(game_op(turn=8)) == 1
    sup2.close()


# ----------------------------------------------------------------- delayed visibility
def test_delayed_remote_visibility(make_supervisor, paths):
    sup1 = make_supervisor(crash_at={"C_after_remote"})
    with pytest.raises(CrashInjected):
        sup1.submit(pr_event())
    assert github_remote(paths).comment_count(PR_OP) == 1
    sup1.close()

    # Remote landed the write but hides it from the probe (eventual consistency).
    sup2 = make_supervisor(github_visible=False)
    # Probe reports ABSENT, so recovery re-dispatches; destination idempotency keeps count 1.
    assert github_remote(paths).comment_count(PR_OP) == 1
    assert sup2.effect_status(PR_OP) == "done"
    sup2.close()


# ----------------------------------------------------------------- stale external effect
def test_stale_external_effect_rejected_by_destination(make_supervisor, paths, clock):
    from gameshark.errors import StaleFenceRejected

    sup1 = make_supervisor(owner_token="host-A", lease_ttl_s=10)   # generation 1
    clock.advance(20)
    sup2 = make_supervisor(owner_token="host-B", lease_ttl_s=10)   # generation 2 takes over
    sup2.submit(pr_event())                                        # lands with fence 2
    assert github_remote(paths).comment_count(PR_OP) == 1

    # A stale worker that bypassed local fencing still cannot land an external effect:
    # the destination rejects the older fence. Local fencing alone could not stop this.
    remote = github_remote(paths)
    with pytest.raises(StaleFenceRejected):
        remote.post_comment(PR_OP, "acme/widget", 42, body="stale", fence=sup1.generation)
    assert github_remote(paths).comment_count(PR_OP) == 1
    sup1.close()
    sup2.close()


# ----------------------------------------------------------------- authorization revocation
def test_authorization_revocation_survives_restart(make_supervisor, paths):
    sup1 = make_supervisor()
    sup1.submit(game_event(turn=1))                    # game works
    sup1.revoke("pr", "pr.comment")                    # durable revocation
    sup1.close()

    sup2 = make_supervisor()                           # restart re-registers adapters
    with pytest.raises(PermissionError):
        sup2.submit(pr_event())                        # refused: cap stays revoked
    assert github_remote(paths).comment_count(PR_OP) == 0
    assert not sup2.grants.effective("pr", "pr.comment")
    sup2.close()


# ----------------------------------------------------------------- real subprocess kill
def test_real_subprocess_kill_then_recover(make_supervisor, paths, tmp_path):
    uuid_out = tmp_path / "worker_uuid.txt"
    worker = Path(__file__).resolve().parent / "crash_worker.py"
    proc = subprocess.run(
        [sys.executable, str(worker), str(paths["runtime"]), str(paths["game"]),
         str(paths["github"]), str(uuid_out), "C_after_remote"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    # The worker killed itself with SIGKILL after the remote write committed.
    assert proc.returncode == -signal.SIGKILL, proc.stderr.decode()
    worker_uuid = uuid_out.read_text(encoding="utf8").strip()
    assert github_remote(paths).comment_count(PR_OP) == 1      # external effect landed once

    # Parent recovers from durable state alone.
    sup = make_supervisor()
    assert sup.identity.uuid == worker_uuid                    # identity survived a real kill
    assert github_remote(paths).comment_count(PR_OP) == 1      # no duplicate on recovery
    assert sup.effect_status(PR_OP) == "done"
    assert sup.responsibility_state(PR_RESP) == "done"
    sup.close()
