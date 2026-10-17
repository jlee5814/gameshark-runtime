"""Persistent memory store: typed, fenced, idempotent writes; honest forgetting."""

from __future__ import annotations

import ast
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.authz import GrantStore
from gameshark.errors import FencedError, StalePresence
from gameshark.inference.journal import InferenceJournal
from gameshark.inference.types import Role
from gameshark.lease import LeaseManager
from gameshark.memory import (
    ACTIVE,
    FORGOTTEN,
    GRANT_ADAPTER,
    INJECT,
    MAX_ACTIVE,
    MAX_TEXT,
    WRITE,
    MemoryConflict,
    MemoryLimit,
    MemoryStore,
)
from gameshark.presence import PresenceManager
from gameshark.store import connect
from tests.helpers import AGENT, HOST

ROOT = Path(__file__).resolve().parents[1]


class Crash(Exception):
    pass


def boot(db: Path, clock, owner: str = HOST, crash=None) -> MemoryStore:
    conn = connect(db)
    lease = LeaseManager(conn, AGENT, clock)
    gen = lease.acquire(owner, 30.0)
    pm = PresenceManager(conn, AGENT, lease, gen, clock)
    pm.ensure_home("mac")
    grants = GrantStore(conn, AGENT, clock)
    return MemoryStore(conn, AGENT, lease, gen, pm, grants, clock, crash=crash)


@pytest.fixture
def ms(paths, clock) -> MemoryStore:
    return boot(paths["runtime"], clock)


def test_remember_persists_with_provenance(ms):
    m = ms.remember("mac", 1, "op1", "My sister is named Ana.")
    assert m.id.startswith("mem-") and len(m.id) == 36
    assert (m.state, m.revision, m.content, m.scope) == (ACTIVE, 1, "My sister is named Ana.",
                                                        "global")
    assert (m.source_surface, m.replayed) == ("mac", False)
    assert m.created == m.updated
    assert [x.id for x in ms.list()] == [m.id]


def test_remember_normalizes_whitespace(ms):
    m = ms.remember("mac", 1, "op1", "  coffee,\n\tno   sugar  ")
    assert m.content == "coffee, no sugar"


def test_resent_op_returns_the_first_result(ms):
    a = ms.remember("mac", 1, "op1", "likes tea")
    b = ms.remember("mac", 1, "op1", "likes tea")
    assert (b.id, b.replayed) == (a.id, True)
    assert len(ms.list()) == 1


def test_op_reused_for_other_text_is_refused(ms):
    ms.remember("mac", 1, "op1", "likes tea")
    with pytest.raises(ValueError):
        ms.remember("mac", 1, "op1", "likes coffee")
    assert [m.content for m in ms.list()] == ["likes tea"]


def test_same_text_under_a_new_op_returns_the_active_memory(ms):
    a = ms.remember("mac", 1, "op1", "likes tea")
    b = ms.remember("mac", 1, "op2", "likes   tea")
    assert (b.id, b.replayed) == (a.id, True)
    assert len(ms.list()) == 1


def test_remember_requires_presence(ms):
    with pytest.raises(StalePresence):
        ms.remember("pocket-abc", 1, "op1", "not my body")
    with pytest.raises(StalePresence):
        ms.remember("mac", 7, "op1", "wrong generation")
    assert ms.list() == []


