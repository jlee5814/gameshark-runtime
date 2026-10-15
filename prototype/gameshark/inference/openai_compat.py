"""OpenAI compatible chat completions provider over stdlib http.client (v0.3 step 4).

Reaches externally managed SGLang, mlx_lm.server, the LM Studio and Bionic local server,
and compatible cloud APIs. GameShark spawns nothing and relies on no server side session:
every request carries the full context.

    provider = OpenAICompatProvider("sglang", "http://127.0.0.1:30000/v1", "qwen", 32768)
    call = provider.generate(req, cancel)   # no I/O
    for chunk in call: ...                  # connect, POST, parse SSE incrementally
    res = call.result()

Locality comes from the validated endpoint, never from a label. Only an IP literal that is
loopback counts as local; every other host is cloud, must use HTTPS, and passes the
router's durable egress grant. Redirects are never followed and no proxy variable is read.

Cancellation and deadlines act during network reads. Every blocking socket step waits at
most min(deadline - now, read_idle_s), and cancellation shuts the socket down from the
cancelling thread, which wakes a blocked read at once. One connection per call, closed
when the stream ends, fails, or is abandoned. Partial text never reaches a result.

Usage is never invented. With stream_usage the request asks for a final usage chunk; when
none arrives, or it is malformed, the result carries usage=None and the journal charges
the attempt its full reservation (ledger status 'unmetered').

The API key lives only in this object and in the Authorization header of a live request.
It never enters ProviderRequest, results, errors, health details, or repr.
"""

from __future__ import annotations

import codecs
import email.utils
import hashlib
import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from gameshark.inference.types import (
    AuthFailed,
    Capabilities,
    CapabilityUnsupported,
    Chunk,
    ContextTooLong,
    EgressDenied,
    FinishReason,
    Health,
    HealthStatus,
    InvalidRequest,
    Locality,
    Privacy,
    ProviderCancelled,
    ProviderError,
    ProviderRequest,
    ProviderResult,
    ProviderUnavailable,
    RateLimited,
    Role,
    StreamInterrupted,
    StreamNotFinished,
    Timeout,
    Usage,
    estimate_tokens,
)
from gameshark.lifecycle import CancelToken

MAX_LINE = 1 << 20
MAX_RESPONSE = 8 << 20
READ_SIZE = 65536
DETAIL_CHARS = 200
DEFAULT_RETRY_AFTER = 5.0
MAX_RETRY_AFTER = 300.0
REDACTED = "[redacted]"

_FINISH = {"stop": FinishReason.STOP, "length": FinishReason.LENGTH}
_BEARER = re.compile(r"(?i)bearer\s+\S+")
_EOL = re.compile(r"\r\n|\r|\n")


# ---------------------------------------------------------------------- endpoint
@dataclass(frozen=True)
class Endpoint:
    scheme: str
    host: str  # bare host; IPv6 without brackets
    port: int
    path: str  # API root, for example /v1; no trailing slash
    locality: Locality

    @classmethod
    def parse(cls, base_url: str) -> Endpoint:
        """Validate a base URL and derive its locality.

        Local only for a loopback IP literal. Hostnames, localhost included, are never
        trusted as local: their resolution can change between check and connect.
        """
        u = urllib.parse.urlsplit(base_url)
        if u.scheme not in ("http", "https"):
            raise ValueError(f"endpoint scheme must be http or https, not {u.scheme!r}")
        if u.username is not None or u.password is not None:
            raise ValueError("endpoint must not embed credentials; pass api_key instead")
        if u.query or u.fragment:
            raise ValueError("endpoint must not carry a query or fragment")
        host = u.hostname
        if not host:
            raise ValueError("endpoint has no host")
        try:
            port = u.port or (443 if u.scheme == "https" else 80)
        except ValueError as exc:
            raise ValueError(f"endpoint port is invalid: {exc}") from None
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError("localhost is not verifiable as loopback; use 127.0.0.1 or [::1]")
        local = _is_loopback(host)
        if not local and u.scheme != "https":
            raise ValueError(f"non loopback endpoint {host} requires https")
        return cls(u.scheme, host, port, u.path.rstrip("/"),
                   Locality.LOCAL if local else Locality.CLOUD)

    @property
    def netloc(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.netloc}{self.path}"


