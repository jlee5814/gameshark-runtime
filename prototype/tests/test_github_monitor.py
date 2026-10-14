"""v0.2 tests: GitHub pull request monitoring as Stark's first real responsibility.

Deterministic transport producing raw GitHub JSON, read only. Covers assignment,
classification, independent id streams, catch up, rate limits, outages, token
revocation, pull request close, dedup, and a real subprocess kill mid processing.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion import Companion
from gameshark.errors import CrashInjected
from gameshark.github_client import (
    AuthRevoked,
    FakeGitHubClient,
    RateLimited,
    RestGitHubClient,
    TransientOutage,
)
from gameshark.supervisor import AgentSupervisor
from sims.github_monitor import GitHubMonitorAdapter, backoff_delay, iso
from tests.helpers import HOST
from tests.test_github_rest import FakeGitHubServer, PagedStream

REPO, PR = "acme/widget", 42
RID = f"monitor:{REPO}:{PR}"
R1_KEY = "notify:acme/widget:42:new_review:review:r1"


def build_monitor(paths, clock, client, owner=HOST, crash_at=None, interval=0.0):
    sup = AgentSupervisor(
        db_path=paths["runtime"], agent_id="stark", owner_token=owner,
        clock=clock, crash_at=crash_at,
    )
    sup.add_adapter(GitHubMonitorAdapter(client, poll_interval_s=interval))
    sup.start()
    return sup


def watch(sup, baseline=False):
    return sup.registry.get("github").accept_responsibility(REPO, PR, baseline=baseline)


def feed(sup):
    return Companion(sup.conn).feed()


def kinds(sup):
    return [r["kind"] for r in feed(sup)]


def state(sup):
    return sup.conn.execute(
        "SELECT * FROM monitor_state WHERE repo=? AND pr=?", (REPO, PR)
    ).fetchone()


# ----------------------------------------------------------------- assign + quiet poll
def test_assign_then_quiet_poll_delivers_nothing(paths, clock):
    sup = build_monitor(paths, clock, FakeGitHubClient())
    watch(sup)
    rep = sup.poll()
    assert rep["github"]["submitted"] == 0
    assert feed(sup) == []
    assert sup.responsibility_state(RID) == "active"
    sup.close()


# ----------------------------------------------------------------- new review delivered once
def test_new_review_delivers_and_dedups_on_repoll(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.add_review("r1", ordinal=100, state="approved")

    sup.poll()
    rows = feed(sup)
    assert len(rows) == 1
    assert rows[0]["kind"] == "new_review"

    sup.poll()                                   # seen set holds r1; nothing new
    assert len(feed(sup)) == 1
    sup.close()


# ----------------------------------------------------------------- classification coverage
def test_classification_kinds_and_relevance(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.add_review("r1", 100, "changes_requested")
    client.add_comment("c1", 101, author="reviewer", body="the retry loop never backs off")
    client.add_comment("c2", 102, author="codecov[bot]", body="coverage 91%")
    client.add_comment("c3", 103, author="reviewer", body="LGTM")
    client.add_comment("c4", 104, author="stark-owner", body="fixed in the next push")
    client.add_check_run("ci1", 105, "failure")
    client.add_check_run("ci2", 106, "success", name="lint")

    sup.poll()
    assert set(kinds(sup)) == {"change_request", "comment", "ci_failure"}
    assert len(feed(sup)) == 3

    audit = {r["event_id"]: (r["kind"], r["relevant"]) for r in sup.conn.execute(
        "SELECT event_id, kind, relevant FROM classification_audit")}
    assert len(audit) == 7                                 # every decision is auditable
    assert audit["review:r1"] == ("change_request", 1)
    assert audit["issue_comment:c1"] == ("comment", 1)
    assert audit["issue_comment:c2"] == ("bot", 0)
    assert audit["issue_comment:c3"] == ("comment", 0)
    assert audit["issue_comment:c4"] == ("self", 0)
    assert audit["check:ci1:failure"] == ("ci_failure", 1)
    assert audit["check:ci2:success"] == ("ci", 0)
    sup.close()


# ----------------------------------------------------------------- independent id streams
def test_review_with_lower_id_than_prior_comment_is_not_lost(paths, clock):
    """Regression: one integer cursor across streams hid this review."""
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.add_comment("900", 900, author="reviewer", body="question about the cache")
    sup.poll()
    assert kinds(sup) == ["comment"]

    client.add_review("100", 950, "changes_requested")      # id 100 < comment id 900
    sup.poll()
    assert kinds(sup) == ["comment", "change_request"]
    sup.close()


def test_inline_comments_roll_up_into_their_review(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.add_review("r1", 100, "commented")
    for i in range(5):
        client.add_review_comment(f"rc{i}", 100, review_id="r1", author="reviewer")
    client.add_review_comment("rc_free", 101, review_id=None, author="reviewer")
    sup.poll()
    rows = feed(sup)
    assert [r["kind"] for r in rows] == ["review_comment", "review_comment"]
    assert "5 inline comments" in rows[0]["summary"]
    sup.close()


# ----------------------------------------------------------------- CI lifecycle
def test_ci_in_progress_then_failure_then_recovery(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.add_check_run("run1", 100, None, name="tests")   # in progress: not a fact yet
    sup.poll()
    assert feed(sup) == []

    client.complete_check_run("run1", 110, "failure")
    sup.poll()
    sup.poll()
    assert kinds(sup) == ["ci_failure"]

    client.push("sha2")                                      # new head commit
    client.add_check_run("run2", 120, "success", name="tests")
    sup.poll()
    assert kinds(sup) == ["ci_failure", "ci_recovered"]

    client.add_check_run("run3", 130, "success", name="tests")
    sup.poll()
    assert kinds(sup) == ["ci_failure", "ci_recovered"]      # repeat success is routine
    sup.close()


def test_ci_cancelled_on_head_notifies_once(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.add_check_run("run1", 100, "cancelled", name="tests")
    sup.poll()
    sup.poll()                                               # repoll: seen set holds it
    rows = feed(sup)
    assert [r["kind"] for r in rows] == ["ci_cancelled"]
    assert "cancelled" in rows[0]["summary"]
    sup.close()


def test_ci_success_after_cancelled_is_recovery(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.add_check_run("run1", 100, "cancelled", name="tests")
    sup.poll()
    client.add_check_run("run2", 110, "success", name="tests")   # rerun on the same head
    sup.poll()
    sup.poll()
    assert kinds(sup) == ["ci_cancelled", "ci_recovered"]
    sup.close()


def test_ci_cancelled_again_on_rerun_does_not_duplicate(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.add_check_run("run1", 100, "cancelled", name="tests")
    sup.poll()
    client.add_check_run("run2", 110, "cancelled", name="tests")
    sup.poll()
    sup.poll()
    assert kinds(sup) == ["ci_cancelled"]
    audit = {r["event_id"]: (r["kind"], r["relevant"]) for r in sup.conn.execute(
        "SELECT event_id, kind, relevant FROM classification_audit")}
    assert audit["check:run1:cancelled"] == ("ci_cancelled", 1)
    assert audit["check:run2:cancelled"] == ("ci", 0)
    sup.close()


def test_ci_cancelled_on_superseded_commit_stays_silent(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.add_check_run("run1", 100, None, name="tests")
    sup.poll()
    client.push("sha2")                                      # cancel in progress on push
    client.complete_check_run("run1", 110, "cancelled")
    client.add_check_run("run2", 120, "success", name="tests")
    sup.poll()
    assert feed(sup) == []                                   # old head is not watched
    sup.close()


def test_checks_unavailable_degrades_without_blocking(paths, clock):
    client = FakeGitHubClient()
    client.checks_unavailable = True
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.add_review("r1", 100, "approved")
    sup.poll()
    assert kinds(sup) == ["new_review"]
    assert state(sup)["last_error"] == "CI unavailable to this token"
    assert sup.responsibility_state(RID) == "active"
    sup.close()


# ----------------------------------------------------------------- baseline
def test_baseline_records_history_without_notifying(paths, clock):
    client = FakeGitHubClient()
    client.add_review("old", 10, "approved")
    client.add_comment("oldc", 11, author="reviewer")
    clock.advance(1_700_000_015 - clock.now())             # watch after the history
    sup = build_monitor(paths, clock, client)
    watch(sup, baseline=True)
    client.stage_fault(AuthRevoked(401, "Bad credentials"))
    sup.poll()                                             # first read fails: blocked
    client.add_review("during", 16, "approved")            # lands while blocked
    sup.registry.get("github").resume(REPO, PR)
    sup.poll()
    assert kinds(sup) == ["system", "new_review"]          # history silent, new work kept
    audit = {r["event_id"]: r["reason"] for r in sup.conn.execute(
        "SELECT event_id, reason FROM classification_audit")}
    assert audit["review:old"].startswith("baseline: ")
    assert audit["issue_comment:oldc"].startswith("baseline: ")

    client.add_review("new", 20, "changes_requested")
    sup.poll()
    assert kinds(sup) == ["system", "new_review", "change_request"]
    sup.close()


# ----------------------------------------------------------------- outages and rate limits
def test_outage_backs_off_durably_then_catches_up(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.add_review("r1", 100, "approved")
    client.add_review("r2", 101, "changes_requested")
    client.stage_fault(TransientOutage("blip"))
    client.stage_fault(TransientOutage("blip again"))

    rep = sup.poll()
    assert rep["github"]["deferred"] and feed(sup) == []
    assert state(sup)["fail_count"] == 1
    assert state(sup)["next_poll_at"] == clock.now() + backoff_delay(1)
    sup.close()

    # Restart during the backoff: the wait is durable, so no request goes out early.
    calls = client.calls
    sup = build_monitor(paths, clock, client)
    rep = sup.poll()
    assert client.calls == calls and rep["github"]["waiting"]

    clock.advance(backoff_delay(1))
    sup.poll()                                              # second fault
    assert state(sup)["fail_count"] == 2
    assert state(sup)["next_poll_at"] == clock.now() + backoff_delay(2)

    clock.advance(backoff_delay(2))
    sup.poll()
    assert kinds(sup) == ["new_review", "change_request"]   # both, in order, none missed
    assert state(sup)["fail_count"] == 0 and state(sup)["status"] == "ok"
    sup.close()


def test_rate_limit_pauses_until_reset_and_long_waits_notify_once(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.stage_fault(RateLimited(120))
    sup.poll()
    assert state(sup)["status"] == "backoff" and feed(sup) == []   # short wait: silent
    calls = client.calls
    clock.advance(60)
    sup.poll()
    assert client.calls == calls                                    # honored the reset
    clock.advance(60)

    client.stage_fault(RateLimited(3600))
    sup.poll()
    assert kinds(sup) == ["system"]                                 # long wait: tell the user
    clock.advance(3600)
    client.add_review("r1", 100, "approved")
    sup.poll()
    assert kinds(sup) == ["system", "new_review"]
    sup.close()


def test_offline_gap_catches_up_and_reports(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    sup.poll()
    sup.close()

    clock.advance(6 * 3600)                                 # laptop asleep
    for i in range(30):
        client.add_comment(f"c{i}", 100 + i, author="reviewer", body=f"point {i}")
    sup = build_monitor(paths, clock, client)
    sup.poll()
    ks = kinds(sup)
    assert ks.count("comment") == 30 and ks.count("system") == 1
    sup.close()


# ----------------------------------------------------------------- token revoked at GitHub
def test_github_auth_revoked_blocks_notifies_once_and_resumes_explicitly(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.stage_fault(AuthRevoked(401, "Bad credentials"))
    rep = sup.poll()
    assert rep["github"]["blocked"]
    assert sup.responsibility_state(RID) == "blocked"
    assert kinds(sup) == ["system"]
    assert state(sup)["status"] == "auth_revoked"
    sup.close()

    # Restart: still blocked, no polling, no second notice.
    sup = build_monitor(paths, clock, client)
    calls = client.calls
    sup.poll()
    assert client.calls == calls and kinds(sup) == ["system"]
    assert sup.responsibility_state(RID) == "blocked"

    # Operator fixes the token and resumes; monitoring continues with nothing lost.
    client.add_review("r1", 100, "approved")
    sup.registry.get("github").resume(REPO, PR)
    sup.poll()
    assert sup.responsibility_state(RID) == "active"
    assert kinds(sup) == ["system", "new_review"]

    # A second revocation is a new episode and earns its own notice.
    client.stage_fault(AuthRevoked(404, "Not Found"))
    sup.poll()
    assert kinds(sup) == ["system", "new_review", "system"]
    sup.close()


# ----------------------------------------------------------------- close completes
def test_merged_pr_notifies_and_completes_responsibility(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.close(merged=True)
    sup.poll()
    assert kinds(sup) == ["pr_state"]
    assert sup.responsibility_state(RID) == "done"
    calls = client.calls
    sup.poll()
    assert client.calls == calls                            # done: no more polling
    sup.close()


# ----------------------------------------------------------------- dedup on duplicate delivery
def test_duplicate_inbox_delivery_absorbed(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.add_review("r1", 100, "approved")
    sup.poll()
    assert len(feed(sup)) == 1

    duplicate = {
        "event_id": f"gh:{REPO}:{PR}:review:r1",
        "adapter": "github", "type": "gh_activity",
        "payload": {"event_id": "review:r1", "raw_kind": "review", "state": "approved",
                    "author": "octo", "repo": REPO, "pr": PR},
    }
    assert sup.submit(duplicate) == "duplicate"
    assert len(feed(sup)) == 1
    sup.close()


# ----------------------------------------------------------------- revoke stops safely
def test_revoke_github_read_stops_monitoring_safely(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    watch(sup)
    client.add_review("r1", 100, "approved")

    sup.revoke("github", "github.read")
    rep = sup.poll()
    assert rep["github"]["status"] == "stopped"
    assert client.calls == 0                           # no read at all
    assert feed(sup) == []
    assert sup.responsibility_state(RID) == "active"   # responsibility intact, just paused
    sup.close()


# ----------------------------------------------------------------- in-process crash + recover
def test_crash_mid_delivery_recovers_without_duplicate(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client, crash_at={"C_after_remote"})
    watch(sup)
    client.add_review("r1", 100, "approved")
    uuid1 = sup.identity.uuid
    with pytest.raises(CrashInjected):
        sup.poll()                                     # companion write commits, then crash
    assert len(feed(sup)) == 1                          # delivered to companion
    assert sup.effect_status(R1_KEY) == "pending"
    sup.close()

    sup2 = build_monitor(paths, clock, client)          # restart on the same remote
    assert sup2.identity.uuid == uuid1
    sup2.poll()                                          # resubmits r1: inbox dedups
    assert len(feed(sup2)) == 1                          # reconciled, no duplicate
    assert sup2.effect_status(R1_KEY) == "done"
    assert sup2.responsibility_state(RID) == "active"
    sup2.close()


# ----------------------------------------------------------------- real subprocess kill
def test_real_subprocess_kill_mid_monitor_then_recover(paths, tmp_path, clock):
    uuid_out = tmp_path / "monitor_uuid.txt"
    worker = Path(__file__).resolve().parent / "monitor_worker.py"
    proc = subprocess.run(
        [sys.executable, str(worker), str(paths["runtime"]), str(uuid_out), "C_after_remote"],
        check=False, capture_output=True, timeout=30,
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr.decode()
    worker_uuid = uuid_out.read_text(encoding="utf8").strip()

    client = FakeGitHubClient()
    client.add_review("r1", ordinal=100, state="approved")
    sup = build_monitor(paths, clock, client)
    assert sup.identity.uuid == worker_uuid             # identity survived a real kill
    assert len(feed(sup)) == 1                           # exactly one notification
    assert sup.effect_status(R1_KEY) == "done"
    assert sup.responsibility_state(RID) == "active"
    sup.poll()                                           # same remote again: still one
    assert len(feed(sup)) == 1
    sup.close()


# ----------------------------------------------------------------- pagination past 20 pages
# The real REST client against the local GitHub server, ten items per page so 25 pages
# stay cheap to journal. Reviews arrive oldest first, exactly as GitHub lists them.
@pytest.fixture
def paged_gh():
    srv = FakeGitHubServer()
    reviews = PagedStream([])
    p = f"/repos/{REPO}"
    srv.routes.update({
        "/user": (200, {"login": "stark-owner"}, {}),
        f"{p}/pulls/{PR}": (200, {"number": PR, "state": "open", "head": {"sha": "abc"},
                                  "user": {"login": "stark-owner", "type": "User"}}, {}),
        f"{p}/pulls/{PR}/reviews": reviews,
        f"{p}/pulls/{PR}/comments": (200, [], {}),
        f"{p}/issues/{PR}/comments": (200, [], {}),
        f"{p}/commits/abc/check-runs": (200, {"total_count": 0, "check_runs": []}, {}),
    })
    yield srv, reviews
    srv.stop()


def _rest(srv) -> RestGitHubClient:
    return RestGitHubClient("test-token", base=srv.base, timeout_s=5, per_page=10)


def _gh_review(i: int, epoch: float, state: str = "COMMENTED") -> dict:
    return {"id": i, "state": state, "user": {"login": "reviewer", "type": "User"},
            "body": "", "submitted_at": iso(epoch), "html_url": f"https://github.test/r/{i}"}


def _history(n: int, clock) -> list[dict]:
    return [_gh_review(i, clock.now() - 10_000 + i) for i in range(1, n + 1)]


def seen_count(sup) -> int:
    return sup.conn.execute("SELECT COUNT(*) FROM monitor_seen").fetchone()[0]


def test_review_on_page_25_delivers_once_and_history_stays_quiet(paths, clock, paged_gh):
    srv, reviews = paged_gh
    reviews.items.extend(_history(241, clock))                 # 25 pages, oldest first
    sup = build_monitor(paths, clock, _rest(srv))
    watch(sup, baseline=True)

    sup.poll()                                                 # cap hit: tail first, warn
    assert kinds(sup) == ["system"]
    assert "reviews" in feed(sup)[0]["summary"]
    sup.poll()                                                 # backfill finishes the hole
    assert seen_count(sup) == 241
    assert json.loads(state(sup)["page_cursors"])["reviews"]["backfill"] == 0

    clock.advance(60)
    reviews.items.append(_gh_review(242, clock.now(), "CHANGES_REQUESTED"))   # page 25
    reviews.hits.clear()
    sup.poll()
    assert reviews.hits == [1, 25, 24]                         # no historical refetch
    sup.poll()
    assert kinds(sup) == ["system", "change_request"]          # once, and one warning only
    audit = {r["event_id"]: r["reason"] for r in sup.conn.execute(
        "SELECT event_id, reason FROM classification_audit")}
    assert all(audit[f"review:{i}"].startswith("baseline: ") for i in range(1, 242))
    sup.close()


def test_backfill_resumes_across_restart_without_duplicates(paths, clock, paged_gh):
    srv, reviews = paged_gh
    reviews.items.extend(_history(300, clock))                 # 30 pages
    sup = build_monitor(paths, clock, _rest(srv))
    watch(sup, baseline=True)
    sup.poll()
    assert json.loads(state(sup)["page_cursors"])["reviews"]["backfill"] == 11
    sup.close()

    reviews.hits.clear()
    sup = build_monitor(paths, clock, _rest(srv))              # fresh process, same files
    sup.poll()
    assert reviews.hits == [1, 30, *range(11, 1, -1)]
    assert seen_count(sup) == 300

    clock.advance(60)
    reviews.items.append(_gh_review(301, clock.now(), "APPROVED"))
    sup.poll()
    sup.close()
    sup = build_monitor(paths, clock, _rest(srv))
    sup.poll()
    assert kinds(sup) == ["system", "new_review"]
    sup.close()


def test_rate_limit_mid_walk_backs_off_and_loses_nothing(paths, clock, paged_gh):
    srv, reviews = paged_gh
    reviews.items.extend(_history(241, clock))
    sup = build_monitor(paths, clock, _rest(srv))
    watch(sup, baseline=True)
    sup.poll()
    sup.poll()
    clock.advance(60)
    reviews.items.extend(_gh_review(i, clock.now() + i, "APPROVED") for i in range(242, 262))
    limited = [(403, {"message": "API rate limit exceeded"},
                {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 300)})]
    reviews.before = lambda page: limited.pop() if page == 26 and limited else None

    rep = sup.poll()
    assert rep["github"]["deferred"] == [f"{REPO}#{PR}:RateLimited"]
    assert kinds(sup) == ["system"]
    sup.close()

    sup = build_monitor(paths, clock, _rest(srv))              # restart during the wait
    clock.advance(400)
    sup.poll()
    sup.poll()
    assert kinds(sup) == ["system"] + ["new_review"] * 20
    assert seen_count(sup) == 261
    sup.close()


def test_crash_while_marking_seen_never_strands_a_hole(paths, clock, paged_gh, monkeypatch):
    """A crash mid poll leaves seen ids above an unread hole; the next walk must not stop."""
    srv, reviews = paged_gh
    reviews.items.extend(_history(241, clock))
    sup = build_monitor(paths, clock, _rest(srv))
    watch(sup, baseline=True)
    sup.poll()
    sup.poll()                                                 # settled
    clock.advance(60)
    reviews.items.extend(_gh_review(i, clock.now() + i) for i in range(242, 492))  # 25 pages

    marks = []
    real = GitHubMonitorAdapter._mark_seen

    def crash_after_fifty(self, repo, pr, ev):
        if len(marks) == 50:
            raise CrashInjected("mid mark seen")
        marks.append(ev["event_id"])
        real(self, repo, pr, ev)
    monkeypatch.setattr(GitHubMonitorAdapter, "_mark_seen", crash_after_fifty)
    with pytest.raises(CrashInjected):
        sup.poll()
    sup.close()
    monkeypatch.setattr(GitHubMonitorAdapter, "_mark_seen", real)

    sup = build_monitor(paths, clock, _rest(srv))
    for _ in range(3):
        sup.poll()
    assert seen_count(sup) == 491                              # every new review, none stranded
    assert json.loads(state(sup)["page_cursors"])["reviews"]["backfill"] == 0
    sup.close()
