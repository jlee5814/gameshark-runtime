"""Synchronous gateway client for tests, demo tooling, and operator scripts.

Speaks the same protocol as StarkMac: answer the challenge, then send typed requests and
read frames. Pushed events that arrive while waiting for a result are kept in events.
"""

from __future__ import annotations

import itertools
import socket
import threading
from pathlib import Path
from typing import Self

from gameshark.gateway import PROTO_VERSIONS, encode, read_frame, sign


class GatewayError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class GatewayClient:
    def __init__(self, path: str | Path, token: bytes, surface: str = "mac",
                 proto: int = PROTO_VERSIONS[-1], timeout_s: float = 5.0) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout_s)
        self.sock.connect(str(path))
        self._alive = threading.Event()
        self._alive.set()
        self._ids = itertools.count(1)
        self.events: list[dict] = []
        challenge = self.read()
        if challenge.get("type") != "challenge":
            raise GatewayError("protocol", f"expected challenge, got {challenge}")
        nonce = bytes.fromhex(challenge["nonce"])
        self.write({"type": "hello", "proto": proto, "surface": surface,
                    "mac": sign(token, nonce, surface, proto), "client": "gateway_client"})
        welcome = self.read()
        if welcome.get("type") == "error":
            raise GatewayError(welcome["code"], welcome.get("message", ""))
        self.welcome = welcome

    def read(self) -> dict:
        return read_frame(self.sock, self._alive)

    def write(self, msg: dict) -> None:
        self.sock.sendall(encode(msg))

    def call(self, kind: str, **fields) -> dict:
        rid = next(self._ids)
        self.write({"type": kind, "id": rid, **fields})
        while True:
            msg = self.read()
            if msg.get("type") == "event":
                self.events.append(msg)
                continue
            if msg.get("id") != rid:
                continue
            if msg["type"] == "error":
                raise GatewayError(msg["code"], msg.get("message", ""))
            return msg["data"]

    def wait_event(self, kind: str, pred=lambda _d: True) -> dict:
        """Return the first pushed event of kind whose data satisfies pred."""
        for i, e in enumerate(self.events):
            if e["kind"] == kind and pred(e["data"]):
                return self.events.pop(i)["data"]
        while True:
            msg = self.read()
            if msg.get("type") == "event":
                if msg["kind"] == kind and pred(msg["data"]):
                    return msg["data"]
                self.events.append(msg)

    def close(self) -> None:
        self._alive.clear()
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
