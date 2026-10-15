"""Paired devices: pairing, device authentication, and revocation.

A phone joins in two steps. The operator runs `stark.py pair`, which writes a pending
pairing holding only the SHA256 of a fresh 128 bit secret; the secret travels to the phone
in the pairing link, out of band. Over the pinned TLS gateway the phone then presents the
secret with a P256 public key it generated (in the Secure Enclave on a real iPhone). The
coordinator consumes the pairing and creates the device in one fenced transaction, so a
secret works once and only before it expires.

Every later session proves the device key: the phone signs

    "stark-device" 0 nonce 0 device_id 0 proto 0 server_key_sha256

with ECDSA P256 SHA256. The nonce is fresh per session, so a captured hello is worthless,
and the server key hash binds the signature to the pinned runtime. The surface comes from
the device row, never from the frame (G1).

Revocation is an operator write: it takes effect at the coordinator's next pump, which
closes the device's sessions and evicts it from presence before answering anything else.

This module runs on the coordinator thread (or in the operator CLI). It never touches a
socket and never runs a process.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from gameshark.clock import Clock
from gameshark.gateway import DEVICE_PREFIX
from gameshark.lease import LeaseManager
from gameshark.store import write_txn

DEVICE_TAG = b"stark-device"
PAIRING_TTL_S = 300.0
MAX_NAME = 64
NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")
PENDING, USED = "pending", "used"


class AuthRejected(Exception):
    """A pairing or device proof failed. The wire answer stays generic."""


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    surface: str
    public_key: bytes
    created: float
    revoked_at: float | None

    @classmethod
    def of(cls, row: sqlite3.Row) -> Device:
        return cls(row["id"], row["name"], row["surface"], bytes(row["public_key"]),
                   row["created"], row["revoked_at"])


def device_transcript(nonce: bytes, device_id: str, proto: int, server_fp: bytes) -> bytes:
    return b"\0".join((DEVICE_TAG, nonce, device_id.encode(), str(proto).encode(), server_fp))


def load_public_key(raw: bytes) -> ec.EllipticCurvePublicKey:
    """Parse an uncompressed P256 point; raise AuthRejected unless it is on the curve."""
    if len(raw) != 65 or raw[0] != 4:
        raise AuthRejected("public key must be an uncompressed P256 point")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError:
        raise AuthRejected("public key is not on P256") from None


def verify(public_key: bytes, signature: bytes, message: bytes) -> bool:
    try:
        load_public_key(public_key).verify(signature, message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, AuthRejected, ValueError):
        return False
    return True


def clean_name(name: object) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name.strip() or "\0"):
        raise AuthRejected(f"device name must be 1 to {MAX_NAME} printable characters")
    return name.strip()


# ---------------------------------------------------------------------- operator side
def create_pairing(conn: sqlite3.Connection, agent_id: str, name: str, now: float,
                   ttl_s: float = PAIRING_TTL_S) -> tuple[str, str]:
    """Write a pending pairing; return (pairing_id, secret). Only the hash is stored."""
    name = clean_name(name)
    pairing_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(16)                 # 128 bits
    with write_txn(conn):
        conn.execute(
            "INSERT INTO pairings(id, agent_id, secret_sha256, name, expires, state, "
            "device_id, created) VALUES(?,?,?,?,?,?,NULL,?)",
            (pairing_id, agent_id, hashlib.sha256(secret.encode()).hexdigest(), name,
             now + ttl_s, PENDING, now))
    return pairing_id, secret


def revoke(conn: sqlite3.Connection, agent_id: str, device_id: str, now: float) -> bool:
    """Mark a device revoked. Returns False when it is unknown or already revoked."""
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE devices SET revoked_at=? WHERE id=? AND agent_id=? AND revoked_at IS NULL",
            (now, device_id, agent_id))
    return cur.rowcount == 1


def list_devices(conn: sqlite3.Connection, agent_id: str) -> list[Device]:
    rows = conn.execute("SELECT * FROM devices WHERE agent_id=? ORDER BY created",
                        (agent_id,)).fetchall()
    return [Device.of(r) for r in rows]


# ---------------------------------------------------------------------- coordinator side
class DeviceRegistry:
    def __init__(self, conn: sqlite3.Connection, agent_id: str, lease: LeaseManager,
                 generation: int, clock: Clock) -> None:
        self.conn = conn
        self.agent_id = agent_id
        self.lease = lease
        self.generation = generation
        self.clock = clock

    def complete_pairing(self, pairing_id: str, secret: str, public_key: bytes,
                         name: str) -> Device:
        """Consume a pending, unexpired pairing whose secret matches; create its device."""
        load_public_key(public_key)
        name = clean_name(name)
        now = self.clock.now()
        with write_txn(self.conn):
            self.lease.guard_in_txn(self.generation)
            row = self.conn.execute("SELECT * FROM pairings WHERE id=? AND agent_id=?",
                                    (pairing_id, self.agent_id)).fetchone()
            digest = hashlib.sha256(secret.encode()).hexdigest()
            if row is None or not hmac.compare_digest(row["secret_sha256"], digest):
                raise AuthRejected(f"pairing {pairing_id} unknown or secret wrong")
            if row["state"] != PENDING:
                raise AuthRejected(f"pairing {pairing_id} already used")
            if now >= row["expires"]:
                raise AuthRejected(f"pairing {pairing_id} expired")
            while True:
                device_id = secrets.token_hex(16)
                surface = DEVICE_PREFIX + device_id[:6]
                if self.conn.execute("SELECT 1 FROM devices WHERE surface=?",
                                     (surface,)).fetchone() is None:
                    break
            self.conn.execute(
                "INSERT INTO devices(id, agent_id, name, surface, public_key, created, "
                "revoked_at, last_seen) VALUES(?,?,?,?,?,?,NULL,NULL)",
                (device_id, self.agent_id, name, surface, public_key, now))
            self.conn.execute("UPDATE pairings SET state=?, device_id=? WHERE id=?",
                              (USED, device_id, pairing_id))
        return self.device(device_id)

    def authenticate(self, device_id: str, nonce: bytes, proto: int, server_fp: bytes,
                     signature: bytes) -> Device:
        """Check a device hello. Unknown, revoked, and forged all raise AuthRejected."""
        d = self.device(device_id)
        if d is None:
            raise AuthRejected(f"unknown device {device_id}")
        if d.revoked_at is not None:
            raise AuthRejected(f"device {device_id} is revoked")
        if not verify(d.public_key, signature,
                      device_transcript(nonce, device_id, proto, server_fp)):
            raise AuthRejected(f"bad signature from device {device_id}")
        return d

    def device(self, device_id: str) -> Device | None:
        row = self.conn.execute("SELECT * FROM devices WHERE id=? AND agent_id=?",
                                (device_id, self.agent_id)).fetchone()
        return Device.of(row) if row else None

    def revoked_surfaces(self) -> frozenset[str]:
        return frozenset(r["surface"] for r in self.conn.execute(
            "SELECT surface FROM devices WHERE agent_id=? AND revoked_at IS NOT NULL",
            (self.agent_id,)))

    def names(self) -> dict[str, str]:
        """Surface to device name for live devices: display data, never a credential."""
        return {r["surface"]: r["name"] for r in self.conn.execute(
            "SELECT surface, name FROM devices WHERE agent_id=? AND revoked_at IS NULL",
            (self.agent_id,))}
