"""GitHub client transports for pull request monitoring.

Read only by construction: the only network primitive issues GET requests, and no method
mutates GitHub. A client returns a Snapshot of raw GitHub JSON for one pull request; the
adapter turns a snapshot into stable logical events (see github_events.normalize), so
event identity and cursoring live next to durable state, not inside the transport.

The real client hits api.github.com with a token, sends conditional requests with ETags
the caller persists, follows Link pagination, and maps failures to typed errors. It never
sleeps or retries: backoff is a durable decision the adapter records, so a restart in the
middle of a backoff keeps waiting instead of hammering the API.

Pagination is tail first. Reviews list chronologically and both comment lists ascend by
id, so new items append at the last page, deletions only move items to lower pages, and
nothing inserts mid list. Each poll reads page 1 for `rel="last"`, walks back from the last
page, and stops at the first page whose items the caller already knows. A per stream
cursor the caller persists carries what a capped walk left unread (`backfill`: pages 2
through that page), so later polls finish the hole instead of rereading history. A stream
left incomplete is reported in `Snapshot.truncated`, never returned as if complete.

The fake client stages the same raw JSON shapes, so tests drive the exact normalization
path a live run uses, with no credentials.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


class RateLimited(Exception):
    """GitHub reported a primary or secondary rate limit; retry after the given delay."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after_s:.0f}s")
        self.retry_after_s = retry_after_s


class TransientOutage(Exception):
    """A transient network, timeout, decode, or 5xx error; retry with backoff."""


class AuthRevoked(Exception):
    """The token no longer grants access: 401, a non rate limit 403, or 404.

    A fine grained token that lost repository access answers 404, which GitHub also
    answers for a deleted repository. The two cannot be told apart, so both block the
    responsibility rather than guess.
    """

    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"GitHub answered {status}: {detail}".strip())
        self.status = status


@dataclass
class Snapshot:
    """Raw GitHub JSON for one pull request.

    A stream answered 304 Not Modified comes back empty: everything it held was already
    seen when its ETag was stored. `pr` is None when the pull request itself was 304.
    """

    pr: dict | None
    reviews: list[dict] = field(default_factory=list)
    review_comments: list[dict] = field(default_factory=list)
    issue_comments: list[dict] = field(default_factory=list)
    check_runs: list[dict] = field(default_factory=list)
    head_sha: str | None = None
    checks_unavailable: bool = False
    etags: dict[str, str] = field(default_factory=dict)
    cursors: dict[str, dict] = field(default_factory=dict)   # stream -> pagination cursor
    truncated: list[str] = field(default_factory=list)       # streams a safety cap left short


Known = Callable[[str, dict], bool]
APPEND_ONLY = ("reviews", "review_comments", "issue_comments")


def _cursor(prev: dict | None, backfill: int, truncated: bool) -> dict:
    """A stream's next cursor. The episode counts entries into truncation, once each."""
    episode = int((prev or {}).get("episode", 0))
    if truncated and not (prev or {}).get("truncated"):
        episode += 1
    return {"backfill": backfill, "truncated": truncated, "episode": episode}


class GitHubClient(Protocol):
    def viewer(self) -> str:
        """Return the login the token acts as, used to filter self activity."""
        ...

    def snapshot(
        self, repo: str, pr: int, etags: dict[str, str], head_sha: str | None,
        known: Known | None = None, cursors: dict[str, dict] | None = None,
    ) -> Snapshot:
        """Fetch every stream for one pull request, conditional on stored ETags.

        `known(stream, item)` says whether the caller already holds an item; `cursors` are
        the pagination cursors a previous snapshot returned.
        """
        ...


def token_from_env() -> str:
    """Resolve a token: GITHUB_TOKEN first, then the GitHub CLI login.

    A fine grained, read only token is the recommended setting. The gh fallback token
    usually carries write scope; the client still cannot write, but the credential could.
    """
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    gh = shutil.which("gh")
    if gh:
        out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True,
                             timeout=10, check=False)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    raise RuntimeError("no GitHub token: set GITHUB_TOKEN or run `gh auth login`")


_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
_LAST = re.compile(r'<([^>]+)>;\s*rel="last"')


