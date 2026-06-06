"""Tests for bot_live.handle_event (forward-bet: settle prior, decide next, miss)."""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_state import BotAccount
from bot_strategy import STRATEGIES
from bot_live import handle_event, _new_state


class _NoMiss:
    def will_miss(self, rng):
        return False

    def __repr__(self):
        return "NoMiss"


class _AlwaysMiss:
    def will_miss(self, rng):
        return True


def _ev(seq, gid, mult, epoch=1):
    return {"type": "round_end", "seq": seq, "epoch": epoch, "game_round_id": str(gid),
            "multiplier": mult, "recent_mults": [2.0] * 210, "ts": "t"}


def _acct(tmp_path):
    cfg = STRATEGIES["compound"]   # no filter, cashout 2.0 -> reliably bets
    return BotAccount(os.path.join(str(tmp_path), "b.duckdb"), cfg=cfg), cfg


def test_first_event_opens_no_settle(tmp_path):
    acct, cfg = _acct(tmp_path)
    st = _new_state()
    handle_event(_ev(1, 100, 5.0), acct, cfg, st, _NoMiss(), random.Random(1), True)
    assert acct.get_totals()["total_bets"] == 0     # nothing settled on first event
    assert st["open_bet"] is not None and st["placed"] == 1


def test_second_event_settles_win(tmp_path):
    acct, cfg = _acct(tmp_path)
    start = acct.get_state()["total_bank_sol"]
    st = _new_state()
    handle_event(_ev(1, 100, 5.0), acct, cfg, st, _NoMiss(), random.Random(1), True)
    handle_event(_ev(2, 101, 3.0), acct, cfg, st, _NoMiss(), random.Random(1), True)  # win
    tot = acct.get_totals()
    assert tot["total_bets"] == 1 and tot["total_wins"] == 1
    assert acct.get_state()["total_bank_sol"] > start


def test_settles_loss(tmp_path):
    acct, cfg = _acct(tmp_path)
    start = acct.get_state()["total_bank_sol"]
    st = _new_state()
    handle_event(_ev(1, 100, 5.0), acct, cfg, st, _NoMiss(), random.Random(1), True)
    handle_event(_ev(2, 101, 1.2), acct, cfg, st, _NoMiss(), random.Random(1), True)  # loss
    assert acct.get_totals()["total_bets"] == 1 and acct.get_totals()["total_wins"] == 0
    assert acct.get_state()["total_bank_sol"] < start


def test_miss_voids_no_bet(tmp_path):
    acct, cfg = _acct(tmp_path)
    st = _new_state()
    handle_event(_ev(1, 100, 5.0), acct, cfg, st, _AlwaysMiss(), random.Random(1), True)
    assert st["open_bet"] is None and st["missed"] == 1 and st["placed"] == 0
    handle_event(_ev(2, 101, 3.0), acct, cfg, st, _AlwaysMiss(), random.Random(1), True)
    assert acct.get_totals()["total_bets"] == 0     # never placed -> never settled


def test_publisher_restart_voids_open_bet(tmp_path):
    acct, cfg = _acct(tmp_path)
    st = _new_state()
    handle_event(_ev(1, 100, 5.0, epoch=1), acct, cfg, st, _NoMiss(), random.Random(1), True)
    assert st["open_bet"] is not None
    # new epoch (publisher restarted) -> the in-flight bet is voided, not settled
    handle_event(_ev(1, 200, 3.0, epoch=2), acct, cfg, st, _NoMiss(), random.Random(1), True)
    assert acct.get_totals()["total_bets"] == 0     # voided, not settled


def test_ignores_non_round_end(tmp_path):
    acct, cfg = _acct(tmp_path)
    st = _new_state()
    handle_event({"type": "hello"}, acct, cfg, st, _NoMiss(), random.Random(1), True)
    assert st["open_bet"] is None and st["placed"] == 0
