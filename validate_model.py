import sys
sys.path.insert(0, 'D:/crash_collector')
import duckdb, math

con = duckdb.connect('D:/crash_collector/data/crash.duckdb', read_only=True)
total = con.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]

print(f"=== HOUSE EDGE ESTIMATION from {total} rounds ===\n")

# Gemini assumes: P(M >= x) = (1 - HE) / x
# So: HE = 1 - x * P(M >= x)
# Test multiple thresholds
print(f"{'Threshold':>12} {'Actual hits':>12} {'P(>=x) actual':>15} {'Implied HE':>12} {'Gemini HE=5%':>15} {'Gemini HE=1%':>15}")
print("-" * 90)
for x in [1.2, 1.5, 2.0, 3.0, 5.0, 10.0, 20.0, 50.0, 100.0]:
    hits = con.execute(f"SELECT COUNT(*) FROM rounds WHERE multiplier >= {x}").fetchone()[0]
    p_actual = hits / total
    implied_he = 1 - x * p_actual if p_actual > 0 else None
    p_gemini_5 = 0.95 / x
    p_gemini_1 = 0.99 / x
    he_str = f"{implied_he*100:.1f}%" if implied_he is not None else "N/A"
    print(f"  x{x:>9.1f}  {hits:>12}  {p_actual*100:>13.3f}%  {he_str:>12}  {p_gemini_5*100:>13.3f}%  {p_gemini_1*100:>13.3f}%")

print("\n=== REALITY CHECK ===")
print("If Gemini's formula P(M>=x) = 0.99/x were correct:")
print(f"  Expected P(M>=2)  = {0.99/2*100:.1f}% — but actual = {con.execute('SELECT COUNT(*)*100.0/'+str(total)+' FROM rounds WHERE multiplier>=2').fetchone()[0]:.2f}%")
print(f"  Expected P(M>=10) = {0.99/10*100:.1f}% — but actual = {con.execute('SELECT COUNT(*)*100.0/'+str(total)+' FROM rounds WHERE multiplier>=10').fetchone()[0]:.2f}%")

print("\n=== TRUE DISTRIBUTION FIT ===")
print("Testing power law: P(M>=x) = k / x^n")
# Use x=2 and x=10 to fit k and n
h2  = con.execute("SELECT COUNT(*)*1.0/"+str(total)+" FROM rounds WHERE multiplier>=2").fetchone()[0]
h10 = con.execute("SELECT COUNT(*)*1.0/"+str(total)+" FROM rounds WHERE multiplier>=10").fetchone()[0]
if h2 > 0 and h10 > 0:
    n = math.log(h2/h10) / math.log(10/2)
    k = h2 * (2**n)
    print(f"  Fitted: P(M>=x) = {k:.4f} / x^{n:.4f}")
    print(f"  Predicted P(M>=100)  = {k/100**n*100:.4f}%")
    print(f"  Predicted P(M>=1000) = {k/1000**n*100:.6f}%")
    print(f"  Predicted P(M>=10000)= {k/10000**n*100:.8f}%")
    games_per_10k = 1/(k/10000**n) if k/10000**n > 0 else float('inf')
    print(f"  Expected x10000 every {games_per_10k:.0f} games = {games_per_10k/900:.1f} days")

print("\n=== CORRECTED STRATEGY EV ===")
if h2 > 0 and h10 > 0:
    for target in [100, 1000, 10000, 50000]:
        p = k / (target**n)
        ev_per_bet = p * target * 0.01 - 0.01
        ev_pct = (p * target - 1) * 100
        avg_games = 1/p if p > 0 else float('inf')
        bankroll_1w  = 900 * 7 * 0.01
        bankroll_2w  = 900 * 14 * 0.01
        p_hit_1w = 1 - (1-p)**(900*7)
        p_hit_2w = 1 - (1-p)**(900*14)
        payout = target * 0.01
        print(f"\n  Target x{target}:")
        print(f"    P(hit)          = {p*100:.6f}%  (1 in {avg_games:.0f} games / {avg_games/900:.1f} days)")
        print(f"    EV per $0.01    = ${ev_per_bet:.5f}  ({ev_pct:.2f}% RTP)")
        print(f"    Payout on hit   = ${payout:.2f}")
        print(f"    1-week budget   = ${bankroll_1w:.2f}, P(hit in 1 week) = {p_hit_1w*100:.1f}%")
        print(f"    2-week budget   = ${bankroll_2w:.2f}, P(hit in 2 weeks) = {p_hit_2w*100:.1f}%")

con.close()
