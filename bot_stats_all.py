"""
Side-by-side stats reporter for ALL running strategy bots.

Usage:
    python bot_stats_all.py [--data-dir PATH] [--no-color]

Auto-discovers data/bot_state_*.duckdb files.
Columns: # | Стратегия | P&L $ | Bal $ | $24h | $48h | %1h | %12h | %24h | WR | Bets | Days | MaxDD | Strategy
"""
import argparse
import glob
import os
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot_state import BotAccount
from bot_strategy import TOTAL_BANK_SOL, STRATEGIES, StrategyConfig

# Short display aliases (max 13 chars so table stays compact)
ALIASES = {
    "martingale":                         "martingale",
    "martingale_21":                      "mart_21",
    "martingale_30":                      "mart_30",
    "x5":                                 "x5",
    "sniper_v2":                          "sniper_v2",
    "waveskip":                           "waveskip",
    "turtle":                             "turtle",
    "turtle_micro":                       "turtle_mic",
    "scalper":                            "scalper",
    "sniper":                             "sniper",
    "compound":                           "compound",
    "antimartingale":                     "antimartgl",
    "tide":                               "tide",
    "turbo":                              "turbo",
    "relwave_bad_veto_21":                "rveto_21",
    "relwave_bad_veto_21_sticky2":        "rveto_stk2",
    "relwave_bad_veto_21_trigger10_x512": "rveto_t10x5",
    "relwave_bad_veto_21_nr8":            "rveto_nr8",
    "relwave_bad_veto_21_nr128":          "rveto_nr128",
    "relwave_bad_veto_21_nr256":          "rveto_nr256",
    "relwave_bad_veto_21_nr512":          "rveto_nr512",
    "relwave_l10_h5_x3":                  "rwave_x3",
    "relwave_l12_h5_21":                  "rl12_21",
    "relwave_l12_h5_21_strict":           "rl12_21s",
    "relwave_l15_h7_x3_d3":               "rl15_x3d3",
    "wave_l12_h5_21":                     "wl12_21",
    "wave_l12_h5_21_wide":                "wl12_21w",
    "wave_l12_h10_x3_shadow":             "wl12_x3sh",
    "elliott_impulse5_21":                "el5_21",
    "elliott_impulse5_x3":                "el5_x3",
    "cold_breakout_21":                   "coldbr_21",
    "cold_breakout_x3":                   "coldbr_x3",
    "pattern_x3_lhlh":                    "pat_x3_lhlh",
    "pattern_21_allow":                   "pat_21_alw",
    "pattern_21_veto":                    "pat_21_vet",
    "numeric_hll_21_delay2":              "nhll_21_d2",
    "numeric_hll_30_50_21_delay2":        "nhll3050d2",
    "numeric_hll_30_50_x3_delay2":        "nhll_x3_d2",
    "relwave_bad_veto_21_strict":         "rveto_21s",
}

# Paper bots start at $100 USD (all bots migrated from SOL to USD)
PAPER_START_USD = 100.0

# Live bot deposit amount
LIVE_START_USD = 24.83


def _describe(cfg: StrategyConfig) -> str:
    parts = []
    parts.append(f"x{cfg.cashout}")
    parts.append(f"bet={cfg.bet_pct * 100:.4g}%")
    if cfg.loss_scale > 1.0:
        parts.append(f"martx{cfg.loss_scale:.0f}")
    if cfg.scaling > 1.0:
        parts.append(f"antix{cfg.scaling}")
    if cfg.filter == "no_x10_last5":
        parts.append("no-x10")
    if cfg.filter == "thermal_skip":
        parts.append(f"therm>{cfg.thermal_threshold}")
    if cfg.filter == "cold_breakout_wave":
        parts.append(f"coldbr{cfg.cold_wave_len}x2")
    if cfg.consec_loss_trigger > 0:
        mins = cfg.consec_loss_pause * 28.5 / 60
        parts.append(f"{cfg.consec_loss_trigger}L->{mins:.0f}m")
    return " ".join(parts)


