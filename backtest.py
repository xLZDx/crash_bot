"""
Backtest all strategies on historical crash.duckdb rounds.
Uses the same decide() / calc_bet() logic as the live bots.

Cooldown modes (can combine):
  --cooldown N        : skip N rounds after EVERY individual loss
  --consec N --pause M: skip M rounds after N consecutive losses (reset scale)

Round timing: avg 28.5s/round  =>  10 min = 21 rounds, 30 min = 63 rounds
"""
import sys
import argparse
import duckdb
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot_strategy import STRATEGIES, decide, new_session_bank

DB = "data/crash.duckdb"
START_BANK = 100.0


def run(name: str, loss_cooldown: int = 0,
        consec_trigger: int = 0, consec_pause: int = 0) -> dict:
    cfg = STRATEGIES[name]

    conn = duckdb.connect(DB, read_only=True)
    rows = conn.execute("SELECT multiplier FROM rounds ORDER BY id ASC").fetchall()
    conn.close()
    mults = [r[0] for r in rows]

    bank          = START_BANK
    session_bank  = new_session_bank(bank, cfg)
    session_start = session_bank
    session_rounds = 0
    consec_losing  = 0   # losing SESSIONS (for cfg.consec_sl_pause)
    current_scale  = 1.0
    cooldown_left  = 0   # rounds to skip
    consec_bet_losses = 0  # consecutive individual bet losses
    sticky_recovery_wins = 0

    total_bets = total_wins = total_sessions = 0
    peak = bank
    max_dd = 0.0

    for i, mult in enumerate(mults):
        if cooldown_left > 0:
            cooldown_left -= 1
            session_rounds += 1
            continue

        if cfg.filter in ("pattern_allow", "pattern_veto"):
            max_pat = max([len(p) for p in (cfg.allow_patterns + cfg.veto_patterns)] or [8])
            recent = mults[max(0, i - max(8, max_pat)):i]
        elif cfg.filter == "numeric_pattern":
            limit = max(8, len(cfg.numeric_pattern) + max(0, cfg.numeric_delay - 1))
            recent = mults[max(0, i - limit):i]
        elif cfg.filter == "wave_regime":
            limit = max(8, cfg.wave_low_len + cfg.wave_high_len + max(0, cfg.wave_delay - 1))
            recent = mults[max(0, i - limit):i]
        elif cfg.filter in ("relative_wave", "relative_bad_veto"):
            limit = max(
                8,
                cfg.rel_base_len + cfg.rel_low_len + cfg.rel_high_len + max(0, cfg.rel_delay - 1),
            )
            recent = mults[max(0, i - limit):i]
        elif cfg.filter == "elliott_impulse5":
            limit = max(8, cfg.elliott_base_len + 4 * cfg.elliott_sub_len + max(0, cfg.elliott_delay - 1))
            recent = mults[max(0, i - limit):i]
        elif cfg.filter == "cold_breakout_wave":
            limit = max(
                8,
                cfg.cold_wave_len * 2 + max(0, cfg.cold_wave_max_gap) + max(0, cfg.cold_wave_delay - 1),
            )
            recent = mults[max(0, i - limit):i]
        else:
            recent = mults[max(0, i - max(cfg.thermal_window, 5)):i]

        decision = decide(
            session_bank=session_bank,
            session_start=session_start,
            total_bank=bank,
            session_rounds=session_rounds,
            consec_losing=consec_losing,
            cfg=cfg,
            current_scale=current_scale,
            recent_multipliers=recent,
        )

        action = decision["action"]

        if action == "end_session":
            pnl = session_bank - session_start
            consec_losing = (consec_losing + 1) if pnl < 0 else 0
            bank += pnl
            total_sessions += 1
            session_bank  = new_session_bank(bank, cfg)
            session_start = session_bank
            session_rounds = 0
            current_scale = 1.0
            consec_bet_losses = 0
            sticky_recovery_wins = 0
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
                total_wins += 1
                consec_bet_losses = 0
                if cfg.loss_scale > 1.0:
                    if cfg.sticky_win_reset_after > 0 and current_scale > 1.0:
                        sticky_recovery_wins += 1
                        if sticky_recovery_wins >= cfg.sticky_win_reset_after:
                            current_scale = 1.0
                            sticky_recovery_wins = 0
                    else:
                        current_scale = 1.0
                        sticky_recovery_wins = 0
                elif cfg.scaling > 1.0:
                    current_scale = min(current_scale * cfg.scaling, cfg.max_scale)
            else:
                session_bank -= bet
                consec_bet_losses += 1
                if cfg.loss_scale > 1.0:
                    current_scale = min(current_scale * cfg.loss_scale, cfg.max_scale)
                    sticky_recovery_wins = 0
                elif cfg.scaling > 1.0:
                    current_scale = 1.0

                # per-loss cooldown
                if loss_cooldown > 0:
                    cooldown_left = loss_cooldown

                # consecutive-loss cooldown: N losses in a row -> pause + reset scale
                if consec_trigger > 0 and consec_bet_losses >= consec_trigger:
                    cooldown_left = max(cooldown_left, consec_pause)
                    current_scale = 1.0       # reset martingale scale
                    consec_bet_losses = 0
                    sticky_recovery_wins = 0

        if bank > peak:
            peak = bank
        dd = (peak - bank) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    bank += (session_bank - session_start)

    win_rate = (total_wins / total_bets * 100) if total_bets > 0 else 0.0
    roi = (bank - START_BANK) / START_BANK * 100

    return {
        "name":     name,
        "final":    bank,
        "pnl":      bank - START_BANK,
        "roi_pct":  roi,
        "win_rate": win_rate,
        "bets":     total_bets,
        "sessions": total_sessions,
        "max_dd":   max_dd * 100,
    }


