#!/usr/bin/env python3
"""
gap_monitor.py
Catches NEW round losses going forward so a future outage is detected in minutes
instead of after thousands of lost rounds. Triggers:
  - collector_stalled: newest game_round_id hasn't advanced for STALL_SECS
    (alerted ONCE per stall episode; timer measures since the last advance).
  - new_gap: total missing rounds jumped by >= GAP_DELTA while progressing.
  - collector_db_reset: max_gid went backwards (DB wipe / checkpoint rewind) ->
    pre-reset rounds may be invisible; surfaced loudly.
  - monitor_db_read_failed: crash.duckdb unreadable -> alert (not a silent crash).
On alert: append logs/gap_monitor.log + atomically write data/gap_alert.json.
Run via cron. Pure evaluate() is unit-tested.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db_util import connect_ro

CRASH_DB = "data/crash.duckdb"
STATE = "data/gap_monitor_state.json"
ALERT = "data/gap_alert.json"
LOG = "logs/gap_monitor.log"
STALL_SECS = 180   # no new round in this long -> collector stalled
GAP_DELTA = 5      # missing increased by >= this while progressing -> new gap


def evaluate(prev, cur, now_ts, stall_secs=STALL_SECS, gap_delta=GAP_DELTA):
    """Pure decision. prev: last state dict or None. cur: {max_gid, missing}.
    Returns (alerts: list[str], new_state: dict). new_state['ts'] tracks when
    max_gid LAST advanced, so the stall timer accumulates across ticks."""
    alerts = []
    stall_alerted_for = (prev or {}).get("stall_alerted_for_gid")

    if not prev:
        last_advance = now_ts
    elif cur["max_gid"] != prev["max_gid"]:
        last_advance = now_ts                       # advanced (or reset)
    else:
        last_advance = prev.get("ts", now_ts)       # stuck -> keep last-advance time

    if prev:
        if cur["max_gid"] < prev["max_gid"]:
            alerts.append(f"collector_db_reset: max_gid {prev['max_gid']} -> {cur['max_gid']}")
            stall_alerted_for = None
        else:
            stalled_for = now_ts - prev.get("ts", now_ts)
            if cur["max_gid"] == prev["max_gid"] and stalled_for >= stall_secs:
                if stall_alerted_for != cur["max_gid"]:
                    alerts.append(
                        f"collector_stalled: max_gid stuck at {cur['max_gid']} "
                        f"for {int(stalled_for)}s")
                    stall_alerted_for = cur["max_gid"]
            if cur["max_gid"] > prev["max_gid"]:
                stall_alerted_for = None
                dm = cur["missing"] - prev["missing"]
                if dm >= gap_delta:
                    alerts.append(f"new_gap: missing +{dm} (now {cur['missing']})")

    new_state = {"max_gid": cur["max_gid"], "missing": cur["missing"],
                 "ts": last_advance, "stall_alerted_for_gid": stall_alerted_for}
    return alerts, new_state


def _read_current(crash_db=CRASH_DB):
    with connect_ro(crash_db) as c:
        gmin, gmax, gd = c.execute(
            "SELECT MIN(g), MAX(g), COUNT(DISTINCT g) FROM "
            "(SELECT TRY_CAST(game_round_id AS BIGINT) g FROM rounds) WHERE g IS NOT NULL"
        ).fetchone()
    expected = (gmax - gmin + 1) if gmin is not None else 0
    return {"max_gid": gmax or 0, "missing": (expected - gd) if expected else 0,
            "distinct": gd or 0, "expected": expected}


def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _emit(alerts, cur, ts, extra=None):
    os.makedirs("logs", exist_ok=True)
    msg = f"[{ts}] ALERT " + " | ".join(alerts)
    if cur:
        msg += f" | coverage {cur['distinct']}/{cur['expected']}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    payload = {"ts": ts, "alerts": alerts, "current": cur}
    if extra:
        payload.update(extra)
    _atomic_write_json(ALERT, payload)
    print(msg)


def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.makedirs("logs", exist_ok=True)

    # Read current state of the collector DB -- LOUD on failure, never silent-crash.
    try:
        cur = _read_current()
    except Exception as e:
        _emit(["monitor_db_read_failed: " + str(e)[:120]], None, ts)
        return

    prev = None
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception as e:
            # corrupt state must NOT silently disable detection
            _emit(["monitor_state_corrupt: " + str(e)[:120] + " (detection skipped this tick)"],
                  cur, ts)
            prev = None

    alerts, new = evaluate(prev, cur, time.time())
    _atomic_write_json(STATE, new)

    if alerts:
        _emit(alerts, cur, ts)
    else:
        line = f"[{ts}] ok max_gid={cur['max_gid']} missing={cur['missing']}"
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line)


if __name__ == "__main__":
    main()
