"""v0.2 tests: GitHub pull request monitoring as Stark's first real responsibility.

Deterministic transport, read only. Covers assignment, classification, pagination and
faults, dedup, a real subprocess kill mid processing, and safe stop on revocation.
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion import Companion
from gameshark.errors import CrashInjected
from gameshark.github_client import FakeGitHubClient, TransientOutage
from gameshark.supervisor import AgentSupervisor
from sims.github_monitor import GitHubMonitorAdapter
from tests.helpers import HOST

REPO, PR = "acme/widget", 42
RID = f"monitor:{REPO}:{PR}"


def build_monitor(paths, clock, client, owner=HOST, crash_at=None):
    sup = AgentSupervisor(
        db_path=paths["runtime"], agent_id="stark", owner_token=owner,
        clock=clock, crash_at=crash_at,
    )
    sup.add_adapter(GitHubMonitorAdapter(client))
    sup.start()
    return sup


def feed(sup):
    return Companion(sup.conn).feed()


# ----------------------------------------------------------------- assign + quiet poll
def test_assign_then_quiet_poll_delivers_nothing(paths, clock):
    sup = build_monitor(paths, clock, FakeGitHubClient())
    sup.registry.get("github").accept_responsibility(REPO, PR, "pull_requests:read")
    rep = sup.poll()
    assert rep["github"]["submitted"] == 0
    assert feed(sup) == []
    assert sup.responsibility_state(RID) == "active"
    sup.close()


# ----------------------------------------------------------------- new review delivered once
def test_new_review_delivers_and_dedups_on_repoll(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    sup.registry.get("github").accept_responsibility(REPO, PR, "pull_requests:read")
    client.add_review("r1", ordinal=100, state="approved")

    sup.poll()
    rows = feed(sup)
    assert len(rows) == 1
    assert rows[0]["kind"] == "new_review"

    sup.poll()                                   # cursor advanced; nothing new
    assert len(feed(sup)) == 1
    sup.close()


# ----------------------------------------------------------------- classification coverage
def test_classification_kinds_and_relevance(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    sup.registry.get("github").accept_responsibility(REPO, PR, "pull_requests:read")
    client.add_review("r1", 100, "changes_requested")
    client.add_comment("c1", 101)
    client.add_check_run("ci1", 102, "failure")
    client.add_check_run("ci2", 103, "success")

    sup.poll()
    delivered = {r["kind"] for r in feed(sup)}
    assert delivered == {"change_request", "ci"}      # comment and CI success not delivered

    audit = sup.conn.execute(
        "SELECT event_id, kind, relevant FROM classification_audit ORDER BY event_id"
    ).fetchall()
    assert len(audit) == 4                            # every decision is auditable
    rels = {r["event_id"]: r["relevant"] for r in audit}
    assert rels["c1"] == 0 and rels["ci2"] == 0
    assert rels["r1"] == 1 and rels["ci1"] == 1
    sup.close()


# ----------------------------------------------------------------- pagination + faults
def test_pagination_rate_limit_and_outage_resume(paths, clock):
    client = FakeGitHubClient(page_size=1)
    sup = build_monitor(paths, clock, client)
    sup.registry.get("github").accept_responsibility(REPO, PR, "pull_requests:read")
    client.add_review("r1", 100, "approved")
    client.add_review("r2", 101, "changes_requested")
    client.stage_fault(TransientOutage("blip"))

    rep = sup.poll()                                  # fault: deferred, nothing missed
    assert rep["github"]["deferred"] and rep["github"]["submitted"] == 0
    assert feed(sup) == []

    sup.poll()                                        # page 1 -> r1
    sup.poll()                                        # page 2 -> r2
    kinds = [r["kind"] for r in feed(sup)]
    assert kinds == ["new_review", "change_request"]  # both delivered, in order, no miss
    sup.close()


# ----------------------------------------------------------------- dedup on duplicate delivery
def test_duplicate_inbox_delivery_absorbed(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    sup.registry.get("github").accept_responsibility(REPO, PR, "pull_requests:read")
    client.add_review("r1", 100, "approved")
    sup.poll()
    assert len(feed(sup)) == 1

    duplicate = {
        "event_id": f"gh:{REPO}:{PR}:r1",
        "adapter": "github", "type": "gh_activity",
        "payload": {"event_id": "r1", "raw_kind": "review", "ordinal": 100,
                    "state": "approved", "author": "octo", "title": "review approved",
                    "repo": REPO, "pr": PR},
    }
    assert sup.submit(duplicate) == "duplicate"
    assert len(feed(sup)) == 1
    sup.close()


# ----------------------------------------------------------------- revoke stops safely
def test_revoke_github_read_stops_monitoring_safely(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client)
    sup.registry.get("github").accept_responsibility(REPO, PR, "pull_requests:read")
    client.add_review("r1", 100, "approved")

    sup.revoke("github", "github.read")
    rep = sup.poll()
    assert rep["github"]["status"] == "stopped"
    assert feed(sup) == []
    assert sup.responsibility_state(RID) == "active"   # responsibility intact, just paused
    sup.close()


# ----------------------------------------------------------------- in-process crash + recover
def test_crash_mid_delivery_recovers_without_duplicate(paths, clock):
    client = FakeGitHubClient()
    sup = build_monitor(paths, clock, client, crash_at={"C_after_remote"})
    sup.registry.get("github").accept_responsibility(REPO, PR, "pull_requests:read")
    client.add_review("r1", 100, "approved")
    uuid1 = sup.identity.uuid
    with pytest.raises(CrashInjected):
        sup.poll()                                     # companion write commits, then crash
    assert len(feed(sup)) == 1                          # delivered to companion
    assert sup.effect_status("notify:acme/widget:42:new_review:r1") == "pending"
    sup.close()

    sup2 = build_monitor(paths, clock, FakeGitHubClient())   # restart, recovery needs no client
    assert sup2.identity.uuid == uuid1
    assert len(feed(sup2)) == 1                          # reconciled, no duplicate
    assert sup2.effect_status("notify:acme/widget:42:new_review:r1") == "done"
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

    sup = build_monitor(paths, clock, FakeGitHubClient())
    assert sup.identity.uuid == worker_uuid             # identity survived a real kill
    assert len(feed(sup)) == 1                           # exactly one notification
    assert sup.effect_status("notify:acme/widget:42:new_review:r1") == "done"
    assert sup.responsibility_state(RID) == "active"
    # cursor state survived too
    row = sup.conn.execute(
        "SELECT last_cursor FROM monitor_state WHERE repo=? AND pr=?", (REPO, PR)
    ).fetchone()
    assert row is not None
    sup.close()
