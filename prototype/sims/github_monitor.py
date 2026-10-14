"""GitHub pull request monitor adapter (read only).

Stark accepts a durable monitor_pull_request responsibility, polls authorized pull
requests through a read only client, classifies activity deterministically, and delivers
relevant updates to the companion feed exactly once per logical event. The adapter has no
path to post, merge, or edit: the client exposes reads only.

Correctness shape:
- Novelty is membership in the durable seen set, keyed by stable logical event id per
  stream (github_events). No integer cursor spans unrelated id spaces, so no event hides
  below another stream's id.
- A detected event enters the durable inbox before it is marked seen. A crash between
  the two resubmits an id the inbox already holds, so nothing is lost or doubled.
- Notifications are journaled effects (intent, then outcome) deduplicated by op_key.
- Failures are durable decisions, never sleeps: a rate limit or outage records
  next_poll_at and a failure count, so a restart keeps honoring the backoff.
- GitHub refusing the token (401, 403, 404) enters the inbox as a system event whose
  handler blocks the responsibility and notifies once per episode. Resuming needs an
  explicit operator request; nothing silently restarts or silently stops.
- Pagination cursors persist with the poll outcome, after every event is marked seen;
  while marking, the stored cursor is unsettled, so a crash forces a full walk rather than
  an early stop above an unread hole. A stream a page cap left incomplete enters the inbox
  as one system warning per episode; later polls finish the backfill instead of treating
  the stream as complete.
- A merged or closed pull request notifies and completes the responsibility.
- A local github.read revocation stops polling with the responsibility intact.
"""

from __future__ import annotations

import json
import time

from companion import Companion
from gameshark.adapters import AdapterContext
from gameshark.classify import Decision, classify
from gameshark.effects import Effect, ProbeResult
from gameshark.errors import Draining
from gameshark.github_client import AuthRevoked, GitHubClient, RateLimited, TransientOutage
from gameshark.github_events import normalize, stream_event_id

KIND = "monitor_pull_request"
BACKOFF_BASE_S = 30.0
BACKOFF_CAP_S = 900.0
LONG_RATE_LIMIT_S = 900.0          # a wait longer than this is worth telling the user
OFFLINE_GAP_S = 3600.0             # a catch up after this long is worth telling the user


def rid_for(repo: str, pr: int) -> str:
    return f"monitor:{repo}:{pr}"


