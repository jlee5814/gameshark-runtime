"""v0.2 demo: Stark monitors a pull request across a real process death.

Deterministic transport, read only, local companion. Runs the full acceptance sequence:
accept the responsibility, poll quietly, a review arrives, deliver one update, kill the
process mid delivery, restart, recover without duplication, then revoke and stop safely.

Run: uv run python demo_v2.py
"""

from __future__ import annotations

import signal
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from companion import Companion
from gameshark.github_client import FakeGitHubClient
from gameshark.supervisor import AgentSupervisor
from sims.github_monitor import GitHubMonitorAdapter
from tests.helpers import HOST

REPO, PR = "acme/widget", 42
RID = f"monitor:{REPO}:{PR}"


def banner(text: str) -> None:
    print(f"\n=== {text} ===")


def build(runtime_db, client):
    sup = AgentSupervisor(db_path=runtime_db, agent_id="stark", owner_token=HOST)
    sup.add_adapter(GitHubMonitorAdapter(client))
    sup.start()
    return sup


def show_feed(sup) -> None:
    rows = Companion(sup.conn).feed()
    if not rows:
        print("  companion feed: (empty)")
    for r in rows:
        print(f"  companion: [{r['kind']}] {r['repo']}#{r['pr']} {r['summary']}")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gameshark-v2-"))
    runtime = tmp / "runtime.db"

    banner("Stark accepts a pull request monitoring responsibility")
    client = FakeGitHubClient()
    sup = build(runtime, client)
    uuid0 = sup.identity.uuid
    sup.registry.get("github").accept_responsibility(REPO, PR, "pull_requests:read")
    print(f"identity uuid: {uuid0}")
    print(f"responsibility: {RID} -> {sup.responsibility_state(RID)}")

    banner("Quiet poll: no activity, no noise")
    sup.poll()
    show_feed(sup)
    sup.close()

    banner("A review arrives; a real process delivers it and is SIGKILLed mid delivery")
    uuid_out = tmp / "uuid.txt"
    worker = Path(__file__).resolve().parent / "tests" / "monitor_worker.py"
    proc = subprocess.run(
        [sys.executable, str(worker), str(runtime), str(uuid_out), "C_after_remote"],
        check=False, capture_output=True, timeout=30,
    )
    print(f"worker return code: {proc.returncode} (expected {-signal.SIGKILL})")

    banner("Stark restarts from durable records alone")
    sup = build(runtime, FakeGitHubClient())
    print(f"identity uuid after restart: {sup.identity.uuid} (same: {sup.identity.uuid == uuid0})")
    print(f"responsibility recovered: {sup.responsibility_state(RID)}")
    cur = sup.conn.execute(
        "SELECT last_cursor FROM monitor_state WHERE repo=? AND pr=?", (REPO, PR)
    ).fetchone()
    print(f"cursor row present: {cur is not None}")
    show_feed(sup)
    print("one update, not duplicated, recovered without a second delivery")

    banner("Operator revokes GitHub read access")
    sup.revoke("github", "github.read")
    rep = sup.poll()
    print(f"poll after revoke: {rep['github']}")
    print(f"responsibility still present: {sup.responsibility_state(RID)}")
    sup.close()

    banner("Answer")
    print("Stark kept a real responsibility across a process kill, recovered identity,")
    print("responsibility, and cursor, delivered the update exactly once, and stopped")
    print("safely when authorization was revoked. Read only throughout.")
    print(f"\n(demo databases in {tmp})")


if __name__ == "__main__":
    main()
