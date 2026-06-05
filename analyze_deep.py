"""
Deep analysis of BCGame crash data.
Explores: time patterns, round duration, bettor volume vs outcome,
distribution fit, volatility clustering, session simulation.
"""
import duckdb
import numpy as np
from scipy import stats
from collections import defaultdict
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

conn = duckdb.connect('data/crash.duckdb', read_only=True)
rows = conn.execute(
    'SELECT id, multiplier, ts, total_bets, num_bettors '
    'FROM rounds ORDER BY id ASC'
).fetchall()
conn.close()

ids      = [r[0] for r in rows]
mults    = [r[1] for r in rows]
ts       = [r[2] for r in rows]
tot_bets = [r[3] for r in rows]  # may be None
n_bet    = [r[4] for r in rows]  # may be None
n = len(mults)
arr = np.array(mults)

print(f"Dataset: {n} rounds  |  {ts[0].date()} -> {ts[-1].date()}\n")

# ==========================================================
# SECTION 1: DISTRIBUTION FIT
# ==========================================================
print("=" * 60)
print("1. MULTIPLIER DISTRIBUTION")
print("=" * 60)

buckets = [(1.0,1.5),(1.5,2.0),(2.0,3.0),(3.0,5.0),(5.0,10.0),
           (10.0,20.0),(20.0,50.0),(50.0,100.0),(100.0,1e9)]
for lo, hi in buckets:
    cnt = sum(1 for m in mults if lo <= m < hi)
    pct = cnt/n*100
    bar = "#" * int(pct/0.5)
    print(f"  [{lo:6.1f}, {hi:6.1f}): {cnt:5d}  {pct:5.1f}%  {bar}")

log_arr = np.log(arr)
print(f"\n  log(mult): mean={log_arr.mean():.4f}  std={log_arr.std():.4f}")
print(f"  Skew: {stats.skew(log_arr):.4f}   Kurtosis: {stats.kurtosis(log_arr):.4f}")

# Power law: P(X > x) ~ x^(-alpha)  =>  alpha ~ 1/mean(log x) for x > x_min
x_min = 2.0
tail = [m for m in mults if m >= x_min]
alpha_hat = 1 + len(tail) / sum(np.log(m / x_min) for m in tail)
print(f"\n  Power law fit (x >= {x_min}): alpha = {alpha_hat:.3f}")
print(f"  Predicted P(x>=10) = {x_min**alpha_hat / 10**(alpha_hat-1) / x_min:.3f}  "
      f"Actual P(x>=10) = {sum(1 for m in mults if m>=10)/n:.3f}")

# ==========================================================
# SECTION 2: TIME PATTERNS
# ==========================================================
print()
print("=" * 60)
print("2. TIME-OF-DAY PATTERNS (local time)")
print("=" * 60)

# Round duration
durations = []
for i in range(1, n):
    dt = (ts[i] - ts[i-1]).total_seconds()
    if 0 < dt < 300:  # ignore gaps > 5 min (collector restarts)
        durations.append(dt)
dur = np.array(durations)
print(f"\n  Round duration: mean={dur.mean():.1f}s  median={np.median(dur):.1f}s  "
      f"p95={np.percentile(dur,95):.1f}s")

# Hour-of-day win rate
hour_wins = defaultdict(list)
for i, (m, t) in enumerate(zip(mults, ts)):
    h = t.hour
    hour_wins[h].append(1 if m >= 2.0 else 0)

print("\n  Win rate by hour (local):")
base_wr = sum(1 for m in mults if m >= 2.0) / n
for h in sorted(hour_wins):
    wins = hour_wins[h]
    if len(wins) < 30:
        continue
    wr = sum(wins) / len(wins)
    dev = wr - base_wr
    flag = "  <-- " if abs(dev) > 0.03 else ""
    print(f"    {h:02d}:00  n={len(wins):4d}  wr={wr*100:.1f}%  ({dev:+.1f}%){flag}")

# Day-of-week
dow_wins = defaultdict(list)
days = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
for m, t in zip(mults, ts):
    dow_wins[t.weekday()].append(1 if m >= 2.0 else 0)

