"""Tests for gap_monitor.evaluate (stall w/ dedup + accumulating timer, new-gap,
collector-reset detection)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gap_monitor import evaluate


def _cur(max_gid, missing):
    return {"max_gid": max_gid, "missing": missing}


def test_first_run_no_alert():
    a, s = evaluate(None, _cur(100, 0), now_ts=1000.0)
    assert a == []
    assert s["max_gid"] == 100 and s["ts"] == 1000.0
    assert s["stall_alerted_for_gid"] is None


def test_healthy_progress_resets_timer():
    prev = {"max_gid": 100, "missing": 2, "ts": 1000.0}
    a, s = evaluate(prev, _cur(110, 2), now_ts=1100.0)
    assert a == [] and s["ts"] == 1100.0   # advanced -> timer reset


def test_stall_alerts_once_and_timer_holds():
    prev = {"max_gid": 100, "missing": 0, "ts": 1000.0}
    a, s = evaluate(prev, _cur(100, 0), now_ts=1200.0)   # stuck 200s >= 180
    assert any("collector_stalled" in x for x in a)
    assert s["stall_alerted_for_gid"] == 100
    assert s["ts"] == 1000.0                              # NOT reset while stuck
    # next tick still stuck -> no re-alert (dedup)
    a2, s2 = evaluate(s, _cur(100, 0), now_ts=1400.0)
    assert a2 == [] and s2["ts"] == 1000.0


def test_stall_not_triggered_within_window():
    prev = {"max_gid": 100, "missing": 0, "ts": 1000.0}
    a, _ = evaluate(prev, _cur(100, 0), now_ts=1100.0)    # 100s < 180
    assert a == []


def test_stall_clears_after_advance():
    stalled = {"max_gid": 100, "missing": 0, "ts": 1000.0, "stall_alerted_for_gid": 100}
    a, s = evaluate(stalled, _cur(105, 0), now_ts=1500.0)
    assert a == [] and s["stall_alerted_for_gid"] is None and s["ts"] == 1500.0


def test_new_gap_alerts():
    prev = {"max_gid": 100, "missing": 0, "ts": 1000.0}
    a, _ = evaluate(prev, _cur(120, 10), now_ts=1100.0)
    assert any("new_gap" in x for x in a)


def test_small_drift_no_alert():
    prev = {"max_gid": 100, "missing": 0, "ts": 1000.0}
    a, _ = evaluate(prev, _cur(110, 2), now_ts=1100.0)    # +2 < 5
    assert a == []


def test_collector_db_reset_alerts():
    prev = {"max_gid": 9_999_000, "missing": 1200, "ts": 1000.0}
    a, _ = evaluate(prev, _cur(50, 0), now_ts=1100.0)     # max went backwards
    assert any("collector_db_reset" in x for x in a)
