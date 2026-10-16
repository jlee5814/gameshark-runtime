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

from gameshark.devices import DeviceRegistry
from gameshark.gateway import GatewayServer, load_or_create_token
from gameshark.gateway_client import DeviceClient, GatewayClient
from gameshark.inference import ProviderConfig, RouterPolicy
from gameshark.net_gateway import LanListener, load_or_create_identity
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
    def __init__(self, db: Path, sock_dir: Path, gate: Gate | None = None,
                 github=None, lan: bool = False, allow=None, limiter=None,
                 port: int = 0, tailnet_allow=None) -> None:
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
        self.server: GatewayServer | None = None
        self.github = github                 # a GitHub client: also serve the PR monitor
        self.sup: AgentSupervisor | None = None
        self.identity = load_or_create_identity(sock_dir) if lan else None
        self.lan: LanListener | None = None
        # Set: also serve a tailnet listener on loopback that admits what this allows, as
        # stark.py does on the Tailscale address (the address itself is not testable here).
        self.tailnet_allow = tailnet_allow
        self.tailnet: LanListener | None = None
        self._lan_kw = {k: v for k, v in (("allow", allow), ("limiter", limiter)) if v}
        self._port = port
        self.logs: list[str] = []
        self.thread = threading.Thread(target=self._run, name="coordinator", daemon=True)

    def _run(self) -> None:
        try:
            sup = AgentSupervisor(db_path=self.db, agent_id=AGENT, owner_token=HOST,
                                  lease_ttl_s=6.0)
            self.chat = StarkChatAdapter()
            sup.add_adapter(self.chat)
            if self.github is not None:
                from sims.github_monitor import GitHubMonitorAdapter

                sup.add_adapter(GitHubMonitorAdapter(self.github, poll_interval_s=3600.0))
            self.sup = sup
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
            devices = None
            if self.identity is not None:
                if self.tailnet_allow is None:
                    self.lan = LanListener(self.identity, port=self._port, host="127.0.0.1",
                                           **self._lan_kw)
                    server.attach(self.lan)
                else:
                    from gameshark.net_gateway import network_listeners

                    self.lan, self.tailnet = network_listeners(
                        self.identity, lan_port=self._port, lan_host="127.0.0.1",
                        tailnet=("127.0.0.1", 0), tailnet_allow=self.tailnet_allow,
                        **self._lan_kw)
                    server.attach(self.lan)
                    server.attach(self.tailnet)
                devices = DeviceRegistry(sup.conn, AGENT, sup.lease, sup.generation, sup.clock)
            server.start()
            self.server = server
            api = SurfaceAPI(sup, server, self.chat, log=self.logs.append, devices=devices)
            self.ready.set()
            self.code = run_forever(sup, self.stop, 0.2, log=self.logs.append,
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

    @property
    def port(self) -> int:
        assert self.lan is not None
        return self.lan.port

    def device(self, device_id: str, key, via: str = "lan", **kw) -> DeviceClient:
        assert self.identity is not None
        listener = self.tailnet if via == "tailnet" else self.lan
        assert listener is not None
        return DeviceClient("127.0.0.1", listener.port, self.identity.pin, device_id, key,
                            **kw)

