"""
Read-only baseline report for the crash ML system.

This report is intentionally conservative: it never promotes the system and
marks current evidence as MANUAL_REVIEW until a post-fix shadow window exists.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb

from config import DB_PATH
from governance import governance_payload
from training import (
    FEATURE_SCHEMA_VERSION,
    PREDICTION_FILE,
    PREDICTION_SCHEMA_VERSION,
    VALIDATION_PROTOCOL_VERSION,
)

SYSTEM_STATUS = "MANUAL_REVIEW"


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name=?",
        [table],
    ).fetchone()
    return bool(row and row[0])


def _scalar(conn, sql: str, default: Any = 0) -> Any:
    try:
        row = conn.execute(sql).fetchone()
        if not row or row[0] is None:
            return default
        return row[0]
    except Exception:
        return default


def _table_columns(conn, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
    except Exception:
        return set()


def _prediction_age_s(prediction_path: Path) -> Optional[float]:
    if not prediction_path.exists():
        return None
    try:
        payload = json.loads(prediction_path.read_text(encoding="utf-8"))
        raw_ts = payload.get("ts") or payload.get("prediction", {}).get("created_at")
        if not raw_ts:
            return None
        ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
    except Exception:
        return None


def _prediction_snapshot(prediction_path: Path) -> dict:
    if not prediction_path.exists():
        return {"exists": False}
    try:
        payload = json.loads(prediction_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"exists": True, "readable": False, "error": str(exc)}

    pred = payload.get("prediction") or {}
    metrics = payload.get("metrics") or {}
    return {
        "exists": True,
        "readable": True,
        "schema_version": payload.get("schema_version"),
        "prediction_schema_version": pred.get("schema_version"),
        "feature_schema_version": payload.get("feature_schema_version"),
        "validation_protocol_version": payload.get("validation_protocol_version"),
        "feature_schema_hash": payload.get("feature_schema_hash") or pred.get("feature_schema_hash"),
        "prediction_id": payload.get("prediction_id") or pred.get("prediction_id"),
        "model_id": payload.get("model_id") or pred.get("model_id"),
        "signal": pred.get("signal"),
        "shadow_candidate": pred.get("shadow_candidate"),
        "auc_val": metrics.get("auc_val"),
        "auc_test": metrics.get("auc_cal"),
        "best_cutoff": payload.get("best_cutoff") or pred.get("best_cutoff"),
        "ev_lb_at_cutoff": pred.get("ev_lb_at_cutoff"),
        "age_s": _prediction_age_s(prediction_path),
    }


def _virtual_invariants(conn) -> dict:
    if not _table_exists(conn, "virtual_account") or not _table_exists(conn, "virtual_bets"):
        return {"ok": False, "problems": ["missing_virtual_tables"]}

    required_cols = {
        "prediction_id",
        "model_id",
        "feature_schema_hash",
        "feature_source_round_db_id",
        "settled_round_db_id",
        "valid_for_promotion",
        "invalid_reason",
    }
    cols = _table_columns(conn, "virtual_bets")
    missing_cols = sorted(required_cols - cols)

    account = conn.execute("""
        SELECT bankroll_sol, total_bets, total_wins, total_pnl_sol
        FROM virtual_account WHERE id=1
    """).fetchone()
    agg = conn.execute("""
        SELECT COUNT(*),
               COALESCE(SUM(CASE WHEN won THEN 1 ELSE 0 END), 0),
               COALESCE(SUM(pnl_sol), 0.0)
        FROM virtual_bets
    """).fetchone()
    if missing_cols:
        # Schema is so old we can't do meaningful binding checks
        invalid_bindings = 0
        legacy_unbound = int(agg[0])
        duplicate_bindings = 0
    else:
        # Pre-fix bets have prediction_id=NULL — expected legacy artifacts, not errors.
        # Real invalid bindings are post-fix bets (prediction_id set) missing other fields.
        legacy_unbound = _scalar(conn, """
            SELECT COUNT(*) FROM virtual_bets WHERE prediction_id IS NULL
        """)
        invalid_bindings = _scalar(conn, """
            SELECT COUNT(*) FROM virtual_bets
            WHERE prediction_id IS NOT NULL
              AND (  model_id IS NULL
                  OR feature_schema_hash IS NULL
                  OR feature_source_round_db_id IS NULL
                  OR settled_round_db_id IS NULL)
        """)
        duplicate_bindings = _scalar(conn, """
            SELECT COUNT(*) FROM (
                SELECT prediction_id, settled_round_db_id, COUNT(*) AS n
                FROM virtual_bets
                WHERE prediction_id IS NOT NULL AND settled_round_db_id IS NOT NULL
                GROUP BY prediction_id, settled_round_db_id
                HAVING COUNT(*) > 1
            )
        """)

    if not account:
        return {"ok": False, "problems": ["missing_virtual_account"]}

    problems = []
    if int(account[1]) != int(agg[0]):
        problems.append("total_bets_mismatch")
    if int(account[2]) != int(agg[1]):
        problems.append("total_wins_mismatch")
    if abs(float(account[3]) - float(agg[2])) > 1e-9:
        problems.append("total_pnl_mismatch")
    if missing_cols:
        problems.append("missing_protocol_columns")
    if int(invalid_bindings) > 0:
        problems.append("invalid_settlement_binding")
    if int(duplicate_bindings) > 0:
        problems.append("duplicate_prediction_round")

    return {
        "ok": not problems,
        "problems": problems,
        "legacy_unbound_bets": int(legacy_unbound),
        "invalid_settlement_bindings": int(invalid_bindings),
        "duplicate_prediction_rounds": int(duplicate_bindings),
        "missing_protocol_columns": missing_cols,
    }


def collect_report(db_path: str = DB_PATH, prediction_path: Path = PREDICTION_FILE) -> dict:
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "system_status": SYSTEM_STATUS,
        "promotion_allowed": False,
        "governance": governance_payload(),
        "promotion_block_reason": "post_fix_shadow_window_required",
        "expected_prediction_schema_version": PREDICTION_SCHEMA_VERSION,
        "expected_feature_schema_version": FEATURE_SCHEMA_VERSION,
        "expected_validation_protocol_version": VALIDATION_PROTOCOL_VERSION,
        "db_path": db_path,
        "prediction": _prediction_snapshot(prediction_path),
    }

    if not Path(db_path).exists():
        report["db"] = {"exists": False}
        return report

    conn = duckdb.connect(db_path, read_only=True)
    try:
        rounds = int(_scalar(conn, "SELECT COUNT(*) FROM rounds"))
        bet_feature_rows = int(_scalar(conn, "SELECT COUNT(*) FROM rounds WHERE total_bets IS NOT NULL"))
        bets = int(_scalar(conn, "SELECT COUNT(*) FROM bets")) if _table_exists(conn, "bets") else 0
        virtual_bets = int(_scalar(conn, "SELECT COUNT(*) FROM virtual_bets")) if _table_exists(conn, "virtual_bets") else 0
        shadow_bets = int(_scalar(
            conn,
            "SELECT COUNT(*) FROM virtual_bets WHERE signal='SHADOW_BET'",
        )) if _table_exists(conn, "virtual_bets") else 0
        valid_for_promotion = int(_scalar(
            conn,
            "SELECT COUNT(*) FROM virtual_bets WHERE valid_for_promotion = true",
        )) if _table_exists(conn, "virtual_bets") else 0
        invariants = _virtual_invariants(conn)

        db_section = {
            "exists": True,
            "rounds": rounds,
            "bet_feature_rows": bet_feature_rows,
            "bet_feature_coverage_pct": round((bet_feature_rows / rounds * 100), 2) if rounds else 0.0,
            "raw_bets": bets,
            "virtual_bets": virtual_bets,
            "shadow_bets": shadow_bets,
            "valid_for_promotion_virtual_bets": valid_for_promotion,
            "virtual_invariants": invariants,
        }
        report["db"] = db_section
        report["promotion_gate"] = _promotion_gate(
            db=db_section,
            invariants=invariants,
            prediction=report["prediction"],
        )
    finally:
        conn.close()
    return report


_SHADOW_BETS_REQUIRED = 500


def _promotion_gate(db: dict, invariants: dict, prediction: dict) -> dict:
    """
    Evaluate all measurable promotion gate criteria from FIX_PLAN_V2.md.
    Returns a dict with per-criterion pass/fail and an overall `ready` flag.
    `ready=True` does NOT mean live execution is allowed — governance is the
    final authority.  This only reports whether observable evidence is sufficient.
    """
    shadow_bets = db.get("shadow_bets", 0)
    age_s = prediction.get("age_s")

    from training import PREDICTION_SCHEMA_VERSION, FEATURE_SCHEMA_VERSION, PREDICTION_TTL_S

    criteria = {
        "shadow_bets_sufficient": shadow_bets >= _SHADOW_BETS_REQUIRED,
        "no_invalid_settlement_binding": invariants.get("invalid_settlement_bindings", 1) == 0,
        "no_duplicate_settlement": invariants.get("duplicate_prediction_rounds", 1) == 0,
        "db_invariants_ok": invariants.get("ok", False),
        "prediction_fresh": age_s is not None and age_s < PREDICTION_TTL_S,
        "schema_versions_match": (
            prediction.get("schema_version") == PREDICTION_SCHEMA_VERSION
            and prediction.get("feature_schema_version") == FEATURE_SCHEMA_VERSION
        ),
        "ev_lb_positive": (
            prediction.get("ev_lb_at_cutoff") is not None
            and prediction.get("ev_lb_at_cutoff", -1) > 0
        ),
    }

    failing = [k for k, v in criteria.items() if not v]
    return {
        "ready": not failing,
        "shadow_bets": shadow_bets,
        "shadow_bets_required": _SHADOW_BETS_REQUIRED,
        "shadow_bets_remaining": max(0, _SHADOW_BETS_REQUIRED - shadow_bets),
        "ev_lb_at_cutoff": prediction.get("ev_lb_at_cutoff"),
        "criteria": criteria,
        "failing_criteria": failing,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--prediction", default=str(PREDICTION_FILE))
    args = parser.parse_args()
    report = collect_report(args.db, Path(args.prediction))
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
