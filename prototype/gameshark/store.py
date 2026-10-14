"""SQLite connection and schema.

The database file is the single authoritative source of resume truth. Memory is
only a cache of it. A restart is modeled by opening a fresh connection on the same
file, so no in memory state survives.
"""

from __future__ import annotations

import sqlite3
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
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with sane durability settings and the schema applied."""
    conn = sqlite3.connect(str(path), isolation_level=None)  # explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.executescript(SCHEMA)
    return conn
