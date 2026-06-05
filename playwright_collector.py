"""
playwright_collector.py — BCGame crash data collector using Playwright WS interception.

Uses a SINGLE persistent headless Chromium session (never restarts) to intercept
Socket.IO frames from socketv4.bcgame61.com. Parses round-end frames via
_parse_cm_round_end from collector.py.

Resource usage: ~200 MB RAM, ~5-10% CPU idle.
Why Playwright: direct WS auth requires per-session tokens that the browser generates
                via Cloudflare challenge — cannot be replicated without the full browser.

Frame types on wss://socketv4.bcgame61.com (reverse-engineered 2026-05-19):
  TYPE A  \\x01e   — player cashout  (ignored: field3=cashout_mult*100, NOT crash)
  TYPE B  \\x02pg  — progress ping   (ignored)
  TYPE C  \\x02ed  — round end       (captured: field1=round_id, field6=crash*100)
  TYPE D  \\x02st  — round stats     (captured: same + field7=provably-fair hash)
"""
import asyncio
import logging
import logging.handlers
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from config import COLLECTOR_LOG, DB_PATH
# _parse_cm_round_end is the correct round-end frame parser from collector.py.
# Importing collector at module level also imports playwright.async_api (collector's top-level
# import), so the lazy `from playwright.async_api import async_playwright` inside run() is
# redundant but harmless — both paths require playwright to be installed.
from collector import _parse_cm_round_end
from storage import CrashStorage

# ── Logging ───────────────────────────────────────────────────────────────────

def _build_logger() -> logging.Logger:
    log = logging.getLogger("pw_collector")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    Path(COLLECTOR_LOG).parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        COLLECTOR_LOG, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    log.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
    log.addHandler(ch)
    return log


log = _build_logger()

# ── Constants ─────────────────────────────────────────────────────────────────

_CM_HEADER = b'\x04\x02\x05/g/cm'
_SEEN_MAX = 10_000

# Privacy: raw binary WS frames are never written to logs by default.
# Set to True only for short debugging sessions; never commit True.
_LOG_RAW_FRAMES: bool = False


# ── Playwright collector ───────────────────────────────────────────────────────

class PlaywrightCollector:
    def __init__(self, storage: CrashStorage):
        self._storage = storage
        self._rounds = 0
        self._last_round_ts = time.time()
        self._seen_ids: OrderedDict = OrderedDict()  # FIFO dedup (popitem(last=False))

    def _store_round(self, crash_point: float, round_id: Optional[str]):
        if round_id:
            if round_id in self._seen_ids:
                return
            self._seen_ids[round_id] = True
            if len(self._seen_ids) > _SEEN_MAX:
                self._seen_ids.popitem(last=False)

        self._last_round_ts = time.time()
        try:
            self._storage.insert(
                multiplier    = crash_point,
                source        = "playwright_ws",
                game_round_id = round_id,
                frame_event   = "round_complete",
            )
            self._rounds += 1
            log.info("Round #%s  %.2fx  (total=%d)",
                     round_id or "?", crash_point, self._rounds)
        except Exception as e:
            log.error("DB insert failed: %s", e)

    def _on_frame(self, data):
        raw: Optional[bytes] = None
        if isinstance(data, (bytes, bytearray)):
            raw = bytes(data)
        elif isinstance(data, dict):
            payload = data.get("payload", b"")
            if isinstance(payload, (bytes, bytearray)):
                raw = bytes(payload)
        if raw is None:
            return

        if _LOG_RAW_FRAMES:
            # Log only the byte-length, never the raw content (privacy policy).
            log.debug("raw_frame len=%d", len(raw))

        result = _parse_cm_round_end(raw)
        if result.multiplier is not None:
            self._store_round(result.multiplier, result.game_round_id)

    def _on_ws(self, ws):
        if "bcgame" not in ws.url:
            return
        log.info("WS connected: %s", ws.url)
        ws.on("framereceived", self._on_frame)

    async def run(self, duration_hours: float = 8760.0):
        from playwright.async_api import async_playwright

        deadline = time.time() + duration_hours * 3600

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context()
            page = await context.new_page()
            page.on("websocket", self._on_ws)

            log.info("Browser launched -- loading BCGame crash page")
            try:
                await page.goto(
                    "https://bcgame61.com/game/crash",
                    timeout=30_000,
                    wait_until="domcontentloaded",
                )
                log.info("Page loaded")
            except Exception as e:
                log.warning("Page load error (continuing): %s", e)

            while time.time() < deadline:
                await asyncio.sleep(60)
                silent_secs = time.time() - self._last_round_ts
                if silent_secs > 300:
                    log.warning("No data for %.0fs -- reloading page", silent_secs)
                    try:
                        await page.reload(timeout=30_000, wait_until="domcontentloaded")
                        log.info("Page reloaded")
                    except Exception as e:
                        log.error("Reload failed: %s", e)
                elif silent_secs > 60:
                    log.info("Quiet for %.0fs  total_rounds=%d", silent_secs, self._rounds)

            await browser.close()


# ── Entry point ───────────────────────────────────────────────────────────────

async def run(db_path: str = DB_PATH, duration_hours: float = 8760.0):
    storage = CrashStorage(db_path)
    n_existing = storage.count()
    log.info("DB: %s  (%d existing rounds)", db_path, n_existing)
    print(f"[pw_collector] DB: {db_path}  ({n_existing:,} rounds)")
    print(f"[pw_collector] Single persistent Chromium session (~200 MB RAM).")
    try:
        collector = PlaywrightCollector(storage)
        await collector.run(duration_hours=duration_hours)
    finally:
        storage.close()
