"""Router overhead on FakeProvider: selection plus scheduler, and the full durable path.

Not collected by pytest and not a CI gate; timing depends on the host and its disk.

    uv run python bench/router_overhead.py [calls]

Reports p50 and p99 in milliseconds for:
    select+slot   InferenceRouter.select plus Scheduler.admit_call and acquire/release
    durable       one InferenceRouter.complete with a zero delay FakeProvider: two fenced
                  writes (reserve, commit) with synchronous=FULL, plus provider and checks
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.clock import FakeClock
from gameshark.inference import (
    BoundKind,
    CallSpec,
    Capabilities,
    FakeProvider,
    InferenceRouter,
    Locality,
    ProviderConfig,
    Role,
    TokenBound,
)
from gameshark.lifecycle import CancelToken
from gameshark.supervisor import AgentSupervisor


def pct(samples: list[float], q: float) -> float:
    return statistics.quantiles(samples, n=100, method="inclusive")[q - 1] * 1000


def main(n: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sup = AgentSupervisor(Path(tmp) / "runtime.db", "stark", "bench", clock=FakeClock())
        sup.start()
        j = sup.inference
        providers = [
            ProviderConfig(FakeProvider(name, Capabilities(f"{name}-model", Locality.LOCAL,
                                                           8192, max_concurrency=4)),
                           token_bound=TokenBound(BoundKind.TRUSTED_BYTES))
            for name in ("a", "b", "c")
        ]
        router = InferenceRouter(j, providers)
        j.set_budget(3600.0, 10**12, 10**12)
        spec = CallSpec(max_tokens=16)
        for i in range(n):
            j.open_conversation(f"c{i}")
            j.append(f"c{i}", Role.SYSTEM, "you are stark", 0)
            j.append(f"c{i}", Role.USER, f"question {i}", 1)

        select: list[float] = []
        durable: list[float] = []
        for i in range(n):
            t0 = time.perf_counter()
            cands = router.select(f"c{i}", spec)
            with router.scheduler.admit_call("stark", f"c{i}", 4, 1):
                router.scheduler.acquire([c.name for c in cands]).release()
            select.append(time.perf_counter() - t0)

            t0 = time.perf_counter()
            router.complete(f"k{i}", f"c{i}", spec, CancelToken())
            durable.append(time.perf_counter() - t0)
        sup.close()

    for name, s in (("select+slot", select), ("durable", durable)):
        print(f"{name:12s} n={len(s)} p50={pct(s, 50):.3f} ms p99={pct(s, 99):.3f} ms")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1000)
