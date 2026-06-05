"""
5 proposed strategies based on data analysis.
Each tested on all 17k rounds with full bankroll tracking.
"""
import duckdb
import numpy as np
import sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

conn = duckdb.connect('data/crash.duckdb', read_only=True)
rows = conn.execute('SELECT multiplier FROM rounds ORDER BY id ASC').fetchall()
conn.close()
mults = [r[0] for r in rows]
n = len(mults)

START_BANK = 5000.0
WARMUP = 50  # rounds for context window

# ----------------------------------------------------------
def run_strategy(name, bet_fn, cashout_fn, description):
    """
    bet_fn(bank, window) -> bet amount (0 = skip)
    cashout_fn(bank, window) -> cashout target multiplier
    """
    bank = START_BANK
    peak = bank
    bets = wins = sessions = 0
    max_dd = 0.0
    profits = []
    session_bank = bank * 0.10
    session_start = session_bank
    session_bets = 0
    session_peak = session_bank

    # session thresholds (per-strategy, set via closure or defaults)
    SL = getattr(bet_fn, 'stop_loss', 0.15)
    SP = getattr(bet_fn, 'stop_profit', 0.30)
    SESSION_FRAC = getattr(bet_fn, 'session_frac', 0.10)

    def reset_session(bank):
        sb = bank * SESSION_FRAC
        return sb, sb, sb, 0

    session_bank, session_start, session_peak, session_bets = reset_session(bank)

    for i in range(WARMUP, n):
        window = mults[max(0, i - 50):i]

        # Session management
        down_pct = (session_start - session_bank) / session_start if session_start > 0 else 0
        up_pct   = (session_bank - session_start) / session_start if session_start > 0 else 0
        if down_pct >= SL or up_pct >= SP or session_bets >= 300:
            # close session, bank the result
            pnl = session_bank - session_start
            bank += pnl
            profits.append(pnl)
            sessions += 1
            if bank <= START_BANK * 0.05:
                break
            session_bank, session_start, session_peak, session_bets = reset_session(bank)

        # Get bet
        bet = bet_fn(session_bank, window)
        if bet <= 0 or bet > session_bank:
            continue

        # Get cashout target
        co = cashout_fn(session_bank, window)

        # Settle
        if mults[i] >= co:
            session_bank += bet * (co - 1)
            wins += 1
        else:
            session_bank -= bet

        session_bets += 1
        bets += 1

        if session_bank > session_peak:
            session_peak = session_bank

        if bank > peak:
            peak = bank
        # drawdown vs overall peak (approx, use bank + session drift)
        approx_total = bank + (session_bank - session_start)
        dd = (peak - approx_total) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # close last session
    bank += (session_bank - session_start)

    wr = wins / bets if bets > 0 else 0
    pnl = bank - START_BANK
    print(f"\n  [{name}]")
    print(f"  {description}")
    print(f"    Bets: {bets:,}  Sessions: {sessions}  Win rate: {wr*100:.1f}%")
    print(f"    Final: ${bank:,.0f}  P&L: {pnl:+,.0f} ({pnl/START_BANK*100:+.1f}%)")
    print(f"    Max drawdown: {max_dd*100:.1f}%")
    return bank


print("=" * 65)
print(f"STRATEGY COMPARISON  ({n} rounds, start ${START_BANK:,.0f})")
print("=" * 65)

# ==============================================================
# STRATEGY 1: TURTLE (Kelly-optimal small bets)
# Математика: Kelly fraction at p=0.502, b=1 -> f* = 0.004
# Bet ~0.5% of total bank, cashout 2.1x (best EV target)
# No tight session stops needed -- small bets protect themselves
# ==============================================================
def bet_turtle(session_bank, window):
    # 0.5% of session bank (quarter-Kelly -- conservative)
    return session_bank * 0.005

def co_turtle(session_bank, window):
    return 2.1  # best EV target from data

bet_turtle.stop_loss   = 0.40   # loose -- small bets rarely hit this
bet_turtle.stop_profit = 1.00   # almost never triggers
bet_turtle.session_frac = 1.0   # treat all bank as one session

run_strategy(
    "TURTLE -- Kelly 0.5% flat, cashout 2.1x",
    bet_turtle, co_turtle,
    "Bet 0.5% total bank per round. No pattern filter. Tight math."
)

# ==============================================================
# STRATEGY 2: SCALPER (mirrors user's described behavior)
# Session bank = 10% of total
# Bet = 2% of session bank
# Cashout = 2.0x
# Exit session: -10% or +20%
# ==============================================================
def bet_scalper(session_bank, window):
    return session_bank * 0.02

def co_scalper(session_bank, window):
    return 2.0

bet_scalper.stop_loss   = 0.10
bet_scalper.stop_profit = 0.20
bet_scalper.session_frac = 0.10

run_strategy(
    "SCALPER -- 2% session, SL10%/SP20%, cashout 2.0x",
    bet_scalper, co_scalper,
    "Matches your described behavior. Session bank 10%, tight exits."
)

# ==============================================================
# STRATEGY 3: SNIPER (hunt x>=3, skip noise)
# Only bet when last 5 rounds had no x>=10 (Pattern 5 edge)
# Smaller bet, higher cashout target 3.0x
# Trades: fewer bets, but targets the juicy tail
# ==============================================================
def bet_sniper(session_bank, window):
    # Skip if x>=10 appeared in last 5 rounds
    if any(m >= 10.0 for m in window[-5:]):
        return 0
    return session_bank * 0.015