def iso(epoch: float) -> str:
    """GitHub's timestamp format, so string order is time order."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def backoff_delay(fail_count: int) -> float:
    """Exponential backoff without jitter, so recovery stays deterministic in tests."""
    return min(BACKOFF_BASE_S * (2 ** max(0, fail_count - 1)), BACKOFF_CAP_S)


class GitHubMonitorAdapter:
    name = "github"
    required_caps = frozenset({"github.read", "companion.notify"})

    def __init__(self, client: GitHubClient, poll_interval_s: float = 0.0,
                 notifier=None) -> None:
        self.client = client
        self.poll_interval_s = poll_interval_s
        self.notifier = notifier            # optional best effort banner, after durable write
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

    # ------------------------------------------------------------------ helpers
    def _now(self) -> float:
        assert self.ctx is not None and self.ctx.clock is not None
        return self.ctx.clock.now()

    def _state(self, repo: str, pr: int):
        assert self.ctx is not None
        return self.ctx.conn.execute(
            "SELECT * FROM monitor_state WHERE agent_id=? AND repo=? AND pr=?",
            (self.ctx.agent_id, repo, pr),
        ).fetchone()

    def _update_state(self, repo: str, pr: int, **cols) -> None:
        assert self.ctx is not None
        cols["updated"] = self._now()
        sets = ", ".join(f"{k}=?" for k in cols)

        def _w(conn):
            conn.execute(
                f"UPDATE monitor_state SET {sets} WHERE agent_id=? AND repo=? AND pr=?",
                (*cols.values(), self.ctx.agent_id, repo, pr),
            )

        self.ctx.fenced(_w)

    def _ensure_state(self, repo: str, pr: int, scope: str = "pull_requests:read",
                      baseline: bool = False) -> None:
        assert self.ctx is not None
        now = self._now()
        before = iso(now) if baseline else None

        def _w(conn):
            conn.execute(
                "INSERT OR IGNORE INTO monitor_state(agent_id, repo, pr, etag, last_cursor, "
                "scope, updated, baseline_before) VALUES(?,?,?,?,?,?,?,?)",
                (self.ctx.agent_id, repo, pr, None, "", scope, now, before),
            )

        self.ctx.fenced(_w)

    # ------------------------------------------------------------------ responsibility
    def accept_responsibility(self, repo: str, pr: int, scope: str = "pull_requests:read",
                              baseline: bool = False) -> str:
        """Persist a durable monitor_pull_request responsibility.

        baseline=True records activity timestamped before the watch as seen without
        notifying, so watching an old pull request does not replay its history. The cut
        is by time, not by first poll: activity that lands while the first reads fail
        (offline, token refused) still arrives once access returns.
        Monitor state is written first: a crash before the responsibility row leaves an
        inert state row and an unapplied request, never a responsibility without state.
        """
        assert self.ctx is not None
        self._ensure_state(repo, pr, scope, baseline)
        rid = rid_for(repo, pr)
        existing = self.ctx.responsibilities.get(rid)
        if existing is None:
            self.ctx.responsibilities.upsert(
                rid, kind=KIND, payload={"repo": repo, "pr": pr, "scope": scope},
                state="active",
            )
        elif existing.state != "active":
            self.ctx.responsibilities.transition(rid, "active", checkpoint="rewatched")
            self._update_state(repo, pr, status="ok", next_poll_at=0.0, fail_count=0,
                               last_error=None)
        return rid

    def unwatch(self, repo: str, pr: int) -> None:
        assert self.ctx is not None
        if self.ctx.responsibilities.get(rid_for(repo, pr)) is not None:
            self.ctx.responsibilities.transition(rid_for(repo, pr), "done",
                                                 checkpoint="unwatched")

    def resume(self, repo: str, pr: int) -> None:
        """Operator says access is fixed: clear the block and poll on the next tick."""
        assert self.ctx is not None
        rid = rid_for(repo, pr)
        r = self.ctx.responsibilities.get(rid)
        if r is None:
            raise KeyError(f"no responsibility {rid}")
        self._update_state(repo, pr, status="ok", next_poll_at=0.0, fail_count=0,
                           last_error=None)
        if r.state == "blocked":
            self.ctx.responsibilities.transition(rid, "active", checkpoint="resumed")

    def apply_control(self, action: str, payload: dict) -> str:
        repo, pr = payload["repo"], int(payload["pr"])
        if action == "watch":
            return self.accept_responsibility(repo, pr, baseline=bool(payload.get("baseline")))
        if action == "unwatch":
            self.unwatch(repo, pr)
            return rid_for(repo, pr)
        if action == "resume":
            self.resume(repo, pr)
            return rid_for(repo, pr)
        raise ValueError(f"unknown action {action}")

    def _targets(self) -> list[tuple[str, int]]:
        """Active monitor responsibilities. Durable responsibilities drive polling."""
        assert self.ctx is not None
        out = []
        for r in self.ctx.responsibilities.outstanding():
            if r.kind == KIND and r.state == "active":
                repo, pr = r.payload["repo"], int(r.payload["pr"])
                if self._state(repo, pr) is None:
                    self._ensure_state(repo, pr, r.payload.get("scope", "pull_requests:read"))
                out.append((repo, pr))
        return out

    # ------------------------------------------------------------------ polling
    def poll(self, ctx: AdapterContext) -> dict:
        self.ctx = ctx
        if "github.read" not in ctx.caps:
            return {"status": "stopped", "reason": "github.read revoked"}

        report: dict = {"status": "ok", "submitted": 0, "deferred": [], "waiting": [],
                        "blocked": [], "next_due": None}
        now = self._now()
        for repo, pr in self._targets():
            if ctx.cancel is not None:
                ctx.cancel.check()
            try:
                if ctx.admit is not None:
                    ctx.admit()                     # draining: start no new target
            except Draining:
                report["deferred"].append(f"{repo}#{pr}:Draining")
                continue
            st = self._state(repo, pr)
            if st["next_poll_at"] > now:
                report["waiting"].append(f"{repo}#{pr}")
            else:
                self._poll_one(repo, pr, st, report)
        dues = [self._state(repo, pr)["next_poll_at"] for repo, pr in self._targets()]
        report["next_due"] = min(dues) if dues else None
        return report

    def _poll_one(self, repo: str, pr: int, st, report: dict) -> None:
        assert self.ctx is not None
        now = self._now()
        seen = {r["event_id"] for r in self.ctx.conn.execute(
            "SELECT event_id FROM monitor_seen WHERE agent_id=? AND repo=? AND pr=?",
            (self.ctx.agent_id, repo, pr))}
        try:
            viewer = st["viewer"] or self.client.viewer()
            snap = self.client.snapshot(
                repo, pr, json.loads(st["etags"] or "{}"), st["head_sha"],
                known=lambda stream, item: stream_event_id(stream, item) in seen,
                cursors=json.loads(st["page_cursors"] or "{}"),
            )
        except RateLimited as e:
            until = now + e.retry_after_s
            self._update_state(repo, pr, status="backoff", next_poll_at=until,
                               last_error=str(e))
            if e.retry_after_s > LONG_RATE_LIMIT_S:
                self._submit_system(repo, pr, f"rate_limited:{int(until // 60)}", {
                    "condition": "rate_limited",
                    "summary": f"GitHub rate limit; next check in {e.retry_after_s / 60:.0f} min",
                    "reason": "rate limit exceeded; monitoring paused, not dropped",
                })
            report["deferred"].append(f"{repo}#{pr}:RateLimited")
            return
        except TransientOutage as e:
            n = int(st["fail_count"]) + 1
            self._update_state(repo, pr, status="backoff", fail_count=n,
                               next_poll_at=now + backoff_delay(n), last_error=str(e))
            report["deferred"].append(f"{repo}#{pr}:TransientOutage")
            return
        except AuthRevoked as e:
            episode = int(st["episode"]) + 1
            self._submit_system(repo, pr, f"auth_revoked:{episode}", {
                "condition": "auth_revoked", "episode": episode, "status": e.status,
                "summary": f"GitHub refused access ({e.status}); monitoring blocked",
                "reason": "token revoked or repository access removed; run stark resume",
            })
            report["blocked"].append(f"{repo}#{pr}:AuthRevoked")
            return

        for stream in snap.truncated:
            episode = snap.cursors[stream]["episode"]
            if stream == "check_runs":
                summary = "check runs exceed the page cap; older runs not read"
            else:
                summary = f"{stream} exceed one poll's page cap; older pages read next poll"
            self._submit_system(repo, pr, f"pagination_truncated:{stream}:{episode}", {
                "condition": "pagination_truncated", "stream": stream, "episode": episode,
                "summary": summary,
                "reason": "pagination safety limit reached; coverage incomplete until done",
            })

        fresh = [e for e in normalize(snap) if e["event_id"] not in seen]
        if fresh:
            # Marking seen is not atomic with the cursor below. A crash in between would
            # leave seen ids above an unread hole, so until the outcome lands no page may
            # stop the next walk.
            self._update_state(repo, pr, page_cursors=json.dumps(
                {s: {**c, "settled": False} for s, c in snap.cursors.items()}))
        history_before = st["baseline_before"]
        last = st["last_cursor"]
        delivered = 0
        for ev in fresh:
            # Safe boundary: an event not yet submitted stays unseen, so a successor
            # detects it again and nothing is lost or doubled.
            if self.ctx.cancel is not None:
                self.ctx.cancel.check()
            payload = {**ev, "repo": repo, "pr": pr, "viewer": viewer}
            if ev["raw_kind"] == "check_run":
                payload["prev_conclusion"] = self._prev_conclusion(repo, pr, ev["name"])
            if history_before and ev["raw_kind"] != "pr_state" and ev["ts"] < history_before:
                self._audit(repo, pr, ev["event_id"], classify(payload), prefix="baseline: ")
            else:
                self.ctx.submit_event({
                    "event_id": f"gh:{repo}:{pr}:{ev['event_id']}",
                    "adapter": self.name, "type": "gh_activity", "payload": payload,
                })
                report["submitted"] += 1
                delivered += 1
            self._mark_seen(repo, pr, ev)
            last = ev["event_id"]

        prior_ok = st["last_ok_at"]
        if prior_ok is not None and now - prior_ok > OFFLINE_GAP_S and delivered:
            hours = (now - prior_ok) / 3600
            self._submit_system(repo, pr, f"caught_up:{int(prior_ok)}", {
                "condition": "caught_up",
                "summary": f"back after {hours:.1f} h offline; {delivered} events reviewed",
                "reason": "offline period ended; missed activity replayed from GitHub",
            })

        closed = self._state(repo, pr)["status"] == "closed"
        self._update_state(
            repo, pr, status="closed" if closed else "ok", fail_count=0,
            next_poll_at=now + self.poll_interval_s, last_ok_at=now,
            last_error="CI unavailable to this token" if snap.checks_unavailable else None,
            etags=json.dumps(snap.etags), head_sha=snap.head_sha, viewer=viewer,
            last_cursor=last, page_cursors=json.dumps(snap.cursors),
        )

    def _prev_conclusion(self, repo: str, pr: int, name: str) -> str | None:
        assert self.ctx is not None
        row = self.ctx.conn.execute(
            "SELECT conclusion FROM monitor_checks WHERE agent_id=? AND repo=? AND pr=? "
            "AND name=?", (self.ctx.agent_id, repo, pr, name),
        ).fetchone()
        return row["conclusion"] if row else None

    def _mark_seen(self, repo: str, pr: int, ev: dict) -> None:
        assert self.ctx is not None
        now = self._now()

        def _w(conn):
            conn.execute(
                "INSERT OR IGNORE INTO monitor_seen(agent_id, repo, pr, event_id, created) "
                "VALUES(?,?,?,?,?)", (self.ctx.agent_id, repo, pr, ev["event_id"], now),
            )
            if ev["raw_kind"] == "check_run":
                conn.execute(
                    "INSERT INTO monitor_checks(agent_id, repo, pr, name, conclusion) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(agent_id, repo, pr, name) "
                    "DO UPDATE SET conclusion=excluded.conclusion",
                    (self.ctx.agent_id, repo, pr, ev["name"], ev["state"]),
                )

        self.ctx.fenced(_w)

    def _submit_system(self, repo: str, pr: int, key: str, fields: dict) -> None:
        assert self.ctx is not None
        payload = {"event_id": f"system:{key}", "raw_kind": "system", "repo": repo, "pr": pr,
                   "url": "", **fields}
        self.ctx.submit_event({
            "event_id": f"gh:{repo}:{pr}:system:{key}", "adapter": self.name,
            "type": "gh_system", "payload": payload,
        })

    # ------------------------------------------------------------------ handling
    def _companion(self) -> Companion:
        assert self.ctx is not None
        return Companion(self.ctx.conn, on_new=self.notifier, fenced=self.ctx.fenced)

    def _audit(self, repo: str, pr: int, event_id: str, decision: Decision,
               prefix: str = "") -> None:
        assert self.ctx is not None
        now = self._now()
        relevant = 0 if prefix else (1 if decision.relevant else 0)

        def _w(conn):
            conn.execute(
                "INSERT OR IGNORE INTO classification_audit(agent_id, repo, pr, event_id, "
                "kind, relevant, reason, created) VALUES(?,?,?,?,?,?,?,?)",
                (self.ctx.agent_id, repo, pr, event_id, decision.kind, relevant,
                 prefix + decision.reason, now),
            )

        self.ctx.fenced(_w)

    def _notify_effect(self, repo: str, pr: int, kind: str, event_id: str,
                       summary: str, reason: str, url: str = "") -> Effect:
        op_key = f"notify:{repo}:{pr}:{kind}:{event_id}"
        intent = {
            "adapter": self.name, "repo": repo, "pr": pr, "kind": kind,
            "event_id": event_id, "summary": summary, "reason": reason, "url": url,
        }
        comp = self._companion()
        return Effect(
            key=op_key,
            intent=intent,
            required_cap="companion.notify",
            adapter=self.name,
            do=lambda fence: comp.write(op_key, repo, pr, kind, event_id, summary, reason,
                                        fence, url=url),
            probe=lambda: (ProbeResult.found(op_key) if comp.has(op_key) else ProbeResult.absent()),
        )

    def build_effect(self, intent: dict) -> Effect:
        return self._notify_effect(
            intent["repo"], int(intent["pr"]), intent["kind"], intent["event_id"],
            intent["summary"], intent["reason"], intent.get("url", ""),
        )

    def on_event(self, event: dict) -> None:
        assert self.ctx is not None
        if "github.read" not in self.ctx.caps:
            raise PermissionError("github adapter lacks github.read")
        p = event["payload"]
        repo, pr = p["repo"], int(p["pr"])
        rid = rid_for(repo, pr)

        if p.get("condition") == "auth_revoked":
            self._update_state(repo, pr, status="auth_revoked", next_poll_at=0.0,
                               episode=int(p["episode"]), last_error=p["summary"])
            r = self.ctx.responsibilities.get(rid)
            if r is not None and r.state == "active":
                self.ctx.responsibilities.transition(rid, "blocked", checkpoint="auth_revoked")

        decision = classify(p)
        self._audit(repo, pr, p["event_id"], decision)
        if decision.relevant:
            self.ctx.emit_effect(self._notify_effect(
                repo, pr, decision.kind, p["event_id"], summary=summarize(p, decision),
                reason=decision.reason, url=p.get("url", ""),
            ))

        if p.get("raw_kind") == "pr_state":
            self._update_state(repo, pr, status="closed")
            r = self.ctx.responsibilities.get(rid)
            if r is not None and r.state != "done":
                self.ctx.responsibilities.transition(rid, "done", checkpoint=f"pr_{p['state']}")


def summarize(p: dict, decision: Decision) -> str:
    """One line for the feed. Deterministic: derived from the payload alone."""
    if p.get("raw_kind") == "system":
        return p.get("summary", decision.reason)
    if p.get("raw_kind") == "check_run":
        return f"{p.get('name', 'check')} {p.get('state')}"
    if p.get("raw_kind") == "pr_state":
        return f"pull request {p.get('state')}"
    body = " ".join((p.get("body") or "").split())
    if len(body) > 100:
        body = body[:97] + "..."
    return f"{decision.reason}: {body}" if body else decision.reason