print("\n  Win rate by day of week:")
for d in sorted(dow_wins):
    wins = dow_wins[d]
    if len(wins) < 20:
        continue
    wr = sum(wins) / len(wins)
    print(f"    {days[d]}  n={len(wins):4d}  wr={wr*100:.1f}%  ({wr-base_wr:+.1f}%)")

# ==========================================================
# SECTION 3: BETTOR VOLUME vs OUTCOME
# ==========================================================
print()
print("=" * 60)
print("3. NUM BETTORS vs OUTCOME  (crowd signal?)")
print("=" * 60)

valid = [(m, b) for m, b in zip(mults, n_bet) if b is not None and b > 0]
if len(valid) > 100:
    vm = [x[0] for x in valid]
    vb = [x[1] for x in valid]
    corr = np.corrcoef(vb, vm)[0,1]
    corr_log = np.corrcoef(vb, np.log(vm))[0,1]
    print(f"\n  n={len(valid)} rounds with bettor data")
    print(f"  corr(num_bettors, mult)      = {corr:+.4f}")
    print(f"  corr(num_bettors, log(mult)) = {corr_log:+.4f}")

    # Quartile breakdown
    q25, q50, q75 = np.percentile(vb, [25, 50, 75])
    print(f"\n  Bettor quartile breakdown (Q1<={q25:.0f}, Q4>{q75:.0f}):")
    for label, lo, hi in [("Q1 (few)",0,q25),("Q2",q25,q50),("Q3",q50,q75),("Q4 (many)",q75,1e9)]:
        subset = [m for m,b in valid if lo < b <= hi]
        if subset:
            wr = sum(1 for m in subset if m>=2.0)/len(subset)
            avg_x10 = sum(1 for m in subset if m>=10.0)/len(subset)
            print(f"    {label:14s}  n={len(subset):4d}  wr={wr*100:.1f}%  P(x>=10)={avg_x10*100:.1f}%")
else:
    print(f"\n  Only {len(valid)} rounds have bettor data -- insufficient")

# ==========================================================
# SECTION 4: VOLATILITY CLUSTERING
# ==========================================================
print()
print("=" * 60)
print("4. VOLATILITY CLUSTERING  (big after big?)")
print("=" * 60)

log_mults = np.log(arr)
# |log-returns| as proxy for "volatility"
abs_log = np.abs(log_mults)
# Rolling 10-round std
win = 10
roll_std = [np.std(log_mults[max(0,i-win):i]) for i in range(win, n)]
roll_std = np.array(roll_std)

# Is current round more volatile if previous window was volatile?
corr_vs = np.corrcoef(roll_std[:-1], abs_log[win+1:])[0,1]
print(f"\n  corr(rolling_std[i], |log_mult[i+1]|) = {corr_vs:+.4f}")
print(f"  (>0 = volatility clusters; ~0 = independent)")

# High-vol vs low-vol windows: win rates
med_std = np.median(roll_std)
high_vol_idx = np.where(roll_std > med_std)[0]
low_vol_idx  = np.where(roll_std <= med_std)[0]
hv_wr = np.mean([1 if mults[win+i+1]>=2.0 else 0 for i in high_vol_idx if win+i+1 < n])
lv_wr = np.mean([1 if mults[win+i+1]>=2.0 else 0 for i in low_vol_idx  if win+i+1 < n])
print(f"  Win rate after HIGH-vol window:  {hv_wr*100:.1f}%")
print(f"  Win rate after LOW-vol  window:  {lv_wr*100:.1f}%")
print(f"  P(x>=10) after HIGH-vol:  {np.mean([1 if mults[win+i+1]>=10.0 else 0 for i in high_vol_idx if win+i+1<n])*100:.1f}%")
print(f"  P(x>=10) after LOW-vol:   {np.mean([1 if mults[win+i+1]>=10.0 else 0 for i in low_vol_idx  if win+i+1<n])*100:.1f}%")

# ==========================================================
# SECTION 5: OPTIMAL CASHOUT TARGETS
# ==========================================================
print()
print("=" * 60)
print("5. OPTIMAL CASHOUT EV AT DIFFERENT TARGETS")
print("=" * 60)

