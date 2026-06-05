"""
Grid search: find optimal (consec_trigger, pause_rounds) for each strategy.
Tests all combinations and finds the global optimum.
"""
import sys
import duckdb
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot_strategy import STRATEGIES, decide, new_session_bank

DB = "data/crash.duckdb"
START_BANK = 100.0

# Load rounds once
conn = duckdb.connect(DB, read_only=True)
MULTS = [r[0] for r in conn.execute("SELECT multiplier FROM rounds ORDER BY id ASC").fetchall()]
conn.close()
N_ROUNDS = len(MULTS)

# Grid to search
# consec: how many consecutive losses trigger the pause
# pause:  how many rounds to skip (28.5s each)
CONSEC_VALUES = [1, 2, 3, 4, 5]
PAUSE_VALUES  = [2, 4, 6, 9, 11, 17, 21, 32, 42, 63]  # ~1/2/3/4.5/5/8/10/15/20/30 min


def run(name: str, consec: int, pause: int) -> float:
    cfg = STRATEGIES[name]
    bank          = START_BANK
    session_bank  = new_session_bank(bank, cfg)
    session_start = session_bank
    session_rounds = 0
    consec_losing  = 0
    current_scale  = 1.0
    cooldown_left  = 0
    consec_bet_losses = 0
    total_bets = total_wins = 0

    for i, mult in enumerate(MULTS):
        if cooldown_left > 0:
            cooldown_left -= 1
            session_rounds += 1
            continue

        recent = MULTS[max(0, i - max(cfg.thermal_window, 5)):i]
        decision = decide(
            session_bank=session_bank, session_start=session_start,
            total_bank=bank, session_rounds=session_rounds,
            consec_losing=consec_losing, cfg=cfg,
            current_scale=current_scale, recent_multipliers=recent,
        )
        action = decision["action"]

        if action == "end_session":
            pnl = session_bank - session_start
            consec_losing = (consec_losing + 1) if pnl < 0 else 0
            bank += pnl
            session_bank  = new_session_bank(bank, cfg)
            session_start = session_bank
            session_rounds = 0
            current_scale  = 1.0
            consec_bet_losses = 0
            if bank <= 0.01:
                break
        elif action in ("paused", "no_bet", "no_funds"):
            session_rounds += 1
        elif action == "bet":
            bet     = decision["bet_sol"]
            cashout = decision["cashout"]
            session_rounds += 1
            total_bets += 1
            if mult >= cashout:
                session_bank += bet * (cashout - 1.0)
                total_wins   += 1
                consec_bet_losses = 0
                if cfg.loss_scale > 1.0:   current_scale = 1.0
                elif cfg.scaling > 1.0:    current_scale = min(current_scale * cfg.scaling, cfg.max_scale)
            else:
                session_bank -= bet
                consec_bet_losses += 1
                if cfg.loss_scale > 1.0:   current_scale = min(current_scale * cfg.loss_scale, cfg.max_scale)
                elif cfg.scaling > 1.0:    current_scale = 1.0
                if consec >= 1 and consec_bet_losses >= consec:
                    cooldown_left     = pause
                    current_scale     = 1.0
                    consec_bet_losses = 0

    bank += (session_bank - session_start)
    return (bank - START_BANK) / START_BANK * 100  # ROI %


def baseline(name: str) -> float:
    return run(name, consec=0, pause=0)


