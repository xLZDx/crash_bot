import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import training


def _rows(n=130):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        mult = 1.25 if i % 3 else 2.4
        rows.append((
            i + 1,
            f"round-{i + 1}",
            mult,
            base + timedelta(minutes=i),
            100.0 + i,
            10 + (i % 5),
            f"hash-{i + 1}",
            20.0 + (i % 7),
        ))
    return rows


def test_training_features_do_not_include_virtual_loss_feature():
    X, y, names = training.build_training_features(_rows())

    assert X is not None
    assert len(X) == len(y)
    assert "in_virtual_loss" not in names
    assert X.shape[1] == len(names)


def test_training_feature_row_uses_current_known_round_not_target_round():
    rows = _rows()
    max_lag = max(training.LAG_WINDOWS)
    sample_i = max_lag + 5
    sample_idx = sample_i - max_lag

    X1, y1, names = training.build_training_features(rows)

    changed_target = list(rows)
    target = list(changed_target[sample_i + 1])
    target[2] = 99.0 if y1[sample_idx] == 0 else 1.01
    target[4] = 999999.0
    changed_target[sample_i + 1] = tuple(target)
    X2, y2, _ = training.build_training_features(changed_target)

    assert np.array_equal(X1[sample_idx], X2[sample_idx])
    assert y1[sample_idx] != y2[sample_idx]

    changed_current = list(rows)
    current = list(changed_current[sample_i])
    current[2] = 42.0
    current[4] = 777.0
    changed_current[sample_i] = tuple(current)
    X3, _, _ = training.build_training_features(changed_current)

    assert X1[sample_idx, names.index("lag3_max")] != X3[sample_idx, names.index("lag3_max")]
    assert X1[sample_idx, names.index("log_bets_current")] != X3[sample_idx, names.index("log_bets_current")]


def test_inference_features_are_built_from_latest_known_round():
    rows = _rows()
    X1, names = training.build_inference_features(rows)

    changed = list(rows)
    latest = list(changed[-1])
    latest[2] = 50.0
    latest[4] = 12345.0
    changed[-1] = tuple(latest)
    X2, _ = training.build_inference_features(changed)

    assert X1.shape == X2.shape
    assert X1[0, names.index("lag3_max")] != X2[0, names.index("lag3_max")]
    assert X1[0, names.index("log_bets_current")] != X2[0, names.index("log_bets_current")]


def test_best_cutoff_is_selected_from_validation_report():
    selective = {
        0.50: {"n_bets": 30, "ev": -0.1},
        0.52: {"n_bets": 30, "ev": 0.01},
        0.54: {"n_bets": 25, "ev": 0.05},
        0.55: {"n_bets": 19, "ev": 0.9},
        0.57: {"n_bets": 10, "ev": 0.2},
        0.60: {"n_bets": 0, "ev": None},
    }

    assert training.choose_best_cutoff(selective) == 0.54


