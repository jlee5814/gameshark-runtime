"""Stark: the local companion CLI for GameShark Runtime.

One prompt assigns a responsibility; the daemon keeps it across restarts.

    uv run python stark.py watch owner/repo#123      # assign (applied now or by the daemon)
    uv run python stark.py run --interval 60         # serve every responsibility, no prompts
    uv run python stark.py status                    # identity, responsibilities, backoff
    uv run python stark.py feed                      # unread updates (--all for history)
    uv run python stark.py ack --all                 # mark updates read
    uv run python stark.py resume owner/repo#123     # after fixing a revoked token
    uv run python stark.py unwatch owner/repo#123
    uv run python stark.py jack ghost                # move presence (demo of stale writes)
    uv run python stark.py run --lan                 # also serve paired phones on the LAN
    uv run python stark.py pair --name "My iPhone"   # one time pairing link for Stark Pocket
    uv run python stark.py devices                   # paired devices; `devices revoke <id>`

State lives in ~/.gameshark/runtime.db (override with --db or GAMESHARK_DB). The GitHub
token comes from GITHUB_TOKEN, else `gh auth token`. Every GitHub call is a GET.

`run` also serves the surface gateway (StarkMac) on a Unix socket in a private directory
beside the database, gateway/stark.sock, authenticated by gateway.token beside it. Replies come from a deterministic local fake
model unless --inference-url names an OpenAI compatible server such as SGLang. With --lan it
also serves paired devices over pinned TLS 1.3 on the local network (net_gateway.py); the
listener is off by default and refuses addresses outside private ranges.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from companion import Companion, macos_banner, render
from gameshark.errors import LeaseDenied
from gameshark.store import connect
from gameshark.worker import (
    AlreadyRunning,
    InstanceLock,
    apply_controls,
    enqueue_control,
    run_forever,
)

AGENT = "stark"
DEFAULT_DB = Path.home() / ".gameshark" / "runtime.db"
_TARGET = re.compile(
    r"^(?:https://github\.com/)?([\w.-]+/[\w.-]+)(?:#|/pull/)(\d+)/?$"
)


def owner_token() -> str:
    # Stable per host: a restarted daemon reclaims its own live lease immediately. The
    # instance lock keeps two processes on one host from sharing it.
    return f"{socket.gethostname()}:{AGENT}"


def parse_target(text: str) -> tuple[str, int]:
    m = _TARGET.match(text.strip())
    if not m:
        raise SystemExit(f"expected owner/repo#N or a pull request URL, got {text!r}")
    return m.group(1), int(m.group(2))


def make_client():
    """Real read only client unless GAMESHARK_TRANSPORT names a test factory."""
    spec = os.environ.get("GAMESHARK_TRANSPORT")
    if spec:
        mod, _, attr = spec.partition(":")
        return getattr(importlib.import_module(mod), attr)()
    from gameshark.github_client import RestGitHubClient, token_from_env

    return RestGitHubClient(token_from_env())


class _LazyClient:
    """Defers token lookup: applying a watch request needs no network."""

    def __init__(self) -> None:
        self._c = None

    def __getattr__(self, name):
        if self._c is None:
            self._c = make_client()
        return getattr(self._c, name)


HOME_SURFACE = "mac"


def socket_path(args, env=os.environ) -> Path:
    """--socket, else STARK_SOCKET, else gateway/stark.sock beside the database. StarkKit's
    ClientConfig.fromEnvironment applies the same rule."""
    if getattr(args, "socket", None):
        return args.socket
    if env.get("STARK_SOCKET"):
        return Path(env["STARK_SOCKET"])
    return args.db.parent / "gateway" / "stark.sock"


def token_path(args, env=os.environ) -> Path:
    """--token, else STARK_TOKEN, else gateway.token beside the socket."""
    if getattr(args, "token", None):
        return args.token
    if env.get("STARK_TOKEN"):
        return Path(env["STARK_TOKEN"])
    return socket_path(args, env).parent / "gateway.token"


def make_provider(args):
    """The model behind Stark's replies: a local fake unless an endpoint is named."""
    from gameshark.inference import OpenAICompatProvider, ProviderConfig
    from gameshark.stark_chat import CompanionFakeProvider

    if args.inference_url:
        return ProviderConfig(OpenAICompatProvider("local", args.inference_url, args.model,
                                                   args.context_window, max_concurrency=2))
    return ProviderConfig(CompanionFakeProvider(args.fake_delay))


