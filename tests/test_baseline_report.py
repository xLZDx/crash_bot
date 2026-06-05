import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from baseline_report import collect_report
from simulator import VirtualAccount
from storage import CrashStorage
import training


def test_baseline_report_blocks_promotion_without_db(tmp_path):
    report = collect_report(
        db_path=str(tmp_path / "missing.duckdb"),
        prediction_path=tmp_path / "missing_prediction.json",
    )

    assert report["system_status"] == "MANUAL_REVIEW"
    assert report["promotion_allowed"] is False
    assert report["db"]["exists"] is False


def test_baseline_report_counts_data_and_protocol(tmp_path):
    db_path = str(tmp_path / "report.duckdb")
    storage = CrashStorage(db_path)
    try:
        storage.insert(2.0, total_bets=100.0, game_round_id="round-1")
        storage.insert(1.5, game_round_id="round-2")
    finally:
        storage.close()

    account = VirtualAccount(db_path)
    account.place_and_settle(
        round_db_id=2,
        game_round_id="round-2",
        actual_mult=2.5,
        prediction={
        "prediction_id": "pred-report",
        "model_id": "model-report",
        "feature_schema_hash": "hash-report",
        "feature_source_round_db_id": 1,
            "signal": "SKIP",
            "shadow_candidate": True,
            "p_above_threshold": 0.6,
            "kelly_bet_fraction": 0.01,
            "ev_at_cutoff": 0.05,
            "threshold": 2.0,
        },
    )

    pred_path = tmp_path / "prediction.json"
    pred_path.write_text(json.dumps({
        "schema_version": training.PREDICTION_SCHEMA_VERSION,
        "feature_schema_version": training.FEATURE_SCHEMA_VERSION,
        "feature_schema_hash": "hash-report",
        "validation_protocol_version": training.VALIDATION_PROTOCOL_VERSION,
        "prediction_id": "pred-report",
        "model_id": "model-report",
        "ts": datetime.now(timezone.utc).isoformat(),
        "prediction": {
            "schema_version": training.PREDICTION_SCHEMA_VERSION,
            "prediction_id": "pred-report",
            "model_id": "model-report",
            "feature_schema_hash": "hash-report",
            "signal": "SKIP",
            "shadow_candidate": True,
        },
        "metrics": {"auc_val": 0.51, "auc_cal": 0.9},
        "best_cutoff": 0.52,
    }), encoding="utf-8")

    report = collect_report(db_path=db_path, prediction_path=pred_path)

    assert report["promotion_allowed"] is False
    assert report["db"]["rounds"] == 2
    assert report["db"]["bet_feature_rows"] == 1
    assert report["db"]["bet_feature_coverage_pct"] == 50.0
    assert report["db"]["virtual_bets"] == 1
    assert report["db"]["valid_for_promotion_virtual_bets"] == 0
    assert report["db"]["virtual_invariants"]["ok"] is True
    assert report["db"]["virtual_invariants"]["invalid_settlement_bindings"] == 0
    assert report["prediction"]["schema_version"] == training.PREDICTION_SCHEMA_VERSION
    assert report["prediction"]["model_id"] == "model-report"
    assert report["prediction"]["feature_schema_hash"] == "hash-report"
    assert report["prediction"]["shadow_candidate"] is True


def test_baseline_report_counts_shadow_bets_separately(tmp_path):
    db_path = str(tmp_path / "shadow.duckdb")
    storage = CrashStorage(db_path)
    try:
        storage.insert(2.5, game_round_id="r1")
        storage.insert(1.2, game_round_id="r2")
    finally:
        storage.close()

    account = VirtualAccount(db_path)
    # SHADOW_BET — governance downgrades BET to SKIP, shadow_candidate=True
    account.place_and_settle(
        round_db_id=2,
        game_round_id="r2",
        actual_mult=1.2,
        prediction={
            "prediction_id": "p1",
            "model_id": "m1",
            "feature_schema_hash": "h1",
            "feature_source_round_db_id": 1,
            "signal": "SKIP",
            "shadow_candidate": True,
            "p_above_threshold": 0.65,
            "kelly_bet_fraction": 0.01,
            "ev_at_cutoff": 0.1,
            "threshold": 2.0,
        },
    )

    report = collect_report(db_path=db_path, prediction_path=tmp_path / "pred.json")

    assert report["db"]["shadow_bets"] == 1
    assert report["db"]["virtual_bets"] == 1


