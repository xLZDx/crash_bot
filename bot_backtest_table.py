#!/usr/bin/env python3
"""
bot_backtest_table.py
Replay ALL historical rounds from data/crash.duckdb for every paper strategy.
Start bank = $100. Output matches screenshot table format:
  # | Strategy | Balance $ | P&L $24h | P&L $48h | %1h | %12h | %24h | WR | Bets | Days | MaxDD | Details

By default backtests ONLY the live paper bots (those with a
data/bot_state_<name>.duckdb file). Pass --all to backtest every key in
STRATEGIES.
"""
import argparse
import glob
import os
import sys
import duckdb
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot_strategy import STRATEGIES, decide, new_session_bank

DB          = "data/crash.duckdb"
START_BANK  = 100.0
AVG_ROUND_S = 28.5        # seconds per round (average)

# Time windows as approximate round counts
R_1H  = round(3600   / AVG_ROUND_S)   # ~126
R_12H = round(43200  / AVG_ROUND_S)   # ~1516
R_24H = round(86400  / AVG_ROUND_S)   # ~3033
R_48H = round(172800 / AVG_ROUND_S)   # ~6065


def _cfg_details(cfg) -> str:
    """One-line strategy config summary."""
    parts = [f"x{getattr(cfg, 'cashout', '?')}"]
    bp = getattr(cfg, 'bet_pct', None)
    if bp:
        parts.append(f"bet={bp*100:.3g}%")
    ls = getattr(cfg, 'loss_scale', 1.0)
    if ls and ls > 1.0:
        ms = getattr(cfg, 'max_scale', ls)
        parts.append(f"mart x{ls:.0f}(max {ms:.0f})")
    sc = getattr(cfg, 'scaling', 1.0)
    if sc and sc > 1.0:
        parts.append(f"anti x{sc:.1f}")
    flt = getattr(cfg, 'filter', None)
    if flt and flt not in ('', 'none', None):
        parts.append(f"f={flt}")
    return "  ".join(parts)


def _total_days_from_ts(tss: list, n: int) -> float:
    """Compute elapsed days from a list of timestamps or fall back to round count."""
    if tss and len(tss) > 1:
        try:
            t0, t1 = tss[0], tss[-1]
            if hasattr(t0, 'timestamp'):
                return (t1.timestamp() - t0.timestamp()) / 86400
            diff = t1 - t0
            if hasattr(diff, 'total_seconds'):
                return diff.total_seconds() / 86400
            return float(diff) / 86400
        except Exception:
            pass
    return n * AVG_ROUND_S / 86400