class RestGitHubClient:
    """Real read only client for api.github.com."""

    BASE = "https://api.github.com"
    PAGE_CAP = 20                          # pages per stream per poll; backfill resumes

    def __init__(self, token: str, base: str | None = None, timeout_s: float = 15.0,
                 per_page: int = 100) -> None:
        if not token:
            raise ValueError("a read only token is required for live mode")
        self._token = token
        self.base = (base or self.BASE).rstrip("/")
        self.timeout_s = timeout_s
        self.per_page = per_page
        self.requests = 0                  # observability: requests sent this process

    # ------------------------------------------------------------------ transport
    def _request(self, url: str, etag: str | None) -> tuple[int, object, dict]:
        """Issue one GET. The only network primitive in this module."""
        req = urllib.request.Request(url, method="GET")
        assert req.get_method() == "GET"
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "gameshark-stark")
        if etag:
            req.add_header("If-None-Match", etag)
        self.requests += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                headers = {k.lower(): v for k, v in resp.headers.items()}
                raw = resp.read().decode("utf8")
                try:
                    body = json.loads(raw) if raw else None
                except json.JSONDecodeError as e:
                    raise TransientOutage(f"undecodable body from {url}") from e
                return resp.status, body, headers
        except urllib.error.HTTPError as e:
            headers = {k.lower(): v for k, v in (e.headers or {}).items()}
            if e.code == 304:
                return 304, None, headers
            detail = ""
            try:
                detail = e.read().decode("utf8", "replace")[:200]
            except Exception:
                detail = ""
            raise _classify_http_error(e.code, headers, detail) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise TransientOutage(str(e)) from e

    @staticmethod
    def _items(body: object, key: str | None) -> list[dict]:
        items = body.get(key, []) if key and isinstance(body, dict) else body
        return items if isinstance(items, list) else []

    def _get_all(self, path: str, etags: dict[str, str], new: dict[str, str],
                 key: str | None = None) -> tuple[list[dict] | None, bool]:
        """Follow Link pagination forward. Return (None, False) when the stream answered 304.

        For lists without an append order (check runs). An ETag is stored only for a
        stream that fit on one page. The flag reports a walk the cap cut short.
        """
        sep = "&" if "?" in path else "?"
        first = f"{self.base}{path}{sep}per_page={self.per_page}"
        url = first
        out: list[dict] = []
        for page in range(self.PAGE_CAP):
            status, body, headers = self._request(url, etags.get(first) if page == 0 else None)
            if status == 304:
                new[first] = etags.get(first, "")
                return None, False
            out.extend(self._items(body, key))
            m = _NEXT.search(headers.get("link", ""))
            if not m:
                if page == 0 and headers.get("etag"):
                    new[first] = headers["etag"]
                return out, False
            url = m.group(1)
        return out, True

    def _get_tail(self, path: str, stream: str, etags: dict[str, str], new: dict[str, str],
                  known: Known, prev: dict | None) -> tuple[list[dict] | None, dict]:
        """Read an append only list tail first. Return (items, next cursor).

        Invariant kept across polls: every item the caller does not know sits either after
        every item it knows, or on pages 2 through `backfill`. The tail walk covers the
        first case by stopping at a fully known page; the backfill covers the second.
        Deletions only move items to lower pages, so a stored backfill page stays safe.
        Without a settled prior cursor nothing below the tail is trusted and no page stops
        a walk: a poll that died while marking items seen leaves its cursor unsettled.
        """
        first = f"{self.base}{path}?per_page={self.per_page}"
        status, body, headers = self._request(first, etags.get(first))
        if status == 304:
            new[first] = etags.get(first, "")
            return None, _cursor(prev, 0, False)   # one page, fully read when the ETag landed
        link = headers.get("link", "")
        m = _LAST.search(link)
        last = _page_of(m.group(1)) if m else None
        if last is None:
            if _NEXT.search(link):                 # no usable rel="last": forward, capped
                items, cut = self._get_all(path, {}, new)
                return items, _cursor(prev, 0, cut)
            if headers.get("etag"):
                new[first] = headers["etag"]
            return self._items(body, None), _cursor(prev, 0, False)

        pages: dict[int, list[dict]] = {1: self._items(body, None)}
        budget = self.PAGE_CAP - 1
        backfill = min(int((prev or {}).get("backfill", 0)), last)
        trusted = prev is not None and prev.get("settled", True)

        def fetch(n: int) -> list[dict]:
            _, b, _ = self._request(f"{first}&page={n}", None)
            pages[n] = self._items(b, None)
            return pages[n]

        page, stopped = last, False
        while page >= 2 and budget:
            items = fetch(page)
            budget -= 1
            if trusted and items and all(known(stream, i) for i in items):
                stopped = True
                break
            page -= 1
        if not stopped:
            # Reached page 1, or the cap left pages 2 through `page` unread.
            backfill = max(backfill, page) if page >= 2 else 0
        while backfill >= 2 and budget:
            if backfill not in pages:
                fetch(backfill)
                budget -= 1
            backfill -= 1
        backfill = backfill if backfill >= 2 else 0

        out: list[dict] = []
        ids: set = set()
        for n in sorted(pages):                    # a mid walk deletion can repeat an item
            for item in pages[n]:
                if item.get("id") not in ids:
                    ids.add(item.get("id"))
                    out.append(item)
        return out, _cursor(prev, backfill, backfill > 0)

    # ------------------------------------------------------------------ reads
    def viewer(self) -> str:
        _, body, _ = self._request(f"{self.base}/user", None)
        return (body or {}).get("login", "") if isinstance(body, dict) else ""

    def snapshot(
        self, repo: str, pr: int, etags: dict[str, str], head_sha: str | None,
        known: Known | None = None, cursors: dict[str, dict] | None = None,
    ) -> Snapshot:
        repo_q = urllib.parse.quote(repo, safe="/")
        known = known or (lambda stream, item: False)
        prev = cursors or {}
        out: dict[str, dict] = dict(prev)
        new: dict[str, str] = {}
        pr_url = f"{self.base}/repos/{repo_q}/pulls/{pr}"
        status, body, headers = self._request(pr_url, etags.get(pr_url))
        pr_json: dict | None = None
        if status == 304:
            new[pr_url] = etags[pr_url]
        else:
            pr_json = body if isinstance(body, dict) else {}
            if "etag" in headers:
                new[pr_url] = headers["etag"]
            head_sha = ((pr_json.get("head") or {}).get("sha")) or head_sha

        snap = Snapshot(pr=pr_json, head_sha=head_sha)
        paths = {
            "reviews": f"/repos/{repo_q}/pulls/{pr}/reviews",
            "review_comments": f"/repos/{repo_q}/pulls/{pr}/comments",
            "issue_comments": f"/repos/{repo_q}/issues/{pr}/comments",
        }
        for stream in APPEND_ONLY:
            items, out[stream] = self._get_tail(paths[stream], stream, etags, new, known,
                                                prev.get(stream))
            setattr(snap, stream, items or [])
        if head_sha:
            try:
                runs, cut = self._get_all(
                    f"/repos/{repo_q}/commits/{head_sha}/check-runs", etags, new,
                    key="check_runs",
                )
                snap.check_runs = runs or []
                if runs is not None:
                    out["check_runs"] = _cursor(prev.get("check_runs"), 0, cut)
            except AuthRevoked:
                # A token without the checks permission still monitors reviews and
                # comments; CI is reported unavailable instead of blocking everything.
                snap.checks_unavailable = True
        snap.etags = new
        snap.cursors = out
        snap.truncated = [s for s in (*APPEND_ONLY, "check_runs")
                          if out.get(s, {}).get("truncated")]
        return snap


