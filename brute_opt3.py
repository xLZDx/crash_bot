#!/usr/bin/env python3
"""
Brute-force strategy optimizer — multiprocessing version (8 workers).
~90k combinations, ~5 min on 8-core VPS.
"""
import sys, heapq, time
import duckdb
from multiprocessing import Pool, cpu_count

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB         = "data/crash.duckdb"
START_BANK = 100.0
BET_PCT    = 0.0001
BET_CAP    = 0.0005
TARGET_ROI = -999.0
MIN_BETS   = 200
TOP_N      = 10

CASHOUTS           = [1.3, 1.5, 1.8, 2.0, 2.1, 2.3, 2.5, 3.0, 5.0]
LOSS_SCALES        = [1.0, 1.5, 2.0, 3.0]
MAX_SCALES         = [5.0, 10.0, 20.0, 50.0]
CONSEC_TRIGGERS    = [0, 2, 3, 4, 5, 6, 8]
CONSEC_PAUSES      = [5, 11, 21, 42, 63]
THERMAL_WINDOWS    = [0, 5, 8, 10, 15]
THERMAL_THRESHOLDS = [-1, -2, -3, -4, -5, -6]

# ── Global data (shared read-only across workers) ─────────────────────────────
_MULTS     = None
_CUMSCORES = None

def _init_worker(mults, cumscores):
    global _MULTS, _CUMSCORES
    _MULTS     = mults
    _CUMSCORES = cumscores

def simulate_batch(batch):
    """Simulate a list of (params) tuples, return hits."""
    results = []
    for co, ls, ms, ct, cp, tw, tt in batch:
        cs   = _CUMSCORES[co]
        mults = _MULTS
        N    = len(mults)
        bank  = START_BANK
        scale = 1.0
        consec = 0
        cooldown = 0
        total_bets = total_wins = 0
        peak = bank
        max_dd = 0.0
        use_th = tw > 0

        for i in range(N):
            if cooldown > 0:
                cooldown -= 1
                continue
            if use_th and i >= tw:
                score = cs[i] - cs[i - tw]
                if score < tt:
                    continue
            bet = min(bank * BET_PCT * scale, BET_CAP)
            if bet <= 0 or bank < 0.01:
                break
            total_bets += 1
            m = mults[i]
            if m >= co:
                bank  += bet * (co - 1.0)
                total_wins += 1
                scale  = 1.0
                consec = 0
            else:
                bank  -= bet
                consec += 1
                if ls > 1.0:
                    scale = min(scale * ls, ms)
                if ct > 0 and consec >= ct:
                    cooldown = cp
                    scale    = 1.0
                    consec   = 0
            if bank > peak:
                peak = bank
            dd = (peak - bank) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
            if bank < 0.01:
                break

        roi = (bank - START_BANK) / START_BANK * 100.0
        wr  = total_wins / total_bets * 100.0 if total_bets else 0.0
        be  = 100.0 / co

        if roi >= TARGET_ROI and total_bets >= MIN_BETS:
            results.append((roi, co, ls, ms, ct, cp, tw, tt,
                            wr, total_bets, max_dd, be, bank))
    return results


