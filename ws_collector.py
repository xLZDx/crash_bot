"""
ws_collector.py -- DEPRECATED.

Production path is: main.py collect -> playwright_collector.py

This file has known bugs (ts= parameter mismatch in CrashStorage.insert,
TLS verification disabled) and is NOT used by watchdog.py or main.py.
It is kept for reference only.

DO NOT import or run this file. Use playwright_collector.py instead.
"""
import sys
sys.exit(
    "ws_collector.py is deprecated and has known bugs (ts= insert mismatch, "
    "TLS disabled).  Production collector is playwright_collector.py via main.py."
)

# ---- original source preserved below for reference, never executed ----
"""
ws_collector.py -- lightweight direct WebSocket collector for BCGame crash.

Connects directly to socketv4.bcgame61.com via Socket.IO / Engine.IO v3.
Resource usage: ~30 MB RAM, ~1-2% CPU.

Protocol (reverse-engineered from _capture_sent2.py on 2026-05-18):
  Server: socketv4.bcgame61.com  (Engine.IO v3, Socket.IO binary frames)

  Connection flow:
    1. Connect WS, receive text OPEN frame: 0{sid,pingInterval,...}
    2. Send namespace connects: \\x04\\x00[len][ns]\\x00
    3. When /g/cm ACK received, send join: \\x04\\x82\\x00\\x00\\x00\\x00\\x05/g/cm\\x04join
    4. Server streams binary events on /g/cm

  Binary event frames: [04][02][05]/g/cm[event_type][protobuf_payload]
    event_type 0x01 = round complete (has crash_point in field 14 or 12)
    event_type 0x02 = live multiplier update (field 14 = multiplier*100)
"""
import asyncio
import json
import logging
import logging.handlers
import ssl as _ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from config import COLLECTOR_LOG, DB_PATH
from storage import CrashStorage

# ── Constants ──────────────────────────────────────────────────────────────────

_WS_URL = (
    "wss://socketv4.bcgame61.com/socket.io/"
    "?Accept-Language=en&EIO=3&transport=websocket"
)

