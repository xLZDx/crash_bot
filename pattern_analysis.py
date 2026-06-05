"""
Pattern analysis: find sequences in crash history that predict
the next round with 90%+ accuracy.

Converts rounds to binary (1=above threshold, 0=below) and
searches all N-length sequences for predictive patterns.
"""
import sys
import duckdb
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB         = "data/crash.duckdb"
THRESHOLDS = [1.5, 2.0, 3.0]   # test multiple cashout levels
SEQ_LENS   = [3, 4, 5, 6, 7]   # pattern lengths to test
MIN_OCCUR  = 20                 # ignore patterns seen < 20 times
MIN_ACC    = 0.85               # report patterns with >= 85% accuracy

conn = duckdb.connect(DB, read_only=True)
mults = [r[0] for r in conn.execute(
    "SELECT multiplier FROM rounds ORDER BY id ASC").fetchall()]
conn.close()

N = len(mults)
print(f"\nHistorical data: {N:,} rounds")
print(f"Searching for patterns with accuracy >= {MIN_ACC*100:.0f}% "
      f"and min {MIN_OCCUR} occurrences\n")

best_patterns = []

for threshold in THRESHOLDS:
    # Convert to binary sequence
    binary = [1 if m >= threshold else 0 for m in mults]
    win_rate = sum(binary) / N

    print(f"{'='*70}")
    print(f"Threshold: {threshold}x  |  Base win rate: {win_rate*100:.1f}%")
    print(f"{'='*70}")

    found_any = False

    for seq_len in SEQ_LENS:
        # Count what follows each pattern
        pattern_counts  = defaultdict(lambda: [0, 0])  # [next=0, next=1]

        for i in range(N - seq_len):
            pattern = tuple(binary[i:i+seq_len])
            next_val = binary[i+seq_len]
            pattern_counts[pattern][next_val] += 1

        # Find high-accuracy patterns
        results = []
        for pattern, counts in pattern_counts.items():
            total = counts[0] + counts[1]
            if total < MIN_OCCUR:
                continue
            acc_next1 = counts[1] / total   # predict next = WIN
            acc_next0 = counts[0] / total   # predict next = LOSS

            if acc_next1 >= MIN_ACC:
                results.append((pattern, "WIN", acc_next1, total, counts[1]))
            elif acc_next0 >= MIN_ACC:
                results.append((pattern, "LOSS", acc_next0, total, counts[0]))

        results.sort(key=lambda x: x[2], reverse=True)

        if results:
            found_any = True
            print(f"\nPattern length {seq_len} — found {len(results)} patterns:")
            print(f"  {'Pattern':<{seq_len+2}}  {'Predict':>7}  "
                  f"{'Accuracy':>9}  {'Count':>6}  {'Hits':>5}")
            print(f"  {'-'*50}")
            for pattern, pred, acc, total, hits in results[:10]:
                sym = "".join("🟢" if p else "🔴" for p in pattern)
                print(f"  {sym}  {pred:>7}  {acc*100:>8.1f}%  "
                      f"{total:>6}  {hits:>5}")

            # Track best overall
            for pattern, pred, acc, total, hits in results:
                best_patterns.append((threshold, seq_len, pattern, pred, acc, total))

    if not found_any:
        print(f"  No patterns >= {MIN_ACC*100:.0f}% found\n")

# === Summary ===
print(f"\n{'='*70}")
print(f"TOP 20 BEST PATTERNS ACROSS ALL THRESHOLDS")
print(f"{'='*70}")
best_patterns.sort(key=lambda x: x[4], reverse=True)
print(f"  {'Threshold':>9}  {'Len':>3}  {'Pattern':<10}  "
      f"{'Predict':>7}  {'Accuracy':>9}  {'Count':>6}")
print(f"  {'-'*60}")
for thr, slen, pattern, pred, acc, total in best_patterns[:20]:
    sym = "".join("G" if p else "O" for p in pattern)
    print(f"  {thr:>8.1f}x  {slen:>3}  {sym:<10}  "
          f"{pred:>7}  {acc*100:>8.1f}%  {total:>6}")

print()

# === Statistical baseline check ===
print(f"{'='*70}")
print("STATISTICAL NOTE:")
print(f"With {N:,} rounds and many patterns tested, some will hit")
print(f"90%+ by CHANCE alone (multiple testing problem).")
print(f"A pattern with 90% accuracy and only 20-50 occurrences")
print(f"is NOT reliable — need 200+ occurrences to trust it.")
print(f"BCGame uses HMAC-SHA256 — rounds are INDEPENDENT.")
print(f"Any pattern is statistical noise, not a real edge.")
print(f"{'='*70}\n")
