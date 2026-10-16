"""Tailscale for paired devices: find this Mac's tailnet address, admit only tailnet peers.

Off unless `stark.py run --lan --tailscale`. The runtime then serves one more listener,
bound to this node's Tailscale IPv4 alone (net_gateway.network_listeners), so a phone on
cellular reaches the same pinned TLS gateway through the WireGuard tunnel. Nothing about
trust changes: the pin, the device proof, and the closed command table decide as on the
LAN. This module only reads Tailscale's state; it never runs `up`, `serve`, or `funnel`.

Two checks keep the port private:

    1. the address must be this node's own, from `tailscale status --json` while the
       backend is Running, inside 100.64.0.0/10; binding it fails if no interface holds it
    2. `tailscale serve status --json` must not forward any runtime port (Funnel or not),
       since a forward would hand internet or tailnet traffic to a loopback peer
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
QUAD100 = ipaddress.ip_address("100.100.100.100")     # tailscaled's own service address
APP_CLI = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
CLI_TIMEOUT_S = 3.0

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class TailscaleUnavailable(Exception):
    """Tailscale is absent, stopped, unreadable, or would expose a runtime port."""


class TailscaleExposed(TailscaleUnavailable):
    """`tailscale serve` or Funnel forwards a runtime port: no network listener may run."""


@dataclass(frozen=True)
class TailnetSelf:
    address: str              # this node's Tailscale IPv4
    dns: str                  # its MagicDNS name, without the trailing dot ("" if none)


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def tailnet_peer(host: str, v4: Network | None = TAILNET_V4,
                 v6: Network | None = TAILNET_V6) -> bool:
    """A Tailscale node address; loopback, LAN, and the internet are refused."""
    ip = _ip(host)
    if ip is None or ip == QUAD100:
        return False
    return any(net is not None and ip.version == net.version and ip in net for net in (v4, v6))


def parse_status(status, v4: Network = TAILNET_V4) -> TailnetSelf:
    """This node's IPv4 and DNS name from `tailscale status --json`, only while Running."""
    if not isinstance(status, dict):
        raise TailscaleUnavailable("unreadable tailscale status")
    state = status.get("BackendState")
    if state != "Running":
        raise TailscaleUnavailable(f"Tailscale is not running (BackendState {state})")
    me = status.get("Self")
    ips = me.get("TailscaleIPs") if isinstance(me, dict) else None
    if not isinstance(ips, list):
        raise TailscaleUnavailable("tailscale status names no address for this node")
    for raw in ips:
        ip = _ip(raw) if isinstance(raw, str) else None
        if ip is not None and ip.version == 4 and ip in v4 and ip != QUAD100:
            dns = me.get("DNSName")
            return TailnetSelf(str(ip), dns.rstrip(".") if isinstance(dns, str) else "")
    raise TailscaleUnavailable(f"no Tailscale IPv4 in {v4} among {ips}")


def _target_port(target: str) -> int | None:
    """Port of a serve target: a URL, host:port, or a bare port number."""
    target = target.strip()
    if target.isdigit():
        return int(target)
    try:
        if "://" in target:
            return urlsplit(target).port
        return urlsplit("//" + target).port
    except ValueError:
        return None


def exposed_ports(serve, ports: Iterable[int]) -> set[int]:
    """Runtime ports any `tailscale serve` or Funnel handler forwards to, at any depth
    (background config and foreground sessions alike)."""
    wanted, found = set(ports), set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("Proxy", "TCPForward") and isinstance(v, str):
                    if (p := _target_port(v)) in wanted:
                        found.add(p)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(serve)
    return found


def detect(cli: Callable[..., object], ports: Iterable[int],
           v4: Network = TAILNET_V4) -> TailnetSelf:
    """This node's tailnet address, provided no serve or Funnel config forwards a port."""
    me = parse_status(cli("status", "--json"), v4)
    ports = set(ports)
    exposed = exposed_ports(cli("serve", "status", "--json"), ports)
    if exposed:
        raise TailscaleExposed(
            f"tailscale serve forwards runtime port {', '.join(map(str, sorted(exposed)))}; "
            "remove it with `tailscale serve reset` first")
    return me


def run_cli(*args: str):
    """Run the Tailscale CLI read only and parse its JSON."""
    exe = shutil.which("tailscale") or (str(APP_CLI) if APP_CLI.exists() else None)
    if exe is None:
        raise TailscaleUnavailable("no tailscale CLI on PATH or in /Applications")
    try:
        out = subprocess.run([exe, *args], capture_output=True, text=True,
                             timeout=CLI_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TailscaleUnavailable(f"tailscale {args[0]}: {e}") from None
    if out.returncode != 0:
        raise TailscaleUnavailable(f"tailscale {args[0]} failed: {out.stderr.strip()[:200]}")
    try:
        return json.loads(out.stdout or "{}")
    except ValueError:
        raise TailscaleUnavailable(f"tailscale {args[0]} printed no JSON") from None


@dataclass(frozen=True)
class Tailscale:
    """The CLI and the address ranges that count as the tailnet. Tests swap both
    (tests/tailscale_fixture.py); the runtime uses the defaults."""
    cli: Callable[..., object] = run_cli
    v4: Network = TAILNET_V4
    v6: Network | None = field(default=TAILNET_V6)

    def detect(self, ports: Iterable[int]) -> TailnetSelf:
        return detect(self.cli, ports, self.v4)

    def peer(self, host: str) -> bool:
        return tailnet_peer(host, self.v4, self.v6)