def simulate(name: str, cfg, mults: list, total_days: float) -> dict:
    n = len(mults)

    # Record effective balance at these round indices (snapshot-at-index)
    snap_targets = {}
    for rounds_ago, key in [(R_48H, "b48"), (R_24H, "b24"), (R_12H, "b12"), (R_1H, "b1")]:
        idx = n - 1 - rounds_ago
        if idx >= 0:
            snap_targets[idx] = key
    snaps = {}

    # --- Exact same state as backtest.py ---
    bank               = START_BANK
    session_bank       = new_session_bank(bank, cfg)
    session_start      = session_bank
    session_rounds     = 0
    consec_losing      = 0
    current_scale      = 1.0
    consec_bet_losses  = 0
    sticky_recovery_wins = 0
    total_bets = total_wins = 0
    peak   = bank
    max_dd = 0.0

    for i, mult in enumerate(mults):

        # Capture effective running balance at snapshot targets
        if i in snap_targets:
            snaps[snap_targets[i]] = bank + (session_bank - session_start)

        # Build recent window (matches backtest.py exactly)
        if cfg.filter in ("pattern_allow", "pattern_veto"):
            max_pat = max([len(p) for p in (cfg.allow_patterns + cfg.veto_patterns)] or [8])
            recent = mults[max(0, i - max(8, max_pat)):i]
        elif cfg.filter == "numeric_pattern":
            limit = max(8, len(cfg.numeric_pattern) + max(0, cfg.numeric_delay - 1))
            recent = mults[max(0, i - limit):i]
        elif cfg.filter == "wave_regime":
            limit = max(8, cfg.wave_low_len + cfg.wave_high_len + max(0, cfg.wave_delay - 1))
            recent = mults[max(0, i - limit):i]
        elif cfg.filter in ("relative_wave", "relative_bad_veto"):
            limit = max(
                8,
                cfg.rel_base_len + cfg.rel_low_len + cfg.rel_high_len + max(0, cfg.rel_delay - 1),
            )
            recent = mults[max(0, i - limit):i]
        elif cfg.filter == "elliott_impulse5":
            limit = max(8, cfg.elliott_base_len + 4 * cfg.elliott_sub_len + max(0, cfg.elliott_delay - 1))
            recent = mults[max(0, i - limit):i]
        elif cfg.filter == "cold_breakout_wave":
            limit = max(
                8,
                cfg.cold_wave_len * 2 + max(0, cfg.cold_wave_max_gap) + max(0, cfg.cold_wave_delay - 1),
            )
            recent = mults[max(0, i - limit):i]
        else:
            recent = mults[max(0, i - max(cfg.thermal_window, 5)):i]

        decision = decide(
            session_bank=session_bank,
            session_start=session_start,
            total_bank=bank,
            session_rounds=session_rounds,
            consec_losing=consec_losing,
            cfg=cfg,
            current_scale=current_scale,
            recent_multipliers=recent,
        )
        action = decision["action"]

        if action == "end_session":
            pnl = session_bank - session_start
            consec_losing = (consec_losing + 1) if pnl < 0 else 0
            bank += pnl
            session_bank  = new_session_bank(bank, cfg)
            session_start = session_bank
            session_rounds = 0
            current_scale  = 1.0
            consec_bet_losses = 0
            sticky_recovery_wins = 0
            if bank <= 0.01:
                break

        elif action in ("paused", "no_bet", "no_funds"):
            session_rounds += 1

        elif action == "bet":
            bet     = decision["bet_sol"]
            cashout = decision["cashout"]
            session_rounds += 1
            total_bets += 1

            if mult >= cashout:
                session_bank += bet * (cashout - 1.0)
                total_wins += 1
                consec_bet_losses = 0
                if cfg.loss_scale > 1.0:
                    if cfg.sticky_win_reset_after > 0 and current_scale > 1.0:
                        sticky_recovery_wins += 1
                        if sticky_recovery_wins >= cfg.sticky_win_reset_after:
                            current_scale = 1.0
                            sticky_recovery_wins = 0
                    else:
                        current_scale = 1.0
                        sticky_recovery_wins = 0
                elif cfg.scaling > 1.0:
                    current_scale = min(current_scale * cfg.scaling, cfg.max_scale)
            else:
                session_bank -= bet
                consec_bet_losses += 1
                if cfg.loss_scale > 1.0:
                    current_scale = min(current_scale * cfg.loss_scale, cfg.max_scale)
                    sticky_recovery_wins = 0
                elif cfg.scaling > 1.0:
                    current_scale = 1.0

        # DD on committed bank (identical to backtest.py)
        if bank > peak:
            peak = bank
        dd = (peak - bank) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    bank += (session_bank - session_start)

    # Retrieve time-window snapshots (fall back to START_BANK if window unavailable)
    b48 = snaps.get("b48", START_BANK)
    b24 = snaps.get("b24", START_BANK)
    b12 = snaps.get("b12", START_BANK)
    b1  = snaps.get("b1",  START_BANK)

    return {
        "name":     name,
        "final":    bank,
        "pnl_24h":  bank - b24,
        "pnl_48h":  bank - b48,
        "pct_1h":   (bank - b1)  / b1  * 100 if b1  > 0 else 0.0,
        "pct_12h":  (bank - b12) / b12 * 100 if b12 > 0 else 0.0,
        "pct_24h":  (bank - b24) / b24 * 100 if b24 > 0 else 0.0,
        "win_rate": (total_wins / total_bets * 100) if total_bets > 0 else 0.0,
        "bets":     total_bets,
        "days":     total_days,
        "max_dd":   max_dd * 100,
        "cfg":      cfg,
    }


# -- table formatting --------------------------------------------------------

W_NUM = 3;  W_STR = 38;  W_BAL = 10;  W_P24 = 11;  W_P48 = 11
W_1H  = 8;  W_12H = 8;   W_24H = 8;   W_WR  = 7;   W_BET = 8
W_DAY = 8;  W_MDD = 9
SEP = "  "


def _usd(v: float, w: int) -> str:
    if abs(v) < 0.005:
        return "--".rjust(w)
    s = f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"
    return s.rjust(w)


def _pct(v: float, w: int) -> str:
    if abs(v) < 0.0005:
        return "--".rjust(w)
    return f"{v:+.2f}%".rjust(w)