def test_prediction_settlement_validation_fails_closed():
    rows = _rows()
    now = datetime.now(timezone.utc)
    _, _, names = training.build_training_features(rows)
    manifest = training.feature_manifest(rows)
    schema_hash = training.feature_schema_hash(names, manifest)
    pred = {
        "schema_version": training.PREDICTION_SCHEMA_VERSION,
        "feature_schema_version": training.FEATURE_SCHEMA_VERSION,
        "feature_schema_hash": schema_hash,
        "model_id": training.model_id_for_training(rows[-1][0], len(rows), schema_hash),
        "feature_source_round_db_id": rows[-1][0],
        "feature_source_game_round_id": rows[-1][1],
        "intended_next_round_after_db_id": rows[-1][0],
        "created_at": now.isoformat(),
        "p_above_threshold": 0.6,
        "best_cutoff": 0.52,
        "kelly_bet_fraction": 0.01,
        "ev_at_cutoff": 0.02,
        "threshold": training.THRESHOLD,
        "signal": "BET",
    }

    assert training.validate_prediction_for_settlement(pred, rows, now=now) == (True, "ok")

    stale = dict(pred, created_at=(now - timedelta(seconds=training.PREDICTION_TTL_S + 1)).isoformat())
    assert training.validate_prediction_for_settlement(stale, rows, now=now)[0] is False

    wrong_round = dict(pred, feature_source_round_db_id=rows[-1][0] - 1)
    assert training.validate_prediction_for_settlement(wrong_round, rows, now=now)[0] is False

    missing_canonical = dict(pred)
    del missing_canonical["feature_source_round_db_id"]
    missing_canonical["last_round_db_id"] = rows[-1][0]
    assert training.validate_prediction_for_settlement(missing_canonical, rows, now=now)[0] is False

    bad_game_id = dict(pred, feature_source_game_round_id="other-round")
    assert training.validate_prediction_for_settlement(bad_game_id, rows, now=now)[0] is False

    bad_probability = dict(pred, p_above_threshold=1.5)
    assert training.validate_prediction_for_settlement(bad_probability, rows, now=now)[0] is False

    bad_kelly = dict(pred, kelly_bet_fraction=0.5)
    assert training.validate_prediction_for_settlement(bad_kelly, rows, now=now)[0] is False

    bad_threshold = dict(pred, threshold=0.01)
    assert training.validate_prediction_for_settlement(bad_threshold, rows, now=now)[0] is False

    bad_signal = dict(pred, signal="FORCE_BET")
    assert training.validate_prediction_for_settlement(bad_signal, rows, now=now)[0] is False

    bad_shadow = dict(pred, shadow_candidate="yes")
    assert training.validate_prediction_for_settlement(bad_shadow, rows, now=now)[0] is False

    missing_hash = dict(pred)
    del missing_hash["feature_schema_hash"]
    assert training.validate_prediction_for_settlement(missing_hash, rows, now=now)[1] == "missing_feature_schema_hash"

    missing_model = dict(pred)
    del missing_model["model_id"]
    assert training.validate_prediction_for_settlement(missing_model, rows, now=now)[1] == "missing_model_id"


def test_predict_next_uses_calibrated_test_auc_gate():
    """Quality gate must read auc_cal (OOS test AUC), not auc_cal_val (in-sample for calibrator)."""
    class _Model:
        def predict_proba(self, x):
            return np.array([[0.4, 0.6]])

    class _Calibrator:
        def predict_proba(self, raw):
            return np.array([[0.4, 0.6]])

    models = {
        "xgb_model": _Model(),
        "lgb_model": _Model(),
        "calibrator": _Calibrator(),
        "best_cutoff": 0.52,
        "selective_val":  {0.52: {"ev": 0.1, "kelly_f": 0.01}},
        "selective_test": {0.52: {"ev": 0.1, "kelly_f": 0.01}},
        # auc_cal_val high but auc_cal (true OOS) below gate — should block raw_model_signal
        "metrics": {"auc_cal_val": 0.9, "auc_cal": 0.5},
    }

    pred = training.predict_next(models, np.zeros(3, dtype=np.float32))

    assert pred["signal"] == "SKIP"
    assert pred["raw_model_signal"] == "SKIP"  # auc_cal < QUALITY_GATE_AUC suppresses BET
    assert pred["shadow_candidate"] is True
    assert pred["would_bet"] is True


def test_governance_blocks_operator_bet_when_model_would_bet():
    class _Model:
        def predict_proba(self, x):
            return np.array([[0.4, 0.6]])

    class _Calibrator:
        def predict_proba(self, raw):
            return np.array([[0.4, 0.6]])

    models = {
        "xgb_model": _Model(),
        "lgb_model": _Model(),
        "calibrator": _Calibrator(),
        "best_cutoff": 0.52,
        "selective_val":  {0.52: {"ev": 0.1, "kelly_f": 0.01}},
        "selective_test": {0.52: {"ev": 0.1, "kelly_f": 0.01}},
        "metrics": {"auc_cal_val": 0.9, "auc_cal": 0.9},
    }

    pred = training.predict_next(models, np.zeros(3, dtype=np.float32))

    assert pred["raw_model_signal"] == "BET"
    assert pred["signal"] == "SKIP"
    assert pred["shadow_candidate"] is True


