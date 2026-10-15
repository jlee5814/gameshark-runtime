"""Budget admission over agent_budgets and budget_ledger, run inside the reserve transaction.

Windows are fixed and aligned: a window of w seconds starts at floor(now / w) * w and
resets at the next boundary. Spend in a window counts settled attempts at actual usage and
lost or still reserved attempts at their full reservation (InferenceJournal.spend). The
check and the reservation insert share one BEGIN IMMEDIATE transaction, so concurrent
admissions on any connection or process serialize on the SQLite write lock and can never
both take the last of a budget.

A hard limit holds only when the reservation bounds actual usage. The reservation is
prompt bound plus max_tokens; see router.TokenBound for which prompt bounds are trusted.
"""

from __future__ import annotations

import math
import sqlite3

from gameshark.inference.journal import InferenceJournal
from gameshark.inference.types import BudgetExceeded


def window_start(now: float, window_s: float) -> float:
    return math.floor(now / window_s) * window_s


def admit_budget(journal: InferenceJournal, conn: sqlite3.Connection, tokens: int,
                 micros: int, now: float) -> None:
    """Raise BudgetExceeded unless this reservation fits every configured window.

    Call inside the journal's write transaction (conn is that transaction's connection).
    reset_at is the latest boundary among violated windows, or inf when the request alone
    exceeds a window's limit and can never fit.
    """
    if conn is not journal.conn or not conn.in_transaction:
        raise RuntimeError("admit_budget must run inside the journal's write transaction")
    resets: list[float] = []
    reasons: list[str] = []
    for b in journal.budgets():
        start = window_start(now, b["window_s"])
        if tokens > b["max_tokens"] or micros > b["max_cost_micros"]:
            resets.append(math.inf)
            reasons.append(f"request {tokens} tokens {micros} micros exceeds the "
                           f"{b['window_s']}s limit outright")
            continue
        spent = journal.spend(start)
        if (spent["tokens"] + tokens > b["max_tokens"]
                or spent["micros"] + micros > b["max_cost_micros"]):
            resets.append(start + b["window_s"])
            reasons.append(f"{b['window_s']}s window spent {spent['tokens']} tokens "
                           f"{spent['micros']} micros of {b['max_tokens']} and "
                           f"{b['max_cost_micros']}")
    if resets:
        raise BudgetExceeded("; ".join(reasons), reset_at=max(resets))
