"""Tests for live_feed.make_events (pure round_end event builder)."""
import os
import sys
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live_feed import make_events


def test_builds_round_end_events():
    ring = deque(maxlen=220)
    rows = [(10, "100", 2.5, "t1"), (11, "101", 1.2, "t2")]
    events, seq, last_id = make_events(rows, ring, seq=0, epoch=99)
    assert len(events) == 2
    e0 = events[0]
    assert e0["type"] == "round_end" and e0["seq"] == 1 and e0["epoch"] == 99
    assert e0["game_round_id"] == "100" and e0["multiplier"] == 2.5
    assert events[1]["seq"] == 2 and events[1]["recent_mults"] == [2.5, 1.2]
    assert seq == 2 and last_id == 11


def test_skips_null_mult_but_advances_id():
    ring = deque(maxlen=220)
    rows = [(5, "50", None, "t"), (6, "51", 3.0, "t")]
    events, seq, last_id = make_events(rows, ring, 0, 1)
    assert len(events) == 1 and events[0]["game_round_id"] == "51"
    assert last_id == 6 and seq == 1   # advanced past the null row


def test_ring_maxlen_caps_recent_mults():
    ring = deque(maxlen=3)
    rows = [(i, str(i), float(i), "t") for i in range(1, 6)]
    events, _, _ = make_events(rows, ring, 0, 1)
    assert events[-1]["recent_mults"] == [3.0, 4.0, 5.0]


def test_empty_rows():
    ring = deque(maxlen=220)
    events, seq, last_id = make_events([], ring, 7, 1)
    assert events == [] and seq == 7 and last_id is None
