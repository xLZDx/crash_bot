"""Anti-miss fix: timing gate waits for the betting window to OPEN (poll) instead
of one-shot-checking then skipping. Tests _wait_for_betting_window in isolation
with injected button-finder / clock / sleeper (no Playwright)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_realbet


def _fake_clock():
    t = {"v": 0.0}
    return t, (lambda: t["v"]), (lambda s: t.__setitem__("v", t["v"] + s))


def test_window_opens_after_a_few_polls():
    calls = {"n": 0}
    def find():
        calls["n"] += 1
        return (None, "Loading" if calls["n"] < 3 else "Bet")
    _, clock, sleeper = _fake_clock()
    opened, btn = bot_realbet._wait_for_betting_window(
        find, lambda t: t == "Bet", 40.0, sleeper, clock)
    assert opened is True
    assert btn == "Bet"
    assert calls["n"] == 3  # waited until the window opened, did not skip


def test_window_open_immediately():
    opened, btn = bot_realbet._wait_for_betting_window(
        lambda: (None, "Bet"), lambda t: t == "Bet", 40.0, lambda s: None, lambda: 0.0)
    assert opened is True


def test_window_never_opens_times_out():
    _, clock, sleeper = _fake_clock()
    opened, btn = bot_realbet._wait_for_betting_window(
        lambda: (None, "Loading"), lambda t: t == "Bet", 5.0, sleeper, clock)
    assert opened is False  # times out cleanly, does not loop forever


def test_find_button_exception_is_tolerated():
    calls = {"n": 0}
    def find():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("page glitch")
        return (None, "Bet")
    _, clock, sleeper = _fake_clock()
    opened, btn = bot_realbet._wait_for_betting_window(
        find, lambda t: t == "Bet", 40.0, sleeper, clock)
    assert opened is True


def test_window_wait_timeout_constant_pinned():
    assert bot_realbet.WINDOW_WAIT_TIMEOUT_S == 40.0