def print_table(results: list, n_rounds: int, total_days: float):
    medals = {0: "[1]", 1: "[2]", 2: "[3]"}

    hdr = (
        f"{'#':>{W_NUM}}{SEP}"
        f"{'Strategy':<{W_STR}}{SEP}"
        f"{'Balance $':>{W_BAL}}{SEP}"
        f"{'P&L $24h':>{W_P24}}{SEP}"
        f"{'P&L $48h':>{W_P48}}{SEP}"
        f"{'%1h':>{W_1H}}{SEP}"
        f"{'%12h':>{W_12H}}{SEP}"
        f"{'%24h':>{W_24H}}{SEP}"
        f"{'WR':>{W_WR}}{SEP}"
        f"{'Bets':>{W_BET}}{SEP}"
        f"{'Days':>{W_DAY}}{SEP}"
        f"{'MaxDD':>{W_MDD}}{SEP}"
        f"Details"
    )
    bar = "-" * len(hdr)

    print()
    print(f"  Backtest $100/strategy -- {len(results)} strategies | {n_rounds:,} rounds | {total_days:.1f} days")
    print()
    print("  " + hdr)
    print("  " + bar)

    for idx, r in enumerate(results):
        num_s = f"{idx + 1:>{W_NUM}}"
        if "error" in r:
            print(f"  {num_s}{SEP}{r['name']:<{W_STR}}  ERROR: {r['error']}")
            continue

        medal = medals.get(idx, "") if r["final"] > START_BANK else ""
        mdd   = r["max_dd"]
        flag  = "*" if mdd > 5 else ""

        name_field = (medal + r["name"])[:W_STR]
        bal_s = f"${r['final']:.2f}".rjust(W_BAL)
        wr_s  = f"{r['win_rate']:.1f}%".rjust(W_WR) if r["bets"] > 0 else "--".rjust(W_WR)
        day_s = f"{r['days']:.1f}d".rjust(W_DAY)
        mdd_s = f"{flag}{mdd:.2f}%".rjust(W_MDD)

        print(
            f"  {num_s}{SEP}"
            f"{name_field:<{W_STR}}{SEP}"
            f"{bal_s}{SEP}"
            f"{_usd(r['pnl_24h'], W_P24)}{SEP}"
            f"{_usd(r['pnl_48h'], W_P48)}{SEP}"
            f"{_pct(r['pct_1h'],  W_1H)}{SEP}"
            f"{_pct(r['pct_12h'], W_12H)}{SEP}"
            f"{_pct(r['pct_24h'], W_24H)}{SEP}"
            f"{wr_s}{SEP}"
            f"{r['bets']:>{W_BET}}{SEP}"
            f"{day_s}{SEP}"
            f"{mdd_s}{SEP}"
            f"{_cfg_details(r['cfg'])}"
        )

    print("  " + bar)
    profitable = sum(1 for r in results if "error" not in r and r["final"] > START_BANK)
    print(f"  Profitable: {profitable} / {len(results)}")
    print()


def _discover_paper_strategies(data_dir: str = "data") -> list:
    """Return strategy names that have a live bot_state_<name>.duckdb file
    AND a matching config in STRATEGIES, preserving STRATEGIES order."""
    live = set()
    for p in glob.glob(os.path.join(data_dir, "bot_state_*.duckdb")):
        nm = os.path.basename(p).replace("bot_state_", "").replace(".duckdb", "")
        live.add(nm)
    return [k for k in STRATEGIES if k in live]


def main():
    ap = argparse.ArgumentParser(description="Backtest all paper strategies at $100")
    ap.add_argument("--all", action="store_true",
                    help="backtest every key in STRATEGIES (default: only live paper bots)")
    args = ap.parse_args()

    print("Loading rounds ...", end="", flush=True)
    conn = duckdb.connect(DB, read_only=True)

    cols = [row[1] for row in conn.execute("PRAGMA table_info(rounds)").fetchall()]
    ts_col = next(
        (c for c in cols if any(k in c.lower() for k in ("ts", "time", "creat", "start", "ended"))),
        None,
    )

    if ts_col:
        rows  = conn.execute(f"SELECT multiplier, {ts_col} FROM rounds ORDER BY id ASC").fetchall()
        mults = [r[0] for r in rows]
        tss   = [r[1] for r in rows]
    else:
        rows  = conn.execute("SELECT multiplier FROM rounds ORDER BY id ASC").fetchall()
        mults = [r[0] for r in rows]
        tss   = []

    conn.close()

    n          = len(mults)
    total_days = _total_days_from_ts(tss, n)
    print(f" {n:,} rounds ({total_days:.1f} days)")

    if args.all:
        names = list(STRATEGIES.keys())
    else:
        names = _discover_paper_strategies()
        if not names:
            print("No live paper bots found; falling back to --all.")
            names = list(STRATEGIES.keys())

    print(f"Running {len(names)} backtests ...", flush=True)

    results = []
    for i, name in enumerate(names):
        print(f"  [{i+1:2}/{len(names)}] {name:<38}", end="\r", flush=True)
        cfg = STRATEGIES[name]
        try:
            results.append(simulate(name, cfg, mults, total_days))
        except Exception as exc:
            results.append({"name": name, "error": str(exc)})

    print(" " * 60, end="\r")

    results.sort(key=lambda r: r.get("final", 0.0), reverse=True)
    print_table(results, n, total_days)


if __name__ == "__main__":
    main()
