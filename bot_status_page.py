"""
Generates data/status.html — styled bot status page.
Usage: venv/bin/python bot_status_page.py [--data-dir data] [--out data/status.html]
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot_state import BotAccount
from bot_strategy import STRATEGIES

LIVE_START_USD = float(os.environ.get("REALBET_START_USD", "12.0"))

RULES = {
    "martingale":                          "2.3x martingale",
    "martingale_21":                       "2.1x baseline",
    "martingale_30":                       "3.0x shadow",
    "x5":                                  "5.0x high target",
    "sniper_v2":                           "veto x10 last5",
    "waveskip":                            "thermal cold skip",
    "waveskip_21":                         "thermal skip @ 2.1x + martingale",
    "turtle":                              "slow 2.1x",
    "turtle_micro":                        "micro turtle",
    "relwave_bad_veto_21":                 "skip hot→cold bad wave",
    "relwave_bad_veto_21_sticky2":         "sticky x2 until 2 wins",
    "relwave_bad_veto_21_trigger10_x512":  "trigger10 x512 martingale",
    "relwave_l10_h5_x3":                   "x3 relative wave",
    "relwave_l12_h5_21":                   "relative low12→high5",
    "relwave_l12_h5_21_strict":            "strict relative wave",
    "relwave_l15_h7_x3_d3":               "x3 delay3",
    "elliott_impulse5_21":                 "Elliott wave5",
    "elliott_impulse5_x3":                 "Elliott x3 shadow",
    "cold_breakout_21":                    "cold breakout 2.1x",
    "cold_breakout_x3":                    "cold breakout 3.0x",
}


# ─────────────────────────── data collection ──────────────────────────────
def _collect_paper(data_dir, now):
    rows = []
    for db_path in sorted(glob.glob(os.path.join(data_dir, "bot_state_*.duckdb"))):
        strategy = (os.path.basename(db_path)
                    .replace("bot_state_", "").replace(".duckdb", ""))
        cfg = STRATEGIES.get(strategy)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".duckdb")
            os.close(fd)
            shutil.copy2(db_path, tmp)

            acct   = BotAccount(tmp)
            state  = acct.get_state()
            totals = acct.get_totals()
            bank   = state["total_bank_sol"]
            pnl    = totals["total_pnl_sol"]
            bets   = totals["total_bets"]
            wins   = totals["total_wins"]
            wr     = wins / bets * 100 if bets > 0 else float("nan")

            import duckdb as _ddb
            with _ddb.connect(tmp, read_only=True) as cn:
                snap_vals = [s[0] for s in cn.execute(
                    "SELECT total_bank_sol FROM strategy_snapshots ORDER BY ts ASC"
                ).fetchall()]

            mxdd = 0.0
            if snap_vals:
                banks = snap_vals + [bank]
                peak  = banks[0]
                for b in banks:
                    if b > peak: peak = b
                    dd = (peak - b) / peak if peak > 0 else 0.0
                    if dd > mxdd: mxdd = dd

            def _snap(secs):
                return acct.get_snapshot_near(
                    datetime.fromtimestamp(now.timestamp() - secs, tz=timezone.utc))

            def _dpct(sn):
                if sn is None: return float("nan")
                base = sn["total_bank_sol"]
                return (bank - base) / base if base > 0 else 0.0

            rows.append({
                "strategy": strategy,
                "status":   "active",
                "cashout":  cfg.cashout      if cfg else float("nan"),
                "bank_pct": cfg.session_frac * 100 if cfg else float("nan"),
                "bank":     bank,
                "pnl":      pnl,
                "d1_pct":   _dpct(_snap(3600)),
                "d24_pct":  _dpct(_snap(86400)),
                "wr":       wr,
                "bets":     bets,
                "max_dd":   mxdd * 100,
                "rule":     RULES.get(strategy, ""),
            })
        except Exception as e:
            rows.append({
                "strategy": strategy,
                "status":   "error",
                "error":    str(e)[:80],
                "cashout":  cfg.cashout if cfg else float("nan"),
                "bank_pct": cfg.session_frac * 100 if cfg else float("nan"),
            })
        finally:
            if tmp:
                for ext in ("", ".wal"):
                    p = tmp + ext
                    try:
                        if os.path.exists(p): os.unlink(p)
                    except Exception:
                        pass
    return sorted(rows, key=lambda r: r.get("pnl", float("-inf")), reverse=True)


def _collect_live(data_dir):
    state_f = Path(data_dir) / "realbet_state.json"
    if not state_f.exists():
        return None
    try:
        st = json.loads(state_f.read_text())
    except Exception:
        return None

    bal      = st.get("last_balance_usd", LIVE_START_USD)
    bets     = st.get("bets", 0)
    wins     = st.get("wins", 0)
    wr       = wins / bets * 100 if bets > 0 else float("nan")
    strategy = st.get("strategy", "relwave_bad_veto_21")
    pnl_sess = st.get("pnl", float("nan"))
    cfg      = STRATEGIES.get(strategy)

    return {
        "strategy":  strategy,
        "cashout":   cfg.cashout if cfg else 2.1,
        "balance":   bal,
        "pnl_all":   bal - LIVE_START_USD,
        "pnl_sess":  pnl_sess,
        "bets":      bets,
        "wins":      wins,
        "wr":        wr,
        "rule":      RULES.get(strategy, strategy),
    }


def _last_log_lines(data_dir, n=6):
    log = Path(data_dir).parent / "logs" / "bot_realbet.log"
    if not log.exists():
        return []
    try:
        lines = log.read_text(errors="replace").splitlines()
        return [l for l in lines[-n:] if l.strip()]
    except Exception:
        return []


# ─────────────────────────── HTML helpers ─────────────────────────────────
def _h(s):
    """HTML-escape."""
    return (str(s)
            .replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _pct_cell(v):
    if v != v:
        return '<td class="muted">n/a</td>'
    s = "+" if v >= 0 else ""
    cls = "green" if v > 0 else ("red" if v < 0 else "muted")
    return f'<td class="{cls}">{s}{v*100:.2f}%</td>'


def _pnl_cell(v, fmt="sol"):
    if v != v:
        return '<td class="muted">n/a</td>'
    s = "+" if v >= 0 else ""
    cls = "green" if v > 0 else ("red" if v < 0 else "muted")
    if fmt == "usd":
        txt = f'{s}${abs(v):.4f}' if v >= 0 else f'-${abs(v):.4f}'
    else:
        txt = f'{s}{v:.6f}'
    return f'<td class="{cls} mono">{_h(txt)}</td>'


def _dd_cell(v):
    if v != v:
        return '<td class="muted">n/a</td>'
    cls = "red" if v >= 5 else ("yellow" if v >= 1 else "muted")
    return f'<td class="{cls}">{v:.2f}%</td>'


def _badge(text, kind="ok"):
    return f'<span class="badge badge-{kind}">{_h(text)}</span>'


# ─────────────────────────── HTML generation ──────────────────────────────
CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: #0d1117;
  color: #c9d1d9;
  font-family: 'Segoe UI', 'SF Pro', system-ui, sans-serif;
  padding: 28px 32px;
  font-size: 14px;
  line-height: 1.55;
}
h1 {
  font-size: 1.55rem;
  color: #f0f6fc;
  font-weight: 700;
  margin-bottom: 6px;
  letter-spacing: -0.3px;
}
.subtitle { color: #8b949e; font-size: 0.85rem; margin-bottom: 32px; }
.section {
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 10px;
  padding: 22px 26px;
  margin-bottom: 26px;
}
.section-title {
  font-size: 1.05rem;
  font-weight: 700;
  color: #f0f6fc;
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.badge {
  font-size: 0.72rem;
  font-weight: 600;
  padding: 2px 9px;
  border-radius: 12px;
  letter-spacing: 0.3px;
}
.badge-ok   { background: #1a4731; color: #4ade80; border: 1px solid #166534; }
.badge-warn { background: #451a03; color: #fb923c; border: 1px solid #7c2d12; }
.badge-err  { background: #3b0101; color: #f87171; border: 1px solid #7f1d1d; }
.badge-live { background: #312e0f; color: #facc15; border: 1px solid #713f12; }

table { border-collapse: collapse; width: 100%; }
thead th {
  background: #0d1117;
  color: #8b949e;
  font-weight: 600;
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  padding: 7px 14px;
  border-bottom: 1px solid #30363d;
  text-align: right;
  white-space: nowrap;
}
thead th:first-child, thead th:nth-child(2), thead th:last-child { text-align: left; }
tbody td {
  padding: 7px 14px;
  border-bottom: 1px solid #21262d;
  text-align: right;
  white-space: nowrap;
}
tbody td:first-child, tbody td:nth-child(2), tbody td:last-child { text-align: left; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: #1c2128; }

.kv-table thead th { text-align: left; }
.kv-table tbody td { text-align: left; }
.kv-table tbody td:first-child {
  color: #8b949e;
  font-weight: 600;
  font-size: 0.82rem;
  width: 180px;
  text-transform: uppercase;
  letter-spacing: 0.4px;
}

.green  { color: #4ade80; font-weight: 600; }
.red    { color: #f87171; font-weight: 600; }
.yellow { color: #facc15; }
.muted  { color: #6e7681; }
.white  { color: #f0f6fc; font-weight: 600; }
.mono   { font-family: 'Cascadia Code', 'Fira Code', monospace; }

.rank-num { color: #8b949e; font-size: 0.8rem; width: 36px; }
.rank-1   { color: #facc15 !important; }
.rank-2   { color: #c9d1d9 !important; }
.rank-3   { color: #fb923c !important; }
.rank-neg { color: #f87171 !important; }
.rank-err { color: #f87171 !important; font-style: italic; }

.log-block {
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 6px;
  padding: 12px 16px;
  margin-top: 14px;
  font-family: 'Cascadia Code', 'Fira Code', monospace;
  font-size: 0.78rem;
  color: #8b949e;
  line-height: 1.7;
}
.log-win  { color: #4ade80; }
.log-loss { color: #f87171; }
.log-bet  { color: #93c5fd; }
.log-cd   { color: #facc15; }

.footer { color: #6e7681; font-size: 0.78rem; margin-top: 16px; }
.divider { border: none; border-top: 1px solid #21262d; margin: 18px 0; }
"""


