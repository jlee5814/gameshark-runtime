"""GitHub client transports for pull request monitoring.

Read only by construction: the only network primitive issues GET requests, and no method
mutates GitHub. A client returns a Snapshot of raw GitHub JSON for one pull request; the
adapter turns a snapshot into stable logical events (see github_events.normalize), so
event identity and cursoring live next to durable state, not inside the transport.

The real client hits api.github.com with a token, sends conditional requests with ETags
the caller persists, follows Link pagination, and maps failures to typed errors. It never
sleeps or retries: backoff is a durable decision the adapter records, so a restart in the
middle of a backoff keeps waiting instead of hammering the API.

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


class GitHubClient(Protocol):
    def viewer(self) -> str:
        """Return the login the token acts as, used to filter self activity."""
        ...

    def snapshot(
        self, repo: str, pr: int, etags: dict[str, str], head_sha: str | None
    ) -> Snapshot:
        """Fetch every stream for one pull request, conditional on stored ETags."""
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


class RestGitHubClient:
    """Real read only client for api.github.com."""

    BASE = "https://api.github.com"
    PAGE_CAP = 20                          # 2000 items per stream bounds one catch up

    def __init__(self, token: str, base: str | None = None, timeout_s: float = 15.0) -> None:
        if not token:
            raise ValueError("a read only token is required for live mode")
        self._token = token
        self.base = (base or self.BASE).rstrip("/")
        self.timeout_s = timeout_s
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

    def _get_all(self, path: str, etags: dict[str, str], new: dict[str, str],
                 key: str | None = None) -> list[dict] | None:
        """Follow Link pagination. Return None when the stream answered 304.

        GitHub lists grow at the last page, so an unchanged first page proves nothing
        about later pages. An ETag is stored only for a stream that fit on one page.
        """
        sep = "&" if "?" in path else "?"
        first = f"{self.base}{path}{sep}per_page=100"
        url = first
        out: list[dict] = []
        first_etag = None
        for page in range(self.PAGE_CAP):
            status, body, headers = self._request(url, etags.get(first) if page == 0 else None)
            if status == 304:
                new[first] = etags.get(first, "")
                return None
            if page == 0:
                first_etag = headers.get("etag")
            items = body.get(key, []) if key and isinstance(body, dict) else body
            out.extend(items if isinstance(items, list) else [])
            m = _NEXT.search(headers.get("link", ""))
            if not m:
                if page == 0 and first_etag:
                    new[first] = first_etag
                break
            url = m.group(1)
        return out

    # ------------------------------------------------------------------ reads
    def viewer(self) -> str:
        _, body, _ = self._request(f"{self.base}/user", None)
        return (body or {}).get("login", "") if isinstance(body, dict) else ""

    def snapshot(
        self, repo: str, pr: int, etags: dict[str, str], head_sha: str | None
    ) -> Snapshot:
        repo_q = urllib.parse.quote(repo, safe="/")
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
        snap.reviews = self._get_all(f"/repos/{repo_q}/pulls/{pr}/reviews", etags, new) or []
        snap.review_comments = (
            self._get_all(f"/repos/{repo_q}/pulls/{pr}/comments", etags, new) or []
        )
        snap.issue_comments = (
            self._get_all(f"/repos/{repo_q}/issues/{pr}/comments", etags, new) or []
        )
        if head_sha:
            try:
                snap.check_runs = self._get_all(
                    f"/repos/{repo_q}/commits/{head_sha}/check-runs", etags, new,
                    key="check_runs",
                ) or []
            except AuthRevoked:
                # A token without the checks permission still monitors reviews and
                # comments; CI is reported unavailable instead of blocking everything.
                snap.checks_unavailable = True
        snap.etags = new
        return snap


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
        self, repo: str, pr: int, etags: dict[str, str], head_sha: str | None
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
