"""RestGitHubClient against a local HTTP server that speaks GitHub's wire shapes.

No network and no token leave the machine. Covers pagination, ETag 304 handling, primary
and secondary rate limits, token refusal, outages, CI permission degradation, and the
read only property.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark import github_client
from gameshark.github_client import (
    AuthRevoked,
    RateLimited,
    RestGitHubClient,
    TransientOutage,
)
from gameshark.github_events import normalize

REPO, PR = "acme/widget", 7


class FakeGitHubServer:
    """Routes map a path to (status, body, headers). A list of routes plays in order."""

    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.log: list[tuple[str, str, dict]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:
                pass

            def _serve(self) -> None:
                server.log.append((self.command, self.path, dict(self.headers)))
                path = self.path.split("?")[0]
                route = server.routes.get(path, (404, {"message": "Not Found"}, {}))
                if isinstance(route, list):
                    route = route.pop(0) if len(route) > 1 else route[0]
                if callable(route):
                    route = route(self)
                status, body, headers = route
                payload = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v.replace("{base}", server.base))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _serve
            do_POST = _serve
            do_PATCH = _serve
            do_PUT = _serve
            do_DELETE = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, args=(0.02,), daemon=True).start()

    def stop(self) -> None:
        self.httpd.shutdown()


@pytest.fixture
def gh():
    srv = FakeGitHubServer()
    p = f"/repos/{REPO}"
    srv.routes.update({
        "/user": (200, {"login": "stark-owner"}, {}),
        f"{p}/pulls/{PR}": (200, {"number": PR, "state": "open", "head": {"sha": "abc"},
                                   "user": {"login": "stark-owner", "type": "User"}},
                            {"ETag": '"pr1"'}),
        f"{p}/pulls/{PR}/reviews": (200, [], {"ETag": '"rv1"'}),
        f"{p}/pulls/{PR}/comments": (200, [], {}),
        f"{p}/issues/{PR}/comments": (200, [], {}),
        f"{p}/commits/abc/check-runs": (200, {"total_count": 0, "check_runs": []}, {}),
    })
    yield srv
    srv.stop()


def client(gh) -> RestGitHubClient:
    return RestGitHubClient("test-token", base=gh.base, timeout_s=5)


def _review(i: int, state: str = "APPROVED") -> dict:
    return {"id": i, "state": state, "user": {"login": f"u{i}", "type": "User"},
            "body": "", "submitted_at": f"2026-10-14T00:00:{i % 60:02d}Z"}


def test_snapshot_reads_every_stream_and_normalizes(gh):
    p = f"/repos/{REPO}"
    gh.routes[f"{p}/pulls/{PR}/reviews"] = (200, [_review(5)], {})
    gh.routes[f"{p}/issues/{PR}/comments"] = (200, [{"id": 9, "body": "hi",
        "user": {"login": "a", "type": "User"}, "created_at": "2026-10-14T00:01:00Z"}], {})
    gh.routes[f"{p}/commits/abc/check-runs"] = (200, {"total_count": 1, "check_runs": [
        {"id": 3, "name": "tests", "status": "completed", "conclusion": "failure",
         "completed_at": "2026-10-14T00:02:00Z", "app": {"slug": "github-actions"}}]}, {})
    snap = client(gh).snapshot(REPO, PR, {}, None)
    ids = [e["event_id"] for e in normalize(snap)]
    assert ids == ["review:5", "issue_comment:9", "check:3:failure"]
    assert snap.head_sha == "abc"
    auth = [h.get("Authorization") for _, _, h in gh.log]
    assert all(a == "Bearer test-token" for a in auth)


def test_pagination_follows_link_and_skips_etag_for_multipage(gh):
    p = f"/repos/{REPO}/pulls/{PR}/reviews"
    gh.routes[p] = lambda h: (
        (200, [_review(2)], {"ETag": '"p2"'}) if "page=2" in h.path else
        (200, [_review(1)], {"ETag": '"p1"', "Link": f'<{{base}}{p}?per_page=100&page=2>; '
                                                     f'rel="next"'})
    )
    snap = client(gh).snapshot(REPO, PR, {}, None)
    assert [r["id"] for r in snap.reviews] == [1, 2]
    assert not any(k.endswith("/reviews?per_page=100") for k in snap.etags)


def test_etag_304_survives_and_yields_no_items(gh):
    c = client(gh)
    first = c.snapshot(REPO, PR, {}, None)
    key = f"{gh.base}/repos/{REPO}/pulls/{PR}/reviews?per_page=100"
    assert first.etags[key] == '"rv1"'
    gh.routes[f"/repos/{REPO}/pulls/{PR}/reviews"] = lambda h: (
        (304, None, {}) if h.headers.get("If-None-Match") == '"rv1"' else (200, [], {}))
    second = c.snapshot(REPO, PR, first.etags, first.head_sha)
    assert second.reviews == [] and second.etags[key] == '"rv1"'


@pytest.mark.parametrize("status,headers,body,exc", [
    (401, {}, {"message": "Bad credentials"}, AuthRevoked),
    (403, {}, {"message": "Resource not accessible"}, AuthRevoked),
    (404, {}, {"message": "Not Found"}, AuthRevoked),
    (403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"}, {}, RateLimited),
    (403, {"retry-after": "30"}, {"message": "secondary rate limit"}, RateLimited),
    (429, {}, {"message": "slow down"}, RateLimited),
    (502, {}, {"message": "bad gateway"}, TransientOutage),
])
def test_http_failures_map_to_typed_errors(gh, status, headers, body, exc):
    gh.routes[f"/repos/{REPO}/pulls/{PR}"] = (status, body, headers)
    with pytest.raises(exc):
        client(gh).snapshot(REPO, PR, {}, None)


def test_secondary_rate_limit_carries_retry_after(gh):
    gh.routes[f"/repos/{REPO}/pulls/{PR}"] = (403, {}, {"retry-after": "45"})
    with pytest.raises(RateLimited) as ei:
        client(gh).snapshot(REPO, PR, {}, None)
    assert ei.value.retry_after_s == 45


def test_connection_refused_is_transient():
    c = RestGitHubClient("t", base="http://127.0.0.1:9", timeout_s=2)
    with pytest.raises(TransientOutage):
        c.snapshot(REPO, PR, {}, None)


def test_checks_forbidden_degrades_instead_of_blocking(gh):
    gh.routes[f"/repos/{REPO}/commits/abc/check-runs"] = (403, {"message": "no"}, {})
    snap = client(gh).snapshot(REPO, PR, {}, None)
    assert snap.checks_unavailable and snap.check_runs == []


def test_client_only_ever_sends_get(gh):
    c = client(gh)
    c.viewer()
    c.snapshot(REPO, PR, {}, None)
    assert gh.log and {m for m, _, _ in gh.log} == {"GET"}
    src = inspect.getsource(github_client)
    assert re.findall(r'method="(\w+)"', src) == ["GET"]
    public = {n for n in dir(RestGitHubClient) if not n.startswith("_")}
    assert public <= {"BASE", "PAGE_CAP", "snapshot", "viewer"}
