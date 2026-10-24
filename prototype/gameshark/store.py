"""SQLite connection and schema.

The database file is the single authoritative source of resume truth. Memory is
only a cache of it. A restart is modeled by opening a fresh connection on the same
file, so no in memory state survives.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS identity (
    agent_id TEXT PRIMARY KEY,
    uuid     TEXT NOT NULL,
    created  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lease (
    agent_id   TEXT PRIMARY KEY,
    owner_token TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS grants (
    agent_id TEXT NOT NULL,
    adapter  TEXT NOT NULL,
    cap      TEXT NOT NULL,
    granted  INTEGER NOT NULL DEFAULT 1,   -- 1 granted, 0 revoked; revocation survives restart
    updated  REAL NOT NULL,
    PRIMARY KEY (agent_id, adapter, cap)
);

CREATE TABLE IF NOT EXISTS responsibilities (
    id        TEXT PRIMARY KEY,
    agent_id  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    state     TEXT NOT NULL,            -- pending | active | done | blocked
    payload   TEXT NOT NULL,           -- JSON
    checkpoint TEXT,
    created   REAL NOT NULL,
    updated   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id       TEXT PRIMARY KEY,   -- dedup anchor
    adapter        TEXT NOT NULL,
    type           TEXT NOT NULL,
    payload        TEXT NOT NULL,      -- JSON
    status         TEXT NOT NULL,      -- pending | acked | dead
    delivered_count INTEGER NOT NULL DEFAULT 0,
    created        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS effects (
    key         TEXT PRIMARY KEY,      -- stable idempotency key
    agent_id    TEXT NOT NULL,
    generation  INTEGER NOT NULL,
    status      TEXT NOT NULL,         -- pending | done | blocked
    external_id TEXT,
    intent      TEXT NOT NULL,         -- JSON, enough to rebuild the effect on recovery
    created     REAL NOT NULL,
    updated     REAL NOT NULL
);

-- v0.2: durable monitor cursor and authorization scope per monitored pull request.
CREATE TABLE IF NOT EXISTS monitor_state (
    agent_id    TEXT NOT NULL,
    repo        TEXT NOT NULL,
    pr          INTEGER NOT NULL,
    etag        TEXT,
    last_cursor TEXT NOT NULL DEFAULT '',   -- highest logical event id processed
    scope       TEXT NOT NULL,              -- recorded authorization scope
    updated     REAL NOT NULL,
    PRIMARY KEY (agent_id, repo, pr)
);

-- v0.2: companion feed; deduplicated by op_key so a logical event shows once.
CREATE TABLE IF NOT EXISTS notifications (
    op_key   TEXT PRIMARY KEY,
    repo     TEXT NOT NULL,
    pr       INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    event_id TEXT NOT NULL,
    summary  TEXT NOT NULL,
    reason   TEXT NOT NULL,
    created  REAL NOT NULL
);

-- v0.2: every classification decision, relevant or not, kept for audit.
CREATE TABLE IF NOT EXISTS classification_audit (
    agent_id TEXT NOT NULL,
    repo     TEXT NOT NULL,
    pr       INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    kind     TEXT NOT NULL,
    relevant INTEGER NOT NULL,
    reason   TEXT NOT NULL,
    created  REAL NOT NULL,
    PRIMARY KEY (agent_id, repo, pr, event_id)
);

-- v0.2 live: logical events already submitted per pull request. Novelty is set
-- membership, not id order, so independent id spaces never hide an event.
CREATE TABLE IF NOT EXISTS monitor_seen (
    agent_id TEXT NOT NULL,
    repo     TEXT NOT NULL,
    pr       INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    created  REAL NOT NULL,
    PRIMARY KEY (agent_id, repo, pr, event_id)
);

-- v0.2 live: last conclusion per check name, so a pass after a failure reads as recovery.
CREATE TABLE IF NOT EXISTS monitor_checks (
    agent_id   TEXT NOT NULL,
    repo       TEXT NOT NULL,
    pr         INTEGER NOT NULL,
    name       TEXT NOT NULL,
    conclusion TEXT NOT NULL,
    PRIMARY KEY (agent_id, repo, pr, name)
);

-- v0.2 live: operator requests (watch, unwatch, resume) written by the CLI without a
-- lease and applied by the lease holder, so a CLI never competes with the daemon.
CREATE TABLE IF NOT EXISTS control_requests (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    adapter  TEXT NOT NULL,
    action   TEXT NOT NULL,
    payload  TEXT NOT NULL,           -- JSON
    status   TEXT NOT NULL,           -- pending | applied | rejected
    detail   TEXT,
    created  REAL NOT NULL,
    applied  REAL
);

-- v0.3: inference journal. Conversations belong to the agent identity, never to a model.
CREATE TABLE IF NOT EXISTS conversations (
    id                TEXT PRIMARY KEY,
    agent_id          TEXT NOT NULL,          -- owner is the identity, never a model
    responsibility_id TEXT,                   -- owning duty, if any
    privacy           TEXT NOT NULL,          -- local_only | cloud_ok
    next_seq          INTEGER NOT NULL DEFAULT 0,
    created           REAL NOT NULL,
    updated           REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    conversation_id TEXT NOT NULL,
    seq             INTEGER NOT NULL,         -- contiguous from 0
    role            TEXT NOT NULL,            -- system | user | assistant | tool
    content         TEXT NOT NULL,
    call_id         TEXT UNIQUE,              -- set on assistant rows; one per call
    generation      INTEGER NOT NULL,         -- lease generation that wrote it
    created         REAL NOT NULL,
    PRIMARY KEY (conversation_id, seq)
);

CREATE TABLE IF NOT EXISTS inference_calls (
    call_id         TEXT PRIMARY KEY,         -- stable: hash of responsibility or event and step
    agent_id        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    base_seq        INTEGER NOT NULL,         -- context = messages with seq < base_seq
    request         TEXT NOT NULL,            -- JSON: needs, max_tokens, temperature; no secrets
    status          TEXT NOT NULL,            -- pending | done | failed | cancelled
    attempts        INTEGER NOT NULL DEFAULT 0,
    provider        TEXT,                     -- answering provider and model, set at done
    model           TEXT,
    instance_ref    TEXT,
    degraded        INTEGER NOT NULL DEFAULT 0,
    error_kind      TEXT,
    generation      INTEGER NOT NULL,
    created         REAL NOT NULL,
    updated         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_ledger (
    call_id        TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    agent_id       TEXT NOT NULL,
    provider       TEXT NOT NULL,
    window_start   REAL NOT NULL,
    reserved_tokens INTEGER NOT NULL,
    reserved_micros INTEGER NOT NULL,
    tokens_in      INTEGER,
    tokens_out     INTEGER,
    cost_micros    INTEGER,
    status         TEXT NOT NULL,             -- reserved | settled | lost
    PRIMARY KEY (call_id, attempt)
);

CREATE TABLE IF NOT EXISTS agent_budgets (
    agent_id        TEXT NOT NULL,
    window_s        REAL NOT NULL,
    max_tokens      INTEGER NOT NULL,
    max_cost_micros INTEGER NOT NULL,
    PRIMARY KEY (agent_id, window_s)
);

-- v0.4: presence. One surface (the Mac panel, a paired phone) is Stark's active body.
-- The generation fences surface writes the way lease.generation fences owners.
CREATE TABLE IF NOT EXISTS presence (
    agent_id   TEXT PRIMARY KEY,
    surface    TEXT NOT NULL,
    home       TEXT NOT NULL,            -- surface presence reverts to after a failed jack
    generation INTEGER NOT NULL,
    updated    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS handoffs (
    id             TEXT PRIMARY KEY,     -- chosen by the requester; retries reuse it
    agent_id       TEXT NOT NULL,
    from_surface   TEXT NOT NULL,
    to_surface     TEXT NOT NULL,
    from_gen       INTEGER NOT NULL,     -- presence generation the jack started under
    to_gen         INTEGER,              -- generation granted at commit
    state          TEXT NOT NULL,        -- preparing | committed | acked | reverted | aborted
    draft          TEXT,                 -- unsent text carried from source to target
    prepare_deadline REAL NOT NULL,
    ack_deadline   REAL,
    created        REAL NOT NULL,
    updated        REAL NOT NULL
);

-- v0.4: surface writes, deduplicated by the client chosen id so a resend lands once.
CREATE TABLE IF NOT EXISTS surface_writes (
    agent_id        TEXT NOT NULL,
    client_msg_id   TEXT NOT NULL,
    surface         TEXT NOT NULL,
    presence_gen    INTEGER NOT NULL,
    conversation_id TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    created         REAL NOT NULL,
    PRIMARY KEY (agent_id, client_msg_id)
);

-- v0.4: paired devices (Stark Pocket). The operator CLI writes pairings and revocations;
-- the runtime consumes a pairing and creates its device in one fenced transaction. Only
-- public keys are stored: a leaked database cannot impersonate a phone.
CREATE TABLE IF NOT EXISTS devices (
    id         TEXT PRIMARY KEY,         -- 32 hex chars, minted at pairing
    agent_id   TEXT NOT NULL,
    name       TEXT NOT NULL,
    surface    TEXT NOT NULL UNIQUE,     -- pocket-xxxxxx, bound at every hello
    public_key BLOB NOT NULL,            -- P-256, X9.62 uncompressed (65 bytes)
    created    REAL NOT NULL,
    revoked_at REAL,
    last_seen  REAL
);

CREATE TABLE IF NOT EXISTS pairings (
    id            TEXT PRIMARY KEY,
    agent_id      TEXT NOT NULL,
    secret_sha256 TEXT NOT NULL,         -- the secret itself lives only in the pairing link
    name          TEXT NOT NULL,
    expires       REAL NOT NULL,
    state         TEXT NOT NULL,         -- pending | used
    device_id     TEXT,
    created       REAL NOT NULL
);

-- v0.5: persistent memory (memory.py). Facts the user explicitly saved through a typed
-- surface command, owned by the identity like conversations. Never model output.
CREATE TABLE IF NOT EXISTS memories (
    id               TEXT PRIMARY KEY,     -- mem-<32 hex>, stable across corrections
    agent_id         TEXT NOT NULL,
    scope            TEXT NOT NULL,        -- global (every Stark conversation)
    privacy          TEXT NOT NULL,        -- local_only | cloud_ok
    state            TEXT NOT NULL,        -- active | forgotten
    content          TEXT NOT NULL,        -- '' once forgotten
    revision         INTEGER NOT NULL,     -- 1, then one more per correction
    source_surface   TEXT NOT NULL,        -- provenance: mac, pocket-xxxxxx, operator-cli
    source_device_id TEXT,
    created          REAL NOT NULL,
    updated          REAL NOT NULL,
    forgotten_at     REAL,
    generation       INTEGER NOT NULL      -- lease generation of the last write
);

-- Memory writes, deduplicated by the client chosen op_id so a resend lands once. The
-- hash covers only the memory's current text; correct and forget null older ones.
CREATE TABLE IF NOT EXISTS memory_ops (
    agent_id         TEXT NOT NULL,
    op_id            TEXT NOT NULL,
    kind             TEXT NOT NULL,        -- remember | correct | forget
    memory_id        TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    content_sha256   TEXT,
    surface          TEXT NOT NULL,
    history_mentions INTEGER NOT NULL DEFAULT 0,
    created          REAL NOT NULL,
    PRIMARY KEY (agent_id, op_id)
);

-- v0.6: avatar loadout (loadout.py). What Stark wears on every surface: presentation
-- only. One row per identity; a missing row means default with no override.
CREATE TABLE IF NOT EXISTS avatar_loadout (
    agent_id         TEXT PRIMARY KEY,
    inferred         TEXT NOT NULL,        -- default | gym, from the newest explicit message
    inferred_seq     INTEGER,              -- the user row that stated it
    override         TEXT,                 -- NULL (auto) | default | gym, pinned by hand
    override_surface TEXT,
    updated          REAL NOT NULL,
    generation       INTEGER NOT NULL      -- lease generation of the last write
);

-- Override writes, deduplicated by the client chosen op_id so a resend lands once.
CREATE TABLE IF NOT EXISTS loadout_ops (
    agent_id         TEXT NOT NULL,
    op_id            TEXT NOT NULL,
    override         TEXT,                 -- the value requested; NULL clears the pin
    surface          TEXT NOT NULL,
    created          REAL NOT NULL,
    PRIMARY KEY (agent_id, op_id)
);
"""

