#!/usr/bin/env python3
"""
paper_table.py
LIVE status table for all paper bots (reads data/bot_state_*.duckdb).
Start bank was $100. Renders the screenshot table from LIVE balances and
writes data/paper_table.{txt,md,csv} (CSV twin per project rule).

Read-only on bot state DBs. Safe to run while bots are live.
"""
import csv
import glob
import os
import sys
from datetime import datetime, timezone, timedelta

import duckdb

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot_state import BotAccount
from bot_strategy import STRATEGIES

DATA_DIR   = "data"
START_BANK = 100.0
EXCLUDE    = {"turbo"}   # B1: blow-up strategy, never shown / never run


def _cfg_details(name: str) -> str:
    cfg = STRATEGIES.get(name)
    if cfg is None:
        return "n/a"
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


def _maxdd_and_days(db_path: str, now: datetime) -> tuple:
    """Compute live MaxDD (% from running peak) and Days-of-play from
    the strategy_snapshots series. Returns (max_dd_pct, days)."""
    try:
        with duckdb.connect(db_path, read_only=True) as c:
            rows = c.execute(
                "SELECT ts, total_bank_sol FROM strategy_snapshots ORDER BY ts ASC"
            ).fetchall()
    except Exception:
        return 0.0, 0.0
    if not rows:
        return 0.0, 0.0
    peak = START_BANK
    max_dd = 0.0
    for _ts, bank in rows:
        if bank is None:
            continue
        if bank > peak:
            peak = bank
        dd = (peak - bank) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    t0 = rows[0][0]
    try:
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=timezone.utc)
        days = (now - t0).total_seconds() / 86400
    except Exception:
        days = 0.0
    return max_dd * 100, max(days, 0.0)


def _delta(bank, snap):
    """Return (abs_pnl, pct) vs a snapshot. Falls back to since-$100 if no snap."""
    if snap is None:
        base = START_BANK
    else:
        base = snap["total_bank_sol"]
    pnl = bank - base
    pct = (pnl / base * 100) if base > 0 else 0.0
    return pnl, pct


def collect_rows():
    now = datetime.now(timezone.utc)
    rows = []
    for db_path in sorted(glob.glob(os.path.join(DATA_DIR, "bot_state_*.duckdb"))):
        name = os.path.basename(db_path).replace("bot_state_", "").replace(".duckdb", "")
        if not name or name in EXCLUDE:
            continue
        try:
            acc    = BotAccount(db_path)
            state  = acc.get_state()
            totals = acc.get_totals()
            bank   = state["total_bank_sol"]
            bets   = totals["total_bets"]
            wins   = totals["total_wins"]
            wr     = (wins / bets * 100) if bets > 0 else float("nan")

            s1  = acc.get_snapshot_near(now - timedelta(hours=1))
            s12 = acc.get_snapshot_near(now - timedelta(hours=12))
            s24 = acc.get_snapshot_near(now - timedelta(hours=24))
            s48 = acc.get_snapshot_near(now - timedelta(hours=48))

            pnl24, _    = _delta(bank, s24)
            pnl48, _    = _delta(bank, s48)
            _, pct1     = _delta(bank, s1)
            _, pct12    = _delta(bank, s12)
            _, pct24    = _delta(bank, s24)

            max_dd, days = _maxdd_and_days(db_path, now)

            rows.append({
                "name": name, "bank": bank, "pnl24": pnl24, "pnl48": pnl48,
                "pct1": pct1, "pct12": pct12, "pct24": pct24,
                "wr": wr, "bets": bets, "days": days, "max_dd": max_dd,
                "details": _cfg_details(name),
            })
        except Exception as exc:
            rows.append({"name": name, "error": str(exc)})

    rows.sort(key=lambda r: r.get("bank", -1e9), reverse=True)
    return rows, now


# ── formatting helpers ───────────────────────────────────────────────────────

def _usd(v):
    if v != v:
        return "--"
    if abs(v) < 0.005:
        return "--"
    return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"


def _pct(v):
    if v != v or abs(v) < 0.0005:
        return "--"
    return f"{v:+.2f}%"


HDR = ["#", "Стратегия", "Balance $ total", "P&L $ last 24h", "P&L $ last 48h",
       "%1h", "%12h", "%24h", "WR", "Bets", "Days of play", "MaxDD",
       "Полные детали стратегии"]