def build_supervisor(db: Path, interval: float, notifier=None, client=None, chat=None):
    from gameshark.stark_chat import StarkChatAdapter
    from gameshark.supervisor import AgentSupervisor
    from sims.github_monitor import GitHubMonitorAdapter

    sup = AgentSupervisor(db_path=db, agent_id=AGENT, owner_token=owner_token(),
                          lease_ttl_s=max(30.0, 3 * interval))
    sup.add_adapter(GitHubMonitorAdapter(client or _LazyClient(), poll_interval_s=interval,
                                         notifier=notifier))
    # Without a chat adapter that serves, reply events wait for the daemon.
    sup.add_adapter(chat or StarkChatAdapter(serve=False))
    sup.enable_presence(HOME_SURFACE)
    return sup


def lan_info_path(args) -> Path:
    """Where `run --lan` records its port and pin for `stark.py pair`."""
    return socket_path(args).parent / "lan.json"


def start_gateway(sup, chat, args, log):
    """Serve surfaces, or log why not. The daemon keeps its duties either way."""
    from gameshark.devices import DeviceRegistry
    from gameshark.gateway import GatewayServer, load_or_create_token
    from gameshark.net_gateway import LanListener, load_or_create_identity
    from gameshark.surface_api import SurfaceAPI

    path = socket_path(args)
    lan = None
    # A previous instance's endpoint is never advertised; this boot rewrites it once served.
    try:
        lan_info_path(args).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        token = load_or_create_token(token_path(args))
        ident = {"agent_id": sup.identity.agent_id, "uuid": sup.identity.uuid}
        server = GatewayServer(path, token, ident, sup.wake)
        # Revocations apply with or without --lan: a revoked phone never keeps presence.
        devices = DeviceRegistry(sup.conn, sup.agent_id, sup.lease, sup.generation, sup.clock)
        if getattr(args, "lan", False):
            identity = load_or_create_identity(path.parent)
            lan = LanListener(identity, port=args.lan_port)
            server.attach(lan)
        server.start()                    # all or nothing: a failure closes every socket
    except (OSError, ValueError, TypeError) as e:
        log(f"gateway disabled: {type(e).__name__}: {e}")
        return None
    log(f"gateway listening on {path}")
    if lan is not None:
        info = lan_info_path(args)
        tmp = info.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"port": lan.port, "pin": lan.identity.pin_hex}, f)
        tmp.replace(info)
        log(f"lan gateway on port {lan.port}, pin {lan.identity.pin_hex[:16]}...")
    return SurfaceAPI(sup, server, chat, log=log, devices=devices)


# ---------------------------------------------------------------------- commands
def cmd_control(args, action: str) -> int:
    if action == "jack":
        adapter, payload = "stark", {"to": args.surface}
    else:
        repo, pr = parse_target(args.target)
        adapter, payload = "github", {"repo": repo, "pr": pr}
    if action == "watch":
        payload["baseline"] = not args.backfill
    conn = connect(args.db)
    rid = enqueue_control(conn, AGENT, adapter, action, payload)
    conn.close()
    try:
        with InstanceLock(args.db):
            sup = build_supervisor(args.db, args.interval)
            sup.start()
            try:
                for line in apply_controls(sup):
                    print(f"applied {line}")
            finally:
                sup.stop()
                sup.close()
    except AlreadyRunning:
        print(f"queued request {rid}; the running daemon applies it on its next tick")
    except LeaseDenied as e:
        print(f"queued request {rid}; another host owns Stark ({e})")
    return 0


