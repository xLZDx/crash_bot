"""Martingale 2.1x backtest — various cashouts and cooldowns."""
import sys, duckdb
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from bot_strategy import StrategyConfig, decide, new_session_bank

DB = "data/crash.duckdb"
START = 100.0

conn = duckdb.connect(DB, read_only=True)
MULTS = [r[0] for r in conn.execute(
    "SELECT multiplier FROM rounds ORDER BY id ASC").fetchall()]
conn.close()
N = len(MULTS)
DAYS = N / (86400 / 28.5)
print(f"Rounds: {N:,}  (~{DAYS:.1f} days / ~{DAYS*24:.0f}h)")

configs = [
    # (name,               bet_pct,  cashout, trigger, pause)
    ("mart_2.1x_no_CD",    0.00001,  2.1,  0,  0),
    ("mart_2.1x_3L->2min", 0.00001,  2.1,  3,  4),
    ("mart_2.1x_4L->2min", 0.00001,  2.1,  4,  4),
    ("mart_2.1x_4L->5min", 0.00001,  2.1,  4, 11),
    ("mart_2.1x_5L->5min", 0.00001,  2.1,  5, 11),
    ("mart_2.1x_3L->5min", 0.00001,  2.1,  3, 11),
    # compare vs current real bot
    ("mart_2.3x_4L->2min", 0.00001,  2.3,  4,  4),
    ("mart_2.0x_4L->2min", 0.00001,  2.0,  4,  4),
    ("mart_1.9x_4L->2min", 0.00001,  1.9,  4,  4),
    ("mart_2.5x_4L->2min", 0.00001,  2.5,  4,  4),
    ("mart_3.0x_4L->2min", 0.00001,  3.0,  4,  4),
]


def run(bet_pct, cashout, trigger, pause):
    cfg = StrategyConfig(
        name="test", bet_pct=bet_pct, cashout=cashout,
        stop_loss=0.50, stop_profit=1.0,
        session_frac=1.0, max_rounds=9999, consec_sl_pause=99,
        bet_hard_cap=0.0005, loss_scale=2.0, max_scale=50.0,
        consec_loss_trigger=trigger, consec_loss_pause=pause,
    )
    bank = START
    sb = new_session_bank(bank, cfg)
    ss = sb; sr = 0; cl = 0; sc = 1.0; cdl = 0; cbl = 0
    bets = wins = 0; peak = bank; mxdd = 0.0

    for i, mult in enumerate(MULTS):
        if cdl > 0:
            cdl -= 1; sr += 1; continue
        dec = decide(sb, ss, bank, sr, cl, cfg, sc, MULTS[max(0,i-5):i])
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
    wr = wins / bets * 100 if bets else 0
    return (bank - START) / START * 100, bank - START, wr, mxdd * 100, bets


print()
print(f"  {'Config':<22}  {'ROI':>8}  {'P&L SOL':>9}  {'WR':>7}  {'MaxDD':>7}  {'Bets':>6}")
print(f"  {'-'*72}")

results = []
for name, bet_pct, cashout, trigger, pause in configs:
    roi, pnl, wr, mxdd, bets = run(bet_pct, cashout, trigger, pause)
    cd = f"{trigger}L->{int(pause*28.5/60)}m" if trigger else "no CD"
    print(f"  {name:<22}  {roi:>+7.2f}%  {pnl:>+8.4f}  {wr:>6.1f}%  {mxdd:>6.1f}%  {bets:>6}  {cd}")
    results.append((name, roi, pnl, wr, mxdd, bets))

results.sort(key=lambda x: x[1], reverse=True)
print()
print(f"  Best:  {results[0][0]}  ROI {results[0][1]:+.2f}%")
print(f"  Worst: {results[-1][0]}  ROI {results[-1][1]:+.2f}%")

# Grid search best cooldown for 2.1x specifically
print()
print("=" * 60)
print("  GRID SEARCH: best cooldown for 2.1x cashout")
print("=" * 60)
print(f"  {'Consec':>6}  {'Pause':>5}  {'~Min':>5}  {'ROI':>8}  {'MaxDD':>7}")
print(f"  {'-'*42}")

grid = []
for c in [2, 3, 4, 5]:
    for p in [2, 4, 6, 9, 11, 17, 21]:
        roi, pnl, wr, mxdd, bets = run(0.00001, 2.1, c, p)
        grid.append((c, p, roi, mxdd))

grid.sort(key=lambda x: x[2], reverse=True)
for c, p, roi, mxdd in grid[:8]:
    mins = p * 28.5 / 60
    print(f"  {c:>6}  {p:>5}  {mins:>4.0f}m  {roi:>+7.2f}%  {mxdd:>6.1f}%")
print()
