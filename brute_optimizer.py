#!/usr/bin/env python3
"""
Brute-force strategy optimizer.
Tests ~100k+ parameter combinations, shows top 10 with ROI >= 10%.
Thermal filter: score = sum(+1 if mult>=cashout else -1, last N rounds)
               if score < threshold -> COLD -> skip betting
"""
import sys, heapq, time, itertools
import duckdb

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = "data/crash.duckdb"
START_BANK = 100.0
BET_PCT    = 0.0001
BET_CAP    = 0.0005
TARGET_ROI = 10.0
MIN_BETS   = 200
TOP_N      = 10

# ── Parameter grid ──────────────────────────────────────────────────────────
CASHOUTS          = [1.3, 1.5, 1.8, 2.0, 2.1, 2.3, 2.5, 3.0, 5.0]
LOSS_SCALES       = [1.0, 1.5, 2.0, 3.0]
MAX_SCALES        = [5.0, 10.0, 20.0, 50.0]   # only used when loss_scale > 1
CONSEC_TRIGGERS   = [0, 2, 3, 4, 5, 6, 8]
CONSEC_PAUSES     = [5, 11, 21, 42, 63]        # only used when trigger > 0
THERMAL_WINDOWS   = [0, 5, 8, 10, 15]          # 0 = no thermal filter
THERMAL_THRESHOLDS= [-1, -2, -3, -4, -5, -6]  # only used when window > 0

# ── Load rounds ──────────────────────────────────────────────────────────────
print("Loading rounds...", flush=True)
conn = duckdb.connect(DB, read_only=True)
mults = [r[0] for r in conn.execute(
    "SELECT multiplier FROM rounds ORDER BY id ASC").fetchall()]
conn.close()
N = len(mults)
print(f"  {N:,} rounds loaded", flush=True)

# ── Pre-compute cumulative win/loss scores per cashout ───────────────────────
# cumscore[co][i] = prefix sum of +1/-1 up to index i (exclusive)
# window_score at round i with window W = cumscore[i] - cumscore[max(0,i-W)]
print("Pre-computing cumulative scores...", flush=True)
cumscores = {}
for co in CASHOUTS:
    cs = [0] * (N + 1)
    for i, m in enumerate(mults):
        cs[i+1] = cs[i] + (1 if m >= co else -1)
    cumscores[co] = cs

# ── Core simulation ──────────────────────────────────────────────────────────
def simulate(cashout, loss_scale, max_scale,
             consec_trigger, consec_pause,
             thermal_window, thermal_threshold):
    cs   = cumscores[cashout]
    bank = START_BANK
    scale = 1.0
    consec = 0
    cooldown = 0
    total_bets = 0
    total_wins = 0
    peak = bank
    max_dd = 0.0
    use_th = thermal_window > 0

    for i in range(N):
        # cooldown skip
        if cooldown > 0:
            cooldown -= 1
            continue

        # thermal filter  (score < threshold => COLD => skip)
        if use_th and i >= thermal_window:
            score = cs[i] - cs[i - thermal_window]
            if score < thermal_threshold:
                continue

        # place bet
        bet = min(bank * BET_PCT * scale, BET_CAP)
        if bet <= 0 or bank < 0.01:
            break

        total_bets += 1
        m = mults[i]

        if m >= cashout:
            bank += bet * (cashout - 1.0)
            total_wins += 1
            scale  = 1.0
            consec = 0
        else:
            bank  -= bet
            consec += 1
            if loss_scale > 1.0:
                scale = min(scale * loss_scale, max_scale)
            if consec_trigger > 0 and consec >= consec_trigger:
                cooldown = consec_pause
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
    be  = 100.0 / cashout
    return roi, wr, total_bets, max_dd, be, bank

# ── Build full parameter list ────────────────────────────────────────────────
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
print(f"  Total combinations: {total:,}", flush=True)
print(f"  Target: ROI >= {TARGET_ROI}%  |  min bets: {MIN_BETS}", flush=True)
print("Running simulation...", flush=True)