def print_table(results: list, label: str):
    print()
    print("=" * 86)
    print(f"  {label}")
    print("=" * 86)
    print(f"  {'Strategy':<16}  {'Final SOL':>10}  {'P&L SOL':>9}  {'ROI':>7}  "
          f"{'WinRate':>8}  {'Bets':>6}  {'Sessions':>8}  {'MaxDD':>7}")
    print(f"  {'-'*82}")
    for r in results:
        if "error" in r:
            print(f"  {r['name']:<16}  ERROR: {r['error']}")
            continue
        sign = "+" if r["pnl"] >= 0 else ""
        bust = "  <-- BUST" if r["final"] < 1.0 else ""
        print(
            f"  {r['name']:<16}"
            f"  {r['final']:>10.4f}"
            f"  {sign}{r['pnl']:>8.4f}"
            f"  {sign}{r['roi_pct']:>6.2f}%"
            f"  {r['win_rate']:>7.1f}%"
            f"  {r['bets']:>6}"
            f"  {r['sessions']:>8}"
            f"  {r['max_dd']:>6.1f}%"
            + bust
        )
    print("=" * 86)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cooldown",  type=int, default=0,
                        help="Rounds to skip after EVERY loss")
    parser.add_argument("--consec",    type=int, default=0,
                        help="Trigger pause after N consecutive losses")
    parser.add_argument("--pause",     type=int, default=21,
                        help="Rounds to pause after N consec losses (default 21 = 10 min)")
    parser.add_argument("--compare",   action="store_true",
                        help="Show baseline vs configured side by side")
    args = parser.parse_args()

    names = list(STRATEGIES.keys())

    if args.compare:
        base, cd = [], []
        for name in names:
            try:
                base.append(run(name, 0, 0, 0))
                cd.append(run(name, args.cooldown, args.consec, args.pause))
            except Exception as e:
                base.append({"name": name, "error": str(e)})
                cd.append({"name": name, "error": str(e)})

        base.sort(key=lambda r: r.get("pnl", float("-inf")), reverse=True)
        cd.sort(  key=lambda r: r.get("pnl", float("-inf")), reverse=True)

        print_table(base, "BASELINE  --  no cooldown")

        if args.consec:
            label = (f"AFTER {args.consec} CONSEC LOSSES -> pause {args.pause} rounds "
                     f"(~{args.pause*28.5/60:.0f} min) + scale reset")
        else:
            label = f"COOLDOWN {args.cooldown} rounds after every loss"
        print_table(cd, label)

        # delta
        base_map = {r["name"]: r for r in base if "error" not in r}
        cd_map   = {r["name"]: r for r in cd   if "error" not in r}
        deltas = sorted(
            [(n, base_map[n]["roi_pct"], cd_map[n]["roi_pct"],
              cd_map[n]["roi_pct"] - base_map[n]["roi_pct"])
             for n in names if n in base_map and n in cd_map],
            key=lambda x: x[3], reverse=True
        )
        print()
        print("=" * 62)
        print("  DELTA  (with cooldown  vs  baseline)")
        print("=" * 62)
        print(f"  {'Strategy':<16}  {'Baseline':>9}  {'With CD':>9}  {'Delta':>8}")
        print(f"  {'-'*58}")
        for name, b, c, d in deltas:
            bust = "  BUST" if cd_map[name]["final"] < 1.0 else ""
            print(f"  {name:<16}  {b:>+8.2f}%  {c:>+8.2f}%  {d:>+7.2f}%{bust}")
        print("=" * 62)
    else:
        results = []
        for name in names:
            try:
                results.append(run(name, args.cooldown, args.consec, args.pause))
            except Exception as e:
                results.append({"name": name, "error": str(e)})
        results.sort(key=lambda r: r.get("pnl", float("-inf")), reverse=True)
        if args.consec:
            label = (f"CONSEC {args.consec} LOSSES -> pause {args.pause}r "
                     f"(~{args.pause*28.5/60:.0f} min)")
        else:
            label = f"COOLDOWN {args.cooldown}r after every loss" if args.cooldown else "BASELINE"
        print_table(results, label)


if __name__ == "__main__":
    main()
