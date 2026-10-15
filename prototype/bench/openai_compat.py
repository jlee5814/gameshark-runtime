"""OpenAI compatible provider overhead against the loopback SSE fixture.

Not collected by pytest and not a CI gate; timing depends on the host.

    uv run python bench/openai_compat.py [calls]

Reports:
    sse decode     SSEDecoder throughput on a 256 token stream fed in 64 byte reads
    call           one full call (connect, POST, 256 chunk stream, [DONE]) p50 and p99 ms
    cancel         time from CancelToken.cancel to ProviderCancelled during a stalled read
"""

from __future__ import annotations

import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.inference import (
    Message,
    OpenAICompatProvider,
    ProviderCancelled,
    ProviderRequest,
    Role,
    SSEDecoder,
    complete,
)
from gameshark.lifecycle import CancelToken
from tests.openai_fixture import OpenAIFixture, Reply, chunk, completion, sse

TOKENS = 256


def pct(samples: list[float], q: int) -> float:
    return statistics.quantiles(samples, n=100, method="inclusive")[q - 1] * 1000


def request(timeout_s: float = 30.0) -> ProviderRequest:
    return ProviderRequest("bench", 1, (Message(Role.USER, "go"),), 512,
                           time.monotonic() + timeout_s)


def main(calls: int) -> None:
    body = completion([f" tok{i}" for i in range(TOKENS)])
    reads = [body[i:i + 64] for i in range(0, len(body), 64)]
    rounds = 200
    start = time.perf_counter()
    for _ in range(rounds):
        d = SSEDecoder()
        for r in reads:
            d.feed(r)
    took = time.perf_counter() - start
    print(f"sse decode     {len(body) * rounds / took / 1e6:8.1f} MB/s  "
          f"{TOKENS * rounds / took / 1e3:8.0f} k events/s")

    srv = OpenAIFixture()
    try:
        srv.routes["/v1/chat/completions"] = Reply(parts=[body])
        p = OpenAICompatProvider("bench", f"{srv.base}/v1", "m", 8192)
        samples = []
        for _ in range(calls):
            t0 = time.perf_counter()
            complete(p.generate(request(), CancelToken()))
            samples.append(time.perf_counter() - t0)
        print(f"call           p50 {pct(samples, 50):6.2f} ms  p99 {pct(samples, 99):6.2f} ms  "
              f"({calls} calls, {TOKENS} chunks each)")

        srv.routes["/v1/chat/completions"] = Reply(parts=[sse(chunk("a"), done=False)],
                                                   stall_after=1)
        lat = []
        for _ in range(20):
            srv.stalled.clear()
            token = CancelToken()
            call = p.generate(request(), token)
            at: dict = {}

            def stop(tok: CancelToken = token, mark: dict = at) -> None:
                srv.stalled.wait(5)
                time.sleep(0.02)
                mark["t"] = time.perf_counter()
                tok.cancel("bench")

            t = threading.Thread(target=stop)
            t.start()
            try:
                complete(call)
            except ProviderCancelled:
                lat.append(time.perf_counter() - at["t"])
            t.join()
        print(f"cancel         p50 {pct(lat, 50):6.2f} ms  max {max(lat) * 1000:6.2f} ms")
    finally:
        srv.stop()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 200)