# ── Run ───────────────────────────────────────────────────────────────────────
top_heap   = []   # min-heap (roi, ...)
n_above    = 0
t0         = time.time()
t_print    = t0

for idx, (co, ls, ms, ct, cp, tw, tt) in enumerate(params_list):
    roi, wr, bets, max_dd, be, final = simulate(co, ls, ms, ct, cp, tw, tt)

    if roi >= TARGET_ROI and bets >= MIN_BETS:
        n_above += 1
        entry = (roi, co, ls, ms, ct, cp, tw, tt, wr, bets, max_dd, be, final)
        if len(top_heap) < TOP_N:
            heapq.heappush(top_heap, entry)
        elif roi > top_heap[0][0]:
            heapq.heapreplace(top_heap, entry)

    now = time.time()
    if now - t_print >= 15 or idx == total - 1:
        pct  = (idx+1)/total*100
        rate = (idx+1)/(now-t0)
        eta  = (total - idx - 1) / rate if rate > 0 else 0
        best = f"{top_heap[0][0]:.1f}%" if top_heap else "none"
        print(f"  {idx+1:>7,}/{total:,}  ({pct:5.1f}%)  "
              f"{rate:,.0f}/s  ETA {eta/60:.1f}min  "
              f"found={n_above}  best={best}", flush=True)
        t_print = now

elapsed = time.time() - t0
print(f"\nDone in {elapsed:.1f}s  ({total/elapsed:,.0f} sims/s)")
print(f"Combinations with ROI >= {TARGET_ROI}%: {n_above:,}", flush=True)

# ── Print results ─────────────────────────────────────────────────────────────
results = sorted(top_heap, reverse=True)

if not results:
    print(f"\nNo combinations found with ROI >= {TARGET_ROI}% and bets >= {MIN_BETS}")
    print("Try lowering TARGET_ROI.")
    sys.exit(0)

print()
print("=" * 115)
print(f"  TOP {len(results)} STRATEGIES  |  ROI >= {TARGET_ROI}%  |  bets >= {MIN_BETS}")
print("=" * 115)
hdr = (f"  {'#':>2}  {'ROI':>7}  {'Final':>7}  {'Cash':>5}  {'Mart':>4}  "
       f"{'MaxSc':>5}  {'CT':>3}  {'CP':>3}  "
       f"{'TW':>3}  {'TT':>3}  {'WR':>7}  {'BEvn':>6}  "
       f"{'WR>BE':>6}  {'Bets':>7}  {'MaxDD':>6}")
print(hdr)
print("  " + "-"*111)

for rank, entry in enumerate(results[:TOP_N], 1):
    roi, co, ls, ms, ct, cp, tw, tt, wr, bets, max_dd, be, final = entry
    above_be = wr - be
    mart_str = f"{ls:.1f}" if ls > 1.0 else " off"
    print(f"  {rank:>2}  {roi:>+7.2f}%  {final:>7.3f}  "
          f"{co:>5.2f}x  {mart_str:>4}  "
          f"{ms:>5.0f}  {ct:>3}  {cp:>3}  "
          f"{tw:>3}  {tt:>3}  "
          f"{wr:>6.2f}%  {be:>5.2f}%  "
          f"{above_be:>+6.2f}%  {bets:>7,}  {max_dd:>5.1f}%")

print("=" * 115)
print()
print("LEGEND:")
print("  Cash=cashout, Mart=loss_scale (off=no martingale), MaxSc=max martingale scale")
print("  CT=consec_loss_trigger, CP=consec_loss_pause (rounds)")
print("  TW=thermal_window (0=no filter), TT=thermal_threshold")
print("  BEvn=break-even WR for this cashout, WR>BE=how far above break-even")
print("  MaxDD=max drawdown %")
