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

    h = pm.ready_out("h1", draft="half typed")
    assert (h.state, h.to_gen, h.draft) == (COMMITTED, 2, "half typed")
    assert (pm.current().surface, pm.current().generation) == ("phone", 2)
    with pytest.raises(StalePresence):
        surface_write(pm, "mac", 1)                       # P2: the ghost cannot act
    assert pm.ack_in("h1", "phone", 2).state == ACKED
    assert pm.ack_in("h1", "phone", 2).state == ACKED     # idempotent

    pm.request_jack("h2", "mac")
    pm.ready_out("h2")
    pm.ack_in("h2", "mac", 3)
    assert (pm.current().surface, pm.current().generation) == ("mac", 3)


def test_ready_out_is_idempotent(pm):
    pm.request_jack("h1", "phone")
    first = pm.ready_out("h1", draft="a")
    again = pm.ready_out("h1", draft="b")
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
    pm.ready_out("h1")
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
    pm.ready_out("h1")
    assert pm.surface_lost("phone").state == REVERTED
    assert (pm.current().surface, pm.current().generation) == ("mac", 3)


def test_failed_jack_home_falls_back_to_the_source(pm, clock):
    pm.request_jack("h1", "phone")
    pm.ready_out("h1")
    pm.ack_in("h1", "phone", 2)
    pm.request_jack("h2", "mac")
    pm.ready_out("h2")
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
        pm.ready_out("h1")
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
