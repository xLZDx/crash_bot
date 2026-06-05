content = open('/root/crash-collector/bot_stats_all.py', encoding='utf-8').read()

# Replace the entire normal row section
old_row_fmt = '''        # ── normal row ────────────────────────────────────────────────
        pnl = r.get("pnl", float("nan"))
        sc  = C.GREEN if pnl > 0 else (C.RED if pnl < 0 else C.WHITE)

        co  = r.get("cashout",  float("nan"))
        bp  = r.get("bank_pct", float("nan"))
        bank = r.get("bank",    float("nan"))
        d24  = r.get("d24_live", float("nan"))
        d1p  = r.get("d1_pct",  float("nan"))
        d24p = r.get("d24_pct", float("nan"))
        wr   = r.get("wr",      float("nan"))
        bets = r.get("bets",    0)
        mdd  = r.get("max_dd",  float("nan"))
        rule = r.get("rule",    "")

        # Plain-text fields (padded to column width BEFORE coloring)
        kind_s  = r["kind"].ljust(_W["kind"])
        name_s  = strategy[:_W["name"]].ljust(_W["name"])
        stat_s  = status.ljust(_W["stat"])
        co_s    = (f"{co:.1f}" if co == co else "?").rjust(_W["co"])
        bp_s    = (f"{bp:.0f}%" if bp == bp else "?").rjust(_W["bp"])

        if is_live:
            bank_s = f"${bank:.4f}".rjust(_W["bank"])
            pnl_s  = (f"+${pnl:.4f}" if pnl >= 0 else f"-${abs(pnl):.4f}").rjust(_W["pnl"])
        else:
            bank_s = f"{bank:.6f}".rjust(_W["bank"])
            pnl_s  = (("+" if pnl >= 0 else "") + f"{pnl:.6f}").rjust(_W["pnl"])

        if d24 == d24:
            d24_s = (f"+${d24:.4f}" if d24 >= 0 else f"-${abs(d24):.4f}").rjust(_W["d24"])
        else:
            d24_s = "n/a".rjust(_W["d24"])

        d1_s   = _fpct(d1p,  _W["d1"])
        d24p_s = _fpct(d24p, _W["d24p"])
        wr_s   = _fwr(wr,    _W["wr"])
        bets_s = str(bets).rjust(_W["bets"])

        if mdd == mdd:
            ddc   = C.RED if mdd >= 5.0 else (C.YELLOW if mdd >= 1.0 else C.GREY)
            mdd_s = f"{mdd:.2f}%".rjust(_W["mdd"])
        else:
            ddc   = None
            mdd_s = "n/a".rjust(_W["mdd"])

        # Colorize (wrap already-padded plain strings)
        kind_c  = _c(kind_s,  C.BOLD, C.YELLOW if is_live else C.GREY)
        name_c  = _c(name_s,  C.BOLD, sc)
        stat_c  = _c(stat_s,  C.GREEN)
        bank_c  = _c(bank_s,  C.WHITE)
        pnl_c   = _vc(pnl,   pnl_s)
        d24_c   = _vc(d24,   d24_s)
        d1_c    = _vc(d1p,   d1_s)
        d24p_c  = _vc(d24p,  d24p_s)
        mdd_c   = (_c(mdd_s, ddc) if ddc else mdd_s)

        print("  " + _SEP.join([
            kind_c, name_c, stat_c,
            co_s, bp_s,
            bank_c, pnl_c, d24_c,
            d1_c, d24p_c, wr_s, bets_s, mdd_c,
            rule,
        ]))'''

new_row_fmt = '''        # ── normal row (new USD structure) ───────────────────────────
        bank    = r.get("bank",      float("nan"))
        pnl     = r.get("pnl",       float("nan"))
        d24_abs = r.get("d24_abs",   float("nan"))
        d48_abs = r.get("d48_abs",   float("nan"))
        d24_live= r.get("d24_live",  float("nan"))
        d1p     = r.get("d1_pct",    float("nan"))
        d12p    = r.get("d12_pct",   float("nan"))
        d24p    = r.get("d24_pct",   float("nan"))
        wr      = r.get("wr",        float("nan"))
        bets    = r.get("bets",      0)
        mdd     = r.get("max_dd",    float("nan"))
        days    = r.get("days_play", float("nan"))
        sc      = C.GREEN if pnl > 0 else (C.RED if pnl < 0 else C.WHITE)

        def _fabs(v, w):
            if v != v: return "n/a".rjust(w)
            s = (f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}")
            return s.rjust(w)

        # Row number
        idx     = rows.index(r) + 1
        idx_s   = str(idx).rjust(4)
        name_s  = strategy[:_W["name"]].ljust(_W["name"])

        bank_s  = (f"${bank:.2f}" if bank == bank else "n/a").rjust(_W["bank"])
        d24a_s  = _fabs(d24_abs, _W["d24a"])
        d48a_s  = _fabs(d48_abs, _W["d48a"])
        d1_s    = _fpct(d1p,   _W["d1"])
        d12p_s  = _fpct(d12p,  _W["d12p"])
        d24p_s  = _fpct(d24p,  _W["d24p"])

        if d24_live == d24_live:
            d24l_s = _fabs(d24_live, _W["d24l"])
        else:
            d24l_s = "n/a".rjust(_W["d24l"])

        wr_s    = _fwr(wr,    _W["wr"])
        bets_s  = str(bets).rjust(_W["bets"])

        if days == days and days > 0:
            days_s = f"{days:.1f}d".rjust(_W["days"])
        else:
            days_s = "n/a".rjust(_W["days"])

        if mdd == mdd:
            ddc   = C.RED if mdd >= 5.0 else (C.YELLOW if mdd >= 1.0 else C.GREY)
            mdd_s = f"{mdd:.2f}%".rjust(_W["mdd"])
        else:
            ddc = None
            mdd_s = "n/a".rjust(_W["mdd"])

        name_c  = _c(name_s,  C.BOLD, C.YELLOW if is_live else sc)
        bank_c  = _c(bank_s,  C.WHITE)
        d24a_c  = _vc(d24_abs,  d24a_s)
        d48a_c  = _vc(d48_abs,  d48a_s)
        d1_c    = _vc(d1p,      d1_s)
        d12p_c  = _vc(d12p,     d12p_s)
        d24p_c  = _vc(d24p,     d24p_s)
        d24l_c  = _vc(d24_live, d24l_s)
        mdd_c   = (_c(mdd_s, ddc) if ddc else mdd_s)

        print("  " + _SEP.join([
            idx_s, name_c,
            bank_c, d24a_c, d48a_c,
            d1_c, d12p_c, d24p_c, d24l_c,
            wr_s, bets_s, days_s, mdd_c,
            "",
        ]))'''

assert old_row_fmt in content, 'row format not found'
content = content.replace(old_row_fmt, new_row_fmt, 1)
open('/root/crash-collector/bot_stats_all.py', 'w', encoding='utf-8').write(content)
print('Row format updated to new structure')