def _is_loopback(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


Connect = Callable[[Endpoint, float], http.client.HTTPConnection]


def default_connect(endpoint: Endpoint, timeout: float) -> http.client.HTTPConnection:
    if endpoint.scheme == "https":
        return http.client.HTTPSConnection(endpoint.host, endpoint.port, timeout=timeout,
                                           context=ssl.create_default_context())
    return http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=timeout)


# ---------------------------------------------------------------------- SSE
class SSEDecoder:
    """Incremental server sent events decoder: bytes in, complete event data out.

    Handles reads split anywhere, including inside a UTF 8 sequence or between CR and LF.
    Lines end in CRLF, LF, or CR. Comment lines (keepalives) are skipped; data lines of one
    event join with LF; a blank line dispatches. Fields other than data are ignored. An
    event cut off by end of stream is discarded.
    """

    def __init__(self, max_line: int = MAX_LINE, max_bytes: int = MAX_RESPONSE) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")("strict")
        self._buf = ""
        self._after_cr = False
        self._data: list[str] = []
        self._total = 0
        self._max_line = max_line
        self._max_bytes = max_bytes

    def feed(self, raw: bytes) -> list[str]:
        self._total += len(raw)
        if self._total > self._max_bytes:
            raise StreamInterrupted(f"response exceeded {self._max_bytes} bytes")
        try:
            text = self._utf8.decode(raw)
        except UnicodeDecodeError as exc:
            raise StreamInterrupted(f"stream is not valid UTF 8: {exc.reason}") from None
        if not text:
            return []
        if self._after_cr and text.startswith("\n"):
            text = text[1:]  # second half of a CRLF split across reads
        self._after_cr = False
        text, self._buf = self._buf + text, ""
        events: list[str] = []
        pos = 0
        for m in _EOL.finditer(text):
            self._line(text[pos:m.start()], events)
            pos = m.end()
        self._buf = text[pos:]
        self._after_cr = pos == len(text) and text.endswith("\r")
        if len(self._buf) > self._max_line:
            raise StreamInterrupted(f"stream line exceeded {self._max_line} characters")
        return events

    def finish(self) -> None:
        try:
            self._utf8.decode(b"", final=True)
        except UnicodeDecodeError:
            raise StreamInterrupted("stream ended inside a UTF 8 sequence") from None

    def _line(self, line: str, events: list[str]) -> None:
        if not line:
            if self._data:
                events.append("\n".join(self._data))
                self._data = []
            return
        if line.startswith(":"):
            return
        name, _, value = line.partition(":")
        if name == "data":
            self._data.append(value.removeprefix(" "))


