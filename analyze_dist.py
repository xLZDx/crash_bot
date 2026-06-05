import sys
sys.path.insert(0, 'D:/crash_collector')
import duckdb

con = duckdb.connect('D:/crash_collector/data/crash.duckdb', read_only=True)

print("=== OVERVIEW ===")
r = con.execute("SELECT COUNT(*) as total, MIN(ts) as first, MAX(ts) as last FROM rounds").fetchdf()
print(r.to_string())

print("\n=== GAMES PER DAY ===")
r = con.execute("SELECT CAST(ts AS DATE) as day, COUNT(*) as games FROM rounds GROUP BY 1 ORDER BY 1").fetchdf()
print(r.to_string())

print("\n=== DISTRIBUTION ===")
r = con.execute("""
SELECT range_lbl as range, COUNT(*) as cnt, ROUND(COUNT(*)*100.0/(SELECT COUNT(*) FROM rounds),2) as pct
FROM (
  SELECT
    CASE
      WHEN multiplier < 1.0   THEN 'A: under 1.0'
      WHEN multiplier < 1.5   THEN 'B: 1.00-1.50'
      WHEN multiplier < 2.0   THEN 'C: 1.50-2.00'
      WHEN multiplier < 2.5   THEN 'D: 2.00-2.50'
      WHEN multiplier < 3.0   THEN 'E: 2.50-3.00'
      WHEN multiplier < 5.0   THEN 'F: 3.00-5.00'
      WHEN multiplier < 10.0  THEN 'G: 5.00-10.0'
      WHEN multiplier < 20.0  THEN 'H: 10.0-20.0'
      WHEN multiplier < 50.0  THEN 'I: 20.0-50.0'
      WHEN multiplier < 100.0 THEN 'J: 50.0-100'
      WHEN multiplier < 1000.0 THEN 'K: 100-1000'
      WHEN multiplier < 10000.0 THEN 'L: 1000-10000'
      ELSE                         'M: 10000+'
    END as range_lbl
  FROM rounds
) t
GROUP BY range_lbl
ORDER BY range_lbl
""").fetchdf()
r.columns = ['range', 'count', 'pct%']
print(r.to_string())

print("\n=== HIGH MULTIPLIERS >= 50x ===")
r = con.execute("SELECT multiplier, ts FROM rounds WHERE multiplier >= 50 ORDER BY multiplier DESC").fetchdf()
print(r.to_string())

print("\n=== STRATEGY: bet 1 cent, target X ===")
total = con.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
for target in [2, 5, 10, 50, 100, 1000, 10000]:
    hits = con.execute(f"SELECT COUNT(*) FROM rounds WHERE multiplier >= {target}").fetchone()[0]
    freq = total / hits if hits > 0 else float('inf')
    games_per_day = 900
    cost_per_cycle = freq * 0.01
    payout = target * 0.01
    profit_per_cycle = payout - cost_per_cycle
    days_per_hit = freq / games_per_day
    print(f"  Target x{target:>8}: hits={hits}/{total} ({hits*100/total:.2f}%), "
          f"avg every {freq:.0f} games ({days_per_hit:.2f} days), "
          f"cost/cycle=${cost_per_cycle:.2f}, payout=${payout:.2f}, "
          f"profit/cycle=${profit_per_cycle:.2f}")

con.close()
