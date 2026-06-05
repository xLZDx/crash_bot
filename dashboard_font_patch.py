"""
Patch: larger font + split table so everything fits without scrolling.
Main table (9 cols): Strategy / Flags / Bankroll / P&L All / %1h / %12h / %24h / WinRate / MaxDD
Detail table (5 cols): Strategy / Bets / Rounds / ConsecL / Session
"""

path = "/root/crash-collector/dashboard.py"
with open(path, encoding="utf-8") as f:
    src = f.read()

# ── Replace the whole bot-stats rendering block ───────────────────────────────

OLD_RENDER = '''        _df_bots = pd.DataFrame(_display_rows)

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

NEW_RENDER = '''        _df_bots = pd.DataFrame(_display_rows)

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

assert OLD_RENDER in src, "Anchor not found — check dashboard.py"
src = src.replace(OLD_RENDER, NEW_RENDER, 1)
print("Patch applied: split table + larger font")

with open(path, "w", encoding="utf-8") as f:
    f.write(src)
print("dashboard.py updated.")
