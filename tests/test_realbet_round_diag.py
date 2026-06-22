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


# ---------------------------------------------------------------------------
# Fix helpers (2026-06-22): anchor landing + outcome to the tb echo's REAL round.
# ---------------------------------------------------------------------------

def test_landed_round_from_fresh_echo():
    echo = {"game_round_id": 9362234, "amount": "0.01", "ts": 1.0}  # ts>0 = fresh
    assert br._landed_round_from_echo(echo) == 9362234


def test_landed_round_from_drifted_echo_returns_real_round():
    # The bug case: bot guessed +1 high; echo carries the REAL (lower) round.
    echo = {"game_round_id": 9362234, "amount": "0.02", "ts": 12345.0}
    assert br._landed_round_from_echo(echo) == 9362234   # not the guess


def test_landed_round_none_when_stale():
    echo = {"game_round_id": 9362234, "amount": "0.01", "ts": 0.0}  # reset before send
    assert br._landed_round_from_echo(echo) is None


def test_landed_round_none_when_no_round():
    assert br._landed_round_from_echo({"game_round_id": None, "amount": None, "ts": 5.0}) is None
    assert br._landed_round_from_echo(None) is None
    assert br._landed_round_from_echo({}) is None


def test_landed_round_garbage_safe():
    assert br._landed_round_from_echo({"game_round_id": "notanint", "ts": 5.0}) is None
    assert br._landed_round_from_echo({"game_round_id": 9362234, "ts": "bad"}) is None


def test_result_from_mult_win_loss_boundary():
    assert br._result_from_mult(2.1, 2.1) == "win"     # exactly cashout = win
    assert br._result_from_mult(5.15, 2.1) == "win"
    assert br._result_from_mult(2.07, 2.1) == "loss"   # the real drift example (9362234)
    assert br._result_from_mult(1.0, 2.1) == "loss"


def test_result_from_mult_string_and_unknown():
    assert br._result_from_mult("5.15", 2.1) == "win"
    assert br._result_from_mult("2.00", 2.1) == "loss"
    assert br._result_from_mult(None, 2.1) is None      # unknown -> caller keeps prior
    assert br._result_from_mult("garbage", 2.1) is None


# ---------------------------------------------------------------------------
# Iteration 2 (2026-06-22): authoritative win/loss from REAL balance delta.
# ---------------------------------------------------------------------------

def test_balance_delta_win():
    # base $0.01 win: +bet*1.1 -> balance up
    assert br._result_from_balance_delta(10.0, 10.011, 0.01) == "win"
    # cap $5.12 win
    assert br._result_from_balance_delta(38.0, 43.63, 5.12) == "win"


def test_balance_delta_loss():
    assert br._result_from_balance_delta(10.0, 9.99, 0.01) == "loss"      # -bet
    assert br._result_from_balance_delta(38.0, 32.88, 5.12) == "loss"     # -bet at cap


def test_balance_delta_inconclusive_small_move():
    # move smaller than bet*0.5 -> None (caller keeps prior result, no phantom)
    assert br._result_from_balance_delta(10.0, 10.002, 0.01) is None      # +0.002 < 0.005
    assert br._result_from_balance_delta(10.0, 10.0, 0.01) is None        # flat
    assert br._result_from_balance_delta(10.0, 9.998, 0.01) is None       # -0.002 > -0.005


def test_balance_delta_missing_or_garbage():
    assert br._result_from_balance_delta(None, 10.0, 0.01) is None
    assert br._result_from_balance_delta(10.0, None, 0.01) is None
    assert br._result_from_balance_delta(10.0, 10.0, None) is None
    assert br._result_from_balance_delta(10.0, 10.0, 0) is None           # zero bet -> no thr
    assert br._result_from_balance_delta("x", 10.0, 0.01) is None


def test_balance_delta_overrides_phantom_win():
    # The exact bug: round-mult said 'win' but the REAL balance fell -> it's a loss.
    # _result_from_balance_delta on the real balances returns 'loss', which the caller
    # uses to override the phantom mult result.
    mult_says = "win"
    real = br._result_from_balance_delta(35.93, 35.92, 0.01)   # balance went DOWN
    assert real == "loss"
    assert real != mult_says


# ---------------------------------------------------------------------------
# Iteration 3 (2026-06-22): demote FALSE recent-bet landings (no balance move).
# ---------------------------------------------------------------------------

def test_demote_recent_bet_no_movement():
    # The exact residual bug: recent-bet fallback 'landed' but balance flat -> demote.
    assert br._should_demote_landing("recent_bet", None) is True


def test_no_demote_when_balance_moved():
    assert br._should_demote_landing("recent_bet", "win") is False
    assert br._should_demote_landing("recent_bet", "loss") is False


def test_no_demote_tb_echo_even_if_inconclusive():
    # tb_echo is the exchange's own accept broadcast -> trusted even on read-timing None.
    assert br._should_demote_landing("tb_echo", None) is False
    assert br._should_demote_landing("tb_echo", "win") is False


def test_no_demote_empty_confirm():
    assert br._should_demote_landing("", None) is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("OK")
