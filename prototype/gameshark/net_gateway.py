"""LAN gateway for paired devices: TLS 1.3 over TCP, a pinned server key, ECDSA devices.

Off unless `stark.py run --lan`. It feeds the same request queue, session table, and
session cap as the Unix gateway, so SurfaceAPI stays the single coordinator and the closed
command table is unchanged. With `--tailscale` a second listener, bound to this node's
Tailscale address alone, admits tailnet peers through the same gate (network_listeners).
What differs is how a session authenticates:

    1. network: on the LAN listener the peer address must be loopback, RFC 1918, link
       local, or ULA outside the tailnet; on the tailnet listener, a Tailscale node
       (tailscale.py). Repeated failures from one address block it on both for a while
    2. TLS 1.3 only, with a self signed P256 certificate created once beside the socket.
       The phone pins SHA256 of the server's public key (X9.62 uncompressed), learned from
       the pairing link, so no certificate authority and no hostname are trusted
    3. the device proves its key (devices.py) over a fresh nonce; the coordinator checks the
       proof against the database, so revocation is exact

Threads: OpenSSL forbids reading and writing one SSL object from two threads at once, and
the coordinator sends while the connection reads. So each connection has one thread that
owns its raw socket and an SSLObject over memory BIOs; send() only queues bytes and wakes
it. A peer that stops reading fills a bounded outbox and is dropped, as on the Unix socket.

This module frames, encrypts, and filters; it never touches SQLite and never runs a process.
"""

from __future__ import annotations

import collections
import datetime
import errno
import hashlib
import ipaddress
import os
import secrets
import select
import socket
import ssl
import stat
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from gameshark import tailscale
from gameshark.gateway import (
    IO_TIMEOUT_S,
    MAX_FRAME,
    PROTO_VERSIONS,
    FrameError,
    GatewayServer,
    Session,
    _mkdir_private,
    decode,
    encode,
)

DEFAULT_PORT = 47820
AUTH_TIMEOUT_S = 10.0         # TLS handshake, challenge, hello, and the coordinator's verdict
SEND_TIMEOUT_S = 5.0          # a raw write blocked this long ends the session
MAX_OUTBOX = 1 << 20          # queued bytes before a slow reader is dropped
MAX_LAN_SESSIONS = 4          # of the shared cap, so the Unix socket always has room
FAIL_LIMIT = 5                # failed handshakes per address within FAIL_WINDOW_S ...
FAIL_WINDOW_S = 60.0          # ... block that address until the window clears
KEEPALIVE_IDLE_S = 20         # a peer gone with its network is noticed in about a minute
KEEPALIVE_INTERVAL_S = 10
KEEPALIVE_COUNT = 3
ACCEPT_RETRY_S = 1.0          # pause after an accept error other than a reset in the queue
LAN_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fe80::/10", "fc00::/7"))


# ---------------------------------------------------------------------- server identity
@dataclass(frozen=True)
class ServerIdentity:
    cert_path: Path
    key_path: Path
    pin: bytes                # SHA256 of the X9.62 uncompressed public key

    @property
    def pin_hex(self) -> str:
        return self.pin.hex()


def public_key_pin(key: ec.EllipticCurvePublicKey) -> bytes:
    raw = key.public_bytes(serialization.Encoding.X962,
                           serialization.PublicFormat.UncompressedPoint)
    return hashlib.sha256(raw).digest()


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _check_private(path: Path) -> None:
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise PermissionError(f"{path} must be a regular file owned by this user, mode 0600")


