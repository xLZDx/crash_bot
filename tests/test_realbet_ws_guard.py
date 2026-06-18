"""Anti fake-bet guard: a WS bet only counts if the packet counter advanced.

Bug (2026-06-18): after a WS desync the send went nowhere (post packet_id stayed None)
but the bot logged 'Placed via WS' and fabricated pnl + the martingale ladder. The
guard _ws_bet_landed(pre, post) gates the 'Placed' path on the counter advancing.

Repo pytest-free convention: plain asserts; run directly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_realbet as br


def test_stuck_none_to_none_is_not_landed():
    # the exact fake-bet case observed: post_ws packet_id stayed None
    assert br._ws_bet_landed(None, None) is False


def test_first_bet_none_to_1_is_landed():
    assert br._ws_bet_landed(None, 1) is True


def test_normal_advance_is_landed():
    assert br._ws_bet_landed(4, 5) is True


def test_no_advance_is_not_landed():
    assert br._ws_bet_landed(5, 5) is False


def test_post_none_after_real_pre_is_not_landed():
    assert br._ws_bet_landed(5, None) is False


def test_wraparound_advance_is_landed():
    # packet_id is &0xFF; 255 -> 0 is a real advance
    assert br._ws_bet_landed(255, 0) is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("OK")
