#!/usr/bin/env python3
"""
reset_to_100.py
Flat-reset every paper bot to exactly $100.00 and clear its history, so the
live leaderboard reflects ONLY live play from a common $100 start (NOT the
backtest seed that reset_usd2.py used).

Run with the paper bots STOPPED (DuckDB needs the write lock). Excludes turbo.
Does NOT touch crash.duckdb, the real-money bot, or last_processed_round_id
(so bots resume from NOW, not replay history).
"""
import duckdb, glob, os
from datetime import datetime, timezone

BANK = 100.0
EXCLUDE = {"turbo"}

def main():
    files = sorted(glob.glob("data/bot_state_*.duckdb"))
    done = 0
    for p in files:
        nm = os.path.basename(p).replace("bot_state_", "").replace(".duckdb", "")
        if not nm or nm in EXCLUDE:
            continue
        try:
            with duckdb.connect(p) as db:
                db.execute(
                    """UPDATE strategy_state SET
                        total_bank_sol = ?, session_bank_sol = ?, session_start_sol = ?,
                        session_id = 1, session_rounds = 0, consec_losing_sessions = 0,
                        paused_rounds_skipped = 0, current_scale = 1.0, updated_at = ?
                       WHERE id = 1""",
                    [BANK, BANK, BANK, datetime.now(timezone.utc)],
                )
                db.execute("DELETE FROM strategy_bets")
                db.execute("DELETE FROM strategy_snapshots")
                db.execute("DELETE FROM strategy_sessions")
                bal = db.execute("SELECT total_bank_sol FROM strategy_state WHERE id=1").fetchone()[0]
            print(f"OK  {nm:42} ${bal:.2f}")
            done += 1
        except Exception as e:
            print(f"ERR {nm:42} {str(e)[:70]}")
    print(f"\nreset {done} bots to ${BANK:.2f}")

if __name__ == "__main__":
    main()
