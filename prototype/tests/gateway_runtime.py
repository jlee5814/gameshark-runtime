"""In process runtime for gateway tests: the real run_forever on its own coordinator thread.

The coordinator thread builds the supervisor, so it owns the SQLite connection exactly as
the daemon's main thread does. The test thread talks to it only through the socket.
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.gateway import GatewayServer, load_or_create_token
from gameshark.gateway_client import GatewayClient
from gameshark.inference import ProviderConfig, RouterPolicy
from gameshark.stark_chat import CompanionFakeProvider, StarkChatAdapter
from gameshark.supervisor import AgentSupervisor
from gameshark.surface_api import SurfaceAPI
from gameshark.worker import run_forever
from tests.helpers import AGENT, HOST


def short_dir() -> Path:
    """A socket directory short enough for sun_path (104 bytes on darwin)."""
    return Path(tempfile.mkdtemp(prefix="gs", dir="/tmp"))


class Gate:
    """Provider sleep that blocks until opened, so a reply stays in flight on demand."""

    def __init__(self) -> None:
        self.opened = threading.Event()
        self.opened.set()
        self.waiting = threading.Event()

    def close(self) -> None:
        self.opened.clear()

    def sleep(self, _seconds: float, token) -> None:
        self.waiting.set()
        while not self.opened.wait(0.01):
            if token.cancelled:
                return


class Runtime:
    def __init__(self, db: Path, sock_dir: Path, gate: Gate | None = None) -> None:
        self.db = db
        self.sock = sock_dir / "stark.sock"
        self.token_path = sock_dir / "gateway.token"
        self.token = load_or_create_token(self.token_path)
        self.gate = gate or Gate()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.code: int | None = None
        self.error: BaseException | None = None
        self.uuid = ""
        self.chat: StarkChatAdapter | None = None
        self.thread = threading.Thread(target=self._run, name="coordinator", daemon=True)

    def _run(self) -> None:
        try:
            sup = AgentSupervisor(db_path=self.db, agent_id=AGENT, owner_token=HOST,
                                  lease_ttl_s=6.0)
            self.chat = StarkChatAdapter()
            sup.add_adapter(self.chat)
            sup.enable_presence("mac", prepare_timeout_s=2.0, ack_timeout_s=1.0)
            provider = CompanionFakeProvider(delay_s=0.01, sleep=self.gate.sleep)
            sup.enable_inference([ProviderConfig(provider)],
                                 policy=RouterPolicy(agent_in_flight=2,
                                                     conversation_in_flight=1),
                                 retry_s=0.05)
            sup.start()
            self.uuid = sup.identity.uuid
            server = GatewayServer(self.sock, self.token,
                                   {"agent_id": AGENT, "uuid": sup.identity.uuid}, sup.wake)
            server.start()
            api = SurfaceAPI(sup, server, self.chat)
            self.ready.set()
            self.code = run_forever(sup, self.stop, 0.2, log=lambda _m: None,
                                    drain_deadline_s=2.0, gateway=api)
            sup.close()
        except BaseException as e:   # surfaced to the test through .error
            self.error = e
            self.ready.set()

    def start(self) -> Runtime:
        self.thread.start()
        assert self.ready.wait(10), "runtime never started"
        if self.error is not None:
            raise self.error
        return self

    def close(self) -> None:
        self.gate.opened.set()
        self.stop.set()
        self.thread.join(15)
        assert not self.thread.is_alive(), "coordinator did not stop"

    def client(self, surface: str = "mac", **kw) -> GatewayClient:
        return GatewayClient(self.sock, self.token, surface, **kw)

