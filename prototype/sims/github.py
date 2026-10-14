"""Mock GitHub simulator and adapter.

Stark monitors a simulated pull request. A review request triggers a responsibility to
post exactly one comment carrying a stable operation id. The mock remote keeps its own
durable comment log, separate from the agent journal, and models two realistic hazards:

- ambiguous mode: a probe cannot confirm state and returns UNKNOWN, which must block and
  escalate rather than guess (review issue 1).
- delayed visibility: a write lands but a probe reports ABSENT until visibility is
  toggled, modeling eventual consistency. Destination side idempotency by operation id
  keeps a re dispatched action from duplicating.

The remote also enforces a fence per operation (review issue 3): a write carrying a
fence older than one already accepted is rejected, which is the only thing that can stop
a stale worker from producing an external effect.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from gameshark.adapters import AdapterContext
from gameshark.effects import Effect, ProbeResult
from gameshark.errors import AmbiguousEffect, StaleFenceRejected

GITHUB_SCHEMA = """
CREATE TABLE IF NOT EXISTS comments (
    op_key TEXT PRIMARY KEY,     -- stable operation id; remote dedup anchor
    repo   TEXT NOT NULL,
    pr     INTEGER NOT NULL,
    body   TEXT NOT NULL,
    fence  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fences (
    op_key TEXT PRIMARY KEY,
    fence  INTEGER NOT NULL      -- highest fence the destination has accepted for this op
);
"""


class MockGitHub:
    """External review service with its own durable comment log and fence ledger."""

    def __init__(
        self, db_path: str | Path, ambiguous: bool = False, visible: bool = True
    ) -> None:
        self.conn = sqlite3.connect(str(db_path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=3000")
        self.conn.executescript(GITHUB_SCHEMA)
        self.ambiguous = ambiguous      # probe returns UNKNOWN
        self.visible = visible          # when False, probe hides a landed write (ABSENT)

    def _check_fence(self, op_key: str, fence: int) -> None:
        row = self.conn.execute(
            "SELECT fence FROM fences WHERE op_key=?", (op_key,)
        ).fetchone()
        if row is not None and fence < row["fence"]:
            raise StaleFenceRejected(
                f"fence {fence} < accepted {row['fence']} for {op_key}"
            )

    def post_comment(self, op_key: str, repo: str, pr: int, body: str, fence: int) -> str:
        # Destination side fence: reject a stale worker even if local fencing missed it.
        self._check_fence(op_key, fence)
        # Idempotent by op key: a repeated post is a no op, the dedup anchor.
        self.conn.execute(
            "INSERT OR IGNORE INTO comments(op_key, repo, pr, body, fence) VALUES(?,?,?,?,?)",
            (op_key, repo, pr, body, fence),
        )
        self.conn.execute(
            "INSERT INTO fences(op_key, fence) VALUES(?,?) "
            "ON CONFLICT(op_key) DO UPDATE SET fence=MAX(fence, excluded.fence)",
            (op_key, fence),
        )
        return op_key

    def probe(self, op_key: str) -> ProbeResult:
        if self.ambiguous:
            return ProbeResult.unknown()
        row = self.conn.execute(
            "SELECT op_key FROM comments WHERE op_key=?", (op_key,)
        ).fetchone()
        if row is not None and self.visible:
            return ProbeResult.found(row["op_key"])
        return ProbeResult.absent()

    def comment_count(self, op_key: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM comments WHERE op_key=?", (op_key,)
        ).fetchone()["c"]


class PrAdapter:
    name = "pr"
    required_caps = frozenset({"pr.observe", "pr.comment"})

    def __init__(self, remote: MockGitHub) -> None:
        self.remote = remote
        self.ctx: AdapterContext | None = None
        self.connected = False

    def init(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self.connected = True

    def reconnect(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self.connected = True

    def teardown(self) -> None:
        self.connected = False

    def _effect(self, repo: str, pr: int, trigger: str, responsibility_id: str) -> Effect:
        op_key = f"pr:{repo}:{pr}:{trigger}"
        intent = {
            "adapter": self.name,
            "repo": repo,
            "pr": pr,
            "trigger": trigger,
            "responsibility_id": responsibility_id,
        }
        return Effect(
            key=op_key,
            intent=intent,
            required_cap="pr.comment",
            adapter=self.name,
            do=lambda fence: self.remote.post_comment(
                op_key, repo, pr, body="Reviewed by Stark", fence=fence
            ),
            probe=lambda: self.remote.probe(op_key),
        )

    def build_effect(self, intent: dict) -> Effect:
        return self._effect(
            intent["repo"], int(intent["pr"]), intent["trigger"], intent["responsibility_id"]
        )

    def on_event(self, event: dict) -> None:
        assert self.ctx is not None
        if "pr.comment" not in self.ctx.caps:
            raise PermissionError("pr adapter lacks pr.comment")
        p = event["payload"]
        repo, pr, trigger = p["repo"], int(p["pr"]), p["trigger"]
        rid = f"pr:{repo}:{pr}"
        self.ctx.responsibilities.upsert(
            rid, kind="pr-review", payload={"repo": repo, "pr": pr}, state="active"
        )
        eff = self._effect(repo, pr, trigger, rid)
        try:
            self.ctx.emit_effect(eff)
        except AmbiguousEffect:
            # Refuse to guess: escalate the responsibility, let the router mark the event dead.
            self.ctx.responsibilities.transition(rid, "blocked", checkpoint="ambiguous")
            raise
        self.ctx.responsibilities.transition(rid, "done", checkpoint=trigger)
