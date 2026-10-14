"""Companion feed: the local delivery surface for Stark's updates.

Writes are deduplicated by op_key, so a logical GitHub event shows exactly once even if
the notification effect is retried during recovery. The feed survives restart because it
lives in the same durable database. A tiny CLI renders the ordered stream.

Run the feed view: uv run python companion.py <runtime_db>
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path


class Companion:
    """Durable notification sink. Reuses the runtime connection; op_key is the dedup anchor."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def write(self, op_key: str, repo: str, pr: int, kind: str, event_id: str,
              summary: str, reason: str, fence: int = 0) -> str:
        # Idempotent by op_key; a retried delivery is a no op (fence accepted but local).
        self.conn.execute(
            "INSERT OR IGNORE INTO notifications(op_key, repo, pr, kind, event_id, summary, "
            "reason, created) VALUES(?,?,?,?,?,?,?,?)",
            (op_key, repo, pr, kind, event_id, summary, reason, time.time()),
        )
        return op_key

    def has(self, op_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM notifications WHERE op_key=?", (op_key,)
        ).fetchone()
        return row is not None

    def count(self, op_key: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM notifications WHERE op_key=?", (op_key,)
        ).fetchone()["c"]

    def feed(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM notifications ORDER BY created, op_key"
        ).fetchall()


def _render(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = Companion(conn).feed()
    if not rows:
        print("(no updates yet)")
        return
    print(f"Companion feed ({len(rows)} updates)")
    for r in rows:
        print(f"  [{r['kind']}] {r['repo']}#{r['pr']}  {r['summary']}  — {r['reason']}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python companion.py <runtime_db>")
        raise SystemExit(2)
    _render(str(Path(sys.argv[1])))
