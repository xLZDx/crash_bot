"""Regression test for _tail_lines (the anti-miss latency fix).

Root cause of ~20% technically-missed real bets: _scale_from_audit read the WHOLE
audit file (110MB+) on every bet via read_text().splitlines(), adding hundreds of
ms of pre-send latency that pushed the WS packet past the betting window ('game has
started' / suspended / place_error). Fix: _tail_lines reads only the file TAIL.

Runs under the repo's pytest-free convention (plain asserts; invoke directly).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_realbet as br


def test_tail_returns_last_n_complete_lines():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".jsonl") as f:
        for i in range(2000):
            f.write("line%d\n" % i)
        p = f.name
    try:
        t = br._tail_lines(p, max_lines=500)
        assert len(t) == 500, len(t)
        assert t[-1] == "line1999", t[-1]
        assert t[0] == "line1500", t[0]
    finally:
        os.unlink(p)


def test_tail_drops_partial_first_line_on_midfile_seek():
    # file bigger than tail_bytes -> seek lands mid-line -> first partial line dropped
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".jsonl") as f:
        for i in range(5000):
            f.write("X" * 200 + "_%d\n" % i)  # ~204 bytes/line -> ~1MB file
        p = f.name
    try:
        t = br._tail_lines(p, max_lines=50, tail_bytes=4096)  # tail << file
        assert 0 < len(t) <= 50
        # every returned line is complete (ends with its index marker, no partial head)
        assert all(ln.startswith("X" * 200 + "_") for ln in t), t[0][:20]
        assert t[-1].endswith("_4999"), t[-1][-10:]
    finally:
        os.unlink(p)


def test_tail_missing_file_returns_empty():
    assert br._tail_lines("/nonexistent/path/zzz.jsonl") == []


def test_tail_small_file_returns_all():
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".jsonl") as f:
        f.write("a\nb\nc\n")
        p = f.name
    try:
        t = br._tail_lines(p, max_lines=500)
        assert t == ["a", "b", "c"], t
    finally:
        os.unlink(p)


def test_scale_from_audit_returns_bounded_scale(tmp_path=None):
    # _scale_from_audit must always return a scale in [1.0, MAX_SCALE]
    sc = br._scale_from_audit()
    assert 1.0 <= sc <= br.MAX_SCALE, sc


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("OK")
