"""Commit 2 wiring: identity loader (_maybe_set_identity from /api/account/get/)
and the armed-pending-bet -> ack path that on_received feeds and the bet loop's
ACK-GATE consults. Pure logic (no Playwright). Repo convention: plain asserts,
run directly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_realbet as br

ACCOUNT_GET = (
    '{"code":0,"msg":null,"data":{"userId":932402231331845,"name":"ivanK1157",'
    '"uniqueUid":932402231331845,"uid":932402231331845,"vipLevel":0}}'
)


def _make_tb(round_id, currency, amount, user):
    enc = br._enc_varint
    body = enc((1 << 3) | 0) + enc(int(round_id))
    cb = currency.encode(); body += enc((2 << 3) | 2) + enc(len(cb)) + cb
    ab = str(amount).encode(); body += enc((3 << 3) | 2) + enc(len(ab)) + ab
    ub = user.encode(); body += enc((5 << 3) | 2) + enc(len(ub)) + ub
    return bytes([4, 2, 5]) + b"/g/cm" + bytes([2]) + b"tb" + body


def _reset():
    br._our_uid = None
    br._our_name = None
    br._last_bet_ack.update({"game_round_id": None, "amount": None, "ts": 0.0})
    br._pending_bet.update({"round": None, "amount": None})


def _recv(raw):
    """Mimic on_received's ack hook: only record when a bet is armed."""
    pb = br._pending_bet
    if pb.get("round") is not None:
        br._note_tb_ack(raw, round_id=pb["round"], amount=pb["amount"],
                        our_name=br._our_name, our_uid=br._our_uid)


def test_maybe_set_identity_sets_globals():
    _reset()
    assert br._maybe_set_identity(ACCOUNT_GET) is True
    assert br._our_uid == 932402231331845
    assert br._our_name == "ivanK1157"


def test_maybe_set_identity_idempotent():
    _reset()
    br._maybe_set_identity(ACCOUNT_GET)
    # second call (e.g. other account/get response) must NOT overwrite / re-set
    assert br._maybe_set_identity('{"data":{"name":"someoneElse","uid":1}}') is False
    assert br._our_name == "ivanK1157"


def test_maybe_set_identity_ignores_garbage():
    _reset()
    assert br._maybe_set_identity("not json") is False
    assert br._our_uid is None and br._our_name is None


def test_armed_bet_confirms_on_our_echo():
    _reset()
    br._maybe_set_identity(ACCOUNT_GET)
    br._pending_bet.update({"round": 9354178, "amount": "0.160000"})
    # other players' echoes first -> not yet confirmed
    _recv(_make_tb(9354178, "USDT", "0.16", "Whale"))
    _recv(_make_tb(9354178, "NGNFIAT", "3869", "ForwarDBet"))
    assert br._bet_ack_fresh(9354178, "0.160000") is False
    # our own echo -> confirmed
    _recv(_make_tb(9354178, "USDT", "0.160000", "ivanK1157"))
    assert br._bet_ack_fresh(9354178, "0.160000") is True


def test_no_ack_on_phantom_round():
    _reset()
    br._maybe_set_identity(ACCOUNT_GET)
    br._pending_bet.update({"round": 9354200, "amount": "0.01"})
    # only other players bet this round; OUR send was silently ignored -> no echo
    _recv(_make_tb(9354200, "USDT", "0.01", "Whale"))
    _recv(_make_tb(9354200, "USDT", "5.00", "BigGuy"))
    assert br._bet_ack_fresh(9354200, "0.01") is False


def test_no_ack_when_identity_missing():
    _reset()  # identity stays None
    br._pending_bet.update({"round": 9354178, "amount": "0.16"})
    _recv(_make_tb(9354178, "USDT", "0.16", "ivanK1157"))
    assert br._bet_ack_fresh(9354178, "0.16") is False


def test_ack_does_not_match_other_round():
    _reset()
    br._maybe_set_identity(ACCOUNT_GET)
    br._pending_bet.update({"round": 9354178, "amount": "0.16"})
    _recv(_make_tb(9354179, "USDT", "0.16", "ivanK1157"))   # our bet, WRONG round
    assert br._bet_ack_fresh(9354178, "0.16") is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("OK")
