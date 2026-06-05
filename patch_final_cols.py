content = open('/root/crash-collector/bot_stats_all.py', encoding='utf-8').read()

# 1. Add pnl_total field to paper bot rows
old_days_calc = '''            rows.append({
                "kind":     "paper",
                "strategy": strategy,
                "status":   "active",
                "cashout":  cfg.cashout      if cfg else float("nan"),
                "bank_pct": cfg.session_frac * 100 if cfg else float("nan"),
                "bank":     bank,
                "pnl":      pnl,'''

new_days_calc = '''            rows.append({
                "kind":     "paper",
                "strategy": strategy,
                "status":   "active",
                "cashout":  cfg.cashout      if cfg else float("nan"),
                "bank_pct": cfg.session_frac * 100 if cfg else float("nan"),
                "bank":     bank,
                "pnl":      pnl,
                "pnl_total": bank - 100.0,'''

assert old_days_calc in content, 'rows append not found'
content = content.replace(old_days_calc, new_days_calc, 1)

# 2. Fix Days of play - use strategy_sessions or strategy_bets
old_days = '''            # Days of play
            try:
                first_ts = db.execute("SELECT MIN(ts) FROM strategy_bets").fetchone()
                days_play = (datetime.now(timezone.utc) - first_ts[0]).total_seconds() / 86400 if first_ts and first_ts[0] else float("nan")
            except:
                days_play = float("nan")'''

new_days = '''            # Days of play - from strategy_sessions (created_at) or first bet
            try:
                import duckdb as _ddb2
                with _ddb2.connect(tmp, read_only=True) as _dc:
                    try:
                        first_ts = _dc.execute("SELECT MIN(started_at) FROM strategy_sessions").fetchone()
                    except:
                        first_ts = _dc.execute("SELECT MIN(ts) FROM strategy_bets").fetchone()
                days_play = (datetime.now(timezone.utc) - first_ts[0]).total_seconds() / 86400 if first_ts and first_ts[0] else float("nan")
            except:
                days_play = float("nan")'''

assert old_days in content, 'days calc not found'
content = content.replace(old_days, new_days, 1)

# 3. Add pnl_total to column widths
old_w_bank = '    "bank":  13,   # Balance $ total'
new_w_bank = '    "pnlt":  12,   # P&L $ total (all-time)\n    "bank":  13,   # Balance $ total'
assert old_w_bank in content, 'bank width not found'
content = content.replace(old_w_bank, new_w_bank, 1)

# 4. Add pnl_total to header
old_hdr_bank = '        ("Balance $ total",  _W["bank"],   "r"),'
new_hdr_bank = '        ("P&L $ total",      _W["pnlt"],  "r"),\n        ("Balance $ total",  _W["bank"],   "r"),'
assert old_hdr_bank in content, 'header bank not found'
content = content.replace(old_hdr_bank, new_hdr_bank, 1)

# 5. Add pnl_total to row output
old_row_fmt = '''        pnl_total = r.get("pnl_total", float("nan")) if False else bank - 100.0'''
# It may not exist, so we need to add it in the row section

# Find where bank_s is defined and add pnlt_s after
old_bank_s = '        bank_s  = (f"${bank:.2f}" if bank == bank else "n/a").rjust(_W["bank"])'
new_bank_s = '''        pnl_total = r.get("pnl_total", bank - 100.0)
        pnlt_s  = _fabs(pnl_total, _W["pnlt"])
        bank_s  = (f"${bank:.2f}" if bank == bank else "n/a").rjust(_W["bank"])'''
assert old_bank_s in content, 'bank_s not found'
content = content.replace(old_bank_s, new_bank_s, 1)

# 6. Add pnlt to colorize section and print
old_bank_c = '        bank_c  = _c(bank_s,  C.WHITE)'
new_bank_c = '        pnlt_c  = _vc(pnl_total, pnlt_s)\n        bank_c  = _c(bank_s,  C.WHITE)'
assert old_bank_c in content, 'bank_c not found'
content = content.replace(old_bank_c, new_bank_c, 1)

# 7. Add pnlt to print list
old_print = '        print("  " + _SEP.join([\n            idx_s, name_c,\n            bank_c, d24a_c, d48a_c,'
new_print = '        print("  " + _SEP.join([\n            idx_s, name_c,\n            pnlt_c, bank_c, d24a_c, d48a_c,'
assert old_print in content, 'print list not found'
content = content.replace(old_print, new_print, 1)

open('/root/crash-collector/bot_stats_all.py', 'w', encoding='utf-8').write(content)
print('All columns added: P&L $ total + fixed Days of play')
