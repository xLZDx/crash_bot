"""Step-2 guard: record_bet must NOT clobber last_processed_round_id with NULL
when called in live mode (round_db_id=None). Otherwise switching back to replay
mode breaks fetch_new_rounds (WHERE id > NULL)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_state import BotAccount


def _acct(tmp_path):
    return BotAccount(os.path.join(str(tmp_path), "s.duckdb"))


def test_live_bet_preserves_replay_cursor(tmp_path):
    acct = _acct(tmp_path)
    # replay bet sets the cursor
    acct.record_bet(round_db_id=500, game_round_id="g1", bet_sol=1.0,
                    cashout_target=2.0, actual_mult=3.0, new_scale=1.0)
    assert acct.get_state()["last_processed_round_id"] == 500

    # live bet (round_db_id=None) must NOT overwrite the cursor with NULL
    acct.record_bet(round_db_id=None, game_round_id="g2", bet_sol=1.0,
                    cashout_target=2.0, actual_mult=1.0, new_scale=2.0)
    st = acct.get_state()
    assert st["last_processed_round_id"] == 500          # preserved
    assert st["current_scale"] == 2.0                    # scale still updated
    assert acct.get_totals()["total_bets"] == 2          # bet still recorded


def test_replay_bet_still_advances_cursor(tmp_path):
    acct = _acct(tmp_path)
    acct.record_bet(round_db_id=10, game_round_id="g1", bet_sol=1.0,
                    cashout_target=2.0, actual_mult=3.0)
    assert acct.get_state()["last_processed_round_id"] == 10
    acct.record_bet(round_db_id=11, game_round_id="g2", bet_sol=1.0,
                    cashout_target=2.0, actual_mult=1.0)
    assert acct.get_state()["last_processed_round_id"] == 11


def test_live_pnl_accounting_correct(tmp_path):
    acct = _acct(tmp_path)
    start = acct.get_state()["total_bank_sol"]
    # win at 2.0x with 1.0 bet -> +1.0
    acct.record_bet(round_db_id=None, game_round_id="w", bet_sol=1.0,
                    cashout_target=2.0, actual_mult=2.5, new_scale=1.0)
    assert abs(acct.get_state()["total_bank_sol"] - (start + 1.0)) < 1e-9
    # loss -> -1.0
    acct.record_bet(round_db_id=None, game_round_id="l", bet_sol=1.0,
                    cashout_target=2.0, actual_mult=1.2, new_scale=2.0)
    assert abs(acct.get_state()["total_bank_sol"] - start) < 1e-9
