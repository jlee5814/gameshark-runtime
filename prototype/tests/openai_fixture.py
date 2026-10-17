"""Local HTTP server that speaks the OpenAI chat completions streaming wire format.

Loopback only; no model and no network. Each reply is scripted down to the byte: status,
headers, body parts, slice size per write, gaps between writes, a stall that holds the
socket open, and close delimited (HTTP/1.0) or chunked (HTTP/1.1) framing. During a stall
the handler watches for the client closing its end and records it in `client_closed`.
"""

from __future__ import annotations

import json
import select
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SSE = {"Content-Type": "text/event-stream"}


@dataclass
class Reply:
    status: int = 200
    headers: dict[str, str] = field(default_factory=lambda: dict(SSE))
    parts: list[bytes] = field(default_factory=list)
    piece: int = 0  # >0: write each part in slices of this many bytes, flushing each
    gap: float = 0.0  # seconds between writes
    stall_after: int | None = None  # hold the socket open after this many parts
    stall_before_headers: bool = False
    chunked: bool = False
    truncate_chunk: bool = False  # chunked: announce a longer chunk than is sent, then close


def chunk(content: str | None = None, finish: str | None = None, *, model: str = "m",
          cid: str = "cmpl-1", usage: object = None, role: str | None = None) -> dict:
    delta: dict = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    obj: dict = {"id": cid, "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage is not None:
        obj["usage"] = usage
    return obj


def usage_chunk(prompt: int, completion: int, *, model: str = "m", cid: str = "cmpl-1") -> dict:
    return {"id": cid, "object": "chat.completion.chunk", "model": model, "choices": [],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                      "total_tokens": prompt + completion}}


def sse(*events: dict | str, done: bool = True, eol: str = "\n") -> bytes:
    """Encode events; a str is written verbatim (comments, raw lines, broken data)."""
    out = []
    for e in events:
        out.append(e if isinstance(e, str) else f"data: {json.dumps(e)}{eol}{eol}")
    if done:
        out.append(f"data: [DONE]{eol}{eol}")
    return "".join(out).encode()


def completion(texts: list[str], *, usage: tuple[int, int] | None = (5, 3),
               finish: str = "stop", model: str = "m", cid: str = "cmpl-1",
               eol: str = "\n", done: bool = True) -> bytes:
    events: list[dict | str] = [chunk(role="assistant", content="", model=model, cid=cid)]
    events += [chunk(t, model=model, cid=cid) for t in texts]
    events.append(chunk(finish=finish, model=model, cid=cid))
    if usage is not None:
        events.append(usage_chunk(*usage, model=model, cid=cid))
    return sse(*events, eol=eol, done=done)


Route = Reply | list[Reply] | Callable[[dict], Reply]


class OpenAIFixture:
    def __init__(self, model: str = "m", port: int = 0) -> None:
        self.routes: dict[str, Route] = {
            "/v1/models": Reply(headers={"Content-Type": "application/json"},
                                parts=[json.dumps({"object": "list",
                                                   "data": [{"id": model}]}).encode()]),
            "/v1/chat/completions": Reply(parts=[completion(["hello", " world"])]),
        }
        self.log: list[tuple[str, str, dict, dict | None]] = []
        self.release = threading.Event()  # ends every stall
        self.client_closed = threading.Event()
        self.stalled = threading.Event()  # a handler reached its stall
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                with server._lock:
                    server.log.append((self.command, self.path, dict(self.headers), body))
                    server.active += 1
                    server.peak = max(server.peak, server.active)
                try:
                    self._reply(server._route(self.path, body))
                except (BrokenPipeError, ConnectionResetError):
                    server.client_closed.set()
                finally:
                    with server._lock:
                        server.active -= 1

            def _reply(self, r: Reply) -> None:
                if r.stall_before_headers:
                    self._hold()
                    return
                if r.chunked:
                    self.protocol_version = "HTTP/1.1"
                self.send_response(r.status)
                for k, v in r.headers.items():
                    self.send_header(k, v)
                if r.chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                for i, part in enumerate(r.parts):
                    if r.stall_after is not None and i == r.stall_after:
                        self._hold()
                        return
                    step = r.piece or len(part) or 1
                    for j in range(0, len(part), step):
                        self._write(part[j:j + step], r.chunked)
                        if r.gap:
                            time.sleep(r.gap)
                if r.stall_after is not None and r.stall_after >= len(r.parts):
                    self._hold()
                    return
                if r.chunked:
                    if r.truncate_chunk:
                        self.wfile.write(b"400\r\npartial")
                    else:
                        self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                self.close_connection = True

            def _write(self, data: bytes, chunked: bool) -> None:
                if chunked:
                    data = f"{len(data):x}\r\n".encode() + data + b"\r\n"
                self.wfile.write(data)
                self.wfile.flush()

            def _hold(self) -> None:
                """Keep the socket open until released or until the client closes it."""
                server.stalled.set()
                sock = self.connection
                while not server.release.is_set():
                    ready, _, _ = select.select([sock], [], [], 0.02)
                    if ready:
                        try:
                            if sock.recv(1, socket.MSG_PEEK) == b"":
                                server.client_closed.set()
                                return
                        except OSError:
                            server.client_closed.set()
                            return
                self.close_connection = True

            do_GET = _serve
            do_POST = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, args=(0.02,), daemon=True).start()

    def _route(self, path: str, body: dict | None) -> Reply:
        with self._lock:
            route = self.routes.get(path.split("?")[0])
            if route is None:
                return Reply(404, {"Content-Type": "application/json"},
                             [b'{"error": "not found"}'])
            if isinstance(route, list):
                route = route.pop(0) if len(route) > 1 else route[0]
        return route(body) if callable(route) else route

    def chats(self) -> list[dict]:
        return [b for (m, p, _, b) in self.log if m == "POST" and p.endswith("/chat/completions")]

    def stop(self) -> None:
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def refused_port() -> int:
    """A loopback port with nothing listening."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