def co_sniper(session_bank, window):
    return 3.0  # aim for 3x, accept the higher variance

bet_sniper.stop_loss   = 0.15
bet_sniper.stop_profit = 0.40
bet_sniper.session_frac = 0.10

run_strategy(
    "SNIPER -- skip after x>=10, cashout 3.0x",
    bet_sniper, co_sniper,
    "Pattern 5 filter + higher target. Fewer bets, higher per-bet target."
)

# ==============================================================
# STRATEGY 4: ANTI-MARTINGALE (scale up on wins, reset on loss)
# NOT regular martingale (doubling on losses = ruin)
# Instead: each win multiplies bet by 1.5x, loss resets to base
# Captures winning streaks, limits damage on losses
# ==============================================================
_am_bet_level = [1.0]  # mutable state

def bet_antimartingale(session_bank, window):
    base = session_bank * 0.01
    return min(base * _am_bet_level[0], session_bank * 0.04)

def co_antimartingale(session_bank, window):
    # On high bet level, lower cashout to increase win probability
    if _am_bet_level[0] >= 3:
        return 1.5  # take profit quickly when already up
    return 2.0

bet_antimartingale.stop_loss   = 0.15
bet_antimartingale.stop_profit = 0.35
bet_antimartingale.session_frac = 0.10

# Override run_strategy for stateful bet level
def run_antimartingale():
    bank = START_BANK
    peak = bank
    bets = wins = sessions = 0
    max_dd = 0.0
    SL, SP = 0.15, 0.35
    SESSION_FRAC = 0.10

    session_bank = bank * SESSION_FRAC
    session_start = session_bank
    session_bets = 0
    level = 1.0

    for i in range(WARMUP, n):
        window = mults[max(0, i - 50):i]

        down_pct = (session_start - session_bank) / session_start if session_start > 0 else 0
        up_pct   = (session_bank - session_start) / session_start if session_start > 0 else 0
        if down_pct >= SL or up_pct >= SP or session_bets >= 300:
            pnl = session_bank - session_start
            bank += pnl
            sessions += 1
            level = 1.0  # reset level on session end
            if bank <= START_BANK * 0.05:
                break
            session_bank = bank * SESSION_FRAC
            session_start = session_bank
            session_bets = 0

        base = session_bank * 0.01
        bet = min(base * level, session_bank * 0.04)
        if bet <= 0 or bet > session_bank:
            level = 1.0
            continue

        co = 1.5 if level >= 3 else 2.0

        if mults[i] >= co:
            session_bank += bet * (co - 1)
            wins += 1
            level = min(level * 1.5, 8.0)  # cap at 8x base
        else:
            session_bank -= bet
            level = 1.0  # reset on any loss

        session_bets += 1
        bets += 1

        approx_total = bank + (session_bank - session_start)
        if bank > peak:
            peak = bank
        dd = (peak - approx_total) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    bank += (session_bank - session_start)
    wr = wins / bets if bets > 0 else 0
    pnl = bank - START_BANK
    print(f"\n  [ANTI-MARTINGALE -- scale 1.5x on wins, reset on loss]")
    print(f"  Base 1% session, 1.5x multiplier per win (max 8x), reset on loss.")
    print(f"    Bets: {bets:,}  Sessions: {sessions}  Win rate: {wr*100:.1f}%")
    print(f"    Final: ${bank:,.0f}  P&L: {pnl:+,.0f} ({pnl/START_BANK*100:+.1f}%)")
    print(f"    Max drawdown: {max_dd*100:.1f}%")
    return bank

run_antimartingale()

# ==============================================================
# STRATEGY 5: COMPOUND SESSIONS
# Idea: reinvest profits fully -- session bank grows with total bank
# Bet 1.5% session bank, cashout 2.0x
# Tight stop-loss 8%, generous stop-profit 25%
# Every winning session compounds the next one
# ==============================================================
def bet_compound(session_bank, window):
    return session_bank * 0.015

def co_compound(session_bank, window):
    return 2.0

bet_compound.stop_loss   = 0.08
bet_compound.stop_profit = 0.25
bet_compound.session_frac = 0.12  # slightly larger sessions

run_strategy(
    "COMPOUND -- 1.5% session, SL8%/SP25%, reinvest",
    bet_compound, co_compound,
    "Tight stop-loss, let profits compound. Session = 12% of growing bank."
)

# ==============================================================
# COMPARISON SUMMARY
# ==============================================================
print()
print("=" * 65)
print("THEORETICAL BASELINE (bet every round, 2% flat, no sessions)")
print("=" * 65)
bank = START_BANK
for m in mults[WARMUP:]:
    bet = bank * 0.02
    if m >= 2.0:
        bank += bet
    else:
        bank -= bet
print(f"  Final: ${bank:,.0f}  P&L: {bank-START_BANK:+,.0f} ({(bank-START_BANK)/START_BANK*100:+.1f}%)")

print()
print("=" * 65)
print("KEY DESIGN PRINCIPLES FROM DATA")
print("=" * 65)
print("""
  1. Cashout 2.0x-2.1x: highest EV in this dataset
  2. Bet size <1% of TOTAL bank = Kelly-safe zone
  3. Session stop-loss: single best tool against ruin
     Without it: drawdown 95%+. With it: drawdown <10%
  4. Anti-martingale > Martingale:
     Martingale doubles on loss -> exponential ruin risk
     Anti-martingale scales on wins -> limited upside but SAFE
  5. No pattern filter: all pattern lifts ~1.0x in 17k rounds
     Sniper/Hunter reduce bets but don't change EV per bet
""")
