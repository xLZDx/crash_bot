"""
Strategy proposals based on streak analysis.
Tests 4 new ideas vs existing strategies on 22k rounds.
"""
import sys
import statistics
import duckdb
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot_strategy import STRATEGIES, decide, new_session_bank, StrategyConfig

DB    = "data/crash.duckdb"
START = 100.0

conn = duckdb.connect(DB, read_only=True)
MULTS = [r[0] for r in conn.execute("SELECT multiplier FROM rounds ORDER BY id ASC").fetchall()]
conn.close()
N = len(MULTS)


# ── Standard backtest engine (reused for all strategies) ─────────────────────

def run_std(name: str) -> dict:
    """Run existing strategy with its individual optimal cooldown."""
    cfg = STRATEGIES[name]
    bank = START
    sb   = new_session_bank(bank, cfg)
    ss   = sb
    sr   = 0
    cl   = 0
    sc   = 1.0
    cdl  = 0
    cbl  = 0
    bets = wins = 0
    peak = bank
    mxdd = 0.0
    trigger = cfg.consec_loss_trigger
    pause   = cfg.consec_loss_pause

    for i, mult in enumerate(MULTS):
        if cdl > 0:
            cdl -= 1; sr += 1; continue
        recent = MULTS[max(0, i - max(cfg.thermal_window, 5)):i]
        dec = decide(sb, ss, bank, sr, cl, cfg, sc, recent)
        act = dec["action"]
        if act == "end_session":
            pnl = sb - ss
            cl  = (cl + 1) if pnl < 0 else 0
            bank += pnl
            sb = new_session_bank(bank, cfg); ss = sb; sr = 0; sc = 1.0; cbl = 0
            if bank <= 0.01: break
        elif act in ("paused", "no_bet", "no_funds"):
            sr += 1
        elif act == "bet":
            bet = dec["bet_sol"]; sr += 1; bets += 1
            if mult >= dec["cashout"]:
                sb += bet * (dec["cashout"] - 1.0); wins += 1; cbl = 0
                if cfg.loss_scale > 1.0: sc = 1.0
                elif cfg.scaling > 1.0:  sc = min(sc * cfg.scaling, cfg.max_scale)
            else:
                sb -= bet; cbl += 1
                if cfg.loss_scale > 1.0: sc = min(sc * cfg.loss_scale, cfg.max_scale)
                elif cfg.scaling > 1.0:  sc = 1.0
                if trigger > 0 and cbl >= trigger:
                    cdl = pause; sc = 1.0; cbl = 0
        if bank > peak: peak = bank
        dd = (peak - bank) / peak if peak > 0 else 0
        if dd > mxdd: mxdd = dd
    bank += (sb - ss)
    wr = wins / bets * 100 if bets > 0 else 0
    return {"roi": (bank - START) / START * 100, "pnl": bank - START,
            "mxdd": mxdd * 100, "bets": bets, "wr": wr, "bank": bank}


# ── Custom backtest for new strategies ───────────────────────────────────────

def run_custom(strategy_fn, name: str) -> dict:
    """
    strategy_fn(i, mults) -> (bet, cashout) | (0, 0) to skip
    Called for each round; manages bank externally.
    Simple flat-bet model for clarity.
    """
    bank = START
    bets = wins = 0
    peak = bank
    mxdd = 0.0
    equity_curve = []

    for i, mult in enumerate(MULTS):
        bet, cashout = strategy_fn(i, MULTS)
        if bet <= 0 or bet > bank:
            continue
        bets += 1
        if mult >= cashout:
            bank += bet * (cashout - 1.0)
            wins += 1
        else:
            bank -= bet
        if bank <= 0.01:
            break
        if bank > peak: peak = bank
        dd = (peak - bank) / peak if peak > 0 else 0
        if dd > mxdd: mxdd = dd
        if i % 500 == 0: equity_curve.append(bank)

    wr = wins / bets * 100 if bets > 0 else 0
    return {"roi": (bank - START) / START * 100, "pnl": bank - START,
            "mxdd": mxdd * 100, "bets": bets, "wr": wr, "bank": bank}


