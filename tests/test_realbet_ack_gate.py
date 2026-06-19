"""Ack-gate state (Commit 1): _note_tb_ack records OUR landing ack from an
incoming 'tb' frame; _bet_ack_fresh reports whether a fresh ack exists for a
pending (round, amount). Pure logic -- the bet loop wires these in a LATER
commit. Repo convention: plain asserts; run directly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_realbet as br


def _make_tb(round_id, currency, amount, user):
    enc = br._enc_varint
    body = enc((1 << 3) | 0) + enc(int(round_id))
    cb = currency.encode(); body += enc((2 << 3) | 2) + enc(len(cb)) + cb
    ab = str(amount).encode(); body += enc((3 << 3) | 2) + enc(len(ab)) + ab
    ub = user.encode(); body += enc((5 << 3) | 2) + enc(len(ub)) + ub
    return bytes([4, 2, 5]) + b"/g/cm" + bytes([2]) + b"tb" + body


def _reset():
    br._last_bet_ack["game_round_id"] = None
    br._last_bet_ack["amount"] = None
    br._last_bet_ack["ts"] = 0.0


def test_note_ack_records_our_bet():
    _reset()
    ok = br._note_tb_ack(_make_tb(9354177, "USDT", "0.160000", "ivanK1157"),
                         round_id=9354177, amount="0.16",
                         our_name="ivanK1157", our_uid=932402231331845)
    assert ok is True
    assert br._last_bet_ack["game_round_id"] == 9354177


def test_note_ack_ignores_other_player():
    _reset()
    ok = br._note_tb_ack(_make_tb(9354177, "USDT", "0.16", "Whale"),
                         round_id=9354177, amount="0.16",
                         our_name="ivanK1157", our_uid=932402231331845)
    assert ok is False
    assert br._last_bet_ack["game_round_id"] is None


def test_note_ack_ignores_non_tb_frame():
    _reset()
    assert br._note_tb_ack(b"garbage", round_id=9354177, amount="0.16",
                           our_name="ivanK1157") is False


def test_ack_fresh_true_within_window():
    _reset()
    br._note_tb_ack(_make_tb(9354200, "USDT", "0.04", "ivanK1157"),
                    round_id=9354200, amount="0.04", our_name="ivanK1157")
    t = br._last_bet_ack["ts"]
    assert br._bet_ack_fresh(9354200, "0.04", now=t + 1.0, max_age_s=2.0) is True


def test_ack_fresh_false_when_stale():
    _reset()
    br._note_tb_ack(_make_tb(9354200, "USDT", "0.04", "ivanK1157"),
                    round_id=9354200, amount="0.04", our_name="ivanK1157")
    t = br._last_bet_ack["ts"]
    assert br._bet_ack_fresh(9354200, "0.04", now=t + 5.0, max_age_s=2.0) is False


def test_ack_fresh_false_wrong_round():
    _reset()
    br._note_tb_ack(_make_tb(9354200, "USDT", "0.04", "ivanK1157"),
                    round_id=9354200, amount="0.04", our_name="ivanK1157")
    t = br._last_bet_ack["ts"]
    assert br._bet_ack_fresh(9354201, "0.04", now=t) is False


def test_ack_fresh_false_no_ack():
    _reset()
    assert br._bet_ack_fresh(9354200, "0.04") is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("OK")
