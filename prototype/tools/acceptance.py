"""StarkMac acceptance demonstration: the seven Phase 2 checks against the real runtime.

Starts `stark.py run` on a demo database, launches StarkMac with its snapshot log and demo
control channel, then drives and verifies each step. Every data check reads SQLite or the
gateway; every visual step leaves a PNG rendered by the app itself. --interactive pauses
for a real mouse drag and a look at the desktop.

    uv run python tools/acceptance.py --app ../mac/build/StarkMac.app --ctl <starkctl>

Writes acceptance.md and the snapshots to the demo directory (default ~/.gameshark/demo).
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gameshark.gateway_client import GatewayClient, GatewayError
from gameshark.stark_chat import CONVERSATION


class Demo:
    def __init__(self, args) -> None:
        self.args = args
        self.dir: Path = args.dir
        self.db = self.dir / "runtime.db"
        self.sock = self.dir / "gateway" / "stark.sock"   # the daemon default
        self.shots = self.dir / "shots"
        self.daemon: subprocess.Popen | None = None
        self.app: subprocess.Popen | None = None
        self.results: list[tuple[str, bool, str]] = []
        self.daemon_logs = 0

    # ------------------------------------------------------------------ processes
    def start_daemon(self) -> None:
        self.daemon_logs += 1
        log = open(self.dir / f"daemon-{self.daemon_logs}.log", "w")  # noqa: SIM115
        env = {**os.environ, "GAMESHARK_TRANSPORT": "tests.file_transport:make",
               "GAMESHARK_FIXTURE": str(self.dir / "github.json")}
        env.pop("GITHUB_TOKEN", None)
        cmd = [sys.executable, str(ROOT / "stark.py"), "--db", str(self.db), "--interval", "2",
               "run", "--fake-delay", str(self.args.fake_delay)]
        if self.args.inference_url:
            cmd += ["--inference-url", self.args.inference_url, "--model", self.args.model]
        self.daemon = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log,
                                       stderr=subprocess.STDOUT)
        self.wait(lambda: self.sock.exists(), 30, "gateway socket")

    def stop_daemon(self) -> int:
        assert self.daemon is not None
        self.daemon.send_signal(signal.SIGTERM)
        code = self.daemon.wait(timeout=30)
        self.daemon = None
        return code

    def start_app(self) -> None:
        exe = self.args.app / "Contents" / "MacOS" / "StarkMac"
        env = {**os.environ, "STARK_SOCKET": str(self.sock), "STARK_SNAPSHOT_DIR": str(self.shots),
               "STARK_DEMO_CONTROL": "1"}
        self.app = subprocess.Popen([str(exe)], env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)

    def ctl(self, *args: str) -> None:
        subprocess.run([str(self.args.ctl), *args], check=True, timeout=10)

    # ------------------------------------------------------------------ observation
    def client(self) -> GatewayClient:
        return GatewayClient(self.sock, bytes.fromhex((self.sock.parent / "gateway.token").read_text()),
                             surface="mac")

    def rows(self) -> list[tuple[int, str, str]]:
        conn = sqlite3.connect(self.db, timeout=5)
        try:
            return conn.execute("SELECT seq, role, content FROM messages WHERE "
                                "conversation_id=? ORDER BY seq", (CONVERSATION,)).fetchall()
        finally:
            conn.close()

    def states(self) -> list[str]:
        p = self.shots / "states.log"
        return p.read_text().splitlines() if p.exists() else []

    def last_state(self) -> str:
        s = self.states()
        return s[-1] if s else ""

    def wait(self, pred, timeout: float, what: str):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if (v := pred()):
                    return v
            except (sqlite3.OperationalError, OSError):
                pass
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    def pause(self, prompt: str) -> None:
        if self.args.interactive:
            input(f"\n  >> {prompt} [Enter] ")

    def check(self, name: str, fn) -> None:
        try:
            detail = fn() or ""
            ok = True
        except (AssertionError, GatewayError, subprocess.SubprocessError) as e:
            ok, detail = False, str(e)
        self.results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)

    # ------------------------------------------------------------------ steps
    def s1_launch(self) -> str:
        self.wait(lambda: any(" idle " in s or " notification " in s for s in self.states()),
                  20, "StarkMac to go live")
        self.pause("Stark floats at the bottom right of the desktop. Confirm, then continue.")
        return self.last_state()

    def s2_drag(self) -> str:
        # Only x is exact: the panel clamps y to keep a tall open chat on screen.
        self.ctl("move", "320", "260")
        self.wait(lambda: "origin=320," in self.last_state(), 5, "panel at 320,260")
        self.ctl("move", "760", "420")
        self.wait(lambda: "origin=760," in self.last_state(), 5, "panel at 760,420")
        detail = "starkctl moved 320,260 then 760,420"
        if self.args.interactive:
            before = self.last_state()
            self.pause("Drag Stark somewhere new with the mouse, release, then continue.")
            self.ctl("snapshot", str(self.shots / "after-drag.png"))
            self.wait(lambda: self.last_state() != before, 5, "a new position after the drag")
            detail += "; manual drag recorded: " + self.last_state().split("origin=")[-1]
        return detail

    def s3_send(self) -> str:
        n = len(self.rows())
        self.ctl("say", "Hello Stark, remember this.")
        self.wait(lambda: any(r[2] == "Hello Stark, remember this." for r in self.rows()), 10,
                  "the message in SQLite")
        self.wait(lambda: self.rows()[-1][1] == "assistant", 30, "Stark's reply")
        self.ctl("snapshot", str(self.shots / "chat-reply.png"))
        return f"rows {n} -> {len(self.rows())}; reply: {self.rows()[-1][2][:60]!r}"

    def s4_reconnect(self) -> str:
        before = self.rows()
        marker = len(self.states())
        self.ctl("reconnect")
        self.wait(lambda: any(" disconnected " in s for s in self.states()[marker:]), 5,
                  "the app to drop its session")
        self.wait(lambda: " disconnected " not in self.last_state()
                  and len(self.states()) > marker + 1, 10, "the app to come back")
        rows_now = self.rows()
        assert rows_now == before, "rows changed across reconnect"
        seqs = [r[0] for r in rows_now]
        assert seqs == list(range(len(seqs))), f"seqs not contiguous: {seqs}"
        app_rows = int(self.last_state().split("rows=")[1].split()[0])
        assert app_rows == len(rows_now), f"app shows {app_rows} rows, runtime has {len(rows_now)}"
        return f"{len(rows_now)} rows before and after, app mirrors {app_rows}, no duplicate"

    def s5_obsolete(self) -> str:
        with self.client() as c:
            g = c.call("snapshot")["presence"]["generation"]
            n = len(self.rows())
            try:
                c.call("send", conversation_id=CONVERSATION, client_msg_id="obsolete-1",
                       text="written under an obsolete generation", presence_gen=g - 1)
                raise AssertionError("an obsolete generation was accepted")
            except GatewayError as e:
                assert e.code == "stale_presence", e
            assert len(self.rows()) == n
        out = subprocess.run([sys.executable, str(ROOT / "stark.py"), "--db", str(self.db),
                              "jack", "ghost"], cwd=ROOT, capture_output=True, text=True,
                             timeout=30, check=True).stdout.strip()
        self.wait(lambda: " ghost " in self.last_state(), 15, "the app to render as ghost")
        self.ctl("snapshot", str(self.shots / "ghost.png"))
        self.pause("Stark is dimmed: another surface holds presence. Continue.")
        self.wait(lambda: f"presence=mac@{g + 2}" in self.last_state(), 20,
                  "presence to revert home")
        return (f"gen {g - 1} write refused stale_presence, 0 rows written; `{out}`; "
                f"app went ghost and returned at mac@{g + 2}")

    def s6_responsive(self) -> str:
        marker = len(self.states())
        self.ctl("say", "Take your time thinking about this one.")
        self.wait(lambda: any(" working " in s for s in self.states()[marker:]), 10,
                  "the working state")
        if self.args.sprites:
            self.wait(lambda: any("anim=thinking" in s for s in self.states()[marker:]), 5,
                      "the thinking animation")
        worst = 0.0
        with self.client() as c:
            for i in range(20):
                t = time.perf_counter()
                c.call("ping")
                worst = max(worst, time.perf_counter() - t)
                if i == 5:
                    self.ctl("move", "700", "380")
        assert worst < 0.1, f"gateway ping {worst * 1000:.1f} ms during inference"
        moved = self.wait(lambda: any(" working " in s and "origin=700," in s
                                      for s in self.states()[marker:]), 5,
                          "the panel to move while working")
        self.wait(lambda: self.rows()[-1][1] == "assistant", 30, "the reply")
        anim = "; thinking animation shown" if self.args.sprites else ""
        return f"worst ping {worst * 1000:.1f} ms; panel moved while working: {bool(moved)}{anim}"

    def s7_restart(self) -> str:
        with self.client() as c:
            uuid = c.welcome["identity"]["uuid"]
        n = len(self.rows())
        code = self.stop_daemon()
        self.wait(lambda: " disconnected " in self.last_state(), 10, "the app to notice")
        self.ctl("snapshot", str(self.shots / "runtime-down.png"))
        self.start_daemon()
        self.wait(lambda: " disconnected " not in self.last_state(), 30, "the app to reconnect")
        with self.client() as c:
            assert c.welcome["identity"]["uuid"] == uuid, "identity changed"
        assert len(self.rows()) == n
        return f"drain exit {code}; same identity {uuid[:8]}; {n} rows intact"

    def s8_gallery(self) -> str:
        gallery = self.shots / "gallery"
        gallery.mkdir(exist_ok=True)
        self.ctl("chat", "close")
        shown = []
        for name in ("idle", "blink", "listening", "thinking", "working", "notify",
                     "walkRight", "walkLeft", "sleep", "error"):
            self.ctl("avatar", name)
            self.wait(lambda n=name: f"anim={n} " in self.last_state(), 5, f"the {name} animation")
            time.sleep(0.4)
            self.ctl("snapshot", str(gallery / f"{name}.png"))
            shown.append(name)
        self.ctl("avatar", "follow")
        for scale in ("2", "3", "1"):
            self.ctl("scale", scale)
            self.wait(lambda v=scale: f"scale={v} " in self.last_state(), 5, f"scale {scale}")
        self.pause("Stark animates through his states. Continue.")
        return f"{len(shown)} animations rendered; scales 2, 3, 1 applied"

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        if self.dir.exists() and self.args.fresh:
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.shots.mkdir(exist_ok=True)
        (self.shots / "states.log").unlink(missing_ok=True)
        (self.dir / "github.json").write_text("{}")
        try:
            self.start_daemon()
            self.start_app()
            steps = [("1 launch: Stark floats on the desktop", self.s1_launch),
                     ("2 drag between positions", self.s2_drag),
                     ("3 message persists in GameShark", self.s3_send),
                     ("4 disconnect and reconnect keeps the conversation", self.s4_reconnect),
                     ("5 obsolete presence generation is rejected", self.s5_obsolete),
                     ("6 inference runs while the avatar stays responsive", self.s6_responsive),
                     ("7 runtime restart keeps identity", self.s7_restart)]
            if self.args.sprites:
                steps.append(("8 sprite animations and scales", self.s8_gallery))
            for name, fn in steps:
                self.check(name, fn)
        finally:
            if self.app is not None and not self.args.keep:
                self.app.terminate()
            if self.daemon is not None and not self.args.keep:
                self.stop_daemon()
        self.report()
        return 0 if all(ok for _, ok, _ in self.results) else 1

    def report(self) -> None:
        lines = ["# StarkMac acceptance", "",
                 f"{time.strftime('%Y-%m-%d %H:%M:%S')}, demo dir `{self.dir}`", "",
                 "| Step | Result | Evidence |", "|---|---|---|"]
        lines += [f"| {n} | {'PASS' if ok else 'FAIL'} | {d.replace('|', '/')} |"
                  for n, ok, d in self.results]
        lines += ["", ("Snapshots and `states.log` (one line per change the app rendered) are in "
                  "`shots/`.")]
        (self.dir / "acceptance.md").write_text("\n".join(lines) + "\n")
        print(f"\nreport: {self.dir / 'acceptance.md'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--app", type=Path, required=True)
    ap.add_argument("--ctl", type=Path, required=True)
    ap.add_argument("--dir", type=Path, default=Path.home() / ".gameshark" / "demo")
    ap.add_argument("--fake-delay", type=float, default=3.0)
    ap.add_argument("--inference-url", default=None, help="for example http://127.0.0.1:30000/v1")
    ap.add_argument("--model", default="default")
    ap.add_argument("--interactive", action="store_true", help="pause for a real drag and a look")
    ap.add_argument("--fresh", action="store_true", help="start from an empty demo database")
    ap.add_argument("--keep", action="store_true", help="leave the daemon and app running")
    ap.add_argument("--no-sprites", dest="sprites", action="store_false",
                    help="the app draws the vector avatar; skip animation checks")
    return Demo(ap.parse_args()).run()


if __name__ == "__main__":
    raise SystemExit(main())
