"""Scalefix regression for _scale_from_audit (pointed at synthetic audit logs).

Streak-ending markers (reset the ladder to 1):
  - cooldown_reset  : cap-8 strategies reset on the 4-loss cooldown.
  - suspended_reset : LEGACY (no longer emitted) -- kept so old tails still parse.

Streak-PRESERVING markers (ladder keeps climbing -- 2026-06-20 nr512 fix):
  - suspended_retry : a missed betting window placed NO money, so the loss it
    tried to recover is still owed -> the SAME (doubled) stake is retried next
    round. Resetting here was the flat-$0.01 bug.
  - cooldown_carry  : nr512 carries the scale through the cooldown pause, so a
    deep streak climbs toward MAX_SCALE (512x = $5.12), not pinned low.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_realbet

LS = bot_realbet.LOSS_SCALE   # 2.0
MAX = bot_realbet.MAX_SCALE   # 8.0


def _audit(tmp_path, events):
    p = tmp_path / "audit.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events))
    bot_realbet.AUDIT_FILE = p
    return p


def L():   return {"event": "result", "result": "loss"}
def W():   return {"event": "result", "result": "win"}
def CR():  return {"event": "cooldown_reset"}
def SR():  return {"event": "suspended_reset"}   # legacy break marker (no longer emitted)
def SRT(): return {"event": "suspended_retry"}   # 2026-06-20: missed window -> ladder PRESERVED
def CC():  return {"event": "cooldown_carry"}    # nr512: cooldown carries the scale


def test_ladder_climbs_on_losses(tmp_path):
    _audit(tmp_path, [W(), L(), L()])          # 2 losses since win
    assert bot_realbet._scale_from_audit() == min(LS ** 2, MAX)   # 4.0 -> $0.04


def test_win_resets_to_one(tmp_path):
    _audit(tmp_path, [L(), L(), L(), W()])
    assert bot_realbet._scale_from_audit() == 1.0


def test_cooldown_reset_breaks_streak(tmp_path):
    # 4 losses then cooldown -> next scale is 1.0 ($0.01), NOT 8.0 ($0.08)
    _audit(tmp_path, [L(), L(), L(), L(), CR()])
    assert bot_realbet._scale_from_audit() == 1.0


def test_loss_after_cooldown_restarts_ladder(tmp_path):
    _audit(tmp_path, [L(), L(), L(), L(), CR(), L()])
    assert bot_realbet._scale_from_audit() == min(LS ** 1, MAX)   # 2.0 -> $0.02


def test_suspended_reset_breaks_streak_legacy(tmp_path):
    # LEGACY: suspended_reset is no longer emitted, but old audit tails may still
    # contain it -> it must keep breaking the streak so the post-fix deploy does not
    # mis-count old (pre-fix) flat losses. 1 loss since the legacy marker -> 2.0.
    _audit(tmp_path, [L(), L(), SR(), L()])
    assert bot_realbet._scale_from_audit() == min(LS ** 1, MAX)   # 2.0


def test_suspended_retry_preserves_ladder(tmp_path):
    # 2026-06-20 fix: a missed betting window (suspended_retry) placed NO money, so it
    # must NOT break the streak. 3 losses across a retry -> ladder = 2**3 = 8x ($0.08),
    # NOT reset to 1.0 (the old flat-$0.01 bug).
    _audit(tmp_path, [W(), L(), SRT(), L(), SRT(), L()])
    assert bot_realbet._scale_from_audit() == min(LS ** 3, MAX)   # 8.0


def test_cooldown_carry_preserves_ladder(tmp_path):
    # nr512: cooldown_carry does NOT break the streak (the scale carries through the
    # pause). 5 losses with a carry after the 4th -> 2**5 = 32x, not reset.
    _audit(tmp_path, [W(), L(), L(), L(), L(), CC(), L()])
    assert bot_realbet._scale_from_audit() == min(LS ** 5, MAX)   # 32.0


def test_deep_streak_climbs_to_cap(tmp_path):
    # Deep nr512 streak with carries every 4 losses reaches MAX_SCALE (512x = $5.12).
    events = [W()] + [L()] * 4 + [CC()] + [L()] * 4 + [CC()] + [L()] * 2  # 10 losses
    _audit(tmp_path, events)
    assert bot_realbet._scale_from_audit() == MAX   # 512.0 ($5.12) -- the real tail risk


def test_nine_loss_streak_not_pinned_at_max(tmp_path):
    # The bleed scenario. With cooldown_reset markers every 4 losses the ladder
    # restarts -> scale stays low instead of pinning at MAX (8 = $0.08/round).
    events = [L(), L(), L(), L(), CR(),
              L(), L(), L(), L(), CR(),
              L()]
    _audit(tmp_path, events)
    scale = bot_realbet._scale_from_audit()
    assert scale == LS ** 1                      # 2.0 ($0.02), not 8.0
    assert scale < MAX


def test_pre_fix_behavior_would_have_pinned(tmp_path):
    # Sanity: WITHOUT any reset marker, 9 straight losses DO pin at MAX -- this is
    # the old (buggy) behavior, confirming markers are what break the pin.
    _audit(tmp_path, [L()] * 9)
    assert bot_realbet._scale_from_audit() == MAX   # 8.0 ($0.08) -- the bleed