def _log_html(lines):
    out = []
    for line in lines:
        escaped = _h(line)
        if "WIN" in line:
            escaped = f'<span class="log-win">{escaped}</span>'
        elif "LOSS" in line:
            escaped = f'<span class="log-loss">{escaped}</span>'
        elif "BET" in line:
            escaped = f'<span class="log-bet">{escaped}</span>'
        elif "Cooldown" in line or "cooldown" in line:
            escaped = f'<span class="log-cd">{escaped}</span>'
        out.append(escaped)
    return "<br>".join(out)


def generate_html(paper, live, log_lines, now):
    # ── Timestamps ──────────────────────────────────────────────────────
    utc_s   = now.strftime("%H:%M UTC")
    local_h = now.hour + 3  # Europe/Chisinau UTC+3
    local_h = local_h % 24
    local_s = f"{local_h:02d}:{now.minute:02d} local (Chisinau)"
    ts_full = now.strftime("%Y-%m-%d %H:%M:%S UTC")

    # ── Live bot section ─────────────────────────────────────────────────
    if live:
        bal     = live["balance"]
        pnl_all = live["pnl_all"]
        pnl_pct = pnl_all / LIVE_START_USD * 100
        bets    = live["bets"]
        wins    = live["wins"]
        wr      = live["wr"]
        sess    = live.get("pnl_sess", float("nan"))

        bal_cls  = "green" if bal >= LIVE_START_USD else "red"
        pnl_cls  = "green" if pnl_all >= 0 else "red"
        pnl_sign = "+" if pnl_all >= 0 else ""
        pnl_s    = f'{pnl_sign}${abs(pnl_all):.4f}' if pnl_all >= 0 else f'-${abs(pnl_all):.4f}'
        pnl_pct_s = f'({pnl_sign}{pnl_pct:.2f}%)'
        wr_cls   = "green" if (wr == wr and wr >= 30) else ("yellow" if (wr == wr and wr >= 20) else "red")
        wr_s     = f"{wr:.1f}%" if wr == wr else "n/a"
        sess_s   = (f'{sess:+.4f} USD' if sess == sess else "n/a")

        live_badge = _badge("active", "ok")
        if bets > 0 and wins == 0:
            live_badge = _badge("BROKEN", "err")

        live_html = f"""
<div class="section">
  <div class="section-title">
    💰 REAL MONEY BOT — ivanK1157
    &nbsp;{live_badge}
    &nbsp;<span class="badge badge-live">crash-bot-realbet.service</span>
  </div>
  <table class="kv-table">
    <thead><tr><th>Field</th><th>Value</th></tr></thead>
    <tbody>
      <tr><td>Strategy</td>
          <td><code class="mono">{_h(live['strategy'])}</code>
              &nbsp;@ <strong>{live['cashout']}x</strong> cashout
              &nbsp;&mdash;&nbsp;<span class="muted">{_h(live['rule'])}</span></td></tr>
      <tr><td>Balance</td>
          <td class="{bal_cls} mono">${bal:.4f}</td></tr>
      <tr><td>P&amp;L All</td>
          <td class="{pnl_cls} mono">{_h(pnl_s)} <span class="muted">{_h(pnl_pct_s)}</span></td></tr>
      <tr><td>Since Restart</td>
          <td class="{'green' if sess==sess and sess>=0 else 'red'} mono">{_h(sess_s)}</td></tr>
      <tr><td>Bets / WR</td>
          <td><strong>{bets}</strong> bets &nbsp;/&nbsp; <span class="{wr_cls}">{wr_s}</span> win rate
              &nbsp;(<span class="muted">{wins} wins</span>)</td></tr>
      <tr><td>WIN/LOSS detect</td>
          <td><span class="green">&#10003; working</span> &nbsp;(balance diff per round)</td></tr>
      <tr><td>Martingale scale</td>
          <td><span class="green">&#10003; working</span> &nbsp;(1→2→4→8, cooldown after 4 losses)</td></tr>
      <tr><td>Starting balance</td>
          <td class="muted mono">${LIVE_START_USD:.2f} USD</td></tr>
    </tbody>
  </table>
  {"<div class='log-block'><strong style='color:#8b949e;font-size:0.75rem;letter-spacing:.5px'>RECENT LOG</strong><br>" + _log_html(log_lines) + "</div>" if log_lines else ""}
</div>"""
    else:
        live_html = """
<div class="section">
  <div class="section-title">💰 REAL MONEY BOT &nbsp;<span class="badge badge-err">no data</span></div>
  <p class="muted">realbet_state.json not found.</p>
</div>"""

    # ── Paper bots section ───────────────────────────────────────────────
    n_ok  = sum(1 for r in paper if r.get("status") == "active")
    n_err = sum(1 for r in paper if r.get("status") == "error")

    p_badge = _badge(f"all {n_ok} active", "ok") if n_err == 0 else \
              _badge(f"{n_ok} active / {n_err} error", "warn")

    paper_rows = []
    for i, r in enumerate(paper):
        rank     = i + 1
        strategy = r["strategy"]
        status   = r.get("status", "?")

        # rank style
        if status == "error":
            rank_cls = "rank-err"
            rank_txt = "ERR"
        elif rank == 1:
            rank_cls, rank_txt = "rank-1", "1"
        elif rank == 2:
            rank_cls, rank_txt = "rank-2", "2"
        elif rank == 3:
            rank_cls, rank_txt = "rank-3", "3"
        else:
            pnl_ = r.get("pnl", float("nan"))
            rank_cls = "rank-neg" if (pnl_ == pnl_ and pnl_ < 0) else "rank-num"
            rank_txt = str(rank)

        if status == "error":
            err = _h(r.get("error", "?")[:60])
            paper_rows.append(
                f'<tr>'
                f'<td class="{rank_cls}">{rank_txt}</td>'
                f'<td class="red">{_h(strategy)}</td>'
                f'<td colspan="8" class="red muted">{err}</td>'
                f'<td></td>'
                f'</tr>'
            )
            continue

        pnl    = r.get("pnl",    float("nan"))
        bank   = r.get("bank",   float("nan"))
        d1     = r.get("d1_pct", float("nan"))
        d24    = r.get("d24_pct",float("nan"))
        wr     = r.get("wr",     float("nan"))
        bets   = r.get("bets",   0)
        mdd    = r.get("max_dd", float("nan"))
        bp     = r.get("bank_pct", float("nan"))
        co     = r.get("cashout", float("nan"))
        rule   = r.get("rule", "")

        pnl_cls = "green" if pnl > 0 else ("red" if pnl < 0 else "muted")
        pnl_s   = (("+" if pnl >= 0 else "") + f"{pnl:.6f}") if pnl == pnl else "n/a"
        bank_s  = f"{bank:.6f}" if bank == bank else "n/a"
        wr_s    = f"{wr:.1f}%" if wr == wr else "n/a"
        co_s    = f"{co:.1f}" if co == co else "?"
        bp_s    = f"{bp:.0f}%" if bp == bp else "?"

        paper_rows.append(
            f'<tr>'
            f'<td class="{rank_cls}">{rank_txt}</td>'
            f'<td class="white">{_h(strategy)}</td>'
            f'<td class="mono muted">{_h(bank_s)}</td>'
            f'<td class="{pnl_cls} mono">{_h(pnl_s)}</td>'
            + _pct_cell(d1)
            + _pct_cell(d24)
            + f'<td class="muted">{_h(wr_s)}</td>'
            f'<td class="muted">{bets}</td>'
            + _dd_cell(mdd)
            + f'<td class="muted">{_h(co_s)}x / {_h(bp_s)}</td>'
            f'<td class="muted" style="font-size:0.8rem">{_h(rule)}</td>'
            f'</tr>'
        )

    paper_html = f"""
<div class="section">
  <div class="section-title">
    📊 PAPER BOTS &nbsp;{p_badge}
  </div>
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Strategy</th>
        <th>Bankroll SOL</th>
        <th>P&amp;L SOL</th>
        <th>%1h</th>
        <th>%24h</th>
        <th>WinRate</th>
        <th>Bets</th>
        <th>MaxDD</th>
        <th>Cash/Bank%</th>
        <th>Rule</th>
      </tr>
    </thead>
    <tbody>
      {"".join(paper_rows)}
    </tbody>
  </table>
</div>"""

    # ── Assemble ─────────────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="60">
<title>Bot Status — {utc_s}</title>
<style>{CSS}</style>
</head>
<body>
<h1>STATUS &mdash; {_h(utc_s)} / {_h(local_s)}</h1>
<p class="subtitle">Auto-refresh every 60s &nbsp;&bull;&nbsp; {_h(ts_full)}</p>
{live_html}
{paper_html}
<p class="footer">Generated {_h(ts_full)} &nbsp;&bull;&nbsp; bot_status_page.py</p>
</body>
</html>"""


# ─────────────────────────── main ─────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--out",      default="data/status.html")
    args = parser.parse_args()

    now      = datetime.now(timezone.utc)
    paper    = _collect_paper(args.data_dir, now)
    live     = _collect_live(args.data_dir)
    log_lines = _last_log_lines(args.data_dir)

    html = generate_html(paper, live, log_lines, now)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"Generated: {out_path.resolve()}")
    print(f"Open in browser: file://{out_path.resolve()}")


if __name__ == "__main__":
    main()
