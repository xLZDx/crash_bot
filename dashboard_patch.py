"""
Patch dashboard.py:
1. load_bot_stats: add max_dd, d12_pct, d1w_pct
2. _display_rows: add MaxDD, %12h, %1w columns with color
3. Flag: MaxDD >= 5% -> red flag
"""
import re, sys

path = "/root/crash-collector/dashboard.py"
with open(path, encoding="utf-8") as f:
    src = f.read()

# ── PATCH 1: load_bot_stats — add max_dd + 12h + 1w snapshots ────────────────

OLD_SNAP = '''            d1_sol, d1_pct   = _snap_delta(3600)
            d24_sol, d24_pct = _snap_delta(86400)

            rows.append({
                "strategy":    strategy,
                "bank":        bank,
                "pnl_all":     total_pnl,
                "d1_sol":      d1_sol,
                "d1_pct":      d1_pct,
                "d24_sol":     d24_sol,
                "d24_pct":     d24_pct,
                "win_rate":    win_rate,
                "bets":        total_bets,
                "rounds":      total_rounds,
                "consec":      state["consec_losing_sessions"],
                "session_id":  state["session_id"],
                "s_rounds":    state["session_rounds"],
                "error":       None,
            })'''

NEW_SNAP = '''            d1_sol,  d1_pct  = _snap_delta(3600)
            d12_sol, d12_pct = _snap_delta(43200)
            d24_sol, d24_pct = _snap_delta(86400)
            d1w_sol, d1w_pct = _snap_delta(604800)

            # MaxDD from snapshots
            with _ddb.connect(db_path, read_only=True) as _dc:
                _snaps = _dc.execute(
                    "SELECT total_bank_sol FROM strategy_snapshots ORDER BY ts ASC"
                ).fetchall()
            if _snaps:
                _bks  = [s[0] for s in _snaps] + [bank]
                _peak = _bks[0]; _mxdd = 0.0
                for _b in _bks:
                    if _b > _peak: _peak = _b
                    _dd = (_peak - _b) / _peak if _peak > 0 else 0.0
                    if _dd > _mxdd: _mxdd = _dd
                max_dd_pct = _mxdd * 100
            else:
                max_dd_pct = 0.0

            rows.append({
                "strategy":    strategy,
                "bank":        bank,
                "pnl_all":     total_pnl,
                "d1_sol":      d1_sol,
                "d1_pct":      d1_pct,
                "d12_pct":     d12_pct,
                "d24_sol":     d24_sol,
                "d24_pct":     d24_pct,
                "d1w_pct":     d1w_pct,
                "max_dd":      max_dd_pct,
                "win_rate":    win_rate,
                "bets":        total_bets,
                "rounds":      total_rounds,
                "consec":      state["consec_losing_sessions"],
                "session_id":  state["session_id"],
                "s_rounds":    state["session_rounds"],
                "error":       None,
            })'''

assert OLD_SNAP in src, "PATCH 1 anchor not found"
src = src.replace(OLD_SNAP, NEW_SNAP, 1)
print("PATCH 1 applied: load_bot_stats extended")


# ── PATCH 2: _display_rows — add MaxDD + 12h + 1w + flag ─────────────────────

OLD_FLAGS = '''            _flags = []
            if r["win_rate"] == r["win_rate"] and r["win_rate"] < 30:
                _flags.append("🔴WR")
            if r["d24_sol"] == r["d24_sol"] and r["d24_pct"] == r["d24_pct"] and r["d24_pct"] < -0.10:
                _flags.append("🔴24h")
            if r["consec"] >= 3:
                _flags.append("⚠️CL")'''

NEW_FLAGS = '''            _flags = []
            if r["win_rate"] == r["win_rate"] and r["win_rate"] < 30:
                _flags.append("🔴WR")
            if r["d24_sol"] == r["d24_sol"] and r["d24_pct"] == r["d24_pct"] and r["d24_pct"] < -0.10:
                _flags.append("🔴24h")
            if r["consec"] >= 3:
                _flags.append("⚠️CL")
            if r.get("max_dd", 0) >= 5.0:
                _flags.append("🔴DD")'''

assert OLD_FLAGS in src, "PATCH 2a anchor not found"
src = src.replace(OLD_FLAGS, NEW_FLAGS, 1)
print("PATCH 2a applied: MaxDD flag added")


OLD_DISPLAY = '''            _display_rows.append({
                "Strategy": r["strategy"],
                "Flags": " ".join(_flags) if _flags else "✅",
                "Bankroll": f"{r['bank']:.6f}",
                "P&L All": _sfmt(r["pnl_all"]),
                "P&L 1h": _sfmt(r["d1_sol"]),
                "%1h": _pfmt(r["d1_pct"]),
                "P&L 24h": _sfmt(r["d24_sol"]),
                "%24h": _pfmt(r["d24_pct"]),
                "WinRate": f"{r['win_rate']:.1f}%" if r["win_rate"] == r["win_rate"] else "n/a",
                "Bets": str(r["bets"]),
                "Rounds": str(r["rounds"]),
                "ConsecL": str(r["consec"]),
                "Session": f"#{r['session_id']} {r['s_rounds']}r",
            })'''

