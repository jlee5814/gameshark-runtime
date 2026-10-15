"""Surface writes: dedup by client_msg_id, presence fencing, and gap free resync."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.conversation import SurfaceConversation
from gameshark.errors import StalePresence
from gameshark.inference.journal import InferenceJournal
from gameshark.inference.types import InvalidRequest
from gameshark.lease import LeaseManager
from gameshark.presence import PresenceManager
from gameshark.store import connect
from tests.helpers import AGENT, HOST

CONV = "stark:main"


def boot(db: Path, clock, crash=None) -> SurfaceConversation:
    conn = connect(db)
    lease = LeaseManager(conn, AGENT, clock)
    gen = lease.acquire(HOST, 30.0)
    pm = PresenceManager(conn, AGENT, lease, gen, clock)
    pm.ensure_home("mac")
    InferenceJournal(conn, AGENT, lease, gen, clock).open_conversation(CONV)
    return SurfaceConversation(conn, AGENT, lease, gen, pm, clock, crash=crash)


@pytest.fixture
def sc(paths, clock) -> SurfaceConversation:
    return boot(paths["runtime"], clock)


def count(sc: SurfaceConversation) -> int:
    return sc.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def test_resend_lands_once(sc):
    a = sc.submit("mac", 1, "m1", CONV, "hello")
    b = sc.submit("mac", 1, "m1", CONV, "hello")
    assert (a.seq, a.replayed, b.seq, b.replayed) == (0, False, 0, True)
    assert count(sc) == 1


def test_stale_surface_writes_nothing(sc):
    with pytest.raises(StalePresence):
        sc.submit("phone", 1, "m1", CONV, "not mine yet")
    assert count(sc) == 0


def test_landed_message_survives_a_jack(sc):
    sc.submit("mac", 1, "m1", CONV, "before jack")
    sc.presence.request_jack("h1", "phone")
    sc.presence.ready_out("h1")
    assert sc.submit("mac", 1, "m1", CONV, "before jack").replayed   # dedup beats stale
    with pytest.raises(StalePresence):
        sc.submit("mac", 1, "m2", CONV, "after jack")
    assert sc.submit("phone", 2, "m3", CONV, "from phone").seq == 1


def test_client_id_cannot_switch_conversation(sc):
    sc.submit("mac", 1, "m1", CONV, "x")
    with pytest.raises(InvalidRequest):
        sc.submit("mac", 1, "m1", "other", "x")


def test_since_returns_exactly_the_gap(sc):
    for i in range(5):
        sc.submit("mac", 1, f"m{i}", CONV, f"msg {i}")
    assert [r.seq for r in sc.since(CONV, 2)] == [3, 4]
    assert [r.seq for r in sc.since(CONV, -1)] == [0, 1, 2, 3, 4]
    assert sc.since(CONV, 4) == []


def test_crash_after_submit_then_resend_after_restart(paths, clock):
    class Crash(Exception):
        pass

    def crash(label: str) -> None:
        if label == "S1_after_submit":
            raise Crash

    first = boot(paths["runtime"], clock, crash=crash)
    with pytest.raises(Crash):
        first.submit("mac", 1, "m1", CONV, "hello")      # durable, but the ack was lost
    first.conn.close()

    again = boot(paths["runtime"], clock)
    r = again.submit("mac", 1, "m1", CONV, "hello")      # the client resends
    assert (r.seq, r.replayed) == (0, True)
    assert count(again) == 1
