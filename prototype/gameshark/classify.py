"""Deterministic relevance classification for pull request activity.

Maps a raw GitHub event to a stable kind and a relevance decision with an auditable
reason. Deterministic by design: the same event always yields the same decision, and the
reason string is the evidence. A model assisted classifier can wrap this later, but the
decision and its evidence must stay auditable, so any such layer records its output the
same way.
"""

from __future__ import annotations

from dataclasses import dataclass

KINDS = ("new_review", "change_request", "comment", "ci")


@dataclass(frozen=True)
class Decision:
    kind: str
    relevant: bool
    reason: str


def classify(event: dict) -> Decision:
    raw = event.get("raw_kind")
    state = (event.get("state") or "").lower()

    if raw == "review":
        if state == "changes_requested":
            return Decision("change_request", True, "reviewer requested changes")
        if state == "approved":
            return Decision("new_review", True, "reviewer approved the pull request")
        # A review left as a plain comment is review activity but not a decision.
        return Decision("comment", False, "review comment without a state change")

    if raw == "comment":
        return Decision("comment", False, "ordinary discussion comment")

    if raw == "check_run":
        if state in ("failure", "timed_out", "cancelled"):
            return Decision("ci", True, f"CI concluded {state}")
        return Decision("ci", False, f"CI concluded {state}")

    return Decision("comment", False, f"unclassified raw kind {raw}")