def _row_cells(idx, r):
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    if "error" in r:
        return [str(idx), r["name"], "ERR", "", "", "", "", "", "", "", "", "", r["error"]]
    rank = idx if r["bank"] > START_BANK else None
    medal = medals.get(rank, "") if rank and rank <= 3 else ""
    mdd_flag = "🔴" if r["max_dd"] > 5 else ""
    wr = "--" if r["wr"] != r["wr"] else f"{r['wr']:.1f}%"
    return [
        str(idx),
        (medal + r["name"]),
        f"${r['bank']:.2f}",
        _usd(r["pnl24"]),
        _usd(r["pnl48"]),
        _pct(r["pct1"]),
        _pct(r["pct12"]),
        _pct(r["pct24"]),
        wr,
        str(r["bets"]),
        f"{r['days']:.1f}d",
        f"{mdd_flag}{r['max_dd']:.2f}%",
        r["details"],
    ]


def write_md(rows, now, path):
    lines = []
    prof = sum(1 for r in rows if "error" not in r and r["bank"] > START_BANK)
    lines.append(f"## 📊 Бумажные боты — LIVE | $100 старт | {now.strftime('%Y-%m-%d %H:%M')} UTC")
    lines.append("")
    lines.append(f"Прибыльных: {prof} / {len(rows)}. 🔴 = MaxDD > 5%.")
    lines.append("")
    lines.append("| " + " | ".join(HDR) + " |")
    align = ["--:", "---"] + ["--:"] * 10 + ["---"]
    lines.append("| " + " | ".join(align) + " |")
    for i, r in enumerate(rows, 1):
        cells = _row_cells(i, r)
        if r.get("max_dd", 0) > 5 and "error" not in r:
            cells[2] = f"**{cells[2]}**"
        lines.append("| " + " | ".join(cells) + " |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_csv(rows, path):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HDR)
        for i, r in enumerate(rows, 1):
            w.writerow(_row_cells(i, r))


def write_txt(rows, now, path):
    W = [3, 38, 11, 12, 12, 8, 8, 8, 7, 7, 8, 9]
    out = []
    prof = sum(1 for r in rows if "error" not in r and r["bank"] > START_BANK)
    out.append(f"  Бумажные боты LIVE -- {len(rows)} | $100 старт | {now.strftime('%Y-%m-%d %H:%M')} UTC | прибыльных {prof}/{len(rows)}")
    out.append("")
    hdr = ("Strategy", "Balance$", "PnL24h", "PnL48h", "%1h", "%12h", "%24h", "WR", "Bets", "Days", "MaxDD")
    line = f"  {'#':>{W[0]}}  {hdr[0]:<{W[1]}}  {hdr[1]:>{W[2]}}  {hdr[2]:>{W[3]}}  {hdr[3]:>{W[4]}}  {hdr[4]:>{W[5]}}  {hdr[5]:>{W[6]}}  {hdr[6]:>{W[7]}}  {hdr[7]:>{W[8]}}  {hdr[8]:>{W[9]}}  {hdr[9]:>{W[10]}}  {hdr[10]:>{W[11]}}  Details"
    out.append(line)
    out.append("  " + "-" * (len(line) - 2))
    for i, r in enumerate(rows, 1):
        if "error" in r:
            out.append(f"  {i:>{W[0]}}  {r['name']:<{W[1]}}  ERROR: {r['error']}")
            continue
        wr = "--" if r["wr"] != r["wr"] else f"{r['wr']:.1f}%"
        mdd = ("*" if r["max_dd"] > 5 else "") + f"{r['max_dd']:.2f}%"
        out.append(
            f"  {i:>{W[0]}}  {r['name']:<{W[1]}}  ${r['bank']:>{W[2]-1}.2f}  "
            f"{_usd(r['pnl24']):>{W[3]}}  {_usd(r['pnl48']):>{W[4]}}  "
            f"{_pct(r['pct1']):>{W[5]}}  {_pct(r['pct12']):>{W[6]}}  {_pct(r['pct24']):>{W[7]}}  "
            f"{wr:>{W[8]}}  {r['bets']:>{W[9]}}  {r['days']:>{W[10]-1}.1f}d  {mdd:>{W[11]}}  {r['details']}"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return "\n".join(out)


def main():
    rows, now = collect_rows()
    txt = write_txt(rows, now, os.path.join(DATA_DIR, "paper_table.txt"))
    write_md(rows, now, os.path.join(DATA_DIR, "paper_table.md"))
    write_csv(rows, os.path.join(DATA_DIR, "paper_table.csv"))
    print(txt)
    print(f"\n  wrote: {DATA_DIR}/paper_table.txt | .md | .csv")


if __name__ == "__main__":
    main()
