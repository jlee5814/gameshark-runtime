"""Avatar loadout: what Stark wears on every surface (Phase 4.4B).

The loadout is presentation only. It changes the sprite atlas a surface draws and
nothing else: no tool, memory, inference request, speech, presence, or audio routing
reads it. Avatar AirPods are costume, never a claim about the user's Bluetooth route.

Two inputs decide it, in this order:

1. A manual override (gym or default) set by a surface's loadout_set command or the
   operator CLI. It pins the loadout until a surface or the operator clears it (auto).
2. Inferred context: the newest user message that explicitly says the user is at, or
   leaving, the gym. Inference runs inside SurfaceConversation.submit, in the same
   transaction as the row, so a resend (a dedup hit) never infers twice and a rolled
   back send infers nothing. While an override is set, inference still records the
   context underneath, so clearing the override shows where the user really is.

With neither, the loadout is default. One row per identity holds both inputs; the
runtime owns it, so every surface reads the same value from the snapshot and the
loadout push, across handoffs, reconnects, and restarts.

Detection is deliberately narrow: first person, present tense, the gym named. A clause
with a negation, a hypothetical, a future marker, or a question mark never counts, and
quoted text is ignored. Paraphrases miss by design; the override covers them.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from gameshark.clock import Clock
from gameshark.lease import LeaseManager
from gameshark.store import write_txn

DEFAULT, GYM = "default", "gym"
LOADOUTS = (DEFAULT, GYM)

_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})
# Quoted speech: double quotes, backticks, and single quotes that open after a non word
# character and close before one, so the apostrophes in I'm and boys' never delimit.
_QUOTED = re.compile(r"""("[^"]*"|`[^`]*`|(?<!\w)'(?=\S).*?(?<=\S)'(?!\w))""")
# Clause boundaries keep the delimiter so a clause knows it was a question.
_CLAUSE = re.compile(r"[^.!?;,\n]+[.!?;,\n]*")
_BUT = re.compile(r"\b(?:but|though|although)\b")
_I_AM = r"\b(?:i'?m|i\s+am)\s+(?:now\s+|just\s+|finally\s+|currently\s+|here\s+)?"
_ENTER = (
    re.compile(_I_AM + r"(?:at|in)\s+the\s+gym\b"),
    re.compile(r"\bi\s+(?:just\s+|finally\s+)?(?:got|arrived)\s+(?:to|at)\s+the\s+gym\b"),
)
_EXIT = (
    re.compile(_I_AM + r"(?:leaving|out\s+of|done\s+at|finished\s+at|back\s+(?:home\s+)?from|"
               r"heading\s+(?:out\s+of|home\s+from)|walking\s+out\s+of)\s+the\s+gym\b"),
    re.compile(r"\bi\s+(?:just\s+)?(?:left|walked\s+out\s+of)\s+the\s+gym\b"),
    re.compile(r"^\s*(?:just\s+)?(?:left|leaving)\s+the\s+gym\b"),
)
_VETO = re.compile(
    # negation
    r"\b(?:not|never|no\s+longer|nope)\b|n't\b"
    # hypothetical or conditional
    r"|\b(?:if|when|whenever|unless|would|could|should|might|maybe|perhaps|imagine|"
    r"pretend|suppose|supposing|wish|hope|hopefully)\b"
    # future or plan
    r"|\b(?:will|shall|gonna|going\s+to|about\s+to|later|tomorrow|tonight|soon|plan|"
    r"planning|planned|next)\b|'ll\b"
    r"|\bin\s+(?:a|an|a\s+few|\d+|one|two|five|ten|fifteen|twenty|thirty)\s+"
    r"(?:sec|secs|second|seconds|min|mins|minute|minutes|hour|hours)\b"
    # habitual, not now
    r"|\b(?:usually|always|often|sometimes|every)\b"
)


def detect(text: str) -> str | None:
    """GYM, DEFAULT, or None for a user message. The last qualifying clause wins."""
    t = _QUOTED.sub(" ", text.translate(_FOLD).lower())
    found: str | None = None
    for m in _CLAUSE.finditer(t):
        sentence = m.group(0)
        if "?" in sentence:
            continue
        for clause in _BUT.split(sentence):
            if _VETO.search(clause):
                continue
            if any(p.search(clause) for p in _EXIT):
                found = DEFAULT
            elif any(p.search(clause) for p in _ENTER):
                found = GYM
    return found


