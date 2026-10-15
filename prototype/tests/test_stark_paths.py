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