# ─────────────────────────────────────────────────────────────────────────────
#  PROPOSAL 1: sniper_v2
#  Fix: sniper uses 3.0x cashout — data shows 3.0x is in COLD 133h/176h.
#  Switch to 2.0x, keep no_x10_last5 filter. Add optimal cooldown.
# ─────────────────────────────────────────────────────────────────────────────

STRATEGIES["sniper_v2"] = StrategyConfig(
    name="sniper_v2",
    bet_pct=0.00015, cashout=2.0, stop_loss=0.15, stop_profit=0.40,
    session_frac=0.10, max_rounds=200, consec_sl_pause=3, bet_hard_cap=0.0005,
    filter="no_x10_last5",
    consec_loss_trigger=3, consec_loss_pause=11,  # 3 losses -> 5 min (opt for 2.0x)
)


# ─────────────────────────────────────────────────────────────────────────────
#  PROPOSAL 2: surfer
#  Insight: HOT/COLD are symmetric at ~5 min. Use a 5-round detector.
#  - Phase NEUTRAL or HOT: bet at 1.5x (high win rate 65.9%)
#  - Phase VERY HOT (>=4/5 last rounds hit 2.0x): escalate to 2.0x
#  - Phase COLD (>=4/5 last rounds miss 1.5x): skip round
#  Flat 0.1% base bet. No martingale, no sessions.
# ─────────────────────────────────────────────────────────────────────────────

BASE_BET_SURFER = START * 0.001   # 0.1% of starting bank

def surfer(i, mults):
    if i < 5:
        return BASE_BET_SURFER, 1.5
    window5 = mults[i-5:i]
    hits_15 = sum(1 for m in window5 if m >= 1.5)  # wins at 1.5x
    hits_20 = sum(1 for m in window5 if m >= 2.0)  # wins at 2.0x
    # COLD: >=4 of last 5 miss 1.5x → skip
    if hits_15 <= 1:
        return 0, 0
    # VERY HOT: >=4 of last 5 hit 2.0x → escalate cashout
    if hits_20 >= 4:
        return BASE_BET_SURFER, 2.0
    # Normal / HOT: bet at 1.5x
    return BASE_BET_SURFER, 1.5


# ─────────────────────────────────────────────────────────────────────────────
#  PROPOSAL 3: harvester
#  Insight: 1.5x cashout is HOT 99% of the time (192h vs 4h COLD).
#  Strategy: bet small at 1.5x continuously, skip ONLY strongly COLD (rare).
#  Use thermal window 5, threshold -3 (stricter than waveskip's -4 on window 10).
#  Flat 0.05% bet — tiny but almost never paused.
# ─────────────────────────────────────────────────────────────────────────────

BASE_BET_HARV = START * 0.0005   # 0.05%

def harvester(i, mults):
    if i < 5:
        return BASE_BET_HARV, 1.5
    window5 = mults[i-5:i]
    # Thermal score at 1.5x
    score = sum(1 if m >= 1.5 else -1 for m in window5)
    if score <= -3:   # strongly cold — skip (very rare at 1.5x)
        return 0, 0
    return BASE_BET_HARV, 1.5


# ─────────────────────────────────────────────────────────────────────────────
#  PROPOSAL 4: phase_trader
#  Insight: HOT/COLD last 5-15 min. Detect with 8-round window.
#  - Score = sum(+1 win, -1 loss) over last 8 rounds at 2.0x
#  - Score >=  4 (HOT confirmed): bet at 2.0x, larger size
#  - Score <= -4 (COLD confirmed): skip
#  - Score in between (neutral): bet at 1.5x, small size
# ─────────────────────────────────────────────────────────────────────────────

BASE_BET_PT_HOT  = START * 0.002   # 0.2% during HOT
BASE_BET_PT_NEUT = START * 0.001   # 0.1% neutral

def phase_trader(i, mults):
    if i < 8:
        return BASE_BET_PT_NEUT, 1.5
    window8 = mults[i-8:i]
    score = sum(1 if m >= 2.0 else -1 for m in window8)
    if score <= -4:
        return 0, 0              # COLD: skip
    if score >= 4:
        return BASE_BET_PT_HOT, 2.0   # HOT: bet big at 2.0x
    return BASE_BET_PT_NEUT, 1.5     # NEUTRAL: bet small at 1.5x