def main():
    # Load data
    print("Loading rounds...", flush=True)
    conn = duckdb.connect(DB, read_only=True)
    mults = [r[0] for r in conn.execute(
        "SELECT multiplier FROM rounds ORDER BY id ASC").fetchall()]
    conn.close()
    N = len(mults)
    print(f"  {N:,} rounds", flush=True)

    # Pre-compute cumscores
    print("Pre-computing cumulative scores...", flush=True)
    cumscores = {}
    for co in CASHOUTS:
        cs = [0] * (N + 1)
        for i, m in enumerate(mults):
            cs[i+1] = cs[i] + (1 if m >= co else -1)
        cumscores[co] = cs

    # Build param list
    params_list = []
    for co in CASHOUTS:
      for ls in LOSS_SCALES:
        ms_vals = MAX_SCALES if ls > 1.0 else [1.0]
        for ms in ms_vals:
          for ct in CONSEC_TRIGGERS:
            cp_vals = CONSEC_PAUSES if ct > 0 else [0]
            for cp in cp_vals:
              for tw in THERMAL_WINDOWS:
                tt_vals = THERMAL_THRESHOLDS if tw > 0 else [0]
                for tt in tt_vals:
                  params_list.append((co, ls, ms, ct, cp, tw, tt))

    total = len(params_list)
    n_workers = min(cpu_count(), 8)
    chunk = max(1, total // (n_workers * 20))  # ~20 chunks per worker
    batches = [params_list[i:i+chunk] for i in range(0, total, chunk)]

    print(f"  {total:,} combinations  |  {n_workers} workers  |  {len(batches)} batches", flush=True)
    print(f"  Target: ROI >= {TARGET_ROI}%  |  min bets: {MIN_BETS}", flush=True)
    print("Running...", flush=True)

    top_heap = []
    n_done   = 0
    n_above  = 0
    t0       = time.time()

    with Pool(n_workers, initializer=_init_worker,
              initargs=(mults, cumscores)) as pool:
        for batch_results in pool.imap_unordered(simulate_batch, batches):
            n_done += chunk
            for entry in batch_results:
                n_above += 1
                roi = entry[0]
                if len(top_heap) < TOP_N:
                    heapq.heappush(top_heap, entry)
                elif roi > top_heap[0][0]:
                    heapq.heapreplace(top_heap, entry)

            # progress
            pct  = min(n_done, total) / total * 100
            rate = n_done / (time.time() - t0)
            eta  = max(0, total - n_done) / rate if rate > 0 else 0
            best = f"{top_heap[0][0]:.1f}%" if top_heap else "n/a"
            print(f"  {min(n_done,total):>7,}/{total:,}  ({pct:5.1f}%)  "
                  f"{rate:,.0f}/s  ETA {eta:.0f}s  found={n_above}  best={best}",
                  flush=True)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s  ({total/elapsed:,.0f} sims/s)", flush=True)
    print(f"Combinations with ROI >= {TARGET_ROI}%: {n_above:,}", flush=True)

    results = sorted(top_heap, reverse=True)
    if not results:
        print(f"\nNo combinations found with ROI >= {TARGET_ROI}%.")
        return

    print()
    print("=" * 118)
    print(f"  TOP {len(results)} STRATEGIES  |  ROI >= {TARGET_ROI}%  |  bets >= {MIN_BETS}")
    print("=" * 118)
    print(f"  {'#':>2}  {'ROI':>7}  {'Final':>7}  {'Cash':>5}  {'Mart':>4}  "
          f"{'MaxSc':>5}  {'CT':>3}  {'CP':>3}  "
          f"{'TW':>3}  {'TT':>3}  "
          f"{'WR%':>7}  {'BEvn%':>6}  {'WR>BE':>6}  {'Bets':>7}  {'MaxDD':>6}")
    print("  " + "-"*114)

    for rank, entry in enumerate(results[:TOP_N], 1):
        roi, co, ls, ms, ct, cp, tw, tt, wr, bets, max_dd, be, final = entry
        above_be = wr - be
        mart_str = f"{ls:.1f}x" if ls > 1.0 else " OFF"
        print(f"  {rank:>2}  {roi:>+7.2f}%  {final:>7.3f}  "
              f"{co:>5.2f}x  {mart_str:>4}  "
              f"{ms:>5.0f}  {ct:>3}  {cp:>3}  "
              f"{tw:>3}  {tt:>3}  "
              f"{wr:>6.2f}%  {be:>5.2f}%  "
              f"{above_be:>+6.2f}%  {bets:>7,}  {max_dd:>5.1f}%")

    print("=" * 118)
    print()
    print("LEGEND: Cash=cashout, Mart=martingale scale (OFF=none), MaxSc=martingale cap,")
    print("        CT=consec_loss_trigger, CP=consec_pause_rounds (~28s each),")
    print("        TW=thermal_window (0=no filter), TT=thermal_threshold,")
    print("        BEvn=break-even WR, WR>BE=WR margin above break-even, MaxDD=max drawdown")


if __name__ == "__main__":
    main()
