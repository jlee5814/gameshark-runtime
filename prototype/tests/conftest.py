"""Shared fixtures.

A restart is modeled by discarding a supervisor and building a fresh one on the same
database files. Fresh simulator objects point at the same remote db files, so external
state is durable across the restart. No test relies on an in memory database or a
surviving Python object.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.clock import FakeClock
from gameshark.supervisor import AgentSupervisor
from sims.game import GameAdapter, GameSimulator
from sims.github import MockGitHub, PrAdapter
from tests.helpers import AGENT, HOST


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "runtime": tmp_path / "runtime.db",
        "game": tmp_path / "game.db",
        "github": tmp_path / "github.db",
    }


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_supervisor(paths, clock) -> Callable[..., AgentSupervisor]:
    """Factory that builds a started supervisor bound to fresh sims on the same files."""

    def _build(
        owner_token: str = HOST,
        crash_at: set[str] | None = None,
        github_ambiguous: bool = False,
        github_visible: bool = True,
        lease_ttl_s: float = 30.0,
        start: bool = True,
        with_game: bool = True,
        with_pr: bool = True,
    ) -> AgentSupervisor:
        sup = AgentSupervisor(
            db_path=paths["runtime"],
            agent_id=AGENT,
            owner_token=owner_token,
            clock=clock,
            crash_at=crash_at,
            lease_ttl_s=lease_ttl_s,
        )
        if with_game:
            sup.add_adapter(GameAdapter(GameSimulator(paths["game"])))
        if with_pr:
            sup.add_adapter(
                PrAdapter(MockGitHub(paths["github"], ambiguous=github_ambiguous,
                                     visible=github_visible))
            )
        if start:
            sup.start()
        return sup

    return _build