# ─────────────────────────────────────────────────────────────────────────────
#  RUN ALL
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*90}")
    print(f"  STRATEGY ANALYSIS + PROPOSALS  --  {N:,} rounds  --  100 SOL start")
    print(f"{'='*90}")

    # -- Existing strategies --------------------------------------------------
    print(f"\n  {'--- EXISTING (with optimal cooldown) ---':^86}")
    print(f"  {'Strategy':<18}  {'ROI':>8}  {'P&L SOL':>9}  {'WinRate':>8}  {'MaxDD':>7}  {'Bets':>6}  {'Issue'}")
    print(f"  {'-'*86}")

    issues = {
        "compound":       "flat, house edge drains",
        "turtle":         "best existing (+1.02%)",
        "scalper":        "flat, short sessions",
        "sniper":         "3.0x = COLD 75% of time",
        "antimartingale": "scale resets on loss",
        "x5":             "5.0x = COLD 99% of time",
        "martingale":     "scale grows dangerously",
        "waveskip":       "window too large, misses HOT",
    }

    for name in [n for n in STRATEGIES if not n.startswith("sniper_v2")]:
        try:
            r = run_std(name)
            print(f"  {name:<18}  {r['roi']:>+7.2f}%  {r['pnl']:>+8.4f}  "
                  f"{r['wr']:>7.1f}%  {r['mxdd']:>6.1f}%  {r['bets']:>6}"
                  f"  {issues.get(name,'')}")
        except Exception as e:
            print(f"  {name:<18}  ERROR: {e}")

    # -- Proposals ------------------------------------------------------------
    print(f"\n  {'--- NEW PROPOSALS ---':^86}")
    print(f"  {'Strategy':<18}  {'ROI':>8}  {'P&L SOL':>9}  {'WinRate':>8}  {'MaxDD':>7}  {'Bets':>6}  {'Idea'}")
    print(f"  {'-'*86}")

    proposals = [
        ("sniper_v2",    run_std,              "sniper with 2.0x cashout (fix 3.0x COLD bias)"),
        ("surfer",       lambda: run_custom(surfer,       "surfer"),       "adaptive: 1.5x normal / 2.0x HOT / skip COLD"),
        ("harvester",    lambda: run_custom(harvester,    "harvester"),    "pure 1.5x, skip only strongly COLD (rare)"),
        ("phase_trader", lambda: run_custom(phase_trader, "phase_trader"), "phase detect: 1.5x neutral / 2.0x HOT / skip COLD"),
    ]

    for name, fn, idea in proposals:
        try:
            r = fn("sniper_v2") if name == "sniper_v2" else fn()
            print(f"  {name:<18}  {r['roi']:>+7.2f}%  {r['pnl']:>+8.4f}  "
                  f"{r['wr']:>7.1f}%  {r['mxdd']:>6.1f}%  {r['bets']:>6}  {idea}")
        except Exception as e:
            print(f"  {name:<18}  ERROR: {e}")

    print(f"\n{'='*90}")

    # -- Summary insights -----------------------------------------------------
    print("""
  KEY INSIGHTS from streak analysis:

  1. SNIPER BUG: cashout 3.0x means 75% of time in COLD zone (win rate 33.4%).
     sniper_v2 switches to 2.0x and keeps the no_x10_last5 filter.

  2. SURFER exploits phase symmetry: detect HOT (>=4/5 at 2.0x) -> escalate.
     Detect COLD (<=1/5 at 1.5x) -> skip. Otherwise ride 1.5x HOT dominance.

  3. HARVESTER: 1.5x is HOT 99% of the time (192h vs 4h COLD).
     Tiny flat bet + skip only strong COLD. Pure volume play.

  4. PHASE_TRADER: 8-round window for cleaner HOT/COLD signal.
     Bigger bet (0.2%) only when HOT confirmed, 0.1% neutral, skip COLD.

  5. x5 WARNING: 5.0x cashout is in COLD 99% of the time.
     The 20% win rate means ~4 in 5 rounds lose. Only profitable during
     rare HOT spikes. Consider replacing with x5_v2 at 3.0x or 2.5x.
""")


if __name__ == "__main__":
    main()