# ANSI color helpers
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    WHITE  = "\033[97m"
    CYAN   = "\033[96m"
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    GREY   = "\033[90m"


_USE_COLOR = True
_CC = 12  # ANSI escape overhead bytes for rjust alignment


def _c(text: str, *codes: str) -> str:
    if not _USE_COLOR:
        return text
    return "".join(codes) + text + C.RESET


def _vc(v: float, text: str) -> str:
    if not _USE_COLOR or v != v:
        return text
    return _c(text, C.GREEN) if v > 0 else (_c(text, C.RED) if v < 0 else text)


def _fpct(v: float, w: int = 7) -> str:
    if v != v:
        return "n/a".rjust(w)
    s = "+" if v >= 0 else ""
    return f"{s}{v * 100:.2f}%".rjust(w)


def _fusd(v: float, w: int = 8) -> str:
    if v != v:
        return "n/a".rjust(w)
    s = "+" if v >= 0 else ""
    return f"{s}{v:.2f}".rjust(w)


def _fwr(v: float, w: int = 6) -> str:
    if v != v:
        return "n/a".rjust(w)
    return f"{v:.1f}%".rjust(w)


def _fdays(v: float, w: int = 6) -> str:
    if v != v:
        return "n/a".rjust(w)
    return f"{v:.1f}d".rjust(w)


def _first_bet_days(db_path: str, now: datetime) -> float:
    """Return days since first bet in strategy_bets table, or nan."""
    try:
        import duckdb as _ddb
        with _ddb.connect(db_path, read_only=True) as cn:
            row = cn.execute("SELECT MIN(placed_at) FROM strategy_bets").fetchone()
        if not row or row[0] is None:
            return float("nan")
        ft = row[0]
        if hasattr(ft, "timestamp"):
            first_dt = ft.astimezone(timezone.utc) if ft.tzinfo else ft.replace(tzinfo=timezone.utc)
        elif isinstance(ft, (int, float)):
            first_dt = datetime.fromtimestamp(float(ft), tz=timezone.utc)
        else:
            s = str(ft).replace("Z", "+00:00")
            if "+" not in s[10:]:
                s += "+00:00"
            first_dt = datetime.fromisoformat(s)
        return (now - first_dt).total_seconds() / 86400
    except Exception:
        return float("nan")


