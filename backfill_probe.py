#!/usr/bin/env python3
"""
backfill_probe.py
Reconnaissance: does BCGame expose a crash round-HISTORY endpoint we could use to
backfill the lost rounds? Opens the crash page anonymously (no login, no bets),
clicks the History tab, and captures network responses + WS frames for a short
window, printing anything that looks like round history.

Read-only recon. If it finds a history endpoint -> backfill is feasible (build a
fetcher). If not -> past outage gaps are unrecoverable; rely on gap_monitor.py to
prevent FUTURE loss.
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

URL = "https://bcgame61.com/game/crash"
CAPTURE_SECS = 25
KEYWORDS = ("history", "record", "recent", "/list", "round", "result", "crash/")
SKIP = ("captcha", "intercom", "tracking", "realtime", "sentry", "rum", "analytics")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def main():
    from playwright.sync_api import sync_playwright
    http_hits = []
    ws_hits = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(user_agent=UA, viewport={"width": 1280, "height": 800})
        pg = ctx.new_page()

        def on_resp(resp):
            u = resp.url.lower()
            if any(s in u for s in SKIP):
                return
            if any(k in u for k in KEYWORDS):
                try:
                    http_hits.append((resp.status, resp.url[:180]))
                except Exception:
                    pass
        pg.on("response", on_resp)

        def on_ws(ws):
            def on_frame(pl):
                try:
                    s = pl if isinstance(pl, str) else ""
                    low = s.lower()
                    if any(k in low for k in ("history", "recent", "round")) and len(ws_hits) < 5:
                        ws_hits.append(s[:200])
                except Exception:
                    pass
            ws.on("framereceived", on_frame)
        pg.on("websocket", on_ws)

        try:
            pg.goto(URL, timeout=30000)
        except Exception as e:
            print(f"goto failed: {e}")
        pg.wait_for_timeout(6000)
        clicked = False
        for sel in ('button:has-text("History")', 'text=History',
                    '[role=tab]:has-text("History")', 'a:has-text("History")'):
            try:
                pg.locator(sel).first.click(timeout=2000)
                print(f"clicked History via: {sel}")
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            try:
                pg.screenshot(path="data/probe_screenshot.png")
            except Exception:
                pass
            print("WARNING: History tab click FAILED for all selectors -- result is")
            print("         UNRELIABLE (tried to save data/probe_screenshot.png)")
        pg.wait_for_timeout(CAPTURE_SECS * 1000)
        b.close()

    seen = set()
    print(f"\n=== candidate HTTP history endpoints ({len(http_hits)}) ===")
    for status, u in http_hits:
        if u in seen:
            continue
        seen.add(u)
        print(f"  HTTP {status}  {u}")
    print(f"\n=== WS frames mentioning history/recent/round ({len(ws_hits)}) ===")
    for s in ws_hits:
        print(f"  WS  {s}")
    if not http_hits and not ws_hits:
        if clicked:
            print("\nNONE found -> no obvious crash-history API; past outage gaps are")
            print("likely UNRECOVERABLE. Rely on gap_monitor.py to prevent future loss.")
        else:
            print("\nNONE found, but the History tab was NEVER opened -> INCONCLUSIVE,")
            print("not 'unrecoverable'. Re-run after fixing the History selector.")
    else:
        print("\nCandidates found -> backfill MAY be feasible; inspect the endpoint(s) above.")


if __name__ == "__main__":
    main()
