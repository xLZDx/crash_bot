"""Scalefix regression: the real bot's martingale scale must RESET after a
cooldown (or an unplaced/suspended bet), not stay pinned at MAX ($0.08) for a
whole losing streak. Tests _scale_from_audit directly by pointing AUDIT_FILE at
synthetic audit logs."""
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


def L():  return {"event": "result", "result": "loss"}
def W():  return {"event": "result", "result": "win"}
def CR(): return {"event": "cooldown_reset"}
def SR(): return {"event": "suspended_reset"}


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


def test_suspended_reset_breaks_streak(tmp_path):
    _audit(tmp_path, [L(), L(), SR(), L()])    # 1 loss since suspended
    assert bot_realbet._scale_from_audit() == min(LS ** 1, MAX)   # 2.0


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
