"""Subprocess worker for the monitor kill test.

Accepts a monitor responsibility, stages one approved review in a fake transport, records
its identity, polls, and sends itself SIGKILL after the companion write commits but before
the effect journal marks done. The parent recovers from durable state alone.

Invoked as: python monitor_worker.py <runtime_db> <uuid_out> <kill_label>
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.github_client import FakeGitHubClient
from gameshark.supervisor import AgentSupervisor
from sims.github_monitor import GitHubMonitorAdapter
from tests.helpers import HOST

REPO, PR = "acme/widget", 42


class KillSupervisor(AgentSupervisor):
    kill_label = ""

    def _maybe_crash(self, label: str) -> None:
        if label == self.kill_label:
            sys.stdout.flush()
            os.kill(os.getpid(), signal.SIGKILL)


def main() -> None:
    runtime_db, uuid_out, kill_label = sys.argv[1:4]

    client = FakeGitHubClient()
    client.add_review("r1", ordinal=100, state="approved")

    sup = KillSupervisor(db_path=runtime_db, agent_id="stark", owner_token=HOST)
    sup.kill_label = kill_label
    sup.add_adapter(GitHubMonitorAdapter(client))
    sup.start()

    Path(uuid_out).write_text(sup.identity.uuid, encoding="utf8")
    adapter = sup.registry.get("github")
    adapter.accept_responsibility(REPO, PR, scope="pull_requests:read")

    sup.poll()                       # reaches kill_label inside the notification effect
    sys.exit(99)                     # only if the kill did not fire


if __name__ == "__main__":
    main()
