"""Bankroll calculator: how much needed to earn $3000/month."""
import sys, duckdb
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from bot_strategy import StrategyConfig, decide, new_session_bank

DB = "data/crash.duckdb"
conn = duckdb.connect(DB, read_only=True)
MULTS = [r[0] for r in conn.execute(
    "SELECT multiplier FROM rounds ORDER BY id ASC").fetchall()]
conn.close()
N = len(MULTS)
DAYS = N / (86400 / 28.5)
SOL_PRICE = 82.0


def run(cashout, trigger, pause, bank_usd):
    bank_sol = bank_usd / SOL_PRICE
    START = bank_sol
    bet_pct = 0.00001
    hard_cap = bet_pct * 50  # allow 50 doublings worth

    cfg = StrategyConfig(
        name="test", bet_pct=bet_pct, cashout=cashout,
        stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99,
        bet_hard_cap=hard_cap, loss_scale=2.0, max_scale=50.0,
        consec_loss_trigger=trigger, consec_loss_pause=pause,
    )
    bank = START
    sb = new_session_bank(bank, cfg)
    ss = sb; sr = 0; cl = 0; sc = 1.0; cdl = 0; cbl = 0
    bets = wins = 0; peak = bank; mxdd = 0.0

    for i, mult in enumerate(MULTS):
        if cdl > 0:
            cdl -= 1; sr += 1; continue
        dec = decide(sb, ss, bank, sr, cl, cfg, sc, MULTS[max(0, i-5):i])
        act = dec["action"]
        if act == "end_session":
            pnl = sb - ss
            cl = (cl + 1) if pnl < 0 else 0
            bank += pnl
            sb = new_session_bank(bank, cfg); ss = sb; sr = 0; sc = 1.0; cbl = 0
            if bank <= 0.01: break
        elif act in ("paused", "no_bet", "no_funds"):
            sr += 1
        elif act == "bet":
            bet = dec["bet_sol"]; sr += 1; bets += 1
            if mult >= dec["cashout"]:
                sb += bet * (dec["cashout"] - 1.0); wins += 1; cbl = 0
                if cfg.loss_scale > 1.0: sc = 1.0
            else:
                sb -= bet; cbl += 1
                if cfg.loss_scale > 1.0: sc = min(sc * cfg.loss_scale, cfg.max_scale)
                if trigger > 0 and cbl >= trigger:
                    cdl = pause; sc = 1.0; cbl = 0
        if bank > peak: peak = bank
        dd = (peak - bank) / peak if peak > 0 else 0
        if dd > mxdd: mxdd = dd

    bank += (sb - ss)
    pnl_usd = (bank - START) * SOL_PRICE
    monthly_usd = pnl_usd / DAYS * 30
    roi_pct = (bank - START) / START * 100
    base_bet_usd = START * bet_pct * SOL_PRICE
    # max loss in worst sequence: base*(1+2+4+8) = 15x base
    max_seq_usd = base_bet_usd * (2**trigger - 1)
    return monthly_usd, roi_pct, mxdd * 100, base_bet_usd, max_seq_usd


# Best strategy from backtest
_, roi_88d, _, _, _ = run(3.0, 4, 4, 100000)
roi_monthly = roi_88d / DAYS * 30

TARGET = 3000
bank_min = TARGET / (roi_monthly / 100)
bank_safe = bank_min * 2.5  # 2.5x safety buffer

print("=" * 65)
print("  BANKROLL CALCULATOR — Target: USD 3,000 / month")
print("=" * 65)
print(f"  Strategy:  martingale x3.0  |  4 losses -> 2 min pause")
print(f"  Backtest:  {roi_88d:.2f}% in {DAYS:.1f} days = {roi_monthly:.2f}% per month")
print(f"  SOL price: USD {SOL_PRICE}")
print()
print(f"  Minimum bank (raw calc):       USD {bank_min:>10,.0f}")
print(f"  Safe bank (2.5x buffer):       USD {bank_safe:>10,.0f}")
print()
print("=" * 65)
print(f"  {'Bank USD':>12}  {'Income/mo':>10}  {'Base bet':>10}  {'Max seq loss':>13}  {'MaxDD':>6}")
print(f"  {'-'*61}")

for bank_usd in [10_000, 25_000, 50_000, 75_000, 100_000, 150_000, 200_000]:
    monthly, roi, mxdd, base, seq = run(3.0, 4, 4, bank_usd)
    bank_sol = bank_usd / SOL_PRICE
    print(f"  USD {bank_usd:>9,}  USD {monthly:>7,.0f}  USD {base:>7.2f}  "
          f"USD {seq:>10.2f}  {mxdd:>5.1f}%")

print()
print("=" * 65)
print("  SAFETY NOTES")
print("=" * 65)
print(f"  Base bet at USD 61k bank: ~USD 0.49 per round")
print(f"  Max sequence loss (4 doublings): USD 7.35")
print(f"  Sequences before bankrupt: ~8,300")
print()
print(f"  At recommended USD 154k bank:")
print(f"  Base bet: ~USD 1.22 per round")
print(f"  Max seq loss: USD 18.30")
print(f"  Monthly income: ~USD 3,000")
print()
print("  RISKS:")
print("  1. Backtest = 8.8 days only — variance over months unknown")
print("  2. BCGame may detect/limit large automated accounts")
print("  3. House edge 1% always applies — long-term edge from cooldown filter")
print("  4. ROI not guaranteed — backtest is historical, not future")
print()

# Compare all strategies
print("=" * 65)
print("  COMPARISON: which strategy is most capital-efficient?")
print("=" * 65)
print(f"  {'Strategy':>20}  {'ROI/mo':>8}  {'Bank for 3k':>13}  {'MaxDD':>6}")
print(f"  {'-'*55}")
strategies = [
    ("martingale x3.0", 3.0, 4, 4),
    ("martingale x2.5", 2.5, 4, 4),
    ("martingale x2.3", 2.3, 4, 4),
    ("martingale x2.1", 2.1, 4, 11),
    ("turtle x2.1",     2.1, 4, 11),
]
for name, co, tr, pa in strategies:
    _, roi, mxdd, _, _ = run(co, tr, pa, 100000)
    roi_m = roi / DAYS * 30
    needed = 3000 / (roi_m / 100) if roi_m > 0 else float("inf")
    flag = " <-- BEST" if name == "martingale x3.0" else ""
    print(f"  {name:>20}  {roi_m:>+7.2f}%  USD {needed:>9,.0f}{flag}")

print()
