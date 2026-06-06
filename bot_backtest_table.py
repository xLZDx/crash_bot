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
import csv
import glob
import os
import sys
from datetime import datetime, timezone
import duckdb
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot_strategy import STRATEGIES, decide, new_session_bank
from db_util import connect_ro


def _now_dual() -> str:
    now = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        loc = now.astimezone(ZoneInfo("Europe/Chisinau"))
        return (f"{loc.strftime('%Y-%m-%d %H:%M')} local (Europe/Chisinau) / "
                f"{now.strftime('%H:%M')} UTC")
    except Exception:
        return f"{now.strftime('%Y-%m-%d %H:%M')} UTC"


def _fmt_dt(dt) -> str:
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)

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


def print_table(results: list, n_rounds: int, total_days: float, meta: dict = None):
    meta = meta or {}
    medals_t = {0: "[1]", 1: "[2]", 2: "[3]"}
    medals_m = {0: "\U0001F947", 1: "\U0001F948", 2: "\U0001F949"}
    profit = sum(1 for r in results if "error" not in r and r["final"] > START_BANK)

    head = [
        f"Backtest $100/strategy -- {meta.get('generated', _now_dual())}",
        f"Strategies: {len(results)} | Rounds used: {n_rounds:,} ({total_days:.1f} days)",
    ]
    if meta.get("period_start"):
        head.append(f"Data period: {meta['period_start']} -> {meta['period_end']}")
    if meta.get("completeness_known") is False:
        why = (f"query failed: {meta.get('completeness_err')}"
               if meta.get("completeness_err") else "no game_round_id column")
        head.append(f"Round completeness: UNKNOWN ({why})")
    elif meta.get("expected"):
        head.append(f"Round completeness: {meta['distinct']:,} / {meta['expected']:,} "
                    f"collected -- MISSING {meta['missing']:,} "
                    f"({meta['coverage']:.2f}% coverage)")
    head.append(f"Profitable: {profit} / {len(results)}")

    txt_hdr = (
        f"{'#':>{W_NUM}}{SEP}{'Strategy':<{W_STR}}{SEP}{'Balance $':>{W_BAL}}{SEP}"
        f"{'P&L $24h':>{W_P24}}{SEP}{'P&L $48h':>{W_P48}}{SEP}{'%1h':>{W_1H}}{SEP}"
        f"{'%12h':>{W_12H}}{SEP}{'%24h':>{W_24H}}{SEP}{'WR':>{W_WR}}{SEP}"
        f"{'Bets':>{W_BET}}{SEP}{'Days':>{W_DAY}}{SEP}{'MaxDD':>{W_MDD}}{SEP}Details"
    )
    bar = "-" * len(txt_hdr)
    txt = [""] + ["  " + h for h in head] + ["", "  " + txt_hdr, "  " + bar]

    md = ["# " + head[0], ""] + [f"- {h}" for h in head[1:]] + [""]
    mdcols = ["#", "Strategy", "Balance $", "P&L $24h", "P&L $48h", "%1h", "%12h",
              "%24h", "WR", "Bets", "Days", "MaxDD", "Details"]
    md.append("| " + " | ".join(mdcols) + " |")
    md.append("| " + " | ".join(["--:", "---"] + ["--:"] * 10 + ["---"]) + " |")

    csv_rows = [["#", "Strategy", "Balance", "PnL_24h", "PnL_48h", "pct_1h", "pct_12h",
                 "pct_24h", "WR", "Bets", "Days", "MaxDD_pct", "Details"]]

    for idx, r in enumerate(results):
        num = idx + 1
        if "error" in r:
            txt.append(f"  {num:>{W_NUM}}{SEP}{r['name']:<{W_STR}}  ERROR: {r['error']}")
            md.append(f"| {num} | {r['name']} | ERROR |  |  |  |  |  |  |  |  |  | {r['error']} |")
            csv_rows.append([num, r["name"], "ERROR", "", "", "", "", "", "", "", "", "", r["error"]])
            continue
        prof = r["final"] > START_BANK
        mt = medals_t.get(idx, "") if prof else ""
        mm = medals_m.get(idx, "") if prof else ""
        mdd = r["max_dd"]
        flag = "*" if mdd > 5 else ""
        name_t = (mt + r["name"])[:W_STR]
        bal = f"${r['final']:.2f}"
        wr = f"{r['win_rate']:.1f}%" if r["bets"] > 0 else "--"
        day = f"{r['days']:.1f}d"
        mddp = f"{mdd:.2f}%"
        det = _cfg_details(r["cfg"])
        txt.append(
            f"  {num:>{W_NUM}}{SEP}{name_t:<{W_STR}}{SEP}{bal.rjust(W_BAL)}{SEP}"
            f"{_usd(r['pnl_24h'], W_P24)}{SEP}{_usd(r['pnl_48h'], W_P48)}{SEP}"
            f"{_pct(r['pct_1h'], W_1H)}{SEP}{_pct(r['pct_12h'], W_12H)}{SEP}"
            f"{_pct(r['pct_24h'], W_24H)}{SEP}{wr.rjust(W_WR)}{SEP}{r['bets']:>{W_BET}}{SEP}"
            f"{day.rjust(W_DAY)}{SEP}{(flag + mddp).rjust(W_MDD)}{SEP}{det}"
        )
        mdd_md = ("\U0001F534**" + mddp + "**") if mdd > 5 else mddp
        md.append(
            f"| {num} | {mm}{r['name']} | {bal} | {_usd(r['pnl_24h'], 0).strip()} | "
            f"{_usd(r['pnl_48h'], 0).strip()} | {_pct(r['pct_1h'], 0).strip()} | "
            f"{_pct(r['pct_12h'], 0).strip()} | {_pct(r['pct_24h'], 0).strip()} | {wr} | "
            f"{r['bets']} | {day} | {mdd_md} | {det} |"
        )
        csv_rows.append([
            num, r["name"], f"{r['final']:.2f}", f"{r['pnl_24h']:.2f}", f"{r['pnl_48h']:.2f}",
            f"{r['pct_1h']:.2f}", f"{r['pct_12h']:.2f}", f"{r['pct_24h']:.2f}",
            (f"{r['win_rate']:.1f}" if r["bets"] > 0 else ""),
            r["bets"], f"{r['days']:.1f}", f"{mdd:.2f}", det,
        ])

    txt += ["  " + bar, f"  Profitable: {profit} / {len(results)}", ""]
    md += ["", f"**Profitable: {profit} / {len(results)}**"]

    txt_text = "\n".join(txt)
    print(txt_text)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = os.path.join("data", f"backtest_table_{stamp}")
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(txt_text + "\n")
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    with open(base + ".csv", "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(csv_rows)
    print(f"\n  saved: {base}.txt | .md | .csv")


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
    conn = connect_ro(DB)
    completeness_err = None
    gmin = gmax = gd = None
    try:
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
        # round-completeness (lost rounds); TRY_CAST drops non-numeric game_round_id
        try:
            gmin, gmax, gd = conn.execute(
                "SELECT MIN(g), MAX(g), COUNT(DISTINCT g) FROM "
                "(SELECT TRY_CAST(game_round_id AS BIGINT) g FROM rounds) WHERE g IS NOT NULL"
            ).fetchone()
        except Exception as e:
            completeness_err = str(e)[:80]
    finally:
        conn.close()

    n          = len(mults)
    total_days = _total_days_from_ts(tss, n)
    print(f" {n:,} rounds ({total_days:.1f} days)")

    known = (gmin is not None and gmax is not None and gd is not None)
    expected = (gmax - gmin + 1) if known else 0
    missing  = (expected - gd) if (known and expected) else 0
    meta = {
        "generated": _now_dual(),
        "period_start": _fmt_dt(tss[0]) if tss else "?",
        "period_end":   _fmt_dt(tss[-1]) if tss else "?",
        "distinct": gd or 0,
        "expected": expected,
        "missing": missing,
        "coverage": (gd / expected * 100.0) if (known and expected) else 0.0,
        "completeness_known": known,
        "completeness_err": completeness_err,
    }

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
    print_table(results, n, total_days, meta)


if __name__ == "__main__":
    main()
