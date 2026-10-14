"""Subprocess worker for a real process kill test.

The worker starts a supervisor, records its identity uuid to a sidecar file, submits a
pull request review event, and sends itself SIGKILL at a labeled crash point so no
cleanup runs and nothing in memory survives. The parent test then recovers from the
durable databases alone.

Invoked as: python crash_worker.py <runtime_db> <game_db> <github_db> <uuid_out> <kill_label>
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.supervisor import AgentSupervisor
from sims.game import GameAdapter, GameSimulator
from sims.github import MockGitHub, PrAdapter
from tests.helpers import HOST, pr_event


class KillSupervisor(AgentSupervisor):
    """Supervisor whose crash hook sends a real SIGKILL at one label."""

    kill_label = ""

    def _maybe_crash(self, label: str) -> None:
        if label == self.kill_label:
            sys.stdout.flush()
            os.kill(os.getpid(), signal.SIGKILL)


def main() -> None:
    runtime_db, game_db, github_db, uuid_out, kill_label = sys.argv[1:6]

    sup = KillSupervisor(db_path=runtime_db, agent_id="stark", owner_token=HOST)
    sup.kill_label = kill_label
    sup.add_adapter(GameAdapter(GameSimulator(game_db)))
    sup.add_adapter(PrAdapter(MockGitHub(github_db)))
    sup.start()

    # Record the durable identity before the kill so the parent can prove it survives.
    Path(uuid_out).write_text(sup.identity.uuid, encoding="utf8")

    sup.submit(pr_event())            # this reaches kill_label inside perform_once
    # If we get here the kill did not fire; exit nonzero to flag the test setup.
    sys.exit(99)


if __name__ == "__main__":
    main()