# ---------------------------------------------------------------------- provider
class OpenAICompatProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        context_window: int,
        *,
        max_concurrency: int = 1,
        price_in_micros: int = 0,
        price_out_micros: int = 0,
        api_key: str | None = None,
        stream_usage: bool = True,
        read_idle_s: float = 30.0,
        connect_timeout_s: float = 5.0,
        max_response_bytes: int = MAX_RESPONSE,
        monotonic: Callable[[], float] = time.monotonic,
        _connect: Connect | None = None,
    ) -> None:
        self.name = name
        self.endpoint = Endpoint.parse(base_url)
        # Tools, JSON mode, and vision stay unadvertised until implemented and tested.
        self._caps = Capabilities(model=model, locality=self.endpoint.locality,
                                  context_window=context_window,
                                  max_concurrency=max_concurrency,
                                  price_in_micros=price_in_micros,
                                  price_out_micros=price_out_micros)
        self.model = model
        self._key = api_key or None
        self.stream_usage = stream_usage
        self.read_idle_s = read_idle_s
        self.connect_timeout_s = connect_timeout_s
        self.max_response_bytes = max_response_bytes
        self.now = monotonic
        self._connect = _connect or default_connect

    def __repr__(self) -> str:
        key = REDACTED if self._key else None
        return (f"OpenAICompatProvider(name={self.name!r}, endpoint={self.endpoint.url!r}, "
                f"model={self.model!r}, locality={self.endpoint.locality.value}, api_key={key})")

    @property
    def config_fingerprint(self) -> str:
        """Changes when endpoint, model, or key change; reveals none of them."""
        key_digest = hashlib.sha256((self._key or "").encode()).hexdigest()
        h = hashlib.sha256(f"{self.endpoint.url}\0{self.model}\0{key_digest}".encode())
        return h.hexdigest()[:16]

    def capabilities(self) -> Capabilities:
        return self._caps

    # -------------------------------------------------------------- helpers
    def scrub(self, text: str) -> str:
        if self._key:
            text = text.replace(self._key, REDACTED)
        return _BEARER.sub(f"Bearer {REDACTED}", text)

    def headers(self, accept: str) -> dict[str, str]:
        h = {"Accept": accept, "User-Agent": "gameshark-stark"}
        if self._key:
            h["Authorization"] = f"Bearer {self._key}"
        return h

    def body(self, req: ProviderRequest) -> bytes:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": m.role.value, "content": m.content} for m in req.messages],
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "stream": True,
        }
        if self.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        return json.dumps(payload, ensure_ascii=False).encode()

    # -------------------------------------------------------------- contract
    def health(self, timeout_s: float) -> Health:
        """GET {base}/models. Never raises; details are scrubbed."""
        conn = None
        try:
            conn = self._connect(self.endpoint, timeout_s)
            conn.request("GET", f"{self.endpoint.path}/models",
                         headers=self.headers("application/json"))
            resp = conn.getresponse()
            raw = resp.read(MAX_LINE)
            if resp.status != 200:
                return Health(HealthStatus.DOWN, self.scrub(
                    f"models answered {resp.status}: {raw[:DETAIL_CHARS].decode('utf8', 'replace')}"))
            data = json.loads(raw).get("data")
            if not isinstance(data, list):
                return Health(HealthStatus.DOWN, "models answer has no data list")
            ids = {d.get("id") for d in data if isinstance(d, dict)}
            if self.model not in ids:
                return Health(HealthStatus.DEGRADED, f"model {self.model} not listed")
            return Health(HealthStatus.OK)
        except (OSError, http.client.HTTPException, ValueError, AttributeError) as exc:
            return Health(HealthStatus.DOWN, self.scrub(f"{type(exc).__name__}: {exc}"))
        finally:
            if conn is not None:
                conn.close()

    def generate(self, req: ProviderRequest, cancel: CancelToken) -> OpenAICompatCall:
        """Validate and return a call handle. No network I/O happens here."""
        if not self._caps.supports(req.needs):
            raise CapabilityUnsupported(f"{self.name} lacks {sorted(req.needs)}")
        if any(m.role is Role.TOOL for m in req.messages):
            raise InvalidRequest(f"{self.name} does not implement tool messages")
        if self._caps.locality is Locality.CLOUD and req.privacy is Privacy.LOCAL_ONLY:
            raise EgressDenied(f"{self.name} is cloud; request is local only")
        if estimate_tokens(req) + req.max_tokens > self._caps.context_window:
            raise ContextTooLong(f"{self.name} window is {self._caps.context_window}")
        return OpenAICompatCall(self, req, cancel)


