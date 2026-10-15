"""Presence and the two phase jack: P1 one active surface, P2 stale surfaces rejected,
P3 every handoff resolves to exactly one committed generation, even across SIGKILL.

A restart is a fresh connection and fresh managers on the same file.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.clock import FakeClock
from gameshark.errors import FencedError, HandoffConflict, StalePresence
from gameshark.lease import LeaseManager
from gameshark.presence import (
    ABORTED,
    ACK_TIMEOUT_S,
    ACKED,
    COMMITTED,
    PREPARE_TIMEOUT_S,
    PREPARING,
    REVERTED,
    PresenceManager,
)
from gameshark.store import connect, write_txn
from tests.helpers import AGENT, HOST

WORKER = Path(__file__).with_name("handoff_worker.py")


def boot(db: Path, clock, owner: str = HOST) -> PresenceManager:
    conn = connect(db)
    lease = LeaseManager(conn, AGENT, clock)
    gen = lease.acquire(owner, 30.0)
    return PresenceManager(conn, AGENT, lease, gen, clock)


@pytest.fixture
def pm(paths, clock) -> PresenceManager:
    p = boot(paths["runtime"], clock)
    p.ensure_home("mac")
    return p


def surface_write(pm: PresenceManager, surface: str, gen: int) -> None:
    with write_txn(pm.conn):
        pm.guard_surface_in_txn(surface, gen)


def test_home_is_created_once_and_survives_restart(paths, clock, pm):
    assert (pm.current().surface, pm.current().generation) == ("mac", 1)
    pm.conn.close()
    again = boot(paths["runtime"], clock)
    assert again.ensure_home("phone").surface == "mac"     # restart never resets presence
    assert again.current().generation == 1


def test_full_jack_in_and_out(pm):
    h = pm.request_jack("h1", "phone")
    assert h.state == PREPARING and pm.current().surface == "mac"
    surface_write(pm, "mac", 1)                           # source still acts while preparing

    h = pm.ready_out("h1", "mac", 1, draft="half typed")
    assert (h.state, h.to_gen, h.draft) == (COMMITTED, 2, "half typed")
    assert (pm.current().surface, pm.current().generation) == ("phone", 2)
    with pytest.raises(StalePresence):
        surface_write(pm, "mac", 1)                       # P2: the ghost cannot act
    assert pm.ack_in("h1", "phone", 2).state == ACKED
    assert pm.ack_in("h1", "phone", 2).state == ACKED     # idempotent

    pm.request_jack("h2", "mac")
    pm.ready_out("h2", "phone", 2)
    pm.ack_in("h2", "mac", 3)
    assert (pm.current().surface, pm.current().generation) == ("mac", 3)


def test_ready_out_is_idempotent(pm):
    pm.request_jack("h1", "phone")
    first = pm.ready_out("h1", "mac", 1, draft="a")
    again = pm.ready_out("h1", "mac", 1, draft="b")
    assert first == again and pm.current().generation == 2


def test_silent_source_commits_without_draft_at_prepare_deadline(pm, clock):
    pm.request_jack("h1", "phone")
    clock.advance(PREPARE_TIMEOUT_S - 0.1)
    assert pm.tick() is None
    clock.advance(0.2)
    h = pm.tick()
    assert (h.state, h.draft, pm.current().surface) == (COMMITTED, None, "phone")


def test_missing_ack_reverts_home_and_fences_the_target(pm, clock):
    pm.request_jack("h1", "phone")
    pm.ready_out("h1", "mac", 1)
    clock.advance(ACK_TIMEOUT_S)
    assert pm.tick().state == REVERTED
    assert (pm.current().surface, pm.current().generation) == ("mac", 3)
    with pytest.raises(StalePresence):
        pm.ack_in("h1", "phone", 2)                       # late ack loses
    with pytest.raises(StalePresence):
        surface_write(pm, "phone", 2)


def test_target_lost_aborts_while_preparing(pm):
    pm.request_jack("h1", "phone")
    assert pm.surface_lost("phone").state == ABORTED
    assert (pm.current().surface, pm.current().generation) == ("mac", 1)
    assert pm.surface_lost("phone") is None


def test_target_lost_after_commit_reverts(pm):
    pm.request_jack("h1", "phone")
    pm.ready_out("h1", "mac", 1)
    assert pm.surface_lost("phone").state == REVERTED
    assert (pm.current().surface, pm.current().generation) == ("mac", 3)


def test_failed_jack_home_falls_back_to_the_source(pm, clock):
    pm.request_jack("h1", "phone")
    pm.ready_out("h1", "mac", 1)
    pm.ack_in("h1", "phone", 2)
    pm.request_jack("h2", "mac")
    pm.ready_out("h2", "phone", 2)
    clock.advance(ACK_TIMEOUT_S)
    assert pm.tick().state == REVERTED
    assert (pm.current().surface, pm.current().generation) == ("phone", 4)


def test_one_open_handoff_at_a_time(pm):
    h = pm.request_jack("h1", "phone")
    assert pm.request_jack("h1", "phone") == h            # retry with the same id
    with pytest.raises(HandoffConflict):
        pm.request_jack("h1", "watch")                    # same id, other jack
    with pytest.raises(HandoffConflict):
        pm.request_jack("h2", "watch")
    pm.surface_lost("phone")
    with pytest.raises(HandoffConflict):
        pm.request_jack("h3", "mac")                      # mac already holds presence


def test_stale_owner_cannot_move_presence(paths, clock, pm):
    pm.request_jack("h1", "phone")
    clock.advance(31)
    boot(paths["runtime"], clock, owner="host-2")         # takeover bumps the owner fence
    with pytest.raises(FencedError):
        pm.ready_out("h1", "mac", 1)
    assert pm.current().surface == "mac"


@pytest.mark.parametrize("label, after_restart", [
    ("P1_after_prepare", PREPARING),
    ("P2_after_commit", COMMITTED),
])
def test_sigkill_mid_jack_resolves_to_one_generation(paths, label, after_restart):
    proc = subprocess.run([sys.executable, str(WORKER), str(paths["runtime"]), label],
                          check=False, capture_output=True, timeout=30)
    assert proc.returncode == -9, proc.stderr.decode()

    # Restart past the dead owner's lease and every persisted deadline.
    clock = FakeClock(time.time() + 60)
    pm = boot(paths["runtime"], clock, owner="host-2")
    h = pm.open_handoff()
    assert h is not None and h.state == after_restart

    if after_restart == PREPARING:
        h = pm.tick()                                     # dead source: commit, no draft
        assert (h.state, h.to_gen, h.draft) == (COMMITTED, 2, None)
        pm.ack_in("h1", "phone", 2)
        assert (pm.current().surface, pm.current().generation) == ("phone", 2)
    else:
        assert h.draft == "half typed"                    # the committed draft survived
        assert pm.tick().state == REVERTED                # nobody acked before the deadline
        assert (pm.current().surface, pm.current().generation) == ("mac", 3)
    assert pm.open_handoff() is None


# ---------------------------------------------------------------------- review fixes
def assert_untouched(pm: PresenceManager, surface: str = "mac", gen: int = 1) -> None:
    assert (pm.current().surface, pm.current().generation) == (surface, gen)


@pytest.mark.parametrize("surface, gen", [
    ("phone", 1),          # the target knows the id and the source generation
    ("phone", 2),          # the target guesses its future generation
    ("watch", 1),          # a bystander
    ("mac", 0),            # the source with a stale generation
    ("mac", 2),            # the source with a future generation
])
def test_ready_out_from_anyone_but_the_source_at_its_generation_is_rejected(pm, surface, gen):
    pm.request_jack("h1", "phone")
    with pytest.raises(StalePresence):
        pm.ready_out("h1", surface, gen, draft="forged")
    assert pm.handoff("h1").state == PREPARING and pm.handoff("h1").draft is None
    assert_untouched(pm)


def test_ready_out_replay_after_commit_answers_only_the_source(pm):
    pm.request_jack("h1", "phone")
    first = pm.ready_out("h1", "mac", 1, draft="kept")
    assert pm.ready_out("h1", "mac", 1, draft="ignored") == first   # no second commit
    assert_untouched(pm, "phone", 2)
    with pytest.raises(StalePresence):
        pm.ready_out("h1", "phone", 2)                    # the target cannot replay it


def test_source_of_an_old_jack_cannot_commit_a_later_one(pm):
    pm.request_jack("h1", "phone")
    pm.ready_out("h1", "mac", 1)
    pm.ack_in("h1", "phone", 2)
    pm.request_jack("h2", "mac")
    with pytest.raises(StalePresence):
        pm.ready_out("h2", "mac", 1)                      # mac was the source of h1, not h2
    assert pm.handoff("h2").state == PREPARING
    assert_untouched(pm, "phone", 2)


def test_ack_after_the_deadline_is_rejected_before_any_tick(pm, clock):
    pm.request_jack("h1", "phone")
    pm.ready_out("h1", "mac", 1)
    clock.advance(ACK_TIMEOUT_S)                          # exactly at the deadline, as tick
    with pytest.raises(StalePresence):
        pm.ack_in("h1", "phone", 2)
    assert pm.handoff("h1").state == REVERTED             # resolved in the ack's transaction
    assert_untouched(pm, "mac", 3)
    assert pm.tick() is None                              # nothing left for tick to do
    with pytest.raises(StalePresence):
        surface_write(pm, "phone", 2)


def test_ack_just_before_the_deadline_lands(pm, clock):
    pm.request_jack("h1", "phone")
    pm.ready_out("h1", "mac", 1)
    clock.advance(ACK_TIMEOUT_S - 0.01)
    assert pm.ack_in("h1", "phone", 2).state == ACKED
    clock.advance(1)
    assert pm.ack_in("h1", "phone", 2).state == ACKED    # an acked replay stays acked
    assert pm.tick() is None
    assert_untouched(pm, "phone", 2)


def test_acked_replay_requires_the_target(pm):
    pm.request_jack("h1", "phone")
    pm.ready_out("h1", "mac", 1)
    pm.ack_in("h1", "phone", 2)
    with pytest.raises(StalePresence):
        pm.ack_in("h1", "mac", 2)


# ---------------------------------------------------------------------- surface jacks
def test_surface_pulls_presence_to_itself_at_the_current_generation(pm):
    h = pm.request_jack_by("in1", "phone", 1, "self")
    assert (h.from_surface, h.to_surface, h.state) == ("mac", "phone", PREPARING)
    assert pm.request_jack_by("in1", "phone", 1, "self") == h      # replay, same handoff
    pm.ready_out("in1", "mac", 1)
    pm.ack_in("in1", "phone", 2)
    assert (pm.current().surface, pm.current().generation) == ("phone", 2)


def test_surface_jack_on_a_stale_view_is_rejected(pm):
    pm.request_jack("h1", "phone")
    pm.ready_out("h1", "mac", 1)
    pm.ack_in("h1", "phone", 2)
    with pytest.raises(StalePresence):
        pm.request_jack_by("in2", "mac", 1, "self")                # mac saw generation 1
    assert pm.open_handoff() is None


def test_only_the_holder_sends_stark_home(pm):
    pm.request_jack("h1", "phone")
    pm.ready_out("h1", "mac", 1)
    pm.ack_in("h1", "phone", 2)
    with pytest.raises(StalePresence):
        pm.request_jack_by("out1", "tablet", 2, "home")            # not the holder
    with pytest.raises(StalePresence):
        pm.request_jack_by("out1", "phone", 1, "home")             # holder, stale gen
    h = pm.request_jack_by("out1", "phone", 2, "home")
    assert (h.from_surface, h.to_surface) == ("phone", "mac")


def test_surface_jack_conflicts_and_rejects_unknown_direction(pm):
    with pytest.raises(HandoffConflict):
        pm.request_jack_by("in1", "mac", 1, "self")                # already holds presence
    with pytest.raises(HandoffConflict):
        pm.request_jack_by("home1", "mac", 1, "home")              # home holding, nowhere to go
    pm.request_jack_by("in1", "phone", 1, "self")
    with pytest.raises(HandoffConflict):
        pm.request_jack_by("in2", "tablet", 1, "self")             # one open handoff
    with pytest.raises(HandoffConflict):
        pm.request_jack_by("in1", "tablet", 1, "self")             # id names another jack
    with pytest.raises(ValueError):
        pm.request_jack_by("x", "phone", 1, "sideways")


# ---------------------------------------------------------------------- eviction
def test_evicting_the_holder_returns_home_under_a_new_generation(pm):
    pm.request_jack("h1", "phone")
    pm.ready_out("h1", "mac", 1)
    pm.ack_in("h1", "phone", 2)
    assert pm.evict("phone") is True
    assert (pm.current().surface, pm.current().generation) == ("mac", 3)
    with pytest.raises(StalePresence):
        surface_write(pm, "phone", 2)
    assert pm.evict("phone") is False                              # idempotent


@pytest.mark.parametrize("phase", ["preparing_to", "committed_to", "preparing_from",
                                   "committed_from"])
def test_evicting_a_surface_closes_its_open_handoff(pm, phase):
    if phase in ("preparing_from", "committed_from"):
        pm.request_jack("h0", "phone")
        pm.ready_out("h0", "mac", 1)
        pm.ack_in("h0", "phone", 2)
        pm.request_jack("h1", "mac")                               # phone -> mac preparing
        if phase == "committed_from":
            pm.ready_out("h1", "phone", 2)
    else:
        pm.request_jack("h1", "phone")
        if phase == "committed_to":
            pm.ready_out("h1", "mac", 1)
    pm.evict("phone")
    assert pm.open_handoff() is None
    assert pm.handoff("h1").state in (ABORTED, REVERTED)
    assert pm.current().surface == "mac"
    surface_write(pm, "mac", pm.current().generation)


def test_revoking_the_source_of_a_committed_jack_home_never_returns_presence(pm, clock):
    """The phone sends Stark home, the Mac holds him unacked, then the phone is revoked.
    The missed ack must not roll presence back to the revoked phone."""
    pm.request_jack("h0", "phone")
    pm.ready_out("h0", "mac", 1)
    pm.ack_in("h0", "phone", 2)
    pm.request_jack_by("h1", "phone", 2, "home")
    assert pm.ready_out("h1", "phone", 2).state == COMMITTED
    assert (pm.current().surface, pm.current().generation) == ("mac", 3)
    assert pm.evict("phone") is True
    assert pm.handoff("h1").state == REVERTED
    assert pm.open_handoff() is None
    assert (pm.current().surface, pm.current().generation) == ("mac", 4)
    clock.advance(ACK_TIMEOUT_S + 1)
    assert pm.tick() is None
    assert (pm.current().surface, pm.current().generation) == ("mac", 4)
    for g in (2, 3, 4):
        with pytest.raises(StalePresence):
            surface_write(pm, "phone", g)
    with pytest.raises(StalePresence):
        pm.ack_in("h1", "mac", 3)                                  # a late ack is fenced
    surface_write(pm, "mac", 4)


def test_evicting_an_uninvolved_surface_changes_nothing(pm):
    pm.request_jack("h1", "phone")
    assert pm.evict("tablet") is False
    assert pm.handoff("h1").state == PREPARING
    assert pm.current().generation == 1
