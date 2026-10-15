"""Companion chat: replies commit through the journal, a lost reply event heals, and a
CLI supervisor that does not serve leaves reply events for the daemon."""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.clock import Clock
from gameshark.conversation import SurfaceConversation
from gameshark.inference.journal import InferenceJournal
from gameshark.inference.types import Privacy, Role
from gameshark.lease import LeaseManager
from gameshark.presence import PresenceManager
from gameshark.stark_chat import CONVERSATION, SYSTEM, StarkChatAdapter, reply_event
from gameshark.store import connect
from gameshark.supervisor import AgentSupervisor
from tests.gateway_runtime import Runtime, short_dir
from tests.helpers import AGENT, HOST


def roles(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute(
            "SELECT role FROM messages WHERE conversation_id=? ORDER BY seq", (CONVERSATION,))]
    finally:
        conn.close()


def user_row_without_event(db: Path) -> None:
    """The state a crash leaves between the surface write and the reply event insert."""
    conn = connect(db)
    clock = Clock()
    lease = LeaseManager(conn, AGENT, clock)
    gen = lease.acquire(HOST, 30.0)
    pm = PresenceManager(conn, AGENT, lease, gen, clock)
    pm.ensure_home("mac")
    journal = InferenceJournal(conn, AGENT, lease, gen, clock)
    journal.open_conversation(CONVERSATION, Privacy.LOCAL_ONLY)
    journal.append(CONVERSATION, Role.SYSTEM, SYSTEM, 0)
    SurfaceConversation(conn, AGENT, lease, gen, pm, clock).submit(
        "mac", 1, "m1", CONVERSATION, "did you get this")
    lease.release(HOST, gen)
    conn.close()


def test_lost_reply_event_heals_on_the_next_tick(tmp_path):
    db = tmp_path / "runtime.db"
    user_row_without_event(db)
    rt = Runtime(db, short_dir()).start()
    try:
        end = time.monotonic() + 10
        while roles(db)[-1] != "assistant" and time.monotonic() < end:
            time.sleep(0.05)
        assert roles(db) == ["system", "user", "assistant"]
    finally:
        rt.close()


def test_non_serving_supervisor_leaves_reply_pending(tmp_path):
    db = tmp_path / "runtime.db"
    user_row_without_event(db)
    sup = AgentSupervisor(db_path=db, agent_id=AGENT, owner_token=HOST)
    sup.add_adapter(StarkChatAdapter(serve=False))
    sup.enable_presence("mac")
    sup.start()
    sup.poll()
    assert sup.router.status(reply_event(1)["event_id"]) == "pending"
    assert roles(db) == ["system", "user"]
    sup.stop()
    sup.close()
