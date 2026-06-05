"""
BCGame crash provably-fair analysis and pattern reporting.

BCGame salt (public):
    0000000000000000000fa3b65e43f4cf0d6c10b7e44240c21315ebe3c571

Hash chain direction (verified from published results):
    H[n-1] = SHA256(H[n])   i.e. each hash is SHA256 of the *next* round's hash.

Crash formula:
    e = HMAC-SHA256(server_hash, salt)[:8]  (first 8 hex chars -> uint32)
    crash = floor(2^32 / (2^32 - e) * (1 - house_edge) * 100) / 100
    house_edge = 0.01
"""

import hashlib
import hmac
import math
import duckdb
from pathlib import Path
from typing import Optional

# ── Constants ────────────────────────────────────────────────────────────────

BCGAME_SALT = "0000000000000000000fa3b65e43f4cf0d6c10b7e44240c21315ebe3c571"
HOUSE_EDGE = 0.01
_MAX32 = 2 ** 32

_STREAK_SQL = """
WITH numbered AS (
    SELECT id, multiplier,
           SUM(CASE WHEN multiplier >= 2.0 THEN 1 ELSE 0 END)
               OVER (ORDER BY id ROWS UNBOUNDED PRECEDING) AS grp
    FROM rounds
),
streaks AS (
    SELECT grp, COUNT(*) AS len
    FROM numbered
    WHERE multiplier < 2.0
    GROUP BY grp
)
SELECT COALESCE(MAX(len), 0) FROM streaks
"""


# ── Provably fair core ────────────────────────────────────────────────────────

