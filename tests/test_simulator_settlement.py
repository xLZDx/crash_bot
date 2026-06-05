import os
import sys

import duckdb
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from simulator import START_SOL, VirtualAccount


@pytest.fixture
def account(tmp_path):
    return VirtualAccount(str(tmp_path / "sim.duckdb"))


def _prediction(prediction_id="pred-1"):
    return {
        "prediction_id": prediction_id,
        "model_id": "model-1",
        "feature_schema_hash": "hash-1",
        "feature_source_round_db_id": 10,
        "signal": "SKIP",
        "shadow_candidate": True,
        "p_above_threshold": 0.6,
        "kelly_bet_fraction": 0.01,
        "ev_at_cutoff": 0.05,
        "threshold": 2.0,
    }


def test_duplicate_prediction_round_settlement_is_noop(account):
    first = account.place_and_settle(
        round_db_id=11,
        game_round_id="round-11",
        actual_mult=2.5,
        prediction=_prediction(),
    )
    duplicate = account.place_and_settle(
        round_db_id=11,
        game_round_id="round-11",
        actual_mult=2.5,
        prediction=_prediction(),
    )

    assert first is not None
    assert duplicate is None

    state = account.get_state()
    assert state["total_bets"] == 1
    assert state["total_wins"] == 1
    assert state["bankroll_sol"] == pytest.approx(START_SOL + first["pnl_sol"])

    with duckdb.connect(account._path, read_only=True) as conn:
        rows = conn.execute(
            "SELECT prediction_id, model_id, feature_schema_hash, settled_round_db_id FROM virtual_bets"
        ).fetchall()
    assert rows == [("pred-1", "model-1", "hash-1", 11)]


def test_malformed_prediction_fails_closed(account):
    bad = _prediction()
    bad["prediction_id"] = ""

    assert account.place_and_settle(11, "round-11", 2.5, bad) is None
    assert account.get_state()["total_bets"] == 0

    bad = _prediction("pred-2")
    bad["kelly_bet_fraction"] = 0.5

    assert account.place_and_settle(12, "round-12", 2.5, bad) is None
    assert account.get_state()["total_bets"] == 0


def test_shadow_candidate_records_virtual_bet_while_signal_skips(account):
    pred = _prediction("pred-shadow")

    result = account.place_and_settle(
        round_db_id=11,
        game_round_id="round-11",
        actual_mult=2.5,
        prediction=pred,
    )

    assert result is not None
    assert result["signal"] == "SHADOW_BET"
    assert account.get_state()["total_bets"] == 1


def test_operator_bet_payload_is_downgraded_to_shadow_by_governance(account):
    pred = _prediction("pred-governance")
    pred["signal"] = "BET"
    pred["shadow_candidate"] = True

    result = account.place_and_settle(
        round_db_id=11,
        game_round_id="round-11",
        actual_mult=2.5,
        prediction=pred,
    )

    assert result is not None
    assert result["signal"] == "SHADOW_BET"


def test_virtual_bets_store_protocol_fields(account):
    result = account.place_and_settle(
        round_db_id=11,
        game_round_id="round-11",
        actual_mult=1.5,
        prediction=_prediction("pred-2"),
    )

    assert result is not None
    with duckdb.connect(account._path, read_only=True) as conn:
        row = conn.execute("""
            SELECT prediction_id, model_id, feature_schema_hash,
                   feature_source_round_db_id,
                   settled_round_db_id, valid_for_promotion, invalid_reason
            FROM virtual_bets
        """).fetchone()

    assert row == ("pred-2", "model-1", "hash-1", 10, 11, False, None)


def test_valid_for_promotion_true_stored_when_prediction_would_bet(account):
    """valid_for_promotion=True in the prediction must reach the DB as TRUE.

    Regression for the bug where training.py never wrote valid_for_promotion
    into the prediction envelope, leaving it always False in virtual_bets.
    """
    pred = _prediction("pred-vfp")
    pred["valid_for_promotion"] = True
    pred["would_bet"] = True

    account.place_and_settle(
        round_db_id=11,
        game_round_id="round-11",
        actual_mult=1.5,
        prediction=pred,
    )

    with duckdb.connect(account._path, read_only=True) as conn:
        row = conn.execute(
            "SELECT valid_for_promotion FROM virtual_bets WHERE prediction_id='pred-vfp'"
        ).fetchone()

    assert row is not None
    assert row[0] is True, f"valid_for_promotion must be TRUE, got {row[0]}"


