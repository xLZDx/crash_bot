"""
bot_latency_model.py
Calibrates a placement-latency + miss model from the REAL bot's execution audit
(data/realbet_execution_audit.jsonl) so the live paper bots experience realistic
delays and occasionally miss the betting window -- just like the real bot. Falls
back to documented priors when there is not enough audit data.

CALIBRATION NOTE (source of the priors): 2026-06-05 real-bot audit showed ~14%
of bet attempts failed to land (place_error / suspended_reset, dominated by a
DuckDB lock collision since fixed) and placement lag was a few hundred ms.
Re-derive by re-running load_model() against a fresh audit; priors are only used
when fewer than MIN_SAMPLES events are available.
"""
import json
import random
from pathlib import Path

PRIOR_MISS_PROB = 0.14
PRIOR_LAG_MS_MEAN = 380.0
PRIOR_LAG_MS_STD = 150.0
MIN_SAMPLES = 30


class LatencyModel:
    def __init__(self, miss_prob, lag_mean_ms, lag_std_ms, source, n):
        self.miss_prob = float(miss_prob)
        self.lag_mean_ms = float(lag_mean_ms)
        self.lag_std_ms = float(lag_std_ms)
        self.source = source
        self.n = int(n)

    def will_miss(self, rng=random) -> bool:
        """True if this round's bet would NOT land (treated as a void, not a loss)."""
        return rng.random() < self.miss_prob

    def sample_delay_ms(self, rng=random) -> float:
        """A realistic placement delay in ms (>= 0)."""
        return max(0.0, rng.gauss(self.lag_mean_ms, self.lag_std_ms))

    def __repr__(self):
        return (f"LatencyModel(miss={self.miss_prob:.3f}, "
                f"lag~{self.lag_mean_ms:.0f}+-{self.lag_std_ms:.0f}ms, "
                f"source={self.source}, n={self.n})")


def _ts(rec):
    t = rec.get("ts", "")
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _prior(reason):
    return LatencyModel(PRIOR_MISS_PROB, PRIOR_LAG_MS_MEAN, PRIOR_LAG_MS_STD, reason, 0)


def load_model(audit_path="data/realbet_execution_audit.jsonl") -> LatencyModel:
    p = Path(audit_path)
    if not p.exists():
        return _prior("prior(no_audit)")

    attempts = 0
    misses = 0
    attempt_ts = {}   # game_round_id -> bet_attempt ts
    lags = []
    try:
        for line in p.read_text(errors="ignore").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            ev = r.get("event", "")
            gid = str(r.get("game_round_id", "")) if r.get("game_round_id") is not None else ""
            if ev == "bet_attempt":
                attempts += 1
                ts = _ts(r)
                if gid and ts is not None:
                    attempt_ts[gid] = ts
            elif ev in ("place_error", "suspended_reset", "suspended_carry_scale"):
                misses += 1
            elif ev == "bet_placed":
                ts = _ts(r)
                if gid and ts is not None and gid in attempt_ts:
                    dl = (ts - attempt_ts.pop(gid)) * 1000.0
                    if 0 <= dl <= 30000:
                        lags.append(dl)
    except Exception:
        return _prior("prior(audit_read_error)")

    miss_prob = (misses / attempts) if attempts >= MIN_SAMPLES else PRIOR_MISS_PROB
    if len(lags) >= MIN_SAMPLES:
        mean = sum(lags) / len(lags)
        std = (sum((x - mean) ** 2 for x in lags) / len(lags)) ** 0.5
    else:
        mean, std = PRIOR_LAG_MS_MEAN, PRIOR_LAG_MS_STD

    if attempts >= MIN_SAMPLES or len(lags) >= MIN_SAMPLES:
        source = "audit"
    else:
        source = "prior(insufficient)"
    return LatencyModel(miss_prob, mean, std, source, attempts)