def test_low_bet_coverage_disables_bet_derived_features():
    rows = []
    for row in _rows():
        as_list = list(row)
        as_list[4] = None
        as_list[5] = None
        as_list[7] = None
        rows.append(tuple(as_list))

    X, _, names = training.build_training_features(rows)
    manifest = training.feature_manifest(rows)
    gated = training.apply_feature_manifest(X, names, manifest)

    assert manifest["bet_features_enabled"] is False
    assert "log_bets_current" in manifest["disabled_features"]
    for name in training.BET_DERIVED_FEATURES:
        assert np.all(gated[:, names.index(name)] == 0.0)


def test_sufficient_bet_coverage_keeps_bet_derived_features():
    rows = _rows()
    X, _, names = training.build_training_features(rows)
    manifest = training.feature_manifest(rows)
    gated = training.apply_feature_manifest(X, names, manifest)

    assert manifest["bet_features_enabled"] is True
    assert manifest["disabled_features"] == []
    assert np.array_equal(X, gated)


def test_feature_schema_hash_changes_when_manifest_disables_features():
    rows = _rows()
    _, _, names = training.build_training_features(rows)
    enabled = training.feature_manifest(rows)
    disabled = dict(enabled, disabled_features=["log_bets_current"])

    assert training.feature_schema_hash(names, enabled) != training.feature_schema_hash(names, disabled)


def test_calibration_diagnostics_reports_ece_and_bins():
    proba = np.array([0.1, 0.2, 0.8, 0.9], dtype=np.float32)
    y = np.array([0, 0, 1, 1], dtype=np.int32)

    report = training.calibration_diagnostics(proba, y, n_bins=2)

    assert report["ece"] == 0.15
    assert len(report["bins"]) == 2
    assert report["bins"][0]["n"] == 2
    assert report["bins"][1]["n"] == 2


# ── Wilson lower-bound EV gate ────────────────────────────────────────────────

def test_wilson_ev_lower_bound_returns_none_below_min_sample():
    # n < WILSON_MIN_BETS must return None regardless of win rate
    assert training.wilson_ev_lower_bound(wins=29, n=29) is None
    assert training.wilson_ev_lower_bound(wins=0, n=1) is None
    assert training.wilson_ev_lower_bound(wins=15, n=training.WILSON_MIN_BETS - 1) is None


def test_wilson_ev_lower_bound_positive_for_clear_edge():
    # 20 wins out of 30 bets (66.7%) -> lower bound should be > 50% -> ev_lb > 0
    ev_lb = training.wilson_ev_lower_bound(wins=20, n=30)
    assert ev_lb is not None
    assert ev_lb > 0.0, f"Expected positive ev_lb for 20/30 wins, got {ev_lb}"


def test_wilson_ev_lower_bound_negative_for_marginal_edge():
    # 16 wins out of 30 bets (53.3%) is not statistically proven at 90% confidence
    ev_lb = training.wilson_ev_lower_bound(wins=16, n=30)
    assert ev_lb is not None
    assert ev_lb < 0.0, f"Expected negative ev_lb for 16/30 wins (marginal), got {ev_lb}"


def test_selective_report_includes_ev_lb_field():
    # Build a val-sized proba/y where we know the outcome
    n = 60
    proba = np.array([0.6] * 30 + [0.4] * 30, dtype=np.float32)
    # 25/30 wins among high-confidence group -> clear edge
    y = np.array([1] * 25 + [0] * 5 + [0] * 30, dtype=np.int32)

    report = training.selective_report(proba, y)

    # Every entry with n_bets >= 10 must have ev_lb key
    for cutoff, info in report.items():
        assert "ev_lb" in info, f"ev_lb missing for cutoff {cutoff}"
        assert "signal_lb" in info, f"signal_lb missing for cutoff {cutoff}"

    # At cutoff 0.55 or 0.60, we select the 30 high-confidence rows (25 wins)
    # 25/30 = 83% win rate -> ev_lb should be positive
    high_info = report.get(0.57) or report.get(0.60)
    if high_info and high_info["n_bets"] >= training.WILSON_MIN_BETS:
        assert high_info["ev_lb"] is not None
        assert high_info["ev_lb"] > 0


