"""Positive bet-landing ack (Commit 1): parse + match the server's 'tb' bet
broadcast to confirm OUR OWN accepted bet (vs a phantom send into a closed
betting window).

Root cause (2026-06-19): the bot settled WIN/LOSS from the round multiplier and
logged 'Placed via WS' on packet-sent + no-explicit-reject, so ~97% of logged
bets were phantom (silently ignored by the exchange). These helpers give a real
ack signal. Pure logic; the live loop is untouched in this commit.

Repo convention: plain asserts; run directly.
"""
import os
import sys
import json
import base64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_realbet as br

# A REAL captured 'tb' frame from data/realbet_network_capture.jsonl (another
# player: round 9354376, NGNFIAT, amount '3869', user 'ForwarDBet').
REAL_TB_B64 = "BAIFL2cvY20CdGIIiPm6BBIHTkdORklBVBoEMzg2OSIHCK/FoxYQASoKRm9yd2FyREJldDD0yJCfvNOF9xlCATA="

# A REAL /api/account/get/ response (ours), trimmed.
ACCOUNT_GET = (
    '{"code":0,"msg":null,"data":{"userId":932402231331845,"name":"ivanK1157",'
    '"uniqueUid":932402231331845,"uid":932402231331845,"vipLevel":0}}'
)


def _make_tb(round_id, currency, amount, user):
    """Build a 'tb' frame mirroring the wire format (f1 round, f2 cur, f3 amt, f5 user)."""
    enc = br._enc_varint
    body = enc((1 << 3) | 0) + enc(int(round_id))
    cb = currency.encode(); body += enc((2 << 3) | 2) + enc(len(cb)) + cb
    ab = str(amount).encode(); body += enc((3 << 3) | 2) + enc(len(ab)) + ab
    ub = user.encode(); body += enc((5 << 3) | 2) + enc(len(ub)) + ub
    return bytes([4, 2, 5]) + b"/g/cm" + bytes([2]) + b"tb" + body


def test_parse_real_captured_tb_frame():
    tb = br._parse_tb_frame(base64.b64decode(REAL_TB_B64))
    assert tb is not None
    assert tb["game_round_id"] == 9354376
    assert tb["currency"] == "NGNFIAT"
    assert tb["amount"] == "3869"
    assert tb["user"] == "ForwarDBet"


def test_parse_non_tb_returns_none():
    assert br._parse_tb_frame(b"not a frame at all") is None
    # an outgoing twice-bet frame is NOT a 'tb' broadcast
    out = bytes([4, 0x82, 0, 0, 0, 7]) + bytes([5]) + b"/g/cm" + bytes([9]) + b"twice-bet"
    assert br._parse_tb_frame(out) is None


def test_roundtrip_our_bet_parses():
    tb = br._parse_tb_frame(_make_tb(9354177, "USDT", "0.160000", "ivanK1157"))
    assert tb == {"game_round_id": 9354177, "currency": "USDT",
                  "amount": "0.160000", "user": "ivanK1157"}


def test_match_our_bet_true():
    tb = br._parse_tb_frame(_make_tb(9354177, "USDT", "0.160000", "ivanK1157"))
    assert br._tb_is_our_bet(tb, round_id=9354177, amount="0.160000",
                             our_name="ivanK1157", our_uid=932402231331845) is True


def test_match_uid_when_user_field_is_uid():
    tb = br._parse_tb_frame(_make_tb(9354177, "USDT", "0.16", "932402231331845"))
    assert br._tb_is_our_bet(tb, round_id=9354177, amount="0.160000",
                             our_name="ivanK1157", our_uid=932402231331845) is True


def test_no_match_wrong_round():
    tb = br._parse_tb_frame(_make_tb(9354177, "USDT", "0.16", "ivanK1157"))
    assert br._tb_is_our_bet(tb, round_id=9354178, amount="0.16",
                             our_name="ivanK1157", our_uid=None) is False


def test_no_match_wrong_amount():
    tb = br._parse_tb_frame(_make_tb(9354177, "USDT", "0.32", "ivanK1157"))
    assert br._tb_is_our_bet(tb, round_id=9354177, amount="0.16",
                             our_name="ivanK1157", our_uid=None) is False


def test_no_match_wrong_currency():
    tb = br._parse_tb_frame(_make_tb(9354177, "ETH", "0.16", "ivanK1157"))
    assert br._tb_is_our_bet(tb, round_id=9354177, amount="0.16",
                             our_name="ivanK1157", our_uid=None) is False


def test_no_match_other_player_same_round_amount():
    # another USDT player betting our amount on our round must NOT match us
    tb = br._parse_tb_frame(_make_tb(9354177, "USDT", "0.16", "SomeWhale"))
    assert br._tb_is_our_bet(tb, round_id=9354177, amount="0.16",
                             our_name="ivanK1157", our_uid=932402231331845) is False


def test_amounts_equal_string_float_variants():
    assert br._amounts_equal("0.160000", "0.16") is True
    assert br._amounts_equal("0.16", 0.16) is True
    assert br._amounts_equal("0.16", "0.17") is False
    assert br._amounts_equal(None, "0.16") is False
    assert br._amounts_equal("nan", "0.16") is False


def test_identity_from_account_get():
    assert br._identity_from_account_get(ACCOUNT_GET) == {"uid": 932402231331845, "name": "ivanK1157"}
    assert br._identity_from_account_get(json.loads(ACCOUNT_GET))["name"] == "ivanK1157"
    assert br._identity_from_account_get("not json") == {"uid": None, "name": None}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("OK")
