"""
Replace st.dataframe with st.markdown HTML table — full font control.
"""

path = "/root/crash-collector/dashboard.py"
with open(path, encoding="utf-8") as f:
    src = f.read()

OLD = '''        _df_bots = pd.DataFrame(_display_rows)

        # ── CSS: larger font for bot stats tables ─────────────────────────────
        st.markdown("""
<style>
.bot-stats-table table {
    font-size: 17px !important;
    line-height: 1.5 !important;
    width: 100% !important;
}
.bot-stats-table th {
    font-size: 15px !important;
    font-weight: 700 !important;
    padding: 6px 10px !important;
    white-space: nowrap !important;
}
.bot-stats-table td {
    padding: 5px 10px !important;
    white-space: nowrap !important;
}
</style>
""", unsafe_allow_html=True)

        def _color_val(v):
            try:
                n = float(str(v).replace("+","").replace("%",""))
                if n > 0:  return "color: #22c55e"
                if n < 0:  return "color: #ef4444"
            except Exception:
                pass
            return ""

        def _style_main(df):
            styles = pd.DataFrame("", index=df.index, columns=df.columns)
            for col in ["P&L All", "%1h", "%12h", "%24h", "%1w", "WinRate"]:
                if col in df.columns:
                    for i, v in enumerate(df[col]):
                        styles.loc[i, col] = _color_val(v)
            if "MaxDD" in df.columns:
                for i, v in enumerate(df["MaxDD"]):
                    try:
                        n = float(str(v).replace("%",""))
                        if n >= 5.0:   styles.loc[i, "MaxDD"] = "color:#ef4444;font-weight:bold"
                        elif n >= 1.0: styles.loc[i, "MaxDD"] = "color:#f59e0b"
                        else:          styles.loc[i, "MaxDD"] = "color:#6b7280"
                    except Exception:
                        pass
            return styles

        # ── Main table: key performance columns ───────────────────────────────
        _main_cols = ["Strategy", "Flags", "Bankroll", "P&L All",
                      "%1h", "%12h", "%24h", "%1w", "WinRate", "MaxDD"]
        _main_cols = [c for c in _main_cols if c in _df_bots.columns]
        _df_main = _df_bots[_main_cols].copy()

        st.markdown('<div class="bot-stats-table">', unsafe_allow_html=True)
        st.dataframe(
            _df_main.style.apply(_style_main, axis=None),
            use_container_width=True, hide_index=True,
            column_config={
                "Strategy":  st.column_config.TextColumn("Strategy",  width="medium"),
                "Flags":     st.column_config.TextColumn("Flags",     width="small"),
                "Bankroll":  st.column_config.TextColumn("Bankroll",  width="medium"),
                "P&L All":   st.column_config.TextColumn("P&L All",   width="small"),
                "%1h":       st.column_config.TextColumn("%1h",       width="small"),
                "%12h":      st.column_config.TextColumn("%12h",      width="small"),
                "%24h":      st.column_config.TextColumn("%24h",      width="small"),
                "%1w":       st.column_config.TextColumn("%1w",       width="small"),
                "WinRate":   st.column_config.TextColumn("WinRate",   width="small"),
                "MaxDD":     st.column_config.TextColumn("MaxDD",     width="small"),
            }
        )
        st.markdown("</div>", unsafe_allow_html=True)

        # ── Detail table: activity columns ────────────────────────────────────
        _det_cols = ["Strategy", "P&L 1h", "P&L 24h", "Bets", "Rounds", "ConsecL", "Session"]
        _det_cols = [c for c in _det_cols if c in _df_bots.columns]
        _df_det = _df_bots[_det_cols].copy()

        def _style_det(df):
            styles = pd.DataFrame("", index=df.index, columns=df.columns)
            for col in ["P&L 1h", "P&L 24h"]:
                if col in df.columns:
                    for i, v in enumerate(df[col]):
                        styles.loc[i, col] = _color_val(v)
            return styles

        with st.expander("Detail: Bets / Rounds / Session", expanded=False):
            st.markdown('<div class="bot-stats-table">', unsafe_allow_html=True)
            st.dataframe(
                _df_det.style.apply(_style_det, axis=None),
                use_container_width=True, hide_index=True,
                column_config={
                    "Strategy":  st.column_config.TextColumn("Strategy",  width="medium"),
                    "P&L 1h":    st.column_config.TextColumn("P&L 1h",    width="small"),
                    "P&L 24h":   st.column_config.TextColumn("P&L 24h",   width="small"),
                    "Bets":      st.column_config.TextColumn("Bets",      width="small"),
                    "Rounds":    st.column_config.TextColumn("Rounds",    width="small"),
                    "ConsecL":   st.column_config.TextColumn("CL",        width="small"),
                    "Session":   st.column_config.TextColumn("Session",   width="medium"),
                }
            )
            st.markdown("</div>", unsafe_allow_html=True)'''

