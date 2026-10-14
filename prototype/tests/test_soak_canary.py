"""Deliberate failure for the live actionable event soak. Never merge."""


def test_canary():
    assert False, "deliberate soak failure"
