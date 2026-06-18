"""nr512 deep-ladder fix: cooldown must CARRY the scale (not reset) when the strategy
has cooldown_resets_scale=False, so _scale_from_audit climbs to MAX_SCALE (512x).

Bug (2026-06-18): bot_realbet always logged `cooldown_reset` after CONSEC_TRIGGER
losses, resetting scale to 1.0 regardless of cooldown_resets_scale -> nr512 capped at
$0.08 even on a 5-loss streak. Fix: branch on COOLDOWN_RESETS_SCALE; nr512 logs
`cooldown_carry` which _scale_from_audit does NOT break on, so consec keeps climbing.

Repo pytest-free convention: plain asserts; run directly.
"""
import os
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot_realbet as br

_L = {"event": "result", "result": "loss"}
_W = {"event": "result", "result": "win"}
_CARRY = {"event": "cooldown_carry"}
_RESET = {"event": "cooldown_reset"}
_SUSP = {"event": "suspended_reset"}


def _audit(events):
    f = tempfile.NamedTemporaryFile("w", delete=False, suffix=".jsonl")
    for e in events:
        f.write(json.dumps(e) + "\n")
    f.close()
    return Path(f.name)


def _scale_for(events):
    p = _audit(events)
    saved = br.AUDIT_FILE
    try:
        br.AUDIT_FILE = p
        return br._scale_from_audit()
    finally:
        br.AUDIT_FILE = saved
        os.unlink(p)


def test_nr512_constant_is_carry():
    # nr512 is the selected strategy -> cooldown must NOT reset scale
    assert br.COOLDOWN_RESETS_SCALE is False
    assert br.MAX_SCALE == 512.0


def test_carry_ladder_climbs_to_512():
    # 9 bet-losses, cooldown_carry after every 4 -> scale = 2^9 capped at 512
    ev = [_W]
    losses = 0
    for _ in range(9):
        ev.append(_L)
        losses += 1
        if losses % 4 == 0:
            ev.append(_CARRY)
    sc = _scale_for(ev)
    assert sc == min(br.LOSS_SCALE ** 9, br.MAX_SCALE) == 512.0, sc


def test_carry_5_losses_reaches_32_not_8():
    # the exact bug case: 5 losses with a carry after 4 -> scale 32 ($0.32), NOT 8
    ev = [_W, _L, _L, _L, _L, _CARRY, _L]
    sc = _scale_for(ev)
    assert sc == br.LOSS_SCALE ** 5 == 32.0, sc   # was buggy 8.0 before the fix


def test_cooldown_reset_still_breaks_the_ladder():
    # old cap-8 behavior: cooldown_reset resets -> only losses AFTER it count
    ev = [_W] + [_L] * 4 + [_RESET] + [_L] * 2
    sc = _scale_for(ev)
    assert sc == br.LOSS_SCALE ** 2 == 4.0, sc


def test_suspended_reset_still_breaks():
    ev = [_W] + [_L] * 3 + [_SUSP] + [_L] * 1
    sc = _scale_for(ev)
    assert sc == br.LOSS_SCALE ** 1 == 2.0, sc


def test_win_resets_to_base():
    sc = _scale_for([_L, _L, _L, _W])
    assert sc == 1.0, sc


def test_cap_holds_beyond_512():
    # 12 losses all carried -> capped at 512, not 4096
    ev = [_W]
    n = 0
    for _ in range(12):
        ev.append(_L)
        n += 1
        if n % 4 == 0:
            ev.append(_CARRY)
    sc = _scale_for(ev)
    assert sc == 512.0, sc


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("OK")
