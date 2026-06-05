import duckdb
import numpy as np

conn = duckdb.connect('data/crash.duckdb', read_only=True)
rows = conn.execute('SELECT multiplier FROM rounds ORDER BY id ASC').fetchall()
conn.close()

mults = [r[0] for r in rows]
n = len(mults)

BET_FRAC   = 0.02   # 2% bankroll per bet
CASHOUT    = 2.0
START_BANK = 5000.0

def simulate(strategy_fn, label):
    bank = START_BANK
    bets = wins = 0
    peak = bank
    max_dd = 0.0
    session_results = []

    for i in range(100, n):
        window = mults[max(0,i-50):i]
        action = strategy_fn(window, mults[i-1], bank)
        if not action:
            continue

        bet = bank * BET_FRAC
        if mults[i] >= CASHOUT:
            bank += bet * (CASHOUT - 1)
            wins += 1
        else:
            bank -= bet
        bets += 1

        if bank > peak:
            peak = bank
        dd = (peak - bank) / peak
        if dd > max_dd:
            max_dd = dd

        if bets % 500 == 0:
            session_results.append(bank)

    wr = wins / bets if bets > 0 else 0
    ev_per_bet = (bank - START_BANK) / bets if bets > 0 else 0
    print(f"\n  [{label}]")
    print(f"    Rounds bet: {bets}  Win rate: {wr*100:.1f}%  (expected 50.3%)")
    print(f"    Final bank: ${bank:,.0f}  (start ${START_BANK:,.0f})")
    print(f"    Net P&L:    ${bank-START_BANK:+,.0f}  ({(bank-START_BANK)/START_BANK*100:+.1f}%)")
    print(f"    Avg EV/bet: ${ev_per_bet:+.3f}")
    print(f"    Max drawdown: {max_dd*100:.1f}%")
    return bank


# Strategy 1: bet every round (flat)
def strat_always(window, last, bank):
    return True

# Strategy 2: bet only after x>=10
def strat_after_x10(window, last, bank):
    return last >= 10.0

# Strategy 3: DON'T bet after 5+ consecutive x>=2 (user's pattern 2)
def strat_avoid_after_streak(window, last, bank):
    streak = 0
    for m in reversed(window[-10:]):
        if m >= 2.0:
            streak += 1
        else:
            break
    return streak < 5  # bet only when no long plus-streak

# Strategy 4: bet only after 5+ consecutive x<2 (contrarian: "polosa zakonchilas")
def strat_after_minus_streak(window, last, bank):
    streak = 0
    for m in reversed(window[-15:]):
        if m < 2.0:
            streak += 1
        else:
            break
    return streak >= 5

# Strategy 5: bet only in "quiet" periods (no x>=10 in last 5 rounds)
def strat_no_recent_big(window, last, bank):
    return all(m < 10.0 for m in window[-5:])

# Strategy 6: bet only AFTER x>=10 (user's pattern 1 - "sleduyuschiy tozhe budet bolshim")
def strat_after_x10_window(window, last, bank):
    return any(m >= 10.0 for m in window[-5:])

print("=" * 60)
print(f"BANKROLL SIMULATION  ({n} rounds, {BET_FRAC*100:.0f}% bet, cashout {CASHOUT}x)")
print("=" * 60)

simulate(strat_always,           "BASELINE: bet every round")
simulate(strat_after_x10,        "PATTERN 1: bet only round after x>=10")
simulate(strat_after_x10_window, "PATTERN 1b: bet if x>=10 in last 5 rounds")
simulate(strat_avoid_after_streak,"PATTERN 2: skip after 5+ x>=2 streak")
simulate(strat_after_minus_streak,"PATTERN 3: bet only after 5+ loss streak")
simulate(strat_no_recent_big,    "PATTERN 5: bet only when no x>=10 in last 5")

print("\n" + "=" * 60)
print("EXPECTED VALUE PER ROUND AT 2.0x CASHOUT:")
wr_real = sum(1 for m in mults if m >= 2.0) / len(mults)
ev = wr_real * 1.0 - (1 - wr_real) * 1.0
print(f"  Win rate: {wr_real*100:.2f}%")
print(f"  EV = {wr_real:.4f}*1 - {1-wr_real:.4f}*1 = {ev:+.4f} per unit bet")
print(f"  House edge: {-ev*100:.2f}%")
print(f"  On $100 bet: expected loss = ${-ev*100:.2f}")