_HEADERS = {
    "Origin": "https://bcgame61.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

# Namespaces to subscribe (same order the browser client uses)
_NAMESPACES = [
    b"/game-support",
    b"/g/tasks/verify",
    b"/user",
    b"/gs",
    b"/g/cm",
    b"/multi/g/cm",
]

_CM_NS          = b"/g/cm"
_CM_NS_LEN      = len(_CM_NS)                          # 5
_CM_HEADER      = b'\x04\x02' + bytes([_CM_NS_LEN]) + _CM_NS  # 04 02 05 /g/cm
_CM_ACK         = b'\x04\x00' + bytes([_CM_NS_LEN]) + _CM_NS + b'\x00'  # /g/cm connect ACK
_CM_JOIN        = b'\x04\x82\x00\x00\x00\x00' + bytes([_CM_NS_LEN]) + _CM_NS + b'\x04join'

_EIO_PING       = "2"                  # EIO text ping
_EIO_PONG       = "3"                  # expected EIO text pong
_DEFAULT_PING_S = 5.0                  # fallback if server doesn't send pingInterval

# BCGame CDN has a non-critical Basic-Constraints CA cert — bypass verify.
# Connection reads public game data only; no credentials sent.
_SSL_CTX = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = _ssl.CERT_NONE

_EVT_ROUND_COMPLETE = 0x01
_EVT_LIVE_UPDATE    = 0x02

_MIN_M, _MAX_M = 1.0, 100_000.0

# ── Logging ───────────────────────────────────────────────────────────────────

def _build_logger() -> logging.Logger:
    log = logging.getLogger("ws_collector")
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

# ── Protobuf helpers ──────────────────────────────────────────────────────────

def _decode_varint(data: bytes, pos: int):
    result = shift = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            return result, pos
    return result, pos


def _skip_pb_field(data: bytes, pos: int, wire_type: int) -> int:
    if wire_type == 0:
        _, pos = _decode_varint(data, pos)
    elif wire_type == 1:
        pos += 8
    elif wire_type == 2:
        length, pos = _decode_varint(data, pos)
        pos += length
    elif wire_type == 5:
        pos += 4
    return pos


def _scan_multiplier(payload: bytes) -> Optional[float]:
    """
    Scan protobuf payload for a crash multiplier.
    Field 14 (tag 0x70) = multiplier*100 confirmed from live "02" frames.
    Also try field 6 (tag 0x30) as fallback.
    Returns float or None.
    """
    pos = 0
    while pos < len(payload):
        if pos >= len(payload):
            break
        tag_byte = payload[pos]; pos += 1
        wire_type = tag_byte & 0x07
        field_num = tag_byte >> 3

        if wire_type == 0:
            val, pos = _decode_varint(payload, pos)
            if field_num in (14, 6):
                m = val / 100.0
                if _MIN_M <= m <= _MAX_M:
                    return round(m, 2)
        elif wire_type == 2:
            length, pos = _decode_varint(payload, pos)
            pos += length
        else:
            new_pos = _skip_pb_field(payload, pos, wire_type)
            if new_pos <= pos:
                break
            pos = new_pos
    return None


def _scan_round_id(payload: bytes) -> Optional[str]:
    """Field 1 (tag 0x08) = round_id varint."""
    pos = 0
    while pos < len(payload):
        tag_byte = payload[pos]; pos += 1
        wire_type = tag_byte & 0x07
        field_num = tag_byte >> 3
        if wire_type == 0:
            val, pos = _decode_varint(payload, pos)
            if field_num == 1:
                return str(val)
        else:
            new_pos = _skip_pb_field(payload, pos, wire_type)
            if new_pos <= pos:
                break
            pos = new_pos
    return None


def _parse_cm_frame(raw: bytes) -> Optional[dict]:
    """
    Parse /g/cm binary frame from socketv4.bcgame61.com.
    Frame format: [04][02][05]/g/cm[event_type_byte][protobuf...]
    Returns dict with multiplier/round_id on success, None otherwise.
    """
    if not raw.startswith(_CM_HEADER):
        return None
    offset = len(_CM_HEADER)
    if offset >= len(raw):
        return None
    event_type = raw[offset]
    payload = raw[offset + 1:]

    if event_type == _EVT_ROUND_COMPLETE:
        mult = _scan_multiplier(payload)
        if mult is None:
            log.debug("01-frame: no crash_point  hex=%s", raw.hex())
            return None
        rid = _scan_round_id(payload)
        return {"multiplier": mult, "game_round_id": rid, "frame_event": "round_complete"}

    # 0x02 = live multiplier update — not a completed round, skip
    return None

# ── Main collector ─────────────────────────────────────────────────────────────

class LightCollector:
    def __init__(self, storage: CrashStorage, url: Optional[str] = None):
        self._storage    = storage
        self._url        = url or _WS_URL
        self._rounds     = 0
        self._ping_iv    = _DEFAULT_PING_S
        self._joined     = False       # True after /g/cm join sent

    async def run(self, duration_hours: float = 8760.0):
        deadline = time.time() + duration_hours * 3600
        reconnect_delay = 5

        while time.time() < deadline:
            self._joined = False
            try:
                log.info("Connecting to %s", self._url)
                await self._session()
                reconnect_delay = 5
            except (ConnectionClosed, WebSocketException, OSError) as e:
                log.warning("WS disconnected: %s — reconnecting in %ds", e, reconnect_delay)
            except Exception as e:
                log.error("Unexpected error: %s — reconnecting in %ds", e, reconnect_delay)

            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)

    async def _session(self):
        async with websockets.connect(
            self._url,
            additional_headers=_HEADERS,
            ssl=_SSL_CTX,
            ping_interval=None,
            close_timeout=5,
            max_size=4 * 2 ** 20,   # 4 MB — initial history burst is ~42 KB
        ) as ws:
            log.info("WS connected")

            # Step 1: receive Engine.IO OPEN frame (text "0{...}")
            try:
                open_msg = await asyncio.wait_for(ws.recv(), timeout=10)
                if isinstance(open_msg, str) and open_msg.startswith("0"):
                    try:
                        eio_data = json.loads(open_msg[1:])
                        self._ping_iv = eio_data.get("pingInterval", 5000) / 1000.0
                        log.info("EIO OPEN  sid=%s  pingInterval=%.1fs",
                                 eio_data.get("sid", "?"), self._ping_iv)
                    except Exception:
                        pass
            except asyncio.TimeoutError:
                log.warning("No EIO OPEN frame within 10s — proceeding anyway")

            # Step 2: subscribe to namespaces
            for ns in _NAMESPACES:
                await ws.send(b'\x04\x00' + bytes([len(ns)]) + ns + b'\x00')
                await asyncio.sleep(0.05)
            log.info("Namespace connects sent (%d)", len(_NAMESPACES))

            await asyncio.gather(
                self._recv_loop(ws),
                self._ping_loop(ws),
            )

    async def _recv_loop(self, ws):
        async for msg in ws:
            if isinstance(msg, bytes):
                log.debug("BIN %db: %s", len(msg), msg[:32].hex())
                self._handle_binary(ws, msg)
            elif isinstance(msg, str):
                log.debug("TEXT: %s", msg[:120])
                self._handle_text(ws, msg)

    async def _ping_loop(self, ws):
        """EIO text ping every pingInterval seconds."""
        await asyncio.sleep(self._ping_iv)
        while True:
            try:
                await ws.send(_EIO_PING)
            except Exception:
                break
            await asyncio.sleep(self._ping_iv)

    def _handle_text(self, ws, msg: str):
        # EIO pong "3" — no action needed (ping_loop handles timing)
        pass

    def _handle_binary(self, ws, raw: bytes):
        # Check for /g/cm ACK and send join if not yet done
        if not self._joined and raw == _CM_ACK:
            asyncio.ensure_future(self._send_join(ws))
            return

        result = _parse_cm_frame(raw)
        if not result:
            return

        mult  = result["multiplier"]
        gid   = result.get("game_round_id")
        event = result["frame_event"]

        try:
            self._storage.insert(
                multiplier    = mult,
                source        = "ws_direct",
                game_round_id = gid,
                frame_event   = event,
                hash          = None,
            )
            self._rounds += 1
            if self._rounds <= 5 or self._rounds % 50 == 0:
                log.info("Round #%d  %.2fx  id=%s", self._rounds, mult, gid)
        except Exception as e:
            log.error("DB insert failed: %s", e)

    async def _send_join(self, ws):
        try:
            await ws.send(_CM_JOIN)
            self._joined = True
            log.info("Sent /g/cm join — data stream should start")
        except Exception as e:
            log.error("Failed to send join: %s", e)


# ── Entry point (called from main.py) ────────────────────────────────────────

async def run(db_path: str = DB_PATH, duration_hours: float = 8760.0,
              url: Optional[str] = None):
    storage = CrashStorage(db_path)
    n_existing = storage.count()
    log.info("DB: %s  (%d existing rounds)", db_path, n_existing)
    print(f"[ws_collector] DB: {db_path}  ({n_existing:,} rounds)")
    print(f"[ws_collector] Lightweight mode — no browser. ~30 MB RAM.")
    try:
        collector = LightCollector(storage, url=url)
        await collector.run(duration_hours=duration_hours)
    finally:
        storage.close()
