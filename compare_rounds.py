import sys
sys.path.insert(0, 'D:/crash_collector')
import duckdb

con = duckdb.connect('D:/crash_collector/data/crash.duckdb', read_only=True)

print("=== ROUNDS FROM SCREENSHOT (9258255-9258270) ===")
r = con.execute("""
    SELECT game_round_id, multiplier, ts
    FROM rounds
    WHERE TRY_CAST(game_round_id AS BIGINT) BETWEEN 9258255 AND 9258270
    ORDER BY TRY_CAST(game_round_id AS BIGINT)
""").fetchdf()
print(r.to_string() if len(r) > 0 else "  NOT FOUND in our DB")

print("\n=== LATEST 20 ROUNDS IN OUR DB ===")
r = con.execute("SELECT game_round_id, multiplier, ts FROM rounds ORDER BY ts DESC LIMIT 20").fetchdf()
print(r.to_string())

print("\n=== TOP 10 MOST COMMON MULTIPLIER VALUES ===")
r = con.execute("""
    SELECT multiplier, COUNT(*) as cnt, ROUND(COUNT(*)*100.0/(SELECT COUNT(*) FROM rounds),2) as pct
    FROM rounds GROUP BY multiplier ORDER BY cnt DESC LIMIT 10
""").fetchdf()
print(r.to_string())

print("\n=== SCREENSHOT ROUNDS vs EXPECTED ===")
screenshot = {
    "9258260": "1.37x",
    "9258261": "14.73x",
    "9258262": "11.67x",
    "9258263": "6.04x",
    "9258264": "1.34x",
    "9258265": "1.20x",
    "9258266": "10.31x",
}
print(f"{'Round ID':<12} {'Screenshot':>12} {'Our DB':>12} {'Match?':>8}")
print("-" * 48)
for rid, expected in screenshot.items():
    row = con.execute(f"SELECT multiplier FROM rounds WHERE game_round_id = '{rid}'").fetchone()
    our_val = f"{row[0]:.2f}x" if row else "MISSING"
    match = "YES" if row and abs(row[0] - float(expected.replace('x',''))) < 0.1 else "NO" if row else "MISSING"
    print(f"  {rid:<12} {expected:>12} {our_val:>12} {match:>8}")

con.close()
