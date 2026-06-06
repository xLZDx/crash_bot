#!/usr/bin/env python3
"""
reconcile_rounds.py
Round-history integrity + reconciliation for the BCGame crash collector.

- Detects LOST rounds: gaps in the game_round_id sequence in crash.duckdb.
- Classifies gaps: outages (big, collector downtime) vs frame-drops (small).
- Reconciles bets: every bet (paper strategy_bets + real audit) must reference a
  round that exists in `rounds` with an outcome; flags "orphan" bets whose round
  is missing (unsettleable / unreconcilable).
- Writes data/reconcile_<UTC>.{md,csv,txt} (CSV twin per project rule) with the
  generation date (local + UTC) and the data date-range.

Read-only on every DB (retry on the collector/bot write-lock).
"""
import csv
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone

import duckdb

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CRASH_DB = "data/crash.duckdb"
DATA_DIR = "data"
AUDIT_FILE = "data/realbet_execution_audit.jsonl"
OUTAGE_THRESHOLD = 50   # gap >= this many missing rounds = outage; below = frame-drop


# ── pure, testable core ──────────────────────────────────────────────────────

def find_gaps(gids):
    """gids: sorted ascending unique ints. Returns [(missing, after_gid, before_gid)]."""
    gaps = []
    for i in range(1, len(gids)):
        d = gids[i] - gids[i - 1]
        if d > 1:
            gaps.append((d - 1, gids[i - 1], gids[i]))
    return gaps


def classify_gaps(gaps, threshold=OUTAGE_THRESHOLD):
    outages = [g for g in gaps if g[0] >= threshold]
    drops = [g for g in gaps if g[0] < threshold]
    return outages, drops


def find_orphan_bets(round_gids, bet_gids):
    """Return the subset of bet_gids NOT present in round_gids (unreconcilable)."""
    rset = round_gids if isinstance(round_gids, set) else set(round_gids)
    return [g for g in bet_gids if g not in rset]


def coverage_pct(distinct, expected):
    return (distinct / expected * 100.0) if expected else 0.0


# ── dual-time helper ─────────────────────────────────────────────────────────

def _now_dual():
    now = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        loc = now.astimezone(ZoneInfo("Europe/Chisinau"))
        return (f"{loc.strftime('%Y-%m-%d %H:%M')} local (Europe/Chisinau) / "
                f"{now.strftime('%H:%M')} UTC")
    except Exception:
        return f"{now.strftime('%Y-%m-%d %H:%M')} UTC"


# ── DB I/O (read-only, lock-retry) ───────────────────────────────────────────

from db_util import connect_ro as _connect_ro


def load_round_gids(crash_db=CRASH_DB):
    """Return (sorted_unique_gids, meta) from crash.duckdb rounds.
    TRY_CAST drops any non-numeric game_round_id instead of poisoning the query."""
    with _connect_ro(crash_db) as c:
        rows = c.execute(
            "SELECT DISTINCT g FROM (SELECT TRY_CAST(game_round_id AS BIGINT) g "
            "FROM rounds) WHERE g IS NOT NULL ORDER BY g"
        ).fetchall()
        gids = [r[0] for r in rows]
        tmin, tmax = c.execute("SELECT MIN(ts), MAX(ts) FROM rounds").fetchone()
    # union recovered rounds from the separate backfill store (backfill_rounds.py)
    backfilled = 0
    bf = os.path.join(os.path.dirname(crash_db) or ".", "backfill_rounds.duckdb")
    if os.path.exists(bf):
        try:
            with _connect_ro(bf) as bc:
                brows = bc.execute(
                    "SELECT DISTINCT TRY_CAST(game_round_id AS BIGINT) g FROM backfill_rounds"
                ).fetchall()
            bset = {r[0] for r in brows if r[0] is not None}
            before = len(gids)
            gids = sorted(set(gids) | bset)
            backfilled = len(gids) - before
        except Exception:
            pass
    meta = {"ts_min": tmin, "ts_max": tmax, "backfilled": backfilled}
    return gids, meta


def load_paper_bet_gids(data_dir=DATA_DIR):
    """Return {strategy: [game_round_id ints]} for each bot_state_*.duckdb."""
    out = {}
    for p in sorted(glob.glob(os.path.join(data_dir, "bot_state_*.duckdb"))):
        nm = os.path.basename(p).replace("bot_state_", "").replace(".duckdb", "")
        if not nm:
            continue
        try:
            with _connect_ro(p) as c:
                rows = c.execute(
                    "SELECT TRY_CAST(game_round_id AS BIGINT) FROM strategy_bets"
                ).fetchall()
            out[nm] = [r[0] for r in rows if r[0] is not None]
        except Exception as e:
            out[nm] = {"error": str(e)[:80]}
    return out


def load_real_bet_gids(audit_file=AUDIT_FILE):
    """Return [game_round_id ints] for real-money settled bets in the audit."""
    gids = []
    if not os.path.exists(audit_file):
        return gids
    with open(audit_file, errors="ignore") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("event") in ("result", "result_db", "result_balance"):
                g = r.get("game_round_id")
                if g is not None:
                    try:
                        gids.append(int(g))
                    except Exception:
                        pass
    return gids


# ── report ───────────────────────────────────────────────────────────────────

