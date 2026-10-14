"""Persistent agent identity.

Assigned once, reloaded forever (invariant I1). The record holds no secrets; real
secret material would live in the OS keychain and be referenced, not stored here.
"""

from __future__ import annotations

import sqlite3
import uuid as uuidlib
from dataclasses import dataclass

from gameshark.clock import Clock
from gameshark.store import write_txn


@dataclass(frozen=True)
class Identity:
    agent_id: str
    uuid: str
    created: float


def load_or_create_identity(conn: sqlite3.Connection, agent_id: str, clock: Clock) -> Identity:
    row = conn.execute(
        "SELECT agent_id, uuid, created FROM identity WHERE agent_id=?", (agent_id,)
    ).fetchone()
    if row is not None:
        return Identity(row["agent_id"], row["uuid"], row["created"])

    ident = Identity(agent_id=agent_id, uuid=str(uuidlib.uuid4()), created=clock.now())
    with write_txn(conn):
        # Guard against a race: another writer may have inserted first.
        existing = conn.execute(
            "SELECT uuid, created FROM identity WHERE agent_id=?", (agent_id,)
        ).fetchone()
        if existing is not None:
            return Identity(agent_id, existing["uuid"], existing["created"])
        conn.execute(
            "INSERT INTO identity(agent_id, uuid, created) VALUES(?,?,?)",
            (ident.agent_id, ident.uuid, ident.created),
        )
    return ident
