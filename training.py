"""
Continuous ML training pipeline for BCGame crash prediction.

HOW TO GET A REAL EDGE:
  1. Walk-forward CV — train on past, validate strictly on future. No shuffle.
  2. Selective betting — only bet top-N% confidence rounds.
  3. Bet volume features — rapid volume increase before crash is a weak signal.
  4. Calibrated probabilities — Platt scaling makes P(win)=0.6 mean 60% real.
  5. Threshold optimization — find the P cutoff where selective EV > 0.
  6. Kelly criterion sizing — never overbets, survives drawdowns.

Break-even:
  - Flat-bet at 2x: need win_rate > 50.5% (with 1% house edge).
  - Selective-bet at 2x with top-30% confidence: if model has AUC=0.56,
    those 30% rounds have ~53-54% win rate → EV turns positive.
"""

import json
import logging
import logging.handlers
import os
import sys
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

import numpy as np

try:
    import xgboost as xgb
    import lightgbm as lgb
    from sklearn.calibration import CalibratedClassifierCV, calibration_curve
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss
    from sklearn.isotonic import IsotonicRegression
    _HAS_ML = True
except ImportError as e:
    _HAS_ML = False
    _ML_ERR = str(e)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DB_PATH
from governance import governance_payload, operator_signal
from simulator import VirtualAccount

# ── Config ────────────────────────────────────────────────────────────────────

THRESHOLD        = 2.0
MIN_ROWS         = 500          # raised: need enough for walk-forward
RETRAIN_EVERY    = 50
QUALITY_GATE_AUC = 0.52         # model must beat this AUC or all signals = SKIP
LAG_WINDOWS      = [3, 5, 10, 20, 50, 100]   # added 3 and 100
MODEL_DIR        = Path(os.path.dirname(__file__)) / "data" / "models"
PREDICTION_FILE  = Path(os.path.dirname(__file__)) / "data" / "prediction.json"
LOG_FILE         = Path(os.path.dirname(__file__)) / "logs" / "training.log"
TRAIN_INTERVAL_S = 60

# Walk-forward: train on first 70%, validate on next 15%, test on last 15%
TRAIN_FRAC = 0.70
VAL_FRAC   = 0.15
# TEST_FRAC  = 0.15 (remainder)
EMBARGO    = max(LAG_WINDOWS)      # rows dropped at each split boundary
EMBARGO_MIN_SET = 20               # skip embargo if val or test would have < this many rows

# Selective betting thresholds to evaluate
CONFIDENCE_CUTOFFS = [0.50, 0.52, 0.54, 0.55, 0.57, 0.60]

PREDICTION_SCHEMA_VERSION = 2
FEATURE_SCHEMA_VERSION = "crash_features_v3_platt_signed_streak"
VALIDATION_PROTOCOL_VERSION = "walk_forward_val_cutoff_v2"
PREDICTION_TTL_S = 180

# Wilson lower-bound EV gate (90% one-sided confidence, z=1.28)
WILSON_Z        = 1.28
WILSON_MIN_BETS = 30
BET_FEATURE_COVERAGE_MIN = 0.20
BET_DERIVED_FEATURES = {
    "bets_mean5", "bets_std5", "bets_mean20", "bets_trend5",
    "bettors_mean5", "bettors_trend5", "log_bets_current",
    "bets_per_player", "whale_ratio", "top1_bet_log",
}

# ── Logging ───────────────────────────────────────────────────────────────────