def load_or_create_identity(d: Path) -> ServerIdentity:
    """The runtime's TLS key and self signed certificate, created once in the private
    gateway directory and reused, so a pin survives restarts. Refuses files others can read."""
    _mkdir_private(d)
    st = d.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise PermissionError(f"{d} must be a private directory (mode 0700)")
    key_path, cert_path = d / "lan.key", d / "lan.crt"
    if not key_path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        _write_private(key_path, key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
    _check_private(key_path)
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise TypeError(f"{key_path} is not an EC key")
    if not cert_path.exists():
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "GameShark Stark")])
        now = datetime.datetime.now(datetime.UTC)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=3650))
                .sign(key, hashes.SHA256()))
        _write_private(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    _check_private(cert_path)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    pin = public_key_pin(key.public_key())
    cert_key = cert.public_key()
    if not isinstance(cert_key, ec.EllipticCurvePublicKey) or public_key_pin(cert_key) != pin:
        raise ValueError(f"{cert_path} does not match {key_path}")
    return ServerIdentity(cert_path, key_path, pin)


def server_context(identity: ServerIdentity) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(identity.cert_path, identity.key_path)
    return ctx


# ---------------------------------------------------------------------- admission
def lan_peer(host: str) -> bool:
    """Loopback, private, link local, or unique local; anything routable is refused."""
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.version == 6 and ip in tailscale.TAILNET_V6:
        return False                        # inside fc00::/7, but only --tailscale admits it
    return any(ip in net for net in LAN_NETS if net.version == ip.version)


class FailureLimiter:
    def __init__(self, limit: int = FAIL_LIMIT, window_s: float = FAIL_WINDOW_S,
                 now=time.monotonic) -> None:
        self.limit = limit
        self.window_s = window_s
        self.now = now
        self._fails: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, ip: str, t: float) -> collections.deque[float]:
        q = self._fails.setdefault(ip, collections.deque())
        while q and t - q[0] >= self.window_s:
            q.popleft()
        return q

    def failed(self, ip: str) -> None:
        with self._lock:
            t = self.now()
            self._recent(ip, t).append(t)

    def blocked(self, ip: str) -> bool:
        with self._lock:
            q = self._recent(ip, self.now())
            if not q:
                self._fails.pop(ip, None)
            return len(q) >= self.limit


class RejectLog:
    """One line per refused connection: `rejected <path> <peer> <stage>: <reason>`.

    Stages: policy (the path does not admit the address), blocked (FailureLimiter), setup
    (socket options or session start), capacity, tls, auth, dropped (the peer left before
    authenticating), accept, internal. Reasons are errno names, TLS reasons, and this
    code's own messages; never frames, nonces, secrets, signatures, or keys. Repeats of one
    path, peer, and stage within window_s are counted and reported on the next line, so a
    scanner cannot flood the log."""

    def __init__(self, log: Callable[[str], None] = lambda _m: None,
                 window_s: float = FAIL_WINDOW_S, now=time.monotonic) -> None:
        self.log = log
        self.window_s = window_s
        self.now = now
        self._last: dict[tuple[str, str, str], float] = {}
        self._quiet: dict[tuple[str, str, str], int] = {}
        self._lock = threading.Lock()

    def __call__(self, path: str, peer: str, stage: str, reason: str) -> None:
        key = (path, peer, stage)
        with self._lock:
            t = self.now()
            last = self._last.get(key)
            if last is not None and t - last < self.window_s:
                self._quiet[key] = self._quiet.get(key, 0) + 1
                return
            if len(self._last) > 1024:
                for k in [k for k, v in self._last.items() if t - v >= self.window_s]:
                    del self._last[k]
            self._last[key] = t
            quiet = self._quiet.pop(key, 0)
        more = f" ({quiet} suppressed)" if quiet else ""
        self.log(f"rejected {path} {peer} {stage}: {reason}{more}")


def why(e: BaseException) -> str:
    """An exception as an errno name, or its type and our own message."""
    code = getattr(e, "errno", None)
    if isinstance(code, int) and code in errno.errorcode:
        return errno.errorcode[code]
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


class SessionSlots:
    """Network sessions open at once, across every listener of one runtime."""

    def __init__(self, cap: int = MAX_LAN_SESSIONS) -> None:
        self.cap = cap
        self._open: set[TLSSession] = set()
        self._lock = threading.Lock()

    def take(self, sess: TLSSession) -> bool:
        with self._lock:
            if len(self._open) >= self.cap:
                return False
            self._open.add(sess)
            return True

    def give_back(self, sess: TLSSession) -> None:
        with self._lock:
            self._open.discard(sess)


def prepare(conn: socket.socket) -> None:
    """Socket options for an accepted connection. On macOS a peer that reset the
    connection between the TCP handshake and accept() leaves a socket XNU has shut both
    ways, and every setsockopt on it fails with EINVAL (sosetoptlock); Linux accepts the
    options and the first read fails instead. Either way only this connection is lost."""
    conn.settimeout(SEND_TIMEOUT_S)
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    keepalive(conn)


def keepalive(conn: socket.socket) -> None:
    """Probe an idle peer, so a phone that left with its network frees its session."""
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    for opt, value in ((idle, KEEPALIVE_IDLE_S),
                       (getattr(socket, "TCP_KEEPINTVL", None), KEEPALIVE_INTERVAL_S),
                       (getattr(socket, "TCP_KEEPCNT", None), KEEPALIVE_COUNT)):
        if opt is not None:
            try:
                conn.setsockopt(socket.IPPROTO_TCP, opt, value)
            except OSError:
                pass