def main():
    # Strategies to optimize (skip turbo variants — martingale kills them all)
    NAMES = [n for n in STRATEGIES if not n.startswith("turbo")]

    print(f"\nGrid search over {len(CONSEC_VALUES)} × {len(PAUSE_VALUES)} = "
          f"{len(CONSEC_VALUES)*len(PAUSE_VALUES)} combinations × {len(NAMES)} strategies")
    print(f"Historical data: {N_ROUNDS:,} rounds  |  avg 28.5s/round\n")

    # Baselines
    bases = {n: baseline(n) for n in NAMES}

    # Per-strategy best
    per_strategy = {}
    for name in NAMES:
        best_roi   = bases[name]
        best_combo = (0, 0)
        grid = {}
        for c in CONSEC_VALUES:
            for p in PAUSE_VALUES:
                roi = run(name, c, p)
                grid[(c, p)] = roi
                if roi > best_roi:
                    best_roi   = roi
                    best_combo = (c, p)
        per_strategy[name] = {
            "base":        bases[name],
            "best_roi":    best_roi,
            "best_combo":  best_combo,
            "grid":        grid,
        }

    # ── Per-strategy results ──────────────────────────────────────────────────
    print("=" * 72)
    print("  PER-STRATEGY OPTIMUM")
    print("=" * 72)
    print(f"  {'Strategy':<16}  {'Baseline':>9}  {'Best ROI':>9}  {'Delta':>7}  {'Consec':>6}  {'Pause':>5}  {'~Min':>5}")
    print(f"  {'-'*68}")
    for name in NAMES:
        d   = per_strategy[name]
        c, p = d["best_combo"]
        delta = d["best_roi"] - d["base"]
        mins  = p * 28.5 / 60
        print(f"  {name:<16}  {d['base']:>+8.2f}%  {d['best_roi']:>+8.2f}%  "
              f"{delta:>+6.2f}%  {c:>6}  {p:>5}  {mins:>4.0f}m")
    print("=" * 72)

    # ── Find best SINGLE combo across ALL strategies ──────────────────────────
    print("\n  Searching best single (consec, pause) across all strategies...")
    combo_scores = {}
    for c in CONSEC_VALUES:
        for p in PAUSE_VALUES:
            # score = sum of ROI improvements vs baseline, weighted equally
            total_delta = sum(
                per_strategy[n]["grid"].get((c, p), bases[n]) - bases[n]
                for n in NAMES
            )
            # also penalize if any strategy gets worse by >0.5%
            worst_delta = min(
                per_strategy[n]["grid"].get((c, p), bases[n]) - bases[n]
                for n in NAMES
            )
            combo_scores[(c, p)] = (total_delta, worst_delta)

    # sort by total_delta desc, then worst_delta desc
    ranked = sorted(combo_scores.items(),
                    key=lambda x: (x[1][0], x[1][1]), reverse=True)

    print("\n" + "=" * 72)
    print("  TOP 10 GLOBAL COMBOS  (best across ALL strategies)")
    print("=" * 72)
    print(f"  {'Consec':>6}  {'Pause':>5}  {'~Min':>5}  {'Total delta':>12}  {'Worst delta':>12}")
    print(f"  {'-'*68}")
    for (c, p), (total, worst) in ranked[:10]:
        mins = p * 28.5 / 60
        print(f"  {c:>6}  {p:>5}  {mins:>4.0f}m  {total:>+11.2f}%  {worst:>+11.2f}%")
    print("=" * 72)

    # ── Show full breakdown for the top global combo ──────────────────────────
    best_combo, (best_total, best_worst) = ranked[0]
    bc, bp = best_combo
    print(f"\n  Best global: consec={bc}, pause={bp} (~{bp*28.5/60:.0f} min)")
    print(f"  Total improvement: {best_total:+.2f}%  |  Worst single strategy: {best_worst:+.2f}%\n")
    print("=" * 72)
    print(f"  BREAKDOWN  --  consec={bc}, pause={bp} (~{bp*28.5/60:.0f} min)")
    print("=" * 72)
    print(f"  {'Strategy':<16}  {'Baseline':>9}  {'With CD':>9}  {'Delta':>8}")
    print(f"  {'-'*56}")
    for name in NAMES:
        b   = bases[name]
        cd  = per_strategy[name]["grid"].get((bc, bp), b)
        print(f"  {name:<16}  {b:>+8.2f}%  {cd:>+8.2f}%  {cd-b:>+7.2f}%")
    print("=" * 72)
    print()


if __name__ == "__main__":
    main()
