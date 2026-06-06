"""
bot_settlement.py
Pure martingale scale + cooldown logic, shared by the live paper bot
(bot_live.py) and mirroring the replay bot (bot.py:294-333). Given win/loss +
the strategy config + a running context, returns the next scale and whether a
cooldown fired.

cooldown_resets_scale=True (default, and what the real bot now does) resets the
ladder to 1.0 ($0.01) on cooldown; False keeps it (variant C). This single flag
drives the reset-vs-keep comparison.
"""


def apply_scale_and_cooldown(won, cfg, current_scale, ctx, cooldown_resets_scale=True):
    """ctx: mutable dict (keys consec_bet_losses, sticky_recovery_wins).
    Returns (new_scale, cooldown_fired)."""
    if cfg.loss_scale > 1.0:
        if won:
            if cfg.sticky_win_reset_after > 0 and current_scale > 1.0:
                ctx["sticky_recovery_wins"] = ctx.get("sticky_recovery_wins", 0) + 1
                if ctx["sticky_recovery_wins"] >= cfg.sticky_win_reset_after:
                    new_scale = 1.0
                    ctx["sticky_recovery_wins"] = 0
                else:
                    new_scale = current_scale
            else:
                new_scale = 1.0
                ctx["sticky_recovery_wins"] = 0
        else:
            new_scale = min(current_scale * cfg.loss_scale, cfg.max_scale)
            ctx["sticky_recovery_wins"] = 0
    elif cfg.scaling > 1.0:
        new_scale = min(current_scale * cfg.scaling, cfg.max_scale) if won else 1.0
    else:
        new_scale = 1.0

    if won:
        ctx["consec_bet_losses"] = 0
    else:
        ctx["consec_bet_losses"] = ctx.get("consec_bet_losses", 0) + 1

    trigger = getattr(cfg, "consec_loss_trigger", 0)
    cooldown_fired = (not won and trigger > 0 and ctx["consec_bet_losses"] >= trigger)
    if cooldown_fired:
        if cooldown_resets_scale:
            new_scale = 1.0
        ctx["consec_bet_losses"] = 0
        ctx["sticky_recovery_wins"] = 0
    return new_scale, cooldown_fired
