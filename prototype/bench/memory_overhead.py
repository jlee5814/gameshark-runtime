"""Persistent memory overhead: what saved memory adds to a reply and what each write costs.

Not collected by pytest and not a CI gate; timing depends on the host and its disk.

    uv run python bench/memory_overhead.py [calls]

Reports p50 and p99 in milliseconds for:
    select        MemoryStore.block at 0, 16, and 256 active memories, 1024 byte budget
    durable       one InferenceRouter.complete with a zero delay FakeProvider, without and
                  with 256 memories (adds selection twice, the request JSON ids, and the
                  commit fence that rereads each carried memory)
    remember      one fenced remember (synchronous=FULL)
    correct       one fenced correct, including the WAL TRUNCATE checkpoint
    forget        one fenced forget, including the WAL TRUNCATE checkpoint
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.authz import GrantStore
from gameshark.clock import Clock
from gameshark.inference import (
    CallSpec,
    FakeProvider,
    InferenceJournal,
    Locality,
    ProviderConfig,
    Role,
)
from gameshark.inference.router import InferenceRouter
from gameshark.lease import LeaseManager
from gameshark.lifecycle import CancelToken
from gameshark.memory import MAX_ACTIVE, MemoryStore
from gameshark.presence import PresenceManager
from gameshark.store import connect

CONV = "stark:main"
BUDGET = 1024


def pct(samples: list[float], q: int) -> float:
    return statistics.quantiles(samples, n=100, method="inclusive")[q - 1] * 1000


def report(name: str, samples: list[float]) -> None:
    print(f"{name:<22} p50 {pct(samples, 50):7.3f} ms   p99 {pct(samples, 99):7.3f} ms")


def env(root: Path):
    conn = connect(root / "runtime.db")
    clock = Clock()
    lease = LeaseManager(conn, "stark", clock)
    gen = lease.acquire("bench", 3600.0)
    pm = PresenceManager(conn, "stark", lease, gen, clock)
    pm.ensure_home("mac")
    mem = MemoryStore(conn, "stark", lease, gen, pm, GrantStore(conn, "stark", clock), clock)
    mem.ensure_grants()
    journal = InferenceJournal(conn, "stark", lease, gen, clock, memory=mem)
    journal.open_conversation(CONV)
    journal.append(CONV, Role.SYSTEM, "You are Stark.", 0)
    return conn, mem, journal


def fill(mem: MemoryStore, n: int) -> None:
    have = mem.count()
    for i in range(have, n):
        mem.remember("mac", 1, f"fill-{i}", f"fact {i}: the user keeps item {i} in drawer {i % 7}")


def timed(fn, calls: int) -> list[float]:
    out = []
    for i in range(calls):
        t = time.perf_counter()
        fn(i)
        out.append(time.perf_counter() - t)
    return out


def main() -> None:
    calls = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    with tempfile.TemporaryDirectory() as d:
        _conn, mem, journal = env(Path(d))
        query = "which drawer holds item 42?"
        for n in (0, 16, MAX_ACTIVE):
            fill(mem, n)
            report(f"select {n:>3}",
                   timed(lambda _i: mem.block(query, BUDGET, Locality.LOCAL), calls))

        p = FakeProvider("local")
        router = InferenceRouter(journal, [ProviderConfig(p)], grants=mem.grants)
        seq = [1]

        def reply(spec: CallSpec):
            def run(i: int) -> None:
                journal.append(CONV, Role.USER, f"{query} ({i})", seq[0])
                router.complete(f"c{spec.memory_budget}-{i}", CONV, spec, CancelToken())
                seq[0] += 2
            return run

        report("durable no memory", timed(reply(CallSpec(max_tokens=16, history_budget=4096)),
                                          calls))
        report("durable 256 memory", timed(reply(CallSpec(max_tokens=16, history_budget=4096,
                                                          memory_budget=BUDGET)), calls))

    with tempfile.TemporaryDirectory() as d:
        _conn, mem, _journal = env(Path(d))
        ids: list[str] = []

        def remember(i: int) -> None:
            ids.append(mem.remember("mac", 1, f"r{i}", f"bench fact {i}").id)

        report("remember", timed(remember, min(calls, MAX_ACTIVE)))
        report("correct", timed(lambda i: mem.correct("mac", 1, f"c{i}", ids[i], 1,
                                                      f"bench fact {i} corrected"), len(ids)))
        report("forget", timed(lambda i: mem.forget("mac", f"f{i}", ids[i]), len(ids)))


if __name__ == "__main__":
    main()
