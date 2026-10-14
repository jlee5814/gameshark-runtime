"""Invariant coverage I1 through I7 from architecture/continuity-invariants.md.

Each test targets one invariant directly. Some scenarios overlap the A-I recovery
tests; the README matrix cross references them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.errors import CrashInjected, LeaseDenied
from tests.helpers import PR_OP, PR_RESP, pr_event


# I1: identity stability across restart.
def test_i1_identity_stable_across_restart(make_supervisor):
    sup1 = make_supervisor()
    uuid1 = sup1.identity.uuid
    sup1.stop()
    sup1.close()
    sup2 = make_supervisor(owner_token="stark-host-v2")
    assert sup2.identity.uuid == uuid1
    sup2.close()


# I2: single owner at a time.
def test_i2_single_owner(make_supervisor):
    sup1 = make_supervisor(owner_token="host-1")
    sup2 = make_supervisor(owner_token="host-2", start=False)
    with pytest.raises(LeaseDenied):
        sup2.start()
    sup1.close()
    sup2.close()


# I3: checkpoint before effect. Intent persists before any external action.
def test_i3_checkpoint_before_effect(make_supervisor, paths):
    from tests.helpers import github_remote

    sup1 = make_supervisor(crash_at={"B_after_intent"})
    with pytest.raises(CrashInjected):
        sup1.submit(pr_event())
    assert sup1.effect_status(PR_OP) == "pending"       # intent recorded
    assert github_remote(paths).comment_count(PR_OP) == 0   # before the effect
    sup1.close()


# I4: idempotent effect application. A confirmed effect is never duplicated.
def test_i4_idempotent_effect(make_supervisor, paths):
    from tests.helpers import github_remote

    sup1 = make_supervisor(crash_at={"C_after_remote"})
    with pytest.raises(CrashInjected):
        sup1.submit(pr_event())
    sup1.close()
    sup2 = make_supervisor()
    assert github_remote(paths).comment_count(PR_OP) == 1
    sup2.close()


# I5: at least once delivery plus dedup. Replay recovers, duplicate delivery is absorbed.
def test_i5_inbox_replay_and_dedup(make_supervisor, paths):
    from tests.helpers import github_remote

    # replay: crash before handling, event survives and replays on restart.
    sup1 = make_supervisor(crash_at={"A_before_intent"})
    with pytest.raises(CrashInjected):
        sup1.submit(pr_event())
    sup1.close()
    sup2 = make_supervisor()
    assert github_remote(paths).comment_count(PR_OP) == 1

    # dedup: delivering the same event id again is a no op.
    outcome = sup2.submit(pr_event())
    assert outcome == "duplicate"
    assert github_remote(paths).comment_count(PR_OP) == 1
    sup2.close()


# I6: authorization never widens, and a revocation survives restart.
def test_i6_authorization_durable(make_supervisor, paths):
    from tests.helpers import github_remote

    sup1 = make_supervisor()
    sup1.revoke("pr", "pr.comment")
    sup1.close()
    sup2 = make_supervisor()
    with pytest.raises(PermissionError):
        sup2.submit(pr_event())
    assert github_remote(paths).comment_count(PR_OP) == 0
    assert not sup2.grants.effective("pr", "pr.comment")
    sup2.close()


# I7: outcome completeness. A done effect carries a non null external id.
def test_i7_outcome_completeness(make_supervisor):
    sup = make_supervisor()
    sup.submit(pr_event())
    assert sup.effect_status(PR_OP) == "done"
    row = sup.conn.execute("SELECT external_id FROM effects WHERE key=?", (PR_OP,)).fetchone()
    assert row["external_id"] is not None
    assert sup.responsibility_state(PR_RESP) == "done"
    sup.close()