class OpenAICompatCall:
    """One request: its own connection, stream state, result, and terminal error."""

    def __init__(self, provider: OpenAICompatProvider, req: ProviderRequest,
                 cancel: CancelToken) -> None:
        self.request = req
        self._p = provider
        self._cancel = cancel
        self._lock = threading.Lock()
        self._conn: http.client.HTTPConnection | None = None
        # http.client drops conn.sock once a close delimited response begins; the call
        # keeps its own reference so timeouts and shutdown always reach the socket.
        self._sock: socket.socket | None = None
        self._resp: http.client.HTTPResponse | None = None
        self._closed = False
        self._result: ProviderResult | None = None
        self._error: ProviderError | None = None
        self._stream = self._run()

    def __iter__(self) -> Iterator[Chunk]:
        return self._stream

    def result(self) -> ProviderResult:
        if self._error is not None:
            raise self._error
        if self._result is None:
            raise StreamNotFinished(f"call {self.request.call_id} has not finished streaming")
        return self._result

    def close(self) -> None:
        self._stream.close()
        self._shutdown()

    # -------------------------------------------------------------- socket control
    def _abort(self) -> None:
        """Shut the socket down. Safe from any thread; wakes a blocked connect or read.

        The owning thread still closes the connection, so a read in progress never sees
        its connection object torn down under it.
        """
        with self._lock:
            self._closed = True
            sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _shutdown(self) -> None:
        self._abort()
        with self._lock:
            conn, resp, sock = self._conn, self._resp, self._sock
        if resp is not None:
            resp.close()
        if conn is not None:
            conn.close()
        if sock is not None:
            sock.close()

    def _remaining(self) -> float:
        left = self.request.deadline - self._p.now()
        if left <= 0:
            raise Timeout(f"call {self.request.call_id} passed its deadline")
        return left

    def _arm(self) -> None:
        """Bound the next blocking socket step by the deadline and the idle limit."""
        self._checkpoint()
        timeout = min(self._remaining(), self._p.read_idle_s)
        if self._sock is not None:
            self._sock.settimeout(timeout)

    def _checkpoint(self) -> None:
        # Operator intent wins over the deadline: a cancelled call is never retried.
        if self._cancel.cancelled:
            raise ProviderCancelled(self._cancel.reason)
        self._remaining()

    def _io_error(self, exc: BaseException, phase: str) -> ProviderError:
        """Map a transport exception; cancellation and the deadline take precedence."""
        if self._cancel.cancelled:
            return ProviderCancelled(self._cancel.reason)
        if self._p.now() >= self.request.deadline:
            return Timeout(f"call {self.request.call_id} passed its deadline during {phase}")
        detail = self._p.scrub(f"{type(exc).__name__}: {exc}")
        if isinstance(exc, TimeoutError):
            return StreamInterrupted(f"{phase} idle beyond {self._p.read_idle_s}s")
        if phase == "connect" or (phase == "response" and isinstance(exc, ConnectionError)):
            return ProviderUnavailable(f"{self._p.name} {phase} failed: {detail}")
        return StreamInterrupted(f"{self._p.name} {phase} failed: {detail}")

    # -------------------------------------------------------------- stream
    def _run(self) -> Iterator[Chunk]:
        unsubscribe = self._cancel.on_cancel(self._abort)
        try:
            yield from self._stream_chunks()
        except ProviderError as exc:
            self._error = exc
            raise
        finally:
            unsubscribe()
            self._shutdown()

    def _open(self) -> http.client.HTTPResponse:
        p, req = self._p, self.request
        self._checkpoint()
        conn = p._connect(p.endpoint, min(self._remaining(), p.connect_timeout_s))
        with self._lock:
            if self._closed:
                raise ProviderCancelled(self._cancel.reason or "closed before dispatch")
            self._conn = conn
        try:
            conn.connect()
        except (OSError, http.client.HTTPException) as exc:
            raise self._io_error(exc, "connect") from None
        with self._lock:
            self._sock = conn.sock
            closed = self._closed
        if closed:
            self._abort()  # cancelled while connecting
        try:
            self._arm()
            conn.request("POST", f"{p.endpoint.path}/chat/completions", body=p.body(req),
                         headers={**p.headers("text/event-stream"),
                                  "Content-Type": "application/json"})
            self._arm()
            resp = conn.getresponse()
            with self._lock:
                self._resp = resp
            return resp
        except ProviderError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise self._io_error(exc, "response") from None

    def _read(self, resp: http.client.HTTPResponse) -> bytes:
        try:
            self._arm()
            return resp.read1(READ_SIZE)
        except ProviderError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise self._io_error(exc, "stream read") from None

    def _stream_chunks(self) -> Iterator[Chunk]:
        p, req = self._p, self.request
        resp = self._open()
        if resp.status != 200:
            raise self._status_error(resp)
        ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "text/event-stream":
            raise StreamInterrupted(f"{p.name} answered {ctype or 'no content type'}, "
                                    "not an event stream")
        decoder = SSEDecoder(max_bytes=p.max_response_bytes)
        state = _StreamState(p.model)
        index = 0
        while True:
            raw = self._read(resp)
            if not raw:
                self._checkpoint()  # a cancel shutdown reads as EOF
                decoder.finish()
                raise StreamInterrupted(f"{p.name} stream ended before [DONE]")
            for data in decoder.feed(raw):
                if data == "[DONE]":
                    self._result = state.result(p, req)
                    return
                for text in state.event(data, p):
                    yield Chunk(req.call_id, index, text)
                    index += 1

    def _status_error(self, resp: http.client.HTTPResponse) -> ProviderError:
        p, status = self._p, resp.status
        try:
            self._arm()
            raw = resp.read(4096)
        except (ProviderError, OSError, http.client.HTTPException, ValueError):
            raw = b""
        detail = p.scrub(raw[:DETAIL_CHARS].decode("utf8", "replace"))
        msg = f"{p.name} answered {status}: {detail}".strip()
        if status in (401, 403):
            return AuthFailed(msg)
        if status == 429:
            return RateLimited(msg, retry_after=retry_after(resp))
        if status == 408:
            return Timeout(msg)
        if status >= 500:
            return ProviderUnavailable(msg)
        if 300 <= status < 400:
            return ProviderUnavailable(f"{p.name} answered {status}; redirect refused")
        return InvalidRequest(msg)


