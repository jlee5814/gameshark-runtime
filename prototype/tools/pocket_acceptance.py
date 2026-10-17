"""Stark Pocket acceptance: Mac → iPhone simulator → Mac, against the real runtime.

    uv run python tools/pocket_acceptance.py --app ../mac/build/StarkMac.app \
        --project ../ios/StarkPocket/StarkPocket.xcodeproj --ctl <path to starkctl>

Uses ~/.gameshark/demo (fresh), never the everyday database. Starts `stark.py run --lan`,
StarkMac with snapshots on, pairs the simulator through `stark.py pair`, and runs the
StarkPocket UI tests in the iOS simulator. Evidence comes from three independent places:
the runtime database (handoffs, rows, surface writes, inference calls), StarkMac's own
states.log and snapshots, and the UI test's screenshots. Writes pocket-acceptance.md.

With --tailscale the runtime also serves this Mac's Tailscale address, and every pairing
link names a dead LAN address and an unresolvable host, so the simulator reaches Stark only
by falling back to the tailnet route (the Mac's own utun address). The runtime log must
show every device session arriving via tailnet. This proves the fallback and the tailnet
listener, not cellular.

This exercises the iOS simulator on this Mac, not a physical iPhone.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from urllib.parse import parse_qs, urlencode, urlsplit

from gameshark.stark_chat import CONVERSATION

AGENT = "stark"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Pocket:
    def __init__(self, args) -> None:
        self.args = args
        self.dir: Path = args.dir
        self.db = self.dir / "runtime.db"
        self.sock = self.dir / "gateway" / "stark.sock"
        self.shots = self.dir / "pocket-shots"
        self.port = free_port()       # fixed across restarts, so the paired phone finds it
        self.tailnet_port = free_port()
        self.daemon: subprocess.Popen | None = None
        self.app: subprocess.Popen | None = None
        self.results: list[tuple[str, bool, str]] = []
        self.boots = 0
        self.faults: list[str] = []

    # ------------------------------------------------------------------ processes
    def start_daemon(self) -> None:
        self.boots += 1
        log = open(self.dir / f"pocket-daemon-{self.boots}.log", "w")  # noqa: SIM115
        env = {**clean_env(), "GAMESHARK_TRANSPORT": "tests.file_transport:make",
               "GAMESHARK_FIXTURE": str(self.dir / "github.json")}
        cmd = [sys.executable, str(ROOT / "stark.py"), "--db", str(self.db), "--interval", "2",
               "run", "--lan", "--lan-port", str(self.port), *model_args(self.args)]
        if self.args.tailscale:
            cmd += ["--tailscale", "--tailscale-port", str(self.tailnet_port)]
        self.daemon = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log,
                                       stderr=subprocess.STDOUT)
        info = self.sock.with_name(self.sock.name + ".lan.json")

        def served() -> bool:
            if not (info.exists() and self.sock.exists()):
                return False
            ad = json.loads(info.read_text())
            return ad["port"] == self.port and (not self.args.tailscale or "tailscale" in ad)

        self.wait(served, 30, "tailnet gateway" if self.args.tailscale else "LAN gateway")

    def stop_daemon(self, sig=signal.SIGTERM) -> int:
        assert self.daemon is not None
        self.daemon.send_signal(sig)
        code = self.daemon.wait(timeout=30)
        self.daemon = None
        return code

    def start_app(self) -> None:
        exe = self.args.app / "Contents" / "MacOS" / "StarkMac"
        env = {**os.environ, "STARK_SOCKET": str(self.sock), "STARK_SNAPSHOT_DIR": str(self.shots),
               "STARK_DEMO_CONTROL": "1"}
        self.app = subprocess.Popen([str(exe)], env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)

    def cli(self, *args: str) -> str:
        out = subprocess.run([sys.executable, str(ROOT / "stark.py"), "--db", str(self.db), *args],
                             cwd=ROOT, env=clean_env(), capture_output=True, text=True,
                             timeout=30, check=True)
        return out.stdout

    def pair_link(self, name: str) -> str:
        if not self.args.tailscale:
            return json.loads(self.cli("pair", "--name", name, "--json",
                                       "--address", "127.0.0.1"))["link"]
        link = json.loads(self.cli("pair", "--name", name, "--json",
                                   "--address", "192.0.2.1"))["link"]   # TEST-NET-1: dead
        q = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
        if "t" not in q:
            raise AssertionError(f"pair issued no tailnet endpoint: {link}")
        q["h"] = "no-such-mac.invalid"            # the Bonjour name must not rescue it either
        return "starkpocket://pair?" + urlencode(q)

    def device_paths(self) -> dict[str, int]:
        """Device sessions per path, from every daemon log of this run."""
        out: dict[str, int] = {}
        for f in sorted(self.dir.glob("pocket-daemon-*.log")):
            for line in f.read_text().splitlines():
                if "device " in line and " via " in line:
                    via = line.split(" via ", 1)[1].split()[0]
                    out[via] = out.get(via, 0) + 1
        return out

    def s6_tailnet_only(self) -> str:
        paths = self.device_paths()
        if not paths.get("tailnet") or set(paths) != {"tailnet"}:
            raise AssertionError(f"device sessions by path: {paths}")
        # Every user row came from exactly one surface write (deduplicated by client id).
        writes = self.q("SELECT COUNT(*), COUNT(DISTINCT seq) FROM surface_writes")[0]
        users = sum(1 for _s, r, _c in self.rows() if r == "user")
        if writes[0] != writes[1] or writes[0] != users:
            raise AssertionError(f"surface writes {writes} against {users} user rows")
        devices = self.q("SELECT COUNT(*) FROM devices")[0][0]
        return (f"{paths['tailnet']} device sessions, all via tailnet, none via LAN; "
                f"{devices} paired devices; {users} user rows from {writes[0]} unique writes")

    def ui_test(self, test: str, link: str, bundle: Path,
                signal_dir: Path | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "TEST_RUNNER_STARK_PAIR_LINK": link}
        if self.args.inference_url:
            env["TEST_RUNNER_STARK_LIVE_MODEL"] = "1"   # the UI test cannot know the words
        if signal_dir is not None:
            env["TEST_RUNNER_STARK_SIGNAL_DIR"] = str(signal_dir)
        shutil.rmtree(bundle, ignore_errors=True)
        cmd = ["xcodebuild", "-project", str(self.args.project), "-scheme", "StarkPocket",
               "-destination", f"platform=iOS Simulator,name={self.args.simulator}",
               "-derivedDataPath", str(self.args.project.parent / "build"),
               "-resultBundlePath", str(bundle), "test-without-building",
               f"-only-testing:StarkPocketUITests/{test}"]
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=900,
                              check=False)

    def export_shots(self, bundle: Path, into: Path) -> list[str]:
        into.mkdir(parents=True, exist_ok=True)
        subprocess.run(["xcrun", "xcresulttool", "export", "attachments", "--path", str(bundle),
                        "--output-path", str(into)], capture_output=True, timeout=120,
                       check=False)
        manifest = into / "manifest.json"
        names = []
        if manifest.exists():
            for test in json.loads(manifest.read_text()):
                for a in test.get("attachments", []):
                    src = into / a["exportedFileName"]
                    dst = into / (a.get("suggestedHumanReadableName") or src.name)
                    if src.exists() and src != dst:
                        src.rename(dst)
                    names.append(dst.name)
        return sorted(names)

    # ------------------------------------------------------------------ observation
    def q(self, sql: str, *params) -> list[tuple]:
        conn = sqlite3.connect(self.db, timeout=5)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def presence(self) -> tuple[str, int]:
        return self.q("SELECT surface, generation FROM presence WHERE agent_id=?", AGENT)[0]

    def rows(self) -> list[tuple[int, str, str]]:
        return self.q("SELECT seq, role, content FROM messages WHERE conversation_id=? "
                      "ORDER BY seq", CONVERSATION)

    def states(self) -> list[str]:
        p = self.shots / "states.log"
        return p.read_text().splitlines() if p.exists() else []

    def wait(self, pred, timeout: float, what: str):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if (v := pred()):
                    return v
            except (sqlite3.OperationalError, OSError, IndexError, KeyError, ValueError):
                pass
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting for {what}")

    def check(self, name: str, fn) -> None:
        try:
            detail = fn()
            self.results.append((name, True, detail))
            print(f"PASS  {name}  {detail}", flush=True)
        except Exception as e:
            self.results.append((name, False, f"{type(e).__name__}: {e}"))
            print(f"FAIL  {name}  {type(e).__name__}: {e}", flush=True)

    # ------------------------------------------------------------------ steps
    def s1_mac_active(self) -> str:
        line = self.wait(lambda: next((s for s in self.states() if "presence=mac@" in s), None),
                         30, "StarkMac live")
        from gameshark.gateway_client import GatewayClient

        token = bytes.fromhex((self.sock.parent / "gateway.token").read_text())
        with GatewayClient(self.sock, token, "mac") as mac:
            gen = mac.call("snapshot")["presence"]["generation"]
            mac.call("send", conversation_id=CONVERSATION, client_msg_id="mac-acceptance-1",
                     text="Remember: the release ships Friday.", presence_gen=gen)
        self.wait(lambda: self.rows()[-1][1] == "assistant", 30, "reply on the Mac")
        return f"StarkMac: {line.split(' ', 1)[1][:60]}; history row from the Mac answered"

    def mac_draft_lengths(self) -> list[tuple[str, int]]:
        """(presence, composer length) from every StarkMac state that reports a draft."""
        out = []
        for s in self.states():
            if " draft=" in s and "presence=" in s:
                out.append((s.split("presence=")[1].split()[0],
                            int(s.split(" draft=")[1].split()[0])))
        return out

    def s2_handoff(self) -> str:
        link = self.pair_link("iPhone Simulator")
        # Mac -> iPhone: StarkMac holds unsent text when the phone Jacks In.
        subprocess.run([str(self.args.ctl), "draft", "Hello", "from", "my", "pocket"],
                       check=True, timeout=10)
        self.wait(lambda: any(n == 20 for _p, n in self.mac_draft_lengths()), 10,
                  "StarkMac composer holding the draft")
        timeline: list[str] = []
        stop = threading.Event()

        def watch() -> None:
            last = None
            while not stop.is_set():
                try:
                    p = self.presence()
                except (sqlite3.OperationalError, IndexError):
                    p = None
                if p and p != last:
                    timeline.append(f"{p[0]}@{p[1]}")
                    last = p
                time.sleep(0.05)

        signals = self.dir / "pocket-signals"
        signals.mkdir(exist_ok=True)
        jacked_in: list[str] = []

        def mac_jacks_in() -> None:
            # Once the phone holds Stark again with unsent text, press Jack In Here on the
            # Mac through StarkMac's own control channel.
            marker = signals / "draft-typed"
            end = time.monotonic() + 300
            while not stop.is_set() and time.monotonic() < end:
                if marker.exists():
                    subprocess.run([str(self.args.ctl), "jackin"], check=False, timeout=10)
                    jacked_in.append(time.strftime("%H:%M:%S"))
                    return
                time.sleep(0.1)

        t = threading.Thread(target=watch, daemon=True)
        t.start()
        m = threading.Thread(target=mac_jacks_in, daemon=True)
        m.start()
        bundle = self.dir / "pocket-handoff.xcresult"
        res = self.ui_test("PocketHandoffUITests/testJackInTalkAndJackOut", link, bundle,
                           signal_dir=signals)
        stop.set()
        t.join()
        m.join()
        self.handoff_shots = self.export_shots(bundle, self.dir / "pocket-sim-shots" / "handoff")
        (self.dir / "pocket-handoff-xcodebuild.log").write_text(res.stdout + res.stderr)
        if res.returncode != 0:
            tail = [ln for ln in (res.stdout + res.stderr).splitlines() if "error" in ln][-3:]
            raise AssertionError(f"UI test failed: {tail}")
        self.timeline = timeline
        if not jacked_in:
            raise AssertionError("the phone never signalled its unsent draft")
        hops = self.q("SELECT from_surface, to_surface, draft, draft_state FROM handoffs "
                      "WHERE state='acked' AND draft IS NOT NULL ORDER BY created")
        want = [("mac", "Hello from my pocket"), ("pocket", "unsent from my pocket")]
        got = [(a.split("-")[0], d) for a, _b, d, _st in hops]
        if got != want or any(st != "delivered" for *_x, st in hops):
            raise AssertionError(f"carried drafts {hops}")
        lengths = self.mac_draft_lengths()
        if not any(p.startswith("pocket-") and n == 0 for p, n in lengths):
            raise AssertionError(f"StarkMac kept the draft that left: {lengths[-5:]}")
        if not any(p.startswith("mac@") and n == 21 for p, n in lengths):
            raise AssertionError(f"StarkMac never restored the phone's draft: {lengths[-6:]}")
        # Leave StarkMac's composer empty for the fault run's own pairing.
        subprocess.run([str(self.args.ctl), "draft"], check=True, timeout=10)
        return (f"UI test passed; presence timeline {' -> '.join(timeline)}; Mac -> iPhone "
                f"draft restored on the phone and confirmed; iPhone -> Mac draft restored in "
                f"StarkMac's composer (21 chars) and confirmed; both delivered, neither sent")

    def s3_mac_dimmed(self) -> str:
        ghost = [s for s in self.states() if s.split(" ")[1] == "ghost" and "presence=pocket-" in s]
        if not ghost:
            raise AssertionError("StarkMac never rendered the read only ghost")
        back = [s for s in self.states() if "presence=mac@" in s and s.split(" ")[1] != "ghost"]
        after = [s for s in back if self.states().index(s) > self.states().index(ghost[-1])]
        if not after:
            raise AssertionError("StarkMac never came back active after the ghost")
        return (f"ghost snapshot {ghost[0].split(' ')[0]}.png ({ghost[0].split('presence=')[1].split()[0]}), "
                f"active again {after[0].split(' ')[0]}.png ({after[0].split('presence=')[1].split()[0]})")

    def s4_journal(self) -> str:
        acked = self.q("SELECT from_surface, to_surface, from_gen, to_gen FROM handoffs "
                       "WHERE state='acked' ORDER BY created")
        if len(acked) < 2 or not acked[0][1].startswith("pocket-") or acked[1][1] != "mac":
            raise AssertionError(f"handoffs {acked}")
        phone = self.q("SELECT w.surface, m.content FROM surface_writes w JOIN messages m ON "
                       "m.conversation_id=w.conversation_id AND m.seq=w.seq")
        from_phone = [c for s, c in phone if s.startswith("pocket-")]
        if from_phone != ["Hello from my pocket"]:
            raise AssertionError(f"phone writes {phone}")
        users = [r for r in self.rows() if r[1] == "user"]
        replies = [r for r in self.rows() if r[1] == "assistant"]
        calls = self.q("SELECT COUNT(*) FROM inference_calls")[0][0]
        if not (len(users) == len(replies) == calls):
            raise AssertionError(f"{len(users)} user rows, {len(replies)} replies, {calls} calls")
        surface, gen = self.presence()
        if surface != "mac":
            raise AssertionError(f"presence {surface}@{gen}")
        mac_rows = self.wait(lambda: next(
            (int(s.split("rows=")[1].split()[0]) for s in reversed(self.states())
             if "presence=mac@" in s), None), 10, "StarkMac row count")
        if mac_rows != len(self.rows()):
            raise AssertionError(f"StarkMac shows {mac_rows} rows, journal has {len(self.rows())}")
        hops = ", ".join(f"{a}@{g}->{b}@{h}" for a, b, g, h in acked)
        return (f"acked {hops}; {len(users)} user turns, {len(replies)} replies, {calls} inference "
                f"calls; StarkMac renders all {mac_rows} rows; presence {surface}@{gen}")

    def s5_faults(self) -> str:
        link = self.pair_link("iPhone Simulator (faults)")
        log: list[str] = []
        error: list[BaseException] = []

        def drive() -> None:
            try:
                self.wait(lambda: any(c == "restart now" for _s, _r, c in self.rows()), 120,
                          "restart now")
                holder = self.presence()
                self.stop_daemon(signal.SIGKILL)
                log.append(f"SIGKILL while {holder[0]}@{holder[1]} held presence")
                time.sleep(2)
                self.start_daemon()
                log.append(f"restarted; presence {'@'.join(map(str, self.presence()))}")
                self.wait(lambda: any(c == "after restart" for _s, _r, c in self.rows()), 120,
                          "after restart")
                self.wait(lambda: self.rows()[-1][1] == "assistant", 30, "reply after restart")
                dev = self.q("SELECT id, surface FROM devices WHERE revoked_at IS NULL "
                             "ORDER BY created DESC LIMIT 1")[0]
                self.cli("devices", "revoke", dev[0])
                self.wait(lambda: self.presence()[0] == "mac", 10, "presence back home")
                log.append(f"revoked {dev[1]}; presence {'@'.join(map(str, self.presence()))}")
            except BaseException as e:
                error.append(e)

        t = threading.Thread(target=drive, daemon=True)
        t.start()
        bundle = self.dir / "pocket-faults.xcresult"
        res = self.ui_test("PocketFaultUITests/testRuntimeRestartThenRevocation", link, bundle)
        t.join(timeout=60)
        self.fault_shots = self.export_shots(bundle, self.dir / "pocket-sim-shots" / "faults")
        (self.dir / "pocket-faults-xcodebuild.log").write_text(res.stdout + res.stderr)
        if error:
            raise error[0]
        if res.returncode != 0:
            tail = [ln for ln in (res.stdout + res.stderr).splitlines() if "error" in ln][-3:]
            raise AssertionError(f"UI test failed: {tail}; driver: {log}")
        user = [c for _s, r, c in self.rows() if r == "user"]
        if user.count("restart now") != 1 or user.count("after restart") != 1:
            raise AssertionError(f"duplicated or lost rows {user}")
        self.faults = log
        return "; ".join(log) + "; app returned to pairing"

    def s7_memory(self) -> str:
        """Phase 4.2: save, recall, correct on the phone; the runtime SIGKILLed and restarted
        mid way; forget. Evidence from the database, not only the UI."""
        link = self.pair_link("iPhone Simulator (memory)")
        signals = self.dir / "memory-signals"
        signals.mkdir(exist_ok=True)
        log: list[str] = []
        error: list[BaseException] = []
        stop = threading.Event()

        def drive() -> None:
            try:
                marker = signals / "memory-restart"
                end = time.monotonic() + 600
                while not marker.exists():
                    if stop.is_set() or time.monotonic() > end:
                        return
                    time.sleep(0.1)
                before = self.q("SELECT id, content, revision FROM memories WHERE "
                                "state='active'")
                self.stop_daemon(signal.SIGKILL)
                log.append(f"SIGKILL with {before}")
                time.sleep(2)
                self.start_daemon()
                after = self.q("SELECT id, content, revision FROM memories WHERE "
                               "state='active'")
                if after != before:
                    raise AssertionError(f"memories {before} became {after} across SIGKILL")
                log.append("restarted with the same memories")
            except BaseException as e:
                error.append(e)

        t = threading.Thread(target=drive, daemon=True)
        t.start()
        bundle = self.dir / "pocket-memory.xcresult"
        res = self.ui_test("PocketMemoryUITests/testRememberRecallCorrectRestartForget", link,
                           bundle, signal_dir=signals)
        stop.set()
        t.join(timeout=60)
        self.memory_shots = self.export_shots(bundle, self.dir / "pocket-sim-shots" / "memory")
        (self.dir / "pocket-memory-xcodebuild.log").write_text(res.stdout + res.stderr)
        if error:
            raise error[0]
        if res.returncode != 0:
            tail = [ln for ln in (res.stdout + res.stderr).splitlines() if "error" in ln][-3:]
            raise AssertionError(f"UI test failed: {tail}; driver: {log}")
        mems = self.q("SELECT id, state, content, revision, source_surface FROM memories")
        if len(mems) != 1 or mems[0][1:4] != ("forgotten", "", 2) or not mems[0][4].startswith(
                "pocket-"):
            raise AssertionError(f"memories {mems}")
        ops = [k for (k,) in self.q("SELECT kind FROM memory_ops WHERE memory_id=? ORDER BY "
                                    "created", mems[0][0])]
        if ops != ["remember", "correct", "forget"]:
            raise AssertionError(f"memory ops {ops}")
        if any("/remember" in c for _s, _r, c in self.rows()):
            raise AssertionError("the /remember command became a chat row")
        replies = {c.split("You said: ")[-1][:30]: c for _s, r, c in self.rows()
                   if r == "assistant"}
        recall = next((c for k, c in replies.items() if k.startswith("Which locker is mine?")), "")
        after = next((c for k, c in replies.items() if k.startswith("Which locker is mine now")),
                     "")
        if not self.args.inference_url and ("My locker is 42" not in recall
                                            or "Saved memories" in after):
            raise AssertionError(f"recall {recall!r}; after forget {after!r}")
        return ("; ".join(log) + f"; ops {' -> '.join(ops)} from {mems[0][4]}; content wiped; "
                f"recall named the fact, the reply after forget did not")

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        self.shots.mkdir()
        (self.dir / "github.json").write_text("{}")
        self.handoff_shots: list[str] = []
        self.fault_shots: list[str] = []
        self.timeline: list[str] = []
        self.memory_shots: list[str] = []
        try:
            self.start_daemon()
            self.start_app()
            self.check("1 Stark is active on the Mac", self.s1_mac_active)
            self.check("2 to 9 simulator pairs, Jacks In, talks, Jacks Out", self.s2_handoff)
            self.check("6, 10 StarkMac dims while away and resumes", self.s3_mac_dimmed)
            self.check("8, 10 one journal, one identity, no duplicate inference", self.s4_journal)
            if not self.args.skip_faults:
                self.check("faults: runtime SIGKILL while the phone holds Stark, then revoke",
                           self.s5_faults)
            if self.args.tailscale:
                self.check("tailnet: every device session took the Tailscale route",
                           self.s6_tailnet_only)
            if not self.args.skip_memory:
                self.check("memory: save, recall, correct, SIGKILL restart, forget on the phone",
                           self.s7_memory)
        finally:
            if self.app is not None:
                self.app.terminate()
            if self.daemon is not None:
                self.stop_daemon()
        self.report()
        return 0 if all(ok for _n, ok, _d in self.results) else 1

    def report(self) -> None:
        mode = " over Tailscale fallback" if self.args.tailscale else ""
        lines = [f"# Stark Pocket acceptance (iOS simulator{mode})", "",
                 (f"Run {time.strftime('%Y-%m-%d %H:%M:%S')} on `{self.args.simulator}`. "
                 "This is the iOS simulator on the Mac, not a physical iPhone."), "",
                 "| Check | Result | Evidence |", "|---|---|---|"]
        for name, ok, detail in self.results:
            lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
        lines += ["", f"StarkMac snapshots and `states.log`: `{self.shots}`.",
                  (f"Simulator screenshots: `{self.dir / 'pocket-sim-shots'}` "
                  f"({', '.join(self.handoff_shots + self.fault_shots + self.memory_shots) or 'none'}).")]
        (self.dir / "pocket-acceptance.md").write_text("\n".join(lines) + "\n")
        print(f"\nreport: {self.dir / 'pocket-acceptance.md'}")


def clean_env() -> dict[str, str]:
    """This shell's environment without settings that would steer the demo daemon or the
    CLI away from the demo database: model variables (the tool names its model) and sockets."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith("GAMESHARK_INFERENCE_")
            and k not in ("GITHUB_TOKEN", "STARK_SOCKET", "STARK_TOKEN", "GAMESHARK_DB")}


