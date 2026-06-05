content = open('/root/crash-collector/bot_stats_all.py', encoding='utf-8').read()

# 1. Add new snapshot fields to paper bot data rows
old_row = '''            rows.append({
                "kind":     "paper",
                "strategy": strategy,
                "status":   "active",
                "cashout":  cfg.cashout      if cfg else float("nan"),
                "bank_pct": cfg.session_frac * 100 if cfg else float("nan"),
                "bank":     bank,
                "pnl":      pnl,
                "d24_live": float("nan"),
                "d1_pct":   _dpct(_snap(3600)),
                "d24_pct":  _dpct(_snap(86400)),
                "wr":       wr,
                "bets":     bets,
                "max_dd":   mxdd * 100,
                "bt_roi":   BT_ROI.get(strategy, float("nan")),
                "rule":     RULES.get(strategy, ""),
            })'''

new_row = '''            def _dabs(sn):
                if sn is None: return float("nan")
                return bank - sn["total_bank_sol"]

            # Days of play
            try:
                first_ts = db.execute("SELECT MIN(ts) FROM strategy_bets").fetchone()
                days_play = (datetime.now(timezone.utc) - first_ts[0]).total_seconds() / 86400 if first_ts and first_ts[0] else float("nan")
            except:
                days_play = float("nan")

            rows.append({
                "kind":     "paper",
                "strategy": strategy,
                "status":   "active",
                "cashout":  cfg.cashout      if cfg else float("nan"),
                "bank_pct": cfg.session_frac * 100 if cfg else float("nan"),
                "bank":     bank,
                "pnl":      pnl,
                "d24_live": float("nan"),
                "d1_pct":   _dpct(_snap(3600)),
                "d12_pct":  _dpct(_snap(12*3600)),
                "d24_pct":  _dpct(_snap(86400)),
                "d24_abs":  _dabs(_snap(86400)),
                "d48_abs":  _dabs(_snap(48*3600)),
                "wr":       wr,
                "bets":     bets,
                "max_dd":   mxdd * 100,
                "days_play": days_play,
                "bt_roi":   BT_ROI.get(strategy, float("nan")),
                "rule":     RULES.get(strategy, ""),
            })'''

assert old_row in content, 'row pattern not found'
content = content.replace(old_row, new_row, 1)

# 2. Update column widths
old_w = '''    "kind":  5,    # "paper" / "live"
    "name":  26,   # strategy name (truncated)
    "stat":  6,    # "active" / "error"
    "co":    5,    # cashout "2.30"
    "bp":    5,    # bank%   " 100%"
    "bank":  13,   # "100.807245   " / "   $13.0984"
    "pnl":   11,   # " +0.807245" / "  -$0.9054"
    "d24":   10,   # "  -$0.3112" / "       n/a"
    "d1":    7,    # " -0.02%"
    "d24p":  7,    # " +0.02%"
    "wr":    7,    # " 43.3%"
    "bets":  6,    # "  7407"
    "mdd":   7,    # "  0.45%" / "    n/a"
}'''

new_w = '''    "kind":  5,    # "paper" / "live"
    "name":  40,   # strategy name
    "bank":  13,   # Balance $ total
    "d24a":  12,   # P&L $ last 24h
    "d48a":  12,   # P&L $ last 48h
    "d1":    7,    # %1h
    "d12p":  7,    # 12%
    "d24p":  7,    # %24h
    "d24l":  10,   # 24h live (live bot only)
    "wr":    7,    # WR
    "bets":  7,    # Bets
    "days":  13,   # Days of play
    "mdd":   7,    # MaxDD
}'''

assert old_w in content, 'width pattern not found'
content = content.replace(old_w, new_w, 1)

# 3. Update header function
old_hdr = '''def _hdr_line():
    cols = [
        ("Kind",         _W["kind"],  "l"),
        ("Name",         _W["name"],  "l"),
        ("Status",       _W["stat"],  "l"),
        ("Cash",         _W["co"],    "r"),
        ("Bank%",        _W["bp"],    "r"),
        ("Balance $",     _W["bank"],  "r"),
        ("P&L All",      _W["pnl"],   "r"),
        ("24h Live",     _W["d24"],   "r"),
        ("%1h",          _W["d1"],    "r"),
        ("%24h",         _W["d24p"],  "r"),
        ("WinRate",      _W["wr"],    "r"),
        ("Bets",         _W["bets"],  "r"),
        ("MaxDD",        _W["mdd"],   "r"),
        ("Rule",         0,           "l"),
    ]'''

new_hdr = '''def _hdr_line():
    cols = [
        ("#",                4,            "r"),
        ("Стратегия",        _W["name"],   "l"),
        ("Balance $ total",  _W["bank"],   "r"),
        ("P&L $ last 24h",   _W["d24a"],  "r"),
        ("P&L $ last 48h",   _W["d48a"],  "r"),
        ("%1h",              _W["d1"],    "r"),
        ("12%",              _W["d12p"],  "r"),
        ("%24h",             _W["d24p"],  "r"),
        ("24h",              _W["d24l"],  "r"),
        ("WR",               _W["wr"],    "r"),
        ("Bets",             _W["bets"],  "r"),
        ("Days of play",     _W["days"],  "r"),
        ("MaxDD",            _W["mdd"],   "r"),
        ("Полные детали",    0,           "l"),
    ]'''

assert old_hdr in content, 'header pattern not found'
content = content.replace(old_hdr, new_hdr, 1)

open('/root/crash-collector/bot_stats_all.py', 'w', encoding='utf-8').write(content)
print('Columns and header updated')
