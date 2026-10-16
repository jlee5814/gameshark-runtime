"""One socket and token path rule shared with StarkKit's ClientConfig.fromEnvironment:
socket from --socket, else STARK_SOCKET, else gateway/stark.sock beside the database;
token from --token, else STARK_TOKEN, else gateway.token beside the socket."""

from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import stark

DB = Path("/data/shared/runtime.db")


@pytest.mark.parametrize("socket, token, env, want_sock, want_token", [
    (None, None, {}, "/data/shared/gateway/stark.sock", "/data/shared/gateway/gateway.token"),
    ("/s/custom.sock", None, {}, "/s/custom.sock", "/s/gateway.token"),
    (None, None, {"STARK_SOCKET": "/e/stark.sock"}, "/e/stark.sock", "/e/gateway.token"),
    ("/s/custom.sock", None, {"STARK_SOCKET": "/e/stark.sock"},
     "/s/custom.sock", "/s/gateway.token"),
    (None, None, {"STARK_TOKEN": "/t/secret"}, "/data/shared/gateway/stark.sock", "/t/secret"),
    ("/s/custom.sock", "/k/tok", {"STARK_TOKEN": "/t/secret"}, "/s/custom.sock", "/k/tok"),
])
def test_socket_and_token_paths(socket, token, env, want_sock, want_token):
    args = Namespace(db=DB, socket=socket and Path(socket), token=token and Path(token))
    assert stark.socket_path(args, env) == Path(want_sock)
    assert stark.token_path(args, env) == Path(want_token)


def test_run_accepts_a_token_override():
    args = stark.build_parser().parse_args(["run", "--socket", "/s/x.sock", "--token", "/k/t"])
    assert (args.socket, args.token) == (Path("/s/x.sock"), Path("/k/t"))


# ---------------------------------------------------------------------- model choice
def test_run_refuses_to_start_without_a_model(monkeypatch, capsys):
    monkeypatch.delenv("GAMESHARK_INFERENCE_URL", raising=False)
    with pytest.raises(SystemExit) as e:
        stark.main(["--db", "/nonexistent/x.db", "run"])
    assert e.value.code == 2
    assert "--inference-url" in capsys.readouterr().err


def test_run_refuses_a_fake_model_beside_an_endpoint(monkeypatch):
    monkeypatch.delenv("GAMESHARK_INFERENCE_URL", raising=False)
    with pytest.raises(SystemExit):
        stark.main(["--db", "/nonexistent/x.db", "run", "--fake-model",
                    "--inference-url", "http://127.0.0.1:1/v1"])


def test_endpoint_flags_build_one_http_provider_with_its_extra_body(monkeypatch):
    monkeypatch.delenv("GAMESHARK_INFERENCE_URL", raising=False)
    args = stark.build_parser().parse_args([
        "run", "--inference-url", "http://127.0.0.1:30000/v1", "--model", "qwen",
        "--inference-extra", '{"chat_template_kwargs": {"enable_thinking": false}}'])
    cfg = stark.make_provider(args)
    p = cfg.provider
    assert (p.model, p.extra_body) == ("qwen", {"chat_template_kwargs":
                                                {"enable_thinking": False}})
    assert cfg.fingerprint == p.config_fingerprint
    spec = stark.chat_spec(args)
    assert spec.max_tokens == args.max_tokens
    assert spec.history_budget is not None
    assert 0 < spec.history_budget < args.context_window


def test_inference_extra_must_be_a_json_object(monkeypatch):
    monkeypatch.delenv("GAMESHARK_INFERENCE_URL", raising=False)
    with pytest.raises(SystemExit):
        stark.main(["--db", "/nonexistent/x.db", "run", "--inference-url",
                    "http://127.0.0.1:1/v1", "--inference-extra", "[1]"])


def test_the_fake_model_says_it_is_fake():
    from gameshark.inference import Message, ProviderRequest, Role
    from gameshark.stark_chat import CompanionFakeProvider

    req = ProviderRequest("c", 1, (Message(Role.USER, "hello"),), 16, deadline=1e18)
    text = "".join(CompanionFakeProvider(0.0).default_text(req))
    assert text.startswith("[fake model] ") and "hello" in text


def test_the_model_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("GAMESHARK_INFERENCE_URL", "http://127.0.0.1:30000/v1")
    monkeypatch.setenv("GAMESHARK_INFERENCE_MODEL", "qwen")
    monkeypatch.setenv("GAMESHARK_INFERENCE_EXTRA", '{"top_p": 0.9}')
    args = stark.build_parser().parse_args(["run"])
    assert (args.inference_url, args.model, args.inference_extra) == (
        "http://127.0.0.1:30000/v1", "qwen", {"top_p": 0.9})
