"""
Show before vs after: individual optimal cooldowns on 22k rounds.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import duckdb
from bot_strategy import STRATEGIES, decide, new_session_bank

DB = "data/crash.duckdb"
START = 100.0

conn = duckdb.connect(DB, read_only=True)
MULTS = [r[0] for r in conn.execute("SELECT multiplier FROM rounds ORDER BY id ASC").fetchall()]
conn.close()


def run(name, use_cooldown):
    cfg = STRATEGIES[name]
    bank = START
    session_bank  = new_session_bank(bank, cfg)
    session_start = session_bank
    session_rounds = 0
    consec_losing  = 0
    current_scale  = 1.0
    cooldown_left  = 0
    consec_bet_losses = 0
    total_bets = total_wins = 0
    peak = bank
    max_dd = 0.0

    trigger = cfg.consec_loss_trigger if use_cooldown else 0
    pause   = cfg.consec_loss_pause   if use_cooldown else 0

    for i, mult in enumerate(MULTS):
        if cooldown_left > 0:
            cooldown_left -= 1
            session_rounds += 1
            continue

        recent = MULTS[max(0, i - max(cfg.thermal_window, 5)):i]
        dec = decide(
            session_bank=session_bank, session_start=session_start,
            total_bank=bank, session_rounds=session_rounds,
            consec_losing=consec_losing, cfg=cfg,
            current_scale=current_scale, recent_multipliers=recent,
        )
        action = dec["action"]

        if action == "end_session":
            pnl = session_bank - session_start
            consec_losing = (consec_losing + 1) if pnl < 0 else 0
            bank += pnl
            session_bank  = new_session_bank(bank, cfg)
            session_start = session_bank
            session_rounds = 0
            current_scale = 1.0
            consec_bet_losses = 0
            if bank <= 0.01:
                break
        elif action in ("paused", "no_bet", "no_funds"):
            session_rounds += 1
        elif action == "bet":
            bet = dec["bet_sol"]
            session_rounds += 1
            total_bets += 1
            if mult >= dec["cashout"]:
                session_bank += bet * (dec["cashout"] - 1.0)
                total_wins   += 1
                consec_bet_losses = 0
                if cfg.loss_scale > 1.0:  current_scale = 1.0
                elif cfg.scaling > 1.0:   current_scale = min(current_scale * cfg.scaling, cfg.max_scale)
            else:
                session_bank -= bet
                consec_bet_losses += 1
                if cfg.loss_scale > 1.0:  current_scale = min(current_scale * cfg.loss_scale, cfg.max_scale)
                elif cfg.scaling > 1.0:   current_scale = 1.0
                if trigger > 0 and consec_bet_losses >= trigger:
                    cooldown_left     = pause
                    current_scale     = 1.0
                    consec_bet_losses = 0

        if bank > peak:
            peak = bank
        dd = (peak - bank) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    bank += (session_bank - session_start)
    wr = total_wins / total_bets * 100 if total_bets > 0 else 0.0
    return bank - START, (bank - START) / START * 100, max_dd * 100, total_bets, wr


NAMES = [n for n in STRATEGIES if n != "turbo"]

print()
print("=" * 96)
print(f"  BEFORE vs AFTER -- individual optimal cooldowns -- {len(MULTS):,} rounds -- 100 SOL start")
print("=" * 96)
print(f"  {'Strategy':<16}  {'Before ROI':>10}  {'After ROI':>10}  {'Delta':>8}"
      f"  {'MaxDD before':>12}  {'MaxDD after':>11}  {'Cooldown':>20}")
print(f"  {'-'*92}")

total_before = total_after = 0.0
for name in NAMES:
    b_pnl, b_roi, b_dd, b_bets, b_wr = run(name, False)
    a_pnl, a_roi, a_dd, a_bets, a_wr = run(name, True)
    d = a_roi - b_roi
    total_before += b_roi
    total_after  += a_roi
    cfg = STRATEGIES[name]
    if cfg.consec_loss_trigger > 0:
        mins = cfg.consec_loss_pause * 28.5 / 60
        cd_str = f"{cfg.consec_loss_trigger} loss -> {mins:.0f}min"
    else:
        cd_str = "disabled"
    arrow = "  ++" if d > 0.1 else ("  + " if d > 0 else "  --")
    print(
        f"  {name:<16}"
        f"  {b_roi:>+9.2f}%"
        f"  {a_roi:>+9.2f}%"
        f"  {d:>+7.2f}%"
        f"  {b_dd:>11.1f}%"
        f"  {a_dd:>10.1f}%"
        f"  {cd_str:>20}"
        f"{arrow}"
    )

print(f"  {'-'*92}")
print(f"  {'TOTAL':<16}  {total_before:>+9.2f}%  {total_after:>+9.2f}%  {total_after-total_before:>+7.2f}%")
print("=" * 96)
