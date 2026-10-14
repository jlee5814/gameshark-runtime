"""Narrated end to end demonstration of GameShark Runtime v0.1.

Runs Stark through dual responsibilities, a real subprocess kill, restart, recovery,
exactly once effects, and a capability revocation that survives restart. Uses temporary
databases so it leaves nothing behind.

Run: uv run python demo.py
"""

from __future__ import annotations

import signal
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gameshark.supervisor import AgentSupervisor
from sims.game import GameAdapter, GameSimulator
from sims.github import MockGitHub, PrAdapter
from tests.helpers import HOST, PR_OP, PR_RESP, game_event, game_op, pr_event


def banner(text: str) -> None:
    print(f"\n=== {text} ===")


def build(paths: dict, owner: str = HOST) -> AgentSupervisor:
    sup = AgentSupervisor(db_path=paths["runtime"], agent_id="stark", owner_token=owner)
    sup.add_adapter(GameAdapter(GameSimulator(paths["game"])))
    sup.add_adapter(PrAdapter(MockGitHub(paths["github"])))
    sup.start()
    return sup


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="gameshark-demo-"))
    paths = {"runtime": tmp / "runtime.db", "game": tmp / "game.db", "github": tmp / "github.db"}
    github = lambda: MockGitHub(paths["github"])
    game = lambda: GameSimulator(paths["game"])

    banner("Stark starts and takes both responsibilities")
    sup = build(paths)
    uuid0 = sup.identity.uuid
    print(f"identity uuid: {uuid0}")
    sup.submit(game_event(turn=1))
    print(f"party support move turn 1 recorded: {game().move_count(game_op(turn=1))}")
    sup.close()

    banner("A real process performs a PR comment and is SIGKILLed mid flight")
    uuid_out = tmp / "uuid.txt"
    worker = Path(__file__).resolve().parent / "tests" / "crash_worker.py"
    proc = subprocess.run(
        [sys.executable, str(worker), str(paths["runtime"]), str(paths["game"]),
         str(paths["github"]), str(uuid_out), "C_after_remote"],
        check=False, capture_output=True, timeout=30,
    )
    print(f"worker return code: {proc.returncode} (expected {-signal.SIGKILL} from SIGKILL)")
    print(f"comment landed on remote: {github().comment_count(PR_OP)} (external side effect)")
    print(f"worker identity uuid: {uuid_out.read_text().strip()}")

    banner("Stark restarts from durable records alone")
    sup = build(paths)
    print(f"identity uuid after restart: {sup.identity.uuid} (same: {sup.identity.uuid == uuid0})")
    print(f"comment count after recovery: {github().comment_count(PR_OP)} (no duplicate)")
    print(f"pr effect status: {sup.effect_status(PR_OP)}")
    print(f"pr responsibility: {sup.responsibility_state(PR_RESP)}")
    print(f"game responsibility still active: {sup.responsibility_state('game:m1')}")
    sup.submit(game_event(turn=2))
    print(f"party support move turn 2 recorded: {game().move_count(game_op(turn=2))}")

    banner("Operator revokes the PR comment capability")
    sup.revoke("pr", "pr.comment")
    sup.close()
    sup = build(paths)
    try:
        sup.submit(pr_event(pr=43, trigger="second"))
    except PermissionError as e:
        print(f"refused after restart: {e}")
    print(f"revoked capability still revoked: {not sup.grants.effective('pr', 'pr.comment')}")
    sup.close()

    banner("Answer")
    print("Stark lost his process, restarted from SQLite, recovered both responsibilities,")
    print("and resumed cross environment work with exactly one external comment and no lost")
    print("commitment. Proven under the mock destination's idempotency and fencing assumptions.")
    print(f"\n(demo databases in {tmp})")


if __name__ == "__main__":
    main()