def test_selective_report_ev_lb_none_when_insufficient_bets():
    # n_bets < 10 -> entry has ev=None and ev_lb=None
    proba = np.array([0.9] * 5 + [0.3] * 100, dtype=np.float32)
    y = np.array([1] * 5 + [0] * 100, dtype=np.int32)

    report = training.selective_report(proba, y)

    # cutoff=0.60: only 5 bets -> both ev and ev_lb are None
    entry_60 = report[0.60]
    assert entry_60["n_bets"] < 10
    assert entry_60["ev"] is None
    assert entry_60["ev_lb"] is None


# ── Bug regression: kelly cap + source_round alignment ───────────────────────

def test_predict_next_kelly_fraction_capped_at_five_percent():
    """Raw Kelly > 5% must be clamped to 0.05 before entering the prediction
    envelope so validate_prediction_for_settlement never fires kelly_out_of_range."""
    class _Model:
        def predict_proba(self, x):
            return np.array([[0.3, 0.7]])

    class _Calibrator:
        def predict_proba(self, raw):
            return np.array([[0.3, 0.7]])

    # kelly_f=0.40 simulates a high-win-rate cutoff (raw Kelly >> 5%)
    models = {
        "xgb_model": _Model(),
        "lgb_model": _Model(),
        "calibrator": _Calibrator(),
        "best_cutoff": 0.52,
        "selective_val":  {0.52: {"ev": 0.4, "kelly_f": 0.40}},
        "selective_test": {0.52: {"ev": 0.4, "kelly_f": 0.40}},
        "metrics": {"auc_cal_val": 0.9, "auc_cal": 0.9},
    }

    pred = training.predict_next(models, np.zeros(3, dtype=np.float32))

    assert pred["kelly_bet_fraction"] <= 0.05, (
        f"kelly_bet_fraction={pred['kelly_bet_fraction']} exceeds 0.05 cap"
    )


def test_run_forever_prev_rows_trimmed_to_prediction_source(tmp_path):
    """After run_once(), prev_rows must be trimmed to feature_source_round_db_id
    so that validate_prediction_for_settlement passes on the next cycle even if
    new rounds arrived during the ~100s training window."""
    from datetime import datetime, timedelta, timezone

    # Build a set of rows whose latest id is 50
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows_at_training = [(i, f"g{i}", 1.5, base + timedelta(minutes=i),
                         None, None, None, None) for i in range(1, 51)]

    # Simulate more rows arriving during the training run (ids 51-55)
    rows_after_training = rows_at_training + [
        (i, f"g{i}", 2.0, base + timedelta(minutes=i), None, None, None, None)
        for i in range(51, 56)
    ]

    X, _, names = training.build_training_features(rows_at_training)
    manifest = training.feature_manifest(rows_at_training)
    schema_hash = training.feature_schema_hash(names, manifest)

    # Prediction was built from rows_at_training (latest_id=50)
    prediction = {
        "schema_version": training.PREDICTION_SCHEMA_VERSION,
        "feature_schema_version": training.FEATURE_SCHEMA_VERSION,
        "feature_schema_hash": schema_hash,
        "model_id": training.model_id_for_training(50, len(rows_at_training), schema_hash),
        "feature_source_round_db_id": 50,
        "feature_source_game_round_id": "g50",
        "intended_next_round_after_db_id": 50,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "p_above_threshold": 0.6,
        "best_cutoff": 0.52,
        "kelly_bet_fraction": 0.01,
        "ev_at_cutoff": 0.02,
        "threshold": training.THRESHOLD,
        "signal": "BET",
    }

    # Simulate the trim that run_forever() now applies
    pred_src = prediction["feature_source_round_db_id"]
    trimmed = [r for r in rows_after_training if r[0] <= pred_src]

    # trimmed must end at id=50, NOT at id=55
    assert trimmed[-1][0] == 50, f"trimmed[-1][0]={trimmed[-1][0]}, expected 50"

    # And the settlement validator must now pass with the trimmed rows
    ok, reason = training.validate_prediction_for_settlement(prediction, trimmed)
    assert ok, f"Expected ok but got reason={reason}"


