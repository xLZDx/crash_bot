import duckdb

con = duckdb.connect('data/crash.duckdb', read_only=True)

print("=== PATTERN AFTER 10x+ ROUND ===")
q = """
WITH numbered AS (
    SELECT multiplier, ts, ROW_NUMBER() OVER (ORDER BY ts) as rn
    FROM rounds
),
big_wins AS (
    SELECT rn FROM numbered WHERE multiplier >= 10.0
),
next_rounds AS (
    SELECT n.multiplier, (n.rn - b.rn) as dist
    FROM numbered n
    JOIN big_wins b ON n.rn BETWEEN b.rn+1 AND b.rn+5
)
SELECT
    dist,
    COUNT(*) as n,
    ROUND(AVG(multiplier), 2) as avg_mult,
    ROUND(100.0*SUM(CASE WHEN multiplier < 2.0 THEN 1 ELSE 0 END)/COUNT(*), 1) as pct_under2
FROM next_rounds
GROUP BY dist
ORDER BY dist
"""
for r in con.execute(q).fetchall():
    print(f"  +{r[0]} round after 10x+:  n={r[1]:4d}  avg={r[2]:7.2f}x  <2x={r[3]}%")


print()
print("=== PATTERN AFTER 3 CONSECUTIVE < 2x ===")
q2 = """
WITH numbered AS (
    SELECT multiplier, ROW_NUMBER() OVER (ORDER BY ts) as rn FROM rounds
),
low AS (SELECT rn FROM numbered WHERE multiplier < 2.0),
streaks AS (
    SELECT a.rn as rn3
    FROM low a
    JOIN low b ON b.rn = a.rn - 1
    JOIN low c ON c.rn = a.rn - 2
),
after AS (
    SELECT n.multiplier
    FROM numbered n JOIN streaks s ON n.rn = s.rn3 + 1
)
SELECT
    COUNT(*) as n,
    ROUND(AVG(multiplier), 2) as avg_mult,
    ROUND(100.0*SUM(CASE WHEN multiplier >= 2.0 THEN 1 ELSE 0 END)/COUNT(*), 1) as pct_above2,
    ROUND(100.0*SUM(CASE WHEN multiplier >= 3.0 THEN 1 ELSE 0 END)/COUNT(*), 1) as pct_above3,
    ROUND(100.0*SUM(CASE WHEN multiplier < 2.0 THEN 1 ELSE 0 END)/COUNT(*), 1) as pct_under2
FROM after
"""
r = con.execute(q2).fetchone()
print(f"  n={r[0]}  avg={r[1]}x  >=2x:{r[2]}%  >=3x:{r[3]}%  <2x:{r[4]}%")

print()
print("=== PATTERN AFTER 5 CONSECUTIVE < 2x ===")
q3 = """
WITH numbered AS (
    SELECT multiplier, ROW_NUMBER() OVER (ORDER BY ts) as rn FROM rounds
),
low AS (SELECT rn FROM numbered WHERE multiplier < 2.0),
streaks AS (
    SELECT a.rn as rn5
    FROM low a
    JOIN low b ON b.rn = a.rn - 1
    JOIN low c ON c.rn = a.rn - 2
    JOIN low d ON d.rn = a.rn - 3
    JOIN low e ON e.rn = a.rn - 4
),
after AS (
    SELECT n.multiplier
    FROM numbered n JOIN streaks s ON n.rn = s.rn5 + 1
)
SELECT
    COUNT(*) as n,
    ROUND(AVG(multiplier), 2) as avg_mult,
    ROUND(100.0*SUM(CASE WHEN multiplier >= 2.0 THEN 1 ELSE 0 END)/COUNT(*), 1) as pct_above2,
    ROUND(100.0*SUM(CASE WHEN multiplier >= 3.0 THEN 1 ELSE 0 END)/COUNT(*), 1) as pct_above3,
    ROUND(100.0*SUM(CASE WHEN multiplier < 2.0 THEN 1 ELSE 0 END)/COUNT(*), 1) as pct_under2
FROM after
"""
r = con.execute(q3).fetchone()
print(f"  n={r[0]}  avg={r[1]}x  >=2x:{r[2]}%  >=3x:{r[3]}%  <2x:{r[4]}%")

print()
print("=== BASE RATE (all rounds) ===")
r = con.execute("""
SELECT
    COUNT(*) as n,
    ROUND(AVG(multiplier), 2) as avg_mult,
    ROUND(100.0*SUM(CASE WHEN multiplier >= 2.0 THEN 1 ELSE 0 END)/COUNT(*), 1) as pct_above2,
    ROUND(100.0*SUM(CASE WHEN multiplier >= 3.0 THEN 1 ELSE 0 END)/COUNT(*), 1) as pct_above3,
    ROUND(100.0*SUM(CASE WHEN multiplier < 2.0 THEN 1 ELSE 0 END)/COUNT(*), 1) as pct_under2
FROM rounds
""").fetchone()
print(f"  n={r[0]}  avg={r[1]}x  >=2x:{r[2]}%  >=3x:{r[3]}%  <2x:{r[4]}%")

print()
print("=== AUTOCORRELATION: does prev round predict next? ===")
q4 = """
WITH numbered AS (
    SELECT multiplier, ROW_NUMBER() OVER (ORDER BY ts) as rn FROM rounds
),
pairs AS (
    SELECT a.multiplier as prev, b.multiplier as curr
    FROM numbered a JOIN numbered b ON b.rn = a.rn + 1
)
SELECT
    CASE WHEN prev < 2.0 THEN 'prev<2x' ELSE 'prev>=2x' END as prev_cat,
    COUNT(*) as n,
    ROUND(100.0*SUM(CASE WHEN curr < 2.0 THEN 1 ELSE 0 END)/COUNT(*), 1) as next_under2_pct,
    ROUND(100.0*SUM(CASE WHEN curr >= 2.0 THEN 1 ELSE 0 END)/COUNT(*), 1) as next_above2_pct,
    ROUND(AVG(curr), 2) as avg_next
FROM pairs
GROUP BY prev_cat
ORDER BY prev_cat
"""
for r in con.execute(q4).fetchall():
    print(f"  {r[0]:12s}  n={r[1]:5d}  next<2x:{r[2]}%  next>=2x:{r[3]}%  avg_next={r[4]}x")

con.close()
