"""
BotAccount -- paper-trading state backed by DuckDB.
All writes go through this class; never touch the tables directly.
"""
import threading
from datetime import datetime, timezone
from typing import Optional

import duckdb

from bot_strategy import TOTAL_BANK_SOL, new_session_bank, StrategyConfig

# PAUSE_RESUME_ROUNDS: after this many skipped rounds in the paused state,
# the bot auto-resets consec_losing_sessions to 0 and opens a new session.
PAUSE_RESUME_ROUNDS = 50

_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_state (
    id                         INTEGER PRIMARY KEY,
    total_bank_sol             DOUBLE  NOT NULL,
    session_bank_sol           DOUBLE  NOT NULL,
    session_start_sol          DOUBLE  NOT NULL,
    session_id                 INTEGER,
    session_rounds             INTEGER NOT NULL DEFAULT 0,
    consec_losing_sessions     INTEGER NOT NULL DEFAULT 0,
    paused_rounds_skipped      INTEGER NOT NULL DEFAULT 0,
    current_scale              DOUBLE  NOT NULL DEFAULT 1.0,
    last_processed_round_id    INTEGER NOT NULL DEFAULT 0,
    updated_at                 TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS strategy_sessions (
    id                INTEGER PRIMARY KEY,
    started_at        TIMESTAMPTZ NOT NULL,
    ended_at          TIMESTAMPTZ,
    session_bank_start DOUBLE NOT NULL,
    session_bank_end  DOUBLE,
    pnl_sol           DOUBLE,
    exit_reason       TEXT,
    bets_placed       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS strategy_bets (
    id                  INTEGER PRIMARY KEY,
    session_id          INTEGER NOT NULL,
    placed_at           TIMESTAMPTZ NOT NULL,
    round_db_id         INTEGER,
    game_round_id       TEXT,
    bet_sol             DOUBLE  NOT NULL,
    cashout_target      DOUBLE  NOT NULL,
    actual_mult         DOUBLE  NOT NULL,
    won                 BOOLEAN NOT NULL,
    pnl_sol             DOUBLE  NOT NULL,
    total_bank_after    DOUBLE  NOT NULL,
    session_bank_after  DOUBLE  NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_snapshots (
    id             INTEGER PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL,
    total_bank_sol DOUBLE  NOT NULL,
    total_bets     INTEGER NOT NULL,
    total_wins     INTEGER NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _next_id(conn, table: str) -> int:
    row = conn.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table}").fetchone()
    return row[0]


class BotAccount:
    """
    Manages paper-trading state for a single strategy.

    State is fully persistent -- the bot can restart at any time and
    continue from where it left off.
    """

    def __init__(self, db_path: str, cfg: StrategyConfig = None):
        self._path = db_path
        self._cfg  = cfg
        self._lock = threading.Lock()
        self._init_schema()
        self._migrate_schema()
        self._ensure_state_row()

    # ── Setup ────────────────────────────────────────────────────────────────

    def _init_schema(self):
        with duckdb.connect(self._path) as conn:
            conn.execute(_SCHEMA)

    def _migrate_schema(self):
        """Add columns introduced after initial schema."""
        with duckdb.connect(self._path) as conn:
            existing = [
                r[1] for r in
                conn.execute("PRAGMA table_info(strategy_state)").fetchall()
            ]
            if "current_scale" not in existing:
                conn.execute(
                    "ALTER TABLE strategy_state "
                    "ADD COLUMN current_scale DOUBLE DEFAULT 1.0"
                )
                conn.execute(
                    "UPDATE strategy_state SET current_scale = 1.0 "
                    "WHERE current_scale IS NULL"
                )

    def _ensure_state_row(self):
        with duckdb.connect(self._path) as conn:
            existing = conn.execute(
                "SELECT id FROM strategy_state WHERE id = 1"
            ).fetchone()
            if existing:
                return

            sbank = new_session_bank(TOTAL_BANK_SOL, self._cfg)
            session_id = self._open_session_inner(conn, TOTAL_BANK_SOL, sbank)
            conn.execute("""
                INSERT INTO strategy_state
                    (id, total_bank_sol, session_bank_sol, session_start_sol,
                     session_id, session_rounds, consec_losing_sessions,
                     paused_rounds_skipped, current_scale,
                     last_processed_round_id, updated_at)
                VALUES (1, ?, ?, ?, ?, 0, 0, 0, 1.0, 0, ?)
            """, [TOTAL_BANK_SOL, sbank, sbank, session_id, _now()])

    # ── Read ─────────────────────────────────────────────────────────────────

    def get_state(self) -> dict:
        with self._lock:
            with duckdb.connect(self._path, read_only=True) as conn:
                row = conn.execute("""
                    SELECT total_bank_sol, session_bank_sol, session_start_sol,
                           session_id, session_rounds, consec_losing_sessions,
                           paused_rounds_skipped, current_scale,
                           last_processed_round_id
                    FROM strategy_state WHERE id = 1
                """).fetchone()
        return {
            "total_bank_sol":          row[0],
            "session_bank_sol":        row[1],
            "session_start_sol":       row[2],
            "session_id":              row[3],
            "session_rounds":          row[4],
            "consec_losing_sessions":  row[5],
            "paused_rounds_skipped":   row[6],
            "current_scale":           row[7],
            "last_processed_round_id": row[8],
        }

    def get_totals(self) -> dict:
        """Aggregate stats across all bets (lock-protected)."""
        with self._lock:
            with duckdb.connect(self._path, read_only=True) as conn:
                row = conn.execute("""
                    SELECT COUNT(*), SUM(CASE WHEN won THEN 1 ELSE 0 END),
                           SUM(pnl_sol)
                    FROM strategy_bets
                """).fetchone()
        return {
            "total_bets":    row[0] or 0,
            "total_wins":    row[1] or 0,
            "total_pnl_sol": row[2] or 0.0,
        }

    # ── Session management ───────────────────────────────────────────────────

    def _open_session_inner(self, conn, total_bank: float,
                            sbank: float) -> int:
        sid = _next_id(conn, "strategy_sessions")
        conn.execute("""
            INSERT INTO strategy_sessions
                (id, started_at, session_bank_start, bets_placed)
            VALUES (?, ?, ?, 0)
        """, [sid, _now(), sbank])
        return sid

    def open_new_session(self) -> int:
        with self._lock:
            with duckdb.connect(self._path) as conn:
                state = conn.execute("""
                    SELECT total_bank_sol FROM strategy_state WHERE id = 1
                """).fetchone()
                total = state[0]
                sbank = new_session_bank(total, self._cfg)
                sid = self._open_session_inner(conn, total, sbank)
                conn.execute("""
                    UPDATE strategy_state
                    SET session_bank_sol       = ?,
                        session_start_sol      = ?,
                        session_id             = ?,
                        session_rounds         = 0,
                        paused_rounds_skipped  = 0,
                        current_scale          = 1.0,
                        updated_at             = ?
                    WHERE id = 1
                """, [sbank, sbank, sid, _now()])
                return sid

    def close_session(self, exit_reason: str):
        with self._lock:
            with duckdb.connect(self._path) as conn:
                row = conn.execute("""
                    SELECT session_id, session_bank_sol, session_start_sol,
                           consec_losing_sessions
                    FROM strategy_state WHERE id = 1
                """).fetchone()
                sid, sbank, sstart, consec = row
                pnl = sbank - sstart
                new_consec = (consec + 1) if pnl < 0 else 0

                bets_placed = conn.execute(
                    "SELECT COUNT(*) FROM strategy_bets WHERE session_id = ?",
                    [sid]
                ).fetchone()[0]

                conn.execute("""
                    UPDATE strategy_sessions
                    SET ended_at          = ?,
                        session_bank_end  = ?,
                        pnl_sol           = ?,
                        exit_reason       = ?,
                        bets_placed       = ?
                    WHERE id = ?
                """, [_now(), sbank, pnl, exit_reason, bets_placed, sid])

                conn.execute("""
                    UPDATE strategy_state
                    SET consec_losing_sessions = ?,
                        updated_at             = ?
                    WHERE id = 1
                """, [new_consec, _now()])

    def reset_pause(self):
        """Auto-resume after PAUSE_RESUME_ROUNDS skipped rounds."""
        with self._lock:
            with duckdb.connect(self._path) as conn:
                conn.execute("""
                    UPDATE strategy_state
                    SET consec_losing_sessions = 0,
                        paused_rounds_skipped  = 0,
                        updated_at             = ?
                    WHERE id = 1
                """, [_now()])

    def increment_paused_skipped(self) -> int:
        """Bump paused_rounds_skipped counter; return new value."""
        with self._lock:
            with duckdb.connect(self._path) as conn:
                conn.execute("""
                    UPDATE strategy_state
                    SET paused_rounds_skipped = paused_rounds_skipped + 1,
                        updated_at            = ?
                    WHERE id = 1
                """, [_now()])
                row = conn.execute(
                    "SELECT paused_rounds_skipped FROM strategy_state WHERE id = 1"
                ).fetchone()
                return row[0]

    # ── Bet recording ────────────────────────────────────────────────────────

    def record_bet(self, round_db_id: int, game_round_id: Optional[str],
                   bet_sol: float, cashout_target: float,
                   actual_mult: float, new_scale: float = 1.0) -> dict:
        """
        Records a virtual bet and updates all running totals.
        new_scale: the scale factor to store after this bet (anti-martingale).
        Returns the settlement result dict.
        """
        won = actual_mult >= cashout_target
        pnl = bet_sol * (cashout_target - 1) if won else -bet_sol

        with self._lock:
            with duckdb.connect(self._path) as conn:
                row = conn.execute("""
                    SELECT total_bank_sol, session_bank_sol, session_id,
                           session_rounds
                    FROM strategy_state WHERE id = 1
                """).fetchone()
                total, sbank, sid, srounds = row

                new_sbank = sbank + pnl
                new_total = total + pnl

                bid = _next_id(conn, "strategy_bets")
                conn.execute("""
                    INSERT INTO strategy_bets
                        (id, session_id, placed_at, round_db_id, game_round_id,
                         bet_sol, cashout_target, actual_mult, won, pnl_sol,
                         total_bank_after, session_bank_after)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [bid, sid, _now(), round_db_id, game_round_id,
                      bet_sol, cashout_target, actual_mult, won, pnl,
                      new_total, new_sbank])

                conn.execute("""
                    UPDATE strategy_state
                    SET total_bank_sol          = ?,
                        session_bank_sol        = ?,
                        session_rounds          = ?,
                        current_scale           = ?,
                        last_processed_round_id = COALESCE(?, last_processed_round_id),
                        updated_at              = ?
                    WHERE id = 1
                """, [new_total, new_sbank, srounds + 1,
                      new_scale, round_db_id, _now()])

        return {"won": won, "pnl_sol": pnl,
                "total_bank_after": new_total,
                "session_bank_after": new_sbank}

    def mark_round_processed(self, round_db_id: int):
        with self._lock:
            with duckdb.connect(self._path) as conn:
                conn.execute("""
                    UPDATE strategy_state
                    SET last_processed_round_id = ?, updated_at = ?
                    WHERE id = 1
                """, [round_db_id, _now()])

    # ── Snapshots ────────────────────────────────────────────────────────────

    def save_snapshot(self):
        with self._lock:
            with duckdb.connect(self._path) as conn:
                row = conn.execute("""
                    SELECT total_bank_sol FROM strategy_state WHERE id = 1
                """).fetchone()
                totals_row = conn.execute("""
                    SELECT COUNT(*), SUM(CASE WHEN won THEN 1 ELSE 0 END)
                    FROM strategy_bets
                """).fetchone()
                sid = _next_id(conn, "strategy_snapshots")
                conn.execute("""
                    INSERT INTO strategy_snapshots
                        (id, ts, total_bank_sol, total_bets, total_wins)
                    VALUES (?, ?, ?, ?, ?)
                """, [sid, _now(),
                      row[0],
                      totals_row[0] or 0,
                      totals_row[1] or 0])

    def get_snapshot_near(self, ts: datetime) -> Optional[dict]:
        """Returns the snapshot row closest to the given timestamp."""
        target_epoch = ts.timestamp()
        with duckdb.connect(self._path, read_only=True) as conn:
            row = conn.execute("""
                SELECT total_bank_sol, total_bets, total_wins, ts
                FROM strategy_snapshots
                ORDER BY ABS(EPOCH(ts) - ?)
                LIMIT 1
            """, [target_epoch]).fetchone()
        if not row:
            return None
        return {"total_bank_sol": row[0],
                "total_bets":     row[1],
                "total_wins":     row[2],
                "ts":             row[3]}
