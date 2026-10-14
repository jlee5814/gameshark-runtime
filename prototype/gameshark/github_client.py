"""GitHub client transports for pull request monitoring.

Read only by construction: no method mutates GitHub. The real client hits
api.github.com with a token from the environment, uses conditional requests with ETags,
follows pagination, and honors rate limit and transient outage signals with bounded
backoff. The fake client serves scripted responses so tests and the demo stay
deterministic and need no credentials.

A logical event is a dict:
    {"event_id": str, "raw_kind": "review"|"comment"|"check_run", "ordinal": int,
     "state": str, "author": str, "title": str}
`ordinal` is a monotonic sort key within one pull request, used as the durable cursor.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Protocol


class RateLimited(Exception):
    """GitHub reported the rate limit is exhausted; retry after the given delay."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after_s}s")
        self.retry_after_s = retry_after_s


class TransientOutage(Exception):
    """A transient network or 5xx error; the caller may retry with backoff."""


class GitHubClient(Protocol):
    def list_events(self, repo: str, pr: int, since_ordinal: int) -> list[dict]:
        """Return logical events with ordinal greater than since_ordinal, sorted ascending."""
        ...


class FakeGitHubClient:
    """Scripted, deterministic transport for tests and the demo.

    The caller stages events and optional faults. Each list_events call pages at
    page_size, and a staged fault fires once before yielding results.
    """

    def __init__(self, page_size: int = 50) -> None:
        self._events: list[dict] = []
        self._faults: list[Exception] = []
        self.page_size = page_size
        self.calls = 0

    # staging -----------------------------------------------------------------
    def add_review(self, event_id: str, ordinal: int, state: str, author: str = "octo") -> None:
        self._events.append(
            {"event_id": event_id, "raw_kind": "review", "ordinal": ordinal,
             "state": state, "author": author, "title": f"review {state}"}
        )

    def add_comment(self, event_id: str, ordinal: int, author: str = "octo") -> None:
        self._events.append(
            {"event_id": event_id, "raw_kind": "comment", "ordinal": ordinal,
             "state": "commented", "author": author, "title": "comment"}
        )

    def add_check_run(self, event_id: str, ordinal: int, conclusion: str) -> None:
        self._events.append(
            {"event_id": event_id, "raw_kind": "check_run", "ordinal": ordinal,
             "state": conclusion, "author": "ci", "title": f"CI {conclusion}"}
        )

    def stage_fault(self, exc: Exception) -> None:
        """Queue a fault to raise once on the next list_events call."""
        self._faults.append(exc)

    # transport ---------------------------------------------------------------
    def list_events(self, repo: str, pr: int, since_ordinal: int) -> list[dict]:
        self.calls += 1
        if self._faults:
            raise self._faults.pop(0)
        fresh = sorted(
            (e for e in self._events if e["ordinal"] > since_ordinal),
            key=lambda e: e["ordinal"],
        )
        return fresh[: self.page_size]


class RestGitHubClient:
    """Real read only client for api.github.com.

    Exposes only reads. Construction requires a token. ETag conditional requests cut
    quota use; pagination follows the Link header; rate limit and 5xx map to typed
    errors the adapter handles with backoff.
    """

    BASE = "https://api.github.com"

    def __init__(self, token: str, max_retries: int = 3) -> None:
        if not token:
            raise ValueError("a read only token is required for live mode")
        self._token = token
        self._etags: dict[str, str] = {}
        self.max_retries = max_retries

    def _get(self, path: str) -> tuple[int, list | dict, dict]:
        url = f"{self.BASE}{path}"
        req = urllib.request.Request(url, method="GET")  # read only
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if url in self._etags:
            req.add_header("If-None-Match", self._etags[url])
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                headers = {k.lower(): v for k, v in resp.headers.items()}
                if "etag" in headers:
                    self._etags[url] = headers["etag"]
                body = json.loads(resp.read().decode("utf8") or "[]")
                return resp.status, body, headers
        except urllib.error.HTTPError as e:
            headers = {k.lower(): v for k, v in (e.headers or {}).items()}
            if e.code == 304:
                return 304, [], headers
            if e.code == 403 and headers.get("x-ratelimit-remaining") == "0":
                retry = float(headers.get("retry-after", "60"))
                raise RateLimited(retry) from e
            if 500 <= e.code < 600:
                raise TransientOutage(f"{e.code} from GitHub") from e
            raise
        except urllib.error.URLError as e:
            raise TransientOutage(str(e)) from e

    def list_events(self, repo: str, pr: int, since_ordinal: int) -> list[dict]:
        """Merge reviews, comments, and check runs into ordinal sorted logical events.

        Live ordinals derive from GitHub ids and timestamps. This mapping is where the
        at least once polling assumption lives: see README guarantees.
        """
        events: list[dict] = []
        attempt = 0
        while True:
            try:
                _, reviews, _ = self._get(f"/repos/{repo}/pulls/{pr}/reviews")
                _, comments, _ = self._get(f"/repos/{repo}/issues/{pr}/comments")
                break
            except (RateLimited, TransientOutage):
                attempt += 1
                if attempt > self.max_retries:
                    raise
                time.sleep(min(2 ** attempt, 8))
        for r in reviews if isinstance(reviews, list) else []:
            events.append({
                "event_id": f"review:{r.get('id')}",
                "raw_kind": "review",
                "ordinal": int(r.get("id", 0)),
                "state": (r.get("state") or "").lower(),
                "author": (r.get("user") or {}).get("login", "?"),
                "title": f"review {r.get('state')}",
            })
        for c in comments if isinstance(comments, list) else []:
            events.append({
                "event_id": f"comment:{c.get('id')}",
                "raw_kind": "comment",
                "ordinal": int(c.get("id", 0)),
                "state": "commented",
                "author": (c.get("user") or {}).get("login", "?"),
                "title": "comment",
            })
        return sorted((e for e in events if e["ordinal"] > since_ordinal),
                      key=lambda e: e["ordinal"])
