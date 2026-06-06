"""Phase 4: bot_backtest_table writes dated .txt/.md/.csv with generation date,
data period, and round-completeness (missing rounds)."""
import csv
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_backtest_table as bt


def _row(name, final, maxdd, bets=100):
    return {"name": name, "final": final, "pnl_24h": final - 100, "pnl_48h": final - 100,
            "pct_1h": 0.1, "pct_12h": 0.5, "pct_24h": final - 100, "win_rate": 50.0,
            "bets": bets, "days": 2.0, "max_dd": maxdd, "cfg": bt.STRATEGIES["x5"]}


def test_writes_dated_files_with_completeness(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)
    results = [_row("alpha", 101.0, 0.5), _row("beta", 99.0, 7.0)]
    meta = {
        "generated": "2026-06-06 10:00 local (Europe/Chisinau) / 07:00 UTC",
        "period_start": "2026-05-19 08:24", "period_end": "2026-06-06 04:00",
        "distinct": 41871, "expected": 53749, "missing": 11878, "coverage": 77.9,
    }
    bt.print_table(results, n_rounds=41871, total_days=18.0, meta=meta)

    md_files = glob.glob("data/backtest_table_*.md")
    csv_files = glob.glob("data/backtest_table_*.csv")
    txt_files = glob.glob("data/backtest_table_*.txt")
    assert len(md_files) == 1 and len(csv_files) == 1 and len(txt_files) == 1

    md = open(md_files[0], encoding="utf-8").read()
    assert "2026-06-06 10:00 local (Europe/Chisinau) / 07:00 UTC" in md   # gen date
    assert "Data period: 2026-05-19 08:24 -> 2026-06-06 04:00" in md       # range
    assert "MISSING 11,878" in md                                         # completeness
    assert "77.9" in md
    assert "\U0001F947" in md          # gold medal on top profitable (alpha)
    assert "\U0001F534" in md          # red dot on beta (MaxDD 7% > 5%)

    rows = list(csv.reader(open(csv_files[0], encoding="utf-8")))
    assert rows[0][0] == "#" and "Balance" in rows[0]
    assert len(rows) == 3              # header + 2 strategies


def test_now_dual_has_both_zones():
    s = bt._now_dual()
    assert "UTC" in s


def test_no_meta_still_works(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)
    bt.print_table([_row("solo", 100.5, 0.1)], n_rounds=10, total_days=1.0)  # meta=None
    assert len(glob.glob("data/backtest_table_*.csv")) == 1