def build_report():
    gids, meta = load_round_gids()
    distinct = len(gids)
    if distinct:
        gmin, gmax = gids[0], gids[-1]
        expected = gmax - gmin + 1
        missing = expected - distinct
    else:
        gmin = gmax = expected = missing = 0
    gaps = find_gaps(gids)
    outages, drops = classify_gaps(gaps)
    round_set = set(gids)

    # bet reconciliation
    paper = load_paper_bet_gids()
    paper_recon = {}
    for nm, bets in paper.items():
        if isinstance(bets, dict):  # error
            paper_recon[nm] = {"error": bets["error"]}
            continue
        orphans = find_orphan_bets(round_set, bets)
        paper_recon[nm] = {"bets": len(bets), "orphans": len(orphans)}
    real_bets = load_real_bet_gids()
    real_orphans = find_orphan_bets(round_set, real_bets)

    return {
        "generated": _now_dual(),
        "ts_min": meta["ts_min"], "ts_max": meta["ts_max"],
        "backfilled": meta.get("backfilled", 0),
        "gmin": gmin, "gmax": gmax, "expected": expected,
        "distinct": distinct, "missing": missing,
        "coverage": coverage_pct(distinct, expected),
        "gaps": gaps, "outages": outages, "drops": drops,
        "paper_recon": paper_recon,
        "real": {"bets": len(real_bets), "orphans": len(real_orphans)},
    }


def write_reports(rep, data_dir=DATA_DIR):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = os.path.join(data_dir, f"reconcile_{stamp}")

    md = []
    md.append(f"# Round reconciliation -- {rep['generated']}")
    md.append("")
    md.append(f"- Data period: `{rep['ts_min']}` -> `{rep['ts_max']}`")
    md.append(f"- game_round_id range: {rep['gmin']} -> {rep['gmax']} "
              f"(expected {rep['expected']:,})")
    md.append(f"- Collected (distinct): **{rep['distinct']:,}**  | "
              f"MISSING: **{rep['missing']:,}** | coverage **{rep['coverage']:.2f}%**")
    md.append(f"- Gap-spans: {len(rep['gaps'])} "
              f"(outages>={OUTAGE_THRESHOLD}: {len(rep['outages'])}, "
              f"frame-drops: {len(rep['drops'])})")
    md.append("")
    md.append("## Outage gaps (collector downtime)")
    md.append("| missing | after_gid | before_gid |")
    md.append("|--:|--:|--:|")
    for g in sorted(rep["outages"], reverse=True):
        md.append(f"| {g[0]:,} | {g[1]} | {g[2]} |")
    md.append("")
    md.append("## Bet reconciliation (orphan = round missing from history)")
    md.append("| source | bets | orphan bets |")
    md.append("|---|--:|--:|")
    md.append(f"| REAL (audit) | {rep['real']['bets']} | {rep['real']['orphans']} |")
    for nm, r in sorted(rep["paper_recon"].items()):
        if "error" in r:
            md.append(f"| paper {nm} | ERR | {r['error']} |")
        else:
            md.append(f"| paper {nm} | {r['bets']} | {r['orphans']} |")
    md_text = "\n".join(md) + "\n"

    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(md_text)

    # CSV twin (per rule): one row per gap + one row per bet-source, section column
    with open(base + ".csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["section", "key", "missing_or_bets", "a_or_orphans", "b"])
        w.writerow(["summary", "generated", rep["generated"], "", ""])
        w.writerow(["summary", "distinct", rep["distinct"], "missing", rep["missing"]])
        w.writerow(["summary", "coverage_pct", f"{rep['coverage']:.2f}", "", ""])
        for g in sorted(rep["gaps"], reverse=True):
            w.writerow(["gap", "", g[0], g[1], g[2]])
        w.writerow(["recon", "REAL_audit", rep["real"]["bets"],
                    rep["real"]["orphans"], ""])
        for nm, r in sorted(rep["paper_recon"].items()):
            if "error" in r:
                w.writerow(["recon", "paper_" + nm, "ERR", "", r["error"]])
            else:
                w.writerow(["recon", "paper_" + nm, r["bets"], r["orphans"], ""])

    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(md_text)

    return base


def main():
    print("Reconciling round history ...", flush=True)
    rep = build_report()
    base = write_reports(rep)
    print(f"\nData period: {rep['ts_min']} -> {rep['ts_max']}")
    print(f"Collected {rep['distinct']:,} / expected {rep['expected']:,} "
          f"-> MISSING {rep['missing']:,} (coverage {rep['coverage']:.2f}%)")
    print(f"Gap-spans: {len(rep['gaps'])} (outages {len(rep['outages'])}, "
          f"drops {len(rep['drops'])})")
    if rep.get("backfilled"):
        print(f"Recovered from backfill store: {rep['backfilled']} rounds")
    print(f"REAL bets: {rep['real']['bets']} orphans {rep['real']['orphans']}")
    errored = [nm for nm, r in rep["paper_recon"].items()
               if isinstance(r, dict) and "error" in r]
    paper_orphans = sum(r.get("orphans", 0) for r in rep["paper_recon"].values()
                        if isinstance(r, dict) and "orphans" in r)
    print(f"PAPER orphan bets (readable strategies): {paper_orphans}")
    if errored:
        print(f"WARNING: {len(errored)} bot DB(s) UNREADABLE -> orphan count is a "
              f"LOWER BOUND: {', '.join(errored)}")
    print(f"wrote: {base}.md | .csv | .txt")


if __name__ == "__main__":
    main()