def test_invariant_check_reports_consistent_account(account):
    account.place_and_settle(
        round_db_id=11,
        game_round_id="round-11",
        actual_mult=2.5,
        prediction=_prediction("pred-3"),
    )

    report = account.check_invariants()

    assert report["ok"] is True
    assert report["problems"] == []
    assert report["account_total_bets"] == 1
    assert report["actual_total_bets"] == 1


def test_invariant_check_detects_account_mismatch(account):
    account.place_and_settle(
        round_db_id=11,
        game_round_id="round-11",
        actual_mult=2.5,
        prediction=_prediction("pred-4"),
    )
    with duckdb.connect(account._path) as conn:
        conn.execute(
            "UPDATE virtual_account SET total_bets=99 WHERE id=1"
        )

    report = account.check_invariants()

    assert report["ok"] is False
    assert "total_bets_mismatch" in report["problems"]


def test_invariant_check_does_not_flag_legacy_null_prediction_id_as_invalid(tmp_path):
    """Pre-fix bets (prediction_id=NULL) are expected artifacts — not invalid bindings.

    The DEGRADED banner must not fire just because historical bets pre-date the
    protocol columns.  Only post-fix bets where prediction_id IS SET but other
    required fields are missing should count as invalid_settlement_binding.
    """
    db_path = str(tmp_path / "legacy.duckdb")
    account = VirtualAccount(db_path)

    # Insert a legacy bet: all protocol columns NULL (simulates pre-fix row).
    with duckdb.connect(db_path) as conn:
        conn.execute("""
            INSERT INTO virtual_bets
                (id, round_db_id, game_round_id, signal, confidence, kelly_f,
                 cashout_target, bet_sol, actual_mult, won, pnl_sol, bankroll_after,
                 prediction_id, model_id, feature_schema_hash,
                 feature_source_round_db_id, settled_round_db_id,
                 valid_for_promotion, invalid_reason)
            VALUES (1, 1, 'r1', 'BET', 0.6, 0.01, 2.0, 0.01, 2.5, true,
                    0.01, 1.11, NULL, NULL, NULL, NULL, NULL, false, NULL)
        """)
        conn.execute("""
            UPDATE virtual_account
            SET bankroll_sol=1.11, total_bets=1, total_wins=1, total_pnl_sol=0.01
            WHERE id=1
        """)

    report = account.check_invariants()

    assert report["ok"] is True
    assert report["problems"] == []
    assert report["legacy_unbound_bets"] == 1
    assert report["invalid_settlement_bindings"] == 0


def test_invariant_check_flags_post_fix_bet_with_missing_fields(tmp_path):
    """A post-fix bet (prediction_id set) with other protocol fields NULL is a real error."""
    db_path = str(tmp_path / "postfix.duckdb")
    account = VirtualAccount(db_path)

    with duckdb.connect(db_path) as conn:
        conn.execute("""
            INSERT INTO virtual_bets
                (id, round_db_id, game_round_id, signal, confidence, kelly_f,
                 cashout_target, bet_sol, actual_mult, won, pnl_sol, bankroll_after,
                 prediction_id, model_id, feature_schema_hash,
                 feature_source_round_db_id, settled_round_db_id,
                 valid_for_promotion, invalid_reason)
            VALUES (1, 1, 'r1', 'BET', 0.6, 0.01, 2.0, 0.01, 2.5, true,
                    0.01, 1.11, 'pred-orphan', NULL, NULL, NULL, NULL, false, NULL)
        """)
        conn.execute("""
            UPDATE virtual_account
            SET bankroll_sol=1.11, total_bets=1, total_wins=1, total_pnl_sol=0.01
            WHERE id=1
        """)

    report = account.check_invariants()

    assert report["ok"] is False
    assert "invalid_settlement_binding" in report["problems"]
    assert report["invalid_settlement_bindings"] == 1
    assert report["legacy_unbound_bets"] == 0
