"""Avatar loadout: narrow detection, inference with the row, override precedence, restart."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gameshark.conversation import SurfaceConversation
from gameshark.errors import FencedError, StalePresence
from gameshark.inference.journal import InferenceJournal
from gameshark.lease import LeaseManager
from gameshark.loadout import DEFAULT, GYM, LoadoutStore, detect
from gameshark.presence import PresenceManager
from gameshark.store import connect
from tests.helpers import AGENT, HOST

CONV = "stark:main"

ENTER = [
    "I'm at the gym",
    "I'm at the gym.",
    "i'm at the gym now",
    "Im at the gym",
    "I am at the gym",
    "I am in the gym",
    "I’m at the gym",                      # curly apostrophe
    "I'm here at the gym",
    "I just got to the gym",
    "I finally arrived at the gym!",
    "Hey Stark, I'm at the gym",
    "Leg day. I'm at the gym",
    "I'm at the gym but my legs are sore",
    "I'm at the gym after work",
]
EXIT = [
    "I'm leaving the gym",
    "I'm leaving the gym now",
    "I am leaving the gym",
    "I’m leaving the gym",
    "I just left the gym",
    "I left the gym",
    "Just left the gym",
    "Leaving the gym",
    "I'm done at the gym",
    "I'm out of the gym",
    "I'm back from the gym",
    "I'm heading home from the gym",
]
NEVER = [
    # negation
    "I'm not at the gym",
    "I'm not at the gym yet",
    "I am not at the gym",
    "I'm never at the gym",
    "I'm no longer at the gym",
    "I'm not leaving the gym",
    "I haven't left the gym",
    "I didn't get to the gym",
    # hypothetical
    "If I'm at the gym, remind me to stretch",
    "When I'm at the gym, play something loud",
    "Imagine I'm at the gym",
    "Pretend I'm at the gym",
    "Maybe I'm at the gym",
    "I wish I'm at the gym",
    "Suppose I left the gym",
    # future or plans
    "I'll be at the gym",
    "I will be at the gym later",
    "I'm going to the gym",
    "I'm heading to the gym",
    "I'm going to be at the gym",
    "I'm gonna be at the gym",
    "I'm about to be at the gym",
    "I'm at the gym tomorrow",
    "I'm at the gym tonight",
    "I'm leaving the gym in 10 minutes",
    "I'm leaving the gym soon",
    "I plan to be at the gym",
    # habitual
    "I'm usually at the gym on Mondays",
    "I'm always at the gym",
    # questions
    "Am I at the gym?",
    "I'm at the gym?",
    "Should I go to the gym?",
    "Did I leave the gym?",
    # someone else, or quoted
    "He's at the gym",
    "She left the gym",
    "My brother is at the gym",
    'He texted "I\'m at the gym"',
    "Type `I'm at the gym` to test",
    # unrelated words
    "Gymshark has a sale",
    "I'm at gymnastics",
    "I'm working out",
    "I'm lifting",
    "I'm training a new model",
    "the gym is closed",
    "",
]


@pytest.mark.parametrize("text", ENTER)
def test_explicit_presence_at_the_gym_is_gym(text):
    assert detect(text) == GYM


@pytest.mark.parametrize("text", EXIT)
def test_explicit_leaving_is_default(text):
    assert detect(text) == DEFAULT


@pytest.mark.parametrize("text", NEVER)
def test_negations_hypotheticals_plans_and_others_never_count(text):
    assert detect(text) is None


def test_the_last_qualifying_clause_wins():
    assert detect("I'm at the gym. Actually I'm leaving the gym") == DEFAULT
    assert detect("I left the gym, now I'm at the gym again") == GYM
    assert detect("I'm at the gym. I'll leave the gym later") == GYM
    assert detect("I'm not at the gym, but I'm leaving the gym") == DEFAULT


# ---------------------------------------------------------------------- with the row
class Stack:
    def __init__(self, db: Path, clock) -> None:
        self.conn = connect(db)
        self.lease = LeaseManager(self.conn, AGENT, clock)
        gen = self.lease.acquire(HOST, 30.0)
        pm = PresenceManager(self.conn, AGENT, self.lease, gen, clock)
        pm.ensure_home("mac")
        InferenceJournal(self.conn, AGENT, self.lease, gen, clock).open_conversation(CONV)
        self.sc = SurfaceConversation(self.conn, AGENT, self.lease, gen, pm, clock)
        self.lo = LoadoutStore(self.conn, AGENT, self.lease, gen, clock)
        self.n = 0

    def say(self, text: str, surface: str = "mac", gen: int = 1):
        self.n += 1
        return self.sc.submit(surface, gen, f"m{self.n}", CONV, text)

    def effective(self) -> str:
        return self.lo.current().effective


@pytest.fixture
def st(paths, clock) -> Stack:
    return Stack(paths["runtime"], clock)


def test_a_fresh_identity_wears_the_default(st):
    lo = st.lo.current()
    assert (lo.effective, lo.inferred, lo.override, lo.inferred_seq) == (
        DEFAULT, DEFAULT, None, None)


def test_gym_after_submit_and_default_after_leaving(st):
    sub = st.say("I'm at the gym")
    lo = st.lo.current()
    assert (lo.effective, lo.inferred, lo.inferred_seq) == (GYM, GYM, sub.seq)
    st.say("how many sets should I do")
    assert st.effective() == GYM                    # unrelated talk keeps the context
    st.say("I'm leaving the gym")
    assert st.effective() == DEFAULT


def test_negated_and_future_messages_change_nothing(st):
    st.say("I'm at the gym")
    for text in ("I'm not leaving the gym yet", "I'll leave the gym soon",
                 "If I left the gym would you notice?"):
        st.say(text)
        assert st.effective() == GYM


def test_assistant_rows_never_infer(st):
    st.conn.execute(
        "INSERT INTO messages(conversation_id, seq, role, content, call_id, generation, "
        "created) VALUES(?,?,?,?,NULL,1,0)", (CONV, 0, "assistant", "I'm at the gym"))
    st.conn.execute("UPDATE conversations SET next_seq=1 WHERE id=?", (CONV,))
    st.say("hello")
    assert st.effective() == DEFAULT


def test_a_resend_never_reapplies_its_context(st):
    st.sc.submit("mac", 1, "gym", CONV, "I'm at the gym")
    st.say("I'm leaving the gym")
    again = st.sc.submit("mac", 1, "gym", CONV, "I'm at the gym")
    assert again.replayed
    assert st.effective() == DEFAULT


def test_a_refused_send_infers_nothing(st):
    with pytest.raises(StalePresence):
        st.say("I'm at the gym", surface="phone")     # stale presence rolls back the row
    assert st.effective() == DEFAULT


# ---------------------------------------------------------------------- override
def test_an_override_pins_over_inferred_context(st):
    st.lo.set_override("mac", "o1", GYM)
    st.say("I'm leaving the gym")
    lo = st.lo.current()
    assert (lo.effective, lo.inferred, lo.override) == (GYM, DEFAULT, GYM)
    st.say("I'm at the gym")
    st.lo.set_override("pocket-1", "o2", DEFAULT)
    lo = st.lo.current()
    assert (lo.effective, lo.inferred, lo.override) == (DEFAULT, GYM, DEFAULT)


def test_auto_returns_to_inferred_context(st):
    st.say("I'm at the gym")
    st.lo.set_override("mac", "o1", DEFAULT)
    assert st.effective() == DEFAULT
    lo = st.lo.set_override("mac", "o2", None)
    assert (lo.effective, lo.override) == (GYM, None)


def test_override_ops_land_once(st):
    a = st.lo.set_override("mac", "o1", GYM)
    st.lo.set_override("mac", "o2", None)
    b = st.lo.set_override("mac", "o1", GYM)      # a late resend of the first op
    assert (a.replayed, b.replayed) == (False, True)
    assert st.lo.current().override is None       # it does not undo the later clear
    with pytest.raises(ValueError):
        st.lo.set_override("mac", "o1", DEFAULT)
    with pytest.raises(ValueError):
        st.lo.set_override("mac", "o3", "tuxedo")


def test_a_stale_owner_cannot_override(paths, clock, st):
    clock.advance(31.0)
    LeaseManager(connect(paths["runtime"]), AGENT, clock).acquire("other-host", 30.0)
    with pytest.raises(FencedError):
        st.lo.set_override("mac", "o1", GYM)
    with pytest.raises(FencedError):
        st.say("I'm at the gym")
    assert st.lo.current().override is None
    assert st.effective() == DEFAULT


def test_restart_keeps_both_inputs(paths, clock, st):
    st.say("I'm at the gym")
    st.lo.set_override("mac", "o1", DEFAULT)
    st.conn.close()
    lo = Stack(paths["runtime"], clock).lo.current()
    assert (lo.effective, lo.inferred, lo.override) == (DEFAULT, GYM, DEFAULT)
