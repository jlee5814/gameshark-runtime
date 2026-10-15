"""The opt in live harness, exercised deterministically: no model, no network.

The harness itself runs only by hand against real servers (see live/smoke.py). These
tests pin what CI can prove about it: targets refuse cleanly, the cloud path needs an
explicit opt in and HTTPS, the observing wrapper records latency, usage, structure, and
cancellation without changing results, a reasoning marked answer is committed but never
posted, policy refusals never dispatch, and an auth failure leaks no key.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from gameshark.inference import OpenAICompatProvider, ProviderRequest
from gameshark.inference.types import Message, Role
from gameshark.lifecycle import CancelToken
from live import smoke
from live.config import (
    EXIT_UNAVAILABLE,
    LiveUnavailable,
    Target,
    cloud_from_env,
    local_from_env,
    probe,
)
from live.observed import ObservedProvider
from tests.openai_fixture import OpenAIFixture, Reply, chunk, completion, refused_port, sse
from tests.test_daemon import ROOT

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

CLOUD = {"GAMESHARK_CLOUD_OPT_IN": "1", "GAMESHARK_CLOUD_URL": "https://api.example.test/v1",
         "GAMESHARK_CLOUD_MODEL": "m", "GAMESHARK_CLOUD_API_KEY": "sk-test-not-real-0001"}
THINK = ["<think>", "\n\n", "</think>", "\n\n", "Red."]


@pytest.fixture
def srv():
    s = OpenAIFixture()
    yield s
    s.stop()


def local_target(srv: OpenAIFixture, key: str | None = None) -> Target:
    return Target("local", f"{srv.base}/v1", "m", 4096, 2, key=key)


# ---------------------------------------------------------------------- targets
def test_local_target_requires_loopback_literal():
    with pytest.raises(LiveUnavailable, match="GAMESHARK_LIVE_LOCAL_URL"):
        local_from_env({})
    for url in ("http://localhost:30000/v1", "https://example.test/v1"):
        with pytest.raises(LiveUnavailable):
            local_from_env({"GAMESHARK_LIVE_LOCAL_URL": url, "GAMESHARK_LIVE_LOCAL_MODEL": "m"})
    t = local_from_env({"GAMESHARK_LIVE_LOCAL_URL": "http://127.0.0.1:30000/v1",
                        "GAMESHARK_LIVE_LOCAL_MODEL": "m"})
    assert (t.kind, t.context, t.slots, t.key) == ("local", 40960, 4, None)


def test_cloud_needs_explicit_opt_in_every_variable_and_https():
    assert "OPT_IN" in cloud_from_env({k: v for k, v in CLOUD.items()
                                       if k != "GAMESHARK_CLOUD_OPT_IN"})
    missing = cloud_from_env({k: v for k, v in CLOUD.items() if k != "GAMESHARK_CLOUD_API_KEY"})
    assert missing == "missing GAMESHARK_CLOUD_API_KEY"
    assert "https" in cloud_from_env({**CLOUD, "GAMESHARK_CLOUD_URL": "http://api.example.test"})
    assert "HTTPS" in cloud_from_env({**CLOUD, "GAMESHARK_CLOUD_URL": "https://127.0.0.1/v1"})
    t = cloud_from_env(CLOUD)
    assert isinstance(t, Target) and t.kind == "cloud" and t.price_in > 0
    key = CLOUD["GAMESHARK_CLOUD_API_KEY"]
    assert key not in repr(t) and key not in json.dumps(t.describe())
    assert t.daemon_env()["LIVE_API_KEY"] == key


def test_probe_requires_the_model_and_refuses_cleanly(srv):
    assert probe(local_target(srv))["model"] == "m"
    with pytest.raises(LiveUnavailable, match="not listed"):
        probe(Target("local", f"{srv.base}/v1", "other", 4096, 1))
    with pytest.raises(LiveUnavailable, match="not answering"):
        probe(Target("local", f"http://127.0.0.1:{refused_port()}/v1", "m", 4096, 1))


def test_smoke_exits_3_and_runs_nothing_when_the_server_is_down(tmp_path):
    out = tmp_path / "out"
    env = {**os.environ, "PYTHONPATH": str(ROOT), "GAMESHARK_LIVE_LOCAL_MODEL": "m",
           "GAMESHARK_LIVE_LOCAL_URL": f"http://127.0.0.1:{refused_port()}/v1"}
    res = subprocess.run([sys.executable, "-m", "live.smoke", "--local", "--out", str(out)],
                         env=env, cwd=ROOT, capture_output=True, text=True, timeout=60,
                         check=False)
    assert res.returncode == EXIT_UNAVAILABLE
    assert "live smoke refused" in res.stderr
    assert not out.exists()


# ---------------------------------------------------------------------- observing wrapper
def request(call_id: str = "c1", max_tokens: int = 16) -> ProviderRequest:
    return ProviderRequest(call_id, 1, (Message(Role.USER, "hi"),), max_tokens,
                           time.monotonic() + 30)


def test_observed_provider_records_latency_usage_and_structure_unchanged(srv):
    srv.routes["/v1/chat/completions"] = Reply(parts=[completion(THINK, usage=(7, 5))])
    records: list[dict] = []
    obs = ObservedProvider(OpenAICompatProvider("local", f"{srv.base}/v1", "m", 4096),
                           records.append, record_text=True)
    call = obs.generate(request(), CancelToken())
    text = "".join(c.text for c in call)
    res = call.result()
    call.close()
    assert res.text == text == "".join(THINK)             # nothing stripped or altered
    kinds = [r["obs"] for r in records]
    assert kinds == ["dispatch", "first_chunk", "end"]
    end = records[-1]
    assert end["usage"] == {"tokens_in": 7, "tokens_out": 5} and end["finish"] == "stop"
    assert end["markers"] == ["<think>", "</think>"] and end["head_chunks"] == THINK[:4]
    assert end["ttft_s"] is not None and end["total_s"] >= end["ttft_s"]
    assert obs.peak == 1 and obs.active == 0 and obs.dispatched == 1

    records.clear()
    quiet = ObservedProvider(OpenAICompatProvider("local", f"{srv.base}/v1", "m", 4096),
                             records.append)
    call = quiet.generate(request("c2"), CancelToken())
    list(call)
    call.result()
    assert "text_prefix" not in records[-1] and "head_chunks" not in records[-1]


def test_observed_provider_times_cancellation_of_a_stalled_stream(srv):
    srv.routes["/v1/chat/completions"] = Reply(
        parts=[sse(chunk(role="assistant", content=""), chunk("1 2"), done=False), b""],
        stall_after=1)
    records: list[dict] = []
    obs = ObservedProvider(OpenAICompatProvider("local", f"{srv.base}/v1", "m", 4096),
                           records.append)
    token = CancelToken()
    call = obs.generate(request(), token)
    threading.Thread(target=lambda: (srv.stalled.wait(10), time.sleep(0.1),
                                     token.cancel("test")), daemon=True).start()
    with pytest.raises(Exception) as exc:
        list(call)
    call.close()
    assert type(exc.value).__name__ == "ProviderCancelled"
    err = next(r for r in records if r["obs"] == "error")
    assert err["error"] == "ProviderCancelled" and 0 <= err["cancel_latency_s"] <= 2.0
    assert [r["obs"] for r in records][:3] == ["dispatch", "first_chunk", "cancel_seen"]
    assert obs.active == 0


# ---------------------------------------------------------------------- daemon scenarios
def test_reasoning_answer_commits_durably_and_never_posts(srv, tmp_path):
    srv.routes["/v1/chat/completions"] = Reply(parts=[completion(THINK, usage=(9, 6))])
    run = smoke.Run(tmp_path / "out", keep_db=False)
    try:
        res = smoke.generate_latency_and_replay_identity(run, local_target(srv))
    finally:
        run.cleanup()
    assert res["passed"], res
    g = res["evidence"]["answers"]["g256"]
    assert g["outcome"] == "UnvalidatedReasoning" and g["posted_moves"] == 0
    assert g["content_prefix"] == "".join(THINK) and g["call"]["finish_reason"] == "stop"
    assert g["responsibility"] == {"state": "blocked", "checkpoint": "UnvalidatedReasoning"}
    assert res["evidence"]["restart"]["leaks_b"]["dispatched"] == 0


def test_policy_suite_refuses_before_dispatch_and_settles_the_soft_path(tmp_path):
    assert smoke.main(["--policy", "--out", str(tmp_path / "out")]) == 0
    results = json.loads((tmp_path / "out" / "results.json").read_text())
    cases = results["policy_fake_cloud"]
    assert {k: v["evidence"]["outcome"] for k, v in cases.items()} == {
        "local_only_never_egresses": "EgressDenied",
        "cloud_ok_without_grant": "EgressDenied",
        "paid_estimate_without_soft_grant": "BudgetUnenforceable",
        "paid_estimate_with_soft_grant": "committed"}
    assert all(v["passed"] for v in cases.values())
    assert (tmp_path / "out" / "summary.md").exists()


def test_auth_failure_is_terminal_and_leaks_no_key(srv, tmp_path):
    key = "sk-live-harness-secret-0123456789"
    srv.routes["/v1/chat/completions"] = Reply(
        401, {"Content-Type": "application/json"}, [b'{"error": "invalid api key"}'])
    run = smoke.Run(tmp_path / "out", keep_db=True)
    run.secrets.append(key)
    try:
        res = smoke.policy_case(run, local_target(srv, key), "auth", "local_only", (), False,
                                "AuthFailed")
        assert res["passed"], res
        assert len(srv.chats()) == 1                      # no retry, no failover
        posts = [h for (m, _, h, _) in srv.log if m == "POST"]
        assert posts[-1]["Authorization"] == f"Bearer {key}"     # the key was used
        assert run.leaked_secrets() == []                 # but is in no log or database
    finally:
        run.keep_db = False
        run.cleanup()