def derive_crash_from_hash(server_hash: str, salt: str = BCGAME_SALT) -> Optional[float]:
    """
    Compute the expected crash multiplier from the server hash.
    Returns None if the hash is invalid or produces an instant-bust (<1.00).
    """
    try:
        mac = hmac.new(
            server_hash.encode("ascii"),
            salt.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        e = int(mac[:8], 16)  # first 8 hex chars -> uint32
        if e >= _MAX32:
            return 1.0
        crash_raw = _MAX32 / (_MAX32 - e) * (1 - HOUSE_EDGE)
        return math.floor(crash_raw * 100) / 100
    except Exception:
        return None


def verify_chain_direction(h_prev: str, h_next: str) -> bool:
    """Return True if SHA256(h_next) == h_prev (forward chain: prev = SHA256(next))."""
    return hashlib.sha256(h_next.encode("ascii")).hexdigest() == h_prev.lower()


# ── Bulk chain analysis ───────────────────────────────────────────────────────

def analyze_chain(rows: list) -> dict:
    """
    rows: list of tuples (id, game_round_id, multiplier, ts, total_bets, num_bettors, hash)
    Returns a dict with chain verification stats and formula match %.
    """
    with_hash = [(r[1], r[2], r[6]) for r in rows if r[6]]
    total_with_hash = len(with_hash)

    hash_rows = [r for r in rows if r[6]]
    chain_links = 0
    chain_pairs = 0
    for i in range(len(hash_rows) - 1):
        h_prev = hash_rows[i][6]
        h_next = hash_rows[i + 1][6]
        chain_pairs += 1
        if verify_chain_direction(h_prev, h_next):
            chain_links += 1

    formula_match = 0
    for _, stored_mult, h in with_hash:
        predicted = derive_crash_from_hash(h)
        if predicted is not None and abs(predicted - stored_mult) <= 0.01:
            formula_match += 1

    return {
        "total": len(rows),
        "with_hash": total_with_hash,
        "chain_pairs": chain_pairs,
        "chain_links": chain_links,
        "chain_valid_pct": (chain_links / chain_pairs * 100) if chain_pairs else 0.0,
        "formula_match": formula_match,
        "formula_match_pct": (formula_match / total_with_hash * 100) if total_with_hash else 0.0,
    }


# ── Empirical distribution ────────────────────────────────────────────────────

def empirical_distribution_report(multipliers: list) -> dict:
    """Compare empirical P(X >= x) to the theoretical (1-edge)/x."""
    n = len(multipliers)
    if n == 0:
        return {}
    thresholds = [1.5, 2.0, 3.0, 5.0, 10.0, 50.0, 100.0]
    report = {"n": n, "thresholds": {}}
    for t in thresholds:
        empirical = sum(1 for m in multipliers if m >= t) / n
        theoretical = (1.0 - HOUSE_EDGE) / t
        report["thresholds"][t] = {
            "empirical_pct": round(empirical * 100, 2),
            "theoretical_pct": round(theoretical * 100, 2),
            "delta_pct": round((empirical - theoretical) * 100, 2),
        }
    return report


# ── Streak analysis ───────────────────────────────────────────────────────────

def streak_analysis(multipliers: list, threshold: float = 2.0) -> dict:
    """Compute streak stats: consecutive rounds above/below threshold."""
    if not multipliers:
        return {}

    below_streaks, above_streaks = [], []
    cur_below, cur_above = 0, 0
    for m in multipliers:
        if m < threshold:
            cur_below += 1
            if cur_above:
                above_streaks.append(cur_above)
                cur_above = 0
        else:
            cur_above += 1
            if cur_below:
                below_streaks.append(cur_below)
                cur_below = 0
    if cur_below:
        below_streaks.append(cur_below)
    if cur_above:
        above_streaks.append(cur_above)

    def _stats(lst):
        if not lst:
            return {"max": 0, "avg": 0.0, "count": 0}
        return {"max": max(lst), "avg": round(sum(lst) / len(lst), 2), "count": len(lst)}

    last_m = multipliers[-1]
    return {
        "threshold": threshold,
        "below": _stats(below_streaks),
        "above": _stats(above_streaks),
        "last_round": {"multiplier": last_m, "above_threshold": last_m >= threshold},
    }


# ── Distribution bucketing ────────────────────────────────────────────────────

def bucket_distribution(multipliers: list) -> dict:
    """Return counts per bucket: [1,2), [2,3), [3,5), [5,10), [10,50), [50+)."""
    buckets = {"1-2x": 0, "2-3x": 0, "3-5x": 0, "5-10x": 0, "10-50x": 0, "50x+": 0}
    for m in multipliers:
        if m < 2:
            buckets["1-2x"] += 1
        elif m < 3:
            buckets["2-3x"] += 1
        elif m < 5:
            buckets["3-5x"] += 1
        elif m < 10:
            buckets["5-10x"] += 1
        elif m < 50:
            buckets["10-50x"] += 1
        else:
            buckets["50x+"] += 1
    return buckets


# ── Prediction disclaimer ─────────────────────────────────────────────────────

def predict_next_crash() -> None:
    """
    SHA-256 is one-way: given H[n] you cannot compute H[n+1].
    BCGame pre-commits the full chain; the next hash is unknown until revealed.
    Returns None always — use ML features from training.py instead.
    """
    return None


# ── Legacy CLI (direct DuckDB) ────────────────────────────────────────────────

def analyze(db_path: str, min_rounds: int = 100) -> None:
    if not Path(db_path).exists():
        print(f"Database not found: {db_path}")
        return

    conn = duckdb.connect(db_path, read_only=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
        if n < min_rounds:
            print(f"Only {n} rounds -- need at least {min_rounds} for reliable stats.")
            return

        row = conn.execute("""
            SELECT COUNT(*), MIN(multiplier), MAX(multiplier),
                   AVG(multiplier), MEDIAN(multiplier), STDDEV(multiplier)
            FROM rounds
        """).fetchone()
        n, lo, hi, mean, median, std = row

        w = 57
        print(f"\n{'='*w}")
        print(f"  Crash Statistics   ({n:,} rounds)")
        print(f"{'='*w}")
        print(f"  Range   {lo:.2f}x  --  {hi:.2f}x")
        print(f"  Mean    {mean:.4f}x")
        print(f"  Median  {median:.4f}x")
        print(f"  Std     {std:.4f}")

        instant = conn.execute(
            "SELECT COUNT(*) FROM rounds WHERE multiplier < 1.01"
        ).fetchone()[0]
        print(f"  Instant busts (<1.01x): {instant:,}  ({instant/n*100:.2f}%)")

        thresholds = [1.2, 1.5, 2.0, 2.5, 3.0, 5.0, 10.0, 25.0, 50.0, 100.0]
        print(f"\n{'─'*w}")
        print(f"  Survival probability  P(X >= m)")
        print(f"  {'Target':>8}  {'Empirical':>10}  {'Fair 1/m':>10}  {'Delta':>8}")
        print(f"  {'─'*8}  {'─'*10}  {'─'*10}  {'─'*8}")
        for t in thresholds:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM rounds WHERE multiplier >= ?", [t]
            ).fetchone()[0]
            emp  = cnt / n
            fair = 1.0 / t
            print(f"  {t:>7.1f}x  {emp:>10.4f}  {fair:>10.4f}  {emp-fair:>+8.4f}")

        edges = []
        for t in [2.0, 3.0, 5.0, 10.0]:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM rounds WHERE multiplier >= ?", [t]
            ).fetchone()[0]
            edges.append(1.0 - t * (cnt / n))
        avg_edge = sum(edges) / len(edges)
        print(f"\n  Estimated house edge:  {avg_edge * 100:.2f}%")

        buckets = [
            (1.00, 1.01), (1.01, 1.10), (1.10, 1.50), (1.50, 2.00),
            (2.00, 3.00), (3.00, 5.00), (5.00, 10.0), (10.0, 50.0),
            (50.0, 100.0), (100.0, 1e9),
        ]
        print(f"\n{'─'*w}")
        print(f"  Distribution")
        rows_data, max_pct = [], 0.0
        for lo_b, hi_b in buckets:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM rounds WHERE multiplier >= ? AND multiplier < ?",
                [lo_b, hi_b],
            ).fetchone()[0]
            pct = cnt / n * 100
            max_pct = max(max_pct, pct)
            rows_data.append((lo_b, hi_b, cnt, pct))
        BAR = 28
        for lo_b, hi_b, cnt, pct in rows_data:
            hi_str = f"{hi_b:.0f}" if hi_b < 1e8 else "inf"
            label  = f"[{lo_b:.2f}, {hi_str})"
            bar    = "#" * int(pct / max_pct * BAR if max_pct else 0)
            print(f"  {label:>16}  {cnt:>7,}  {pct:>5.1f}%  {bar}")

        max_streak = conn.execute(_STREAK_SQL).fetchone()[0]
        print(f"\n  Max consecutive rounds < 2x:  {max_streak}")

        # Hash chain analysis
        hash_rows = conn.execute(
            "SELECT id, game_round_id, multiplier, NULL, NULL, NULL, hash "
            "FROM rounds WHERE hash IS NOT NULL ORDER BY id ASC LIMIT 1000"
        ).fetchall()
        if hash_rows:
            chain = analyze_chain(hash_rows)
            print(f"\n{'─'*w}")
            print(f"  Provably Fair Verification")
            print(f"  Rounds with hash:   {chain['with_hash']}")
            print(f"  Chain valid pairs:  {chain['chain_links']}/{chain['chain_pairs']}  ({chain['chain_valid_pct']:.1f}%)")
            print(f"  Formula match:      {chain['formula_match']}/{chain['with_hash']}  ({chain['formula_match_pct']:.1f}%)")

        print(f"{'='*w}\n")

    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    import os
    db = os.path.join(os.path.dirname(__file__), "data", "crash.db")
    analyze(db)
