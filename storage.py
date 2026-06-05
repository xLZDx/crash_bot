import re
import time
import os
import duckdb
from pathlib import Path
from typing import Optional

_UNSAFE_RE = re.compile(r"""[;'"`\x00]|\.\.""")


def _connect(db_path: str, read_only: bool = False, retries: int = 6) -> duckdb.DuckDBPyConnection:
    """Open DuckDB connection with retries for Windows file-lock contention."""
    delay = 0.1
    for attempt in range(retries):
        try:
            return duckdb.connect(db_path, read_only=read_only)
        except duckdb.IOException:
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


class CrashStorage:
    """
    Thin DuckDB wrapper.  Per-operation connections with explicit close()
    because DuckDB's context-manager __exit__ only commits (DB-API 2.0),
    it does not close the connection.
    """

    def __init__(self, db_path: str):
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rounds (
                    id            BIGINT PRIMARY KEY,
                    game_round_id VARCHAR UNIQUE,
                    multiplier    DOUBLE NOT NULL,
                    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
                    source        VARCHAR NOT NULL DEFAULT 'ws',
                    total_bets    DOUBLE,
                    num_bettors   INTEGER,
                    frame_event   VARCHAR,
                    hash          VARCHAR,
                    hash_verified BOOLEAN DEFAULT FALSE
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS system_health (
                    component VARCHAR PRIMARY KEY,
                    last_ok_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_round_id BIGINT,
                    last_game_round_id VARCHAR,
                    pid INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bets (
                    id        BIGINT PRIMARY KEY,
                    round_id  VARCHAR NOT NULL,
                    currency  VARCHAR,
                    amount    DOUBLE,
                    username  VARCHAR,
                    ts        TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            try:
                conn.execute("CREATE SEQUENCE seq_round_id START 1")
            except Exception:
                pass
            try:
                conn.execute("CREATE SEQUENCE seq_bet_id START 1")
            except Exception:
                pass
            # Idempotent migrations for older DBs lacking new columns
            for col, defn in [("hash", "VARCHAR"), ("hash_verified", "BOOLEAN DEFAULT FALSE")]:
                try:
                    conn.execute(f"ALTER TABLE rounds ADD COLUMN {col} {defn}")
                except Exception:
                    pass
        finally:
            conn.close()

    # ── Rounds ────────────────────────────────────────────────────────────────

    def insert(
        self,
        multiplier: float,
        source: str = "ws",
        game_round_id: Optional[str] = None,
        total_bets: Optional[float] = None,
        num_bettors: Optional[int] = None,
        frame_event: Optional[str] = None,
        hash: Optional[str] = None,
    ) -> Optional[int]:
        conn = _connect(self._path)
        try:
            conn.execute("BEGIN TRANSACTION")
            row = conn.execute(
                """
                INSERT INTO rounds
                    (id, game_round_id, multiplier, source,
                     total_bets, num_bettors, frame_event, hash)
                VALUES (nextval('seq_round_id'), ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (game_round_id) DO NOTHING
                RETURNING id
                """,
                [game_round_id, round(multiplier, 4), source,
                 total_bets, num_bettors, frame_event, hash],
            ).fetchone()
            heartbeat_round_id = row[0] if row else None
            if heartbeat_round_id is None and game_round_id:
                existing = conn.execute(
                    "SELECT id FROM rounds WHERE game_round_id=?",
                    [game_round_id],
                ).fetchone()
                heartbeat_round_id = existing[0] if existing else None
            conn.execute("""
                INSERT INTO system_health
                    (component, last_ok_at, last_round_id, last_game_round_id, pid)
                VALUES ('collector', now(), ?, ?, ?)
                ON CONFLICT (component) DO UPDATE SET
                    last_ok_at=excluded.last_ok_at,
                    last_round_id=excluded.last_round_id,
                    last_game_round_id=excluded.last_game_round_id,
                    pid=excluded.pid
            """, [heartbeat_round_id, game_round_id, os.getpid()])
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()
        return row[0] if row else None

    def update_hash(self, game_round_id: str, hash_val: str, verified: bool = False) -> None:
        conn = _connect(self._path)
        try:
            conn.execute(
                "UPDATE rounds SET hash=?, hash_verified=? WHERE game_round_id=?",
                [hash_val, verified, game_round_id],
            )
        finally:
            conn.close()

    def count(self) -> int:
        conn = _connect(self._path, read_only=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
        finally:
            conn.close()

    def last_multiplier(self) -> Optional[float]:
        conn = _connect(self._path, read_only=True)
        try:
            row = conn.execute(
                "SELECT multiplier FROM rounds ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def export_csv(self, path: str) -> None:
        if _UNSAFE_RE.search(path):
            raise ValueError(f"Unsafe export path: {path!r}")
        resolved = str(Path(path).resolve())
        conn = _connect(self._path)
        try:
            conn.execute(f"COPY rounds TO '{resolved}' (HEADER, DELIMITER ',')")
        finally:
            conn.close()

    # ── Bets ──────────────────────────────────────────────────────────────────

    def insert_bet(
        self,
        round_id: str,
        currency: Optional[str],
        amount: Optional[float],
        username: Optional[str] = None,  # accepted but never stored — privacy
    ) -> None:
        conn = _connect(self._path)
        try:
            conn.execute(
                """
                INSERT INTO bets (id, round_id, currency, amount, username)
                VALUES (nextval('seq_bet_id'), ?, ?, ?, NULL)
                """,
                [round_id, currency, amount],
            )
        finally:
            conn.close()

    def purge_stored_usernames(self) -> int:
        """
        Set username = NULL for any bets row where a username was previously
        stored.  Returns the count of rows updated.

        Privacy policy: usernames are never stored from new bets (insert_bet
        always passes NULL), but older schema versions or direct imports may
        have written non-NULL values.  Call this once on DB upgrade or as a
        scheduled maintenance task to ensure no usernames are retained.
        """
        conn = _connect(self._path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM bets WHERE username IS NOT NULL"
            ).fetchone()
            count = int(row[0]) if row else 0
            if count:
                conn.execute(
                    "UPDATE bets SET username = NULL WHERE username IS NOT NULL"
                )
            return count
        finally:
            conn.close()

    def write_component_heartbeat(self, component: str) -> None:
        """Update system_health for a named component without a round association."""
        conn = _connect(self._path)
        try:
            conn.execute("""
                INSERT INTO system_health (component, last_ok_at, pid)
                VALUES (?, now(), ?)
                ON CONFLICT (component) DO UPDATE SET
                    last_ok_at = excluded.last_ok_at,
                    pid        = excluded.pid
            """, [component, os.getpid()])
        finally:
            conn.close()

    def get_bet_summary(self, round_id: str):
        """Returns (total_bets_usdt, num_bettors) for a round."""
        conn = _connect(self._path, read_only=True)
        try:
            row = conn.execute(
                """
                SELECT SUM(CASE WHEN currency='USDT' THEN amount ELSE 0 END),
                       COUNT(*)
                FROM bets WHERE round_id=?
                """,
                [round_id],
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None, None
        return (row[0] if row[0] else None), (row[1] if row[1] else None)

    # ── Training data ─────────────────────────────────────────────────────────

    def get_training_rows(self, min_id: int = 0, limit: int = 50_000):
        """Return ordered rows for feature engineering.

        Tuple layout:
          0  id
          1  game_round_id
          2  multiplier
          3  ts
          4  total_bets
          5  num_bettors
          6  hash
          7  top1_bet   -- largest individual USDT/USD bet in this round (NULL if none)
        """
        conn = _connect(self._path, read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT r.id, r.game_round_id, r.multiplier, r.ts,
                       r.total_bets, r.num_bettors, r.hash,
                       bq.top1_bet
                FROM rounds r
                LEFT JOIN (
                    SELECT round_id,
                           MAX(CASE WHEN currency IN ('USDT', 'USD') AND amount > 0
                                    THEN amount ELSE NULL END) AS top1_bet
                    FROM bets
                    GROUP BY round_id
                ) bq ON bq.round_id = r.game_round_id
                WHERE r.id > ?
                ORDER BY r.id ASC
                LIMIT ?
                """,
                [min_id, limit],
            ).fetchall()
        finally:
            conn.close()
        return rows

    def close(self) -> None:
        pass