class _StreamState:
    """Accumulates completion metadata across events. Text lives only in yielded chunks."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.completion_id: str | None = None
        self.finish: FinishReason | None = None
        self.usage: Usage | None = None
        self.usage_bad = False
        self.parts: list[str] = []

    def event(self, data: str, p: OpenAICompatProvider) -> list[str]:
        try:
            obj = json.loads(data)
        except ValueError:
            raise StreamInterrupted(f"{p.name} sent an undecodable event") from None
        if not isinstance(obj, dict):
            raise StreamInterrupted(f"{p.name} sent a non object event")
        if obj.get("error") is not None:
            raise ProviderUnavailable(p.scrub(
                f"{p.name} stream error: {str(obj['error'])[:DETAIL_CHARS]}"))
        if isinstance(obj.get("model"), str) and obj["model"]:
            self.model = obj["model"]
        if isinstance(obj.get("id"), str) and obj["id"]:
            self.completion_id = obj["id"]
        if obj.get("usage") is not None:
            self._usage(obj["usage"])
        choices = obj.get("choices")
        if choices is None and "usage" in obj:
            choices = []
        if not isinstance(choices, list):
            raise StreamInterrupted(f"{p.name} event has no choices list")
        out: list[str] = []
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise StreamInterrupted(f"{p.name} sent an unexpected choice")
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                raise StreamInterrupted(f"{p.name} sent a malformed delta")
            if delta.get("tool_calls") or delta.get("function_call"):
                raise InvalidRequest(f"{p.name} returned tool calls that were not requested")
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                raise StreamInterrupted(f"{p.name} sent non text content")
            if content:
                self.parts.append(content)
                out.append(content)
            reason = choice.get("finish_reason")
            if reason is not None:
                if reason == "tool_calls":
                    raise InvalidRequest(f"{p.name} finished with tool calls")
                if reason not in _FINISH:
                    raise StreamInterrupted(f"{p.name} finished with {reason!r}")
                self.finish = _FINISH[reason]
        return out

    def _usage(self, raw: object) -> None:
        def count(key: str) -> int | None:
            v = raw.get(key) if isinstance(raw, dict) else None
            return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

        tin, tout = count("prompt_tokens"), count("completion_tokens")
        if tin is None or tout is None:
            self.usage_bad = True
            return
        self.usage = Usage(tokens_in=tin, tokens_out=tout)

    def result(self, p: OpenAICompatProvider, req: ProviderRequest) -> ProviderResult:
        if self.finish is None:
            raise StreamInterrupted(f"{p.name} sent [DONE] without a finish reason")
        return ProviderResult(
            call_id=req.call_id, attempt=req.attempt, text="".join(self.parts),
            finish_reason=self.finish,
            usage=None if self.usage_bad else self.usage,
            model=self.model, instance_ref=f"{p.name}@{p.endpoint.netloc}",
            completion_id=self.completion_id,
        )


def retry_after(resp: http.client.HTTPResponse, now: Callable[[], float] = time.time) -> float:
    """Seconds to wait from Retry-After (seconds or HTTP date) or retry-after-ms."""
    value = (resp.getheader("Retry-After") or "").strip()
    seconds: float | None = None
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = email.utils.parsedate_to_datetime(value).timestamp() - now()
            except (TypeError, ValueError, IndexError):
                seconds = None
    if seconds is None:
        ms = (resp.getheader("retry-after-ms") or "").strip()
        try:
            seconds = float(ms) / 1000.0 if ms else None
        except ValueError:
            seconds = None
    if seconds is None or math.isnan(seconds):
        seconds = DEFAULT_RETRY_AFTER
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)
