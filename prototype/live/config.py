"""Live targets from the environment, and a probe that refuses cleanly.

A local target must be a loopback IP literal; a cloud target must use HTTPS and needs all
of its variables plus an explicit opt in. Nothing here starts or stops a model server.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from gameshark.inference.openai_compat import Endpoint, default_connect
from gameshark.inference.types import Locality

EXIT_UNAVAILABLE = 3

LOCAL_VARS = ("GAMESHARK_LIVE_LOCAL_URL", "GAMESHARK_LIVE_LOCAL_MODEL")
CLOUD_VARS = ("GAMESHARK_CLOUD_URL", "GAMESHARK_CLOUD_MODEL", "GAMESHARK_CLOUD_API_KEY")
OPT_IN = "GAMESHARK_CLOUD_OPT_IN"


class LiveUnavailable(Exception):
    """The live target is missing, misconfigured, or not answering. Nothing ran."""


@dataclass(frozen=True)
class Target:
    kind: str  # local | cloud | fake_cloud
    url: str
    model: str
    context: int
    slots: int
    price_in: int = 0
    price_out: int = 0
    key: str | None = field(default=None, repr=False)

    def daemon_env(self) -> dict[str, str]:
        """Variables for one daemon subprocess. The key travels only here."""
        env = {"LIVE_PROVIDER": self.kind, "LIVE_URL": self.url, "LIVE_MODEL": self.model,
               "LIVE_CONTEXT": str(self.context), "LIVE_SLOTS": str(self.slots),
               "LIVE_PRICE_IN": str(self.price_in), "LIVE_PRICE_OUT": str(self.price_out)}
        if self.key:
            env["LIVE_API_KEY"] = self.key
        return env

    def describe(self) -> dict:
        """Safe to record: no key, and no URL path or host beyond what locality needs."""
        d = {"kind": self.kind, "model": self.model, "context": self.context,
             "slots": self.slots, "price_in_micros": self.price_in,
             "price_out_micros": self.price_out}
        if self.kind == "local":
            d["url"] = self.url
        return d


def local_from_env(env: Mapping[str, str]) -> Target:
    missing = [v for v in LOCAL_VARS if not env.get(v)]
    if missing:
        raise LiveUnavailable(f"set {', '.join(missing)}")
    url = env["GAMESHARK_LIVE_LOCAL_URL"]
    try:
        ep = Endpoint.parse(url)
    except ValueError as exc:
        raise LiveUnavailable(f"local URL refused: {exc}") from None
    if ep.locality is not Locality.LOCAL:
        raise LiveUnavailable("local URL must be a loopback IP literal")
    return Target("local", url, env["GAMESHARK_LIVE_LOCAL_MODEL"],
                  int(env.get("GAMESHARK_LIVE_LOCAL_CONTEXT", "40960")),
                  int(env.get("GAMESHARK_LIVE_LOCAL_SLOTS", "4")))


def cloud_from_env(env: Mapping[str, str]) -> Target | str:
    """A cloud target, or the reason cloud scenarios do not run."""
    if env.get(OPT_IN) != "1":
        return f"{OPT_IN}=1 not set"
    missing = [v for v in CLOUD_VARS if not env.get(v)]
    if missing:
        return f"missing {', '.join(missing)}"
    url = env["GAMESHARK_CLOUD_URL"]
    try:
        ep = Endpoint.parse(url)
    except ValueError as exc:
        return f"cloud URL refused: {exc}"
    if ep.locality is not Locality.CLOUD or ep.scheme != "https":
        return "cloud URL must be a non loopback HTTPS endpoint"
    return Target("cloud", url, env["GAMESHARK_CLOUD_MODEL"],
                  int(env.get("GAMESHARK_CLOUD_CONTEXT", "8192")),
                  int(env.get("GAMESHARK_CLOUD_SLOTS", "2")),
                  int(env.get("GAMESHARK_CLOUD_PRICE_IN_MICROS", "1")),
                  int(env.get("GAMESHARK_CLOUD_PRICE_OUT_MICROS", "1")),
                  env["GAMESHARK_CLOUD_API_KEY"])


def fake_cloud() -> Target:
    """A FakeProvider with cloud locality and a price: policy checks with no network."""
    return Target("fake_cloud", "fake://cloud", "fake-cloud-model", 4096, 2, 1, 1)


def probe(target: Target, timeout_s: float = 3.0) -> dict:
    """GET {base}/models and require the target model. Raise LiveUnavailable otherwise.

    Returns what the server advertises for the model; never the key, never a body beyond
    the model entry.
    """
    ep = Endpoint.parse(target.url)
    headers = {"Accept": "application/json"}
    if target.key:
        headers["Authorization"] = f"Bearer {target.key}"
    conn = None
    try:
        conn = default_connect(ep, timeout_s)
        conn.request("GET", f"{ep.path}/models", headers=headers)
        resp = conn.getresponse()
        raw = resp.read(1 << 20)
        status = resp.status
    except OSError as exc:
        raise LiveUnavailable(f"{target.kind} endpoint not answering: "
                              f"{type(exc).__name__}") from None
    finally:
        if conn is not None:
            conn.close()
    if status != 200:
        raise LiveUnavailable(f"{target.kind} endpoint answered HTTP {status} to /models")
    try:
        data = json.loads(raw)["data"]
        entry = next((m for m in data if m.get("id") == target.model), None)
    except (ValueError, KeyError, TypeError):
        raise LiveUnavailable(f"{target.kind} endpoint sent an unreadable model list") from None
    if entry is None:
        raise LiveUnavailable(f"model {target.model} is not listed by the {target.kind} endpoint")
    return {"model": entry["id"], "max_model_len": entry.get("max_model_len"),
            "owned_by": entry.get("owned_by")}