NEW = '''        _df_bots = pd.DataFrame(_display_rows)

        # ── HTML table renderer (full font control, no iframe) ─────────────────
        def _pnl_color(v):
            try:
                n = float(str(v).replace("+","").replace("%","").replace("n/a","0"))
                if n > 0: return "#22c55e"
                if n < 0: return "#ef4444"
            except Exception:
                pass
            return "#e2e8f0"

        def _dd_color(v):
            try:
                n = float(str(v).replace("%",""))
                if n >= 5.0:  return "#ef4444"
                if n >= 1.0:  return "#f59e0b"
            except Exception:
                pass
            return "#6b7280"

        _MAIN = ["Strategy","Flags","Bankroll","P&L All","%1h","%12h","%24h","%1w","WinRate","MaxDD"]
        _DET  = ["Strategy","P&L 1h","P&L 24h","Bets","Rounds","CL","Session"]
        _PNL_COLS = {"P&L All","P&L 1h","P&L 24h","%1h","%12h","%24h","%1w"}

        # remap column names for detail table
        for r in _display_rows:
            if "error" not in r:
                r["CL"] = r.get("ConsecL","")

        _STYLE = """
        <style>
        .bst { width:100%; border-collapse:collapse; font-size:18px; font-family:monospace; }
        .bst th { background:#1e293b; color:#94a3b8; font-size:14px; font-weight:600;
                  padding:8px 12px; text-align:right; border-bottom:2px solid #334155; white-space:nowrap; }
        .bst th:first-child { text-align:left; }
        .bst td { padding:7px 12px; text-align:right; border-bottom:1px solid #1e293b;
                  font-size:16px; white-space:nowrap; }
        .bst td:first-child { text-align:left; font-weight:600; }
        .bst tr:hover td { background:#1e293b55; }
        </style>"""

        def _html_table(rows, cols):
            h = _STYLE + '<table class="bst"><thead><tr>'
            for c in cols:
                h += f"<th>{c}</th>"
            h += "</tr></thead><tbody>"
            for r in rows:
                if r.get("error"):
                    h += f'<tr><td>{r["strategy"]}</td><td colspan="{len(cols)-1}" style="color:#ef4444">{r["error"][:80]}</td></tr>'
                    continue
                h += "<tr>"
                for c in cols:
                    v = str(r.get(c, ""))
                    if c == "Strategy":
                        col = "#22c55e" if r.get("pnl_all",0) >= 0 else "#ef4444"
                        h += f'<td style="color:{col}">{v}</td>'
                    elif c == "MaxDD":
                        col = _dd_color(v)
                        bold = "font-weight:bold;" if col == "#ef4444" else ""
                        h += f'<td style="color:{col};{bold}">{v}</td>'
                    elif c in _PNL_COLS:
                        col = _pnl_color(v)
                        h += f'<td style="color:{col}">{v}</td>'
                    elif c == "Flags":
                        h += f'<td style="text-align:center">{v}</td>'
                    else:
                        h += f'<td style="color:#e2e8f0">{v}</td>'
                h += "</tr>"
            h += "</tbody></table>"
            return h

        # remap display rows to use simple dict keys
        _rows_main = []
        for r in _display_rows:
            if r.get("error"):
                _rows_main.append({"strategy": r["strategy"], "error": r.get("error","")})
                continue
            _rows_main.append({
                "Strategy": r["strategy"],
                "Flags":    r.get("Flags",""),
                "Bankroll": r.get("Bankroll",""),
                "P&L All":  r.get("P&L All",""),
                "%1h":      r.get("%1h",""),
                "%12h":     r.get("%12h",""),
                "%24h":     r.get("%24h",""),
                "%1w":      r.get("%1w",""),
                "WinRate":  r.get("WinRate",""),
                "MaxDD":    r.get("MaxDD",""),
                "pnl_all":  r.get("pnl_all", 0),
                "error":    None,
            })

        st.markdown(_html_table(_rows_main, _MAIN), unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        with st.expander("▶  Bets / P&L 1h / Rounds / Session", expanded=False):
            _rows_det = []
            for r in _display_rows:
                if r.get("error"): continue
                _rows_det.append({
                    "Strategy": r["strategy"],
                    "P&L 1h":   r.get("P&L 1h",""),
                    "P&L 24h":  r.get("P&L 24h",""),
                    "Bets":     r.get("Bets",""),
                    "Rounds":   r.get("Rounds",""),
                    "CL":       r.get("ConsecL",""),
                    "Session":  r.get("Session",""),
                    "pnl_all":  r.get("pnl_all",0),
                    "error":    None,
                })
            st.markdown(_html_table(_rows_det, _DET), unsafe_allow_html=True)'''

assert OLD in src, "Anchor not found"
src = src.replace(OLD, NEW, 1)
print("Patch applied: HTML table renderer")

with open(path, "w", encoding="utf-8") as f:
    f.write(src)
print("dashboard.py saved.")
