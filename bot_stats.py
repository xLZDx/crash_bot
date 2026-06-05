"""
Standalone stats reporter for the COMPOUND paper-trading bot.

Usage:
    python bot_stats.py [--bot-db PATH]

Prints current bankroll + deltas vs 1h / 12h / 24h / 7d.
"""
import argparse
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot_state import BotAccount
from bot_strategy import TOTAL_BANK_SOL

BOT_DB_DEFAULT = "data/bot_state.duckdb"


def _fmt_sol(v: float, width: int = 9) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.4f}".rjust(width)


def _fmt_pct(v: float, width: int = 8) -> str:
    sign = "+" if v >= 0 else ""
    return f"{sign}{v*100:.2f}%".rjust(width)


def _fmt_wr(v: float, width: int = 8) -> str:
    if v != v:  # nan
        return "  n/a".rjust(width)
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}%".rjust(width)


def report(bot_db: str):
    account = BotAccount(bot_db)
    now = datetime.now(timezone.utc)
    state = account.get_state()
    totals = account.get_totals()

    bank = state["total_bank_sol"]
    sbank = state["session_bank_sol"]
    sstart = state["session_start_sol"]
    session_pnl = sbank - sstart
    session_pct = (session_pnl / sstart * 100) if sstart > 0 else 0.0
    session_rounds = state["session_rounds"]
    consec = state["consec_losing_sessions"]
    session_id = state["session_id"]

    win_rate = (totals["total_wins"] / totals["total_bets"] * 100
                if totals["total_bets"] > 0 else 0.0)
    total_pnl = totals["total_pnl_sol"]
    total_pnl_frac = total_pnl / TOTAL_BANK_SOL  # fraction for _fmt_pct

    print(f"\n{'='*58}")
    print(f"  COMPOUND BOT STATS  --  {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*58}")
    print(f"  Bankroll:         {bank:.6f} SOL   ({_fmt_pct(total_pnl_frac)} vs start)")
    print(f"  Session #{session_id:<4}     bank {sbank:.6f}  ({session_pct:+.2f}%  {session_rounds} rounds)")
    print(f"  Consecutive losses: {consec}    Total bets: {totals['total_bets']}    Win rate: {win_rate:.1f}%")
    print(f"  Total P&L:        {_fmt_sol(total_pnl)} SOL")

    spans = [
        ("1h",   3600),
        ("12h",  43200),
        ("24h",  86400),
        ("7d",   604800),
    ]

    col = 10
    lbl = 12
    print()
    print(f"  {'DELTA':>{lbl}}", end="")
    for name, _ in spans:
        print(f"  {name:>{col}}", end="")
    print()
    print(f"  {'-'*(lbl + (col+2)*len(spans))}")

    rows = {
        "P&L SOL":  [],
        "P&L %":    [],
        "Win rate": [],
        "Rounds":   [],
        "Sessions": [],
    }

    for name, secs in spans:
        snap_ts = datetime.fromtimestamp(now.timestamp() - secs, tz=timezone.utc)
        snap = account.get_snapshot_near(snap_ts)

        if snap is None:
            for key in rows:
                rows[key].append("n/a".rjust(col))
            continue

        d_bank = bank - snap["total_bank_sol"]
        base = snap["total_bank_sol"]
        d_pct = (d_bank / base) if base > 0 else 0.0
        d_bets = totals["total_bets"] - snap["total_bets"]
        d_wins = totals["total_wins"] - snap["total_wins"]
        d_wr = (d_wins / d_bets * 100) if d_bets > 0 else float("nan")

        rows["P&L SOL"].append(_fmt_sol(d_bank, col))
        rows["P&L %"].append(_fmt_pct(d_pct, col))
        rows["Win rate"].append(_fmt_wr(d_wr, col))
        rows["Rounds"].append(str(d_bets).rjust(col))

        # Count sessions in window (approximate via bets table doesn't exist here;
        # we omit or note n/a — sessions delta needs a separate query)
        rows["Sessions"].append("n/a".rjust(col))

    for metric, vals in rows.items():
        print(f"  {metric:>{lbl}}", end="")
        for v in vals:
            print(f"  {v}", end="")
        print()

    print(f"{'='*58}\n")


def main():
    parser = argparse.ArgumentParser(description="Bot stats reporter")
    parser.add_argument("--bot-db", default=BOT_DB_DEFAULT)
    args = parser.parse_args()
    report(args.bot_db)


if __name__ == "__main__":
    main()