def model_args(args) -> list[str]:
    """The daemon's model: the named endpoint, else the fake asked for by name."""
    if not args.inference_url:
        return ["--fake-model", "--fake-delay", str(args.fake_delay)]
    out = ["--inference-url", args.inference_url, "--model", args.model]
    return out + (["--inference-extra", args.inference_extra] if args.inference_extra else [])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--app", type=Path, required=True)
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--ctl", type=Path, required=True, help="starkctl, to press Jack In Here")
    ap.add_argument("--simulator", default="iPhone 18 Pro")
    ap.add_argument("--dir", type=Path, default=Path.home() / ".gameshark" / "demo")
    ap.add_argument("--fake-delay", type=float, default=2.0)
    ap.add_argument("--inference-url", default=None, help="for example http://127.0.0.1:30000/v1")
    ap.add_argument("--model", default="default")
    ap.add_argument("--inference-extra", default=None,
                    help="JSON merged into each request, as for stark.py run")
    ap.add_argument("--skip-faults", action="store_true")
    ap.add_argument("--skip-memory", action="store_true")
    ap.add_argument("--tailscale", action="store_true",
                    help="serve the tailnet too and pair with a dead LAN address")
    args = ap.parse_args()
    args.app, args.project, args.ctl = args.app.resolve(), args.project.resolve(), args.ctl.resolve()
    return Pocket(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