def cmd_run(args) -> int:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def log(msg: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

    def on_new(note: dict) -> None:
        log(f"[{note['kind']}] {note['repo']}#{note['pr']} {note['summary']}")
        if args.notify:
            macos_banner(note)

    try:
        lock = InstanceLock(args.db).acquire()
    except AlreadyRunning as e:
        print(f"Stark is already running: {e}", file=sys.stderr)
        return 3
    try:
        from gameshark.inference import RouterPolicy
        from gameshark.stark_chat import StarkChatAdapter

        chat = StarkChatAdapter()
        sup = build_supervisor(args.db, args.interval, notifier=on_new, client=make_client(),
                               chat=chat)
        sup.enable_inference([make_provider(args)],
                             policy=RouterPolicy(agent_in_flight=2, conversation_in_flight=1),
                             retry_s=0.25)
        try:
            sup.start()                       # recover: reconcile effects, replay inbox
        except LeaseDenied as e:
            print(f"another host owns Stark: {e}", file=sys.stderr)
            return 3
        active = [r.id for r in sup.responsibilities.outstanding()]
        log(f"Stark {sup.identity.uuid} generation {sup.generation}; "
            f"resuming {len(active)} responsibilities: {', '.join(active) or 'none'}")
        gateway = start_gateway(sup, chat, args, log) if args.gateway else None
        code = run_forever(sup, stop, args.interval, log=log,
                           drain_deadline_s=args.drain_deadline, gateway=gateway)
        if gateway is not None:
            lan_info_path(args).unlink(missing_ok=True)   # nothing serves it any more
        log("stopped")
        sup.close()
        return code
    finally:
        lock.release()


def lan_address() -> str:
    """This Mac's address on the default route's interface; sends nothing."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))                # TEST-NET-1: routing lookup only
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def lan_served(port: int, pin: bytes) -> bool:
    """Whether this Mac answers on port with the pinned key and a device challenge. Closes
    before any hello, which the gateway counts as a disconnect, not a failed handshake."""
    from gameshark.gateway import FrameError, read_frame
    from gameshark.gateway_client import GatewayError, tls_connect

    try:
        with tls_connect("127.0.0.1", port, pin, timeout_s=2.0) as s:
            alive = threading.Event()
            alive.set()
            first = read_frame(s, alive)
    except (OSError, GatewayError, FrameError):
        return False
    return first.get("type") == "challenge" and first.get("server_fp") == pin.hex()


def cmd_pair(args) -> int:
    from urllib.parse import urlencode

    from gameshark.devices import AuthRejected, create_pairing

    info = lan_info_path(args)
    if not info.exists():
        print("no LAN gateway yet: start `stark.py run --lan` first", file=sys.stderr)
        return 2
    if not InstanceLock(args.db).held_elsewhere():
        print("the daemon is not running, so lan.json is stale: start `stark.py run --lan` "
              "first", file=sys.stderr)
        return 2
    try:
        lan = json.loads(info.read_text())
        port, pin = int(lan["port"]), bytes.fromhex(lan["pin"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"unreadable {info}: {e}", file=sys.stderr)
        return 2
    if not lan_served(port, pin):
        print(f"the running daemon does not serve LAN port {port} with this pin; restart it "
              "with `stark.py run --lan`", file=sys.stderr)
        return 2
    conn = connect(args.db)
    try:
        pid, secret = create_pairing(conn, AGENT, args.name, time.time(), args.ttl)
    except AuthRejected as e:
        print(e, file=sys.stderr)
        return 2
    finally:
        conn.close()
    host = socket.gethostname()
    host = host if host.endswith(".local") else host + ".local"
    query = {"v": 1, "h": host, "a": args.address or lan_address(), "p": port,
             "fp": pin.hex(), "id": pid, "s": secret}
    link = "starkpocket://pair?" + urlencode(query)
    if args.json:
        print(json.dumps({"link": link, "pairing_id": pid, "expires_in": args.ttl}))
        return 0
    print(f"Pairing link for {args.name!r} (single use, expires in {args.ttl:.0f} s):\n")
    print(link + "\n")
    try:
        import qrcode  # optional: uv run --with qrcode ...

        qr = qrcode.QRCode(border=1)
        qr.add_data(link)
        qr.print_ascii(invert=True)
    except ImportError:
        print("(for a QR code: uv run --with qrcode==8.0 python stark.py pair ...)")
    print("Scan it with the iPhone camera, or paste it in Stark Pocket. Anyone holding this "
          "link before it is used can pair, so share it only with your own phone.")
    return 0


def cmd_devices(args) -> int:
    from gameshark.devices import list_devices, revoke

    conn = connect(args.db)
    try:
        if args.action == "revoke":
            if not args.device:
                print("devices revoke needs a device id", file=sys.stderr)
                return 2
            if not revoke(conn, AGENT, args.device, time.time()):
                print(f"no live device {args.device}", file=sys.stderr)
                return 1
            print(f"revoked {args.device}")
            _poke_daemon(args)
            return 0
        for d in list_devices(conn, AGENT):
            state = "revoked" if d.revoked_at else "active"
            print(f"{d.id}  {d.surface}  {state:7}  {d.name}")
    finally:
        conn.close()
    return 0


def _poke_daemon(args) -> None:
    """Ask a running daemon to pump now, so a revocation lands at once (a ping)."""
    from gameshark.gateway import load_or_create_token
    from gameshark.gateway_client import GatewayClient, GatewayError

    if not InstanceLock(args.db).held_elsewhere():
        return
    try:
        token = load_or_create_token(token_path(args))
        with GatewayClient(socket_path(args), token, "cli", timeout_s=2.0) as c:
            c.call("ping")
    except (OSError, GatewayError) as e:
        print(f"daemon not reachable ({e}); it applies the revocation on its next tick")


def cmd_status(args) -> int:
    conn = connect(args.db)
    ident = conn.execute("SELECT uuid FROM identity WHERE agent_id=?", (AGENT,)).fetchone()
    lease = conn.execute("SELECT * FROM lease WHERE agent_id=?", (AGENT,)).fetchone()
    running = InstanceLock(args.db).held_elsewhere()
    print(f"identity  {ident['uuid'] if ident else '(not yet created)'}")
    if lease:
        live = lease["expires_at"] > time.time()
        print(f"lease     generation {lease['generation']} "
              f"{lease['state'] + ' by ' + str(lease['owner_token']) if live else 'free'}")
    print(f"daemon    {'running' if running else 'not running'}")
    rows = conn.execute(
        "SELECT r.id, r.state, r.checkpoint, m.status, m.next_poll_at, m.last_ok_at, "
        "m.last_error, m.last_cursor, (SELECT COUNT(*) FROM monitor_seen s WHERE "
        "s.repo=m.repo AND s.pr=m.pr AND s.agent_id=m.agent_id) AS seen "
        "FROM responsibilities r LEFT JOIN monitor_state m ON m.agent_id=r.agent_id AND "
        "r.id = 'monitor:' || m.repo || ':' || m.pr WHERE r.agent_id=? ORDER BY r.created",
        (AGENT,),
    ).fetchall()
    print(f"responsibilities ({len(rows)})")
    now = time.time()
    for r in rows:
        due = ""
        if r["state"] == "active" and r["next_poll_at"]:
            due = f" next poll in {max(0, r['next_poll_at'] - now):.0f}s"
        ok = f" last ok {now - r['last_ok_at']:.0f}s ago" if r["last_ok_at"] else ""
        print(f"  {r['id']}  {r['state']}  monitor={r['status']}  seen={r['seen']}"
              f"  cursor={r['last_cursor'] or '-'}{due}{ok}")
        if r["last_error"]:
            print(f"      last error: {r['last_error']}")
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM control_requests WHERE status='pending'").fetchone()["c"]
    unread = len(Companion(conn).unread())
    print(f"queued requests {pending}; unread updates {unread}")
    return 0


def cmd_feed(args) -> int:
    conn = connect(args.db)
    comp = Companion(conn)
    print(render(comp.feed() if args.all else comp.unread()))
    return 0


def cmd_ack(args) -> int:
    conn = connect(args.db)
    n = Companion(conn).ack(None if args.all else [int(i) for i in args.ids])
    print(f"marked {n} read")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="stark", description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path,
                    default=Path(os.environ.get("GAMESHARK_DB", DEFAULT_DB)))
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between polls")
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="assign a pull request monitoring responsibility")
    w.add_argument("target")
    w.add_argument("--backfill", action="store_true",
                   help="also deliver activity that predates the watch")
    for name in ("unwatch", "resume"):
        sub.add_parser(name).add_argument("target")
    r = sub.add_parser("run", help="serve responsibilities until stopped")
    r.add_argument("--notify", action="store_true", help="macOS banners for new updates")
    r.add_argument("--drain-deadline", type=float, default=10.0,
                   help="seconds in flight work may finish after SIGTERM before cancellation")
    r.add_argument("--no-gateway", dest="gateway", action="store_false",
                   help="do not serve surfaces on the Unix socket")
    r.add_argument("--socket", type=Path, default=None,
                   help="gateway socket path (default: STARK_SOCKET, else "
                        "gateway/stark.sock beside the database)")
    r.add_argument("--token", type=Path, default=None,
                   help="gateway token file (default: STARK_TOKEN, else gateway.token "
                        "beside the socket)")
    r.add_argument("--fake-delay", type=float, default=2.0,
                   help="seconds the local fake model takes per reply")
    r.add_argument("--inference-url", default=os.environ.get("GAMESHARK_INFERENCE_URL"),
                   help="OpenAI compatible base URL, for example http://127.0.0.1:30000/v1")
    r.add_argument("--model", default=os.environ.get("GAMESHARK_INFERENCE_MODEL", "default"))
    r.add_argument("--context-window", type=int, default=8192)
    r.add_argument("--lan", action="store_true",
                   help="also serve paired devices over TLS on the local network")
    r.add_argument("--lan-port", type=int, default=47820)
    pr = sub.add_parser("pair", help="print a one time pairing link for Stark Pocket")
    pr.add_argument("--name", default="iPhone", help="the device's display name")
    pr.add_argument("--ttl", type=float, default=300.0, help="seconds the link stays valid")
    pr.add_argument("--address", default=None, help="LAN address to put in the link")
    pr.add_argument("--json", action="store_true", help="machine readable output")
    dv = sub.add_parser("devices", help="list paired devices, or revoke one")
    dv.add_argument("action", nargs="?", choices=["list", "revoke"], default="list")
    dv.add_argument("device", nargs="?")
    j = sub.add_parser("jack", help="move Stark's presence to another surface")
    j.add_argument("surface")
    sub.add_parser("status")
    f = sub.add_parser("feed")
    f.add_argument("--all", action="store_true")
    a = sub.add_parser("ack")
    a.add_argument("ids", nargs="*")
    a.add_argument("--all", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    if args.cmd in ("watch", "unwatch", "resume", "jack"):
        return cmd_control(args, args.cmd)
    return {"run": cmd_run, "status": cmd_status, "feed": cmd_feed, "ack": cmd_ack,
            "pair": cmd_pair, "devices": cmd_devices}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