def report(data_dir: str):
    import duckdb as _ddb

    now = datetime.now(timezone.utc)
    pattern = os.path.join(data_dir, "bot_state_*.duckdb")
    db_files = sorted(glob.glob(pattern))

    if not db_files:
        print(f"No bot_state_*.duckdb files found in {data_dir}")
        return

    rows = []
    for db_path in db_files:
        fname    = os.path.basename(db_path)
        strategy = fname.replace("bot_state_", "").replace(".duckdb", "")
        try:
            account    = BotAccount(db_path)
            state      = account.get_state()
            totals     = account.get_totals()
            # total_bank_sol field stores USD balance (migrated from SOL)
            bank       = state["total_bank_sol"]
            total_bets = totals["total_bets"]
            win_rate   = (totals["total_wins"] / total_bets * 100
                          if total_bets > 0 else float("nan"))

            # MaxDD from snapshot history
            with _ddb.connect(db_path, read_only=True) as cn:
                snap_rows = cn.execute(
                    "SELECT total_bank_sol FROM strategy_snapshots ORDER BY ts ASC"
                ).fetchall()

            if snap_rows:
                banks = [s[0] for s in snap_rows] + [bank]
                peak  = banks[0]
                mxdd  = 0.0
                for b in banks:
                    if b > peak:
                        peak = b
                    dd = (peak - b) / peak if peak > 0 else 0.0
                    if dd > mxdd:
                        mxdd = dd
                max_dd_pct = mxdd * 100
            else:
                max_dd_pct = 0.0

            # Snapshot-based deltas
            def _snap(secs):
                return account.get_snapshot_near(
                    datetime.fromtimestamp(now.timestamp() - secs, tz=timezone.utc))

            def _delta(snap):
                if snap is None:
                    return float("nan"), float("nan")
                d    = bank - snap["total_bank_sol"]
                base = snap["total_bank_sol"]
                return d, (d / base if base > 0 else 0.0)

            d_abs_24h, d_pct_24h = _delta(_snap(86400))
            d_abs_48h, _         = _delta(_snap(172800))
            _, d_pct_1h          = _delta(_snap(3600))
            _, d_pct_12h         = _delta(_snap(43200))

            days  = _first_bet_days(db_path, now)
            alias = ALIASES.get(strategy, strategy[:13])
            cfg   = STRATEGIES.get(strategy)
            rule  = _describe(cfg) if cfg else ""

            rows.append({
                "strategy":  strategy,
                "alias":     alias,
                "bank":      bank,
                "pnl":       bank - PAPER_START_USD,
                "d_abs_24h": d_abs_24h,
                "d_abs_48h": d_abs_48h,
                "d_pct_1h":  d_pct_1h,
                "d_pct_12h": d_pct_12h,
                "d_pct_24h": d_pct_24h,
                "wr":        win_rate,
                "bets":      total_bets,
                "days":      days,
                "max_dd":    max_dd_pct,
                "rule":      rule,
            })
        except Exception as e:
            rows.append({
                "strategy": strategy,
                "alias":    ALIASES.get(strategy, strategy[:13]),
                "error":    str(e),
            })

    # Sort by balance descending
    rows.sort(key=lambda r: r.get("bank", float("-inf")), reverse=True)

    # Column widths
    W_NUM  = 3
    W_ALI  = 13
    W_PNL  = 8    # "+99.99"
    W_BANK = 8    # "199.99"
    W_D24  = 8    # "+9.999"
    W_D48  = 8    # "+9.999"
    W_P1H  = 7    # "+0.50%"
    W_P12H = 7
    W_P24H = 7
    W_WR   = 6    # "52.3%"
    W_BETS = 6    # "12345"
    W_DAYS = 6    # "2.5d"
    W_MXDD = 7    # "10.0%"
    SEP    = "  "
    cc     = _CC if _USE_COLOR else 0
    W      = 140  # table width

    def hc(s, w, col=C.WHITE):
        if _USE_COLOR:
            return _c(s, C.BOLD, col).rjust(w + cc)
        return s.rjust(w)

    print()
    print(_c("=" * W, C.BOLD, C.WHITE))
    ts = now.strftime("%Y-%m-%d  %H:%M:%S UTC")
    print(_c(f"  PAPER BOTS  --  {ts}  ({len(rows)} bots)  start=${PAPER_START_USD:.0f}", C.BOLD, C.WHITE))
    print(_c("=" * W, C.BOLD, C.WHITE))

    hdr = (
        f"  {'#':>{W_NUM}}"
        f"{SEP}{_c('Стратегия', C.BOLD, C.WHITE):<{W_ALI + cc}}"
        f"{SEP}{hc('P&L $', W_PNL)}"
        f"{SEP}{hc('Bal $', W_BANK)}"
        f"{SEP}{hc('$24h', W_D24, C.CYAN)}"
        f"{SEP}{hc('$48h', W_D48)}"
        f"{SEP}{hc('%1h', W_P1H, C.CYAN)}"
        f"{SEP}{hc('%12h', W_P12H)}"
        f"{SEP}{hc('%24h', W_P24H)}"
        f"{SEP}{hc('WR', W_WR)}"
        f"{SEP}{hc('Bets', W_BETS)}"
        f"{SEP}{hc('Days', W_DAYS)}"
        f"{SEP}{hc('MaxDD', W_MXDD, C.YELLOW)}"
        f"  {_c('Strategy', C.BOLD, C.GREY)}"
    )
    print(hdr)
    print(_c("  " + "-" * (W - 2), C.GREY))

    for i, r in enumerate(rows):
        num_str = str(i + 1).rjust(W_NUM)

        if "error" in r:
            print(f"  {num_str}{SEP}{_c(r['alias'], C.YELLOW):<{W_ALI}}  "
                  f"{_c('ERR: ' + r['error'][:65], C.RED)}")
            continue

        pnl  = r["pnl"]
        bank = r["bank"]
        dd   = r["max_dd"]
        sc   = C.GREEN if pnl >= 0 else C.RED
        ddc  = (C.RED if dd >= 5.0 else C.YELLOW if dd >= 2.0 else C.GREY)

        dd_str = f"{dd:.1f}%".rjust(W_MXDD)
        dd_out = (_c(dd_str, C.BOLD, C.RED) if dd >= 5.0 else _c(dd_str, ddc))

        line = (
            f"  {num_str}"
            f"{SEP}{_c(r['alias'], C.BOLD, sc):<{W_ALI + cc}}"
            f"{SEP}{_vc(pnl, _fusd(pnl, W_PNL))}"
            f"{SEP}{_c(f'{bank:.2f}'.rjust(W_BANK), C.WHITE)}"
            f"{SEP}{_vc(r['d_abs_24h'], _fusd(r['d_abs_24h'], W_D24))}"
            f"{SEP}{_vc(r['d_abs_48h'], _fusd(r['d_abs_48h'], W_D48))}"
            f"{SEP}{_vc(r['d_pct_1h'],  _fpct(r['d_pct_1h'],  W_P1H))}"
            f"{SEP}{_vc(r['d_pct_12h'], _fpct(r['d_pct_12h'], W_P12H))}"
            f"{SEP}{_vc(r['d_pct_24h'], _fpct(r['d_pct_24h'], W_P24H))}"
            f"{SEP}{_fwr(r['wr'], W_WR)}"
            f"{SEP}{str(r['bets']):>{W_BETS}}"
            f"{SEP}{_fdays(r['days'], W_DAYS)}"
            f"{SEP}{dd_out}"
            f"  {_c(r['rule'], C.GREY)}"
        )
        print(line)

    print(_c("=" * W, C.BOLD, C.WHITE))

    valid = [r for r in rows if "error" not in r]
    if valid:
        top3 = [r for r in valid if r["pnl"] > 0][:3]
        if top3:
            medals = ["🥇", "🥈", "🥉"]
            parts = [f"{medals[i]} {r['alias']} {_fusd(r['pnl']).strip()}"
                     for i, r in enumerate(top3)]
            print(f"\n  Top:  {'  '.join(parts)}")
        worst = valid[-1]
        print(f"  Worst: {worst['alias']}  {_fusd(worst['pnl']).strip()}")
    print()


