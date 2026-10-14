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
import urllib.parse
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


# ----------------------------------------------------------------- tail first pagination
REVIEWS = f"/repos/{REPO}/pulls/{PR}/reviews"


class PagedStream:
    """One GitHub list endpoint over a live, mutable item list.

    Serves `page` and `per_page` like GitHub: oldest first, a Link header with next, last,
    prev, and first, and an empty page past the end. `hits` records each page served, and
    `before` may mutate the list or return a canned response for a given page.
    """

    def __init__(self, items: list[dict], key: str | None = None) -> None:
        self.items = items
        self.key = key
        self.hits: list[int] = []
        self.before = None

    def __call__(self, h) -> tuple[int, object, dict]:
        url = urllib.parse.urlsplit(h.path)
        q = urllib.parse.parse_qs(url.query)
        page, per = int(q.get("page", ["1"])[0]), int(q.get("per_page", ["30"])[0])
        self.hits.append(page)
        if self.before is not None:
            canned = self.before(page)
            if canned is not None:
                return canned
        last = max(1, -(-len(self.items) // per))
        rows = self.items[(page - 1) * per: page * per]

        def link(n: int, rel: str) -> str:
            return f'<{{base}}{url.path}?per_page={per}&page={n}>; rel="{rel}"'

        rels = []
        if page < last:
            rels += [link(page + 1, "next"), link(last, "last")]
        if page > 1:
            rels += [link(min(page - 1, last), "prev"), link(1, "first")]
        body = {"total_count": len(self.items), self.key: rows} if self.key else rows
        return 200, body, {"Link": ", ".join(rels)} if rels else {}


def _reviews(lo: int, hi: int) -> list[dict]:
    return [_review(i, "COMMENTED") for i in range(lo, hi + 1)]


def _known(ids: set[str]):
    def known(stream: str, item: dict) -> bool:
        return f"{stream}:{item.get('id')}" in ids
    return known


def _ids(snap) -> list[int]:
    return [r["id"] for r in snap.reviews]


def _settled(items: list[dict]) -> tuple[set[str], dict]:
    """State after a complete prior read: every item seen, every cursor complete."""
    seen = {f"reviews:{r['id']}" for r in items}
    done = {"backfill": 0, "truncated": False, "episode": 0}
    return seen, {s: dict(done) for s in ("reviews", "review_comments", "issue_comments")}


def test_first_read_past_page_cap_reaches_the_tail(gh):
    """Issue #4: 25 pages of chronological reviews; the newest sits on page 25."""
    gh.routes[REVIEWS] = PagedStream(_reviews(1, 2450))
    snap = client(gh).snapshot(REPO, PR, {}, None)
    assert 2450 in _ids(snap)
    assert len(set(_ids(snap))) == len(_ids(snap))


def test_truncated_first_read_warns_then_backfills(gh):
    stream = PagedStream(_reviews(1, 2500))
    gh.routes[REVIEWS] = stream
    c = client(gh)
    first = c.snapshot(REPO, PR, {}, None)
    assert stream.hits == [1, *range(25, 6, -1)]               # page 1, then tail first
    assert first.truncated == ["reviews"]
    assert first.cursors["reviews"] == {"backfill": 6, "truncated": True, "episode": 1}

    stream.hits.clear()
    seen = {f"reviews:{i}" for i in _ids(first)}
    second = c.snapshot(REPO, PR, {}, None, known=_known(seen), cursors=first.cursors)
    assert stream.hits == [1, 25, 6, 5, 4, 3, 2]               # tail is settled; fill the hole
    assert second.truncated == []
    assert second.cursors["reviews"] == {"backfill": 0, "truncated": False, "episode": 1}
    assert set(_ids(first)) | set(_ids(second)) == set(range(1, 2501))


def test_steady_state_reads_page_one_and_the_tail_only(gh):
    items = _reviews(1, 2450)
    stream = PagedStream(items)
    gh.routes[REVIEWS] = stream
    c = client(gh)
    seen, cursors = _settled(items)

    quiet = c.snapshot(REPO, PR, {}, None, known=_known(seen), cursors=cursors)
    assert stream.hits == [1, 25]
    assert quiet.truncated == []

    stream.hits.clear()
    items.append(_review(2451))
    snap = c.snapshot(REPO, PR, {}, None, known=_known(seen), cursors=quiet.cursors)
    assert stream.hits == [1, 25, 24]                          # stop at the first settled page
    assert 2451 in _ids(snap)


@pytest.mark.parametrize("change", ["delete_then_append", "grow_two_pages"])
def test_page_boundary_changes_between_polls(gh, change):
    items = _reviews(1, 2500)                                  # page 25 exactly full
    gh.routes[REVIEWS] = PagedStream(items)
    seen, cursors = _settled(items)
    if change == "delete_then_append":
        del items[4]                                           # shifts every later item down
        items.append(_review(2501))                            # lands on old page 25, not 26
        new = {2501}
    else:
        items.extend(_reviews(2501, 2650))                     # pages 26 and 27 appear
        new = set(range(2501, 2651))
    snap = client(gh).snapshot(REPO, PR, {}, None, known=_known(seen), cursors=cursors)
    assert new <= set(_ids(snap))
    assert snap.truncated == []


def test_deletion_during_a_walk_loses_nothing(gh):
    items = _reviews(1, 2500)
    stream = PagedStream(items)
    gh.routes[REVIEWS] = stream
    seen, cursors = _settled(items)
    items.extend(_reviews(2501, 2800))                         # three new pages

    def before(page: int) -> None:
        if page == 27:                                         # mid walk: an old review goes
            del items[0]
    stream.before = before
    snap = client(gh).snapshot(REPO, PR, {}, None, known=_known(seen), cursors=cursors)
    assert set(range(2501, 2801)) <= set(_ids(snap))
    assert len(set(_ids(snap))) == len(_ids(snap))


@pytest.mark.parametrize("fault,exc", [
    ((403, {"message": "API rate limit exceeded"},
      {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"}), RateLimited),
    ((403, {"message": "secondary rate limit"}, {"retry-after": "30"}), RateLimited),
    ((502, {"message": "bad gateway"}, {}), TransientOutage),
])
def test_fault_mid_walk_raises_then_retry_recovers(gh, fault, exc):
    stream = PagedStream(_reviews(1, 2450))
    gh.routes[REVIEWS] = stream
    faults = [fault]
    stream.before = lambda page: faults.pop() if page == 13 and faults else None
    c = client(gh)
    with pytest.raises(exc):
        c.snapshot(REPO, PR, {}, None)
    snap = c.snapshot(REPO, PR, {}, None)                      # nothing persisted; redo
    assert 2450 in _ids(snap)


def test_pending_review_is_never_a_stop_page(gh):
    items = _reviews(1, 2450)
    stream = PagedStream(items)
    gh.routes[REVIEWS] = stream
    seen, cursors = _settled(items)
    items[2449]["state"] = "PENDING"                           # a draft is never marked seen
    seen.discard("reviews:2450")
    client(gh).snapshot(REPO, PR, {}, None, known=_known(seen), cursors=cursors)
    assert stream.hits == [1, 25, 24]


def test_capped_check_runs_report_truncation(gh):
    runs = [{"id": i, "name": f"job{i}", "status": "completed", "conclusion": "success",
             "completed_at": "2026-10-14T00:00:00Z"} for i in range(2100)]
    gh.routes[f"/repos/{REPO}/commits/abc/check-runs"] = PagedStream(runs, key="check_runs")
    snap = client(gh).snapshot(REPO, PR, {}, None)
    assert snap.truncated == ["check_runs"]
    assert snap.cursors["check_runs"]["episode"] == 1
