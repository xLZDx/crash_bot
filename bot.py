"""
Multi-strategy paper-trading bot.
Polls the rounds DuckDB for new entries and places virtual bets.

Usage:
    python -u bot.py [--db PATH] [--bot-db PATH] [--strategy NAME]

Stop:
    echo . > data/bot_stop.flag
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

import duckdb

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot_state import BotAccount, PAUSE_RESUME_ROUNDS
from bot_strategy import decide, STRATEGIES, StrategyConfig

DB_DEFAULT     = "data/crash.duckdb"
BOT_DB_DEFAULT = "data/bot_state.duckdb"
STOP_FLAG      = "data/bot_stop.flag"
POLL_INTERVAL  = 10      # seconds between round polls
SNAP_INTERVAL  = 600     # seconds between snapshots (10 min)
STATS_INTERVAL = 3600    # seconds between hourly stat prints


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_sol(v: float, w: int = 9) -> str:
    if v == 0.0:
        return "0.0000".rjust(w)
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.4f}".rjust(w)


def _fmt_pct(v: float, w: int = 8) -> str:
    if v == 0.0:
        return "0.00%".rjust(w)
    sign = "+" if v > 0 else ""
    return f"{sign}{v*100:.2f}%".rjust(w)


def fetch_new_rounds(rounds_db: str, since_id: int) -> list[dict]:
    """Read new rounds from the collector DB (crash.duckdb)."""
    with duckdb.connect(rounds_db, read_only=True) as conn:
        rows = conn.execute("""
            SELECT id, game_round_id, multiplier
            FROM rounds
            WHERE id > ?
            ORDER BY id ASC
        """, [since_id]).fetchall()
    return [{"id": r[0], "game_round_id": r[1], "multiplier": r[2]}
            for r in rows]


def fetch_recent_multipliers(rounds_db: str, before_id: int, limit: int = 5) -> list[float]:
    """Return the last `limit` multipliers BEFORE the given round id."""
    with duckdb.connect(rounds_db, read_only=True) as conn:
        rows = conn.execute("""
            SELECT multiplier FROM rounds
            WHERE id < ?
            ORDER BY id DESC
            LIMIT ?
        """, [before_id, limit]).fetchall()
    return [r[0] for r in reversed(rows)]


def _get_max_round_id(rounds_db: str) -> int:
    with duckdb.connect(rounds_db, read_only=True) as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM rounds").fetchone()
    return row[0]


def print_stats(account: BotAccount, strategy_name: str):
    """Print bankroll + P&L last hour / last 24h / since start."""
    now    = _utcnow()
    state  = account.get_state()
    totals = account.get_totals()

    bank      = state["total_bank_sol"]
    sbank     = state["session_bank_sol"]
    sstart    = state["session_start_sol"]
    s_pct     = ((sbank - sstart) / sstart * 100) if sstart > 0 else 0.0
    s_rds     = state["session_rounds"]
    consec    = state["consec_losing_sessions"]
    sid       = state["session_id"]
    total_pnl = totals["total_pnl_sol"]
    wr        = (totals["total_wins"] / totals["total_bets"] * 100
                 if totals["total_bets"] > 0 else 0.0)

    print(f"\n{'='*58}")
    print(f"  [{strategy_name.upper()}]  {now.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"{'='*58}")
    print(f"  Bankroll : {bank:.6f} SOL   (all-time P&L: {_fmt_sol(total_pnl).strip()} SOL)")
    print(f"  Session #{sid:<4}: bank {sbank:.6f}  ({s_pct:+.2f}%,  {s_rds} rounds)")
    print(f"  Bets: {totals['total_bets']}   Win rate: {wr:.1f}%   Consec losses: {consec}")

    spans = [("1h", 3600), ("24h", 86400), ("7d", 604800)]
    col, lbl = 11, 12

    print()
    print(f"  {'':>{lbl}}", end="")
    for name, _ in spans:
        print(f"  {name:>{col}}", end="")
    print()
    print(f"  {'-'*(lbl + (col+2)*len(spans))}")

    for metric in ("P&L SOL", "P&L %", "Win rate", "Rounds"):
        print(f"  {metric:>{lbl}}", end="")
        for _, secs in spans:
            snap_ts = datetime.fromtimestamp(now.timestamp() - secs, tz=timezone.utc)
            snap    = account.get_snapshot_near(snap_ts)
            if snap is None:
                print(f"  {'n/a':>{col}}", end="")
                continue
            d_bank = bank - snap["total_bank_sol"]
            d_bets = totals["total_bets"] - snap["total_bets"]
            d_wins = totals["total_wins"] - snap["total_wins"]
            d_wr   = (d_wins / d_bets * 100) if d_bets > 0 else float("nan")
            base   = snap["total_bank_sol"]
            d_pct  = (d_bank / base) if base > 0 else 0.0
            if metric == "P&L SOL":
                print(f"  {_fmt_sol(d_bank, col)}", end="")
            elif metric == "P&L %":
                print(f"  {_fmt_pct(d_pct, col)}", end="")
            elif metric == "Win rate":
                if d_bets == 0 or d_wr != d_wr:
                    print(f"  {'n/a':>{col}}", end="")
                else:
                    print(f"  {f'{d_wr:.1f}%':>{col}}", end="")
            elif metric == "Rounds":
                print(f"  {str(d_bets):>{col}}", end="")
        print()

    print(f"{'='*58}")


def run(rounds_db: str, bot_db: str, cfg: StrategyConfig):
    account = BotAccount(bot_db, cfg=cfg)

    # First boot: skip historical rounds, start from latest live round only.
    state = account.get_state()
    if state["last_processed_round_id"] == 0 and state["session_rounds"] == 0:
        max_id = _get_max_round_id(rounds_db)
        if max_id > 0:
            account.mark_round_processed(max_id)
            print(f"[{cfg.name}] first boot -- fast-forwarding to round {max_id}")

    stop_flag = f"data/bot_stop_{cfg.name}.flag"

    print(f"[{cfg.name}] started   rounds_db={rounds_db}   bot_db={bot_db}")
    print(f"[{cfg.name}] stop:     echo . > {stop_flag}")
    if cfg.consec_loss_trigger > 0:
        mins = cfg.consec_loss_pause * 28.5 / 60
        print(f"[{cfg.name}] cooldown: {cfg.consec_loss_trigger} consec losses "
              f"-> pause {cfg.consec_loss_pause} rounds (~{mins:.0f} min) + scale reset")

    last_snap  = time.time()
    last_stats = time.time()
    print_stats(account, cfg.name)

    # In-memory cooldown state (resets on restart — acceptable for this use case)
    ctx = {"consec_bet_losses": 0, "cooldown_left": 0, "sticky_recovery_wins": 0}

    while True:
        if os.path.exists(STOP_FLAG) or os.path.exists(stop_flag):
            flag = STOP_FLAG if os.path.exists(STOP_FLAG) else stop_flag
            print(f"[{cfg.name}] stop flag -- exiting")
            try:
                os.remove(flag)
            except Exception:
                pass
            break

        state = account.get_state()
        since = state["last_processed_round_id"]

        try:
            new_rounds = fetch_new_rounds(rounds_db, since)
        except Exception as e:
            print(f"[{cfg.name}] poll error: {e}")
            time.sleep(POLL_INTERVAL)
            continue

        for r in new_rounds:
            try:
                # --- cooldown skip ---
                if ctx["cooldown_left"] > 0:
                    ctx["cooldown_left"] -= 1
                    account.mark_round_processed(r["id"])
                    continue
                _process_round(account, r, cfg, rounds_db, ctx)
            except Exception as e:
                print(f"[{cfg.name}] ERROR round {r.get('id')}: {e} -- skipping")
                try:
                    account.mark_round_processed(r["id"])
                except Exception:
                    pass

        now_t = time.time()
        if now_t - last_snap >= SNAP_INTERVAL:
            account.save_snapshot()
            last_snap = now_t

        if now_t - last_stats >= STATS_INTERVAL:
            print_stats(account, cfg.name)
            last_stats = now_t

        time.sleep(POLL_INTERVAL)


def _process_round(account: BotAccount, r: dict, cfg: StrategyConfig,
                   rounds_db: str, ctx: dict):
    state = account.get_state()

    # Fetch recent multipliers for filters that need them
    recent_mults = None
    if cfg.filter == "no_x10_last5":
        recent_mults = fetch_recent_multipliers(rounds_db, r["id"], limit=5)
    elif cfg.filter == "thermal_skip" and cfg.thermal_window > 0:
        recent_mults = fetch_recent_multipliers(rounds_db, r["id"], limit=cfg.thermal_window)
    elif cfg.filter in ("pattern_allow", "pattern_veto"):
        max_pat = max([len(p) for p in (cfg.allow_patterns + cfg.veto_patterns)] or [8])
        recent_mults = fetch_recent_multipliers(rounds_db, r["id"], limit=max(8, max_pat))
    elif cfg.filter == "numeric_pattern":
        limit = max(8, len(cfg.numeric_pattern) + max(0, cfg.numeric_delay - 1))
        recent_mults = fetch_recent_multipliers(rounds_db, r["id"], limit=limit)
    elif cfg.filter == "wave_regime":
        limit = max(8, cfg.wave_low_len + cfg.wave_high_len + max(0, cfg.wave_delay - 1))
        recent_mults = fetch_recent_multipliers(rounds_db, r["id"], limit=limit)
    elif cfg.filter in ("relative_wave", "relative_bad_veto"):
        limit = max(
            8,
            cfg.rel_base_len + cfg.rel_low_len + cfg.rel_high_len + max(0, cfg.rel_delay - 1),
        )
        recent_mults = fetch_recent_multipliers(rounds_db, r["id"], limit=limit)
    elif cfg.filter == "elliott_impulse5":
        limit = max(8, cfg.elliott_base_len + 4 * cfg.elliott_sub_len + max(0, cfg.elliott_delay - 1))
        recent_mults = fetch_recent_multipliers(rounds_db, r["id"], limit=limit)
    elif cfg.filter == "cold_breakout_wave":
        limit = max(
            8,
            cfg.cold_wave_len * 2 + max(0, cfg.cold_wave_max_gap) + max(0, cfg.cold_wave_delay - 1),
        )
        recent_mults = fetch_recent_multipliers(rounds_db, r["id"], limit=limit)

    dec = decide(
        session_bank      = state["session_bank_sol"],
        session_start     = state["session_start_sol"],
        total_bank        = state["total_bank_sol"],
        session_rounds    = state["session_rounds"],
        consec_losing     = state["consec_losing_sessions"],
        cfg               = cfg,
        current_scale     = state["current_scale"],
        recent_multipliers= recent_mults,
    )
    action = dec["action"]

    if action == "end_session":
        account.close_session(dec["reason"])
        new_sid = account.open_new_session()
        account.mark_round_processed(r["id"])
        ctx["consec_bet_losses"] = 0
        ctx["sticky_recovery_wins"] = 0
        print(f"[{cfg.name}] session closed ({dec['reason']})  new session #{new_sid}")
        return

    if action == "paused":
        skipped = account.increment_paused_skipped()
        account.mark_round_processed(r["id"])
        if skipped == 1:
            print(f"[{cfg.name}] PAUSED -- {dec['reason']}  (auto-resume in {PAUSE_RESUME_ROUNDS} rounds)")
        if skipped >= PAUSE_RESUME_ROUNDS:
            account.reset_pause()
            new_sid = account.open_new_session()
            print(f"[{cfg.name}] auto-resumed after {skipped} paused rounds -- session #{new_sid}")
        return

    if action in ("no_funds", "no_bet"):
        account.mark_round_processed(r["id"])
        return

    # action == "bet"
    won = r["multiplier"] >= dec["cashout"]

    # Scale update (martingale / anti-martingale)
    if cfg.loss_scale > 1.0:
        if won:
            if cfg.sticky_win_reset_after > 0 and state["current_scale"] > 1.0:
                ctx["sticky_recovery_wins"] = ctx.get("sticky_recovery_wins", 0) + 1
                if ctx["sticky_recovery_wins"] >= cfg.sticky_win_reset_after:
                    new_scale = 1.0
                    ctx["sticky_recovery_wins"] = 0
                else:
                    new_scale = state["current_scale"]
            else:
                new_scale = 1.0
                ctx["sticky_recovery_wins"] = 0
        else:
            new_scale = min(state["current_scale"] * cfg.loss_scale, cfg.max_scale)
            ctx["sticky_recovery_wins"] = 0
    elif cfg.scaling > 1.0:
        new_scale = min(state["current_scale"] * cfg.scaling, cfg.max_scale) if won else 1.0
    else:
        new_scale = 1.0

    # Update consecutive bet loss counter
    if won:
        ctx["consec_bet_losses"] = 0
    else:
        ctx["consec_bet_losses"] += 1

    # Check cooldown trigger BEFORE recording (so scale reset is saved)
    trigger = cfg.consec_loss_trigger
    pause   = cfg.consec_loss_pause
    cooldown_fired = (
        not won
        and trigger > 0
        and ctx["consec_bet_losses"] >= trigger
    )
    if cooldown_fired:
        if getattr(cfg, "cooldown_resets_scale", True):
            new_scale = 1.0  # reset scale on cooldown
        ctx["cooldown_left"]      = pause
        ctx["consec_bet_losses"]  = 0
        ctx["sticky_recovery_wins"] = 0

    res = account.record_bet(
        round_db_id    = r["id"],
        game_round_id  = r.get("game_round_id"),
        bet_sol        = dec["bet_sol"],
        cashout_target = dec["cashout"],
        actual_mult    = r["multiplier"],
        new_scale      = new_scale,
    )
    won_str   = "WIN " if res["won"] else "LOSS"
    scale_str = f"  scale x{new_scale:.2f}" if (cfg.scaling > 1.0 or cfg.loss_scale > 1.0) else ""
    cd_str    = f"  [COOLDOWN {pause}r]" if cooldown_fired else ""
    print(
        f"[{cfg.name}] rnd {r['id']:>7}  x{r['multiplier']:.2f}"
        f"  bet {dec['bet_sol']:.5f}"
        f"  {won_str}  pnl {_fmt_sol(res['pnl_sol']).strip()}"
        f"  bank {res['total_bank_after']:.5f}"
        f"{scale_str}{cd_str}"
    )


def main():
    ap = argparse.ArgumentParser(description="Multi-strategy paper-trading bot")
    ap.add_argument("--db",       default=DB_DEFAULT,     help="collector rounds DB")
    ap.add_argument("--bot-db",   default=BOT_DB_DEFAULT, help="bot state DB")
    ap.add_argument("--strategy", default="compound",
                    choices=list(STRATEGIES.keys()),
                    help="trading strategy to use")
    args = ap.parse_args()
    os.makedirs("data", exist_ok=True)
    cfg = STRATEGIES[args.strategy]
    run(args.db, args.bot_db, cfg)


if __name__ == "__main__":
    main()
