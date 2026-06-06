#!/usr/bin/env python3
"""
backfill_rounds.py
Recover recently-lost crash rounds from BCGame's history API
(api/game/bet/multi/history) and store them in a SEPARATE DuckDB
(data/backfill_rounds.duckdb) -- never touches crash.duckdb (the collector holds
its write-lock). The endpoint serves only the most recent ~100 rounds, so this
fills frame-drops / short gaps BEFORE they scroll out of the window. Run via cron
(e.g. every 3 min). The 3 big historical outages are out of this window and stay
unrecoverable.

parse_history() is pure and unit-tested.
"""
import json
import sys
from datetime import datetime, timezone

import duckdb

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db_util import connect_ro

SESSION = "/root/crash-collector/bcgame_session.json"
ENDPOINT = "https://bcgame61.com/api/game/bet/multi/history?page=1&pageSize=100"
STORE = "data/backfill_rounds.duckdb"
CRASH = "data/crash.duckdb"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def parse_history(data):
    """Pure: BCGame history JSON -> [(game_round_id:str, multiplier:float, end_ms:int)].
    Skips malformed entries. Returns [] for an unexpected shape."""
    out = []
    try:
        items = data["data"]["list"]
    except Exception:
        return out
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        gid = it.get("gameId")
        if gid is None:
            continue
        try:
            detail = json.loads(it.get("gameDetail", "{}"))
            rate = float(detail.get("rate"))
            end_ms = int(detail.get("endTime")) if detail.get("endTime") is not None else None
        except Exception:
            continue
        if not (1.0 <= rate <= 1_000_000.0):
            continue
        out.append((str(gid), round(rate, 2), end_ms))
    return out


def _fetch():
    # The history API sits behind Cloudflare and rejects direct/explicit calls;
    # the reliable path is to let the page's History tab issue the request
    # organically and capture the response body.
    from playwright.sync_api import sync_playwright
    import os
    captured = {"data": None}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"])
        kw = {"storage_state": SESSION} if os.path.exists(SESSION) else {}
        ctx = b.new_context(user_agent=UA, viewport={"width": 1280, "height": 800}, **kw)
        pg = ctx.new_page()

        def on_resp(resp):
            if "bet/multi/history" in resp.url.lower():
                try:
                    captured["data"] = resp.json()
                except Exception:
                    pass
        pg.on("response", on_resp)

        try:
            pg.goto("https://bcgame61.com/game/crash", timeout=30000)
            pg.wait_for_timeout(6000)
            for sel in ('button:has-text("History")', 'text=History',
                        '[role=tab]:has-text("History")'):
                try:
                    pg.locator(sel).first.click(timeout=2000)
                    break
                except Exception:
                    continue
            pg.wait_for_timeout(8000)
        finally:
            b.close()
    return captured["data"] or {}


def _ensure_store():
    s = duckdb.connect(STORE)
    s.execute("""
        CREATE TABLE IF NOT EXISTS backfill_rounds(
            game_round_id VARCHAR PRIMARY KEY,
            multiplier    DOUBLE,
            end_ms        BIGINT,
            fetched_ts    TIMESTAMP WITH TIME ZONE
        )""")
    return s


def main():
    data = _fetch()
    rounds = parse_history(data)
    if not rounds:
        print("no rounds parsed from history endpoint (auth/shape?) -- nothing to do")
        return

    with connect_ro(CRASH) as c:
        present = set(r[0] for r in c.execute(
            "SELECT DISTINCT game_round_id FROM rounds WHERE game_round_id IS NOT NULL"
        ).fetchall())

    s = _ensure_store()
    already = set(r[0] for r in s.execute("SELECT game_round_id FROM backfill_rounds").fetchall())
    now = datetime.now(timezone.utc)
    new = 0
    for gid, rate, end_ms in rounds:
        if gid in present or gid in already:
            continue
        s.execute("INSERT INTO backfill_rounds VALUES (?,?,?,?)", [gid, rate, end_ms, now])
        new += 1
    total_store = s.execute("SELECT COUNT(*) FROM backfill_rounds").fetchone()[0]
    s.close()

    in_crash = sum(1 for r in rounds if r[0] in present)
    print(f"window {len(rounds)} rounds | already in crash.duckdb {in_crash} | "
          f"backfilled NEW {new} | backfill store total {total_store}")


if __name__ == "__main__":
    main()
