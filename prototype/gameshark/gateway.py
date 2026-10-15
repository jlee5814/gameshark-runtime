"""Surface gateway transport: a versioned, authenticated Unix domain socket.

This module frames and authenticates; it never touches SQLite. Each connection has its own
reader thread. A connection becomes a session only after three checks pass:

    1. filesystem: the socket sits in a 0700 directory and is itself 0600
    2. kernel: the peer's uid equals ours (LOCAL_PEERCRED on darwin, SO_PEERCRED on Linux)
    3. token: the client answers a fresh nonce with HMAC_SHA256(token, transcript), where
       transcript binds the nonce, the surface name, and the protocol version

The session's surface is fixed at hello; no later frame can name another. Authenticated
requests go onto a queue, and the shared wake event tells the coordinator, which answers
them (surface_api.SurfaceAPI). Frames are a 4 byte big endian length and UTF8 JSON, at most
MAX_FRAME bytes; anything oversized or malformed ends the session.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import os
import queue
import re
import secrets
import socket
import stat
import struct
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

PROTO_VERSIONS = (1,)
MAX_FRAME = 64 * 1024
MAX_SESSIONS = 8
IO_TIMEOUT_S = 1.0          # reader poll slice; also bounds a blocked send
HELLO_TIMEOUT_S = 5.0
SURFACE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
TRANSCRIPT_TAG = b"stark-gateway"


class FrameError(Exception):
    """A frame violated the wire format; the session ends."""


# ---------------------------------------------------------------------- framing
def encode(msg: dict) -> bytes:
    body = json.dumps(msg, separators=(",", ":")).encode()
    if len(body) > MAX_FRAME:
        raise FrameError(f"frame of {len(body)} bytes exceeds {MAX_FRAME}")
    return struct.pack(">I", len(body)) + body


def _recv_exact(sock: socket.socket, n: int, alive: threading.Event) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except TimeoutError:
            if not alive.is_set():
                raise ConnectionError("gateway closing") from None
            continue
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return bytes(buf)


def read_frame(sock: socket.socket, alive: threading.Event) -> dict:
    (n,) = struct.unpack(">I", _recv_exact(sock, 4, alive))
    if n == 0 or n > MAX_FRAME:
        raise FrameError(f"frame length {n} outside 1..{MAX_FRAME}")
    try:
        msg = json.loads(_recv_exact(sock, n, alive))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FrameError(f"malformed frame: {e}") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise FrameError("a frame is a JSON object with a string type")
    return msg


# ---------------------------------------------------------------------- auth
def transcript(nonce: bytes, surface: str, proto: int) -> bytes:
    return b"\0".join((TRANSCRIPT_TAG, nonce, surface.encode(), str(proto).encode()))


def sign(token: bytes, nonce: bytes, surface: str, proto: int) -> str:
    return hmac.new(token, transcript(nonce, surface, proto), hashlib.sha256).hexdigest()


def load_or_create_token(path: Path) -> bytes:
    """The shared secret, created once with mode 0600. Refuse a file others can read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    st = path.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise PermissionError(f"{path} must be owned by this user with mode 0600")
    return bytes.fromhex(path.read_text().strip())


def peer_uid(sock: socket.socket) -> int | None:
    """The connecting process's uid from the kernel, or None when unsupported."""
    if sys.platform == "darwin":
        sol_local, local_peercred = 0, 0x001
        raw = sock.getsockopt(sol_local, local_peercred, struct.calcsize("IIh16I") + 2)
        return struct.unpack_from("I", raw, 4)[0]       # struct xucred: version, uid, ...
    if hasattr(socket, "SO_PEERCRED"):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", raw)[1]               # struct ucred: pid, uid, gid
    return None


def _prepare_dir(path: Path) -> None:
    d = path.parent
    d.mkdir(parents=True, exist_ok=True)
    st = d.stat()
    if st.st_uid != os.getuid():
        raise PermissionError(f"{d} is not owned by this user")
    if st.st_mode & 0o077:
        os.chmod(d, 0o700)
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise FileExistsError(f"{path} exists and is not a socket")
        path.unlink()       # the caller holds the instance lock, so it is a dead socket


# ---------------------------------------------------------------------- sessions
@dataclass(eq=False)
class Session:
    id: str
    sock: socket.socket
    surface: str = ""
    proto: int = 0
    authed: bool = False
    closed: bool = False
    synced_seq: int | None = None   # coordinator only: highest seq this session was sent
    lock: threading.Lock = field(default_factory=threading.Lock)

    def send(self, msg: dict) -> bool:
        """Send one frame; a failure or a stalled peer ends the session."""
        if self.closed:
            return False
        data = encode(msg)
        with self.lock:
            try:
                self.sock.sendall(data)
                return True
            except OSError:
                self.close()
                return False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


