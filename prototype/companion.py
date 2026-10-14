"""Companion feed: the local delivery surface for Stark's updates.

Writes are deduplicated by op_key, so a logical GitHub event shows exactly once even if
the notification effect is retried during recovery. The feed survives restart because it
lives in the same durable database. Each row stays unread until acknowledged, so an
update delivered while nobody watched the terminal waits instead of scrolling away.

An optional on_new hook (a macOS banner, for example) fires only after a row is newly
inserted. The row is the source of truth; the banner is best effort and may repeat if the
process dies between insert and journal outcome.

Run the feed view: uv run python companion.py <runtime_db>
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path


class Companion:
    """Durable notification sink. Reuses the runtime connection; op_key is the dedup anchor."""

    def __init__(self, conn: sqlite3.Connection,
                 on_new: Callable[[dict], None] | None = None) -> None:
        self.conn = conn
        self.on_new = on_new

    def write(self, op_key: str, repo: str, pr: int, kind: str, event_id: str,
              summary: str, reason: str, fence: int = 0, url: str | None = None) -> str:
        # Idempotent by op_key; a retried delivery is a no op (fence accepted but local).
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO notifications(op_key, repo, pr, kind, event_id, summary, "
            "reason, created, url) VALUES(?,?,?,?,?,?,?,?,?)",
            (op_key, repo, pr, kind, event_id, summary, reason, time.time(), url),
        )
        if cur.rowcount == 1 and self.on_new is not None:
            try:
                self.on_new({"repo": repo, "pr": pr, "kind": kind, "summary": summary,
                             "reason": reason, "url": url})
            except Exception:  # noqa: S110
                pass                      # a banner failure never fails a durable delivery
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
            "SELECT rowid AS id, * FROM notifications ORDER BY created, op_key"
        ).fetchall()

    def unread(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT rowid AS id, * FROM notifications WHERE read_at IS NULL "
            "ORDER BY created, op_key"
        ).fetchall()

    def ack(self, ids: list[int] | None = None) -> int:
        """Mark rows read. None acknowledges everything unread."""
        now = time.time()
        if ids is None:
            cur = self.conn.execute(
                "UPDATE notifications SET read_at=? WHERE read_at IS NULL", (now,))
        else:
            marks = ",".join("?" for _ in ids)
            cur = self.conn.execute(
                f"UPDATE notifications SET read_at=? WHERE read_at IS NULL AND rowid IN ({marks})",
                (now, *ids),
            )
        return cur.rowcount


def macos_banner(note: dict) -> None:
    """Best effort desktop banner through osascript; silent where unavailable."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return
    title = f"Stark: {note['repo']}#{note['pr']}"
    text = note["summary"].replace('"', "'")[:200]
    script = f'display notification "{text}" with title "{title}"'
    subprocess.run(["osascript", "-e", script], check=False, timeout=5, capture_output=True)


def render(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "(no updates)"
    lines = []
    for r in rows:
        mark = " " if r["read_at"] else "*"
        when = time.strftime("%m-%d %H:%M", time.localtime(r["created"]))
        lines.append(f"{mark} {r['id']:>4} {when} [{r['kind']}] {r['repo']}#{r['pr']}  "
                     f"{r['summary']}")
        if r["url"]:
            lines.append(f"{'':12}{r['url']}")
    return "\n".join(lines)


def _render(db_path: str) -> None:
    from gameshark.store import connect

    conn = connect(db_path)                  # applies additive migrations to older files
    rows = Companion(conn).feed()
    print(f"Companion feed ({len(rows)} updates)" if rows else "(no updates yet)")
    if rows:
        print(render(rows))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python companion.py <runtime_db>")
        raise SystemExit(2)
    _render(str(Path(sys.argv[1])))