def _page_of(url: str) -> int | None:
    page = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("page", [""])[0]
    return int(page) if page.isdigit() else None


def _classify_http_error(code: int, headers: dict, detail: str) -> Exception:
    """Map an HTTP failure to the typed error the adapter reasons about."""
    remaining = headers.get("x-ratelimit-remaining")
    if "retry-after" in headers and code in (403, 429):
        return RateLimited(float(headers["retry-after"]))
    if code in (403, 429) and remaining == "0":
        reset = float(headers.get("x-ratelimit-reset", time.time() + 60))
        return RateLimited(max(1.0, reset - time.time()))
    if code == 429 or (code == 403 and "rate limit" in detail.lower()):
        return RateLimited(60.0)
    if code in (401, 403, 404):
        return AuthRevoked(code, detail)
    if code >= 500 or code == 408:
        return TransientOutage(f"{code} from GitHub")
    return TransientOutage(f"unexpected {code} from GitHub: {detail}")


class FakeGitHubClient:
    """Scripted, deterministic transport producing raw GitHub JSON shapes.

    Staging helpers accept the original v0.2 arguments; `ordinal` becomes the item's
    numeric id and timestamp so order stays explicit.
    """

    def __init__(self, viewer: str = "stark-owner", head_sha: str = "sha1") -> None:
        self._viewer = viewer
        self.head_sha = head_sha
        self.pr_json: dict = {"number": 0, "state": "open", "merged": False,
                              "user": {"login": viewer, "type": "User"},
                              "head": {"sha": head_sha}}
        self.reviews: list[dict] = []
        self.review_comments: list[dict] = []
        self.issue_comments: list[dict] = []
        self.check_runs: dict[str, list[dict]] = {}
        self._faults: list[Exception] = []
        self.checks_unavailable = False
        self.calls = 0

    # staging -----------------------------------------------------------------
    @staticmethod
    def _user(login: str) -> dict:
        is_bot = login.endswith("[bot]")
        return {"login": login, "type": "Bot" if is_bot else "User"}

    @staticmethod
    def _ts(ordinal: int) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(1_700_000_000 + ordinal))

    def add_review(self, event_id: str, ordinal: int, state: str, author: str = "octo",
                   body: str = "") -> None:
        self.reviews.append({
            "id": event_id, "state": state.upper(), "user": self._user(author), "body": body,
            "submitted_at": self._ts(ordinal), "html_url": f"https://github.test/r/{event_id}",
        })

    def add_review_comment(self, event_id: str, ordinal: int, review_id: str | None = None,
                           author: str = "octo", body: str = "nit") -> None:
        self.review_comments.append({
            "id": event_id, "pull_request_review_id": review_id, "user": self._user(author),
            "body": body, "created_at": self._ts(ordinal),
            "html_url": f"https://github.test/rc/{event_id}",
        })

    def add_comment(self, event_id: str, ordinal: int, author: str = "octo",
                    body: str = "please take a look at the retry path") -> None:
        self.issue_comments.append({
            "id": event_id, "user": self._user(author), "body": body,
            "created_at": self._ts(ordinal), "html_url": f"https://github.test/c/{event_id}",
        })

    def add_check_run(self, event_id: str, ordinal: int, conclusion: str | None,
                      name: str = "test", sha: str | None = None) -> None:
        run = {
            "id": event_id, "name": name,
            "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion, "completed_at": self._ts(ordinal) if conclusion else None,
            "html_url": f"https://github.test/ci/{event_id}",
        }
        self.check_runs.setdefault(sha or self.head_sha, []).append(run)

    def complete_check_run(self, event_id: str, ordinal: int, conclusion: str) -> None:
        for runs in self.check_runs.values():
            for run in runs:
                if run["id"] == event_id:
                    run.update(status="completed", conclusion=conclusion,
                               completed_at=self._ts(ordinal))

    def push(self, sha: str) -> None:
        self.head_sha = sha
        self.pr_json["head"] = {"sha": sha}

    def close(self, merged: bool) -> None:
        self.pr_json.update(state="closed", merged=merged)

    def stage_fault(self, exc: Exception) -> None:
        """Queue a fault to raise once on the next snapshot call."""
        self._faults.append(exc)

    # transport ---------------------------------------------------------------
    def viewer(self) -> str:
        return self._viewer

    def snapshot(
        self, repo: str, pr: int, etags: dict[str, str], head_sha: str | None,
        known: Known | None = None, cursors: dict[str, dict] | None = None,
    ) -> Snapshot:
        self.calls += 1
        if self._faults:
            raise self._faults.pop(0)
        return Snapshot(
            pr={**self.pr_json, "number": pr},
            reviews=[dict(r) for r in self.reviews],
            review_comments=[dict(c) for c in self.review_comments],
            issue_comments=[dict(c) for c in self.issue_comments],
            check_runs=[] if self.checks_unavailable
            else [dict(r) for r in self.check_runs.get(self.head_sha, [])],
            head_sha=self.head_sha,
            checks_unavailable=self.checks_unavailable,
            etags={},
        )