# Queue markers. Compared by identity, so no decoded frame can ever equal one.
HELLO: dict = {"type": "hello"}     # queued once a session authenticates
LOST: dict = {"type": "lost"}       # queued when an authenticated session ends


class GatewayServer:
    """Accepts surfaces on a Unix socket and queues their authenticated requests."""

    def __init__(self, path: Path, token: bytes, identity: dict, wake: threading.Event,
                 check_peer: bool = True) -> None:
        self.path = Path(path)
        self.token = token
        self.identity = identity
        self.wake = wake
        self.check_peer = check_peer
        self.requests: queue.SimpleQueue[tuple[Session, dict]] = queue.SimpleQueue()
        self.sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._alive = threading.Event()
        self._ids = itertools.count(1)
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        _prepare_dir(self.path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            s.bind(str(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        s.listen(MAX_SESSIONS)
        s.settimeout(IO_TIMEOUT_S)
        self._listener = s
        self._alive.set()
        self._accept_thread = threading.Thread(target=self._accept, name="gateway-accept",
                                               daemon=True)
        self._accept_thread.start()

    def close(self) -> None:
        self._alive.clear()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        with self._lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for sess in sessions:
            sess.send({"type": "bye", "reason": "runtime stopping"})
            sess.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2 * IO_TIMEOUT_S)
        try:
            if stat.S_ISSOCK(self.path.lstat().st_mode):
                self.path.unlink()
        except FileNotFoundError:
            pass

    def live_sessions(self) -> list[Session]:
        with self._lock:
            return [s for s in self.sessions.values() if s.authed and not s.closed]

    def take(self) -> list[tuple[Session, dict]]:
        out = []
        while True:
            try:
                out.append(self.requests.get_nowait())
            except queue.Empty:
                return out

    # ------------------------------------------------------------------ threads
    def _accept(self) -> None:
        while self._alive.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.settimeout(IO_TIMEOUT_S)
            sess = Session(f"s{next(self._ids)}", conn)
            with self._lock:
                full = len(self.sessions) >= MAX_SESSIONS
                if not full:
                    self.sessions[sess.id] = sess
            if full:
                sess.send({"type": "error", "code": "busy", "message": "too many sessions"})
                sess.close()
                continue
            threading.Thread(target=self._serve, args=(sess,), name=f"gateway-{sess.id}",
                             daemon=True).start()

    def _serve(self, sess: Session) -> None:
        try:
            if self._handshake(sess):
                self.requests.put((sess, HELLO))
                self.wake.set()
                while self._alive.is_set() and not sess.closed:
                    msg = read_frame(sess.sock, self._alive)
                    self.requests.put((sess, msg))
                    self.wake.set()
        except FrameError as e:
            sess.send({"type": "error", "code": "bad_frame", "message": str(e)})
        except (ConnectionError, OSError):
            pass
        finally:
            sess.close()
            with self._lock:
                self.sessions.pop(sess.id, None)
            if sess.authed:
                self.requests.put((sess, LOST))
                self.wake.set()

    def _reject(self, sess: Session, code: str, message: str) -> bool:
        sess.send({"type": "error", "code": code, "message": message})
        return False

    def _handshake(self, sess: Session) -> bool:
        if self.check_peer:
            uid = peer_uid(sess.sock)
            if uid is not None and uid != os.getuid():
                return self._reject(sess, "auth_failed", "peer belongs to another user")
        nonce = secrets.token_bytes(32)
        sess.send({"type": "challenge", "proto": list(PROTO_VERSIONS), "nonce": nonce.hex()})
        sess.sock.settimeout(HELLO_TIMEOUT_S)
        try:
            hello = read_frame(sess.sock, self._alive)
        except TimeoutError:
            return self._reject(sess, "auth_failed", "no hello")
        finally:
            sess.sock.settimeout(IO_TIMEOUT_S)
        if hello.get("type") != "hello":
            return self._reject(sess, "auth_failed", "expected hello")
        proto, surface, mac = hello.get("proto"), hello.get("surface"), hello.get("mac")
        if not isinstance(proto, int) or proto not in PROTO_VERSIONS:
            return self._reject(sess, "unsupported_version",
                                f"server speaks {list(PROTO_VERSIONS)}")
        if not isinstance(surface, str) or not SURFACE_RE.match(surface):
            return self._reject(sess, "invalid", "surface must match [a-z][a-z0-9-]{0,31}")
        expected = sign(self.token, nonce, surface, proto)
        if not isinstance(mac, str) or not hmac.compare_digest(mac, expected):
            return self._reject(sess, "auth_failed", "bad token")
        sess.surface, sess.proto, sess.authed = surface, proto, True
        sess.send({"type": "welcome", "proto": proto, "session": sess.id, "surface": surface,
                   "identity": self.identity})
        return True