def test_text_limits(ms):
    with pytest.raises(ValueError):
        ms.remember("mac", 1, "op1", "   ")
    with pytest.raises(ValueError):
        ms.remember("mac", 1, "op2", "x" * (MAX_TEXT + 1))
    with pytest.raises(ValueError):
        ms.remember("mac", 1, "op3", "bell\x07")
    assert ms.remember("mac", 1, "op4", "é" * (MAX_TEXT // 2)).revision == 1


def test_active_count_is_bounded(ms):
    for i in range(MAX_ACTIVE):
        ms.remember("mac", 1, f"op{i}", f"fact {i}")
    with pytest.raises(MemoryLimit):
        ms.remember("mac", 1, "one-more", "fact overflow")
    ms.forget("mac", "f0", ms.list()[0].id)
    assert ms.remember("mac", 1, "one-more", "fact overflow").state == ACTIVE


def test_correct_replaces_in_place_and_bumps_revision(ms):
    m = ms.remember("mac", 1, "op1", "lives in Boston")
    c = ms.correct("mac", 1, "op2", m.id, 1, "lives in Atlanta")
    assert (c.id, c.revision, c.content) == (m.id, 2, "lives in Atlanta")
    assert [x.content for x in ms.list()] == ["lives in Atlanta"]
    # The superseded text survives nowhere in the memory tables.
    dump = "\n".join(str(tuple(r)) for t in ("memories", "memory_ops")
                     for r in ms.conn.execute(f"SELECT * FROM {t}"))
    assert "Boston" not in dump


def test_correct_with_a_stale_revision_conflicts(ms):
    m = ms.remember("mac", 1, "op1", "v1")
    ms.correct("mac", 1, "op2", m.id, 1, "v2")
    with pytest.raises(MemoryConflict):
        ms.correct("mac", 1, "op3", m.id, 1, "v2 from a stale screen")
    assert ms.get(m.id).content == "v2"
    # A resend of the winning op still answers with its own result.
    assert ms.correct("mac", 1, "op2", m.id, 1, "v2").replayed


def test_correct_requires_presence(ms):
    m = ms.remember("mac", 1, "op1", "v1")
    with pytest.raises(StalePresence):
        ms.correct("pocket-abc", 1, "op2", m.id, 1, "v2")


def test_correct_of_a_forgotten_memory_is_refused(ms):
    m = ms.remember("mac", 1, "op1", "v1")
    ms.forget("mac", "op2", m.id)
    with pytest.raises(LookupError):
        ms.correct("mac", 1, "op3", m.id, 1, "v2")


def test_forget_wipes_content_and_is_idempotent(ms):
    m = ms.remember("mac", 1, "op1", "door code 4711")
    f = ms.forget("mac", "op2", m.id)
    assert (f.id, f.state, f.content, f.replayed) == (m.id, FORGOTTEN, "", False)
    assert f.forgotten_at is not None
    assert ms.list() == []
    again = ms.forget("pocket-abc", "op3", m.id)       # another surface, another op
    assert (again.state, again.replayed) == (FORGOTTEN, True)
    dump = "\n".join(str(tuple(r)) for t in ("memories", "memory_ops")
                     for r in ms.conn.execute(f"SELECT * FROM {t}"))
    assert "4711" not in dump


def test_forget_needs_no_presence(ms):
    m = ms.remember("mac", 1, "op1", "x")
    assert ms.forget("pocket-not-present", "op2", m.id).state == FORGOTTEN


def test_forget_of_an_unknown_id_is_refused(ms):
    with pytest.raises(LookupError):
        ms.forget("mac", "op1", "mem-" + "0" * 32)


def test_forget_reports_history_mentions(ms, paths, clock):
    journal = InferenceJournal(ms.conn, AGENT, ms.lease, ms.generation, clock)
    journal.open_conversation("stark:main")
    journal.append("stark:main", Role.SYSTEM, "sys", 0)
    journal.append("stark:main", Role.USER, "fyi my Door Code 4711 is new", 1)
    m = ms.remember("mac", 1, "op1", "door code 4711")
    assert ms.forget("mac", "op2", m.id).history_mentions == 1
    m2 = ms.remember("mac", 1, "op3", "unrelated fact")
    assert ms.forget("mac", "op4", m2.id).history_mentions == 0


def test_forgotten_plaintext_leaves_the_database_files(paths, clock):
    ms = boot(paths["runtime"], clock)
    secret = "zebra-quartz-" + "q" * 40
    m = ms.remember("mac", 1, "op1", secret)
    ms.forget("mac", "op2", m.id)
    ms.conn.close()
    for f in paths["runtime"].parent.iterdir():
        if f.name.startswith(paths["runtime"].name):
            assert secret.encode() not in f.read_bytes(), f.name


def test_write_grant_revocation_is_durable(paths, clock):
    ms = boot(paths["runtime"], clock)
    ms.grants.revoke(GRANT_ADAPTER, WRITE)
    with pytest.raises(PermissionError):
        ms.remember("mac", 1, "op1", "x")
    ms.conn.close()
    again = boot(paths["runtime"], clock)
    again.ensure_grants()
    with pytest.raises(PermissionError):
        again.remember("mac", 1, "op1", "x")
    assert again.inject_enabled()


def test_inject_grant_defaults_on_and_revocation_sticks(paths, clock):
    ms = boot(paths["runtime"], clock)
    ms.ensure_grants()
    assert ms.inject_enabled()
    ms.grants.revoke(GRANT_ADAPTER, INJECT)
    ms.ensure_grants()
    assert not ms.inject_enabled()


def test_stale_owner_writes_nothing(paths, clock):
    old = boot(paths["runtime"], clock)
    clock.advance(31.0)
    boot(paths["runtime"], clock, owner="other-host")
    with pytest.raises(FencedError):
        old.remember("mac", 1, "op1", "x")
    with pytest.raises(FencedError):
        old.forget("mac", "op2", "mem-" + "0" * 32)


def test_memories_survive_restart(paths, clock):
    a = boot(paths["runtime"], clock)
    m = a.remember("mac", 1, "op1", "plays chess on Sundays")
    a.conn.close()
    b = boot(paths["runtime"], clock)
    got = b.list()
    assert [(x.id, x.content, x.revision) for x in got] == [(m.id, m.content, 1)]
    assert b.remember("mac", 1, "op1", "plays chess on Sundays").replayed


def test_crash_after_write_then_resend_lands_once(paths, clock):
    def crash(label: str) -> None:
        if label == "M1_after_memory_write":
            raise Crash

    first = boot(paths["runtime"], clock, crash=crash)
    with pytest.raises(Crash):
        first.remember("mac", 1, "op1", "durable before the ack")
    first.conn.close()
    again = boot(paths["runtime"], clock)
    r = again.remember("mac", 1, "op1", "durable before the ack")
    assert r.replayed and len(again.list()) == 1


def test_crash_after_forget_wipe_keeps_it_forgotten(paths, clock):
    def crash(label: str) -> None:
        if label == "M2_after_forget_wipe":
            raise Crash

    first = boot(paths["runtime"], clock)
    m = first.remember("mac", 1, "op1", "short lived")
    first._crash = crash
    with pytest.raises(Crash):
        first.forget("mac", "op2", m.id)
    first.conn.close()
    again = boot(paths["runtime"], clock)
    assert again.list() == []
    assert again.forget("mac", "op2", m.id).replayed


def test_two_connections_race_one_revision(paths, clock):
    a = boot(paths["runtime"], clock)
    m = a.remember("mac", 1, "op1", "v1")
    conn = connect(paths["runtime"])
    lease = LeaseManager(conn, AGENT, clock)
    b = MemoryStore(conn, AGENT, lease, a.generation,
                    PresenceManager(conn, AGENT, lease, a.generation, clock),
                    GrantStore(conn, AGENT, clock), clock)
    a.correct("mac", 1, "op2", m.id, 1, "from a")
    with pytest.raises(MemoryConflict):
        b.correct("mac", 1, "op3", m.id, 1, "from b")
    assert a.get(m.id).content == "from a"


def test_model_output_has_no_path_to_memory_writes():
    """Only typed surface handlers and operator controls reach the write methods."""
    writers = {"remember", "correct", "forget"}
    allowed = {ROOT / "gameshark" / "memory.py", ROOT / "gameshark" / "surface_api.py",
               ROOT / "gameshark" / "stark_chat.py"}
    for f in (ROOT / "gameshark").rglob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in writers and isinstance(
                    node.ctx, ast.Load):
                parent = f.resolve()
                assert parent in allowed, f"{f}: .{node.attr} outside the typed paths"
    # stark_chat applies operator forget controls only; never remember or correct.
    chat = (ROOT / "gameshark" / "stark_chat.py").read_text()
    assert ".remember(" not in chat and ".correct(" not in chat
    # Nothing under inference/ touches memory writes at all.
    for f in (ROOT / "gameshark" / "inference").rglob("*.py"):
        text = f.read_text()
        assert ".remember(" not in text and ".forget(" not in text and ".correct(" not in text


def test_schema_upgrade_keeps_old_rows(paths):
    conn = sqlite3.connect(paths["runtime"])
    conn.execute("CREATE TABLE identity (agent_id TEXT PRIMARY KEY, uuid TEXT NOT NULL, "
                 "created REAL NOT NULL)")
    conn.execute("INSERT INTO identity VALUES('stark','u-1',1.0)")
    conn.commit()
    conn.close()
    c = connect(paths["runtime"])
    assert c.execute("SELECT uuid FROM identity").fetchone()[0] == "u-1"
    assert c.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert c.execute("PRAGMA secure_delete").fetchone()[0] == 1
