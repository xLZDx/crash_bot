"""Tests for bot_latency_model (placement-latency + miss model calibration)."""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot_latency_model import load_model, LatencyModel, PRIOR_MISS_PROB


def test_no_audit_uses_prior(tmp_path):
    m = load_model(str(tmp_path / "missing.jsonl"))
    assert m.source.startswith("prior")
    assert m.miss_prob == PRIOR_MISS_PROB
    assert m.n == 0


def test_audit_computes_miss_rate(tmp_path):
    p = tmp_path / "audit.jsonl"
    lines = []
    for i in range(100):
        lines.append(json.dumps({"event": "bet_attempt", "game_round_id": str(i),
                                 "ts": "2026-06-05T00:00:00+00:00"}))
    for i in range(20):
        lines.append(json.dumps({"event": "place_error", "game_round_id": str(i)}))
    p.write_text("\n".join(lines))
    m = load_model(str(p))
    assert m.source == "audit"
    assert abs(m.miss_prob - 0.20) < 0.01


def test_insufficient_audit_falls_back(tmp_path):
    p = tmp_path / "audit.jsonl"
    p.write_text("\n".join(
        json.dumps({"event": "bet_attempt", "game_round_id": str(i)})
        for i in range(5)
    ))
    m = load_model(str(p))
    assert m.source.startswith("prior")
    assert m.miss_prob == PRIOR_MISS_PROB


def test_sample_delay_non_negative():
    m = LatencyModel(0.1, 300.0, 200.0, "test", 50)
    rng = random.Random(42)
    vals = [m.sample_delay_ms(rng) for _ in range(200)]
    assert all(v >= 0.0 for v in vals)
    assert max(vals) > 0.0


def test_will_miss_distribution():
    m = LatencyModel(0.5, 300.0, 100.0, "test", 50)
    rng = random.Random(1)
    results = [m.will_miss(rng) for _ in range(200)]
    assert any(results) and not all(results)


def test_will_miss_deterministic_with_seed():
    m = LatencyModel(0.3, 300.0, 100.0, "test", 50)
    a = [m.will_miss(random.Random(7)) for _ in range(1)]
    b = [m.will_miss(random.Random(7)) for _ in range(1)]
    assert a == b
