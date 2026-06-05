"""
Virtual betting account for BCGame crash simulator.

Stores all virtual bets and account state in the same DuckDB.
Called by training.py run_forever() to place/settle bets each cycle.
Dashboard reads this data as read-only.

Starting bankroll: START_SOL (~$100 at current SOL rate).
Minimum bet: 0.000006 SOL (BCGame minimum).
"""
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import duckdb
import math
from governance import operator_signal

_CONN_RETRIES = 6

START_SOL:    float = 1.1       # ~$100 @ ~$91/SOL
MIN_BET_SOL:  float = 0.000006  # BCGame minimum
MAX_BET_FRAC: float = 0.05      # never bet more than 5% per round


def _connect(db_path: str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    delay = 0.1
    for attempt in range(_CONN_RETRIES):
        try:
            return duckdb.connect(db_path, read_only=read_only)
        except duckdb.IOException:
            if attempt == _CONN_RETRIES - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def ensure_tables(db_path: str) -> None:
    """Idempotent schema migration — safe to call on every startup."""
    conn = _connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS virtual_bets (
                id             BIGINT PRIMARY KEY,
                round_db_id    BIGINT,
                game_round_id  VARCHAR,
                ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
                signal         VARCHAR,
                confidence     DOUBLE,
                kelly_f        DOUBLE,
                cashout_target DOUBLE,
                bet_sol        DOUBLE,
                actual_mult    DOUBLE,
                won            BOOLEAN,
                pnl_sol        DOUBLE,
                bankroll_after DOUBLE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS virtual_account (
                id            INTEGER PRIMARY KEY,
                bankroll_sol  DOUBLE NOT NULL,
                total_bets    INTEGER NOT NULL DEFAULT 0,
                total_wins    INTEGER NOT NULL DEFAULT 0,
                total_pnl_sol DOUBLE NOT NULL DEFAULT 0.0,
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        conn.execute(
            "INSERT INTO virtual_account (id, bankroll_sol) VALUES (1, ?) "
            "ON CONFLICT (id) DO NOTHING",
            [START_SOL],
        )
        try:
            conn.execute("CREATE SEQUENCE seq_vbet_id START 1")
        except Exception:
            pass
        for ddl in (
            "ALTER TABLE virtual_bets ADD COLUMN IF NOT EXISTS prediction_id VARCHAR",
            "ALTER TABLE virtual_bets ADD COLUMN IF NOT EXISTS model_id VARCHAR",
            "ALTER TABLE virtual_bets ADD COLUMN IF NOT EXISTS feature_schema_hash VARCHAR",
            "ALTER TABLE virtual_bets ADD COLUMN IF NOT EXISTS feature_source_round_db_id BIGINT",
            "ALTER TABLE virtual_bets ADD COLUMN IF NOT EXISTS settled_round_db_id BIGINT",
            "ALTER TABLE virtual_bets ADD COLUMN IF NOT EXISTS valid_for_promotion BOOLEAN DEFAULT false",
            "ALTER TABLE virtual_bets ADD COLUMN IF NOT EXISTS invalid_reason VARCHAR",
        ):
            conn.execute(ddl)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_vbet_prediction_round
            ON virtual_bets(prediction_id, settled_round_db_id)
        """)
    finally:
        conn.close()


class VirtualAccount:
    def __init__(self, db_path: str):
        self._path = db_path
        ensure_tables(db_path)

    # ── Account state ─────────────────────────────────────────────────────────

    def get_state(self) -> dict:
        conn = _connect(self._path, read_only=True)
        try:
            row = conn.execute(
                "SELECT bankroll_sol, total_bets, total_wins, total_pnl_sol "
                "FROM virtual_account WHERE id=1"
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {"bankroll_sol": START_SOL, "total_bets": 0,
                    "total_wins": 0, "total_pnl_sol": 0.0}
        return {
            "bankroll_sol":  row[0],
            "total_bets":    row[1],
            "total_wins":    row[2],
            "total_pnl_sol": row[3],
        }

    def reset(self) -> None:
        """Reset to starting bankroll, clear all bet history."""
        conn = _connect(self._path)
        try:
            conn.execute("BEGIN TRANSACTION")
            conn.execute(
                "UPDATE virtual_account "
                "SET bankroll_sol=?, total_bets=0, total_wins=0, "
                "total_pnl_sol=0.0, updated_at=now() WHERE id=1",
                [START_SOL],
            )
            conn.execute("DELETE FROM virtual_bets")
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    # ── Bet placement & settlement ────────────────────────────────────────────

    def place_and_settle(
        self,
        round_db_id:   int,
        game_round_id: str,
        actual_mult:   float,
        prediction:    dict,
    ) -> Optional[dict]:
        """
        Given a completed round and the ML prediction that was active before it,
        record a virtual bet and settle it.

        prediction: the dict from prediction.json["prediction"]
        Returns the bet record, or None if model said SKIP or bankroll depleted.
        """
        fields = self._parse_prediction(prediction)
        if not fields:
            return None

        signal = fields["signal"]
        if signal == "BET" and operator_signal(signal, fields["shadow_candidate"]) != "BET":
            signal = "SKIP"
        is_shadow = fields["shadow_candidate"] and signal == "SKIP"
        if signal != "BET" and not is_shadow:
            return None
        if fields["ev"] <= 0:
            return None
        stored_signal = "SHADOW_BET" if is_shadow else signal

        conn = _connect(self._path)
        try:
            conn.execute("BEGIN TRANSACTION")
            duplicate = conn.execute("""
                SELECT id FROM virtual_bets
                WHERE prediction_id=? AND settled_round_db_id=?
                LIMIT 1
            """, [fields["prediction_id"], round_db_id]).fetchone()
            if duplicate:
                conn.execute("COMMIT")
                return None

            row = conn.execute(
                "SELECT bankroll_sol FROM virtual_account WHERE id=1"
            ).fetchone()
            bankroll = row[0] if row else START_SOL
            if bankroll < MIN_BET_SOL:
                conn.execute("COMMIT")
                return None

            kelly_f = fields["kelly_f"]
            confidence = fields["confidence"]
            cashout = fields["cashout"]

            # Kelly-sized bet, floored at MIN_BET_SOL, capped at MAX_BET_FRAC
            kelly_bet = bankroll * max(kelly_f, 0.005)
            bet_sol   = max(MIN_BET_SOL, min(kelly_bet, bankroll * MAX_BET_FRAC))

            won    = actual_mult >= cashout
            pnl    = bet_sol * (cashout - 1) if won else -bet_sol
            new_br = max(0.0, bankroll + pnl)

            conn.execute("""
                INSERT INTO virtual_bets
                    (id, round_db_id, game_round_id, signal, confidence,
                     kelly_f, cashout_target, bet_sol, actual_mult,
                     won, pnl_sol, bankroll_after, prediction_id, model_id,
                     feature_schema_hash, feature_source_round_db_id,
                     settled_round_db_id, valid_for_promotion, invalid_reason)
                VALUES (nextval('seq_vbet_id'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?)
            """, [
                round_db_id, game_round_id, stored_signal, confidence,
                kelly_f, cashout, bet_sol, actual_mult, won, pnl, new_br,
                fields["prediction_id"], fields["model_id"],
                fields["feature_schema_hash"], fields["feature_source_round_db_id"],
                round_db_id, fields["valid_for_promotion"], None,
            ])

            conn.execute("""
                UPDATE virtual_account
                SET bankroll_sol  = ?,
                    total_bets    = total_bets + 1,
                    total_wins    = total_wins + ?,
                    total_pnl_sol = total_pnl_sol + ?,
                    updated_at    = now()
                WHERE id = 1
            """, [new_br, 1 if won else 0, pnl])
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

        return {
            "round_db_id":  round_db_id,
            "game_round_id": game_round_id,
            "prediction_id": fields["prediction_id"],
            "model_id": fields["model_id"],
            "signal":       stored_signal,
            "bet_sol":      round(bet_sol, 6),
            "cashout":      cashout,
            "actual_mult":  actual_mult,
            "won":          won,
            "pnl_sol":      round(pnl, 6),
            "bankroll":     round(new_br, 6),
        }

    def _parse_prediction(self, prediction: dict) -> Optional[dict]:
        if not prediction:
            return None
        try:
            confidence = float(prediction.get("p_above_threshold"))
            kelly_f = float(prediction.get("kelly_bet_fraction"))
            ev = float(prediction.get("ev_at_cutoff"))
            cashout = float(prediction.get("threshold"))
            source_id = int(prediction.get("feature_source_round_db_id"))
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(v) for v in (confidence, kelly_f, ev, cashout)):
            return None
        if not 0.0 <= confidence <= 1.0:
            return None
        if not 0.0 <= kelly_f <= MAX_BET_FRAC:
            return None
        if cashout <= 1.0:
            return None
        prediction_id = prediction.get("prediction_id")
        if not prediction_id:
            return None
        model_id = prediction.get("model_id")
        feature_schema_hash = prediction.get("feature_schema_hash")
        if not model_id or not feature_schema_hash:
            return None
        signal = prediction.get("signal", "SKIP")
        if signal not in {"BET", "SKIP"}:
            return None
        shadow_candidate = bool(prediction.get("shadow_candidate", False))
        return {
            "prediction_id": str(prediction_id),
            "model_id": str(model_id),
            "feature_schema_hash": str(feature_schema_hash),
            "feature_source_round_db_id": source_id,
            "signal": signal,
            "shadow_candidate": shadow_candidate,
            "confidence": confidence,
            "kelly_f": kelly_f,
            "ev": ev,
            "cashout": cashout,
            "valid_for_promotion": bool(prediction.get("valid_for_promotion", False)),
        }

    # ── History queries ───────────────────────────────────────────────────────

    def get_history(self, limit: int = 200) -> list:
        conn = _connect(self._path, read_only=True)
        try:
            rows = conn.execute("""
                SELECT id, round_db_id, game_round_id, ts::TIMESTAMPTZ,
                       signal, confidence, kelly_f, cashout_target,
                       bet_sol, actual_mult, won, pnl_sol, bankroll_after
                FROM virtual_bets ORDER BY id DESC LIMIT ?
            """, [limit]).fetchall()
        except Exception:
            return []
        finally:
            conn.close()
        cols = ["id", "round_db_id", "game_round_id", "ts",
                "signal", "confidence", "kelly_f", "cashout_target",
                "bet_sol", "actual_mult", "won", "pnl_sol", "bankroll_after"]
        return [dict(zip(cols, r)) for r in rows]

    def get_bankroll_curve(self) -> list:
        conn = _connect(self._path, read_only=True)
        try:
            rows = conn.execute(
                "SELECT bankroll_after FROM virtual_bets ORDER BY id ASC"
            ).fetchall()
        except Exception:
            return [START_SOL]
        finally:
            conn.close()
        return [START_SOL] + [r[0] for r in rows]

    def check_invariants(self) -> dict:
        conn = _connect(self._path, read_only=True)
        problems = []
        try:
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
            duplicates = conn.execute("""
                SELECT COUNT(*) FROM (
                    SELECT prediction_id, settled_round_db_id, COUNT(*) AS n
                    FROM virtual_bets
                    WHERE prediction_id IS NOT NULL AND settled_round_db_id IS NOT NULL
                    GROUP BY prediction_id, settled_round_db_id
                    HAVING COUNT(*) > 1
                )
            """).fetchone()[0]

            # Determine which protocol columns are present.
            cols = {r[1] for r in conn.execute("PRAGMA table_info('virtual_bets')").fetchall()}
            required_cols = {
                "prediction_id", "model_id", "feature_schema_hash",
                "feature_source_round_db_id", "settled_round_db_id",
                "valid_for_promotion", "invalid_reason",
            }
            missing_cols = sorted(required_cols - cols)

            if missing_cols:
                # Schema predates protocol columns — all bets are legacy artifacts, not errors.
                legacy_unbound = int(agg[0])
                invalid_bindings = 0
            else:
                # Pre-fix bets have prediction_id=NULL — expected legacy artifacts, not errors.
                # Real invalid bindings: post-fix bets (prediction_id set) missing other fields.
                legacy_unbound = conn.execute("""
                    SELECT COUNT(*) FROM virtual_bets WHERE prediction_id IS NULL
                """).fetchone()[0]
                invalid_bindings = conn.execute("""
                    SELECT COUNT(*) FROM virtual_bets
                    WHERE prediction_id IS NOT NULL
                      AND (  model_id IS NULL
                          OR feature_schema_hash IS NULL
                          OR feature_source_round_db_id IS NULL
                          OR settled_round_db_id IS NULL)
                """).fetchone()[0]
        finally:
            conn.close()

        if not account:
            return {"ok": False, "problems": ["missing_virtual_account"]}

        bankroll, total_bets, total_wins, total_pnl = account
        bet_count, win_count, pnl_sum = agg
        expected_bankroll = START_SOL + float(pnl_sum)

        if int(total_bets) != int(bet_count):
            problems.append("total_bets_mismatch")
        if int(total_wins) != int(win_count):
            problems.append("total_wins_mismatch")
        if abs(float(total_pnl) - float(pnl_sum)) > 1e-6:
            problems.append("total_pnl_mismatch")
        if abs(float(bankroll) - expected_bankroll) > 1e-6:
            problems.append("bankroll_mismatch")
        if int(duplicates) > 0:
            problems.append("duplicate_prediction_round")
        if missing_cols:
            problems.append("missing_protocol_columns")
        if int(invalid_bindings) > 0:
            problems.append("invalid_settlement_binding")

        return {
            "ok": not problems,
            "problems": problems,
            "account_total_bets": int(total_bets),
            "actual_total_bets": int(bet_count),
            "account_total_wins": int(total_wins),
            "actual_total_wins": int(win_count),
            "account_total_pnl_sol": float(total_pnl),
            "actual_total_pnl_sol": float(pnl_sum),
            "account_bankroll_sol": float(bankroll),
            "expected_bankroll_sol": expected_bankroll,
            "duplicate_prediction_rounds": int(duplicates),
            "legacy_unbound_bets": int(legacy_unbound),
            "invalid_settlement_bindings": int(invalid_bindings),
            "missing_protocol_columns": missing_cols,
        }

    def get_loss_round_ids(self, last_n: int = 500) -> set:
        """Return game_round_ids of recent virtual losses for training signal."""
        conn = _connect(self._path, read_only=True)
        try:
            rows = conn.execute("""
                SELECT game_round_id FROM virtual_bets
                WHERE won = false
                ORDER BY id DESC LIMIT ?
            """, [last_n]).fetchall()
        except Exception:
            return set()
        finally:
            conn.close()
        return {r[0] for r in rows if r[0]}