@dataclass(eq=False)
class AuthRequest:
    """Queued for the coordinator once a device's first frame arrives. Not a dict, so no
    decoded frame can ever be mistaken for one."""
    kind: str                 # hello | pair
    frame: dict
    nonce: bytes
    server_fp: bytes


# ---------------------------------------------------------------------- sessions
class TLSSession(Session):
    def __init__(self, sid: str, raw: socket.socket, peer: str, ctx: ssl.SSLContext,
                 listener: LanListener) -> None:
        super().__init__(sid, raw)
        self.peer = peer
        self.device_id = ""
        self.listener = listener
        self.path = listener.path               # lan | tailnet: which listener accepted it
        self.live = threading.Event()       # set by the coordinator once authenticated
        self._in, self._out = ssl.MemoryBIO(), ssl.MemoryBIO()
        self._ssl = ctx.wrap_bio(self._in, self._out, server_side=True)
        self._outbox: collections.deque[bytes] = collections.deque()
        self._queued = 0
        self._olock = threading.Lock()
        self._buf = bytearray()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._failed = False

    # Called from any thread ---------------------------------------------------------
    def send(self, msg: dict) -> bool:
        if self.closed:
            return False
        data = encode(msg)
        with self._olock:
            if self._queued + len(data) > MAX_OUTBOX:
                overflow = True
            else:
                overflow = False
                self._outbox.append(data)
                self._queued += len(data)
        if overflow:
            self.close()
            return False
        self._poke()
        return True

    def close(self) -> None:
        """Stop after flushing what is queued (an error or bye reaches the peer)."""
        if self.closed:
            return
        self.closed = True
        self._poke()

    def _poke(self) -> None:
        try:
            self._wake_w.send(b"\0")
        except OSError:
            pass                            # full or closed: the thread is awake or gone

    # Coordinator only -----------------------------------------------------------------
    def accept(self, surface: str, device_id: str, proto: int, welcome: dict) -> None:
        self.surface, self.device_id, self.proto, self.authed = surface, device_id, proto, True
        # Live before the welcome is queued: the peer may answer the welcome at once, and
        # its first request must not look like a frame sent before authentication.
        self.live.set()
        self.send(welcome)

    def supersede(self) -> None:
        """The same device proved itself on a newer session (it changed networks)."""
        self.send({"type": "bye", "reason": "superseded"})
        self.close()

    def rejected(self, stage: str, reason: str) -> None:
        """Log why this connection never authenticated (RejectLog)."""
        self.listener.rejects(self.path, self.peer, stage, reason)

    def reject(self, code: str, message: str) -> None:
        self._failed = True
        self.send({"type": "error", "code": code, "message": message})
        self.close()

    # Connection thread ----------------------------------------------------------------
    def run(self) -> None:
        server = self.listener.server
        try:
            self._serve(server)
        except FrameError as e:
            if not self.authed:
                self._failed = True
                self.rejected("auth", f"bad frame: {e}")
            self.send({"type": "error", "code": "bad_frame", "message": str(e)})
            self.closed = True
            self._try_flush()
        except ssl.SSLError as e:
            self._failed = True             # plaintext, TLS 1.2, or a broken handshake
            self.rejected("tls", e.reason or type(e).__name__)
        except TimeoutError as e:
            if not self.authed:
                self.rejected("auth" if self._failed else "dropped", why(e))
        except (ConnectionError, OSError) as e:
            if not self.authed:
                self.rejected("dropped", why(e))
        except Exception as e:
            self.rejected("internal", type(e).__name__)
            raise
        finally:
            self.closed = True
            if self._failed:
                self.listener.limiter.failed(self.peer)
            self._discard()
            self.listener.forget(self)
            server.retire(self)

    def _discard(self) -> None:
        for s in (self.sock, self._wake_r, self._wake_w):
            try:
                s.close()
            except OSError:
                pass

    def _serve(self, server: GatewayServer) -> None:
        deadline = time.monotonic() + AUTH_TIMEOUT_S
        while True:
            try:
                self._ssl.do_handshake()
                break
            except ssl.SSLWantReadError:
                self._flush_tls()
                self._expect(deadline)
                self._read_tls()
        nonce = secrets.token_bytes(32)
        fp = self.listener.identity.pin
        challenge = {"type": "challenge", "proto": list(PROTO_VERSIONS), "nonce": nonce.hex(),
                     "server_fp": fp.hex(), "transport": "tls"}
        if self.listener.instance is not None:
            challenge["instance"] = self.listener.instance
        self.send(challenge)
        first = None
        while first is None:
            self._expect(deadline)
            frames = self._io(0)
            if frames:
                first = frames[0]
                if len(frames) > 1:
                    raise FrameError("wait for the welcome before sending requests")
        if first["type"] not in ("hello", "pair"):
            self._failed = True
            self.rejected("auth", "expected hello or pair")
            self.reject("auth_failed", "expected hello or pair")
            self._try_flush()
            return
        server.submit(self, AuthRequest(first["type"], first, nonce, fp))
        while not self.live.is_set():
            if self.closed:
                self._try_flush()
                return
            self._expect(deadline)
            frames = self._io(0)
            if frames and not self.live.is_set():
                raise FrameError("wait for the welcome before sending requests")
            for frame in frames:            # the welcome went out in this same _io call
                server.submit(self, frame)
        while not self.closed and server.alive.is_set():
            for frame in self._io(IO_TIMEOUT_S):
                server.submit(self, frame)
        self._try_flush()

    def _expect(self, deadline: float) -> None:
        """Wait for input or a wake until the auth deadline; raise once it passes."""
        left = deadline - time.monotonic()
        if left <= 0:
            self._failed = True
            raise TimeoutError("authentication took too long")
        select.select([self.sock, self._wake_r], [], [], min(left, IO_TIMEOUT_S))

    def _io(self, timeout: float) -> list[dict]:
        """Flush queued frames, then read whatever arrived; return complete frames."""
        readable = [self.sock]
        if timeout:
            readable, _, _ = select.select([self.sock, self._wake_r], [], [], timeout)
        try:
            while self._wake_r.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass
        self._flush_outbox()
        if self.sock in readable:
            self.sock.setblocking(False)
            try:
                self._recv_raw()
            except BlockingIOError:
                pass
            finally:
                self.sock.settimeout(SEND_TIMEOUT_S)
        self._drain_tls()       # every record buffered so far, not only this read's
        return self._frames()

    def _read_tls(self) -> None:
        """Handshake step: feed the next bytes from the socket to the TLS engine."""
        self._recv_raw()

    def _recv_raw(self) -> None:
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError("peer closed")
        self._in.write(data)

    def _drain_tls(self) -> None:
        if not self._handshaken():
            return                          # do_handshake consumes it
        while True:
            try:
                chunk = self._ssl.read(65536)
            except ssl.SSLWantReadError:
                break
            except ssl.SSLZeroReturnError:
                raise ConnectionError("peer closed") from None
            if not chunk:
                raise ConnectionError("peer closed")
            self._buf += chunk

    def _handshaken(self) -> bool:
        try:
            return self._ssl.version() is not None
        except ValueError:
            return False

    def _frames_peek(self) -> list[int]:
        if len(self._buf) < 4:
            return []
        (n,) = struct.unpack(">I", self._buf[:4])
        if n == 0 or n > MAX_FRAME:
            raise FrameError(f"frame length {n} outside 1..{MAX_FRAME}")
        return [n] if len(self._buf) >= 4 + n else []

    def _frames(self) -> list[dict]:
        out = []
        while (ready := self._frames_peek()):
            n = ready[0]
            body = bytes(self._buf[4:4 + n])
            del self._buf[:4 + n]
            out.append(decode(body))
        return out

    def _flush_outbox(self) -> None:
        with self._olock:
            pending = list(self._outbox)
            self._outbox.clear()
            self._queued = 0
        for data in pending:
            self._ssl.write(data)
        self._flush_tls()

    def _flush_tls(self) -> None:
        data = self._out.read()
        if data:
            self.sock.settimeout(SEND_TIMEOUT_S)
            self.sock.sendall(data)

    def _try_flush(self) -> None:
        try:
            if self._handshaken():
                self._flush_outbox()
        except (OSError, ssl.SSLError):
            pass


