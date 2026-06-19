"""Commit 3: confirm a bet LANDED via /api/game/bet/recent-bet/ (ground-truth bet
history) instead of the WS 'tb' echo (which the exchange does not send back to us).
recent-bet is reachable (200) only via the VPN cgroup (home residential IP); from the
datacenter IP it 403s -- so the bot also fails fast if not routed (VPN guard).

Pure-logic tests with a fake page (no Playwright). Repo convention: plain asserts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_realbet as br

# real recent-bet entry shape (from the live 200 response)
def _entry(gid, amt, uid=932402231331845, cur="USDT", win="0"):
    return {"userId": uid, "currencyName": cur, "gameId": str(gid),
            "betAmount": str(amt), "winAmount": win, "nickName": "ivanK1157"}


class _FakePage:
    def __init__(self, result):
        self._result = result

    def evaluate(self, js, *args):
        return self._result


# ---- _recent_bet_match (pure) ---------------------------------------------
def test_match_our_bet_present():
    data = [_entry(9354366, "0.01"), _entry(9354300, "0.16")]
    assert br._recent_bet_match(data, 9354366, "0.01", our_uid=932402231331845) is True


def test_match_amount_format_tolerant():
    data = [_entry(9354366, "0.16")]
    assert br._recent_bet_match(data, 9354366, "0.160000", our_uid=932402231331845) is True


def test_no_match_wrong_round():
    data = [_entry(9354366, "0.01")]
    assert br._recent_bet_match(data, 9354999, "0.01") is False


def test_no_match_wrong_amount():
    data = [_entry(9354366, "0.32")]
    assert br._recent_bet_match(data, 9354366, "0.01") is False


def test_no_match_wrong_uid():
    data = [_entry(9354366, "0.01", uid=111111111)]
    assert br._recent_bet_match(data, 9354366, "0.01", our_uid=932402231331845) is False


def test_no_match_empty_or_none():
    assert br._recent_bet_match([], 9354366, "0.01") is False
    assert br._recent_bet_match(None, 9354366, "0.01") is False


def test_match_gameid_int_or_str():
    data = [{"userId": 932402231331845, "gameId": 9354366, "betAmount": "0.01"}]  # int gameId
    assert br._recent_bet_match(data, 9354366, "0.01") is True


# ---- _recent_bet_status (fake page) ---------------------------------------
def test_status_200():
    assert br._recent_bet_status(_FakePage({"status": 200, "data": []})) == 200


def test_status_403_blocked():
    assert br._recent_bet_status(_FakePage({"status": 403, "data": None})) == 403


def test_status_error_minus1():
    assert br._recent_bet_status(_FakePage({"status": -1, "data": None})) == -1


# ---- _recent_bet_confirms (fake page) -------------------------------------
def test_confirms_true_when_present_and_200():
    page = _FakePage({"status": 200, "data": [_entry(9354366, "0.01")]})
    assert br._recent_bet_confirms(page, 9354366, "0.01", our_uid=932402231331845) is True


def test_confirms_false_when_403():
    page = _FakePage({"status": 403, "data": None})
    assert br._recent_bet_confirms(page, 9354366, "0.01") is False


def test_confirms_false_when_not_in_history():
    page = _FakePage({"status": 200, "data": [_entry(9354300, "0.16")]})
    assert br._recent_bet_confirms(page, 9354366, "0.01") is False


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("OK")
