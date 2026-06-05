"""
Backtest with daily profit withdrawal: every 24h, if bank > 100 SOL -> withdraw surplus.
22k rounds / (86400s / 28.5s_per_round) = ~7.3 simulated days.
"""
import sys
import duckdb
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot_strategy import STRATEGIES, decide, new_session_bank

DB        = "data/crash.duckdb"
START     = 100.0
ROUNDS_PER_DAY = int(86400 / 28.5)   # 3032 rounds ~ 1 day

conn = duckdb.connect(DB, read_only=True)
MULTS = [r[0] for r in conn.execute("SELECT multiplier FROM rounds ORDER BY id ASC").fetchall()]
conn.close()
N = len(MULTS)
DAYS_TOTAL = N / ROUNDS_PER_DAY


def run(name: str) -> dict:
    cfg = STRATEGIES[name]
    bank = START
    sb   = new_session_bank(bank, cfg)
    ss   = sb
    sr   = 0
    cl   = 0
    sc   = 1.0
    cdl  = 0
    cbl  = 0

    trigger = cfg.consec_loss_trigger
    pause   = cfg.consec_loss_pause

    total_withdrawn = 0.0
    withdrawals = []          # (round_idx, amount, bank_before, bank_after)
    next_withdraw_at = ROUNDS_PER_DAY
    peak = bank
    max_dd = 0.0

    for i, mult in enumerate(MULTS):
        # Daily withdrawal check
        if i >= next_withdraw_at:
            if bank > START:
                amount = bank - START
                withdrawals.append((i, amount, bank, START))
                total_withdrawn += amount
                bank = START
                sb   = new_session_bank(bank, cfg)
                ss   = sb
                sr   = 0
                sc   = 1.0
                cbl  = 0
                peak = bank
            next_withdraw_at += ROUNDS_PER_DAY

        if cdl > 0:
            cdl -= 1; sr += 1; continue

        recent = MULTS[max(0, i - max(cfg.thermal_window, 5)):i]
        dec = decide(sb, ss, bank, sr, cl, cfg, sc, recent)
        act = dec["action"]

        if act == "end_session":
            pnl = sb - ss
            cl  = (cl + 1) if pnl < 0 else 0
            bank += pnl
            sb = new_session_bank(bank, cfg); ss = sb; sr = 0; sc = 1.0; cbl = 0
            if bank <= 0.01: break
        elif act in ("paused", "no_bet", "no_funds"):
            sr += 1
        elif act == "bet":
            bet = dec["bet_sol"]; sr += 1
            if mult >= dec["cashout"]:
                sb += bet * (dec["cashout"] - 1.0); cbl = 0
                if cfg.loss_scale > 1.0: sc = 1.0
                elif cfg.scaling > 1.0:  sc = min(sc * cfg.scaling, cfg.max_scale)
            else:
                sb -= bet; cbl += 1
                if cfg.loss_scale > 1.0: sc = min(sc * cfg.loss_scale, cfg.max_scale)
                elif cfg.scaling > 1.0:  sc = 1.0
                if trigger > 0 and cbl >= trigger:
                    cdl = pause; sc = 1.0; cbl = 0

        if bank > peak: peak = bank
        dd = (peak - bank) / peak if peak > 0 else 0
        if dd > max_dd: max_dd = dd

    bank += (sb - ss)

    # Final withdrawal
    if bank > START:
        amount = bank - START
        withdrawals.append((N, amount, bank, START))
        total_withdrawn += amount
        bank = START

    return {
        "name":            name,
        "total_withdrawn": total_withdrawn,
        "n_withdrawals":   len(withdrawals),
        "avg_per_day":     total_withdrawn / DAYS_TOTAL,
        "best_day":        max((w[1] for w in withdrawals), default=0.0),
        "days_no_profit":  sum(1 for d in range(int(DAYS_TOTAL)) if not any(
                               d * ROUNDS_PER_DAY <= w[0] < (d+1) * ROUNDS_PER_DAY
                               for w in withdrawals)),
        "final_bank":      bank,
        "max_dd":          max_dd * 100,
        "withdrawals":     withdrawals,
    }


def main():
    NAMES = [n for n in STRATEGIES if n != "turbo"]

    print(f"\n{'='*100}")
    print(f"  WITHDRAWAL BACKTEST  --  {N:,} rounds  (~{DAYS_TOTAL:.1f} days)  --  "
          f"withdraw surplus every {ROUNDS_PER_DAY} rounds (~24h)")
    print(f"  Target: bank > 100 SOL -> withdraw excess, reset to 100 SOL")
    print(f"{'='*100}")
    print(f"  {'Strategy':<16}  {'Total out':>10}  {'Withdrawals':>11}  "
          f"{'Avg/day':>9}  {'Best day':>9}  {'Days 0':>7}  {'MaxDD':>6}  Rules")
    print(f"  {'-'*96}")

    results = []
    for name in NAMES:
        r = run(name)
        results.append(r)

    results.sort(key=lambda r: r["total_withdrawn"], reverse=True)

    for r in results:
        print(
            f"  {r['name']:<16}"
            f"  {r['total_withdrawn']:>+9.4f}"
            f"  {r['n_withdrawals']:>11}"
            f"  {r['avg_per_day']:>+8.4f}"
            f"  {r['best_day']:>+8.4f}"
            f"  {r['days_no_profit']:>7}"
            f"  {r['max_dd']:>5.1f}%"
            f"  {STRATEGIES[r['name']].consec_loss_trigger}L->{STRATEGIES[r['name']].consec_loss_pause}r"
        )

    print(f"{'='*100}")

    # Show daily breakdown for top 3
    print(f"\n  DAILY WITHDRAWAL BREAKDOWN (top 3 strategies)")
    for r in results[:3]:
        print(f"\n  {r['name'].upper()} -- total withdrawn: {r['total_withdrawn']:+.4f} SOL")
        for i, (rnd, amt, b_before, b_after) in enumerate(r["withdrawals"]):
            day = rnd / ROUNDS_PER_DAY
            print(f"    Day {day:4.1f}: withdrew {amt:+.4f} SOL  (bank was {b_before:.4f})")
    print()


if __name__ == "__main__":
    main()
