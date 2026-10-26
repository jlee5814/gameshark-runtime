"""Avatar loadout through the surface gateway: snapshot, pushes, override, reconnect,
handoff to a paired device, restart, and no side effects beyond the costume."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.gateway_client import GatewayError
from gameshark.stark_chat import CONVERSATION
from tests.gateway_runtime import Runtime, short_dir
from tests.test_memory_gateway import Recording, assistant_count, gen, jack_in, paired, wait_for


@pytest.fixture
def rec() -> Recording:
    return Recording()


@pytest.fixture
def rt(tmp_path, rec):
    r = Runtime(tmp_path / "runtime.db", short_dir(), lan=True, providers=rec).start()
    yield r
    r.close()


def send(c, mid: str, text: str, g: int | None = None) -> dict:
    return c.call("send", conversation_id=CONVERSATION, client_msg_id=mid, text=text,
                  presence_gen=gen(c) if g is None else g)


def loadout(c) -> dict:
    return c.call("snapshot")["loadout"]


def pin(c, op: str, override: str | None) -> dict:
    return c.call("loadout_set", op_id=op, override=override)


def test_snapshot_advertises_the_default_loadout(rt):
    with rt.client() as mac:
        snap = mac.call("snapshot")
        assert "loadout" in snap["features"]
        assert snap["loadout"] == {"effective": "default", "inferred": "default",
                                   "override": None, "inferred_seq": None}


def test_a_submitted_message_switches_every_surface(rt):
    with rt.client() as mac, rt.client("ghost") as watcher:
        watcher.call("snapshot")
        sub = send(mac, "m1", "I'm at the gym")
        data = watcher.wait_event("loadout", lambda d: d["effective"] == "gym")
        assert data["inferred_seq"] == sub["seq"]
        assert loadout(mac)["effective"] == "gym"
        send(mac, "m2", "I'm leaving the gym")
        watcher.wait_event("loadout", lambda d: d["effective"] == "default")


def test_negations_and_plans_leave_the_loadout_alone(rt):
    with rt.client() as mac:
        for i, text in enumerate(("I'm not at the gym", "I'll be at the gym later",
                                  "If I'm at the gym, remind me", "Am I at the gym?")):
            send(mac, f"m{i}", text)
        assert loadout(mac)["effective"] == "default"


def test_manual_override_pins_until_auto(rt):
    with rt.client() as mac:
        send(mac, "m1", "I'm at the gym")
        out = pin(mac, "o1", "default")
        assert (out["loadout"]["effective"], out["replayed"]) == ("default", False)
        send(mac, "m2", "I'm at the gym")                  # inferred updates underneath
        lo = loadout(mac)
        assert (lo["effective"], lo["inferred"], lo["override"]) == ("default", "gym",
                                                                     "default")
        assert pin(mac, "o2", None)["loadout"]["effective"] == "gym"
        assert pin(mac, "o2", None)["replayed"]


def test_override_frames_are_schema_checked(rt):
    with rt.client() as mac:
        for fields in ({"op_id": "o1", "override": "tuxedo"}, {"op_id": "o1"},
                       {"op_id": "has space", "override": "gym"},
                       {"op_id": "o1", "override": 3}):
            with pytest.raises(GatewayError) as e:
                mac.call("loadout_set", **fields)
            assert e.value.code == "invalid", fields
        pin(mac, "o1", "gym")
        with pytest.raises(GatewayError) as e:
            pin(mac, "o1", "default")
        assert e.value.code == "invalid"
        assert loadout(mac)["effective"] == "gym"


def test_an_override_starts_no_reply(rt):
    with rt.client() as mac:
        send(mac, "m1", "hello")
        wait_for(lambda: assistant_count(rt.db) == 1)
        rows = mac.call("sync", conversation_id=CONVERSATION, after_seq=-1)["rows"]
        pin(mac, "o1", "gym")
        pin(mac, "o2", None)
        assert mac.call("sync", conversation_id=CONVERSATION, after_seq=-1)["rows"] == rows
        assert assistant_count(rt.db) == 1


def test_a_reconnecting_surface_reads_the_current_loadout(rt):
    with rt.client() as mac:
        send(mac, "m1", "I'm at the gym")
    # Changed while nothing was connected: the snapshot carries it, not a missed push.
    with rt.client() as mac:
        pin(mac, "o1", "default")
    with rt.client() as mac:
        lo = loadout(mac)
        assert (lo["effective"], lo["inferred"], lo["override"]) == ("default", "gym",
                                                                     "default")


def test_handoff_keeps_the_loadout_and_the_phone_may_pin(rt):
    device_id, _surface, key = paired(rt)
    with rt.client() as mac, rt.device(device_id, key) as phone:
        send(mac, "m1", "I'm at the gym")
        to_gen = jack_in(mac, phone, "h1")
        assert loadout(phone)["effective"] == "gym"
        send(phone, "p1", "I'm leaving the gym", g=to_gen)
        assert loadout(mac)["effective"] == "default"
        # Any authenticated surface may pin, present or not: it is costume, not a write
        # to the conversation.
        pin(mac, "o1", "gym")
        assert phone.wait_event("loadout", lambda d: d["override"] == "gym")["effective"] \
            == "gym"
        jack_in(phone, mac, "h2")                            # and home again
        assert loadout(phone)["effective"] == loadout(mac)["effective"] == "gym"


def test_a_phone_reconnect_reads_the_loadout_set_while_it_was_away(rt):
    device_id, _surface, key = paired(rt)
    with rt.device(device_id, key) as phone:
        assert loadout(phone)["effective"] == "default"
    with rt.client() as mac:
        send(mac, "m1", "I'm at the gym")
    with rt.device(device_id, key) as phone:
        assert loadout(phone)["effective"] == "gym"


def test_restart_keeps_the_loadout(tmp_path, rec):
    db, sock = tmp_path / "runtime.db", short_dir()
    rt = Runtime(db, sock, lan=True, providers=rec).start()
    try:
        with rt.client() as mac:
            send(mac, "m1", "I'm at the gym")
            pin(mac, "o1", "default")
    finally:
        rt.close()
    rt = Runtime(db, sock, lan=True, providers=Recording()).start()
    try:
        with rt.client() as mac:
            lo = loadout(mac)
            assert (lo["effective"], lo["inferred"], lo["override"]) == ("default", "gym",
                                                                         "default")
    finally:
        rt.close()


def test_the_operator_cli_pins_and_clears(tmp_path, capsys):
    import stark

    db = tmp_path / "r.db"
    assert stark.main(["--db", str(db), "loadout", "gym"]) == 0
    assert "loadout:gym:override=gym" in capsys.readouterr().out
    stark.main(["--db", str(db), "loadout"])
    assert "loadout gym (override gym, inferred default)" in capsys.readouterr().out
    stark.main(["--db", str(db), "loadout", "auto"])
    stark.main(["--db", str(db), "loadout", "show"])
    assert "loadout default (override auto, inferred default)" in capsys.readouterr().out