def test_baseline_report_promotion_gate_reports_progress(tmp_path):
    db_path = str(tmp_path / "gate.duckdb")
    storage = CrashStorage(db_path)
    try:
        storage.insert(2.5, game_round_id="r1")
        storage.insert(1.2, game_round_id="r2")
    finally:
        storage.close()

    pred_path = tmp_path / "prediction.json"
    pred_path.write_text(json.dumps({
        "schema_version": training.PREDICTION_SCHEMA_VERSION,
        "feature_schema_version": training.FEATURE_SCHEMA_VERSION,
        "feature_schema_hash": "hash-gate",
        "validation_protocol_version": training.VALIDATION_PROTOCOL_VERSION,
        "prediction_id": "pred-gate",
        "model_id": "model-gate",
        "ts": datetime.now(timezone.utc).isoformat(),
        "prediction": {
            "schema_version": training.PREDICTION_SCHEMA_VERSION,
            "prediction_id": "pred-gate",
            "model_id": "model-gate",
            "feature_schema_hash": "hash-gate",
            "signal": "SKIP",
            "shadow_candidate": True,
            "ev_lb_at_cutoff": None,
        },
        "metrics": {"auc_val": 0.51, "auc_cal": 0.9},
        "best_cutoff": 0.52,
    }), encoding="utf-8")

    report = collect_report(db_path=db_path, prediction_path=pred_path)
    gate = report["promotion_gate"]

    assert gate["ready"] is False
    assert gate["shadow_bets_required"] == 500
    assert gate["shadow_bets_remaining"] == 500
    assert "shadow_bets_sufficient" in gate["failing_criteria"]
    # ev_lb is None -> ev_lb_positive fails
    assert "ev_lb_positive" in gate["failing_criteria"]


def test_baseline_report_promotion_gate_includes_ev_lb_in_prediction_snapshot(tmp_path):
    db_path = str(tmp_path / "evlb.duckdb")
    CrashStorage(db_path).close()

    pred_path = tmp_path / "prediction.json"
    pred_path.write_text(json.dumps({
        "schema_version": training.PREDICTION_SCHEMA_VERSION,
        "feature_schema_version": training.FEATURE_SCHEMA_VERSION,
        "feature_schema_hash": "h",
        "validation_protocol_version": training.VALIDATION_PROTOCOL_VERSION,
        "prediction_id": "p",
        "model_id": "m",
        "ts": datetime.now(timezone.utc).isoformat(),
        "prediction": {
            "schema_version": training.PREDICTION_SCHEMA_VERSION,
            "prediction_id": "p",
            "model_id": "m",
            "feature_schema_hash": "h",
            "signal": "SKIP",
            "shadow_candidate": True,
            "ev_lb_at_cutoff": 0.03,
        },
        "metrics": {"auc_val": 0.55, "auc_cal": 0.9},
        "best_cutoff": 0.54,
    }), encoding="utf-8")

    report = collect_report(db_path=db_path, prediction_path=pred_path)

    assert report["prediction"]["ev_lb_at_cutoff"] == 0.03
    assert report["promotion_gate"]["ev_lb_at_cutoff"] == 0.03


def test_baseline_report_degrades_when_protocol_columns_are_missing(tmp_path):
    db_path = str(tmp_path / "legacy.duckdb")
    storage = CrashStorage(db_path)
    try:
        storage.insert(2.0, game_round_id="round-1")
    finally:
        storage.close()

    import duckdb
    with duckdb.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE virtual_account (
                id INTEGER PRIMARY KEY,
                bankroll_sol DOUBLE NOT NULL,
                total_bets INTEGER NOT NULL,
                total_wins INTEGER NOT NULL,
                total_pnl_sol DOUBLE NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE virtual_bets (
                id BIGINT PRIMARY KEY,
                round_db_id BIGINT,
                game_round_id VARCHAR,
                signal VARCHAR,
                pnl_sol DOUBLE,
                won BOOLEAN
            )
        """)
        conn.execute("INSERT INTO virtual_account VALUES (1, 1.1, 1, 1, 0.1)")
        conn.execute("INSERT INTO virtual_bets VALUES (1, 1, 'round-1', 'BET', 0.1, true)")

    report = collect_report(
        db_path=db_path,
        prediction_path=tmp_path / "missing_prediction.json",
    )

    invariants = report["db"]["virtual_invariants"]
    assert invariants["ok"] is False
    assert "missing_protocol_columns" in invariants["problems"]
    # When protocol columns are absent we can't evaluate binding integrity —
    # the missing-columns problem is surfaced instead; invalid_settlement_binding
    # is reserved for post-fix bets that have prediction_id but incomplete other fields.
    assert invariants["invalid_settlement_bindings"] == 0
    assert invariants["legacy_unbound_bets"] == 1
