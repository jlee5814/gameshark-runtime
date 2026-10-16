"""Tailscale detection and admission: the tailnet listener binds only this Mac's current
Tailscale address, admits only tailnet peers, and never starts while `tailscale serve` or
Funnel forwards one of the runtime's ports. Nothing here touches the real CLI."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.tailscale import (
    TailnetSelf,
    TailscaleUnavailable,
    detect,
    exposed_ports,
    parse_status,
    tailnet_peer,
)

from gameshark.net_gateway import lan_peer

RUNNING = {"BackendState": "Running", "MagicDNSSuffix": "tail7fe60e.ts.net",
           "Self": {"DNSName": "mac.tail7fe60e.ts.net.",
                    "TailscaleIPs": ["fd7a:115c:a1e0::3501:39dd", "100.123.57.23"]}}


# ---------------------------------------------------------------------- admission
@pytest.mark.parametrize("ip", ["fd7a:115c:a1e0::1", "fd7a:115c:a1e0::3501:39dd"])
def test_lan_mode_refuses_tailnet_ipv6_peers(ip):
    """Tailscale's IPv6 range sits inside fc00::/7; LAN mode must not admit it unasked."""
    assert lan_peer(ip) is False


@pytest.mark.parametrize("ip,ok", [
    ("100.64.0.1", True), ("100.127.255.254", True), ("100.123.57.23", True),
    ("fd7a:115c:a1e0::1", True), ("::ffff:100.64.0.1", True),
    ("100.63.255.255", False), ("100.128.0.0", False), ("100.100.100.100", False),
    ("192.168.1.2", False), ("10.0.0.1", False), ("127.0.0.1", False), ("::1", False),
    ("fd12::1", False), ("8.8.8.8", False), ("garbage", False)])
def test_only_tailnet_peers_are_admitted(ip, ok):
    assert tailnet_peer(ip) is ok


# ---------------------------------------------------------------------- detection
def test_running_status_yields_the_ipv4_address_and_dns_name():
    assert parse_status(RUNNING) == TailnetSelf("100.123.57.23", "mac.tail7fe60e.ts.net")


@pytest.mark.parametrize("status", [
    {**RUNNING, "BackendState": "Stopped"},
    {**RUNNING, "BackendState": "NeedsLogin"},
    {"BackendState": "Running"},
    {**RUNNING, "Self": {"DNSName": "x.", "TailscaleIPs": ["fd7a:115c:a1e0::1"]}},
    {**RUNNING, "Self": {"DNSName": "x.", "TailscaleIPs": ["192.168.1.2"]}},
    {**RUNNING, "Self": {"DNSName": "x.", "TailscaleIPs": "100.64.0.1"}},
    [],
])
def test_unusable_status_is_refused(status):
    with pytest.raises(TailscaleUnavailable):
        parse_status(status)


class FakeCLI:
    def __init__(self, status, serve=None, fail: set[str] = frozenset()) -> None:
        self.status_json, self.serve_json, self.fail = status, serve or {}, fail
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        if args[0] in self.fail:
            raise TailscaleUnavailable(f"tailscale {args[0]} failed")
        return self.status_json if args[0] == "status" else self.serve_json


def test_detect_reads_status_and_serve_config_and_never_changes_them():
    cli = FakeCLI(RUNNING)
    found = detect(cli, ports={47820, 47821})
    assert found == TailnetSelf("100.123.57.23", "mac.tail7fe60e.ts.net")
    assert cli.calls == [("status", "--json"), ("serve", "status", "--json")]


def test_detect_fails_closed_when_the_serve_config_is_unreadable():
    with pytest.raises(TailscaleUnavailable):
        detect(FakeCLI(RUNNING, fail={"serve"}), ports={47820})


def test_detect_refuses_when_serve_or_funnel_forwards_a_runtime_port():
    serve = {"TCP": {"443": {"HTTPS": True}},
             "Web": {"mac.tail7fe60e.ts.net:443": {"Handlers": {
                 "/": {"Proxy": "http://127.0.0.1:47820"}}}},
             "AllowFunnel": {"mac.tail7fe60e.ts.net:443": True}}
    with pytest.raises(TailscaleUnavailable, match="47820"):
        detect(FakeCLI(RUNNING, serve), ports={47820, 47821})


# ---------------------------------------------------------------------- exposure
@pytest.mark.parametrize("serve,found", [
    ({}, set()),
    ({"TCP": {"8443": {"TCPForward": "127.0.0.1:47821"}}}, {47821}),
    ({"TCP": {"10000": {"TCPForward": "localhost:47820"}}}, {47820}),
    ({"Web": {"h:443": {"Handlers": {"/": {"Proxy": "http://localhost:47820/"}}}}}, {47820}),
    ({"Web": {"h:443": {"Handlers": {"/": {"Proxy": "https+insecure://[::1]:47821"}}}}},
     {47821}),
    ({"Web": {"h:443": {"Handlers": {"/": {"Proxy": "47820"}}}}}, {47820}),
    ({"Foreground": {"s1": {"TCP": {"443": {"TCPForward": "127.0.0.1:47820"}}}}}, {47820}),
    ({"Web": {"h:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}, set()),
    ({"Web": {"h:443": {"Handlers": {"/": {"Path": "/srv/47820"}}}}}, set()),
])
def test_exposed_ports_finds_every_forward_to_a_runtime_port(serve, found):
    assert exposed_ports(serve, {47820, 47821}) == found