def report_realbet(data_dir: str):
    import json as _json
    from pathlib import Path as _P

    state_f = _P(data_dir) / "realbet_state.json"
    snap_f  = _P(data_dir) / "realbet_snapshots.json"
    if not state_f.exists():
        return
    try:
        st    = _json.loads(state_f.read_text())
        snaps = _json.loads(snap_f.read_text()) if snap_f.exists() else []
    except Exception:
        return

    now     = datetime.now(timezone.utc)
    bal_usd = st.get("last_balance_usd", LIVE_START_USD)
    pnl_all = bal_usd - LIVE_START_USD
    pnl_pct = pnl_all / LIVE_START_USD if LIVE_START_USD > 0 else 0.0
    bets    = st.get("bets", 0)
    wins    = st.get("wins", 0)
    wr      = wins / bets * 100 if bets > 0 else float("nan")

    def _snap_d(secs):
        if not snaps:
            return float("nan"), float("nan")
        target = now.timestamp() - secs
        best   = min(snaps, key=lambda s:
                     abs(datetime.fromisoformat(
                         s["ts"].replace("Z", "+00:00")).timestamp() - target))
        old = best.get("balance_usd", LIVE_START_USD)
        d   = bal_usd - old
        return d, (d / old if old > 0 else 0.0)

    d_abs_24h, d_pct_24h = _snap_d(86400)
    d_abs_48h, _         = _snap_d(172800)
    _, d_pct_1h          = _snap_d(3600)
    _, d_pct_12h         = _snap_d(43200)

    W   = 140
    cc  = _CC if _USE_COLOR else 0
    SEP = "  "

    print()
    print(_c("=" * W, C.BOLD, C.YELLOW))
    ts = now.strftime("%Y-%m-%d  %H:%M:%S UTC")
    print(_c(
        f"  REAL MONEY BOT  --  {ts}  --  start=${LIVE_START_USD:.2f} USDT",
        C.BOLD, C.YELLOW))
    print(_c("=" * W, C.BOLD, C.YELLOW))

    hdr = (
        f"  {'#':>3}"
        f"{SEP}{_c('Стратегия', C.BOLD, C.WHITE):<{13 + cc}}"
        f"{SEP}{_c('P&L $',  C.BOLD, C.WHITE):>{8 + cc}}"
        f"{SEP}{_c('Bal $',  C.BOLD, C.WHITE):>{8 + cc}}"
        f"{SEP}{_c('$24h',   C.BOLD, C.CYAN):>{8 + cc}}"
        f"{SEP}{_c('$48h',   C.BOLD, C.WHITE):>{8 + cc}}"
        f"{SEP}{_c('%1h',    C.BOLD, C.CYAN):>{7 + cc}}"
        f"{SEP}{_c('%12h',   C.BOLD, C.WHITE):>{7 + cc}}"
        f"{SEP}{_c('%24h',   C.BOLD, C.WHITE):>{7 + cc}}"
        f"{SEP}{_c('WR',     C.BOLD, C.WHITE):>{6 + cc}}"
        f"{SEP}{_c('Bets',   C.BOLD, C.WHITE):>{6 + cc}}"
    )
    print(hdr)
    print(_c("  " + "-" * (W - 2), C.GREY))

    strategy = st.get("strategy", "realbet")
    alias    = ALIASES.get(strategy, strategy[:13])
    sc       = C.GREEN if pnl_all >= 0 else C.RED

    note = ""
    if bets > 0 and wins == 0:
        note = _c("  !! wins=0 -- balance reader may be broken", C.RED, C.BOLD)
    elif wr != wr or (bets > 20 and wr < 20):
        note = _c("  ! low WR -- check cashout", C.YELLOW)

    line = (
        f"  {'1':>3}"
        f"{SEP}{_c(alias + '_LIVE', C.BOLD, sc):<{13 + cc}}"
        f"{SEP}{_vc(pnl_all,   _fusd(pnl_all,   8))}"
        f"{SEP}{_c(f'{bal_usd:.2f}'.rjust(8), C.WHITE)}"
        f"{SEP}{_vc(d_abs_24h, _fusd(d_abs_24h, 8))}"
        f"{SEP}{_vc(d_abs_48h, _fusd(d_abs_48h, 8))}"
        f"{SEP}{_vc(d_pct_1h,  _fpct(d_pct_1h,  7))}"
        f"{SEP}{_vc(d_pct_12h, _fpct(d_pct_12h, 7))}"
        f"{SEP}{_vc(d_pct_24h, _fpct(d_pct_24h, 7))}"
        f"{SEP}{_fwr(wr, 6)}"
        f"{SEP}{str(bets):>6}"
        f"{note}"
    )
    print(line)
    print(_c("=" * W, C.BOLD, C.YELLOW))
    print()


def main():
    global _USE_COLOR, _CC
    parser = argparse.ArgumentParser(description="All-bot stats reporter")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()
    if args.no_color:
        _USE_COLOR = False
        _CC = 0
    report(args.data_dir)
    report_realbet(args.data_dir)


if __name__ == "__main__":
    main()
