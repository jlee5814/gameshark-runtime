"""GitHub pull request monitor adapter (read only).

Stark accepts a durable monitor_pull_request responsibility, polls authorized pull
requests through a read only client, classifies activity deterministically, and delivers
relevant updates to the companion feed exactly once per logical event. All GitHub access
is read only; the adapter has no path to post, merge, or edit.

Correctness shape:
- Detected events flow through the durable inbox (dedup by logical id), so a re poll
  never produces a second notification.
- The notification effect is journaled (intent then outcome), so a crash mid delivery
  recovers without duplicating the companion entry.
- The cursor advances only after an event is durably handled, under a fenced write, so a
  stale worker cannot corrupt it and no event is skipped.
- Revoking github.read stops polling safely: no read, no notification, no crash.
"""

from __future__ import annotations

import time

from companion import Companion
from gameshark.adapters import AdapterContext
from gameshark.classify import classify
from gameshark.effects import Effect, ProbeResult
from gameshark.github_client import GitHubClient, RateLimited, TransientOutage


class GitHubMonitorAdapter:
    name = "github"
    required_caps = frozenset({"github.read", "companion.notify"})

    def __init__(self, client: GitHubClient) -> None:
        self.client = client
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

    # ------------------------------------------------------------------ responsibility
    def accept_responsibility(self, repo: str, pr: int, scope: str) -> str:
        """Persist a durable monitor_pull_request responsibility and its cursor."""
        assert self.ctx is not None
        rid = f"monitor:{repo}:{pr}"
        self.ctx.responsibilities.upsert(
            rid, kind="monitor_pull_request",
            payload={"repo": repo, "pr": pr, "scope": scope}, state="active",
        )
        now = time.time()

        def _w(conn):
            conn.execute(
                "INSERT OR IGNORE INTO monitor_state(agent_id, repo, pr, etag, last_cursor, "
                "scope, updated) VALUES(?,?,?,?,?,?,?)",
                (self.ctx.agent_id, repo, pr, None, "", scope, now),
            )

        self.ctx.fenced(_w)
        return rid

    def _monitored(self) -> list[tuple[str, int, int]]:
        assert self.ctx is not None
        rows = self.ctx.conn.execute(
            "SELECT repo, pr, last_cursor FROM monitor_state WHERE agent_id=?",
            (self.ctx.agent_id,),
        ).fetchall()
        return [(r["repo"], int(r["pr"]), int(r["last_cursor"] or 0)) for r in rows]

    def _advance_cursor(self, repo: str, pr: int, ordinal: int) -> None:
        assert self.ctx is not None
        now = time.time()

        def _w(conn):
            conn.execute(
                "UPDATE monitor_state SET last_cursor=?, updated=? "
                "WHERE agent_id=? AND repo=? AND pr=? AND CAST(last_cursor AS INTEGER) < ?",
                (str(ordinal), now, self.ctx.agent_id, repo, pr, ordinal),
            )

        self.ctx.fenced(_w)

    # ------------------------------------------------------------------ polling
    def poll(self, ctx: AdapterContext) -> dict:
        self.ctx = ctx
        # Authorization gate: a revoked read capability stops monitoring safely.
        if "github.read" not in ctx.caps:
            return {"status": "stopped", "reason": "github.read revoked"}

        report: dict[str, object] = {"status": "ok", "submitted": 0, "deferred": []}
        for repo, pr, cursor in self._monitored():
            try:
                events = self.client.list_events(repo, pr, cursor)
            except (RateLimited, TransientOutage) as e:
                # Do not advance the cursor; the next poll resumes without missing events.
                report["deferred"].append(f"{repo}#{pr}:{type(e).__name__}")
                continue
            for event in events:
                inbox = {
                    "event_id": f"gh:{repo}:{pr}:{event['event_id']}",
                    "adapter": self.name,
                    "type": "gh_activity",
                    "payload": {**event, "repo": repo, "pr": pr},
                }
                ctx.submit_event(inbox)                 # durable inbox, deduped by event_id
                self._advance_cursor(repo, pr, int(event["ordinal"]))
                report["submitted"] = int(report["submitted"]) + 1
        return report

    # ------------------------------------------------------------------ handling
    def _companion(self) -> Companion:
        assert self.ctx is not None
        return Companion(self.ctx.conn)

    def _audit(self, repo: str, pr: int, event_id: str, decision) -> None:
        assert self.ctx is not None
        now = time.time()

        def _w(conn):
            conn.execute(
                "INSERT OR IGNORE INTO classification_audit(agent_id, repo, pr, event_id, "
                "kind, relevant, reason, created) VALUES(?,?,?,?,?,?,?,?)",
                (self.ctx.agent_id, repo, pr, event_id, decision.kind,
                 1 if decision.relevant else 0, decision.reason, now),
            )

        self.ctx.fenced(_w)

    def _notify_effect(self, repo: str, pr: int, kind: str, event_id: str,
                       summary: str, reason: str) -> Effect:
        op_key = f"notify:{repo}:{pr}:{kind}:{event_id}"
        intent = {
            "adapter": self.name, "repo": repo, "pr": pr, "kind": kind,
            "event_id": event_id, "summary": summary, "reason": reason,
        }
        comp = self._companion()
        return Effect(
            key=op_key,
            intent=intent,
            required_cap="companion.notify",
            adapter=self.name,
            do=lambda fence: comp.write(op_key, repo, pr, kind, event_id, summary, reason, fence),
            probe=lambda: (ProbeResult.found(op_key) if comp.has(op_key) else ProbeResult.absent()),
        )

    def build_effect(self, intent: dict) -> Effect:
        return self._notify_effect(
            intent["repo"], int(intent["pr"]), intent["kind"], intent["event_id"],
            intent["summary"], intent["reason"],
        )

    def on_event(self, event: dict) -> None:
        assert self.ctx is not None
        if "github.read" not in self.ctx.caps:
            raise PermissionError("github adapter lacks github.read")
        p = event["payload"]
        repo, pr = p["repo"], int(p["pr"])
        decision = classify(p)
        self._audit(repo, pr, p["event_id"], decision)
        if not decision.relevant:
            return                                       # recorded for audit, not delivered
        eff = self._notify_effect(repo, pr, decision.kind, p["event_id"],
                                  summary=p.get("title", decision.kind), reason=decision.reason)
        self.ctx.emit_effect(eff)