print("\n  At 50.29% base win rate:")
for target in [1.5, 1.7, 1.9, 2.0, 2.1, 2.5, 3.0, 4.0, 5.0, 10.0]:
    wr = sum(1 for m in mults if m >= target) / n
    ev = wr * (target - 1) - (1 - wr) * 1
    print(f"    cashout {target:5.1f}x:  win_rate={wr*100:.1f}%  EV={ev:+.4f}  "
          f"({'POSITIVE' if ev > 0 else 'negative'})")

# ==========================================================
# SECTION 6: SESSION MANAGEMENT SIMULATION
# ==========================================================
print()
print("=" * 60)
print("6. SESSION MANAGEMENT SIMULATION")
print("=" * 60)

CASHOUT       = 2.0
SESSION_START = 500.0   # session bank = 10% of total
TOTAL_START   = 5000.0
BET_PCT       = 0.02    # 2% of session bank per bet

def run_session_sim(stop_loss_pct, stop_profit_pct, max_rounds=200):
    """Simulate sessions with stop-loss and stop-profit rules."""
    total_bank = TOTAL_START
    total_sessions = 0
    total_rounds_bet = 0
    peak = total_bank

    round_idx = 100  # start after warmup
    while round_idx < n - 10 and total_bank > SESSION_START:
        # Start a new session
        session_bank = min(SESSION_START, total_bank * 0.10)
        session_start = session_bank
        session_rounds = 0

        while (session_rounds < max_rounds and
               session_bank > session_start * (1 - stop_loss_pct) and
               session_bank < session_start * (1 + stop_profit_pct) and
               round_idx < n):

            bet = session_bank * BET_PCT
            if mults[round_idx] >= CASHOUT:
                session_bank += bet * (CASHOUT - 1)
            else:
                session_bank -= bet
            session_rounds += 1
            round_idx += 1
            total_rounds_bet += 1

        total_bank = total_bank - session_start + session_bank
        total_sessions += 1
        if total_bank > peak:
            peak = total_bank

    pnl = total_bank - TOTAL_START
    max_dd = (peak - total_bank) / peak if peak > 0 else 0
    return {
        "final": total_bank,
        "pnl_pct": pnl / TOTAL_START * 100,
        "sessions": total_sessions,
        "rounds": total_rounds_bet,
        "max_dd": max_dd * 100
    }

print(f"\n  Session bank: ${SESSION_START:.0f} (10% of total)")
print(f"  Bet size: {BET_PCT*100:.0f}% of session bank")
print(f"  Cashout: {CASHOUT}x\n")
print(f"  {'SL':>5} {'SP':>5}  {'Final':>8}  {'P&L':>8}  {'Sessions':>8}  {'Rounds':>7}  {'MaxDD':>7}")
print(f"  {'-'*5} {'-'*5}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*7}  {'-'*7}")

results = []
for sl in [0.05, 0.10, 0.15, 0.20]:
    for sp in [0.10, 0.20, 0.30, 0.50]:
        r = run_session_sim(sl, sp)
        results.append((sl, sp, r))
        flag = " <-- best" if r["pnl_pct"] > 0 else ""
        print(f"  {sl*100:4.0f}% {sp*100:4.0f}%  "
              f"${r['final']:>7,.0f}  {r['pnl_pct']:>+7.1f}%  "
              f"{r['sessions']:>8d}  {r['rounds']:>7d}  "
              f"{r['max_dd']:>6.1f}%{flag}")

best = max(results, key=lambda x: x[2]["final"])
print(f"\n  Best combo: SL={best[0]*100:.0f}% SP={best[1]*100:.0f}%  "
      f"Final=${best[2]['final']:,.0f}  P&L={best[2]['pnl_pct']:+.1f}%")

# ==========================================================
# SECTION 7: CONDITIONAL MULTI-FACTOR PATTERNS
# ==========================================================
print()
print("=" * 60)
print("7. COMBINED CONDITIONS  (do combos have more lift?)")
print("=" * 60)

