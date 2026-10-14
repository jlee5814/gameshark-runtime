"""Deterministic relevance classification for pull request activity.

Maps one logical event (see github_events) to a stable kind and a relevance decision
with an auditable reason. The same event always yields the same decision, and every
input the rules read travels in the event payload (viewer login, the previous conclusion
of the same check), so a replay after a crash decides exactly as the first run did. A
model assisted classifier can wrap this later, but it must record decisions the same way.

Rules run in order; the first match wins:

1. pr_state, check_run, and system facts are judged on their own terms, never filtered
   by author: a merge by the viewer or a failing bot run still matters.
2. Activity by the viewer is never news to the viewer.
3. Review decisions (approved, changes requested) count from anyone else, bots included.
4. Bot comments are automated noise.
5. Inline comments that belong to a review roll up into that review's notification.
6. Bare acknowledgements (lgtm, +1, thanks) are routine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gameshark.github_events import CANCELLED, FAILED

KINDS = (
    "new_review", "change_request", "review_comment", "comment",
    "ci_failure", "ci_cancelled", "ci_recovered", "ci", "pr_state", "self", "bot", "system",
)

_ACK = re.compile(
    r"^\s*(\+1|lgtm!?|looks good( to me)?!?|thanks!?|thank you!?|ty|:\+1:|👍|🎉|ship it!?)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Decision:
    kind: str
    relevant: bool
    reason: str


def _is_bot(event: dict) -> bool:
    author = event.get("author") or ""
    return event.get("author_type") == "Bot" or author.endswith("[bot]")


def classify(event: dict) -> Decision:
    raw = event.get("raw_kind")
    state = (event.get("state") or "").lower()
    author = event.get("author") or "?"
    body = (event.get("body") or "").strip()

    # 1. facts judged on their own terms
    if raw == "system":
        return Decision("system", True, event.get("reason") or "runtime condition")
    if raw == "pr_state":
        return Decision("pr_state", True, f"pull request {state}")
    if raw == "check_run":
        name = event.get("name") or "check"
        if state in FAILED:
            return Decision("ci_failure", True, f"CI {name} concluded {state}")
        prev = (event.get("prev_conclusion") or "").lower()
        if state == CANCELLED:
            if prev == CANCELLED:
                return Decision("ci", False, f"CI {name} cancelled again")
            return Decision("ci_cancelled", True, f"CI {name} was cancelled before finishing")
        if state == "success" and (prev in FAILED or prev == CANCELLED):
            return Decision("ci_recovered", True, f"CI {name} passed after {prev}")
        return Decision("ci", False, f"CI {name} concluded {state}")

    # 2. the viewer's own activity
    viewer = event.get("viewer") or ""
    if viewer and author.lower() == viewer.lower():
        return Decision("self", False, f"own activity by {author}")

    # 3. review decisions
    if raw == "review":
        if state == "changes_requested":
            return Decision("change_request", True, f"{author} requested changes")
        if state == "approved":
            return Decision("new_review", True, f"{author} approved the pull request")
        if state == "dismissed":
            return Decision("review_comment", False, f"review by {author} dismissed")

    # 4. automated accounts
    if _is_bot(event):
        return Decision("bot", False, f"automated account {author}")

    if raw == "review":
        inline = int(event.get("inline_count") or 0)
        if body:
            return Decision("review_comment", True, f"{author} left review feedback")
        if inline:
            return Decision("review_comment", True, f"{author} left {inline} inline comments")
        return Decision("review_comment", False, "empty review without inline comments")

    # 5. inline comments roll up into their review
    if raw == "review_comment":
        if event.get("review_id"):
            return Decision("review_comment", False,
                            f"inline comment grouped under review {event['review_id']}")
        return Decision("review_comment", True, f"{author} commented on the diff")

    # 6. discussion comments
    if raw in ("issue_comment", "comment"):
        if not body or _ACK.match(body):
            return Decision("comment", False, "acknowledgement without new information")
        return Decision("comment", True, f"{author} commented")

    return Decision("comment", False, f"unclassified raw kind {raw}")
