"""
Test + optimize 3 new strategies via grid search on 22k rounds.
Uses full session management (same engine as live bots).
"""
import sys
import duckdb
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot_strategy import STRATEGIES, StrategyConfig, decide, new_session_bank

DB    = "data/crash.duckdb"
START = 100.0

conn = duckdb.connect(DB, read_only=True)
MULTS = [r[0] for r in conn.execute("SELECT multiplier FROM rounds ORDER BY id ASC").fetchall()]
conn.close()

# ── Register new strategies ───────────────────────────────────────────────────

# 1. sniper_v2: fix cashout 3.0x -> 2.0x, keep no_x10_last5 filter
STRATEGIES["sniper_v2"] = StrategyConfig(
    name="sniper_v2",
    bet_pct=0.00015, cashout=2.0, stop_loss=0.15, stop_profit=0.40,
    session_frac=0.10, max_rounds=200, consec_sl_pause=3, bet_hard_cap=0.0005,
    filter="no_x10_last5",
)

# 2. turtle_micro: half the bet size of turtle, same session mgmt
STRATEGIES["turtle_micro"] = StrategyConfig(
    name="turtle_micro",
    bet_pct=0.000025, cashout=2.1, stop_loss=0.40, stop_profit=1.0,
    session_frac=1.0, max_rounds=9999, consec_sl_pause=99, bet_hard_cap=0.00025,
)

# 3. tide: phase-aware, bets 3x bigger on confirmed HOT, skips on COLD
#    Uses thermal_skip filter (score < -2 = skip) with small window
#    Bigger bet handled via scaling=3.0 when HOT -- approximated via
#    thermal_threshold=-2 to skip cold + larger base bet
STRATEGIES["tide"] = StrategyConfig(
    name="tide",
    bet_pct=0.00008, cashout=2.0, stop_loss=0.15, stop_profit=0.35,
    session_frac=0.15, max_rounds=300, consec_sl_pause=3, bet_hard_cap=0.0006,
    filter="thermal_skip", thermal_window=5, thermal_threshold=-2,
)

CONSEC_VALUES = [1, 2, 3, 4, 5]
PAUSE_VALUES  = [2, 4, 6, 9, 11, 17, 21, 32, 42, 63]
NEW_NAMES     = ["sniper_v2", "turtle_micro", "tide"]


def run(name: str, consec: int, pause: int) -> float:
    cfg = STRATEGIES[name]
    bank = START
    sb   = new_session_bank(bank, cfg)
    ss   = sb
    sr   = 0
    cl   = 0
    sc   = 1.0
    cdl  = 0
    cbl  = 0

    for i, mult in enumerate(MULTS):
        if cdl > 0:
            cdl -= 1; sr += 1; continue
        recent = MULTS[max(0, i - max(cfg.thermal_window, 5)):i]
        dec = decide(sb, ss, bank, sr, cl, cfg, sc, recent)
        act = dec["action"]
        if act == "end_session":
            pnl = sb - ss
            cl = (cl + 1) if pnl < 0 else 0
            bank += pnl
            sb = new_session_bank(bank, cfg); ss = sb; sr = 0; sc = 1.0; cbl = 0
            if bank <= 0.01: break
        elif act in ("paused", "no_bet", "no_funds"):
            sr += 1
        elif act == "bet":
            bet = dec["bet_sol"]; sr += 1
            if mult >= dec["cashout"]:
                sb += bet * (dec["cashout"] - 1.0); cbl = 0
                if cfg.loss_scale > 1.0: sc = 1.0
                elif cfg.scaling > 1.0: sc = min(sc * cfg.scaling, cfg.max_scale)
            else:
                sb -= bet; cbl += 1
                if cfg.loss_scale > 1.0: sc = min(sc * cfg.loss_scale, cfg.max_scale)
                elif cfg.scaling > 1.0: sc = 1.0
                if consec > 0 and cbl >= consec:
                    cdl = pause; sc = 1.0; cbl = 0
        if bank > START * 10: break  # safety cap
    bank += (sb - ss)
    return (bank - START) / START * 100


def baseline(name): return run(name, 0, 0)


