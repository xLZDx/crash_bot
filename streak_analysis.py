"""
Streak analysis: find hot/cold periods in historical crash data.

HOT  streak: rolling window where >= THRESHOLD% of rounds hit >= cashout (bots win)
COLD streak: rolling window where >= THRESHOLD% of rounds crash  < cashout (bots lose)

Reports duration stats for each streak type.
"""
import sys
import duckdb
import statistics
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB         = "data/crash.duckdb"
SEC_ROUND  = 28.5          # avg seconds per round
CASHOUTS   = [1.5, 2.0, 3.0, 5.0]
THRESHOLDS = [0.70, 0.80, 0.90]
WINDOW     = 10            # rolling window size in rounds


def fmt_dur(rounds: int) -> str:
    secs = rounds * SEC_ROUND
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs/60:.1f}min"
    return f"{secs/3600:.1f}h"


def find_streaks(wins: list[bool], window: int, threshold: float) -> list[int]:
    """
    Slide a window over the win/loss sequence.
    Return list of streak lengths (in rounds) where the condition holds continuously.
    A streak starts when win_rate >= threshold and ends when it drops below.
    """
    n = len(wins)
    in_streak = False
    streak_len = 0
    streaks = []

    for i in range(n - window + 1):
        w = sum(wins[i:i + window])
        rate = w / window
        if rate >= threshold:
            if not in_streak:
                in_streak  = True
                streak_len = window
            else:
                streak_len += 1
        else:
            if in_streak:
                streaks.append(streak_len)
                in_streak  = False
                streak_len = 0

    if in_streak:
        streaks.append(streak_len)

    return streaks


def streak_stats(streaks: list[int]) -> dict:
    if not streaks:
        return {"count": 0, "avg": 0, "median": 0, "max": 0, "min": 0, "total_rounds": 0}
    return {
        "count":        len(streaks),
        "avg":          statistics.mean(streaks),
        "median":       statistics.median(streaks),
        "max":          max(streaks),
        "min":          min(streaks),
        "total_rounds": sum(streaks),
    }


def main():
    conn = duckdb.connect(DB, read_only=True)
    rows = conn.execute(
        "SELECT multiplier, ts FROM rounds ORDER BY id ASC"
    ).fetchall()
    conn.close()

    mults = [r[0] for r in rows]
    n     = len(mults)
    total_mins = n * SEC_ROUND / 60

    print(f"\n{'='*80}")
    print(f"  STREAK ANALYSIS  --  {n:,} rounds  (~{total_mins/60:.0f}h of data)")
    print(f"  Window: {WINDOW} rounds  |  Round avg: {SEC_ROUND}s")
    print(f"{'='*80}")

    for cashout in CASHOUTS:
        wins  = [m >= cashout for m in mults]
        losses = [not w for w in wins]
        base_wr = sum(wins) / n * 100

        print(f"\n  CASHOUT x{cashout:.1f}  --  base win rate: {base_wr:.1f}%")
        print(f"  {'-'*76}")
        print(f"  {'Threshold':>10}  {'Type':<6}  {'Count':>6}  {'Avg dur':>9}  "
              f"{'Median':>8}  {'Max':>9}  {'Min':>6}  {'Total time':>11}")
        print(f"  {'-'*76}")

        for thr in THRESHOLDS:
            # HOT streaks (wins >= thr)
            hot  = streak_stats(find_streaks(wins,  WINDOW, thr))
            # COLD streaks (losses >= thr, i.e. wins < 1-thr... use loss list)
            cold = streak_stats(find_streaks(losses, WINDOW, thr))

            for label, s in [("HOT", hot), ("COLD", cold)]:
                if s["count"] == 0:
                    print(f"  {thr*100:.0f}%{'':<7}  {label:<6}  {'none':>6}")
                    continue
                print(
                    f"  {thr*100:.0f}%{'':<7}"
                    f"  {label:<6}"
                    f"  {s['count']:>6}"
                    f"  {fmt_dur(int(s['avg'])):>9}"
                    f"  {fmt_dur(int(s['median'])):>8}"
                    f"  {fmt_dur(s['max']):>9}"
                    f"  {fmt_dur(s['min']):>6}"
                    f"  {fmt_dur(s['total_rounds']):>11}"
                )

    # ── Longest individual streaks ─────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  TOP 5 LONGEST STREAKS  (cashout x2.0, 80% threshold, window {WINDOW})")
    print(f"{'='*80}")

    wins2 = [m >= 2.0 for m in mults]
    losses2 = [not w for w in wins2]

    for label, seq in [("HOT (>=80% hit x2)", wins2), ("COLD (>=80% miss x2)", losses2)]:
        all_s = find_streaks(seq, WINDOW, 0.80)
        top5  = sorted(all_s, reverse=True)[:5]
        print(f"\n  {label}")
        for i, s in enumerate(top5, 1):
            print(f"    #{i}: {s} rounds = {fmt_dur(s)}")

    # ── Distribution bucketed ─────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  DURATION DISTRIBUTION  (x2.0, 80% threshold)")
    print(f"{'='*80}")

    for label, seq in [("HOT", wins2), ("COLD", losses2)]:
        streaks = find_streaks(seq, WINDOW, 0.80)
        if not streaks:
            continue
        buckets = {"<30s": 0, "30s-2min": 0, "2-5min": 0, "5-15min": 0,
                   "15-30min": 0, ">30min": 0}
        for s in streaks:
            secs = s * SEC_ROUND
            if   secs < 30:    buckets["<30s"]     += 1
            elif secs < 120:   buckets["30s-2min"]  += 1
            elif secs < 300:   buckets["2-5min"]    += 1
            elif secs < 900:   buckets["5-15min"]   += 1
            elif secs < 1800:  buckets["15-30min"]  += 1
            else:              buckets[">30min"]     += 1
        total = len(streaks)
        print(f"\n  {label} streaks ({total} total):")
        for bucket, cnt in buckets.items():
            bar = "#" * int(cnt / total * 40)
            print(f"    {bucket:>10}: {cnt:>4}  ({cnt/total*100:>4.0f}%)  {bar}")

    print()


if __name__ == "__main__":
    main()
