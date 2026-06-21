"""Round-id drift diagnostic (2026-06-22, instrumentation only).

_note_our_tb_echo records the REAL round of OUR 'tb' bet echo by identity ONLY
(BET_CURRENCY + our name/uid) -- NO round/amount gating -- into _last_our_tb_echo.
It is telemetry: it must NEVER touch _last_bet_ack or any control-flow state.

These tests lock that contract so the diag can pin bot-internal-round vs
exchange-round divergence without altering bet behavior.

Repo convention: plain asserts; run directly OR via pytest.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_realbet as br


def _make_tb(round_id, currency, amount, user):
    """Build a 'tb' frame mirroring the wire format (f1 round, f2 cur, f3 amt, f5 user)."""
    enc = br._enc_varint
    body = enc((1 << 3) | 0) + enc(int(round_id))
    cb = currency.encode(); body += enc((2 << 3) | 2) + enc(len(cb)) + cb
    ab = str(amount).encode(); body += enc((3 << 3) | 2) + enc(len(ab)) + ab
    ub = user.encode(); body += enc((5 << 3) | 2) + enc(len(ub)) + ub
    return bytes([4, 2, 5]) + b"/g/cm" + bytes([2]) + b"tb" + body


def _reset():
    br._last_our_tb_echo["game_round_id"] = None
    br._last_our_tb_echo["amount"] = None
    br._last_our_tb_echo["ts"] = 0.0


def test_records_our_echo_by_name():
    _reset()
    raw = _make_tb(9354177, "USDT", "0.160000", "ivanK1157")
    assert br._note_our_tb_echo(raw, our_name="ivanK1157", our_uid=932402231331845) is True
    assert br._last_our_tb_echo["game_round_id"] == 9354177
    assert br._last_our_tb_echo["amount"] == "0.160000"
    assert br._last_our_tb_echo["ts"] > 0.0


def test_records_our_echo_by_uid_in_user_field():
    _reset()
    raw = _make_tb(9354177, "USDT", "0.16", "932402231331845")
    assert br._note_our_tb_echo(raw, our_name="ivanK1157", our_uid=932402231331845) is True
    assert br._last_our_tb_echo["game_round_id"] == 9354177


def test_records_real_round_regardless_of_any_guess():
    # The whole point: it records whatever round the echo carries (e.g. a drifted
    # round) without requiring it to equal the bet loop's +1 guess.
    _reset()
    raw = _make_tb(9999999, "USDT", "2.56", "ivanK1157")
    assert br._note_our_tb_echo(raw, our_name="ivanK1157", our_uid=932402231331845) is True
    assert br._last_our_tb_echo["game_round_id"] == 9999999
    assert br._last_our_tb_echo["amount"] == "2.56"


def test_ignores_other_player():
    _reset()
    raw = _make_tb(9354177, "USDT", "0.16", "SomeWhale")
    assert br._note_our_tb_echo(raw, our_name="ivanK1157", our_uid=932402231331845) is False
    assert br._last_our_tb_echo["game_round_id"] is None


def test_ignores_wrong_currency():
    _reset()
    raw = _make_tb(9354177, "ETH", "0.16", "ivanK1157")
    assert br._note_our_tb_echo(raw, our_name="ivanK1157", our_uid=932402231331845) is False
    assert br._last_our_tb_echo["game_round_id"] is None


def test_garbage_safe():
    _reset()
    assert br._note_our_tb_echo(b"not a frame", our_name="ivanK1157", our_uid=1) is False
    assert br._note_our_tb_echo(b"", our_name="ivanK1157", our_uid=1) is False
    assert br._last_our_tb_echo["game_round_id"] is None


def test_telemetry_only_does_not_touch_bet_ack():
    # _last_bet_ack is the REAL landing gate; the diag must never write to it.
    _reset()
    br._last_bet_ack["game_round_id"] = None
    br._last_bet_ack["amount"] = None
    br._last_bet_ack["ts"] = 0.0
    snapshot = dict(br._last_bet_ack)
    raw = _make_tb(9354177, "USDT", "0.16", "ivanK1157")
    br._note_our_tb_echo(raw, our_name="ivanK1157", our_uid=932402231331845)
    assert dict(br._last_bet_ack) == snapshot   # gate untouched


def test_no_identity_no_record():
    # If our identity is unknown, nothing is recorded (cannot prove ownership).
    _reset()
    raw = _make_tb(9354177, "USDT", "0.16", "ivanK1157")
    assert br._note_our_tb_echo(raw, our_name=None, our_uid=None) is False
    assert br._last_our_tb_echo["game_round_id"] is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("OK")
