"""Game simulator and adapter.

Stark holds a recurring party support responsibility in a simulated party. The
environment emits turn events; the adapter submits exactly one support move per turn.
The simulator keeps its own durable store, independent of the agent journal, and
enforces a fence per operation so a stale worker cannot land a move after a takeover.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from gameshark.adapters import AdapterContext
from gameshark.effects import Effect, ProbeResult
from gameshark.errors import StaleFenceRejected

GAME_SCHEMA = """
CREATE TABLE IF NOT EXISTS moves (
    op_key TEXT PRIMARY KEY,     -- one move per op key; remote dedup anchor
    match  TEXT NOT NULL,
    turn   INTEGER NOT NULL,
    data   TEXT NOT NULL,
    fence  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS fences (
    op_key TEXT PRIMARY KEY,
    fence  INTEGER NOT NULL
);
"""


class GameSimulator:
    """External game service with its own durable move log and fence ledger."""

    def __init__(self, db_path: str | Path, visible: bool = True) -> None:
        self.conn = sqlite3.connect(str(db_path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=3000")
        self.conn.executescript(GAME_SCHEMA)
        self.visible = visible

    def _check_fence(self, op_key: str, fence: int) -> None:
        row = self.conn.execute(
            "SELECT fence FROM fences WHERE op_key=?", (op_key,)
        ).fetchone()
        if row is not None and fence < row["fence"]:
            raise StaleFenceRejected(
                f"fence {fence} < accepted {row['fence']} for {op_key}"
            )

    def submit_move(self, op_key: str, match: str, turn: int, data: str, fence: int) -> str:
        self._check_fence(op_key, fence)
        self.conn.execute(
            "INSERT OR IGNORE INTO moves(op_key, match, turn, data, fence) VALUES(?,?,?,?,?)",
            (op_key, match, turn, data, fence),
        )
        self.conn.execute(
            "INSERT INTO fences(op_key, fence) VALUES(?,?) "
            "ON CONFLICT(op_key) DO UPDATE SET fence=MAX(fence, excluded.fence)",
            (op_key, fence),
        )
        return op_key

    def probe(self, op_key: str) -> ProbeResult:
        row = self.conn.execute(
            "SELECT op_key FROM moves WHERE op_key=?", (op_key,)
        ).fetchone()
        if row is not None and self.visible:
            return ProbeResult.found(row["op_key"])
        return ProbeResult.absent()

    def move_count(self, op_key: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM moves WHERE op_key=?", (op_key,)
        ).fetchone()["c"]


class GameAdapter:
    name = "game"
    required_caps = frozenset({"game.observe", "game.move"})

    def __init__(self, sim: GameSimulator) -> None:
        self.sim = sim
        self.ctx: AdapterContext | None = None
        self.connected = False

    def init(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self.connected = True

    def reconnect(self, ctx: AdapterContext) -> None:
        # Reconnect reuses identity and grant; it never resets the agent (I1, I6).
        self.ctx = ctx
        self.connected = True

    def teardown(self) -> None:
        self.connected = False

    def _effect(self, match: str, turn: int, responsibility_id: str) -> Effect:
        op_key = f"game:{match}:{turn}"
        intent = {
            "adapter": self.name,
            "match": match,
            "turn": turn,
            "responsibility_id": responsibility_id,
        }
        return Effect(
            key=op_key,
            intent=intent,
            required_cap="game.move",
            adapter=self.name,
            do=lambda fence: self.sim.submit_move(op_key, match, turn, data="support", fence=fence),
            probe=lambda: self.sim.probe(op_key),
        )

    def build_effect(self, intent: dict) -> Effect:
        return self._effect(intent["match"], int(intent["turn"]), intent["responsibility_id"])

    def on_event(self, event: dict) -> None:
        assert self.ctx is not None
        if "game.move" not in self.ctx.caps:
            raise PermissionError("game adapter lacks game.move")
        match = event["payload"]["match"]
        turn = int(event["payload"]["turn"])
        rid = f"game:{match}"                 # recurring party support responsibility
        self.ctx.responsibilities.upsert(
            rid, kind="party-support", payload={"match": match}, state="active"
        )
        eff = self._effect(match, turn, rid)
        self.ctx.emit_effect(eff)