# ---------------------------------------------------------------------- listener
class LanListener:
    """Accepts devices on TCP. Attach to a GatewayServer before it starts."""

    def __init__(self, identity: ServerIdentity, port: int = DEFAULT_PORT, host: str = "",
                 allow=lan_peer, limiter: FailureLimiter | None = None,
                 instance: str | None = None, slots: SessionSlots | None = None,
                 path: str = "lan", log: Callable[[str], None] = lambda _m: None,
                 rejects: RejectLog | None = None) -> None:
        self.identity = identity
        # Names this runtime process in every challenge. Runtimes sharing a gateway
        # directory share its key, so `stark.py pair` checks this against the
        # advertisement to know which live runtime answers.
        self.instance = instance
        self.host = host
        self.requested_port = port
        self.allow = allow
        self.limiter = limiter or FailureLimiter()
        self.slots = slots or SessionSlots()
        self.path = path
        self.rejects = rejects or RejectLog(log)
        self.server: GatewayServer
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._alive = threading.Event()

    @property
    def port(self) -> int:
        assert self._sock is not None
        return self._sock.getsockname()[1]

    def start(self, server: GatewayServer) -> None:
        self.server = server
        self._ctx = server_context(self.identity)
        if self.host == "" and socket.has_dualstack_ipv6():
            s = socket.create_server(("", self.requested_port), family=socket.AF_INET6,
                                     dualstack_ipv6=True)
        else:
            s = socket.create_server((self.host, self.requested_port))
        try:
            s.settimeout(IO_TIMEOUT_S)
            self._sock = s
            self._alive.set()
            self._thread = threading.Thread(target=self._accept,
                                            name=f"{self.path}-accept",
                                            daemon=True)
            self._thread.start()
        except BaseException:
            self._alive.clear()
            self._sock = None
            s.close()
            raise

    def stop(self) -> None:
        self._alive.clear()
        if self._sock is not None:
            self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2 * IO_TIMEOUT_S)

    def _accept(self) -> None:
        while self._alive.is_set():
            try:
                conn, addr = self._sock.accept()      # type: ignore[union-attr]
            except TimeoutError:
                continue
            except OSError as e:
                if not self._alive.is_set():
                    return                          # stop() closed the socket
                # A connection reset while queued costs nothing; anything else (out of
                # descriptors) waits a moment rather than spin.
                self.rejects(self.path, "-", "accept", why(e))
                if e.errno != errno.ECONNABORTED:
                    time.sleep(ACCEPT_RETRY_S)
                continue
            self._admit(conn, addr)

    def _admit(self, conn: socket.socket, addr) -> None:
        """Hand one connection to its own thread, or close it. Nothing one connection does
        ends the accept loop. Setup failures do not count against the address: a reset
        is not an authentication attempt.

        A peer that reset before accept() returned shows up two ways on macOS: an AF_INET6
        socket (the dual stack LAN listener) reports no address at all, and an AF_INET one
        reports the address but refuses every socket option with EINVAL (prepare)."""
        sess: TLSSession | None = None
        ip = addr[0] if isinstance(addr, tuple) and addr else "-"
        stage, reason = "", ""
        try:
            if ip == "-":
                stage, reason = "setup", "no peer address (peer reset)"
            elif not self.allow(ip):
                stage, reason = "policy", f"not a {self.path} peer"
            elif self.limiter.blocked(ip):
                stage, reason = "blocked", "too many recent failures"
            else:
                try:
                    prepare(conn)
                except OSError as e:
                    reset = e.errno in (errno.EINVAL, errno.ECONNRESET)
                    stage, reason = "setup", why(e) + (" (peer reset)" if reset else "")
                else:
                    sess = TLSSession(self.server.next_id(), conn, ip, self._ctx, self)
                    if not self.slots.take(sess):
                        stage, reason = "capacity", f"{self.slots.cap} network sessions open"
                    elif not self.server.admit(sess):
                        self.forget(sess)
                        stage, reason = "capacity", "gateway full"
                    else:
                        threading.Thread(target=sess.run, name=f"{self.path}-{sess.id}",
                                         daemon=True).start()
                        return
        except Exception as e:
            if sess is not None:
                self.forget(sess)
                self.server.retire(sess)
            stage, reason = "setup", why(e)
        if sess is not None:
            sess._discard()
        conn.close()
        self.rejects(self.path, ip, stage, reason)

    def forget(self, sess: TLSSession) -> None:
        self.slots.give_back(sess)


def network_listeners(identity: ServerIdentity, *, lan_port: int = DEFAULT_PORT,
                      lan_host: str = "", tailnet: tuple[str, int] | None = None,
                      tailnet_allow=tailscale.tailnet_peer, instance: str | None = None,
                      allow=lan_peer, limiter: FailureLimiter | None = None,
                      log: Callable[[str], None] = lambda _m: None) -> list[LanListener]:
    """The LAN listener, plus with tailnet=(address, port) one bound to that address alone.
    They share the key, instance id, session cap, failure limiter, and rejection log, so a
    second path adds no second budget and no second identity."""
    limiter = limiter or FailureLimiter()
    slots = SessionSlots()
    rejects = RejectLog(log)
    out = [LanListener(identity, port=lan_port, host=lan_host, allow=allow, limiter=limiter,
                       instance=instance, slots=slots, path="lan", rejects=rejects)]
    if tailnet is not None:
        host, port = tailnet
        out.append(LanListener(identity, port=port, host=host, allow=tailnet_allow,
                               limiter=limiter, instance=instance, slots=slots,
                               path="tailnet", rejects=rejects))
    return out