def cond_wr(condition_fn, label, min_n=30):
    hits = total = 0
    for i in range(20, n-1):
        window = mults[max(0,i-20):i]
        if condition_fn(window, mults[i]):
            total += 1
            if mults[i+1] >= 2.0:
                hits += 1
    if total >= min_n:
        wr = hits / total
        print(f"  {label:55s}  n={total:5d}  wr={wr*100:.1f}%  ({wr-base_wr:+.1f}%)")
    else:
        print(f"  {label:55s}  n={total:5d}  (too few)")

print(f"\n  Base win rate: {base_wr*100:.1f}%\n")

# Individual conditions
cond_wr(lambda w,last: last >= 10.0,
        "After x>=10")
cond_wr(lambda w,last: last < 2.0 and sum(1 for m in w[-5:] if m<2.0)==5,
        "After 5 consecutive losses (last is loss)")
cond_wr(lambda w,last: last >= 2.0 and sum(1 for m in w[-5:] if m>=2.0)==5,
        "After 5 consecutive wins")

# Combined conditions
cond_wr(lambda w,last: last >= 10.0 and sum(1 for m in w[-3:] if m>=2.0) >= 2,
        "x>=10 AND last 3 mostly wins")
cond_wr(lambda w,last: last >= 10.0 and sum(1 for m in w[-3:] if m<2.0) >= 2,
        "x>=10 AND last 3 mostly losses")
cond_wr(lambda w,last: last < 2.0 and sum(1 for m in w[-10:] if m>=10.0) >= 1,
        "Loss AND x>=10 appeared in last 10")
cond_wr(lambda w,last: last < 2.0 and sum(1 for m in w[-10:] if m>=10.0) == 0,
        "Loss AND no x>=10 in last 10")
cond_wr(lambda w,last: np.std(np.log([m for m in w[-5:] if m>0]+[0.01])) > 1.0,
        "High volatility last 5 rounds (std log > 1.0)")
cond_wr(lambda w,last: np.std(np.log([m for m in w[-5:] if m>0]+[0.01])) < 0.3,
        "Low volatility last 5 rounds (std log < 0.3)")
cond_wr(lambda w,last: sum(1 for m in w[-3:] if m>=10.0) >= 1,
        "x>=10 in last 3 rounds")
cond_wr(lambda w,last: all(m < 2.0 for m in w[-3:]),
        "3 consecutive losses")
cond_wr(lambda w,last: all(m >= 2.0 for m in w[-8:]),
        "8 consecutive wins")
cond_wr(lambda w,last: all(m < 2.0 for m in w[-8:]),
        "8 consecutive losses")

# ==========================================================
# SECTION 8: ROUND DURATION vs OUTCOME
# ==========================================================
print()
print("=" * 60)
print("8. ROUND DURATION vs OUTCOME  (does long round = high mult?)")
print("=" * 60)

dur_mult = []
for i in range(1, n):
    dt = (ts[i] - ts[i-1]).total_seconds()
    if 5 < dt < 120:  # clean rounds only
        dur_mult.append((dt, mults[i]))

if dur_mult:
    dvals = [x[0] for x in dur_mult]
    mvals = [x[1] for x in dur_mult]
    corr_d = np.corrcoef(dvals, mvals)[0,1]
    corr_dlog = np.corrcoef(dvals, np.log(mvals))[0,1]
    print(f"\n  n={len(dur_mult)} clean rounds")
    print(f"  corr(duration, mult)      = {corr_d:+.4f}")
    print(f"  corr(duration, log(mult)) = {corr_dlog:+.4f}")

    # Duration quartile breakdown
    q33, q66 = np.percentile(dvals, [33, 66])
    print(f"\n  Duration tercile breakdown:")
    for label, lo, hi in [("Short (<{:.0f}s)".format(q33),0,q33),
                           ("Medium".format(q33,q66),q33,q66),
                           ("Long (>{:.0f}s)".format(q66),q66,1e9)]:
        subset = [m for d,m in dur_mult if lo <= d < hi]
        if subset:
            wr = sum(1 for m in subset if m>=2.0)/len(subset)
            x10 = sum(1 for m in subset if m>=10.0)/len(subset)
            print(f"    {label:20s}  n={len(subset):4d}  wr={wr*100:.1f}%  P(x>=10)={x10*100:.1f}%")

print()
print("=" * 60)
print("ANALYSIS COMPLETE")
print("=" * 60)
