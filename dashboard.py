"""
Streamlit dashboard — read-only view of crash.db + ML predictions.
Run:  streamlit run dashboard.py
"""
import json
import os
import sys
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DB_PATH
from governance import governance_payload
from price_feed import get_sol_usd, PriceFeedStatus
from simulator import START_SOL, VirtualAccount
from strategy_optimizer import run_optimizer

PREDICTION_FILE = Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "prediction.json"
ALLOW_DASHBOARD_MUTATIONS = os.environ.get("CRASH_DASHBOARD_MUTATIONS") == "1"
_DATA_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "data"

st.set_page_config(
    page_title="Crash Analytics",
    page_icon="chart_with_upwards_trend",
    layout="wide",
    initial_sidebar_state="expanded",
)

_STREAK_SQL = """
WITH numbered AS (
    SELECT id, multiplier,
           SUM(CASE WHEN multiplier >= 2.0 THEN 1 ELSE 0 END)
               OVER (ORDER BY id ROWS UNBOUNDED PRECEDING) AS grp
    FROM rounds
),
streaks AS (
    SELECT grp, COUNT(*) AS len
    FROM numbered
    WHERE multiplier < 2.0
    GROUP BY grp
)
SELECT COALESCE(MAX(len), 0) FROM streaks
"""

_EMPTY_COLS = [
    "id", "game_round_id", "multiplier", "ts", "source",
    "total_bets", "num_bettors", "frame_event", "hash",
    "date", "hour", "weekday", "category",
]


# ── Data loading ──────────────────────────────────────────────────────────────

def _ro_connect(db_path: str):
    import time
    delay = 0.1
    for attempt in range(8):
        try:
            return duckdb.connect(db_path, read_only=True)
        except duckdb.IOException:
            if attempt == 7:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


@st.cache_data(ttl=20)
def load_data(db_path: str) -> pd.DataFrame:
    if not Path(db_path).exists():
        return pd.DataFrame(columns=_EMPTY_COLS)
    conn = None
    try:
        conn = _ro_connect(db_path)
        existing = {r[1] for r in conn.execute("PRAGMA table_info('rounds')").fetchall()}
        hash_col = "hash" if "hash" in existing else "NULL AS hash"
        df = conn.execute(f"""
            SELECT id, game_round_id, ROUND(multiplier,4) AS multiplier,
                   ts::TIMESTAMPTZ AS ts, source, total_bets, num_bettors,
                   frame_event, {hash_col}
            FROM rounds ORDER BY id DESC
        """).df()
    except duckdb.IOException:
        return pd.DataFrame(columns=_EMPTY_COLS)
    finally:
        if conn is not None:
            conn.close()

    if df.empty:
        return pd.DataFrame(columns=_EMPTY_COLS)

    df["ts"]      = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None)
    df["date"]    = df["ts"].dt.date
    df["hour"]    = df["ts"].dt.hour
    df["weekday"] = df["ts"].dt.day_name()

    bins   = [0, 1.01, 1.1, 1.5, 2, 3, 5, 10, 25, 100, float("inf")]
    labels = ["<1.01","1.01-1.1","1.1-1.5","1.5-2","2-3","3-5","5-10","10-25","25-100",">100"]
    df["category"] = pd.cut(df["multiplier"], bins=bins, labels=labels, right=False)
    return df


@st.cache_data(ttl=20)
def load_bets(db_path: str) -> pd.DataFrame:
    """Load per-round bet table for bet analytics."""
    if not Path(db_path).exists():
        return pd.DataFrame()
    conn = None
    try:
        conn = _ro_connect(db_path)
        df = conn.execute("""
            SELECT round_id, currency, amount, username,
                   ts::TIMESTAMPTZ AS ts
            FROM bets ORDER BY ts DESC LIMIT 50000
        """).df()
    except Exception:
        return pd.DataFrame()
    finally:
        if conn is not None:
            conn.close()
    return df


@st.cache_data(ttl=20)
def load_streak(db_path: str) -> int:
    if not Path(db_path).exists():
        return 0
    conn = None
    try:
        conn = _ro_connect(db_path)
        return conn.execute(_STREAK_SQL).fetchone()[0]
    except duckdb.IOException:
        return 0
    finally:
        if conn is not None:
            conn.close()


@st.cache_data(ttl=20)
def load_staleness(db_path: str) -> str:
    if not Path(db_path).exists():
        return "no data"
    conn = None
    try:
        conn = _ro_connect(db_path)
        row = conn.execute("SELECT MAX(ts) FROM rounds").fetchone()
    except duckdb.IOException:
        return "collector active"
    finally:
        if conn is not None:
            conn.close()
    if not row or row[0] is None:
        return "no data"
    last = pd.to_datetime(row[0], utc=True)
    delta = pd.Timestamp.now("UTC") - last
    mins = int(delta.total_seconds() / 60)
    if mins < 2:
        return "fresh"
    return f"last round {mins} min ago"


def load_prediction() -> dict:
    if not PREDICTION_FILE.exists():
        return {}
    try:
        return json.loads(PREDICTION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


@st.cache_data(ttl=30)
def load_bot_stats(data_dir: str) -> list[dict]:
    """Load stats for all bot_state_*.duckdb files. Returns list of row dicts."""
    import glob as _glob
    import duckdb as _ddb

    now = pd.Timestamp.now("UTC")
    pattern = str(Path(data_dir) / "bot_state_*.duckdb")
    db_files = sorted(_glob.glob(pattern))
    rows = []
    for db_path in db_files:
        strategy = Path(db_path).stem.replace("bot_state_", "")
        try:
            from bot_state import BotAccount
            account = BotAccount(db_path)
            state   = account.get_state()
            totals  = account.get_totals()

            bank      = state["total_bank_sol"]
            total_pnl = totals["total_pnl_sol"]
            total_bets = totals["total_bets"]
            total_wins = totals["total_wins"]
            win_rate  = (total_wins / total_bets * 100) if total_bets > 0 else float("nan")

            with _ddb.connect(db_path, read_only=True) as _c:
                _last  = state["last_processed_round_id"]
                _first = _c.execute(
                    "SELECT COALESCE(MIN(round_db_id), 0) FROM strategy_bets"
                ).fetchone()[0]
            total_rounds = (_last - _first + 1) if _first > 0 else 0

            def _snap_delta(secs):
                snap = account.get_snapshot_near(
                    pd.Timestamp.fromtimestamp(now.timestamp() - secs, tz="UTC")
                )
                if snap is None:
                    return float("nan"), float("nan")
                d_bank = bank - snap["total_bank_sol"]
                base   = snap["total_bank_sol"]
                return d_bank, (d_bank / base) if base > 0 else 0.0

            d1_sol,  d1_pct  = _snap_delta(3600)
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
            })
        except Exception as e:
            rows.append({"strategy": strategy, "error": str(e)})
    return rows


@st.cache_data(ttl=60)
def cached_sol_price() -> tuple:
    """Returns (price_usd, PriceFeedStatus, source_name). Cached 60s."""
    return get_sol_usd()


