"""Subprocess worker for real SIGKILL tests at the inference crash points E1 to E4.

The worker starts a supervisor with the assistant adapter, records its identity uuid,
submits one ask event, and sends itself SIGKILL at the labeled crash point, so no cleanup
runs and nothing in memory survives. The parent recovers from the durable files alone.

Invoked as: python inference_worker.py <runtime_db> <game_db> <uuid_out> <kill_label>
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.inference import FakeProvider
from gameshark.supervisor import AgentSupervisor
from sims.game import GameSimulator
from tests.helpers import AGENT, HOST
from tests.inference_adapter import AssistantAdapter, ask_event


class KillSupervisor(AgentSupervisor):
    kill_label = ""

    def _maybe_crash(self, label: str) -> None:
        if label == self.kill_label:
            sys.stdout.flush()
            os.kill(os.getpid(), signal.SIGKILL)


def main() -> None:
    runtime_db, game_db, uuid_out, kill_label = sys.argv[1:5]
    sup = KillSupervisor(db_path=runtime_db, agent_id=AGENT, owner_token=HOST)
    sup.kill_label = kill_label
    sup.add_adapter(AssistantAdapter(FakeProvider(), GameSimulator(game_db)))
    sup.start()
    Path(uuid_out).write_text(sup.identity.uuid, encoding="utf8")
    sup.submit(ask_event())          # reaches kill_label inside InferenceJournal.complete
    sys.exit(99)


if __name__ == "__main__":
    main()
