"""Durable inference journal: model results commit once, fenced, beside their message.

Conversations and messages belong to the agent identity; a provider only ever sees a
request built from journal rows and returns text. Each call has a stable call_id chosen
by its owner (an inbox event or a responsibility step). Two fenced writes per attempt,
the same shape as the effect journal:

    reserve   bump inference_calls.attempts, insert a budget_ledger row 'reserved'
    (dispatch on the caller's thread; collect verifies every chunk and the result)
    commit    insert the assistant message at seq = base_seq, bump next_seq, mark the
              call done, settle the ledger to actual usage, all in one transaction

Crash points, fired through the supervisor's crash hook:

    E1_before_reserve   nothing durable; replay runs attempt 1
    E2_after_reserve    reservation persists; recovery settles it lost; replay runs attempt 2
    E3_after_provider   the answer dies in memory; same as E2, provider ran twice
    E4_after_commit     replay finds the call done and returns it with no provider call

Execution is at least once; commit is at most once. Recovery only settles abandoned
reservations. It never dispatches: the owner replays and reissues the same call_id.
Nothing here claims exactly once provider execution or billing.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from gameshark.clock import Clock
from gameshark.inference.provider import Provider, ProviderCall
from gameshark.inference.types import (
    MAX_ATTEMPTS,
    AttemptsExhausted,
    CallCancelled,
    Capabilities,
    Capability,
    ContextConflict,
    InvalidRequest,
    Message,
    OwnershipMismatch,
    Privacy,
    ProviderError,
    ProviderRequest,
    ProviderResult,
    ResultMismatch,
    Role,
    StaleAttempt,
    estimate_tokens,
)
from gameshark.lease import LeaseManager
from gameshark.lifecycle import CancelToken
from gameshark.store import write_txn

PENDING, DONE, FAILED, CANCELLED = "pending", "done", "failed", "cancelled"
RESERVED, SETTLED, LOST = "reserved", "settled", "lost"
# A completed attempt whose provider reported no usage. Charged its full reservation, like
# a lost attempt: spend() counts every row except settled ones at reserved figures.
UNMETERED = "unmetered"


@dataclass(frozen=True)
class CallSpec:
    max_tokens: int
    temperature: float = 0.0
    needs: frozenset[Capability] = frozenset()
    timeout_s: float = 60.0


@dataclass(frozen=True)
class Reservation:
    call_id: str
    conversation_id: str
    attempt: int
    base_seq: int
    generation: int
    provider: str
    request: ProviderRequest
    reserved_tokens: int
    reserved_micros: int


@dataclass(frozen=True)
class Committed:
    call_id: str
    conversation_id: str
    seq: int
    content: str
    model: str | None
    replayed: bool  # True when this process found the call already done


def _micros(tokens: int, price_per_1k: int) -> int:
    return -(-tokens * price_per_1k // 1000)


def cost_micros(caps: Capabilities, tokens_in: int, tokens_out: int) -> int:
    return _micros(tokens_in, caps.price_in_micros) + _micros(tokens_out, caps.price_out_micros)


class InferenceJournal:
    def __init__(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        lease: LeaseManager,
        generation: int,
        clock: Clock,
        crash: Callable[[str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.conn = conn
        self.agent_id = agent_id
        self.lease = lease
        self.generation = generation
        self.clock = clock
        self._crash = crash or (lambda _label: None)
        self._monotonic = monotonic
        # Reservations this instance made and has not yet settled. Recovery skips them, so
        # settle_abandoned never charges a call that is still in flight here.
        self._active: dict[tuple[str, int], Capabilities] = {}

    # ------------------------------------------------------------------ fenced writes
    def _fenced(self, fn: Callable[[sqlite3.Connection], object]) -> object:
        with write_txn(self.conn):
            self.lease.guard_in_txn(self.generation)
            return fn(self.conn)

    def _conversation(self, conversation_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"conversation {conversation_id} does not exist")
        if row["agent_id"] != self.agent_id:
            raise OwnershipMismatch(f"conversation {conversation_id} belongs to another agent")
        return row

    def conversation(self, conversation_id: str) -> sqlite3.Row:
        """Read a conversation this agent owns. Raise if it is missing or another agent's."""
        return self._conversation(conversation_id)

    def call_row(self, call_id: str) -> sqlite3.Row | None:
        return self._call(call_id)

    def _call(self, call_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM inference_calls WHERE call_id=?", (call_id,)
        ).fetchone()

    def _committed(self, call_id: str) -> Committed:
        row = self.conn.execute(
            "SELECT m.conversation_id, m.seq, m.content, c.model FROM messages m "
            "JOIN inference_calls c ON c.call_id = m.call_id WHERE m.call_id=?",
            (call_id,),
        ).fetchone()
        return Committed(call_id, row["conversation_id"], row["seq"], row["content"],
                         row["model"], replayed=True)

    # ------------------------------------------------------------------ conversations
    def open_conversation(self, conversation_id: str, privacy: Privacy = Privacy.LOCAL_ONLY,
                          responsibility_id: str | None = None) -> None:
        """Create the conversation once. Reopening keeps its rows and its owner."""
        now = self.clock.now()

        def body(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT agent_id FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is not None:
                if row["agent_id"] != self.agent_id:
                    raise OwnershipMismatch(
                        f"conversation {conversation_id} belongs to another agent")
                return
            conn.execute(
                "INSERT INTO conversations(id, agent_id, responsibility_id, privacy, next_seq, "
                "created, updated) VALUES(?,?,?,?,0,?,?)",
                (conversation_id, self.agent_id, responsibility_id, privacy.value, now, now),
            )

        self._fenced(body)

    def append(self, conversation_id: str, role: Role, content: str, at_seq: int) -> int:
        """Append a non assistant message at at_seq. Idempotent for the same row on replay.

        Assistant rows enter only through commit, so model output always carries its call.
        """
        if role is Role.ASSISTANT:
            raise InvalidRequest("assistant messages commit through the journal, not append")
        now = self.clock.now()

        def body(conn: sqlite3.Connection) -> int:
            conv = self._conversation(conversation_id)
            row = conn.execute(
                "SELECT role, content FROM messages WHERE conversation_id=? AND seq=?",
                (conversation_id, at_seq),
            ).fetchone()
            if row is not None:
                if row["role"] == role.value and row["content"] == content:
                    return at_seq
                raise ContextConflict(f"seq {at_seq} of {conversation_id} holds another message")
            if conv["next_seq"] != at_seq:
                raise ContextConflict(
                    f"append at {at_seq} but {conversation_id} is at {conv['next_seq']}")
            conn.execute(
                "INSERT INTO messages(conversation_id, seq, role, content, call_id, generation, "
                "created) VALUES(?,?,?,?,NULL,?,?)",
                (conversation_id, at_seq, role.value, content, self.generation, now),
            )
            conn.execute("UPDATE conversations SET next_seq=?, updated=? WHERE id=?",
                         (at_seq + 1, now, conversation_id))
            return at_seq

        return self._fenced(body)

    def context(self, conversation_id: str, before_seq: int) -> tuple[Message, ...]:
        rows = self.conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id=? AND seq<? ORDER BY seq",
            (conversation_id, before_seq),
        ).fetchall()
        return tuple(Message(Role(r["role"]), r["content"]) for r in rows)

    # ------------------------------------------------------------------ calls
    def lookup(self, call_id: str) -> Committed | None:
        """Return the committed message for a done call, or None while it is pending.

        Raise the call's terminal error if it failed or was cancelled.
        """
        row = self._call(call_id)
        if row is None or row["status"] == PENDING:
            return None
        if row["status"] == DONE:
            return self._committed(call_id)
        if row["status"] == CANCELLED:
            raise CallCancelled(f"call {call_id} was cancelled")
        raise AttemptsExhausted(f"call {call_id} failed: {row['error_kind']}")

    def is_done(self, call_id: str) -> bool:
        row = self._call(call_id)
        return row is not None and row["status"] == DONE

    def reserve(self, call_id: str, conversation_id: str, provider: str, caps: Capabilities,
                spec: CallSpec,
                prompt_tokens: Callable[[tuple[Message, ...]], int] | None = None,
                admit: Callable[[sqlite3.Connection, int, int, float], None] | None = None,
                ) -> Reservation | Committed:
        """Durably reserve the next attempt before anything leaves the process (R6).

        Return the committed message instead when the call is already done (replay short
        circuit). The request is built from journal rows only, never from the caller (R1).

        prompt_tokens sizes the prompt for the reservation (default: estimate_tokens).
        admit(conn, reserved_tokens, reserved_micros, now) runs inside the same write
        transaction before any row is written; a refusal it raises rolls everything back, so
        a refused admission consumes no attempt and no budget.
        """
        self._crash("E1_before_reserve")
        now = self.clock.now()
        exhausted = False

        def body(conn: sqlite3.Connection) -> Reservation | Committed | None:
            nonlocal exhausted
            conv = self._conversation(conversation_id)
            row = self._call(call_id)
            if row is not None:
                if row["agent_id"] != self.agent_id or row["conversation_id"] != conversation_id:
                    raise OwnershipMismatch(
                        f"call {call_id} belongs to conversation {row['conversation_id']}")
                if row["status"] == DONE:
                    return self._committed(call_id)
                if row["status"] == CANCELLED:
                    raise CallCancelled(f"call {call_id} was cancelled")
                if row["status"] == FAILED:
                    raise AttemptsExhausted(f"call {call_id} failed: {row['error_kind']}")
                if row["attempts"] >= MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE inference_calls SET status=?, error_kind=?, generation=?, "
                        "updated=? WHERE call_id=?",
                        (FAILED, AttemptsExhausted.__name__, self.generation, now, call_id),
                    )
                    exhausted = True
                    return None
            base_seq = conv["next_seq"]
            attempt = (row["attempts"] if row is not None else 0) + 1
            messages = self.context(conversation_id, base_seq)
            if not messages:
                raise InvalidRequest(f"conversation {conversation_id} has no context")
            req = ProviderRequest(
                call_id=call_id, attempt=attempt, messages=messages,
                max_tokens=spec.max_tokens, deadline=self._monotonic() + spec.timeout_s,
                privacy=Privacy(conv["privacy"]), temperature=spec.temperature,
                needs=spec.needs,
            )
            prompt = prompt_tokens(messages) if prompt_tokens else estimate_tokens(req)
            reserved_tokens = prompt + spec.max_tokens
            reserved_micros = cost_micros(caps, prompt, spec.max_tokens)
            if admit is not None:
                admit(conn, reserved_tokens, reserved_micros, now)
            request_json = json.dumps({"max_tokens": spec.max_tokens,
                                       "temperature": spec.temperature,
                                       "needs": sorted(spec.needs)})
            if row is None:
                conn.execute(
                    "INSERT INTO inference_calls(call_id, agent_id, conversation_id, base_seq, "
                    "request, status, attempts, generation, created, updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (call_id, self.agent_id, conversation_id, base_seq, request_json, PENDING,
                     attempt, self.generation, now, now),
                )
            else:
                # Rebase is allowed only while pending: the owner reissues with fresh context.
                conn.execute(
                    "UPDATE inference_calls SET base_seq=?, request=?, attempts=?, generation=?, "
                    "updated=? WHERE call_id=?",
                    (base_seq, request_json, attempt, self.generation, now, call_id),
                )
            conn.execute(
                "INSERT INTO budget_ledger(call_id, attempt, agent_id, provider, window_start, "
                "reserved_tokens, reserved_micros, status) VALUES(?,?,?,?,?,?,?,?)",
                (call_id, attempt, self.agent_id, provider, now, reserved_tokens,
                 reserved_micros, RESERVED),
            )
            return Reservation(call_id, conversation_id, attempt, base_seq, self.generation,
                               provider, req, reserved_tokens, reserved_micros)

        out = self._fenced(body)
        if exhausted:
            raise AttemptsExhausted(f"call {call_id} used all {MAX_ATTEMPTS} attempts")
        if isinstance(out, Reservation):
            self._active[(call_id, out.attempt)] = caps
            self._crash("E2_after_reserve")
        return out

    def unreserve(self, res: Reservation) -> None:
        """Undo a reservation refused before dispatch: no attempt and no budget consumed."""
        def body(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM budget_ledger WHERE call_id=? AND attempt=? AND status=?",
                (res.call_id, res.attempt, RESERVED),
            )
            conn.execute(
                "UPDATE inference_calls SET attempts=attempts-1 WHERE call_id=? AND attempts=? "
                "AND status=?",
                (res.call_id, res.attempt, PENDING),
            )

        self._fenced(body)
        self._active.pop((res.call_id, res.attempt), None)

    def abandon(self, res: Reservation, error_kind: str | None = None) -> None:
        """Settle a dispatched attempt that produced no committable result at full charge.

        The call stays pending: its owner decides whether to retry, and the durable cap
        bounds how often it can. error_kind, when given, records why on the pending call.
        """
        def body(conn: sqlite3.Connection) -> None:
            self._settle_lost(conn, res.call_id, res.attempt)
            if error_kind is not None:
                conn.execute(
                    "UPDATE inference_calls SET error_kind=?, updated=? WHERE call_id=? "
                    "AND status=?",
                    (error_kind, self.clock.now(), res.call_id, PENDING),
                )

        self._fenced(body)
        self._active.pop((res.call_id, res.attempt), None)

    @staticmethod
    def _settle_lost(conn: sqlite3.Connection, call_id: str, attempt: int) -> None:
        conn.execute(
            "UPDATE budget_ledger SET status=?, cost_micros=reserved_micros "
            "WHERE call_id=? AND attempt=? AND status=?",
            (LOST, call_id, attempt, RESERVED),
        )

    @staticmethod
    def _settle_actual(conn: sqlite3.Connection, res: Reservation, result: ProviderResult,
                       caps: Capabilities | None) -> None:
        used = result.usage
        if used is None:
            conn.execute(
                "UPDATE budget_ledger SET status=?, cost_micros=reserved_micros "
                "WHERE call_id=? AND attempt=? AND status=?",
                (UNMETERED, res.call_id, res.attempt, RESERVED),
            )
            return
        cost = cost_micros(caps, used.tokens_in, used.tokens_out) if caps else None
        conn.execute(
            "UPDATE budget_ledger SET status=?, tokens_in=?, tokens_out=?, "
            "cost_micros=COALESCE(?, reserved_micros) WHERE call_id=? AND attempt=? AND status=?",
            (SETTLED, used.tokens_in, used.tokens_out, cost, res.call_id, res.attempt, RESERVED),
        )

    @staticmethod
    def _check_identity(res: Reservation, call_id: str, attempt: int, what: str) -> None:
        if call_id != res.call_id or attempt != res.attempt:
            raise ResultMismatch(
                f"{what} for {call_id} attempt {attempt} offered to {res.call_id} attempt "
                f"{res.attempt}")

    @classmethod
    def collect(cls, res: Reservation, call: ProviderCall) -> ProviderResult:
        """Drain a call and verify every chunk and the result belong to this attempt.

        Pure: no SQLite, so an executor thread may run it. Partial text never escapes: a
        fault mid stream raises and the joined chunks are dropped.
        """
        cls._check_identity(res, call.request.call_id, call.request.attempt, "call")
        parts: list[str] = []
        for i, chunk in enumerate(call):
            if chunk.call_id != res.call_id or chunk.index != i:
                raise ResultMismatch(
                    f"chunk {chunk.call_id}#{chunk.index} in stream of {res.call_id}#{i}")
            parts.append(chunk.text)
        result = call.result()
        cls._check_identity(res, result.call_id, result.attempt, "result")
        if result.text != "".join(parts):
            raise ResultMismatch(f"result text of {res.call_id} disagrees with its chunks")
        return result

    def commit(self, res: Reservation, result: ProviderResult,
               degraded: bool = False) -> Committed:
        """Commit one verified result. At most one assistant message per call_id (R2).

        Fails closed on a stale, released, or expired lease (FencedError), a superseded
        attempt (StaleAttempt), a cancelled call (CallCancelled), or a moved conversation
        (ContextConflict). A call already done returns its committed message unchanged.
        degraded records an answer from a fallback rather than the first chosen provider.
        """
        self._check_identity(res, result.call_id, result.attempt, "result")
        caps = self._active.get((res.call_id, res.attempt))
        now = self.clock.now()
        refusal: ProviderError | None = None

        def body(conn: sqlite3.Connection) -> Committed | None:
            nonlocal refusal
            row = self._call(res.call_id)
            if row is None or row["agent_id"] != self.agent_id or (
                row["conversation_id"] != res.conversation_id
            ):
                raise OwnershipMismatch(f"call {res.call_id} is not {res.conversation_id}'s")
            ledger = conn.execute(
                "SELECT status FROM budget_ledger WHERE call_id=? AND attempt=?",
                (res.call_id, res.attempt),
            ).fetchone()
            # Whatever happens to the message, a dispatched attempt was spent.
            self._settle_actual(conn, res, result, caps)
            if row["status"] == DONE:
                return self._committed(res.call_id)
            if row["status"] == CANCELLED:
                refusal = CallCancelled(f"call {res.call_id} was cancelled; result discarded")
                return None
            if row["status"] == FAILED:
                refusal = AttemptsExhausted(f"call {res.call_id} already failed")
                return None
            if row["attempts"] != res.attempt or ledger is None or ledger["status"] != RESERVED:
                refusal = StaleAttempt(
                    f"attempt {res.attempt} of {res.call_id} was superseded "
                    f"(current {row['attempts']}, ledger {ledger and ledger['status']})")
                return None
            conv = self._conversation(res.conversation_id)
            if row["base_seq"] != res.base_seq or conv["next_seq"] != res.base_seq:
                refusal = ContextConflict(
                    f"{res.conversation_id} moved to {conv['next_seq']}; call based on "
                    f"{res.base_seq}")
                return None
            conn.execute(
                "INSERT INTO messages(conversation_id, seq, role, content, call_id, generation, "
                "created) VALUES(?,?,?,?,?,?,?)",
                (res.conversation_id, res.base_seq, Role.ASSISTANT.value, result.text,
                 res.call_id, self.generation, now),
            )
            conn.execute("UPDATE conversations SET next_seq=?, updated=? WHERE id=?",
                         (res.base_seq + 1, now, res.conversation_id))
            conn.execute(
                "UPDATE inference_calls SET status=?, provider=?, model=?, instance_ref=?, "
                "degraded=?, error_kind=NULL, generation=?, updated=? WHERE call_id=?",
                (DONE, res.provider, result.model, result.instance_ref, int(degraded),
                 self.generation, now, res.call_id),
            )
            return Committed(res.call_id, res.conversation_id, res.base_seq, result.text,
                             result.model, replayed=False)

        out = self._fenced(body)
        self._active.pop((res.call_id, res.attempt), None)
        if refusal is not None:
            raise refusal
        return out

    def complete(self, call_id: str, conversation_id: str, provider: Provider, spec: CallSpec,
                 cancel: CancelToken) -> Committed:
        """Return the committed answer for call_id, running one attempt if none exists.

        Retries belong to the owner and, from increment 3, the router; the attempt cap is
        durable either way.
        """
        res = self.reserve(call_id, conversation_id, provider.name, provider.capabilities(),
                           spec)
        if isinstance(res, Committed):
            return res
        try:
            call = provider.generate(res.request, cancel)
        except ProviderError:
            # generate does no I/O by contract, so nothing was dispatched.
            self.unreserve(res)
            raise
        try:
            result = self.collect(res, call)
        except ProviderError:
            self.abandon(res)
            raise
        finally:
            call.close()
        self._crash("E3_after_provider")
        committed = self.commit(res, result)
        self._crash("E4_after_commit")
        return committed

    def cancel(self, call_id: str) -> bool:
        """Operator cancel: a pending call becomes terminal (R9). A done call stays done."""
        now = self.clock.now()

        def body(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "UPDATE inference_calls SET status=?, error_kind=?, updated=? "
                "WHERE call_id=? AND agent_id=? AND status=?",
                (CANCELLED, CallCancelled.__name__, now, call_id, self.agent_id, PENDING),
            )
            return cur.rowcount == 1

        return self._fenced(body)

    # ------------------------------------------------------------------ recovery
    def settle_abandoned(self) -> list[tuple[str, int]]:
        """Charge every reservation no live attempt holds as lost, at full reservation.

        Runs at start before serving, when nothing is in flight. It never dispatches and
        never moves a call's status: the owning event or responsibility replays the call.
        """
        rows = self.conn.execute(
            "SELECT call_id, attempt FROM budget_ledger WHERE agent_id=? AND status=? "
            "ORDER BY call_id, attempt",
            (self.agent_id, RESERVED),
        ).fetchall()
        abandoned = [(r["call_id"], r["attempt"]) for r in rows
                     if (r["call_id"], r["attempt"]) not in self._active]
        if abandoned:
            def body(conn: sqlite3.Connection) -> None:
                for call_id, attempt in abandoned:
                    self._settle_lost(conn, call_id, attempt)

            self._fenced(body)
        return abandoned

    # ------------------------------------------------------------------ budgets
    def set_budget(self, window_s: float, max_tokens: int, max_cost_micros: int) -> None:
        def body(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO agent_budgets(agent_id, window_s, max_tokens, max_cost_micros) "
                "VALUES(?,?,?,?) ON CONFLICT(agent_id, window_s) DO UPDATE SET "
                "max_tokens=excluded.max_tokens, max_cost_micros=excluded.max_cost_micros",
                (self.agent_id, window_s, max_tokens, max_cost_micros),
            )

        self._fenced(body)

    def spend(self, since: float = 0.0) -> dict[str, int]:
        """Committed spend since a time: settled actuals, lost and open reservations in full."""
        row = self.conn.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status=? THEN COALESCE(tokens_in,0)+COALESCE(tokens_out,0) "
            "ELSE reserved_tokens END), 0) AS tokens, "
            "COALESCE(SUM(CASE WHEN status=? THEN reserved_micros ELSE cost_micros END), 0) "
            "AS micros FROM budget_ledger WHERE agent_id=? AND window_start>=?",
            (SETTLED, RESERVED, self.agent_id, since),
        ).fetchone()
        return {"tokens": row["tokens"], "micros": row["micros"]}

    def overruns(self, since: float = 0.0) -> list[sqlite3.Row]:
        """Settled attempts whose actual usage or cost exceeded their reservation.

        The reservation came from a prompt estimate; actual usage is recorded unmodified,
        so an untrusted estimate shows up here instead of being hidden.
        """
        return self.conn.execute(
            "SELECT call_id, attempt, provider, reserved_tokens, reserved_micros, tokens_in, "
            "tokens_out, cost_micros FROM budget_ledger WHERE agent_id=? AND status=? AND "
            "window_start>=? AND (tokens_in + tokens_out > reserved_tokens OR "
            "cost_micros > reserved_micros) ORDER BY window_start, call_id, attempt",
            (self.agent_id, SETTLED, since),
        ).fetchall()

    def budgets(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT window_s, max_tokens, max_cost_micros FROM agent_budgets WHERE agent_id=? "
            "ORDER BY window_s",
            (self.agent_id,),
        ).fetchall()
