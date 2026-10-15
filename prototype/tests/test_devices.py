"""Device registry: single use pairing, ECDSA device proofs, revocation. Coordinator side,
no sockets; the wire path is in test_net_gateway.py."""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark import devices as dv
from gameshark.errors import FencedError
from gameshark.lease import LeaseManager
from gameshark.store import connect
from tests.helpers import AGENT, HOST

FP = bytes(32)


def keypair() -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    k = ec.generate_private_key(ec.SECP256R1())
    raw = k.public_key().public_bytes(serialization.Encoding.X962,
                                      serialization.PublicFormat.UncompressedPoint)
    return k, raw


def sign(k, nonce: bytes, device_id: str, proto: int = 1, fp: bytes = FP) -> bytes:
    return k.sign(dv.device_transcript(nonce, device_id, proto, fp), ec.ECDSA(hashes.SHA256()))


@pytest.fixture
def reg(paths, clock) -> dv.DeviceRegistry:
    conn = connect(paths["runtime"])
    lease = LeaseManager(conn, AGENT, clock)
    return dv.DeviceRegistry(conn, AGENT, lease, lease.acquire(HOST, 30.0), clock)


def pair(reg, clock, name="Jae's iPhone"):
    pid, secret = dv.create_pairing(reg.conn, AGENT, name, clock.now())
    k, raw = keypair()
    return reg.complete_pairing(pid, secret, raw, name), k


def test_pairing_stores_only_the_secret_hash_and_mints_a_device_surface(reg, clock):
    pid, secret = dv.create_pairing(reg.conn, AGENT, "phone", clock.now())
    stored = reg.conn.execute("SELECT * FROM pairings WHERE id=?", (pid,)).fetchone()
    assert secret not in tuple(str(v) for v in stored)
    _k, raw = keypair()
    d = reg.complete_pairing(pid, secret, raw, "phone")
    assert d.surface.startswith(dv.DEVICE_PREFIX) and d.public_key == raw


def test_a_pairing_works_once(reg, clock):
    pid, secret = dv.create_pairing(reg.conn, AGENT, "phone", clock.now())
    reg.complete_pairing(pid, secret, keypair()[1], "phone")
    with pytest.raises(dv.AuthRejected, match="already used"):
        reg.complete_pairing(pid, secret, keypair()[1], "phone")
    assert len(dv.list_devices(reg.conn, AGENT)) == 1


def test_expired_or_wrong_secret_pairings_are_rejected(reg, clock):
    pid, secret = dv.create_pairing(reg.conn, AGENT, "phone", clock.now(), ttl_s=5)
    with pytest.raises(dv.AuthRejected, match="secret wrong"):
        reg.complete_pairing(pid, secret + "x", keypair()[1], "phone")
    with pytest.raises(dv.AuthRejected, match="unknown"):
        reg.complete_pairing("nope", secret, keypair()[1], "phone")
    clock.advance(5)
    with pytest.raises(dv.AuthRejected, match="expired"):
        reg.complete_pairing(pid, secret, keypair()[1], "phone")
    assert dv.list_devices(reg.conn, AGENT) == []


@pytest.mark.parametrize("raw", [b"", bytes(65), b"\x04" + bytes(64), b"\x02" + bytes(32)])
def test_malformed_public_keys_are_refused(reg, clock, raw):
    pid, secret = dv.create_pairing(reg.conn, AGENT, "phone", clock.now())
    with pytest.raises(dv.AuthRejected):
        reg.complete_pairing(pid, secret, raw, "phone")


@pytest.mark.parametrize("name", ["", " ", "a\nb", "x" * 65, 7])
def test_bad_names_are_refused(reg, clock, name):
    with pytest.raises(dv.AuthRejected):
        dv.create_pairing(reg.conn, AGENT, name, clock.now())


def test_device_proof_binds_nonce_device_proto_and_server(reg, clock):
    d, k = pair(reg, clock)
    nonce = secrets.token_bytes(32)
    assert reg.authenticate(d.id, nonce, 1, FP, sign(k, nonce, d.id)) == d
    other = secrets.token_bytes(32)
    cases = [
        (d.id, other, 1, FP, sign(k, nonce, d.id)),                 # replay on a new nonce
        (d.id, nonce, 2, FP, sign(k, nonce, d.id)),                 # another protocol
        (d.id, nonce, 1, b"\1" * 32, sign(k, nonce, d.id)),         # another server
        (d.id, nonce, 1, FP, sign(keypair()[0], nonce, d.id)),      # forged key
        (d.id, nonce, 1, FP, b"garbage"),
        ("0" * 32, nonce, 1, FP, sign(k, nonce, "0" * 32)),         # unknown device
    ]
    for args in cases:
        with pytest.raises(dv.AuthRejected):
            reg.authenticate(*args)


def test_revoked_device_cannot_authenticate_and_is_reported(reg, clock):
    d, k = pair(reg, clock)
    assert dv.revoke(reg.conn, AGENT, d.id, clock.now()) is True
    assert dv.revoke(reg.conn, AGENT, d.id, clock.now()) is False
    nonce = secrets.token_bytes(32)
    with pytest.raises(dv.AuthRejected, match="revoked"):
        reg.authenticate(d.id, nonce, 1, FP, sign(k, nonce, d.id))
    assert reg.revoked_surfaces() == {d.surface}
    assert reg.names() == {}


def test_stale_owner_cannot_pair(paths, clock, reg):
    pid, secret = dv.create_pairing(reg.conn, AGENT, "phone", clock.now())
    reg.lease.release(HOST, reg.generation)
    with pytest.raises(FencedError):
        reg.complete_pairing(pid, secret, keypair()[1], "phone")


# Shared with StarkKitTests.DeviceAuthTests: the Swift client must build the same bytes and
# accept the same signature encoding.
KAT_NONCE = bytes([0xAA]) * 32
KAT_FP = bytes(range(32))
KAT_DEVICE = "0123456789abcdef0123456789abcdef"
KAT_TRANSCRIPT_SHA256 = "d2528181eaa9e396c8991371ef6acbccc2110fa1b32a72598c2a083590d3c9a4"


def test_device_transcript_known_answer_matches_swift_vector():
    import hashlib

    t = dv.device_transcript(KAT_NONCE, KAT_DEVICE, 1, KAT_FP)
    assert hashlib.sha256(t).hexdigest() == KAT_TRANSCRIPT_SHA256
