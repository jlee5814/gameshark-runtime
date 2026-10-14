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
"""

# Columns added after a table first shipped. Applied idempotently so a v0.1 or early v0.2
# database upgrades in place without losing rows.
MIGRATIONS: dict[str, list[tuple[str, str]]] = {
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
    ],
    "notifications": [
        ("url", "TEXT"),
        ("read_at", "REAL"),
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
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn
