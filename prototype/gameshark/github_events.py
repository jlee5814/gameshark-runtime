"""Normalize a GitHub snapshot into stable logical events.

A logical event id names one observable fact and never changes across polls, restarts,
or pagination: `review:{id}`, `review_comment:{id}`, `issue_comment:{id}`,
`check:{id}:{conclusion}`, `pr_state:{merged|closed}`. Each stream keeps its own id
space, and the durable seen set (not id order) decides novelty, so a new review whose id
sorts below an older comment id is never skipped.

Events come back in chronological order (timestamp, then id) so the companion feed reads
in the order things happened. Pure function: no I/O, no clock.
"""

from __future__ import annotations

from collections import Counter

from gameshark.github_client import Snapshot

FAILED = frozenset({"failure", "timed_out", "action_required", "startup_failure"})
CANCELLED = "cancelled"                               # did not finish; not a failure verdict


def _login(obj: dict) -> tuple[str, str]:
    user = obj.get("user") or obj.get("app") or {}
    login = user.get("login") or user.get("slug") or "?"
    utype = user.get("type") or ("Bot" if login.endswith("[bot]") else "User")
    return login, utype


def _event(event_id: str, raw_kind: str, ts: str | None, **fields) -> dict:
    return {"event_id": event_id, "raw_kind": raw_kind, "ts": ts or "", **fields}


def normalize(snap: Snapshot) -> list[dict]:
    events: list[dict] = []
    inline_per_review = Counter(
        str(c.get("pull_request_review_id")) for c in snap.review_comments
        if c.get("pull_request_review_id") is not None
    )

    for r in snap.reviews:
        state = (r.get("state") or "").lower()
        if state == "pending":
            continue                                  # an unsubmitted draft is not activity
        login, utype = _login(r)
        events.append(_event(
            f"review:{r.get('id')}", "review", r.get("submitted_at"),
            state=state, author=login, author_type=utype, body=r.get("body") or "",
            url=r.get("html_url", ""), inline_count=inline_per_review.get(str(r.get("id")), 0),
        ))

    for c in snap.review_comments:
        login, utype = _login(c)
        rid = c.get("pull_request_review_id")
        events.append(_event(
            f"review_comment:{c.get('id')}", "review_comment", c.get("created_at"),
            state="commented", author=login, author_type=utype, body=c.get("body") or "",
            url=c.get("html_url", ""), review_id=None if rid is None else str(rid),
        ))

    for c in snap.issue_comments:
        login, utype = _login(c)
        events.append(_event(
            f"issue_comment:{c.get('id')}", "issue_comment", c.get("created_at"),
            state="commented", author=login, author_type=utype, body=c.get("body") or "",
            url=c.get("html_url", ""),
        ))

    for run in snap.check_runs:
        if run.get("status") != "completed" or not run.get("conclusion"):
            continue                                  # in progress runs are not facts yet
        conclusion = run["conclusion"].lower()
        events.append(_event(
            f"check:{run.get('id')}:{conclusion}", "check_run", run.get("completed_at"),
            state=conclusion, name=run.get("name") or "check", author="ci",
            author_type="Bot", body="", url=run.get("html_url", ""),
        ))

    pr = snap.pr or {}
    if pr.get("state") == "closed":
        outcome = "merged" if pr.get("merged") or pr.get("merged_at") else "closed"
        events.append(_event(
            f"pr_state:{outcome}", "pr_state", pr.get("merged_at") or pr.get("closed_at")
            or "9999", state=outcome, author=(pr.get("merged_by") or {}).get("login", "?"),
            author_type="User", body="", url=pr.get("html_url", ""),
        ))

    events.sort(key=lambda e: (e["ts"], e["event_id"]))
    return events
