#!/usr/bin/env python3
"""
bot_live.py
Event-driven paper bot. Subscribes to live_feed's ZMQ round_end events and
simulates the real bot's flow with an internal balance:
  on each round_end -> (1) SETTLE the bet placed at the previous round_end against
  THIS round's multiplier, (2) DECIDE a bet for the next round (decide() +
  latency/miss model), holding it IN MEMORY until the next round_end.
No DB polling -- the round + recent_mults arrive in the event.
--cooldown-scale reset|keep drives the reset-vs-keep comparison.
"""
import argparse
import json
import random
import sys

import zmq

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bot_state import BotAccount
from bot_strategy import STRATEGIES, decide
from bot_settlement import apply_scale_and_cooldown
from bot_latency_model import load_model

ENDPOINT = "tcp://127.0.0.1:5560"
TOPIC = b"ROUND"
RCVTIMEO_MS = 120000


def _new_state():
    return {"open_bet": None, "sctx": {"consec_bet_losses": 0, "sticky_recovery_wins": 0},
            "cooldown_left": 0, "current_scale": 1.0, "last_seq": None, "last_epoch": None,
            "placed": 0, "settled": 0, "missed": 0}


def handle_event(ev, acct, cfg, st, lat, rng, cooldown_resets, log=lambda *a: None):
    """Process one round_end event: settle prior bet, then decide next. Mutates
    st + writes a settled bet via acct.record_bet. Returns None."""
    if ev.get("type") != "round_end":
        return
    mult = ev["multiplier"]
    gid = ev["game_round_id"]
    recent = ev.get("recent_mults") or None
    epoch = ev.get("epoch")
    seq = ev.get("seq")

    # publisher restart -> the in-flight bet can't be trusted; void it
    if st["last_epoch"] is not None and epoch != st["last_epoch"] and st["open_bet"]:
        log("publisher epoch changed -> void open bet")
        st["open_bet"] = None
    st["last_epoch"] = epoch
    st["last_seq"] = seq

    # (1) settle the bet placed last round against THIS round's outcome
    ob = st["open_bet"]
    if ob is not None:
        won = mult >= ob["cashout"]
        new_scale, cd = apply_scale_and_cooldown(won, cfg, ob["scale"], st["sctx"], cooldown_resets)
        acct.record_bet(round_db_id=None, game_round_id=gid, bet_sol=ob["bet"],
                        cashout_target=ob["cashout"], actual_mult=mult, new_scale=new_scale)
        st["current_scale"] = new_scale
        st["settled"] += 1
        if cd:
            st["cooldown_left"] = cfg.consec_loss_pause
        st["open_bet"] = None
    else:
        st["current_scale"] = acct.get_state()["current_scale"]

    # (2) decide the next bet (settled at the next round_end)
    if st["cooldown_left"] > 0:
        st["cooldown_left"] -= 1
        return
    state = acct.get_state()
    dec = decide(session_bank=state["session_bank_sol"], session_start=state["session_start_sol"],
                 total_bank=state["total_bank_sol"], session_rounds=state["session_rounds"],
                 consec_losing=state["consec_losing_sessions"], cfg=cfg,
                 current_scale=st["current_scale"], recent_multipliers=recent)
    action = dec["action"]
    if action == "end_session":
        acct.close_session(dec["reason"])
        acct.open_new_session()
        st["sctx"] = {"consec_bet_losses": 0, "sticky_recovery_wins": 0}
        return
    if action == "paused":
        acct.increment_paused_skipped()
        return
    if action in ("no_funds", "no_bet"):
        return
    # action == "bet": apply the latency/miss model (a miss = void, NOT a loss)
    if lat.will_miss(rng):
        st["missed"] += 1
        return
    st["open_bet"] = {"bet": dec["bet_sol"], "cashout": dec["cashout"], "scale": st["current_scale"]}
    st["placed"] += 1


def main():
    ap = argparse.ArgumentParser(description="Event-driven paper bot (ZMQ)")
    ap.add_argument("--strategy", required=True, choices=list(STRATEGIES.keys()))
    ap.add_argument("--bot-db", required=True)
    ap.add_argument("--cooldown-scale", choices=["reset", "keep"], default="reset")
    ap.add_argument("--zmq-endpoint", default=ENDPOINT)
    ap.add_argument("--seed", default="")   # "" = real randomness; int = reproducible
    args = ap.parse_args()

    cfg = STRATEGIES[args.strategy]
    cooldown_resets = (args.cooldown_scale == "reset")
    acct = BotAccount(args.bot_db, cfg=cfg)
    lat = load_model()
    rng = random.Random(int(args.seed)) if str(args.seed).strip() else random.Random()

    ctxz = zmq.Context.instance()
    sub = ctxz.socket(zmq.SUB)
    sub.connect(args.zmq_endpoint)
    sub.setsockopt(zmq.SUBSCRIBE, TOPIC)
    sub.setsockopt(zmq.RCVTIMEO, RCVTIMEO_MS)

    st = _new_state()
    print(f"[{cfg.name}] live SUB {args.zmq_endpoint} cooldown-scale={args.cooldown_scale} {lat}",
          flush=True)
    n = 0
    while True:
        try:
            _topic, raw = sub.recv_multipart()
        except zmq.Again:
            print(f"[{cfg.name}] no event for {RCVTIMEO_MS/1000:.0f}s (publisher down?)", flush=True)
            continue
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        handle_event(ev, acct, cfg, st, lat, rng, cooldown_resets,
                     log=lambda m: print(f"[{cfg.name}] {m}", flush=True))
        n += 1
        if n % 50 == 0:
            bk = acct.get_state()["total_bank_sol"]
            print(f"[{cfg.name}] ev={n} placed={st['placed']} settled={st['settled']} "
                  f"missed={st['missed']} bank={bk:.4f}", flush=True)


if __name__ == "__main__":
    main()