@st.cache_data(ttl=10)
def load_virtual_data(db_path: str) -> dict:
    """Load virtual account state and bet history."""
    try:
        acc = VirtualAccount(db_path)
        state   = acc.get_state()
        history = acc.get_history(limit=200)
        curve   = acc.get_bankroll_curve()
        invariants = acc.check_invariants()
        return {
            "state": state,
            "history": history,
            "curve": curve,
            "invariants": invariants,
            "error": None,
        }
    except Exception as e:
        return {
            "state": {},
            "history": [],
            "curve": [],
            "invariants": {"ok": False, "problems": ["load_virtual_data_failed"]},
            "error": str(e),
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _backtest(mults, cashout: float, bet_frac: float = 0.01, start: float = 1000.0):
    """Flat-bet strategy: bet bet_frac of current bankroll, cash out at cashout."""
    bankroll = start
    curve = [bankroll]
    wins = losses = 0
    peak = start
    max_dd = 0.0
    for m in mults:
        if bankroll <= 0:
            break
        bet = bankroll * bet_frac
        if m >= cashout:
            bankroll += bet * (cashout - 1)
            wins += 1
        else:
            bankroll -= bet
            losses += 1
        if bankroll > peak:
            peak = bankroll
        dd = (peak - bankroll) / peak
        if dd > max_dd:
            max_dd = dd
        curve.append(bankroll)
    total = wins + losses
    return {
        "curve": curve,
        "final": bankroll,
        "roi_pct": (bankroll - start) / start * 100,
        "win_rate": wins / total if total else 0,
        "ev_per_bet": ((cashout - 1) * (wins / total) - (losses / total)) if total else 0,
        "max_drawdown_pct": max_dd * 100,
        "wins": wins,
        "losses": losses,
    }


def _backtest_martingale(mults, cashout: float, base_bet_frac: float = 0.01,
                         start: float = 1000.0):
    """Martingale: double bet after loss, reset to base % of bankroll after win."""
    bankroll  = start
    curve     = [bankroll]
    wins = losses = 0
    peak     = start
    max_dd   = 0.0
    last_bet = bankroll * base_bet_frac
    for m in mults:
        if bankroll <= 0:
            break
        bet = min(last_bet, bankroll)
        if m >= cashout:
            bankroll += bet * (cashout - 1)
            wins += 1
            last_bet = bankroll * base_bet_frac   # reset to base on new bankroll
        else:
            bankroll -= bet
            losses += 1
            last_bet = bet * 2.0                  # double on loss
        if bankroll > peak:
            peak = bankroll
        dd = (peak - bankroll) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
        curve.append(bankroll)
    total = wins + losses
    return {
        "curve":            curve,
        "final":            bankroll,
        "roi_pct":          (bankroll - start) / start * 100,
        "win_rate":         wins / total if total else 0.0,
        "ev_per_bet":       ((cashout - 1) * (wins / total) - (losses / total)) if total else 0.0,
        "max_drawdown_pct": max_dd * 100,
        "wins":             wins,
        "losses":           losses,
    }


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("Crash Analytics")
    st.markdown("---")

    # ── Capital + Strategy — single source of truth for ALL sections ─────────
    st.markdown("**Bankroll (total account balance)**")
    st.caption("Bankroll = весь твой счёт. Ставка = % от банкролла за раунд.")
    capital_usd = st.number_input(
        "Amount ($)",
        min_value=1.0,
        max_value=1_000_000.0,
        value=100.0,
        step=10.0,
        format="%.2f",
    )
    _sol_p, _sol_status, _sol_src = cached_sol_price()
    capital_sol = capital_usd / _sol_p
    _price_label = f"= {capital_sol:.4f} SOL  (1 SOL = ${_sol_p:.2f} via {_sol_src})"
    if _sol_status in (PriceFeedStatus.STALE_CACHE, PriceFeedStatus.UNAVAILABLE):
        st.caption(_price_label + f"  ⚠️ price {_sol_status.value} -- do not size real positions")
    else:
        st.caption(_price_label)

    st.markdown("**Strategy (applies to ALL charts)**")
    global_cashout = st.select_slider(
        "Cashout target",
        options=[1.2, 1.5, 2.0, 3.0, 5.0, 10.0],
        value=2.0,
        format_func=lambda x: f"{x}x",
        help="Автоматически вывести деньги когда краш достигнет этого множителя.",
    )
    global_stake_pct = st.slider(
        "Stake per round (% of bankroll)",
        min_value=1, max_value=10, value=1, step=1,
        help="Manual simulator sizing as a percent of current bankroll.",
    )
    global_bet_frac = global_stake_pct / 100.0
    _stake_usd = capital_usd * global_bet_frac
    _stake_sol = _stake_usd / _sol_p
    st.caption(
        f"Ставка за раунд: ${_stake_usd:.4f}  =  {_stake_sol:.6f} SOL"
    )
    st.markdown("---")

    db_path   = st.text_input("DB Path", DB_PATH)
    df_all    = load_data(db_path)
    staleness = load_staleness(db_path)

    if df_all.empty or "date" not in df_all.columns or df_all["date"].isna().all():
        st.warning("No data yet. Start collection:\n```\npython main.py collect\n```")
        st.stop()

    st.success(f"{len(df_all):,} rounds in DB")
    st.caption(staleness)
    st.markdown("---")

    min_date = df_all["date"].dropna().min()
    max_date = df_all["date"].dropna().max()
    date_range = st.date_input("Period", value=(min_date, max_date),
                               min_value=min_date, max_value=max_date)
    if not date_range:
        st.warning("Select at least one date.")
        st.stop()

    max_m_cap = min(float(df_all["multiplier"].max()), 200.0)
    mult_range = st.slider("Multiplier range", 1.0, max_m_cap, (1.0, max_m_cap), step=0.1)

    sources = df_all["source"].dropna().unique().tolist()
    src_filter = st.multiselect("Source", sources, default=sources)

    st.markdown("---")
    if st.button("Refresh"):
        st.cache_data.clear()
        st.rerun()
    st.caption("Auto-refresh every 20 s")


# ── Filters ───────────────────────────────────────────────────────────────────

d_from = date_range[0]
d_to   = date_range[1] if len(date_range) == 2 else date_range[0]

df = df_all[
    (df_all["date"] >= d_from) &
    (df_all["date"] <= d_to) &
    (df_all["multiplier"] >= mult_range[0]) &
    (df_all["multiplier"] <= mult_range[1]) &
    (df_all["source"].isin(src_filter))
].copy()

if df.empty:
    st.warning("No data matches current filters.")
    st.stop()

n = len(df)


# ── Bot stats card ────────────────────────────────────────────────────────────

with st.container():
    st.markdown("### Bot stats")
    _bot_rows = load_bot_stats(str(_DATA_DIR))
    if not _bot_rows:
        st.info("No bot_state_*.duckdb files found in data/")
    else:
        _display_rows = []
        for r in _bot_rows:
            if r.get("error"):
                _display_rows.append({
                    "Strategy": r["strategy"],
                    "Flags": "❌",
                    "Bankroll": "ERROR",
                    "P&L All": r["error"][:60],
                    "P&L 1h": "", "%1h": "", "%12h": "",
                    "P&L 24h": "", "%24h": "", "%1w": "",
                    "MaxDD": "",
                    "WinRate": "", "Bets": "", "Rounds": "",
                    "ConsecL": "", "Session": "",
                })
                continue

            _flags = []
            if r["win_rate"] == r["win_rate"] and r["win_rate"] < 30:
                _flags.append("🔴WR")
            if r["d24_sol"] == r["d24_sol"] and r["d24_pct"] == r["d24_pct"] and r["d24_pct"] < -0.10:
                _flags.append("🔴24h")
            if r["consec"] >= 3:
                _flags.append("⚠️CL")
            if r.get("max_dd", 0) >= 5.0:
                _flags.append("🔴DD")

            def _sfmt(v, decimals=4):
                if v != v:
                    return "n/a"
                sign = "+" if v >= 0 else ""
                return f"{sign}{v:.{decimals}f}"

            def _pfmt(v):
                if v != v:
                    return "n/a"
                sign = "+" if v >= 0 else ""
                return f"{sign}{v*100:.2f}%"

            _dd = r.get("max_dd", 0.0)
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
            })

        _df_bots = pd.DataFrame(_display_rows)

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

        _MAIN = ["Strategy","Flags","Bankroll","P&L All","%1h","%12h","%24h","%1w","WinRate","Bets","MaxDD"]
        _DET  = ["Strategy","P&L 1h","P&L 24h","Bets","Rounds","CL","Session"]
        _PNL_COLS = {"P&L All","P&L 1h","P&L 24h","%1h","%12h","%24h","%1w"}

        # remap column names for detail table
        for r in _display_rows:
            if "error" not in r:
                r["CL"] = r.get("ConsecL","")

        _STYLE = """
        <style>
        .bst { width:100%; border-collapse:collapse; font-family:'Segoe UI',monospace; }
        .bst th { background:#1e293b; color:#94a3b8; font-size:15px; font-weight:700;
                  padding:9px 14px; text-align:right; border-bottom:2px solid #334155;
                  white-space:nowrap; letter-spacing:0.3px; }
        .bst th:first-child { text-align:left; }
        .bst td { padding:8px 14px; text-align:right; border-bottom:1px solid #1e293b;
                  font-size:17px; font-weight:700; white-space:nowrap; }
        .bst td:first-child { text-align:left; font-size:17px; font-weight:700; }
        .bst tr:hover td { background:#1e293b66; }
        </style>"""

        def _html_table(rows, cols):
            h = _STYLE + '<table class="bst"><thead><tr>'
            for c in cols:
                h += f"<th>{c}</th>"
            h += "</tr></thead><tbody>"
            for r in rows:
                if r.get("error"):
                    h += f'<tr><td>{r.get("Strategy", r.get("strategy","?"))}</td><td colspan="{len(cols)-1}" style="color:#ef4444">{r["error"][:80]}</td></tr>'
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
                _rows_main.append({"Strategy": r["Strategy"], "error": r.get("error","")})
                continue
            _rows_main.append({
                "Strategy": r["Strategy"],
                "Flags":    r.get("Flags",""),
                "Bankroll": r.get("Bankroll",""),
                "P&L All":  r.get("P&L All",""),
                "%1h":      r.get("%1h",""),
                "%12h":     r.get("%12h",""),
                "%24h":     r.get("%24h",""),
                "%1w":      r.get("%1w",""),
                "WinRate":  r.get("WinRate",""),
                "MaxDD":    r.get("MaxDD",""),
                "pnl_all":  (lambda v: (float(v.replace("+","")) if v not in ("","n/a","ERROR") else 0))(r.get("P&L All","0")),
                "error":    None,
            })

        st.markdown(_html_table(_rows_main, _MAIN), unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        with st.expander("▶  Bets / P&L 1h / Rounds / Session", expanded=False):
            _rows_det = []
            for r in _display_rows:
                if r.get("error"): continue
                _rows_det.append({
                    "Strategy": r["Strategy"],
                    "P&L 1h":   r.get("P&L 1h",""),
                    "P&L 24h":  r.get("P&L 24h",""),
                    "Bets":     r.get("Bets",""),
                    "Rounds":   r.get("Rounds",""),
                    "CL":       r.get("ConsecL",""),
                    "Session":  r.get("Session",""),
                    "pnl_all":  r.get("pnl_all",0),
                    "error":    None,
                })
            st.markdown(_html_table(_rows_det, _DET), unsafe_allow_html=True)

        _valid = [r for r in _bot_rows if not r.get("error")]
        if _valid:
            _best  = max(_valid, key=lambda r: r["pnl_all"])
            _worst = min(_valid, key=lambda r: r["pnl_all"])
            _bc1, _bc2 = st.columns(2)
            _bc1.metric("Best all-time", _best["strategy"],
                        f"{_best['pnl_all']:+.4f} SOL")
            _bc2.metric("Worst all-time", _worst["strategy"],
                        f"{_worst['pnl_all']:+.4f} SOL")

st.divider()

# ── KPI row ───────────────────────────────────────────────────────────────────

st.markdown("### Key metrics")
c1, c2, c3, c4, c5, c6 = st.columns(6)
today_n    = len(df_all[df_all["date"] == date.today()])
under101   = (df_all["multiplier"] < 1.01).sum()
under110   = (df_all["multiplier"] < 1.1).sum()
over10     = (df_all["multiplier"] >= 10).sum()
max_streak = load_streak(db_path)
total_all  = len(df_all)
has_bets   = df_all["total_bets"].notna().any()
total_bets_usdt = df_all["total_bets"].sum() if has_bets else 0

c1.metric("Total rounds",         f"{total_all:,}")
c2.metric("Today",                f"{today_n:,}")
c3.metric("< 1.01x (inst.bust)",  f"{under101:,}", f"{under101/total_all*100:.1f}%")
c4.metric("< 1.1x",               f"{under110:,}", f"{under110/total_all*100:.1f}%")
c5.metric(">= 10x",               f"{over10:,}", f"{over10/total_all*100:.1f}%")
c6.metric("Max streak < 2x",      str(max_streak))

st.markdown("---")


# ── 24h Projection ────────────────────────────────────────────────────────────

# ── 24h Projection uses the SAME global_cashout + global_bet_frac as everything else ──
# "Typical day" = median rounds per calendar day in the selected period.
# Each day simulates a FRESH start from capital_usd (not continuous).

_by_day_cnt   = df.groupby("date").size()
_typical_day  = int(_by_day_cnt.median()) if len(_by_day_cnt) else 100
_mults_seq_all = df.sort_values("id")["multiplier"].tolist()
# Use last _typical_day rounds as representative sample
_day_sample   = _mults_seq_all[-_typical_day:] if len(_mults_seq_all) >= _typical_day else _mults_seq_all

# Per-day P&L: each day = independent fresh-start simulation
_day_results  = []
_day_labels   = []
for _d in sorted(df["date"].dropna().unique()):
    _dm = df[df["date"] == _d]["multiplier"].tolist()
    if _dm:
        _dr = _backtest(_dm, global_cashout, global_bet_frac, capital_usd)
        _day_results.append(_dr["final"] - capital_usd)
        _day_labels.append(str(_d))

_avg_day_pnl     = sum(_day_results) / len(_day_results) if _day_results else 0
_pos_days         = sum(1 for x in _day_results if x > 0)
_total_days_hist  = len(_day_results)
_day_pnl_min     = min(_day_results) if _day_results else 0
_day_pnl_max     = max(_day_results) if _day_results else 0

# Typical-day single simulation
_typical_r = _backtest(_day_sample, global_cashout, global_bet_frac, capital_usd)
_typical_pnl = _typical_r["final"] - capital_usd

st.markdown(
    f"### 24h Projection — bankroll ${capital_usd:,.2f}  |  "
    f"cashout {global_cashout}x  |  stake {global_stake_pct}%  |  "
    f"stake per round = ${capital_usd * global_bet_frac:.4f}"
)
st.caption(
    f"Каждый день считается НЕЗАВИСИМО — каждый день начинаешь заново с ${capital_usd:,.2f}.  "
    f"Типичный день = {_typical_day} раундов (медиана по выбранному периоду)."
)

p1, p2, p3, p4, p5 = st.columns(5)
p1.metric("Типичный день (раундов)", f"{_typical_day:,}")
p2.metric(
    "Типичный день P&L",
    f"${_typical_pnl:+.2f}",
    f"{'WIN' if _typical_pnl > 0 else 'LOSS'}",
)
p3.metric(
    "Прибыльных дней",
    f"{_pos_days}/{_total_days_hist}",
    f"{_pos_days/_total_days_hist*100:.0f}%" if _total_days_hist else "—",
)
p4.metric("Лучший день", f"${_day_pnl_max:+.2f}")
p5.metric("Худший день", f"${_day_pnl_min:+.2f}")

if _day_results:
    _fig_24h = go.Figure(go.Bar(
        x=_day_labels,
        y=_day_results,
        marker_color=["#2ecc71" if v > 0 else "#e74c3c" for v in _day_results],
        hovertemplate=(
            "<b>%{x}</b><br>"
            f"Банкролл: ${capital_usd:,.2f} (старт)<br>"
            "P&L: $%{y:+.2f}<extra></extra>"
        ),
    ))
    _fig_24h.add_hline(y=0, line_color="white", opacity=0.5,
                       annotation_text="0 (breakeven)")
    _fig_24h.add_hline(
        y=_avg_day_pnl, line_dash="dash", line_color="#f39c12",
        annotation_text=f"avg ${_avg_day_pnl:+.2f}/day",
    )
    _fig_24h.update_layout(
        height=240, margin=dict(l=0, r=0, t=10, b=0),
        yaxis_title="P&L за день ($)", xaxis_title="",
        title=(
            f"P&L за день  |  банкролл ${capital_usd:,.0f}  |  "
            f"{global_cashout}x / {global_stake_pct}% ставка  |  "
            f"каждый день — свежий старт"
        ),
        title_font_size=12,
    )
    st.plotly_chart(_fig_24h, use_container_width=True)

# Comparison: same stake, all cashout options — shows WHY cashout matters
st.markdown(f"**Сравнение каш-аутов при ставке {global_stake_pct}% — типичный день ({_typical_day} раундов)**")
_comp_rows = []
for _co in [1.2, 1.5, 2.0, 3.0, 5.0, 10.0]:
    _r = _backtest(_day_sample, _co, global_bet_frac, capital_usd)
    _pnl = _r["final"] - capital_usd
    _comp_rows.append({
        "Каш-аут":         f"{_co}x",
        "Банкролл (старт)":f"${capital_usd:,.2f}",
        "Ставка/раунд":    f"${capital_usd * global_bet_frac:.4f}",
        "Win rate":         f"{_r['win_rate']*100:.1f}%",
        "P&L за день":     f"${_pnl:+.2f}",
        "Банкролл (конец)":f"${_r['final']:.2f}",
        "Макс. просадка":  f"{_r['max_drawdown_pct']:.1f}%",
        "Выбран":           "★" if abs(_co - global_cashout) < 0.01 else "",
    })

_comp_df = pd.DataFrame(_comp_rows)

def _comp_color(row):
    if row.get("Выбран") == "★":
        return ["background-color: rgba(52,152,219,0.25)"] * len(row)
    try:
        v = float(row["P&L за день"].replace("$", "").replace("+", ""))
        if v > 0:
            return ["background-color: rgba(46,204,113,0.10)"] * len(row)
    except Exception:
        pass
    return [""] * len(row)

st.dataframe(_comp_df.style.apply(_comp_color, axis=1),
             use_container_width=True, hide_index=True)

st.markdown("---")


# ── Strategy Agenda ───────────────────────────────────────────────────────────

with st.expander("Strategy Agenda — какая стратегия выгоднее?", expanded=True):
    mults_seq = df.sort_values("id")["multiplier"].tolist()

    STRATEGIES = [
        {"label": "1.2x (очень консервативная)", "cashout": 1.2,  "color": "#2ecc71"},
        {"label": "1.5x (консервативная)",        "cashout": 1.5,  "color": "#27ae60"},
        {"label": "2x  (стандарт)",               "cashout": 2.0,  "color": "#3498db"},
        {"label": "3x  (умеренная)",               "cashout": 3.0,  "color": "#f39c12"},
        {"label": "5x  (агрессивная)",             "cashout": 5.0,  "color": "#e67e22"},
        {"label": "10x (очень агрессивная)",       "cashout": 10.0, "color": "#e74c3c"},
    ]

    results = {s["label"]: _backtest(mults_seq, s["cashout"], bet_frac=global_bet_frac, start=capital_usd) for s in STRATEGIES}

    # ── Comparison table ──────────────────────────────────────────────────────
    st.markdown(
        f"#### Сравнение стратегий  |  капитал: **${capital_usd:,.2f}**  |  ставка {global_stake_pct}% банкролла"
    )
    tbl_rows = []
    for s in STRATEGIES:
        r = results[s["label"]]
        n_rounds = len(mults_seq)
        theo_win = (1.0 - 0.01) / s["cashout"]
        emp_win  = r["win_rate"]
        pnl      = r["final"] - capital_usd
        tbl_rows.append({
            "Стратегия":         s["label"],
            "Каш-аут":           f"{s['cashout']}x",
            "Теор. % побед":     f"{theo_win*100:.1f}%",
            "Эмп. % побед":      f"{emp_win*100:.1f}%",
            "EV на ставку ($)":  f"${r['ev_per_bet'] * capital_usd * global_bet_frac:+.4f}",
            "P&L на "+f"{n_rounds:,}"+" раундов": f"${pnl:+.2f}",
            "ROI":               f"{r['roi_pct']:+.1f}%",
            "Макс. просадка":    f"{r['max_drawdown_pct']:.1f}%",
            "Финал. банкролл":   f"${r['final']:.2f}",
        })

    tbl_df = pd.DataFrame(tbl_rows)
    best_ev_idx   = max(range(len(STRATEGIES)), key=lambda i: results[STRATEGIES[i]["label"]]["ev_per_bet"])
    best_roi_idx  = max(range(len(STRATEGIES)), key=lambda i: results[STRATEGIES[i]["label"]]["roi_pct"])
    lowest_dd_idx = min(range(len(STRATEGIES)), key=lambda i: results[STRATEGIES[i]["label"]]["max_drawdown_pct"])

    def _highlight(row):
        idx = tbl_df.index[tbl_df["Стратегия"] == row["Стратегия"]].tolist()
        if not idx:
            return [""] * len(row)
        i = idx[0]
        if i == best_ev_idx:
            return ["background-color: rgba(52,152,219,0.25)"] * len(row)
        return [""] * len(row)

    st.dataframe(tbl_df.style.apply(_highlight, axis=1),
                 use_container_width=True, hide_index=True)

    # ── Bankroll curves ───────────────────────────────────────────────────────
    st.markdown(f"#### Кривые банкролла  |  старт ${capital_usd:,.2f}  |  ставка {global_stake_pct}%")
    fig_curves = go.Figure()
    for s in STRATEGIES:
        curve = results[s["label"]]["curve"]
        x = list(range(len(curve)))
        fig_curves.add_trace(go.Scatter(
            x=x, y=curve,
            mode="lines",
            name=s["label"],
            line=dict(color=s["color"], width=1.5),
        ))
    fig_curves.add_hline(y=capital_usd, line_dash="dot", line_color="white",
                         opacity=0.3, annotation_text=f"старт ${capital_usd:,.0f}")
    fig_curves.update_layout(
        height=380, margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", y=-0.15),
        xaxis_title="Раунды", yaxis_title="Банкролл ($)",
    )
    st.plotly_chart(fig_curves, use_container_width=True)

    # ── EV bar ────────────────────────────────────────────────────────────────
    col_ev, col_dd = st.columns(2)
    with col_ev:
        st.markdown(f"**EV на одну ставку ({global_stake_pct}% от ${capital_usd:,.0f})**")
        _stake = capital_usd * global_bet_frac
        ev_df = pd.DataFrame([
            {"Стратегия": s["label"].split("(")[0].strip(),
             "EV ($)": results[s["label"]]["ev_per_bet"] * _stake}
            for s in STRATEGIES
        ])
        fig_ev = go.Figure(go.Bar(
            x=ev_df["EV ($)"], y=ev_df["Стратегия"],
            orientation="h",
            marker_color=[s["color"] for s in STRATEGIES],
            hovertemplate="%{y}<br>EV: $%{x:+.4f}<extra></extra>",
        ))
        fig_ev.add_vline(x=0, line_color="white", opacity=0.5)
        fig_ev.update_layout(height=260, margin=dict(l=0, r=0, t=0, b=0),
                              xaxis_title="EV per bet ($)", yaxis_title="")
        st.plotly_chart(fig_ev, use_container_width=True)

    with col_dd:
        st.markdown("**Максимальная просадка**")
        dd_df = pd.DataFrame([
            {"Стратегия": s["label"].split("(")[0].strip(),
             "DD": results[s["label"]]["max_drawdown_pct"]}
            for s in STRATEGIES
        ])
        fig_dd = go.Figure(go.Bar(
            x=dd_df["DD"], y=dd_df["Стратегия"],
            orientation="h",
            marker_color=["#e74c3c" if d > 30 else "#f39c12" if d > 15 else "#2ecc71"
                          for d in dd_df["DD"]],
        ))
        fig_dd.update_layout(height=260, margin=dict(l=0, r=0, t=0, b=0),
                              xaxis_title="Просадка (%)", yaxis_title="")
        st.plotly_chart(fig_dd, use_container_width=True)

    # ── Kelly ─────────────────────────────────────────────────────────────────
    st.markdown(f"#### Kelly Criterion  |  капитал ${capital_usd:,.2f}")
    kelly_rows = []
    for s in STRATEGIES:
        r = results[s["label"]]
        p = r["win_rate"]
        q = 1 - p
        b = s["cashout"] - 1
        kelly_f   = max(0.0, (b * p - q) / b) if b > 0 else 0
        kelly_usd = kelly_f * capital_usd
        kelly_sol = kelly_usd / _sol_p
        kelly_rows.append({
            "Стратегия":   s["label"].split("(")[0].strip(),
            "Win rate":    f"{p*100:.1f}%",
            "Kelly f*":    f"{kelly_f*100:+.2f}%",
            "Shadow size ($)":  f"${kelly_usd:.2f}",
            "Shadow size (SOL)":f"{kelly_sol:.6f}",
            "Решение":     (
                f"shadow candidate ${kelly_usd:.2f}"
                if kelly_f > 0.005
                else "observe — negative EV"
            ),
        })
    st.dataframe(pd.DataFrame(kelly_rows), use_container_width=True, hide_index=True)

    # ── Вывод ────────────────────────────────────────────────────────────────
    st.markdown("---")
    best_s   = STRATEGIES[best_ev_idx]
    best_r   = results[best_s["label"]]
    low_s    = STRATEGIES[lowest_dd_idx]
    best_pnl = best_r["final"] - capital_usd
    best_ev_usd = best_r["ev_per_bet"] * capital_usd * global_bet_frac

    st.markdown(
        f"""
**Вывод по данным ({len(mults_seq):,} раундов)  |  капитал ${capital_usd:,.2f}:**

| | |
|---|---|
| **Лучший EV** | {best_s["label"]} → EV = `${best_ev_usd:+.4f}` на ставку  |  P&L = `${best_pnl:+.2f}` за весь период |
| **Наименьшая просадка** | {low_s["label"]} → `{results[low_s["label"]]["max_drawdown_pct"]:.1f}%` = `${results[low_s["label"]]["max_drawdown_pct"]/100*capital_usd:.2f}` |
| **Важно** | BCGame провабли-фэйр. Все стратегии дают **отрицательное ожидание** из-за house edge ~1%. ML-модель ищет краткосрочные паттерны. |

> Чем **меньше** каш-аут — тем медленнее теряешь. 1.5x медленнее сливает ${capital_usd:,.0f} чем 10x. Единственный способ бить казино — ML-сигналы с AUC > 0.55 на >5000 раундах.
"""
    )

    # ── Martingale vs Flat — ML/EV target ─────────────────────────────────────
    st.markdown("---")
    st.markdown(f"#### Martingale vs Flat  |  на основе ML-таргета и разных cashout")

    _ml_pred   = load_prediction()
    _ml_cashout = float(_ml_pred.get("prediction", {}).get("threshold", 2.0)) if _ml_pred else 2.0
    _ml_would_bet = bool(_ml_pred.get("prediction", {}).get("shadow_candidate", False)) if _ml_pred else False
    _ml_ev      = _ml_pred.get("prediction", {}).get("ev_at_cutoff", None) if _ml_pred else None

    _mg_caption = (
        f"ML shadow candidate: **{_ml_would_bet}**  |  ML cashout target: **{_ml_cashout}x**"
        + (f"  |  EV = {_ml_ev:+.4f}" if _ml_ev is not None else "")
        + "  |  Martingale удваивает ставку после проигрыша, сбрасывает на базу после выигрыша"
    )
    st.caption(_mg_caption)

    _mg_rows = []
    for _co in [1.5, 2.0, _ml_cashout, 3.0, 5.0]:
        _co = round(_co, 2)
        _flat = _backtest(mults_seq, _co, global_bet_frac, capital_usd)
        _mart = _backtest_martingale(mults_seq, _co, global_bet_frac, capital_usd)
        _mg_rows.append({
            "Cashout":           f"{_co}x" + (" ★ML" if abs(_co - _ml_cashout) < 0.01 else ""),
            "Flat P&L":          f"${_flat['final'] - capital_usd:+.2f}",
            "Flat ROI":          f"{_flat['roi_pct']:+.1f}%",
            "Flat max DD":       f"{_flat['max_drawdown_pct']:.1f}%",
            "Martingale P&L":    f"${_mart['final'] - capital_usd:+.2f}",
            "Martingale ROI":    f"{_mart['roi_pct']:+.1f}%",
            "Martingale max DD": f"{_mart['max_drawdown_pct']:.1f}%",
            "Лучше":             ("Martingale" if _mart["roi_pct"] > _flat["roi_pct"] else "Flat"),
        })

    _mg_df = pd.DataFrame(_mg_rows)

    def _mg_color(row):
        if "ML" in str(row.get("Cashout", "")):
            return ["background-color: rgba(52,152,219,0.25)"] * len(row)
        try:
            v = float(str(row["Martingale P&L"]).replace("$","").replace("+",""))
            if v > 0:
                return ["background-color: rgba(46,204,113,0.10)"] * len(row)
        except Exception:
            pass
        return [""] * len(row)

    st.dataframe(_mg_df.style.apply(_mg_color, axis=1),
                 use_container_width=True, hide_index=True)

    # Bankroll curves: Flat vs Martingale at ML cashout
    _fig_mg = go.Figure()
    _flat_ml = _backtest(mults_seq, _ml_cashout, global_bet_frac, capital_usd)
    _mart_ml = _backtest_martingale(mults_seq, _ml_cashout, global_bet_frac, capital_usd)
    _fig_mg.add_trace(go.Scatter(
        y=_flat_ml["curve"], mode="lines",
        name=f"Flat {_ml_cashout}x (ML)",
        line=dict(color="#3498db", width=2),
    ))
    _fig_mg.add_trace(go.Scatter(
        y=_mart_ml["curve"], mode="lines",
        name=f"Martingale {_ml_cashout}x (ML)",
        line=dict(color="#f39c12", width=2),
    ))
    _fig_mg.add_hline(y=capital_usd, line_dash="dot", line_color="white",
                      opacity=0.3, annotation_text=f"Start ${capital_usd:.0f}")
    _fig_mg.update_layout(
        height=300, margin=dict(l=0, r=0, t=10, b=0),
        legend=dict(orientation="h", y=-0.2),
        xaxis_title="Раунды", yaxis_title="Банкролл ($)",
        title=dict(text=f"Flat vs Martingale @ ML target {_ml_cashout}x  |  "
                        f"shadow candidate: {_ml_would_bet}", font=dict(size=12)),
    )
    st.plotly_chart(_fig_mg, use_container_width=True)

st.markdown("---")


# ── $100 Daily Calculator ─────────────────────────────────────────────────────

with st.expander(f"Daily P&L Calculator — стартовый капитал ${capital_usd:,.2f}", expanded=True):

    st.markdown(
        f"Стартовый банкролл **${capital_usd:,.2f}**  "
        f"= **{capital_sol:.4f} SOL** (1 SOL = ${_sol_p:.2f} via {_sol_src}).  "
        "Расчёт по реальным раундам с разбивкой по дням."
    )

    START_100 = capital_usd

    # Group rounds by calendar date for day-by-day P&L
    df_days = df.sort_values("id")[["multiplier", "date"]].copy()
    days_list = sorted(df_days["date"].unique())

    # Используем глобальные контролы из сайдбара
    sel_cashout  = global_cashout
    sel_bet_pct  = global_stake_pct
    bet_frac_sel = global_bet_frac

    # ── Daily backtest: each day runs on the continuous bankroll ──────────────
    daily_stats = []
    running_bankroll = START_100
    daily_curves = {}       # date -> list of bankroll snapshots within day

    for d in days_list:
        day_mults = df_days[df_days["date"] == d]["multiplier"].tolist()
        if not day_mults:
            continue
        day_start = running_bankroll
        curve_today = [day_start]
        wins_today = losses_today = 0
        for m in day_mults:
            if running_bankroll <= 0:
                break
            bet = running_bankroll * bet_frac_sel
            if m >= sel_cashout:
                running_bankroll += bet * (sel_cashout - 1)
                wins_today += 1
            else:
                running_bankroll -= bet
                losses_today += 1
            curve_today.append(running_bankroll)
        day_pnl = running_bankroll - day_start
        daily_stats.append({
            "date":        d,
            "start":       day_start,
            "end":         running_bankroll,
            "pnl":         day_pnl,
            "pnl_pct":     day_pnl / day_start * 100 if day_start > 0 else 0,
            "rounds":      wins_today + losses_today,
            "wins":        wins_today,
            "profit_day":  day_pnl > 0,
        })
        daily_curves[str(d)] = curve_today

    if not daily_stats:
        st.warning("Недостаточно данных по дням. Нужно хотя бы 2 дня в выбранном периоде.")
    else:
        ds_df = pd.DataFrame(daily_stats)
        avg_rounds_per_day = ds_df["rounds"].mean()
        profitable_days    = ds_df["profit_day"].sum()
        total_days         = len(ds_df)
        avg_pnl            = ds_df["pnl"].mean()
        median_pnl         = ds_df["pnl"].median()
        best_day           = ds_df.loc[ds_df["pnl"].idxmax()]
        worst_day          = ds_df.loc[ds_df["pnl"].idxmin()]
        final_bankroll     = running_bankroll
        total_pnl          = final_bankroll - START_100

        # ── KPI strip ────────────────────────────────────────────────────────
        k1, k2, k3, k4, k5, k6 = st.columns(6)
        k1.metric("Финальный банкролл",    f"${final_bankroll:.2f}",
                  f"{total_pnl:+.2f}$ итого")
        k2.metric("Прибыльных дней",       f"{profitable_days}/{total_days}",
                  f"{profitable_days/total_days*100:.0f}%")
        k3.metric("Ср. прибыль/день",      f"${avg_pnl:+.2f}")
        k4.metric("Медиана день",          f"${median_pnl:+.2f}")
        k5.metric("Лучший день",           f"${best_day['pnl']:+.2f}",
                  str(best_day["date"]))
        k6.metric("Худший день",           f"${worst_day['pnl']:+.2f}",
                  str(worst_day["date"]))

        # ── Bankroll growth curve ─────────────────────────────────────────────
        st.markdown(f"**Рост банкролла по дням (старт ${capital_usd:,.2f})**")
        fig_br = go.Figure()
        fig_br.add_trace(go.Scatter(
            x=ds_df["date"].astype(str), y=ds_df["end"],
            mode="lines+markers",
            line=dict(color="#3498db", width=2),
            marker=dict(
                color=["#2ecc71" if p else "#e74c3c" for p in ds_df["profit_day"]],
                size=8,
            ),
            name="Банкролл",
            hovertemplate="<b>%{x}</b><br>Банкролл: $%{y:.2f}<extra></extra>",
        ))
        fig_br.add_hline(y=START_100, line_dash="dot", line_color="white",
                         opacity=0.4, annotation_text=f"старт ${capital_usd:,.0f}")
        fig_br.update_layout(height=280, margin=dict(l=0, r=0, t=10, b=0),
                              yaxis_title="Банкролл ($)", xaxis_title="")
        st.plotly_chart(fig_br, use_container_width=True)

        # ── Daily P&L bars ────────────────────────────────────────────────────
        col_pnl, col_hist = st.columns(2)
        with col_pnl:
            st.markdown("**Прибыль/убыток по дням**")
            fig_pnl = go.Figure(go.Bar(
                x=ds_df["date"].astype(str),
                y=ds_df["pnl"],
                marker_color=["#2ecc71" if p else "#e74c3c" for p in ds_df["profit_day"]],
                hovertemplate="<b>%{x}</b><br>%{y:+.2f}$<extra></extra>",
            ))
            fig_pnl.add_hline(y=0, line_color="white", opacity=0.3)
            fig_pnl.update_layout(height=260, margin=dict(l=0, r=0, t=0, b=0),
                                  yaxis_title="P&L ($)")
            st.plotly_chart(fig_pnl, use_container_width=True)

        with col_hist:
            st.markdown("**Распределение дневных P&L**")
            fig_hist = go.Figure()
            pos = ds_df[ds_df["pnl"] >= 0]["pnl"]
            neg = ds_df[ds_df["pnl"] < 0]["pnl"]
            if len(pos):
                fig_hist.add_trace(go.Histogram(x=pos, nbinsx=20,
                                                marker_color="#2ecc71", name="Прибыль"))
            if len(neg):
                fig_hist.add_trace(go.Histogram(x=neg, nbinsx=20,
                                                marker_color="#e74c3c", name="Убыток"))
            fig_hist.add_vline(x=avg_pnl, line_dash="dash", line_color="#f39c12",
                               annotation_text=f"avg {avg_pnl:+.2f}$")
            fig_hist.update_layout(height=260, margin=dict(l=0, r=0, t=0, b=0),
                                   barmode="overlay", xaxis_title="P&L ($)", yaxis_title="Дней")
            st.plotly_chart(fig_hist, use_container_width=True)

        # ── All strategies at capital_usd ────────────────────────────────────
        st.markdown(f"**Все стратегии — финальный банкролл со стартом ${capital_usd:,.2f}**")
        strat_100 = []
        for s in STRATEGIES:
            r100 = _backtest(mults_seq, s["cashout"], bet_frac=bet_frac_sel, start=START_100)
            # days estimate
            days_est = total_days if total_days > 0 else 1
            strat_100.append({
                "Стратегия":          s["label"],
                "Финал":              f"${r100['final']:.2f}",
                "P&L":                f"${r100['final'] - START_100:+.2f}",
                "P&L %":              f"{r100['roi_pct']:+.1f}%",
                "Ср./день ($)":       f"${(r100['final']-START_100)/days_est:+.2f}",
                "% прибыльных ставок":f"{r100['win_rate']*100:.1f}%",
                "Макс. просадка":     f"{r100['max_drawdown_pct']:.1f}%",
            })
        st.dataframe(pd.DataFrame(strat_100), use_container_width=True, hide_index=True)

        # ── Honest answer ─────────────────────────────────────────────────────
        st.markdown("---")

        # Find the best strategy across $100 starts
        best_100_idx  = max(range(len(STRATEGIES)),
                            key=lambda i: _backtest(mults_seq, STRATEGIES[i]["cashout"],
                                                     bet_frac_sel, START_100)["roi_pct"])
        best_100_s = STRATEGIES[best_100_idx]
        best_100_r = _backtest(mults_seq, best_100_s["cashout"], bet_frac_sel, START_100)
        avg_per_day_best = (best_100_r["final"] - START_100) / max(total_days, 1)
        pct_win_days_sel = profitable_days / total_days * 100 if total_days else 0

        if avg_pnl > 0:
            verdict_color = "success"
            verdict = (
                f"На выбранной стратегии **{sel_cashout}x** с {sel_bet_pct}% ставкой "
                f"средний день даёт **+${avg_pnl:.2f}**. "
                f"Прибыльных дней: {profitable_days}/{total_days} ({pct_win_days_sel:.0f}%). "
                f"Это может быть статистическим шумом — нужно больше данных."
            )
        else:
            verdict_color = "warning"
            verdict = (
                f"На выбранной стратегии **{sel_cashout}x** с {sel_bet_pct}% ставкой "
                f"средний день даёт **${avg_pnl:.2f}** (убыток). "
                f"Прибыльных дней: {profitable_days}/{total_days} ({pct_win_days_sel:.0f}%)."
            )

        getattr(st, verdict_color)(verdict)

        _ev_per_round = (
            START_100 * bet_frac_sel
            * (1 / sel_cashout * (1 - 0.01) * (sel_cashout - 1)
               - (1 - 1 / sel_cashout * (1 - 0.01)))
        )
        _math_ev_day = _ev_per_round * int(avg_rounds_per_day)
        st.info(
            f"**Честный ответ:** гарантированно зарабатывать с ${capital_usd:,.0f} в день невозможно — "
            f"house edge ~1% делает ожидаемый результат отрицательным при любой ставке. "
            f"За {int(avg_rounds_per_day)} раундов в день при ставке {sel_bet_pct}% "
            f"математическое ожидание = "
            f"**${_math_ev_day:.2f}** в день. "
            f"Положительный результат возможен только через ML-сигналы с реальным edge."
        )

st.markdown("---")


# ── Crash curve helpers ───────────────────────────────────────────────────────

def _crash_curve_fig(crash_point: float, player_target: float,
                     bot_target: float, round_id: str = "") -> go.Figure:
    """Animated Plotly crash curve with Play button."""
    import numpy as np
    cp = max(float(crash_point), 1.01)
    n = 70
    t = np.linspace(0, 1.0, n)
    y = cp ** t  # 1.0x at t=0, crash_point at t=1

    y_max = max(cp * 1.15, player_target * 1.2, bot_target * 1.2, 2.0)

    frames = [
        go.Frame(
            data=[go.Scatter(
                x=t[:i + 1].tolist(), y=y[:i + 1].tolist(),
                mode="lines", line=dict(color="#00e676", width=3),
            )],
            name=str(i),
        )
        for i in range(1, n)
    ]

    fig = go.Figure(
        data=[go.Scatter(
            x=t[:1].tolist(), y=y[:1].tolist(),
            mode="lines", line=dict(color="#00e676", width=3), name="Multiplier",
        )],
        frames=frames,
    )

    # Player target
    if player_target <= cp:
        fig.add_shape(type="line", x0=0, x1=1, y0=player_target, y1=player_target,
                      line=dict(color="#ffd700", dash="dash", width=2))
        fig.add_annotation(x=0.02, y=player_target, text=f"You {player_target}x",
                           font=dict(color="#ffd700", size=11), showarrow=False,
                           xanchor="left", yanchor="bottom")

    # Bot target
    if bot_target <= cp:
        fig.add_shape(type="line", x0=0, x1=1, y0=bot_target, y1=bot_target,
                      line=dict(color="#00bcd4", dash="dot", width=2))
        fig.add_annotation(x=0.98, y=bot_target, text=f"Bot {bot_target}x",
                           font=dict(color="#00bcd4", size=11), showarrow=False,
                           xanchor="right", yanchor="bottom")

    # Crash marker
    fig.add_shape(type="line", x0=0, x1=1, y0=cp, y1=cp,
                  line=dict(color="#ff5252", width=1, dash="solid"))
    fig.add_annotation(x=0.98, y=cp, text=f"CRASH {cp:.2f}x",
                       font=dict(color="#ff5252", size=12, family="monospace"),
                       showarrow=False, xanchor="right", yanchor="top")

    title = f"Round #{round_id}" if round_id else "Crash Round"
    fig.update_layout(
        title=dict(text=title, font=dict(color="white", size=13), x=0),
        height=340,
        plot_bgcolor="#0e1117",
        paper_bgcolor="#0e1117",
        font=dict(color="white"),
        xaxis=dict(showticklabels=False, showgrid=False, zeroline=False, title=""),
        yaxis=dict(title="Multiplier", range=[0.9, y_max], gridcolor="#1e2a3a",
                   tickformat=".2f", tickfont=dict(size=11)),
        margin=dict(l=55, r=15, t=40, b=10),
        showlegend=False,
        updatemenus=[{
            "type": "buttons", "showactive": False,
            "y": 1.12, "x": 0.0, "xanchor": "left",
            "buttons": [
                {"label": "  Play  ", "method": "animate",
                 "args": [None, {"frame": {"duration": 35, "redraw": True},
                                  "fromcurrent": True, "mode": "immediate",
                                  "transition": {"duration": 0}}]},
                {"label": "Pause", "method": "animate",
                 "args": [[None], {"frame": {"duration": 0, "redraw": False},
                                    "mode": "immediate"}]},
            ],
        }],
        sliders=[{
            "active": 0, "x": 0, "y": -0.02, "len": 1.0,
            "pad": {"t": 15},
            "currentvalue": {"visible": False},
            "steps": [
                {"args": [[f.name], {"frame": {"duration": 0, "redraw": True},
                                      "mode": "immediate",
                                      "transition": {"duration": 0}}],
                 "label": "", "method": "animate"}
                for f in frames
            ],
        }],
    )
    return fig


@st.cache_data(ttl=30)
def _bot_target_cached(mults_tuple: tuple, pred_cashout) -> float:
    """Best cashout target: ML prediction if available, else math_engine EV."""
    if pred_cashout:
        try:
            return float(pred_cashout)
        except (ValueError, TypeError):
            pass
    if len(mults_tuple) >= 50:
        try:
            import numpy as np
            from math_engine import strategy_ev
            rows = strategy_ev(np.array(mults_tuple),
                               targets=[1.5, 2.0, 2.5, 3.0, 5.0, 10.0])
            if rows:
                # Least-negative EV target (or best positive)
                best = max(rows, key=lambda r: r["ev_per_bet"])
                return float(best["target"])
        except Exception:
            pass
    return 2.0


# ── Virtual Simulator ─────────────────────────────────────────────────────────

with st.expander(
    f"Virtual Simulator — {capital_sol:.4f} SOL (${capital_usd:,.2f}) Test Account",
    expanded=True,
):
    tab_auto, tab_manual = st.tabs(["Auto (ML)", "Manual (play mode)"])

    # ── AUTO TAB ──────────────────────────────────────────────────────────────
    with tab_auto:
        sol_price, sol_price_status, sol_source = cached_sol_price()
        not_enough    = len(df_all) < 500

        st.info("Training process supervision is owned by watchdog.py or the CLI, not by the dashboard.")

        if not_enough:
            st.caption(
                f"Раундов в DB: {len(df_all):,} / 500 нужно для первого обучения.  "
                "Коллектор собирает данные — обучение начнётся автоматически."
            )

        vd     = load_virtual_data(db_path)
        state  = vd["state"]
        hist   = vd["history"]
        curve  = vd["curve"]
        invariants = vd.get("invariants") or {"ok": False, "problems": ["missing_invariant_report"]}

        if not invariants.get("ok", False):
            st.error(
                "DEGRADED — virtual account invariants failed: "
                + ", ".join(invariants.get("problems") or ["unknown"])
            )

        bankroll_sol = state.get("bankroll_sol", START_SOL)
        bankroll_usd = bankroll_sol * sol_price
        total_bets   = state.get("total_bets", 0)
        total_wins   = state.get("total_wins", 0)
        total_pnl    = state.get("total_pnl_sol", 0.0)
        win_rate     = total_wins / total_bets if total_bets else 0.0
        total_losses = total_bets - total_wins

        if bankroll_sol < 0.000006:
            st.error(
                "BANKRUPT — virtual bankroll depleted.  "
                f"Started: {START_SOL:.4f} SOL (${START_SOL * sol_price:.2f})  |  "
                f"Lost: {(START_SOL - bankroll_sol):.6f} SOL"
            )
        elif bankroll_sol < START_SOL * 0.5:
            st.warning(
                f"LOW BALANCE — {bankroll_sol:.6f} SOL (${bankroll_usd:.2f})  "
                f"({bankroll_sol/START_SOL*100:.0f}% of start)"
            )
        else:
            _price_info = f"1 SOL = ${sol_price:.2f}  (via {sol_source}"
            if sol_price_status in (PriceFeedStatus.STALE_CACHE, PriceFeedStatus.UNAVAILABLE):
                _price_info += f"  ⚠️ {sol_price_status.value}"
            _price_info += ")"
            st.info(
                f"{_price_info}  |  Bankroll: {bankroll_sol:.6f} SOL = ${bankroll_usd:.2f}"
            )

        k1, k2, k3, k4, k5, k6 = st.columns(6)
        k1.metric("Bankroll (SOL)",  f"{bankroll_sol:.6f}",
                  f"{(bankroll_sol - START_SOL):+.6f} SOL")
        k2.metric("Bankroll (USD)",  f"${bankroll_usd:.2f}",
                  f"${(bankroll_sol - START_SOL)*sol_price:+.2f}")
        k3.metric("Rounds bet",      f"{total_bets:,}")
        k4.metric("Win rate",        f"{win_rate*100:.1f}%",
                  f"W:{total_wins} L:{total_losses}")
        k5.metric("P&L (SOL)",       f"{total_pnl:+.6f}")
        k6.metric("P&L (USD)",       f"${total_pnl * sol_price:+.2f}")

        if len(curve) > 1:
            st.markdown("**Bankroll over time**")
            curve_usd = [v * sol_price for v in curve]
            fig_sim = go.Figure()
            fig_sim.add_trace(go.Scatter(
                y=curve_usd, mode="lines",
                line=dict(color="#3498db", width=2),
                name="Bankroll ($)",
                hovertemplate="Bet #%{x}<br>$%{y:.2f}<extra></extra>",
            ))
            fig_sim.add_hline(y=START_SOL * sol_price, line_dash="dot",
                              line_color="rgba(255,255,255,0.4)",
                              annotation_text=f"Start ${START_SOL*sol_price:.0f}")
            fig_sim.add_hline(y=0, line_color="#e74c3c", opacity=0.6,
                              annotation_text="Bankrupt")
            fig_sim.update_layout(
                height=260, margin=dict(l=0, r=0, t=10, b=0),
                yaxis_title="Bankroll ($)", xaxis_title="Bet #",
            )
            st.plotly_chart(fig_sim, use_container_width=True)
        else:
            st.info(
                "No virtual bets recorded yet.  "
                "No virtual shadow records yet. Start the training pipeline outside the dashboard."
            )

        if hist:
            st.markdown("**Last 20 virtual bets**")
            rows_disp = []
            for b in hist[:20]:
                won    = b.get("won")
                br_usd = (b.get("bankroll_after") or 0) * sol_price
                rows_disp.append({
                    "Round":        b.get("game_round_id", "—"),
                    "Signal":       b.get("signal", "—"),
                    "Confidence":   f"{(b.get('confidence') or 0)*100:.1f}%",
                    "Bet (SOL)":    f"{b.get('bet_sol', 0):.6f}",
                    "Bet ($)":      f"${(b.get('bet_sol', 0)*sol_price):.4f}",
                    "Cashout":      f"{b.get('cashout_target', 2)}x",
                    "Crash":        f"{b.get('actual_mult', 0):.2f}x",
                    "Result":       "WIN" if won else "LOSS",
                    "P&L ($)":      f"${(b.get('pnl_sol', 0)*sol_price):+.4f}",
                    "Bankroll ($)": f"${br_usd:.2f}",
                })
            bets_df = pd.DataFrame(rows_disp)

            def _bet_color(row):
                c = "rgba(46,204,113,0.15)" if row["Result"] == "WIN" else "rgba(231,76,60,0.15)"
                return [f"background-color: {c}"] * len(row)

            st.dataframe(bets_df.style.apply(_bet_color, axis=1),
                         use_container_width=True, hide_index=True)

            # Crash curve for most recent bot bet
            last_b = hist[0] if hist else None
            if last_b and last_b.get("actual_mult") and last_b.get("cashout_target"):
                st.markdown("**Last round played by bot**")
                _auto_bot_tgt  = float(last_b.get("cashout_target", 2))
                _auto_crash    = float(last_b.get("actual_mult", 1))
                _auto_rid      = str(last_b.get("game_round_id", ""))
                fig_auto = _crash_curve_fig(
                    crash_point=_auto_crash,
                    player_target=_auto_bot_tgt,
                    bot_target=_auto_bot_tgt,
                    round_id=_auto_rid,
                )
                st.plotly_chart(fig_auto, use_container_width=True)

        st.markdown("---")
        col_reset, col_info = st.columns([1, 5])
        with col_reset:
            if st.button(
                "Reset virtual account",
                type="secondary",
                disabled=not ALLOW_DASHBOARD_MUTATIONS,
            ):
                VirtualAccount(db_path).reset()
                st.cache_data.clear()
                st.rerun()
        with col_info:
            st.caption(
                f"Reset restores starting bankroll to {START_SOL} SOL.  "
                "Losses are fed back into model training automatically."
            )

    # ── MANUAL TAB ────────────────────────────────────────────────────────────
    with tab_manual:
        import time as _mtime
        import random as _rnd

        _M_N_FRAMES  = 60    # animation frames per round
        _M_FRAME_SEC = 0.15  # seconds per frame → ~9 s total animation

        # Session state — resets when capital changes
        if ("m_balance" not in st.session_state
                or st.session_state.get("m_capital") != capital_usd):
            st.session_state.m_balance      = capital_usd
            st.session_state.m_capital      = capital_usd
            st.session_state.m_history      = []
            st.session_state.m_last         = None
            st.session_state.m_round_active = False
        # Ensure animation keys exist for older sessions
        for _mk, _mv in [("m_round_active", False), ("m_frame_idx", 0),
                          ("m_current_crash", None), ("m_current_rid", ""),
                          ("m_cashed_out", False), ("m_cashout_mult", None),
                          ("m_bet_amount", 0.0), ("m_bot_tgt", 2.0),
                          ("m_martingale", False)]:
            if _mk not in st.session_state:
                st.session_state[_mk] = _mv

        m_bal  = st.session_state.m_balance
        m_hist = st.session_state.m_history

        # Bot target (cached, 30 s TTL)
        _pred         = load_prediction()
        _mults_sample = tuple(df["multiplier"].tolist()[-2000:])
        bot_tgt       = _bot_target_cached(_mults_sample, _pred.get("cashout_target"))

        mults_avail = df["multiplier"].tolist()
        rids_avail  = (df["game_round_id"].tolist()
                       if "game_round_id" in df.columns else [])

        # ── Stats row (always visible) ────────────────────────────────────────
        pnl_total = m_bal - capital_usd
        _sa, _sb, _sc, _sd = st.columns(4)
        _sa.metric("Balance", f"${m_bal:.2f}", f"{pnl_total:+.2f}$")
        _sb.metric("P&L",     f"${pnl_total:+.2f}",
                   f"{pnl_total / capital_usd * 100:+.1f}%")
        if m_hist:
            _wr  = sum(1 for r in m_hist if r["won"]) / len(m_hist)
            _bwr = sum(1 for r in m_hist if r.get("bot_won")) / len(m_hist)
            _sc.metric("Win rate (you / bot)",
                       f"{_wr*100:.0f}% / {_bwr*100:.0f}%",
                       f"{len(m_hist)} rounds")
        else:
            _sc.metric("Win rate", "--")
        _sd.metric("Bot target", f"{bot_tgt}x",
                   help="ML prediction or best EV target from historical data.")

        # ══════════════════════════════════════════════════════════════════════
        # SANDBOX ROUND — server-side animation loop with simulator exit button
        # ══════════════════════════════════════════════════════════════════════
        if st.session_state.m_round_active:
            cp    = float(st.session_state.m_current_crash)
            frame = int(st.session_state.m_frame_idx)
            rid   = str(st.session_state.m_current_rid)
            bet   = float(st.session_state.m_bet_amount)
            _btgt = float(st.session_state.m_bot_tgt)

            crashed    = frame >= _M_N_FRAMES
            cashed_out = bool(st.session_state.m_cashed_out)

            if not crashed and not cashed_out:
                # ── Draw partial live curve ───────────────────────────────
                import numpy as _np2
                t_now  = frame / _M_N_FRAMES
                c_mult = max(1.0, cp ** t_now)
                ts_so_far = _np2.linspace(0, t_now, frame + 1)
                ys_so_far = cp ** ts_so_far

                _color = "#ff9800" if c_mult >= _btgt else "#00e676"
                fig_live = go.Figure()
                fig_live.add_trace(go.Scatter(
                    x=ts_so_far.tolist(), y=ys_so_far.tolist(),
                    mode="lines", line=dict(color=_color, width=4),
                ))
                fig_live.add_hline(
                    y=_btgt, line_dash="dot", line_color="#00bcd4",
                    annotation_text=f"Bot {_btgt}x",
                    annotation_font_color="#00bcd4",
                )
                fig_live.update_layout(
                    height=300,
                    margin=dict(l=55, r=15, t=55, b=10),
                    plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                    font=dict(color="white"),
                    xaxis=dict(showticklabels=False, showgrid=False,
                               zeroline=False, range=[0, 1]),
                    yaxis=dict(title="Multiplier", gridcolor="#1e2a3a",
                               tickformat=".2f",
                               range=[0.9, max(cp * 1.1, _btgt * 1.1, 3.0)]),
                    title=dict(
                        text=f"<b>{c_mult:.2f}x</b>   SANDBOX",
                        font=dict(color=_color, size=30), x=0.5,
                    ),
                    showlegend=False,
                )
                st.plotly_chart(fig_live, use_container_width=True,
                                key=f"live_{frame}")

                # Simulator exit button
                if st.button(
                    f"Record simulator exit  {c_mult:.2f}x",
                    type="primary",
                    disabled=not ALLOW_DASHBOARD_MUTATIONS,
                    use_container_width=True,
                    key=f"co_{frame}",
                ):
                    st.session_state.m_cashed_out   = True
                    st.session_state.m_cashout_mult = c_mult
                    st.rerun()

                # Advance to next frame
                _mtime.sleep(_M_FRAME_SEC)
                st.session_state.m_frame_idx += 1
                st.rerun()

            else:
                # ── Round OVER (crashed or cashed out) — finalize once ────
                cashout_mult = st.session_state.m_cashout_mult
                if cashed_out and cashout_mult is not None:
                    won = True
                    pnl = bet * (cashout_mult - 1.0)
                    player_line = cashout_mult
                else:
                    won = False
                    pnl = -bet
                    player_line = cp
                bot_won = cp >= _btgt
                new_bal = max(0.0, m_bal + pnl)

                # Persist round result
                st.session_state.m_balance      = new_bal
                st.session_state.m_round_active = False
                st.session_state.m_last = {
                    "mult":       cp,
                    "won":        won,
                    "pnl":        pnl,
                    "cashout":    player_line,
                    "bet":        bet,
                    "bot_target": _btgt,
                    "bot_won":    bot_won,
                    "round_id":   rid,
                }
                st.session_state.m_history.insert(0, {
                    "Round":        rid or "--",
                    "Crash":        f"{cp:.2f}x",
                    "Your cashout": (f"{cashout_mult:.2f}x"
                                     if cashed_out and cashout_mult else "MISS"),
                    "Your result":  "WIN" if won else "LOSS",
                    "Bot target":   f"{_btgt}x",
                    "Bot result":   "WIN" if bot_won else "LOSS",
                    "P&L":          f"${pnl:+.4f}",
                    "Balance":      f"${new_bal:.2f}",
                    "Bet":          f"${bet:.4f}",
                    "won":          won,
                    "bot_won":      bot_won,
                    "bet_amt":      bet,
                })
                st.rerun()  # → IDLE branch renders result curve + banner

        else:
            # ══════════════════════════════════════════════════════════════
            # IDLE — sandbox controls
            # ══════════════════════════════════════════════════════════════

            # Bet size slider + Reset
            _ic1, _ic2 = st.columns([5, 1])
            with _ic1:
                m_stake_pct = st.slider(
                    "Bet (% of balance)", 1, 20, global_stake_pct, step=1,
                    key="m_stake",
                )
            with _ic2:
                if st.button(
                    "Reset sandbox",
                    key="m_reset",
                    disabled=not ALLOW_DASHBOARD_MUTATIONS,
                    use_container_width=True,
                ):
                    for _k2 in ("m_balance", "m_capital", "m_history", "m_last",
                                "m_round_active", "m_frame_idx", "m_current_crash",
                                "m_current_rid", "m_cashed_out", "m_cashout_mult",
                                "m_bet_amount", "m_bot_tgt"):
                        if _k2 in st.session_state:
                            del st.session_state[_k2]
                    st.rerun()

            m_bet_base = m_bal * (m_stake_pct / 100.0)

            m_martingale = st.checkbox(
                "Martingale: x2 if loss, reset to base if win",
                value=st.session_state.get("m_martingale", False),
                key="m_martingale_chk",
                help="After a loss the next bet doubles. After a win the bet resets to base (stake % of balance).",
            )
            st.session_state.m_martingale = m_martingale

            # ML prediction for this round
            _m_pred       = load_prediction()
            _m_shadow     = bool(_m_pred.get("prediction", {}).get("shadow_candidate", False)) if _m_pred else False
            _m_ml_cashout = float(_m_pred.get("prediction", {}).get("threshold", bot_tgt)) if _m_pred else bot_tgt
            _m_ev         = _m_pred.get("prediction", {}).get("ev_at_cutoff", None) if _m_pred else None
            _m_kelly      = _m_pred.get("prediction", {}).get("kelly_bet_fraction", 0.0) if _m_pred else 0.0

            if m_martingale and m_hist:
                _last_r = m_hist[0]
                if not _last_r["won"]:
                    _prev_bet = _last_r.get("bet_amt", m_bet_base)
                    m_bet = min(_prev_bet * 2.0, m_bal)
                    _mg_label = f"LOSS → x2 = **${m_bet:.4f}**"
                else:
                    m_bet = m_bet_base
                    _mg_label = f"WIN → база = **${m_bet:.4f}**"
            elif m_martingale:
                m_bet = m_bet_base
                _mg_label = f"старт = **${m_bet:.4f}**"
            else:
                m_bet = m_bet_base
                _mg_label = None

            # Caption with ML shadow signal
            _sig_color = "[SHADOW]" if _m_shadow else "[OBSERVE]"
            if _mg_label:
                _play_advice = (
                    f"Observation: {_sig_color} ML shadow @ {_m_ml_cashout}x"
                    + (f" EV={_m_ev:+.4f}" if _m_ev is not None else "")
                    + (f" shadow Kelly={_m_kelly*100:.1f}%" if _m_kelly > 0 else "")
                )
                st.caption(
                    f"Martingale {_mg_label}  |  {_play_advice}  |  "
                    f"ML shadow target **{_m_ml_cashout}x**"
                )
            else:
                st.caption(
                    f"Sandbox amount: ${m_bet:.4f}  |  {_sig_color} ML shadow @ {_m_ml_cashout}x"
                    + (f"  EV={_m_ev:+.4f}" if _m_ev is not None else "")
                    + f"  |  Simulation controls are disabled unless local mutations are enabled."
                )

            # Last round: static animated curve + result banner
            if st.session_state.m_last:
                lr = st.session_state.m_last
                fig_last = _crash_curve_fig(
                    crash_point=lr["mult"],
                    player_target=lr["cashout"],
                    bot_target=lr["bot_target"],
                    round_id=str(lr.get("round_id", "")),
                )
                st.plotly_chart(fig_last, use_container_width=True)

                _yw = lr["won"]
                _bw = lr.get("bot_won")
                if _yw and _bw:
                    st.success(
                        f"WIN  Crash {lr['mult']:.2f}x | "
                        f"You {lr['cashout']:.2f}x +${lr['pnl']:.4f} | "
                        f"Bot {lr['bot_target']}x WIN"
                    )
                elif _yw and not _bw:
                    st.success(
                        f"YOU WIN  Crash {lr['mult']:.2f}x | "
                        f"You {lr['cashout']:.2f}x +${lr['pnl']:.4f} | "
                        f"Bot {lr['bot_target']}x LOSS"
                    )
                elif not _yw and _bw:
                    st.warning(
                        f"YOU LOSE  Crash {lr['mult']:.2f}x | "
                        f"Missed cashout -${abs(lr['pnl']):.4f} | "
                        f"Bot {lr['bot_target']}x WIN"
                    )
                else:
                    st.error(
                        f"LOSS  Crash {lr['mult']:.2f}x | "
                        f"Missed cashout -${abs(lr['pnl']):.4f} | "
                        f"Bot {lr['bot_target']}x LOSS too"
                    )
            else:
                st.info(
                    "Sandbox simulator is disabled by default. "
                    "Set CRASH_DASHBOARD_MUTATIONS=1 locally to enable manual simulation controls."
                )

            # Start button
            if st.button(
                "Start sandbox round",
                disabled=(m_bal < 0.0001 or not mults_avail or not ALLOW_DASHBOARD_MUTATIONS),
                type="primary",
                key="m_play",
                use_container_width=True,
            ):
                _idx = _rnd.randrange(len(mults_avail))
                st.session_state.m_round_active = True
                st.session_state.m_frame_idx    = 0
                st.session_state.m_current_crash = mults_avail[_idx]
                st.session_state.m_current_rid  = (str(rids_avail[_idx])
                                                    if rids_avail else "")
                st.session_state.m_cashed_out   = False
                st.session_state.m_cashout_mult = None
                st.session_state.m_bet_amount   = m_bet
                st.session_state.m_bot_tgt      = bot_tgt
                st.rerun()

            # Bankroll chart
            if len(m_hist) > 1:
                _bkv = [capital_usd] + [
                    float(r["Balance"].replace("$", "")) for r in reversed(m_hist)
                ]
                _bkc = ["#3498db"] + [
                    "#2ecc71" if r["won"] else "#e74c3c" for r in reversed(m_hist)
                ]
                fig_bk = go.Figure(go.Scatter(
                    y=_bkv, mode="lines+markers",
                    line=dict(color="#9b59b6", width=2),
                    marker=dict(color=_bkc, size=7),
                    hovertemplate="Round %{x}<br>$%{y:.2f}<extra></extra>",
                ))
                fig_bk.add_hline(y=capital_usd, line_dash="dot",
                                 line_color="white", opacity=0.3,
                                 annotation_text=f"start ${capital_usd:.0f}")
                fig_bk.update_layout(
                    height=200, margin=dict(l=0, r=0, t=10, b=0),
                    yaxis_title="Balance ($)", xaxis_title="Round",
                    plot_bgcolor="#0e1117", paper_bgcolor="#0e1117",
                    font=dict(color="white"),
                )
                st.plotly_chart(fig_bk, use_container_width=True)

            # History table
            if m_hist:
                st.markdown("**Round history**")
                _disp = [{k: v for k, v in r.items()
                          if k not in ("won", "bot_won")} for r in m_hist[:30]]
                _hdf  = pd.DataFrame(_disp)

                def _mh_color(row):
                    _c = ("rgba(46,204,113,0.15)" if row["Your result"] == "WIN"
                          else "rgba(231,76,60,0.15)")
                    return [f"background-color: {_c}"] * len(row)

                st.dataframe(_hdf.style.apply(_mh_color, axis=1),
                             use_container_width=True, hide_index=True)

st.markdown("---")


# ── Strategy Optimizer (SOL) ──────────────────────────────────────────────────

with st.expander("Strategy Optimizer — SOL combinations (Flat / Martingale / Anti-Martingale)", expanded=False):

    sol_price_opt, *_ = cached_sol_price()
    mults_opt = df.sort_values("id")["multiplier"].tolist()

    if len(mults_opt) < 50:
        st.warning("Need at least 50 rounds for optimizer. Collect more data.")
    else:
        start_sol_input = capital_usd / sol_price_opt
        start_usd_disp  = capital_usd
        st.caption(
            f"Capital: ${capital_usd:,.2f} = {start_sol_input:.4f} SOL  |  "
            f"Optimizer uses last {min(len(mults_opt), 5000):,} rounds  |  "
            f"1 SOL = ${sol_price_opt:.2f}"
        )

        with st.spinner("Running strategy grid search..."):
            opt_results = run_optimizer(mults_opt, start_sol=start_sol_input,
                                        sol_usd=sol_price_opt)

        if not opt_results:
            st.warning("No optimizer results.")
        else:
            # ── Top-10 table ──────────────────────────────────────────────────
            st.markdown("**Top 10 strategies by Sharpe ratio**")
            top10 = opt_results[:10]
            tbl = []
            for r in top10:
                bb_usd = r.base_bet_sol * sol_price_opt
                final_usd = r.final_sol * sol_price_opt
                pnl_usd   = (r.final_sol - r.start_sol) * sol_price_opt
                tbl.append({
                    "Strategy":       r.name,
                    "Base bet (SOL)": f"{r.base_bet_sol:.6f}",
                    "Base bet ($)":   f"${bb_usd:.4f}",
                    "Cashout":        f"{r.cashout}x",
                    "Win rate":       f"{r.win_rate*100:.1f}%",
                    "EV/bet":         f"{r.ev_per_bet:+.4f}",
                    "Final (SOL)":    f"{r.final_sol:.6f}",
                    "Final ($)":      f"${final_usd:.2f}",
                    "P&L ($)":        f"${pnl_usd:+.2f}",
                    "Max DD":         f"{r.max_drawdown_pct:.1f}%",
                    "Sharpe":         f"{r.sharpe:+.3f}",
                    "Busts":          str(r.busts),
                })
            tbl_df = pd.DataFrame(tbl)

            def _opt_color(row):
                try:
                    pnl = float(row["P&L ($)"].replace("$", "").replace("+", ""))
                    if pnl > 0:
                        return ["background-color: rgba(46,204,113,0.15)"] * len(row)
                except Exception:
                    pass
                return [""] * len(row)

            st.dataframe(tbl_df.style.apply(_opt_color, axis=1),
                         use_container_width=True, hide_index=True)

            # ── Bankroll curves (top 5) ───────────────────────────────────────
            st.markdown(f"**Bankroll curves — top 5 strategies (start: {start_sol_input:.4f} SOL = ${start_usd_disp:.2f})**")
            colors5 = ["#3498db","#2ecc71","#f39c12","#e67e22","#9b59b6"]
            fig_opt = go.Figure()
            for i, r in enumerate(opt_results[:5]):
                curve_usd = [v * sol_price_opt for v in r.bankroll_curve]
                fig_opt.add_trace(go.Scatter(
                    y=curve_usd, mode="lines",
                    name=r.name[:35],
                    line=dict(color=colors5[i % 5], width=1.5),
                    hovertemplate="Round %{x}<br>$%{y:.2f}<extra></extra>",
                ))
            fig_opt.add_hline(y=start_usd_disp, line_dash="dot",
                              line_color="white", opacity=0.3,
                              annotation_text=f"Start ${start_usd_disp:.0f}")
            fig_opt.add_hline(y=0, line_color="#e74c3c", opacity=0.5,
                              annotation_text="Bankrupt")
            fig_opt.update_layout(
                height=360, margin=dict(l=0, r=0, t=10, b=0),
                legend=dict(orientation="h", y=-0.2),
                yaxis_title="Bankroll ($)", xaxis_title="Round",
            )
            st.plotly_chart(fig_opt, use_container_width=True)

            # ── EV vs Drawdown scatter ────────────────────────────────────────
            st.markdown("**EV per bet vs Max Drawdown (bubble = |Sharpe|)**")
            all_r = opt_results[:40]
            scatter_df = pd.DataFrame([{
                "Strategy":  r.name[:30],
                "EV":        r.ev_per_bet,
                "DrawdownPct": r.max_drawdown_pct,
                "Sharpe":    abs(r.sharpe),
                "PnL_usd":   (r.final_sol - r.start_sol) * sol_price_opt,
            } for r in all_r])
            fig_sc = px.scatter(
                scatter_df,
                x="DrawdownPct", y="EV",
                size="Sharpe", color="PnL_usd",
                color_continuous_scale="RdYlGn",
                hover_name="Strategy",
                labels={"DrawdownPct": "Max Drawdown (%)", "EV": "EV per bet",
                        "PnL_usd": "P&L ($)"},
                height=320,
            )
            fig_sc.add_hline(y=0, line_dash="dash", line_color="white", opacity=0.4)
            fig_sc.update_layout(margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig_sc, use_container_width=True)

            st.caption(
                "Strategies to the top-left (high EV, low drawdown) are best.  "
                "Green = positive P&L; red = loss.  "
                "Note: positive EV here is backtested — real results depend on future data distribution."
            )

st.markdown("---")


# ── ML Prediction card ────────────────────────────────────────────────────────

with st.expander("ML Prediction — следующий раунд", expanded=True):
    pred_data = load_prediction()
    if pred_data:
        pred        = pred_data.get("prediction", {})
        metrics     = pred_data.get("metrics", {})
        top_features= pred_data.get("top_features", {})
        selective   = pred_data.get("selective", {})
        best_cutoff = pred_data.get("best_cutoff", 0.52)

        signal    = pred.get("signal", "?")
        p_cal     = pred.get("p_above_threshold", 0)
        p_raw     = pred.get("p_raw", p_cal)
        threshold = pred.get("threshold", 2.0)
        kelly_f   = pred.get("kelly_bet_fraction", 0)
        ev_cut    = pred.get("ev_at_cutoff", 0)

        gov = pred_data.get("governance") or governance_payload()
        would_bet = bool(pred.get("shadow_candidate") or pred.get("would_bet"))
        st.warning(
            f"{gov.get('system_status', 'MANUAL_REVIEW')} / observation only  |  "
            f"promotion_allowed={gov.get('promotion_allowed', False)}"
        )
        if would_bet and ev_cut > 0:
            st.info(
                f"SHADOW_CANDIDATE  |  P(>={threshold}x) = {p_cal:.1%}  |  "
                f"EV = {ev_cut:+.4f}  |  Kelly shadow size = {kelly_f*100:.1f}%  |  "
                f"confidence cutoff: {best_cutoff:.0%}"
            )
        else:
            st.info(
                f"OBSERVE_ONLY  |  P(>={threshold}x) = {p_cal:.1%}  |  "
                f"no shadow candidate for this round"
            )

        # ── Metrics row ───────────────────────────────────────────────────────
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("AUC (calibrated)",  f"{metrics.get('auc_cal', 0):.4f}")
        m2.metric("AUC (raw)",         f"{metrics.get('auc_raw', 0):.4f}")
        m3.metric("Brier score",       f"{metrics.get('brier_score', 0):.4f}",
                  help="Ниже = лучше. 0.25 = случайная модель")
        m4.metric("Base win rate",     f"{metrics.get('base_win_rate', 0):.1%}")
        m5.metric("Test set size",     f"{metrics.get('test_n', 0):,}")
        m6.metric("Trained on",        f"{pred_data.get('rows_used', 0):,}")

        # ── Selective shadow table ───────────────────────────────────────────
        if selective:
            st.markdown("**Selective shadow candidates when P >= cutoff:**")
            sel_rows = []
            for cut_str, info in sorted(selective.items(), key=lambda x: float(x[0])):
                cut = float(cut_str)
                if info.get("win_rate") is None:
                    sel_rows.append({
                        "Порог P": f">= {cut:.0%}",
                        "% раундов": "< 10 ставок",
                        "Win rate": "—",
                        "EV на ставку": "—",
                        "Kelly %": "—",
                        "Решение": "мало данных",
                    })
                else:
                    ev = info["ev"]
                    wr = info["win_rate"]
                    kf = info["kelly_f"]
                    is_best = abs(cut - best_cutoff) < 0.001
                    sel_rows.append({
                        "Порог P":     f">= {cut:.0%}" + (" ★" if is_best else ""),
                        "% раундов":   f"{info['pct_bets']:.0f}%  ({info['n_bets']} ставок)",
                        "Win rate":    f"{wr*100:.1f}%",
                        "EV на ставку":f"{ev:+.4f}",
                        "Kelly %":     f"{kf*100:.1f}%",
                        "Решение":     "shadow candidate" if ev > 0 else "observe",
                    })
            sel_df = pd.DataFrame(sel_rows)

            def _sel_color(row):
                if "shadow candidate" in str(row.get("Решение", "")):
                    return ["background-color: rgba(46,204,113,0.2)"] * len(row)
                return [""] * len(row)

            st.dataframe(sel_df.style.apply(_sel_color, axis=1),
                         use_container_width=True, hide_index=True)

            st.caption(
                "EV > 0 is reported as shadow evidence only. "
                "Kelly % is a model diagnostic, not an execution instruction. "
                "★ = оптимальный порог на исторических данных."
            )

        # ── Feature importance ────────────────────────────────────────────────
        if top_features:
            st.markdown("**Топ-10 фич (что модель считает важным):**")
            feat_df = pd.DataFrame(
                [(k, v) for k, v in top_features.items()],
                columns=["Feature", "Importance"]
            ).sort_values("Importance", ascending=False)
            fig = px.bar(feat_df, x="Importance", y="Feature", orientation="h",
                         color="Importance", color_continuous_scale="Blues", height=300)
            fig.update_layout(margin=dict(l=0,r=0,t=0,b=0),
                              coloraxis_showscale=False, yaxis_title="")
            st.plotly_chart(fig, use_container_width=True)

        st.caption(pred_data.get("disclaimer", ""))
    else:
        st.info(
            "ML модель ещё не обучена. Запусти:\n"
            "```\npython training.py\n```\n"
            "Нужно минимум 500 раундов."
        )

st.markdown("---")


# ── Crashes per day ───────────────────────────────────────────────────────────

with st.expander("Crashes per day / hour / weekday", expanded=True):
    tab_day, tab_hour, tab_week = st.tabs(["By day", "By hour", "By weekday"])

    with tab_day:
        daily = df.groupby("date").agg(
            count=("multiplier", "size"),
            under110=("multiplier", lambda x: (x < 1.1).sum()),
        ).reset_index()
        fig = go.Figure()
        fig.add_bar(x=daily["date"], y=daily["count"],    name="Total",  marker_color="#3498db")
        fig.add_bar(x=daily["date"], y=daily["under110"], name="< 1.1x", marker_color="#e74c3c")
        fig.update_layout(barmode="overlay", height=300,
                          margin=dict(l=0,r=0,t=10,b=0),
                          legend=dict(orientation="h", y=1.1))
        st.plotly_chart(fig, use_container_width=True)

    with tab_hour:
        hourly = df.groupby("hour").size().reset_index(name="count")
        fig = px.bar(hourly, x="hour", y="count", color="count",
                     color_continuous_scale="Blues",
                     labels={"hour": "Hour", "count": "Rounds"})
        fig.update_layout(height=280, margin=dict(l=0,r=0,t=0,b=0),
                          coloraxis_showscale=False)
        st.plotly_chart(fig, use_container_width=True)

    with tab_week:
        order = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        wday = df.groupby("weekday").size().reindex(order).reset_index()
        wday.columns = ["Day", "Rounds"]
        fig = px.bar(wday, x="Day", y="Rounds", color="Rounds",
                     color_continuous_scale="Purples")
        fig.update_layout(height=280, margin=dict(l=0,r=0,t=0,b=0),
                          coloraxis_showscale=False)
        st.plotly_chart(fig, use_container_width=True)

st.markdown("---")


# ── Distribution ──────────────────────────────────────────────────────────────

with st.expander("Multiplier distribution", expanded=True):
    col_a, col_b = st.columns(2)

    with col_a:
        cat_df = df["category"].value_counts().sort_index().reset_index()
        cat_df.columns = ["Range", "Count"]
        cat_df["Pct"] = (cat_df["Count"] / n * 100).round(2)
        colors = ["#e74c3c","#e67e22","#f39c12","#f1c40f",
                  "#2ecc71","#27ae60","#1abc9c","#3498db","#9b59b6","#8e44ad"]
        fig = px.bar(cat_df, x="Range", y="Count",
                     color="Range", color_discrete_sequence=colors, text="Pct")
        fig.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
        fig.update_layout(height=350, margin=dict(l=0,r=0,t=10,b=0),
                          showlegend=False, xaxis_title="")
        st.plotly_chart(fig, use_container_width=True)

    with col_b:
        fig = px.histogram(df[df["multiplier"] <= 20], x="multiplier",
                           nbins=80, log_y=True,
                           color_discrete_sequence=["#e74c3c"],
                           labels={"multiplier": "Multiplier"})
        fig.add_vline(x=2.0, line_dash="dash", line_color="white", opacity=0.5,
                      annotation_text="2x", annotation_position="top right")
        fig.update_layout(height=350, margin=dict(l=0,r=0,t=10,b=0))
        st.plotly_chart(fig, use_container_width=True)

st.markdown("---")


# ── Timeline (recent rounds) ──────────────────────────────────────────────────

with st.expander("Recent rounds timeline", expanded=True):
    last_n = st.slider("Show rounds", 50, 500, 300, step=50)
    recent = df.head(last_n).sort_values("id")
    bar_colors = ["#e74c3c" if m < 2.0 else "#2ecc71" for m in recent["multiplier"]]

    fig = go.Figure(go.Bar(
        x=recent["id"].astype(str),
        y=recent["multiplier"],
        marker_color=bar_colors,
        hovertemplate="Round #%{x}<br>%{y:.2f}x<extra></extra>",
    ))
    fig.add_hline(y=2.0, line_dash="dash", line_color="rgba(255,255,255,0.3)",
                  annotation_text="2x")
    fig.update_layout(height=300, margin=dict(l=0,r=0,t=10,b=0),
                      showlegend=False, yaxis_type="log",
                      yaxis_title="Multiplier (log)", xaxis=dict(showticklabels=False))
    st.plotly_chart(fig, use_container_width=True)

st.markdown("---")


# ── Survival probability table ────────────────────────────────────────────────

with st.expander("Survival probability vs fair theory", expanded=False):
    thresholds = [1.1, 1.5, 2.0, 2.5, 3.0, 5.0, 10.0, 25.0, 50.0, 100.0]
    prob_rows = []
    for t in thresholds:
        cnt  = (df["multiplier"] >= t).sum()
        emp  = cnt / n
        fair = 1.0 / t
        edge = 1.0 - t * emp
        prob_rows.append({
            "Target":         f"{t}x",
            "Reached":        int(cnt),
            "Empirical P":    round(emp, 4),
            "Fair P (1/x)":   round(fair, 4),
            "House edge est": f"{edge*100:.2f}%",
            "Delta":          f"{(emp-fair):+.4f}",
        })
    st.dataframe(pd.DataFrame(prob_rows), use_container_width=True, hide_index=True,
                 column_config={
                     "Reached":      st.column_config.NumberColumn(format="%d"),
                     "Empirical P":  st.column_config.NumberColumn(format="%.4f"),
                     "Fair P (1/x)": st.column_config.NumberColumn(format="%.4f"),
                 })

st.markdown("---")


# ── Bet size analytics ────────────────────────────────────────────────────────

with st.expander("Bet size & money at stake", expanded=True):
    bets_df = load_bets(db_path)
    has_bet_table = not bets_df.empty
    has_round_bets = df["total_bets"].notna().any()

    if has_round_bets:
        bet_df = df.dropna(subset=["total_bets"])
        b1, b2, b3 = st.columns(3)
        b1.metric("Avg USDT per round", f"{bet_df['total_bets'].mean():.2f}")
        b2.metric("Max USDT in round",  f"{bet_df['total_bets'].max():.2f}")
        b3.metric("Rounds with bets",   f"{len(bet_df):,}")

        col_l, col_r = st.columns(2)
        with col_l:
            _sdf = bet_df.copy()
            _sdf["ts"] = _sdf["ts"].astype(str)
            fig = px.scatter(_sdf, x="total_bets", y="multiplier",
                             color="multiplier", color_continuous_scale="RdYlGn",
                             log_x=True, log_y=True, opacity=0.6,
                             labels={"total_bets": "Total bets (USDT)",
                                     "multiplier": "Crash multiplier"},
                             hover_data={"ts": True, "num_bettors": True})
            fig.update_layout(height=380, margin=dict(l=0,r=0,t=10,b=0))
            st.plotly_chart(fig, use_container_width=True)
            corr = bet_df[["total_bets","multiplier"]].corr().iloc[0,1]
            direction = ("weak negative" if corr < -0.1
                         else "weak positive" if corr > 0.1
                         else "essentially none")
            st.caption(f"Pearson r (bets vs multiplier): {corr:.4f} — {direction} correlation")

        with col_r:
            # Bets over time
            bet_time = bet_df.sort_values("id").tail(500)
            fig2 = go.Figure()
            fig2.add_trace(go.Scatter(
                x=bet_time["id"].astype(str), y=bet_time["total_bets"],
                mode="lines", name="Total bets", line=dict(color="#3498db", width=1),
            ))
            fig2.update_layout(height=380, margin=dict(l=0,r=0,t=10,b=0),
                               yaxis_title="USDT", xaxis=dict(showticklabels=False))
            st.plotly_chart(fig2, use_container_width=True)

        # Bettor count distribution
        if df["num_bettors"].notna().any():
            st.markdown("**Bettor count distribution**")
            nc_df = df.dropna(subset=["num_bettors"])
            fig3 = px.histogram(nc_df, x="num_bettors", nbins=50,
                                color_discrete_sequence=["#9b59b6"],
                                labels={"num_bettors": "Players per round"})
            fig3.update_layout(height=250, margin=dict(l=0,r=0,t=0,b=0))
            st.plotly_chart(fig3, use_container_width=True)

    elif has_bet_table:
        st.info("Round-level bet totals not yet available — showing raw bet records.")
        currency_counts = bets_df["currency"].value_counts().reset_index()
        currency_counts.columns = ["Currency", "Count"]
        st.dataframe(currency_counts, use_container_width=True, hide_index=True)
    else:
        st.info(
            "Bet-size data will appear once the collector processes `\\x01b` bet frames. "
            "Data is stored in both the `rounds` table (aggregated) and the `bets` table (per-bet)."
        )

st.markdown("---")


# ── Pattern analysis ──────────────────────────────────────────────────────────

with st.expander("Pattern analysis", expanded=False):
    pa1, pa2 = st.columns(2)

    with pa1:
        st.markdown("**Streak distribution (< 2x consecutive)**")
        mults_seq = df.sort_values("id")["multiplier"].tolist()
        below_streaks = []
        cur = 0
        for m in mults_seq:
            if m < 2.0:
                cur += 1
            else:
                if cur:
                    below_streaks.append(cur)
                cur = 0
        if cur:
            below_streaks.append(cur)

        if below_streaks:
            from collections import Counter
            streak_counts = Counter(below_streaks)
            sk_df = pd.DataFrame(
                sorted(streak_counts.items()), columns=["Streak length", "Count"]
            ).head(20)
            fig = px.bar(sk_df, x="Streak length", y="Count",
                         color="Count", color_continuous_scale="Reds",
                         labels={"Streak length": "Consecutive rounds < 2x"})
            fig.update_layout(height=300, margin=dict(l=0,r=0,t=0,b=0),
                              coloraxis_showscale=False)
            st.plotly_chart(fig, use_container_width=True)
            st.caption(
                f"Max streak: {max_streak}  |  "
                f"Avg streak: {sum(below_streaks)/len(below_streaks):.1f}  |  "
                f"Total streaks: {len(below_streaks)}"
            )

    with pa2:
        st.markdown("**Autocorrelation lag plot (multiplier)**")
        lags = list(range(1, 21))
        series = df.sort_values("id")["multiplier"].values
        acf_vals = []
        for lag in lags:
            if len(series) > lag:
                corr = pd.Series(series).autocorr(lag=lag)
                acf_vals.append(corr)
            else:
                acf_vals.append(0.0)
        fig = go.Figure(go.Bar(x=lags, y=acf_vals,
                               marker_color=["#e74c3c" if v < 0 else "#2ecc71" for v in acf_vals]))
        fig.add_hline(y=0, line_color="white", opacity=0.3)
        fig.update_layout(height=300, margin=dict(l=0,r=0,t=0,b=0),
                          xaxis_title="Lag", yaxis_title="Autocorrelation")
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Near-zero autocorrelation at all lags confirms independent rounds (as expected for provably fair).")

st.markdown("---")


# ── Provably fair hash verification ──────────────────────────────────────────

with st.expander("Provably fair hash verification", expanded=False):
    hash_df = df_all.dropna(subset=["hash"]) if "hash" in df_all.columns else pd.DataFrame()
    if hash_df.empty:
        st.info(
            "No hashes collected yet. The collector captures hashes from "
            "`\\x02st` (round-stats) frames automatically."
        )
    else:
        st.metric("Rounds with hash", f"{len(hash_df):,} / {total_all:,}")

        # Show last 20 hashes
        recent_hash = hash_df.sort_values("id", ascending=False).head(20)[
            ["id", "game_round_id", "multiplier", "hash"]
        ].copy()
        recent_hash.columns = ["Round #", "Game Round ID", "Multiplier", "Hash (64 hex)"]
        st.dataframe(recent_hash, use_container_width=True, hide_index=True,
                     column_config={
                         "Multiplier": st.column_config.NumberColumn(format="%.2fx"),
                         "Hash (64 hex)": st.column_config.TextColumn(width="large"),
                     })

        st.caption(
            "BCGame uses SHA-256 hash chain: H[n-1] = SHA256(H[n]). "
            "Run `python analysis.py` for full chain verification."
        )

st.markdown("---")


# ── Full data table ───────────────────────────────────────────────────────────

with st.expander("All rounds (sortable table)", expanded=False):
    ctrl1, ctrl2, ctrl3, ctrl4 = st.columns([2, 1, 1, 1])
    with ctrl1:
        sort_col = st.selectbox("Sort by",
            ["id","multiplier","ts","total_bets","num_bettors"],
            format_func=lambda x: {
                "id":"Round #","multiplier":"Multiplier","ts":"Time",
                "total_bets":"Bets","num_bettors":"Players",
            }.get(x, x))
    with ctrl2:
        sort_asc = st.radio("Order", ["Desc", "Asc"], horizontal=True) == "Asc"
    with ctrl3:
        page_size = st.selectbox("Rows", [50, 100, 200, 500])
    with ctrl4:
        page_num = st.number_input("Page", min_value=1, value=1, step=1)

    df_sorted   = df.sort_values(sort_col, ascending=sort_asc)
    total_pages = max(1, (len(df_sorted) - 1) // page_size + 1)
    page_num    = min(page_num, total_pages)
    start       = (page_num - 1) * page_size

    chunk = df_sorted.iloc[start : start + page_size][
        ["id","multiplier","ts","category","source","total_bets","num_bettors","game_round_id","hash"]
    ].copy()
    chunk.columns = ["Round #","Multiplier","Time","Category","Source","Bets","Players","Round ID","Hash"]

    st.caption(f"Page {page_num}/{total_pages}  |  {len(df_sorted):,} rows total")
    st.dataframe(
        chunk, use_container_width=True, hide_index=True,
        column_config={
            "Multiplier": st.column_config.NumberColumn(format="%.2fx"),
            "Bets":       st.column_config.NumberColumn(format="%.2f"),
            "Time":       st.column_config.DatetimeColumn(format="DD.MM.YY HH:mm:ss"),
        },
    )

    if st.button("Export current view to CSV"):
        csv = df_sorted[["id","multiplier","ts","source","total_bets","num_bettors"]].to_csv(index=False)
        st.download_button("Download CSV", data=csv,
                           file_name=f"crash_{d_from}_{d_to}.csv", mime="text/csv")
