"""Tests for bot_settlement.apply_scale_and_cooldown (reset vs keep on cooldown)."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_settlement import apply_scale_and_cooldown


def _cfg(loss_scale=2.0, max_scale=8.0, consec_loss_trigger=4,
         scaling=1.0, sticky_win_reset_after=0):
    return SimpleNamespace(loss_scale=loss_scale, max_scale=max_scale,
                           consec_loss_trigger=consec_loss_trigger, scaling=scaling,
                           sticky_win_reset_after=sticky_win_reset_after)


def test_ladder_climbs_on_losses():
    cfg = _cfg()
    ctx = {}
    scale = 1.0
    seq = []
    for _ in range(3):
        scale, cd = apply_scale_and_cooldown(False, cfg, scale, ctx)
        seq.append(scale)
    assert seq == [2.0, 4.0, 8.0]   # capped at max_scale=8


def test_win_resets_to_one():
    cfg = _cfg()
    ctx = {"consec_bet_losses": 2}
    scale, cd = apply_scale_and_cooldown(True, cfg, 4.0, ctx)
    assert scale == 1.0 and cd is False and ctx["consec_bet_losses"] == 0


def test_cooldown_reset_variant():
    cfg = _cfg(consec_loss_trigger=4)
    ctx = {"consec_bet_losses": 3}
    # 4th loss -> cooldown -> RESET scale to 1.0
    scale, cd = apply_scale_and_cooldown(False, cfg, 8.0, ctx, cooldown_resets_scale=True)
    assert cd is True and scale == 1.0 and ctx["consec_bet_losses"] == 0


def test_cooldown_keep_variant():
    cfg = _cfg(consec_loss_trigger=4)
    ctx = {"consec_bet_losses": 3}
    # 4th loss -> cooldown but KEEP scale (variant C)
    scale, cd = apply_scale_and_cooldown(False, cfg, 8.0, ctx, cooldown_resets_scale=False)
    assert cd is True and scale == 8.0   # NOT reset


def test_reset_and_keep_diverge_only_at_cooldown():
    cfg = _cfg(consec_loss_trigger=4)
    # 3 losses identical for both
    for resets in (True, False):
        ctx = {}
        scale = 1.0
        for _ in range(3):
            scale, _ = apply_scale_and_cooldown(False, cfg, scale, ctx, cooldown_resets_scale=resets)
        assert scale == 8.0   # same up to the cap
    # 4th loss (cooldown) diverges
    ctx_r = {"consec_bet_losses": 3}
    ctx_k = {"consec_bet_losses": 3}
    sr, _ = apply_scale_and_cooldown(False, cfg, 8.0, ctx_r, cooldown_resets_scale=True)
    sk, _ = apply_scale_and_cooldown(False, cfg, 8.0, ctx_k, cooldown_resets_scale=False)
    assert sr == 1.0 and sk == 8.0


def test_anti_martingale_scaling():
    cfg = _cfg(loss_scale=1.0, scaling=1.5, max_scale=8.0, consec_loss_trigger=0)
    ctx = {}
    s1, _ = apply_scale_and_cooldown(True, cfg, 1.0, ctx)    # win -> scale up
    assert s1 == 1.5
    s2, _ = apply_scale_and_cooldown(False, cfg, 1.5, ctx)   # loss -> reset
    assert s2 == 1.0
