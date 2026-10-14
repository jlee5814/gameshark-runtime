"""Shared test helpers: event builders and remote constructors on the same db files."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sims.game import GameSimulator
from sims.github import MockGitHub

AGENT = "stark"
HOST = "stark-host"

PR_OP = "pr:acme/widget:42:needs_review"
PR_RESP = "pr:acme/widget:42"


def pr_event(repo: str = "acme/widget", pr: int = 42, trigger: str = "needs_review") -> dict:
    return {
        "event_id": f"ev-pr-{repo}-{pr}-{trigger}",
        "adapter": "pr",
        "type": "review_requested",
        "payload": {"repo": repo, "pr": pr, "trigger": trigger},
    }


def game_event(match: str = "m1", turn: int = 7) -> dict:
    return {
        "event_id": f"ev-game-{match}-{turn}",
        "adapter": "game",
        "type": "turn",
        "payload": {"match": match, "turn": turn},
    }


def game_op(match: str = "m1", turn: int = 7) -> str:
    return f"game:{match}:{turn}"


def github_remote(paths, ambiguous: bool = False, visible: bool = True) -> MockGitHub:
    return MockGitHub(paths["github"], ambiguous=ambiguous, visible=visible)


def game_remote(paths) -> GameSimulator:
    return GameSimulator(paths["game"])
