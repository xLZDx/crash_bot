#!/usr/bin/env python3
"""
live_feed.py
Publisher for the event-driven paper bots. Tails crash.duckdb (the collector's
output) -- ONE reader instead of 25 bots each polling the DB -- and fans out each
new round as a ZMQ 'round_end' event. Each event carries recent_mults (>=202, for
the relwave strategies) so subscribers never touch the DB.

The collector writes a round at crash-time; the publisher emits it ~POLL_S later.
That ~1s is fine: a paper bot bets on the NEXT round (several-second window) and
the realism comes from the latency/miss model in bot_live.py.
"""
import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone

import zmq

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db_util import connect_ro

CRASH = "data/crash.duckdb"
ENDPOINT = "tcp://127.0.0.1:5560"
TOPIC = b"ROUND"
RING = 220          # >= 202 (relwave_bad_veto_21: 180+7+15)
POLL_S = 1.0
HEARTBEAT = "data/live_feed_heartbeat.json"


def make_events(rows, ring, seq, epoch):
    """Pure. rows: [(id, game_round_id, multiplier, ts_str)] sorted by id ASC.
    Appends each multiplier to `ring`, returns (events, new_seq, new_last_id)."""
    events = []
    new_seq = seq
    last_id = None
    for rid, gid, mult, ts in rows:
        last_id = rid
        if mult is None:
            continue
        ring.append(float(mult))
        new_seq += 1
        events.append({
            "type": "round_end", "seq": new_seq, "epoch": epoch,
            "game_round_id": str(gid), "multiplier": float(mult),
            "recent_mults": list(ring), "ts": ts,
        })
    return events, new_seq, last_id


def _bootstrap():
    with connect_ro(CRASH) as c:
        rows = c.execute(
            "SELECT multiplier FROM rounds ORDER BY id DESC LIMIT ?", [RING]
        ).fetchall()
        last_id = c.execute("SELECT COALESCE(MAX(id), 0) FROM rounds").fetchone()[0]
    ring = deque((float(r[0]) for r in reversed(rows) if r[0] is not None), maxlen=RING)
    return ring, last_id


def _fetch_new(last_id):
    with connect_ro(CRASH) as c:
        return c.execute(
            "SELECT id, game_round_id, multiplier, CAST(ts AS VARCHAR) "
            "FROM rounds WHERE id > ? ORDER BY id ASC", [last_id]
        ).fetchall()


def _heartbeat(seq, last_id, last_gid):
    tmp = HEARTBEAT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "iso": datetime.now(timezone.utc).isoformat(),
                   "seq": seq, "last_id": last_id, "last_gid": last_gid}, f)
    os.replace(tmp, HEARTBEAT)


def main():
    epoch = int(time.time())
    ctx = zmq.Context.instance()
    pub = ctx.socket(zmq.PUB)
    pub.bind(ENDPOINT)
    time.sleep(0.3)
    ring, last_id = _bootstrap()
    seq = 0
    last_gid = None
    print(f"live_feed epoch={epoch} bind={ENDPOINT} ring={len(ring)} last_id={last_id}",
          flush=True)
    while True:
        try:
            rows = _fetch_new(last_id)
        except Exception as e:
            print(f"[feed] db read error: {str(e)[:120]}", flush=True)
            time.sleep(POLL_S)
            continue
        if rows:
            events, seq, new_last = make_events(rows, ring, seq, epoch)
            for ev in events:
                pub.send_multipart([TOPIC, json.dumps(ev).encode()])
                last_gid = ev["game_round_id"]
            if new_last is not None:
                last_id = new_last
            if events:
                print(f"[feed] emitted {len(events)} (seq->{seq}, gid {last_gid})", flush=True)
        _heartbeat(seq, last_id, last_gid)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
