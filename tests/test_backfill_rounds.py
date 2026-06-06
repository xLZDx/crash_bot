"""Tests for backfill_rounds.parse_history (pure parser of BCGame history JSON)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backfill_rounds import parse_history


def _entry(gid, rate, end_ms):
    return {"gameId": gid, "gameDetail": json.dumps(
        {"hash": "h", "salt": "s", "rate": str(rate), "endTime": end_ms,
         "beginTime": end_ms - 7000})}


def test_parses_real_shape():
    data = {"code": 0, "data": {"list": [
        _entry("9313905", "4.16", 1780745517219),
        _entry("9313904", "2.77", 1780745481228),
        _entry("9313903", "1.04", 1780745451691),
    ]}}
    out = parse_history(data)
    assert out == [("9313905", 4.16, 1780745517219),
                   ("9313904", 2.77, 1780745481228),
                   ("9313903", 1.04, 1780745451691)]


def test_skips_malformed_entry():
    data = {"data": {"list": [
        _entry("1", "2.0", 1000),
        {"gameId": "2", "gameDetail": "not json"},
        {"gameId": None, "gameDetail": "{}"},
        _entry("3", "3.0", 3000),
    ]}}
    out = parse_history(data)
    assert [g for g, _, _ in out] == ["1", "3"]


def test_rejects_out_of_range_rate():
    data = {"data": {"list": [_entry("1", "0.5", 1000), _entry("2", "5.0", 2000)]}}
    out = parse_history(data)
    assert [g for g, _, _ in out] == ["2"]   # 0.5x rejected, 5.0x kept


def test_bad_shape_returns_empty():
    assert parse_history({}) == []
    assert parse_history({"data": {}}) == []
    assert parse_history({"data": {"list": "nope"}}) == []


def test_missing_endtime_tolerated():
    data = {"data": {"list": [{"gameId": "1", "gameDetail": json.dumps({"rate": "2.0"})}]}}
    out = parse_history(data)
    assert out == [("1", 2.0, None)]
