"""Pre-send latency fix regression.

On the ws fast path, bal_before (audit/log only) must come from the CACHED balance
-- never a blocking /api/user/amount fetch, which (observed ~9s) pushes the bet past
the betting window -> server reject "Bet suspended. The game has started." This also
proves the WS bet packet is self-sufficient (carries amount+cashout+currency), so
skipping the fresh balance read does not change the bet itself.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_realbet


def test_presend_balance_ws_uses_cache_and_never_fetches():
    called = {"n": 0}

    def boom():
        called["n"] += 1
        raise AssertionError("fresh balance fetch must NOT run on the ws fast path")

    assert bot_realbet._presend_balance("ws", 5.0, boom) == 5.0
    assert called["n"] == 0  # fresh_fn never invoked on ws


def test_presend_balance_ws_handles_none_cache():
    # first bet before the interceptor has populated the cache -> None, no fetch
    assert bot_realbet._presend_balance("ws", None, lambda: 9.0) is None


def test_presend_balance_ws_case_insensitive():
    assert bot_realbet._presend_balance("WS", 3.0, lambda: 9.0) == 3.0
    assert bot_realbet._presend_balance(" ws ", 3.0, lambda: 9.0) == 3.0


def test_presend_balance_gui_uses_fresh():
    assert bot_realbet._presend_balance("gui", 5.0, lambda: 9.0) == 9.0


def test_ws_packet_is_self_sufficient_roundtrip():
    # real captured values: currency=USDT, amount=$0.08, cashout=2.10x
    pkt = bot_realbet._build_twice_bet_packet(
        packet_id=7,
        game_round_id=9318820,
        sol_amount="0.080000",
        cashout=2.1,
        browser_now_ms=1700000000000,
    )
    dec = bot_realbet._parse_twice_bet_packet_b64(base64.b64encode(pkt).decode("ascii"))
    assert dec["event"] == "twice-bet"
    assert dec["path"] == "/g/cm"
    assert dec["currency"] == bot_realbet.BET_CURRENCY  # default USDT
    assert dec["sol_amount"] == "0.080000"              # amount IS in the packet
    assert dec["cashout_int"] == 210                    # 2.10x IS in the packet
    assert dec["game_round_id"] == 9318820