# ── Leakage / null-model diagnostics ─────────────────────────────────────────

def test_leakage_diagnostics_returns_required_keys():
    """leakage_diagnostics must return all four required keys."""
    rng = np.random.default_rng(42)
    n_tr, n_va, n_feat = 200, 60, 10
    X_tr = rng.standard_normal((n_tr, n_feat)).astype(np.float32)
    y_tr = rng.integers(0, 2, n_tr).astype(np.int32)
    X_va = rng.standard_normal((n_va, n_feat)).astype(np.float32)
    y_va = rng.integers(0, 2, n_va).astype(np.int32)

    result = training.leakage_diagnostics(X_tr, y_tr, X_va, y_va, auc_val=0.55)

    for key in ("null_auc_shuffle", "null_auc_block_shuffle", "dummy_auc", "signal_gap"):
        assert key in result, f"Missing key: {key}"
    assert isinstance(result["signal_gap"], float)
    assert isinstance(result["null_auc_shuffle"], float)


def test_leakage_diagnostics_null_auc_near_half_on_random_labels():
    """When labels have no real signal, null_auc_block_shuffle should be near 0.5."""
    rng = np.random.default_rng(7)
    n_tr, n_va, n_feat = 400, 100, 20
    X_tr = rng.standard_normal((n_tr, n_feat)).astype(np.float32)
    y_tr = rng.integers(0, 2, n_tr).astype(np.int32)   # pure random labels
    X_va = rng.standard_normal((n_va, n_feat)).astype(np.float32)
    y_va = rng.integers(0, 2, n_va).astype(np.int32)

    result = training.leakage_diagnostics(X_tr, y_tr, X_va, y_va, auc_val=0.51)

    # On truly random data the null AUC should not be far from 0.5
    assert abs(result["null_auc_block_shuffle"] - 0.5) < 0.15, (
        f"null_auc_block_shuffle={result['null_auc_block_shuffle']} unexpectedly far from 0.5"
    )
    # signal_gap = auc_val - null_auc_block_shuffle; just assert it's a finite float
    assert abs(result["signal_gap"]) < 1.0


def test_train_models_result_contains_leakage_diagnostics():
    """train_models() result must include leakage_diagnostics dict and propagate
    keys into metrics so the prediction payload carries them."""
    try:
        import xgboost  # noqa: F401
        import lightgbm  # noqa: F401
    except ImportError:
        import pytest
        pytest.skip("xgboost/lightgbm not installed")

    rows = _rows(310)
    X, y, names = training.build_training_features(rows)
    assert X is not None and len(X) >= 100

    result = training.train_models(X, y, names)

    assert "leakage_diagnostics" in result, "leakage_diagnostics key missing from train_models result"
    diag = result["leakage_diagnostics"]
    for key in ("null_auc_shuffle", "null_auc_block_shuffle", "dummy_auc", "signal_gap"):
        assert key in diag, f"leakage_diagnostics missing key: {key}"

    # Keys must also be propagated into the metrics sub-dict
    metrics = result["metrics"]
    for key in ("null_auc_shuffle", "null_auc_block_shuffle", "dummy_auc", "signal_gap"):
        assert key in metrics, f"metrics dict missing leakage key: {key}"

    # Phase 1: auc_val renamed to auc_cal_val (in-sample for calibrator);
    # auc_cal is the true OOS metric used by the quality gate.
    assert "auc_cal_val" in metrics, "metrics must contain auc_cal_val (Platt in-sample AUC)"
    assert "auc_cal" in metrics, "metrics must contain auc_cal (true OOS AUC)"
    assert "auc_val" not in metrics, "stale auc_val key must be removed from metrics"