@dataclass(frozen=True)
class Loadout:
    effective: str
    inferred: str
    override: str | None
    inferred_seq: int | None = None
    replayed: bool = False

    def view(self) -> dict:
        """The wire shape surfaces render."""
        return {"effective": self.effective, "inferred": self.inferred,
                "override": self.override, "inferred_seq": self.inferred_seq}


def _of(row: sqlite3.Row | None, replayed: bool = False) -> Loadout:
    if row is None:
        return Loadout(DEFAULT, DEFAULT, None, None, replayed)
    override = row["override"]
    return Loadout(override or row["inferred"], row["inferred"], override,
                   row["inferred_seq"], replayed)


def current(conn: sqlite3.Connection, agent_id: str) -> Loadout:
    return _of(conn.execute("SELECT * FROM avatar_loadout WHERE agent_id=?",
                            (agent_id,)).fetchone())


def infer_in_txn(conn: sqlite3.Connection, agent_id: str, seq: int, text: str,
                 generation: int, now: float) -> str | None:
    """Record the context a just inserted user row states. Call inside its write txn."""
    found = detect(text)
    if found is None:
        return None
    conn.execute(
        "INSERT INTO avatar_loadout(agent_id, inferred, inferred_seq, override, "
        "override_surface, updated, generation) VALUES(?,?,?,NULL,NULL,?,?) "
        "ON CONFLICT(agent_id) DO UPDATE SET inferred=excluded.inferred, "
        "inferred_seq=excluded.inferred_seq, updated=excluded.updated, "
        "generation=excluded.generation", (agent_id, found, seq, now, generation))
    return found


class LoadoutStore:
    def __init__(self, conn: sqlite3.Connection, agent_id: str, lease: LeaseManager,
                 generation: int, clock: Clock,
                 crash: Callable[[str], None] | None = None) -> None:
        self.conn = conn
        self.agent_id = agent_id
        self.lease = lease
        self.generation = generation
        self.clock = clock
        self._crash = crash or (lambda _label: None)

    def current(self) -> Loadout:
        return current(self.conn, self.agent_id)

    def set_override(self, surface: str, op_id: str, override: str | None) -> Loadout:
        """Pin gym or default, or clear the pin with None. Any authenticated surface may,
        whoever holds presence: the loadout is costume, not a conversation write. A resend
        of op_id returns the current loadout; a reused op_id with another value is refused.
        """
        if override is not None and override not in LOADOUTS:
            raise ValueError(f"override must be one of {list(LOADOUTS)} or null")
        now = self.clock.now()
        with write_txn(self.conn):
            self.lease.guard_in_txn(self.generation)
            seen = self.conn.execute(
                "SELECT override FROM loadout_ops WHERE agent_id=? AND op_id=?",
                (self.agent_id, op_id)).fetchone()
            if seen is not None:
                if seen["override"] != override:
                    raise ValueError(f"op {op_id} was already used for another loadout")
                return _of(self._row(), replayed=True)
            self.conn.execute(
                "INSERT INTO avatar_loadout(agent_id, inferred, inferred_seq, override, "
                "override_surface, updated, generation) VALUES(?,?,NULL,?,?,?,?) "
                "ON CONFLICT(agent_id) DO UPDATE SET override=excluded.override, "
                "override_surface=excluded.override_surface, updated=excluded.updated, "
                "generation=excluded.generation",
                (self.agent_id, DEFAULT, override, surface, now, self.generation))
            self.conn.execute(
                "INSERT INTO loadout_ops(agent_id, op_id, override, surface, created) "
                "VALUES(?,?,?,?,?)", (self.agent_id, op_id, override, surface, now))
            out = _of(self._row())
        self._crash("L1_after_loadout_write")
        return out

    def _row(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM avatar_loadout WHERE agent_id=?",
                                 (self.agent_id,)).fetchone()


__all__ = ["DEFAULT", "GYM", "LOADOUTS", "Loadout", "LoadoutStore", "current", "detect",
           "infer_in_txn"]
