"""Synchronous gateway client for tests, demo tooling, and operator scripts.

Speaks the same protocol as StarkMac: answer the challenge, then send typed requests and
read frames. Pushed events that arrive while waiting for a result are kept in events.
GatewayClient uses the Unix socket and token; DeviceClient and pair_device speak to the LAN
gateway as a paired phone does (pinned TLS 1.3, ECDSA device key).
"""

from __future__ import annotations

import itertools
import socket
import ssl
import threading
from pathlib import Path
from typing import Self

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from gameshark.devices import device_transcript
from gameshark.gateway import PROTO_VERSIONS, encode, read_frame, sign
from gameshark.net_gateway import public_key_pin


class GatewayError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class _Frames:
    sock: socket.socket

    def _init_frames(self) -> None:
        self._alive = threading.Event()
        self._alive.set()
        self._ids = itertools.count(1)
        self.events: list[dict] = []

    def _expect_welcome(self) -> dict:
        welcome = self.read()
        if welcome.get("type") == "error":
            raise GatewayError(welcome["code"], welcome.get("message", ""))
        return welcome

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


class GatewayClient(_Frames):
    def __init__(self, path: str | Path, token: bytes, surface: str = "mac",
                 proto: int = PROTO_VERSIONS[-1], timeout_s: float = 5.0) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout_s)
        self.sock.connect(str(path))
        self._init_frames()
        challenge = self.read()
        if challenge.get("type") != "challenge":
            raise GatewayError("protocol", f"expected challenge, got {challenge}")
        nonce = bytes.fromhex(challenge["nonce"])
        self.write({"type": "hello", "proto": proto, "surface": surface,
                    "mac": sign(token, nonce, surface, proto), "client": "gateway_client"})
        self.welcome = self._expect_welcome()


# ---------------------------------------------------------------------- devices
def new_device_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def raw_public_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)


def tls_connect(host: str, port: int, pin: bytes, timeout_s: float = 5.0,
                maximum_version: ssl.TLSVersion | None = None) -> ssl.SSLSocket:
    """TLS 1.3 to the LAN gateway, trusting exactly the pinned server key."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE         # no CA trust at all; the pin below decides
    if maximum_version is None:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    else:
        ctx.maximum_version = maximum_version
    raw = socket.create_connection((host, port), timeout=timeout_s)
    s = ctx.wrap_socket(raw)
    der = s.getpeercert(binary_form=True)
    key = x509.load_der_x509_certificate(der).public_key() if der else None
    if not isinstance(key, ec.EllipticCurvePublicKey) or public_key_pin(key) != pin:
        s.close()
        raise GatewayError("pin_mismatch", "server key does not match the pairing pin")
    return s


class DeviceClient(_Frames):
    """A paired phone: pinned TLS, then an ECDSA proof over the session nonce."""

    def __init__(self, host: str, port: int, pin: bytes, device_id: str,
                 key: ec.EllipticCurvePrivateKey, proto: int = PROTO_VERSIONS[-1],
                 timeout_s: float = 5.0, extra: dict | None = None) -> None:
        self.sock = tls_connect(host, port, pin, timeout_s)
        self._init_frames()
        challenge = self.read()
        if challenge.get("type") != "challenge" or challenge.get("server_fp") != pin.hex():
            raise GatewayError("protocol", f"unexpected challenge {challenge}")
        nonce = bytes.fromhex(challenge["nonce"])
        self.nonce = nonce
        sig = key.sign(device_transcript(nonce, device_id, proto, pin),
                       ec.ECDSA(hashes.SHA256()))
        self.hello = {"type": "hello", "proto": proto, "device_id": device_id,
                      "sig": sig.hex(), "client": "gateway_client", **(extra or {})}
        self.write(self.hello)
        self.welcome = self._expect_welcome()


def pair_device(host: str, port: int, pin: bytes, pairing_id: str, secret: str,
                name: str, key: ec.EllipticCurvePrivateKey | None = None,
                timeout_s: float = 5.0) -> tuple[str, str, ec.EllipticCurvePrivateKey]:
    """Run the one time pairing; return (device_id, surface, key)."""
    key = key or new_device_key()
    s = tls_connect(host, port, pin, timeout_s)
    alive = threading.Event()
    alive.set()
    try:
        challenge = read_frame(s, alive)
        if challenge.get("type") != "challenge":
            raise GatewayError("protocol", f"unexpected {challenge}")
        s.sendall(encode({"type": "pair", "proto": PROTO_VERSIONS[-1],
                          "pairing_id": pairing_id, "secret": secret, "name": name,
                          "public_key": raw_public_key(key).hex()}))
        msg = read_frame(s, alive)
    finally:
        s.close()
    if msg.get("type") != "paired":
        raise GatewayError(msg.get("code", "protocol"), msg.get("message", str(msg)))
    return msg["device_id"], msg["surface"], key