def full_stats(name, consec, pause):
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

    for i, mult in enumerate(MULTS):
        if cdl > 0:
            cdl -= 1; sr += 1; continue
        recent = MULTS[max(0, i - max(cfg.thermal_window, 5)):i]
        dec = decide(sb, ss, bank, sr, cl, cfg, sc, recent)
        act = dec["action"]
        if act == "end_session":
            pnl = sb - ss
            cl = (cl + 1) if pnl < 0 else 0
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
                elif cfg.scaling > 1.0: sc = min(sc * cfg.scaling, cfg.max_scale)
            else:
                sb -= bet; cbl += 1
                if cfg.loss_scale > 1.0: sc = min(sc * cfg.loss_scale, cfg.max_scale)
                elif cfg.scaling > 1.0: sc = 1.0
                if consec > 0 and cbl >= consec:
                    cdl = pause; sc = 1.0; cbl = 0
        if bank > peak: peak = bank
        dd = (peak - bank) / peak if peak > 0 else 0
        if dd > mxdd: mxdd = dd
        if bank > START * 10: break
    bank += (sb - ss)
    wr = wins / bets * 100 if bets > 0 else 0
    return {
        "roi": (bank - START) / START * 100,
        "pnl": bank - START,
        "wr": wr, "bets": bets,
        "mxdd": mxdd * 100,
    }


def main():
    print(f"\n{'='*80}")
    print(f"  NEW STRATEGY OPTIMIZATION  --  {len(MULTS):,} rounds  --  100 SOL")
    print(f"{'='*80}")

    for name in NEW_NAMES:
        base = baseline(name)
        best_roi   = base
        best_combo = (0, 0)
        grid = {}
        for c in CONSEC_VALUES:
            for p in PAUSE_VALUES:
                roi = run(name, c, p)
                grid[(c, p)] = roi
                if roi > best_roi:
                    best_roi   = roi
                    best_combo = (c, p)

        bc, bp = best_combo
        mins   = bp * 28.5 / 60
        delta  = best_roi - base

        print(f"\n  {name.upper()}")
        print(f"  Baseline (no cooldown): {base:+.2f}%")
        print(f"  Best combo: {bc} losses -> {bp} rounds (~{mins:.0f} min)  =>  {best_roi:+.2f}%  (delta {delta:+.2f}%)")

        # Top 5 combos
        ranked = sorted(grid.items(), key=lambda x: x[1], reverse=True)[:5]
        print(f"  Top 5:")
        for (c, p), roi in ranked:
            m = p * 28.5 / 60
            print(f"    consec={c} pause={p}r (~{m:.0f}min)  =>  {roi:+.2f}%")

        # Full stats for best combo
        s = full_stats(name, bc, bp)
        print(f"  Final stats: ROI={s['roi']:+.2f}%  WinRate={s['wr']:.1f}%  "
              f"Bets={s['bets']}  MaxDD={s['mxdd']:.1f}%")

    # ── Comparison table vs existing best ─────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  FINAL COMPARISON (new vs existing top performers)")
    print(f"{'='*80}")
    print(f"  {'Strategy':<16}  {'ROI':>8}  {'WinRate':>8}  {'MaxDD':>7}  {'Bets':>6}  {'Note'}")
    print(f"  {'-'*76}")

    # Existing top with their optimal cooldowns
    existing = {
        "turtle":   (4, 11),
        "martingale": (4, 4),
        "antimartingale": (5, 17),
        "compound": (3, 4),
        "scalper":  (3, 4),
    }
    for name, (c, p) in existing.items():
        s = full_stats(name, c, p)
        print(f"  {name:<16}  {s['roi']:>+7.2f}%  {s['wr']:>7.1f}%  "
              f"{s['mxdd']:>6.1f}%  {s['bets']:>6}  existing")

    # New strategies with best combos
    new_combos = {}
    for name in NEW_NAMES:
        base = baseline(name)
        best_roi = base; best_combo = (0, 0)
        for c in CONSEC_VALUES:
            for p in PAUSE_VALUES:
                roi = run(name, c, p)
                if roi > best_roi:
                    best_roi = roi; best_combo = (c, p)
        new_combos[name] = best_combo
        s = full_stats(name, *best_combo)
        bc, bp = best_combo
        print(f"  {name:<16}  {s['roi']:>+7.2f}%  {s['wr']:>7.1f}%  "
              f"{s['mxdd']:>6.1f}%  {s['bets']:>6}  NEW (cd={bc}->{bp}r)")

    print(f"{'='*80}")
    print(f"\n  Optimal cooldowns for deployment:")
    for name in NEW_NAMES:
        bc, bp = new_combos[name]
        print(f"    {name}: consec_loss_trigger={bc}, consec_loss_pause={bp} (~{bp*28.5/60:.0f}min)")
    print()


if __name__ == "__main__":
    main()
