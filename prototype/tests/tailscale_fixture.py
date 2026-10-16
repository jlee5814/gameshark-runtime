"""File backed Tailscale for daemon tests, loaded through
GAMESHARK_TAILSCALE=tests.tailscale_fixture:make with GAMESHARK_TAILSCALE_FIXTURE naming a
JSON file {"status": ..., "serve": ...}. Every call rereads it, so a test can stop
Tailscale, move its address, or add a serve forward between `run` and `pair`. The tailnet
here is loopback: 127.0.0.0/8 stands in for 100.64.0.0/10, which no test host can bind."""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path

from gameshark.tailscale import Tailscale, TailscaleUnavailable

LOOPBACK = ipaddress.ip_network("127.0.0.0/8")


def status(address: str = "127.0.0.1", state: str = "Running") -> dict:
    return {"BackendState": state,
            "Self": {"DNSName": "mac.tailtest.ts.net.", "TailscaleIPs": [address]}}


def write(path: Path, status_json: dict | None = None, serve: dict | None = None) -> None:
    path.write_text(json.dumps({"status": status_json or status(), "serve": serve or {}}))


def make() -> Tailscale:
    path = Path(os.environ["GAMESHARK_TAILSCALE_FIXTURE"])

    def cli(*args: str):
        data = json.loads(path.read_text())
        key = "status" if args[0] == "status" else "serve"
        if data.get(key) is None:
            raise TailscaleUnavailable(f"tailscale {args[0]} failed")
        return data[key]

    return Tailscale(cli=cli, v4=LOOPBACK, v6=None)
