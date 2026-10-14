"""File backed GitHub transport for daemon tests.

The test edits a JSON file between daemon runs; every snapshot rereads it, so a real
`stark run` subprocess sees new activity exactly as it would from GitHub. Loaded through
GAMESHARK_TRANSPORT=tests.file_transport:make with GAMESHARK_FIXTURE pointing at the file.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from gameshark.github_client import AuthRevoked, Snapshot


class FileClient:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf8"))

    def viewer(self) -> str:
        return self._load().get("viewer", "stark-owner")

    def snapshot(self, repo, pr, etags, head_sha, known=None, cursors=None) -> Snapshot:
        data = self._load()
        if data.get("snapshot_delay_s"):
            time.sleep(data["snapshot_delay_s"])    # a non cooperative stall, like a hung GET
        if data.get("revoked"):
            raise AuthRevoked(401, "Bad credentials")
        return Snapshot(
            pr={"number": pr, "state": data.get("state", "open"),
                "merged": data.get("merged", False), "head": {"sha": "sha1"}},
            reviews=data.get("reviews", []),
            issue_comments=data.get("issue_comments", []),
            check_runs=data.get("check_runs", []),
            head_sha="sha1",
        )


def make() -> FileClient:
    return FileClient(os.environ["GAMESHARK_FIXTURE"])


def write(path: Path, **data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf8")
    tmp.replace(path)                       # atomic: the daemon never reads half a file


def review(i: int, state: str, author: str = "reviewer", ago_s: float = 0.0) -> dict:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago_s))
    return {"id": i, "state": state.upper(), "user": {"login": author, "type": "User"},
            "body": "", "submitted_at": ts}


def check_run(i: int, conclusion: str, name: str = "test") -> dict:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time()))
    return {"id": i, "name": name, "status": "completed", "conclusion": conclusion,
            "completed_at": ts, "html_url": f"https://github.test/ci/{i}"}