def _build_logger() -> logging.Logger:
    log = logging.getLogger("training")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        str(LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    log.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
    log.addHandler(ch)
    return log


log = _build_logger()


# ── Feature engineering ───────────────────────────────────────────────────────

def _feature_names() -> list:
    feature_names = []
    for w in LAG_WINDOWS:
        for s in ["min", "max", "mean", "std", "lt2_rate", "lt1p1_rate", "gt5_rate", "median"]:
            feature_names.append(f"lag{w}_{s}")

    feature_names += [
        "signed_streak",      # positive = consecutive rounds above threshold; negative = below
        "rounds_since_big",
        "rounds_since_huge",
        "bets_mean5", "bets_std5", "bets_mean20", "bets_trend5",
        "bettors_mean5", "bettors_trend5",
        "hour_sin", "hour_cos", "dow_sin", "dow_cos",
        "log_mean5", "log_mean20",
        "minute_sin", "minute_cos",
        "log_bets_current",
        "bets_per_player",
        "whale_ratio",
        "top1_bet_log",
        "log_std5",
    ]
    return feature_names


def _row_arrays(rows: list) -> tuple:
    mults     = np.array([r[2] for r in rows], dtype=np.float64)
    bets      = np.array([r[4] if r[4] else np.nan for r in rows], dtype=np.float64)
    bettors   = np.array([r[5] if r[5] else np.nan for r in rows], dtype=np.float64)
    top1_bets = np.array([r[7] if len(r) > 7 and r[7] is not None else np.nan for r in rows], dtype=np.float64)
    ts_vals   = [r[3] for r in rows]
    return mults, bets, bettors, top1_bets, ts_vals


def _build_feature_row(rows: list, i: int) -> list:
    """Build features from rows available at index i and earlier only."""
    mults, bets, bettors, top1_bets, ts_vals = _row_arrays(rows)
    feats = []

    for w in LAG_WINDOWS:
        window = mults[i - w + 1: i + 1]
        feats.append(float(np.min(window)))
        feats.append(float(np.max(window)))
        feats.append(float(np.mean(window)))
        feats.append(float(np.std(window) + 1e-6))
        feats.append(float(np.mean(window < 2.0)))
        feats.append(float(np.mean(window < 1.1)))
        feats.append(float(np.mean(window >= 5.0)))
        feats.append(float(np.median(window)))

    streak_below = streak_above = 0
    for j in range(i, max(i - 100, -1), -1):
        if mults[j] < THRESHOLD:
            streak_below += 1
            if streak_above:
                break
        else:
            streak_above += 1
            if streak_below:
                break
    # Single signed streak: positive = above-threshold run, negative = below-threshold run.
    feats.append(float(streak_above if streak_above > 0 else -streak_below))

    rsb = rsh = None
    for j in range(i, max(i - 200, -1), -1):
        if mults[j] >= 5.0 and rsb is None:
            rsb = i - j
        if mults[j] >= 10.0 and rsh is None:
            rsh = i - j
        if rsb is not None and rsh is not None:
            break
    feats.append(float(rsb if rsb is not None else 200))
    feats.append(float(rsh if rsh is not None else 200))

    b5  = bets[max(0, i-5):i];  b5v  = b5[~np.isnan(b5)]
    b20 = bets[max(0, i-20):i]; b20v = b20[~np.isnan(b20)]
    feats.append(float(np.mean(b5v))  if len(b5v) >= 2 else 0.0)
    feats.append(float(np.std(b5v))   if len(b5v) >= 2 else 0.0)
    feats.append(float(np.mean(b20v)) if len(b20v) >= 3 else 0.0)
    if len(b5v) >= 5:
        trend = float(np.mean(b5v[-2:]) - np.mean(b5v[:3]))
    else:
        trend = 0.0
    feats.append(trend)

    n5  = bettors[max(0, i-5):i]; n5v = n5[~np.isnan(n5)]
    feats.append(float(np.mean(n5v))  if len(n5v) >= 2 else 0.0)
    bt_trend = float(np.mean(n5v[-2:]) - np.mean(n5v[:3])) if len(n5v) >= 5 else 0.0
    feats.append(bt_trend)

    ts = ts_vals[i]
    hour = dow = minute = 0
    if ts is not None:
        try:
            if hasattr(ts, "hour"):
                hour, dow, minute = ts.hour, ts.weekday(), ts.minute
            else:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                hour, dow, minute = dt.hour, dt.weekday(), dt.minute
        except Exception:
            pass
    feats.append(float(np.sin(2 * np.pi * hour / 24)))
    feats.append(float(np.cos(2 * np.pi * hour / 24)))
    feats.append(float(np.sin(2 * np.pi * dow / 7)))
    feats.append(float(np.cos(2 * np.pi * dow / 7)))

    w5  = mults[max(0, i-4):i + 1]
    w20 = mults[max(0, i-19):i + 1]
    feats.append(float(np.mean(np.log1p(w5))))
    feats.append(float(np.mean(np.log1p(w20))))

    feats.append(float(np.sin(2 * np.pi * minute / 60)))
    feats.append(float(np.cos(2 * np.pi * minute / 60)))

    cur_bet = bets[i]
    cur_cnt = bettors[i]
    t1      = top1_bets[i]
    feats.append(float(np.log1p(cur_bet)) if (not np.isnan(cur_bet) and cur_bet >= 0.0) else 0.0)
    if not np.isnan(cur_bet) and not np.isnan(cur_cnt) and cur_cnt > 0:
        feats.append(float(cur_bet / cur_cnt))
    else:
        feats.append(0.0)
    if not np.isnan(t1) and not np.isnan(cur_bet) and cur_bet > 0:
        feats.append(float(t1 / cur_bet))
    else:
        feats.append(0.0)
    feats.append(float(np.log1p(t1)) if not np.isnan(t1) else 0.0)

    _lw5 = np.log1p(mults[max(0, i - 4):i + 1])
    feats.append(float(np.std(_lw5) + 1e-6) if len(_lw5) >= 2 else 0.0)

    return feats


def build_training_features(rows: list, loss_ids: Optional[set] = None) -> tuple:
    """Return (X, y, feature_names) for causal training rows only."""
    mults = np.array([r[2] for r in rows], dtype=np.float64)
    max_lag = max(LAG_WINDOWS)
    n = len(mults)
    if n <= max_lag + 1:
        return None, None, None

    X_rows, y_rows = [], []
    for i in range(max_lag, n - 1):
        X_rows.append(_build_feature_row(rows, i))
        label = 1 if mults[i + 1] >= THRESHOLD else 0
        y_rows.append(label)

    if not X_rows:
        return None, None, None

    return np.array(X_rows, dtype=np.float32), np.array(y_rows, dtype=np.int32), _feature_names()


def build_inference_features(rows: list) -> tuple:
    """Return (X, feature_names) for the next unknown round after rows[-1]."""
    if len(rows) <= max(LAG_WINDOWS):
        return None, None
    feats = _build_feature_row(rows, len(rows) - 1)
    return np.array([feats], dtype=np.float32), _feature_names()


def build_features(rows: list, loss_ids: Optional[set] = None) -> tuple:
    """Backward-compatible alias for training features."""
    return build_training_features(rows, loss_ids=loss_ids)


def feature_manifest(rows: list) -> dict:
    n = len(rows)
    total_bets_n = sum(1 for r in rows if len(r) > 4 and r[4] is not None)
    bettors_n = sum(1 for r in rows if len(r) > 5 and r[5] is not None)
    top1_n = sum(1 for r in rows if len(r) > 7 and r[7] is not None)
    bet_coverage = (total_bets_n / n) if n else 0.0
    enabled = bet_coverage >= BET_FEATURE_COVERAGE_MIN
    return {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "bet_feature_coverage_min": BET_FEATURE_COVERAGE_MIN,
        "total_rows": n,
        "total_bets_coverage": round(bet_coverage, 4),
        "bettors_coverage": round((bettors_n / n), 4) if n else 0.0,
        "top1_bet_coverage": round((top1_n / n), 4) if n else 0.0,
        "bet_features_enabled": enabled,
        "disabled_features": sorted(BET_DERIVED_FEATURES if not enabled else []),
    }


def feature_schema_hash(feature_names: list, manifest: dict) -> str:
    payload = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "lag_windows": LAG_WINDOWS,
        "feature_names": feature_names,
        "disabled_features": sorted(manifest.get("disabled_features") or []),
        "validation_protocol_version": VALIDATION_PROTOCOL_VERSION,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def model_id_for_training(latest_round_id: int, rows_used: int, schema_hash: str) -> str:
    raw = json.dumps({
        "latest_round_id": latest_round_id,
        "rows_used": rows_used,
        "feature_schema_hash": schema_hash,
        "validation_protocol_version": VALIDATION_PROTOCOL_VERSION,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def apply_feature_manifest(X: np.ndarray, feature_names: list, manifest: dict) -> np.ndarray:
    disabled = set(manifest.get("disabled_features") or [])
    if not disabled:
        return X
    X2 = X.copy()
    for name in disabled:
        if name in feature_names:
            X2[:, feature_names.index(name)] = 0.0
    return X2


# ── Leakage / null-model diagnostics ─────────────────────────────────────────

def leakage_diagnostics(
    X_tr: np.ndarray,
    y_tr: np.ndarray,
    X_va: np.ndarray,
    y_va: np.ndarray,
    auc_val: float,
    block_size: int = 50,
    seed: int = 0,
) -> dict:
    """
    Permutation and dummy-baseline diagnostics to detect leakage.

    Trains two lightweight null models (globally shuffled labels and
    block-shuffled labels) plus a constant-prediction dummy, then reports
    the 'signal gap' between the real validation AUC and the block-shuffle
    null AUC.  A gap < 0.02 suggests the model is not learning real signal.

    Returns:
      null_auc_shuffle:       AUC when labels are globally shuffled before fit
      null_auc_block_shuffle: AUC when labels are shuffled within 50-round blocks
      dummy_auc:              AUC of constant base-rate prediction (analytic)
      signal_gap:             auc_val - null_auc_block_shuffle
    """
    rng = np.random.default_rng(seed)

    # Global shuffle
    y_shuf = y_tr.copy()
    rng.shuffle(y_shuf)

    # Block shuffle -- preserves local temporal density of positives
    y_block = y_tr.copy()
    for start in range(0, len(y_block), block_size):
        end = min(start + block_size, len(y_block))
        rng.shuffle(y_block[start:end])

    def _quick_lgb(y_labels: np.ndarray) -> float:
        pos = float(y_labels.mean())
        scale = (1 - pos) / max(pos, 1e-6)
        m = lgb.LGBMClassifier(
            n_estimators=50, max_depth=4, learning_rate=0.1,
            scale_pos_weight=scale, verbosity=-1, n_jobs=1,
        )
        try:
            m.fit(X_tr, y_labels)
            proba = m.predict_proba(X_va)[:, 1]
            if len(np.unique(y_va)) > 1:
                return float(roc_auc_score(y_va, proba))
        except Exception:
            pass
        return 0.5

    null_auc_shuffle = _quick_lgb(y_shuf)
    null_auc_block   = _quick_lgb(y_block)

    # Dummy: always predicts training base rate -- analytic AUC
    base_rate = float(y_tr.mean())
    dummy_proba = np.full(len(y_va), base_rate)
    try:
        dummy_auc = float(roc_auc_score(y_va, dummy_proba)) if len(np.unique(y_va)) > 1 else 0.5
    except Exception:
        dummy_auc = 0.5

    signal_gap = round(auc_val - null_auc_block, 4)

    return {
        "null_auc_shuffle":       round(null_auc_shuffle, 4),
        "null_auc_block_shuffle": round(null_auc_block, 4),
        "dummy_auc":              round(dummy_auc, 4),
        "signal_gap":             signal_gap,
    }


# ── Walk-forward training ─────────────────────────────────────────────────────

def train_models(X: np.ndarray, y: np.ndarray, feature_names: list) -> dict:
    """
    Walk-forward split (no shuffle): train → val → test.
    Calibrate probabilities with isotonic regression on val set.
    Select betting cutoff on validation only; report test metrics separately.
    """
    n = len(X)
    tr_end  = int(n * TRAIN_FRAC)
    val_end = int(n * (TRAIN_FRAC + VAL_FRAC))

    X_tr, y_tr = X[:tr_end], y[:tr_end]

    # Embargo: drop the first EMBARGO rows of val and test to prevent lag-window
    # contamination across split boundaries. Skip when the resulting sets are too small.
    va_start = tr_end  + EMBARGO
    te_start = val_end + EMBARGO
    if (val_end - va_start) < EMBARGO_MIN_SET or (n - te_start) < EMBARGO_MIN_SET:
        va_start = tr_end
        te_start = val_end
    X_va, y_va = X[va_start:val_end], y[va_start:val_end]
    X_te, y_te = X[te_start:],        y[te_start:]

    pos_rate = float(y_tr.mean())
    scale_pos = (1 - pos_rate) / max(pos_rate, 1e-6)

    # ── XGBoost ───────────────────────────────────────────────────────────────
    xgb_model = xgb.XGBClassifier(
        n_estimators=400,
        max_depth=4,
        learning_rate=0.04,
        subsample=0.8,
        colsample_bytree=0.7,
        min_child_weight=10,    # prevents overfitting on small segments
        gamma=0.1,
        scale_pos_weight=scale_pos,
        eval_metric="logloss",
        verbosity=0,
        tree_method="hist",
        early_stopping_rounds=30,
    )
    xgb_model.fit(
        X_tr, y_tr,
        eval_set=[(X_va, y_va)],
        verbose=False,
    )

    # ── LightGBM ──────────────────────────────────────────────────────────────
    lgb_model = lgb.LGBMClassifier(
        n_estimators=400,
        max_depth=4,
        learning_rate=0.04,
        subsample=0.8,
        colsample_bytree=0.7,
        min_child_samples=20,
        scale_pos_weight=scale_pos,
        verbosity=-1,
    )
    lgb_model.fit(
        X_tr, y_tr,
        eval_set=[(X_va, y_va)],
        callbacks=[lgb.early_stopping(30, verbose=False),
                   lgb.log_evaluation(-1)],
    )

    # ── Raw ensemble proba on val ────────────────────────────────────────────
    xgb_va = xgb_model.predict_proba(X_va)[:, 1]
    lgb_va = lgb_model.predict_proba(X_va)[:, 1]
    raw_va = (xgb_va + lgb_va) / 2.0

    # ── Platt scaling calibration (val → test generalisation) ────────────────
    # LogisticRegression on 1 feature (raw ensemble prob) = Platt scaling.
    # Fitted on val; cal_va is in-sample for calibrator but out-of-sample for
    # the underlying XGB/LGB ensemble. cal_te is the true OOS evaluation.
    calibrator = LogisticRegression(C=1e10, solver="lbfgs", max_iter=300)
    calibrator.fit(raw_va.reshape(-1, 1), y_va)

    xgb_te = xgb_model.predict_proba(X_te)[:, 1]
    lgb_te = lgb_model.predict_proba(X_te)[:, 1]
    raw_te = (xgb_te + lgb_te) / 2.0
    cal_va = calibrator.predict_proba(raw_va.reshape(-1, 1))[:, 1]
    cal_te = calibrator.predict_proba(raw_te.reshape(-1, 1))[:, 1]

    # ── Metrics on test set ──────────────────────────────────────────────────
    auc_cal_val = roc_auc_score(y_va, cal_va) if len(np.unique(y_va)) > 1 else 0.5
    auc_raw = roc_auc_score(y_te, raw_te) if len(np.unique(y_te)) > 1 else 0.5
    auc_cal = roc_auc_score(y_te, cal_te) if len(np.unique(y_te)) > 1 else 0.5
    brier   = brier_score_loss(y_te, cal_te)
    base_wr = float(y_te.mean())

    selective_val = selective_report(cal_va, y_va)
    selective_test = selective_report(cal_te, y_te)
    calibration_val = calibration_diagnostics(cal_va, y_va)
    calibration_test = calibration_diagnostics(cal_te, y_te)
    best_cutoff = choose_best_cutoff(selective_val)

    # ── Feature importance ───────────────────────────────────────────────────
    importance = dict(zip(feature_names, xgb_model.feature_importances_.tolist()))
    top10 = sorted(importance.items(), key=lambda x: -x[1])[:10]

    # ── Leakage / null-model diagnostics ─────────────────────────────────────
    leak_diag = leakage_diagnostics(X_tr, y_tr, X_va, y_va, auc_val=float(auc_cal_val))

    return {
        "xgb_model":   xgb_model,
        "lgb_model":   lgb_model,
        "calibrator":  calibrator,
        "metrics": {
            "auc_raw":     round(float(auc_raw), 4),
            "auc_cal":     round(float(auc_cal), 4),
            "auc_cal_val": round(float(auc_cal_val), 4),
            "brier_score": round(float(brier), 4),
            "ece_val": calibration_val["ece"],
            "ece_test": calibration_test["ece"],
            "base_win_rate": round(base_wr, 4),
            "test_n":      len(y_te),
            "train_n":     len(y_tr),
            "val_n":       len(y_va),
            "pos_rate":    round(pos_rate, 4),
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "validation_protocol_version": VALIDATION_PROTOCOL_VERSION,
            "null_auc_shuffle":       leak_diag["null_auc_shuffle"],
            "null_auc_block_shuffle": leak_diag["null_auc_block_shuffle"],
            "dummy_auc":              leak_diag["dummy_auc"],
            "signal_gap":             leak_diag["signal_gap"],
        },
        "selective":    selective_test,
        "selective_val": selective_val,
        "selective_test": selective_test,
        "calibration_val": calibration_val,
        "calibration_test": calibration_test,
        "leakage_diagnostics": leak_diag,
        "best_cutoff":  best_cutoff,
        "top_features": top10,
        "feature_names": feature_names,
    }


def selective_report(proba: np.ndarray, y: np.ndarray) -> dict:
    selective = {}
    for cutoff in CONFIDENCE_CUTOFFS:
        mask = proba >= cutoff
        n_bet = int(mask.sum())
        if n_bet < 10:
            selective[cutoff] = {"n_bets": n_bet, "win_rate": None, "ev": None, "ev_lb": None}
            continue
        wins = int(y[mask].sum())
        wr = float(y[mask].mean())
        ev = wr * (THRESHOLD - 1) - (1 - wr) * 1.0
        b = THRESHOLD - 1
        kelly_f = max(0.0, (b * wr - (1 - wr)) / b)
        ev_lb = wilson_ev_lower_bound(wins, n_bet)
        selective[cutoff] = {
            "n_bets":   n_bet,
            "pct_bets": round(n_bet / len(y) * 100, 1),
            "win_rate": round(wr, 4),
            "ev":       round(ev, 4),
            "ev_lb":    ev_lb,
            "kelly_f":  round(kelly_f, 4),
            "signal":   "BET" if ev > 0 else "SKIP",
            "signal_lb": "BET" if (ev_lb is not None and ev_lb > 0) else "SKIP",
        }
    return selective


def choose_best_cutoff(selective: dict) -> float:
    return max(
        [c for c in CONFIDENCE_CUTOFFS
         if selective[c].get("ev") is not None and selective[c]["n_bets"] >= 20],
        key=lambda c: selective[c]["ev"],
        default=0.52,
    )


def wilson_ev_lower_bound(wins: int, n: int, z: float = WILSON_Z) -> Optional[float]:
    """
    Wilson score interval lower bound on win_rate, converted to EV.

    Returns conservative EV assuming true win_rate is at the lower edge of
    the Wilson confidence interval.  Positive result means we are (1-alpha)
    confident the strategy has positive EV.  Returns None when n < WILSON_MIN_BETS.
    """
    if n < WILSON_MIN_BETS:
        return None
    p = wins / n
    z2 = z * z
    lower_wr = (
        p + z2 / (2 * n) - z * np.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    ) / (1 + z2 / n)
    lower_wr = max(0.0, min(1.0, float(lower_wr)))
    ev_lb = lower_wr * (THRESHOLD - 1) - (1 - lower_wr) * 1.0
    return round(ev_lb, 4)


def calibration_diagnostics(proba: np.ndarray, y: np.ndarray, n_bins: int = 10) -> dict:
    bins = []
    ece = 0.0
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    for idx in range(n_bins):
        lo = float(edges[idx])
        hi = float(edges[idx + 1])
        if idx == n_bins - 1:
            mask = (proba >= lo) & (proba <= hi)
        else:
            mask = (proba >= lo) & (proba < hi)
        n = int(mask.sum())
        if n == 0:
            bins.append({
                "lo": round(lo, 2),
                "hi": round(hi, 2),
                "n": 0,
                "mean_pred": None,
                "win_rate": None,
            })
            continue
        mean_pred = float(proba[mask].mean())
        win_rate = float(y[mask].mean())
        ece += (n / len(y)) * abs(mean_pred - win_rate)
        bins.append({
            "lo": round(lo, 2),
            "hi": round(hi, 2),
            "n": n,
            "mean_pred": round(mean_pred, 4),
            "win_rate": round(win_rate, 4),
        })
    return {"ece": round(float(ece), 4), "bins": bins}


# ── Prediction ────────────────────────────────────────────────────────────────

def predict_next(models: dict, X_last_row: np.ndarray) -> dict:
    x = X_last_row.reshape(1, -1)
    xgb_p = float(models["xgb_model"].predict_proba(x)[0, 1])
    lgb_p = float(models["lgb_model"].predict_proba(x)[0, 1])
    raw_p = (xgb_p + lgb_p) / 2.0
    cal_p = float(models["calibrator"].predict_proba([[raw_p]])[0, 1])

    best_cut = models["best_cutoff"]
    # Use test-set selective stats for EV evidence — val stats are biased because
    # the cutoff was selected to maximise val EV (double-dipping).
    sel_info = models.get("selective_test", models.get("selective", {})).get(best_cut, {})
    ev_at_cut = sel_info.get("ev", 0) or 0
    ev_lb_at_cut = sel_info.get("ev_lb")  # None when n < WILSON_MIN_BETS
    # Raw Kelly can exceed the simulator's 5% cap — clamp before storing in envelope.
    kelly = min(sel_info.get("kelly_f", 0) or 0, 0.05)

    auc_ok = models["metrics"].get("auc_cal", 0) >= QUALITY_GATE_AUC
    would_bet = bool(cal_p >= best_cut and ev_at_cut > 0)
    raw_signal = "BET" if (auc_ok and would_bet) else "SKIP"
    signal = operator_signal(raw_signal, would_bet)

    return {
        "p_above_threshold":  round(cal_p, 4),
        "p_raw":              round(raw_p, 4),
        "threshold":          THRESHOLD,
        "best_cutoff":        best_cut,
        "signal":             signal,
        "raw_model_signal":   raw_signal,
        "shadow_candidate":   would_bet,
        "would_bet":          would_bet,
        "kelly_bet_fraction": round(kelly, 4),
        "ev_at_cutoff":       round(ev_at_cut, 4),
        "ev_lb_at_cutoff":    ev_lb_at_cut,
        "confidence":         "HIGH" if abs(cal_p - 0.5) > 0.08 else "LOW",
        "xgb_p":              round(xgb_p, 4),
        "lgb_p":              round(lgb_p, 4),
    }


def _write_prediction(payload: dict) -> None:
    PREDICTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PREDICTION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(PREDICTION_FILE)


def _read_prediction_file() -> dict:
    try:
        if PREDICTION_FILE.exists():
            return json.loads(PREDICTION_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Failed to read prediction file %s: %s", PREDICTION_FILE, exc)
    return {}


def _parse_prediction_time(prediction: dict) -> Optional[datetime]:
    raw = prediction.get("created_at") or prediction.get("ts")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def validate_prediction_for_settlement(
    prediction: dict,
    prev_rows: list,
    now: Optional[datetime] = None,
) -> tuple:
    if not prediction:
        return False, "missing_prediction"
    if not prev_rows:
        return False, "missing_prev_rows"
    if prediction.get("schema_version") != PREDICTION_SCHEMA_VERSION:
        return False, "schema_version_mismatch"
    if prediction.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        return False, "feature_schema_mismatch"
    if not prediction.get("feature_schema_hash"):
        return False, "missing_feature_schema_hash"
    if not prediction.get("model_id"):
        return False, "missing_model_id"

    expected_id = prev_rows[-1][0]
    source_id = prediction.get("feature_source_round_db_id")
    if source_id != expected_id:
        return False, "source_round_mismatch"
    if prediction.get("intended_next_round_after_db_id") != expected_id:
        return False, "intended_round_mismatch"

    expected_game_id = prev_rows[-1][1]
    if prediction.get("feature_source_game_round_id") != expected_game_id:
        return False, "source_game_round_mismatch"

    created_at = _parse_prediction_time(prediction)
    if created_at is None:
        return False, "missing_created_at"
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_s = (now - created_at).total_seconds()
    if age_s < -5:
        return False, "prediction_from_future"
    if age_s > PREDICTION_TTL_S:
        return False, "prediction_ttl_expired"

    for key in ("p_above_threshold", "best_cutoff", "kelly_bet_fraction", "ev_at_cutoff", "threshold"):
        try:
            value = float(prediction.get(key))
        except (TypeError, ValueError):
            return False, f"non_numeric_{key}"
        if not np.isfinite(value):
            return False, f"non_finite_{key}"

    p = float(prediction["p_above_threshold"])
    cutoff = float(prediction["best_cutoff"])
    kelly = float(prediction["kelly_bet_fraction"])
    threshold = float(prediction["threshold"])
    if not 0.0 <= p <= 1.0:
        return False, "probability_out_of_range"
    if not 0.0 <= cutoff <= 1.0:
        return False, "cutoff_out_of_range"
    if not 0.0 <= kelly <= 0.05:
        return False, "kelly_out_of_range"
    if threshold != THRESHOLD:
        return False, "threshold_mismatch"
    if prediction.get("signal") not in {"BET", "SKIP"}:
        return False, "invalid_signal"
    if "shadow_candidate" in prediction and not isinstance(prediction["shadow_candidate"], bool):
        return False, "invalid_shadow_candidate"

    return True, "ok"


# ── Main pipeline ─────────────────────────────────────────────────────────────

class TrainingPipeline:
    def __init__(self, db_path: str = DB_PATH):
        self._db_path   = db_path
        self._last_trained_at: int = 0
        self._models: Optional[dict] = None
        self._sim = VirtualAccount(db_path)
        MODEL_DIR.mkdir(parents=True, exist_ok=True)

    def _load_rows(self) -> list:
        from storage import CrashStorage
        return CrashStorage(self._db_path).get_training_rows(limit=50_000)

    def run_once(self, loss_ids: Optional[set] = None) -> bool:
        rows = self._load_rows()
        if not rows:
            return False

        latest_id = rows[-1][0]
        if latest_id - self._last_trained_at < RETRAIN_EVERY and self._models:
            self._refresh_prediction(rows, loss_ids=loss_ids)
            return False

        n = len(rows)
        if n < MIN_ROWS:
            log.info("Only %d rows, need %d to train", n, MIN_ROWS)
            return False

        log.info("Training on %d rows (latest_id=%d)", n, latest_id)
        t0 = time.time()

        X, y, feat_names = build_training_features(rows, loss_ids=loss_ids)
        if X is None or len(X) < 200:
            log.warning("Not enough feature rows after build_features: %s",
                        len(X) if X is not None else 0)
            return False
        manifest = feature_manifest(rows)
        X = apply_feature_manifest(X, feat_names, manifest)
        schema_hash = feature_schema_hash(feat_names, manifest)

        result = train_models(X, y, feat_names)
        result["feature_manifest"] = manifest
        result["feature_schema_hash"] = schema_hash
        result["model_id"] = model_id_for_training(latest_id, len(rows), schema_hash)
        elapsed = time.time() - t0

        self._models = result
        self._last_trained_at = latest_id

        m = result["metrics"]
        log.info(
            "Walk-forward  auc_cal=%.4f  brier=%.4f  "
            "train=%d  val=%d  test=%d  elapsed=%.1fs",
            m["auc_cal"], m["brier_score"],
            m["train_n"], m["val_n"], m["test_n"], elapsed,
        )
        best = result["best_cutoff"]
        sel  = result["selective_val"].get(best, {})
        log.info(
            "Best cutoff=%.2f  val_win_rate=%s  val_ev=%s  val_kelly=%s  val_n_bets=%s",
            best,
            sel.get("win_rate"), sel.get("ev"),
            sel.get("kelly_f"),  sel.get("n_bets"),
        )

        self._refresh_prediction(rows, elapsed=elapsed, loss_ids=loss_ids)
        return True

    def _refresh_prediction(
        self,
        rows: list,
        elapsed: float = 0.0,
        loss_ids: Optional[set] = None,
    ) -> None:
        if not self._models:
            return
        X, _ = build_inference_features(rows)
        if X is None or len(X) == 0:
            return
        manifest = self._models.get("feature_manifest") or feature_manifest(rows)
        X = apply_feature_manifest(X, self._models.get("feature_names", _feature_names()), manifest)

        pred    = predict_next(self._models, X[0])
        metrics = self._models["metrics"]

        # Attach latest round context so simulator can match predictions to rounds
        last_row = rows[-1]
        created_at = datetime.now(timezone.utc).isoformat()
        prediction_id = str(uuid4())
        pred.update({
            "schema_version": PREDICTION_SCHEMA_VERSION,
            "prediction_id": prediction_id,
            "model_id": self._models.get("model_id"),
            "created_at": created_at,
            "ts": created_at,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "feature_schema_hash": self._models.get("feature_schema_hash"),
            "validation_protocol_version": VALIDATION_PROTOCOL_VERSION,
            "feature_source_round_db_id": last_row[0],
            "feature_source_game_round_id": last_row[1],
            "intended_next_round_after_db_id": last_row[0],
            "last_round_db_id": last_row[0],
            "last_game_round_id": last_row[1],
            # Count toward the 500-shadow-bet promotion gate when the model
            # would have placed a real bet (regardless of governance suppression).
            "valid_for_promotion": bool(pred.get("would_bet", False)),
        })

        payload = {
            "schema_version": PREDICTION_SCHEMA_VERSION,
            "prediction_id":  prediction_id,
            "model_id": self._models.get("model_id"),
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "feature_schema_hash": self._models.get("feature_schema_hash"),
            "validation_protocol_version": VALIDATION_PROTOCOL_VERSION,
            "feature_manifest": self._models.get("feature_manifest", {}),
            "governance": governance_payload(),
            "ts":             created_at,
            "rows_used":      len(rows),
            "prediction":     pred,
            "metrics":        metrics,
            "selective":      {str(k): v for k, v in self._models["selective"].items()},
            "selective_val":  {str(k): v for k, v in self._models["selective_val"].items()},
            "selective_test": {str(k): v for k, v in self._models["selective_test"].items()},
            "calibration_val": self._models.get("calibration_val", {}),
            "calibration_test": self._models.get("calibration_test", {}),
            "leakage_diagnostics": self._models.get("leakage_diagnostics", {}),
            "best_cutoff":    self._models["best_cutoff"],
            "top_features":   {k: round(v, 4) for k, v in self._models["top_features"]},
            "train_elapsed_s": round(elapsed, 1),
            "disclaimer": (
                "BCGame crash is provably fair (SHA-256 chain). "
                "ML finds empirical patterns only. "
                "Positive EV requires model AUC > 0.53 on out-of-sample test data."
            ),
        }
        _write_prediction(payload)
        try:
            from storage import CrashStorage as _CS
            _CS(self._db_path).write_component_heartbeat("training")
        except Exception as exc:
            log.warning("Heartbeat write failed: %s", exc)  # informational; never blocks

    # ── Simulator integration ─────────────────────────────────────────────────

    def _settle_new_rounds(
        self,
        prev_rows: list,
        curr_rows: list,
        last_pred: dict,
    ) -> None:
        """
        For each round that arrived since the last prediction, apply the prediction
        and record in virtual_bets. Only the first new round is considered (the one
        the prediction was made FOR).
        """
        if not last_pred or not prev_rows or not curr_rows:
            return
        last_known_id = prev_rows[-1][0]
        valid, reason = validate_prediction_for_settlement(last_pred, prev_rows)
        if not valid:
            log.warning("Skipping virtual settlement: %s", reason)
            return
        new_rounds = [r for r in curr_rows if r[0] > last_known_id]
        if not new_rounds:
            return
        if new_rounds[0][0] != last_known_id + 1:
            log.warning(
                "Skipping virtual settlement: round gap after prediction "
                "(source_id=%s next_id=%s)",
                last_known_id,
                new_rounds[0][0],
            )
            return
        if len(new_rounds) > 1:
            log.warning(
                "Multiple new rounds since prediction (%d); settling first only",
                len(new_rounds),
            )
        # Apply prediction to the first round that happened after it was made
        r = new_rounds[0]
        result = self._sim.place_and_settle(
            round_db_id=r[0],
            game_round_id=r[1] or str(r[0]),
            actual_mult=r[2],
            prediction=last_pred,
        )
        if result:
            log.info(
                "VirtualBet  round=%s  mult=%.2fx  %s  pnl=%.6f SOL  bankroll=%.6f SOL",
                r[1], r[2],
                "WIN" if result["won"] else "LOSS",
                result["pnl_sol"],
                result["bankroll"],
            )

    def run_forever(self, interval_s: int = TRAIN_INTERVAL_S) -> None:
        log.info("Training pipeline started  threshold=%.1fx  retrain_every=%d",
                 THRESHOLD, RETRAIN_EVERY)

        prev_rows: list = []
        last_pred: dict = {}

        while True:
            try:
                # Settle the previous prediction against rounds that arrived
                curr_rows = self._load_rows()
                self._settle_new_rounds(prev_rows, curr_rows, last_pred)

                self.run_once()

                # Update state for next iteration.
                # prev_rows must reflect exactly what the new prediction was built
                # from, not all rows in the DB at this moment (more rounds may have
                # arrived during the ~100s training run).  Trim to the source id
                # recorded in the prediction so the settlement validator's
                # source_round_mismatch check always passes on the next cycle.
                raw_pred  = _read_prediction_file()
                last_pred = raw_pred.get("prediction", {})
                all_rows  = self._load_rows()
                pred_src  = last_pred.get("feature_source_round_db_id")
                if pred_src is not None:
                    prev_rows = [r for r in all_rows if r[0] <= pred_src]
                else:
                    prev_rows = all_rows

            except Exception:
                log.error("run_forever iteration failed", exc_info=True)
            time.sleep(interval_s)


if __name__ == "__main__":
    import argparse
    if not _HAS_ML:
        print(f"ERROR: {_ML_ERR}")
        sys.exit(1)
    ap = argparse.ArgumentParser()
    ap.add_argument("--db",       default=DB_PATH)
    ap.add_argument("--once",     action="store_true")
    ap.add_argument("--interval", type=int, default=TRAIN_INTERVAL_S)
    args = ap.parse_args()
    p = TrainingPipeline(db_path=args.db)
    if args.once:
        p.run_once()
    else:
        p.run_forever(interval_s=args.interval)