NEW_DISPLAY = '''            _dd = r.get("max_dd", 0.0)
            _dd_str = f"{_dd:.1f}%" if _dd == _dd else "n/a"
            _display_rows.append({
                "Strategy": r["strategy"],
                "Flags": " ".join(_flags) if _flags else "✅",
                "Bankroll": f"{r['bank']:.6f}",
                "P&L All": _sfmt(r["pnl_all"]),
                "P&L 1h": _sfmt(r["d1_sol"]),
                "%1h": _pfmt(r["d1_pct"]),
                "%12h": _pfmt(r.get("d12_pct", float("nan"))),
                "P&L 24h": _sfmt(r["d24_sol"]),
                "%24h": _pfmt(r["d24_pct"]),
                "%1w": _pfmt(r.get("d1w_pct", float("nan"))),
                "MaxDD": _dd_str,
                "WinRate": f"{r['win_rate']:.1f}%" if r["win_rate"] == r["win_rate"] else "n/a",
                "Bets": str(r["bets"]),
                "Rounds": str(r["rounds"]),
                "ConsecL": str(r["consec"]),
                "Session": f"#{r['session_id']} {r['s_rounds']}r",
            })'''

assert OLD_DISPLAY in src, "PATCH 2b anchor not found"
src = src.replace(OLD_DISPLAY, NEW_DISPLAY, 1)
print("PATCH 2b applied: display rows extended with MaxDD, %12h, %1w")


# ── PATCH 3: error rows — add empty MaxDD, %12h, %1w ─────────────────────────

OLD_ERR = '''                _display_rows.append({
                    "Strategy": r["strategy"],
                    "Flags": "❌",
                    "Bankroll": "ERROR",
                    "P&L All": r["error"][:60],
                    "P&L 1h": "", "%1h": "",
                    "P&L 24h": "", "%24h": "",
                    "WinRate": "", "Bets": "", "Rounds": "",
                    "ConsecL": "", "Session": "",
                })'''

NEW_ERR = '''                _display_rows.append({
                    "Strategy": r["strategy"],
                    "Flags": "❌",
                    "Bankroll": "ERROR",
                    "P&L All": r["error"][:60],
                    "P&L 1h": "", "%1h": "", "%12h": "",
                    "P&L 24h": "", "%24h": "", "%1w": "",
                    "MaxDD": "",
                    "WinRate": "", "Bets": "", "Rounds": "",
                    "ConsecL": "", "Session": "",
                })'''

assert OLD_ERR in src, "PATCH 3 anchor not found"
src = src.replace(OLD_ERR, NEW_ERR, 1)
print("PATCH 3 applied: error rows updated")


# ── PATCH 4: st.dataframe → styled with MaxDD coloring ───────────────────────

OLD_DF = '''        _df_bots = pd.DataFrame(_display_rows)
        st.dataframe(_df_bots, use_container_width=True, hide_index=True)'''

NEW_DF = '''        _df_bots = pd.DataFrame(_display_rows)

        def _style_bots(df):
            styles = pd.DataFrame("", index=df.index, columns=df.columns)
            for col in ["P&L All", "P&L 1h", "P&L 24h"]:
                if col in df.columns:
                    for i, v in enumerate(df[col]):
                        try:
                            n = float(str(v).replace("+",""))
                            styles.loc[i, col] = "color: #22c55e" if n > 0 else ("color: #ef4444" if n < 0 else "")
                        except Exception:
                            pass
            for col in ["%1h", "%12h", "%24h", "%1w"]:
                if col in df.columns:
                    for i, v in enumerate(df[col]):
                        try:
                            n = float(str(v).replace("+","").replace("%",""))
                            styles.loc[i, col] = "color: #22c55e" if n > 0 else ("color: #ef4444" if n < 0 else "")
                        except Exception:
                            pass
            if "MaxDD" in df.columns:
                for i, v in enumerate(df["MaxDD"]):
                    try:
                        n = float(str(v).replace("%",""))
                        if n >= 5.0:
                            styles.loc[i, "MaxDD"] = "color: #ef4444; font-weight: bold"
                        elif n >= 1.0:
                            styles.loc[i, "MaxDD"] = "color: #f59e0b"
                        else:
                            styles.loc[i, "MaxDD"] = "color: #6b7280"
                    except Exception:
                        pass
            return styles

        st.dataframe(
            _df_bots.style.apply(_style_bots, axis=None),
            use_container_width=True, hide_index=True
        )'''

assert OLD_DF in src, "PATCH 4 anchor not found"
src = src.replace(OLD_DF, NEW_DF, 1)
print("PATCH 4 applied: styled dataframe with color coding")


with open(path, "w", encoding="utf-8") as f:
    f.write(src)

print("\nAll patches applied successfully.")
