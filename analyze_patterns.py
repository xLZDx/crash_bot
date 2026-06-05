import duckdb
import numpy as np

conn = duckdb.connect('data/crash.duckdb', read_only=True)
rows = conn.execute('SELECT multiplier FROM rounds ORDER BY id ASC').fetchall()
conn.close()

mults = [r[0] for r in rows]
n = len(mults)

print(f"=== ANALYSIS: {n} BCGAME ROUNDS ===\n")

p_plus  = sum(1 for m in mults if m >= 2.0) / n
p_minus = 1 - p_plus
p_x10   = sum(1 for m in mults if m >= 10.0) / n
p_x5    = sum(1 for m in mults if m >= 5.0) / n

print("-- BASE PROBABILITIES (any random round) --")
print(f"  P(x >= 2.0)  = {p_plus*100:.1f}%")
print(f"  P(x >= 5.0)  = {p_x5*100:.1f}%")
print(f"  P(x >= 10.0) = {p_x10*100:.1f}%")
print(f"  P(x <  2.0)  = {p_minus*100:.1f}%")
print()

print("-- PATTERN 1: after x>=10, probability of another x>=10 in next N rounds --")
for window in [1, 2, 3, 5, 10]:
    hits = 0
    total = 0
    for i in range(n - window):
        if mults[i] >= 10.0:
            total += 1
            if any(mults[i+k] >= 10.0 for k in range(1, window+1)):
                hits += 1
    if total > 0:
        p_cond = hits / total
        lift = p_cond / p_x10 if p_x10 > 0 else 0
        # baseline: P(at least one x10 in N independent rounds) = 1-(1-p_x10)^N
        p_base_window = 1 - (1 - p_x10)**window
        print(f"  Window {window:2d}: {p_cond*100:.1f}%  (base for {window} rounds: {p_base_window*100:.1f}%,  lift vs base: {p_cond/p_base_window:.2f}x)  n={total}")
print()

print("-- PATTERN 2: after N consecutive x>=2, probability next round is x<2 --")
for streak_len in [3, 5, 7, 10]:
    hits = 0
    total = 0
    streak = 0
    for i in range(n - 1):
        if mults[i] >= 2.0:
            streak += 1
        else:
            streak = 0
        if streak >= streak_len:
            total += 1
            if mults[i+1] < 2.0:
                hits += 1
    if total > 0:
        p_cond = hits / total
        lift = p_cond / p_minus
        print(f"  After {streak_len:2d}+ x>=2: P(next < 2.0) = {p_cond*100:.1f}%  (base {p_minus*100:.1f}%, lift {lift:.2f}x)  n={total}")
print()

print("-- PATTERN 3: after N consecutive x<2, probability next is also x<2 --")
for streak_len in [3, 5, 7, 10, 15]:
    hits = 0
    total = 0
    streak = 0
    for i in range(n - 1):
        if mults[i] < 2.0:
            streak += 1
        else:
            streak = 0
        if streak >= streak_len:
            total += 1
            if mults[i+1] < 2.0:
                hits += 1
    if total > 0:
        p_cond = hits / total
        lift = p_cond / p_minus
        print(f"  After {streak_len:2d}+ x<2:   P(next < 2.0) = {p_cond*100:.1f}%  (base {p_minus*100:.1f}%, lift {lift:.2f}x)  n={total}")
print()

print("-- AUTOCORRELATION TEST ('system balances itself') --")
arr = np.array(mults)
log_arr = np.log(arr)
for lag in [1, 2, 3, 5, 10, 20]:
    corr = np.corrcoef(log_arr[:-lag], log_arr[lag:])[0, 1]
    print(f"  corr(log_mult[i], log_mult[i+{lag:2d}]) = {corr:+.4f}")
print()

print("-- KEY TEST: average of NEXT round after a big x --")
for threshold in [5.0, 10.0, 20.0, 50.0]:
    after_big = [mults[i+1] for i in range(n-1) if mults[i] >= threshold]
    if after_big:
        print(f"  After x>={threshold:.0f}: avg_next={np.mean(after_big):.2f}  median_next={np.median(after_big):.2f}  n={len(after_big)}")
print(f"  Any round:        avg={np.mean(arr):.2f}  median={np.median(arr):.2f}")
print()

print("-- CONSECUTIVE STREAKS DISTRIBUTION --")
plus_streaks = []
minus_streaks = []
cur_plus = cur_minus = 0
for m in mults:
    if m >= 2.0:
        cur_plus += 1
        if cur_minus > 0:
            minus_streaks.append(cur_minus)
            cur_minus = 0
    else:
        cur_minus += 1
        if cur_plus > 0:
            plus_streaks.append(cur_plus)
            cur_plus = 0

print(f"  x>=2 streaks: max={max(plus_streaks)}, avg={np.mean(plus_streaks):.1f}, "
      f">=5: {sum(1 for s in plus_streaks if s>=5)}, "
      f">=10: {sum(1 for s in plus_streaks if s>=10)}")
print(f"  x<2  streaks: max={max(minus_streaks)}, avg={np.mean(minus_streaks):.1f}, "
      f">=5: {sum(1 for s in minus_streaks if s>=5)}, "
      f">=10: {sum(1 for s in minus_streaks if s>=10)}, "
      f">=15: {sum(1 for s in minus_streaks if s>=15)}")