# Columns added after a table first shipped. Applied idempotently so a v0.1 or early v0.2
# database upgrades in place without losing rows.
MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "lease": [
        ("state", "TEXT NOT NULL DEFAULT 'held'"),     # held | draining | released
    ],
    "monitor_state": [
        ("status", "TEXT NOT NULL DEFAULT 'ok'"),      # ok | backoff | auth_revoked | closed
        ("next_poll_at", "REAL NOT NULL DEFAULT 0"),
        ("fail_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_ok_at", "REAL"),
        ("last_error", "TEXT"),
        ("etags", "TEXT NOT NULL DEFAULT '{}'"),       # JSON url -> etag, survives restart
        ("head_sha", "TEXT"),
        ("viewer", "TEXT"),
        ("baseline_before", "TEXT"),                   # ISO time; older activity is history
        ("episode", "INTEGER NOT NULL DEFAULT 0"),     # auth failure episodes, dedup anchor
        ("page_cursors", "TEXT NOT NULL DEFAULT '{}'"),  # JSON stream -> pagination cursor
    ],
    "notifications": [
        ("url", "TEXT"),
        ("read_at", "REAL"),
    ],
    # v0.3 step 6: the answer's finish reason, so a replayed call can still be judged
    # complete or truncated before anything acts on it.
    "inference_calls": [
        ("finish_reason", "TEXT"),                      # stop | length | tool_calls
    ],
    # v0.4: a carried draft waits for its target's receipt.
    "handoffs": [
        ("draft_state", "TEXT"),                        # NULL (no draft) | pending | delivered
    ],
}


@contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run the body in one BEGIN IMMEDIATE transaction; commit on success.

    On any failure, roll back at most once and only while a transaction is still open,
    then reraise the original exception. A failed rollback is attached as a note and
    never replaces the error that caused it, so FencedError always reaches its caller.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException as exc:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error as rb:
                exc.add_note(f"rollback failed: {rb!r}")
        raise


def migrate(conn: sqlite3.Connection) -> None:
    """Add missing columns inside one write txn so a concurrent CLI and daemon agree."""
    with write_txn(conn):
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with sane durability settings and the schema applied."""
    conn = sqlite3.connect(str(path), isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=3000")
    # Freed cells are zeroed, so a forgotten or corrected memory leaves no plaintext behind.
    conn.execute("PRAGMA secure_delete=ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn
