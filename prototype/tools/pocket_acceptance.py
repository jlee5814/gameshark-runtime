"""Stark Pocket acceptance: Mac → iPhone simulator → Mac, against the real runtime.

    uv run python tools/pocket_acceptance.py --app ../mac/build/StarkMac.app \
        --project ../ios/StarkPocket/StarkPocket.xcodeproj

Uses ~/.gameshark/demo (fresh), never the everyday database. Starts `stark.py run --lan`,
StarkMac with snapshots on, pairs the simulator through `stark.py pair`, and runs the
StarkPocket UI tests in the iOS simulator. Evidence comes from three independent places:
the runtime database (handoffs, rows, surface writes, inference calls), StarkMac's own
states.log and snapshots, and the UI test's screenshots. Writes pocket-acceptance.md.

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
        self.daemon: subprocess.Popen | None = None
        self.app: subprocess.Popen | None = None
        self.results: list[tuple[str, bool, str]] = []
        self.boots = 0
        self.faults: list[str] = []

    # ------------------------------------------------------------------ processes
    def start_daemon(self) -> None:
        self.boots += 1
        log = open(self.dir / f"pocket-daemon-{self.boots}.log", "w")  # noqa: SIM115
        env = {**os.environ, "GAMESHARK_TRANSPORT": "tests.file_transport:make",
               "GAMESHARK_FIXTURE": str(self.dir / "github.json")}
        env.pop("GITHUB_TOKEN", None)
        cmd = [sys.executable, str(ROOT / "stark.py"), "--db", str(self.db), "--interval", "2",
               "run", "--lan", "--lan-port", str(self.port),
               "--fake-delay", str(self.args.fake_delay)]
        self.daemon = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log,
                                       stderr=subprocess.STDOUT)
        info = self.sock.parent / "lan.json"
        self.wait(lambda: info.exists() and json.loads(info.read_text())["port"] == self.port
                  and self.sock.exists(), 30, "LAN gateway")

    def stop_daemon(self, sig=signal.SIGTERM) -> int:
        assert self.daemon is not None
        self.daemon.send_signal(sig)
        code = self.daemon.wait(timeout=30)
        self.daemon = None
        return code

    def start_app(self) -> None:
        exe = self.args.app / "Contents" / "MacOS" / "StarkMac"
        env = {**os.environ, "STARK_SOCKET": str(self.sock), "STARK_SNAPSHOT_DIR": str(self.shots)}
        self.app = subprocess.Popen([str(exe)], env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)

    def cli(self, *args: str) -> str:
        out = subprocess.run([sys.executable, str(ROOT / "stark.py"), "--db", str(self.db), *args],
                             cwd=ROOT, capture_output=True, text=True, timeout=30, check=True)
        return out.stdout

    def pair_link(self, name: str) -> str:
        return json.loads(self.cli("pair", "--name", name, "--json",
                                   "--address", "127.0.0.1"))["link"]

    def ui_test(self, test: str, link: str, bundle: Path) -> subprocess.CompletedProcess:
        env = {**os.environ, "TEST_RUNNER_STARK_PAIR_LINK": link}
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

    def s2_handoff(self) -> str:
        link = self.pair_link("iPhone Simulator")
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

        t = threading.Thread(target=watch, daemon=True)
        t.start()
        bundle = self.dir / "pocket-handoff.xcresult"
        res = self.ui_test("PocketHandoffUITests/testJackInTalkAndJackOut", link, bundle)
        stop.set()
        t.join()
        self.handoff_shots = self.export_shots(bundle, self.dir / "pocket-sim-shots" / "handoff")
        (self.dir / "pocket-handoff-xcodebuild.log").write_text(res.stdout + res.stderr)
        if res.returncode != 0:
            tail = [ln for ln in (res.stdout + res.stderr).splitlines() if "error" in ln][-3:]
            raise AssertionError(f"UI test failed: {tail}")
        self.timeline = timeline
        return f"UI test passed; presence timeline {' -> '.join(timeline)}"

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
        finally:
            if self.app is not None:
                self.app.terminate()
            if self.daemon is not None:
                self.stop_daemon()
        self.report()
        return 0 if all(ok for _n, ok, _d in self.results) else 1

    def report(self) -> None:
        lines = ["# Stark Pocket acceptance (iOS simulator)", "",
                 (f"Run {time.strftime('%Y-%m-%d %H:%M:%S')} on `{self.args.simulator}`. "
                 "This is the iOS simulator on the Mac, not a physical iPhone."), "",
                 "| Check | Result | Evidence |", "|---|---|---|"]
        for name, ok, detail in self.results:
            lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
        lines += ["", f"StarkMac snapshots and `states.log`: `{self.shots}`.",
                  (f"Simulator screenshots: `{self.dir / 'pocket-sim-shots'}` "
                  f"({', '.join(self.handoff_shots + self.fault_shots) or 'none'}).")]
        (self.dir / "pocket-acceptance.md").write_text("\n".join(lines) + "\n")
        print(f"\nreport: {self.dir / 'pocket-acceptance.md'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--app", type=Path, required=True)
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--simulator", default="iPhone 18 Pro")
    ap.add_argument("--dir", type=Path, default=Path.home() / ".gameshark" / "demo")
    ap.add_argument("--fake-delay", type=float, default=2.0)
    ap.add_argument("--skip-faults", action="store_true")
    args = ap.parse_args()
    args.app, args.project = args.app.resolve(), args.project.resolve()
    return Pocket(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
