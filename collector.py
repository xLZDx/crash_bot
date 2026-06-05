import ast
import asyncio
import json
import logging
import logging.handlers
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, WebSocket, async_playwright

from config import (
    BET_AMOUNT_KEYS, BET_COUNT_KEYS, COLLECTOR_LOG, DEBUG_WS_LOG,
    DOM_SELECTORS, GAME_URL, LOGIN_TIMEOUT_SECONDS,
    MULTIPLIER_KEYS, ROUND_END_EVENTS,
)

_MULT_KEYS = {k.lower() for k in MULTIPLIER_KEYS}
_BET_AMT   = {k.lower() for k in BET_AMOUNT_KEYS}
_BET_CNT   = {k.lower() for k in BET_COUNT_KEYS}
_ROUND_END = {e.lower() for e in ROUND_END_EVENTS}
_ROUND_ID_KEYS = {"round_id", "roundid", "game_id", "gameid", "hash", "gamehash"}

_MIN_M, _MAX_M = 1.0, 100_000.0
_EVENT_FIELDS  = ("type", "event", "action", "msg_type", "msgtype", "cmd")

# bcgame /g/cm binary Socket.IO frame prefixes
_CM_PREFIX  = b'\x04\x02\x05/g/cm'
_ED_SUFFIX  = b'\x02ed'   # round-end:   field1=round_id, field6=crash*100
_ST_SUFFIX  = b'\x02st'   # round-stats: same + field7=provably-fair hash (64-char hex)
_BET_SUFFIX = b'\x01b'    # bet placed:  field1=round_id, field2=currency, field3=amount, field5=username


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _build_logger(debug: bool) -> logging.Logger:
    log = logging.getLogger("collector")
    if log.handlers:
        return log
    log.setLevel(logging.DEBUG if debug else logging.INFO)
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


# ---------------------------------------------------------------------------
# Binary protobuf helpers (bcgame /g/cm protocol)
# ---------------------------------------------------------------------------

def _decode_varint(data: bytes, pos: int) -> tuple:
    """Decode a protobuf varint starting at pos. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            return result, pos
    return result, pos


def _skip_pb_field(data: bytes, pos: int, wire_type: int) -> int:
    """Skip a protobuf field. Returns new pos."""
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


def _parse_cm_round_end(raw: bytes) -> "FrameResult":
    """
    Parse a /g/cm ed (round-end) or st (round-stats) binary frame.
    Protobuf: field1=round_id, field6=crash*100, field7=hash (st only).
    """
    if not raw.startswith(_CM_PREFIX):
        return _NO_RESULT
    rest = raw[len(_CM_PREFIX):]
    is_st = rest.startswith(_ST_SUFFIX)
    if not (rest.startswith(_ED_SUFFIX) or is_st):
        return _NO_RESULT
    # strip 2-byte prefix (stray 3rd byte is tag 0x64 wire_type=4, harmlessly skipped)
    data = rest[2:]

    game_round_id: Optional[str] = None
    multiplier: Optional[float] = None
    pf_hash: Optional[str] = None
    pos = 0
    while pos < len(data):
        tag_byte = data[pos]; pos += 1
        wire_type = tag_byte & 0x07
        field_num = tag_byte >> 3
        if wire_type == 0:
            val, pos = _decode_varint(data, pos)
            if field_num == 1:
                game_round_id = str(val)
            elif field_num == 6:
                m = val / 100.0
                if _MIN_M <= m <= _MAX_M:
                    multiplier = round(m, 2)
        elif wire_type == 2:
            length, pos = _decode_varint(data, pos)
            raw_val = data[pos:pos + length]
            pos += length
            if field_num == 7 and is_st:
                # provably-fair hash — 64-char hex string
                try:
                    candidate = raw_val.decode("ascii", errors="replace").strip()
                    if len(candidate) == 64 and all(c in "0123456789abcdefABCDEF" for c in candidate):
                        pf_hash = candidate.lower()
                except Exception:
                    pass
        else:
            pos = _skip_pb_field(data, pos, wire_type)
            if pos > len(data):
                break

    if multiplier is None:
        return _NO_RESULT
    return FrameResult(
        multiplier=multiplier,
        total_bets=None,
        num_bettors=None,
        game_round_id=game_round_id,
        frame_event="binary_st" if is_st else "binary_ed",
        hash=pf_hash,
    )


def _parse_bet_frame(raw: bytes) -> Optional["BetResult"]:
    """
    Parse a /g/cm \\x01b (bet-placed) binary frame.
    Protobuf: field1=round_id, field2=currency, field3=amount_str, field5=username.
    """
    if not raw.startswith(_CM_PREFIX):
        return None
    rest = raw[len(_CM_PREFIX):]
    if not rest.startswith(_BET_SUFFIX):
        return None
    data = rest[len(_BET_SUFFIX):]  # \x01b is exactly 2 bytes — strip both

    round_id: Optional[str] = None
    currency: Optional[str] = None
    amount: Optional[float] = None
    username: Optional[str] = None
    pos = 0
    while pos < len(data):
        tag_byte = data[pos]; pos += 1
        wire_type = tag_byte & 0x07
        field_num = tag_byte >> 3
        if wire_type == 0:
            val, pos = _decode_varint(data, pos)
            if field_num == 1:
                round_id = str(val)
        elif wire_type == 2:
            length, pos = _decode_varint(data, pos)
            raw_val = data[pos:pos + length]
            pos += length
            try:
                s = raw_val.decode("utf-8", errors="replace")
                if field_num == 2:
                    currency = s.strip()
                elif field_num == 3:
                    amount = float(s.strip())
                elif field_num == 5:
                    username = s.strip()
            except (ValueError, UnicodeDecodeError):
                pass
        else:
            pos = _skip_pb_field(data, pos, wire_type)
            if pos > len(data):
                break

    if round_id and amount is not None:
        return BetResult(round_id=round_id, currency=currency, amount=amount, username=username)
    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _find_value(obj, keys: set, depth: int = 0) -> Optional[float]:
    if depth > 8:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in keys:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
            r = _find_value(v, keys, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _find_value(item, keys, depth + 1)
            if r is not None:
                return r
    return None


def _find_str(obj, keys: set, depth: int = 0) -> Optional[str]:
    if depth > 8:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in keys and isinstance(v, (str, int)):
                return str(v)
            r = _find_str(v, keys, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _find_str(item, keys, depth + 1)
            if r is not None:
                return r
    return None


def _find_event_type(obj, depth: int = 0) -> Optional[str]:
    if not isinstance(obj, dict) or depth > 3:
        return None
    for field in _EVENT_FIELDS:
        if field in obj:
            return str(obj[field]).lower()
    for v in obj.values():
        r = _find_event_type(v, depth + 1)
        if r is not None:
            return r
    return None


def _sum_bets_array(obj, depth: int = 0) -> Optional[float]:
    if depth > 5 or not isinstance(obj, dict):
        return None
    for k, v in obj.items():
        if k.lower() in ("bets", "players", "bet_list", "betlist") and isinstance(v, list):
            total = 0.0
            found = False
            for item in v:
                if isinstance(item, dict):
                    for ak in ("amount", "bet", "wager", "stake"):
                        if ak in item:
                            try:
                                total += float(item[ak])
                                found = True
                                break
                            except (TypeError, ValueError):
                                pass
            if found:
                return total
        r = _sum_bets_array(v, depth + 1)
        if r is not None:
            return r
    return None


# ---------------------------------------------------------------------------
# FrameResult
# ---------------------------------------------------------------------------

class FrameResult:
    __slots__ = ("multiplier", "total_bets", "num_bettors", "game_round_id", "frame_event", "hash")

    def __init__(self, multiplier, total_bets, num_bettors, game_round_id, frame_event, hash=None):
        self.multiplier    = multiplier
        self.total_bets    = total_bets
        self.num_bettors   = num_bettors
        self.game_round_id = game_round_id
        self.frame_event   = frame_event
        self.hash          = hash


class BetResult:
    __slots__ = ("round_id", "currency", "amount", "username")

    def __init__(self, round_id, currency, amount, username=None):
        self.round_id = round_id
        self.currency = currency
        self.amount   = amount
        self.username = username


_NO_RESULT = FrameResult(None, None, None, None, None)


def parse_frame(payload, require_round_end: bool = True) -> FrameResult:
    """
    Parse one WebSocket frame payload.

    Returns FrameResult.multiplier = None if no crash point found.

    Handles two formats:
    - Binary /g/cm protobuf (bcgame): checks for ed/st round-end frames first.
    - JSON fallback: respects require_round_end filter.
    """
    if isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
        # Try binary bcgame /g/cm round-end frame first
        result = _parse_cm_round_end(raw)
        if result.multiplier is not None:
            return result
        try:
            payload = raw.decode("utf-8", errors="replace")
        except Exception:
            return _NO_RESULT

    if not isinstance(payload, str) or not payload.strip():
        return _NO_RESULT

    # JSON path
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        # Short raw-text fallback: "2.45x" style strings
        if len(payload) < 30:
            m = re.search(r"(\d+\.\d{2,})\s*[xX]?$", payload.strip())
            if m:
                f = float(m.group(1))
                if _MIN_M <= f <= _MAX_M:
                    return FrameResult(f, None, None, None, "raw_text")
        return _NO_RESULT

    event_type = _find_event_type(obj)

    # Filter by event type when required
    if require_round_end and event_type is not None and event_type not in _ROUND_END:
        return _NO_RESULT

    mult = _find_value(obj, _MULT_KEYS)
    if mult is None or not (_MIN_M <= mult <= _MAX_M):
        return _NO_RESULT

    bets_amt = _find_value(obj, _BET_AMT)
    if bets_amt is None:
        bets_amt = _sum_bets_array(obj)
    if bets_amt is not None and bets_amt < 0:
        bets_amt = None

    bets_cnt_raw = _find_value(obj, _BET_CNT)
    bets_cnt = (
        int(bets_cnt_raw) if bets_cnt_raw is not None and 0 < bets_cnt_raw < 100_000
        else None
    )

    game_round_id = _find_str(obj, _ROUND_ID_KEYS)

    return FrameResult(
        multiplier=mult,
        total_bets=bets_amt,
        num_bettors=bets_cnt,
        game_round_id=game_round_id,
        frame_event=event_type or "ws_unknown",
    )


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class _BetAccumulator:
    """Per-round in-memory bet aggregator."""

    def __init__(self, keep_rounds: int = 20):
        self._data: dict = {}   # round_id -> list[(currency, amount, username)]
        self._keep = keep_rounds

    def add(self, b: "BetResult") -> None:
        lst = self._data.setdefault(b.round_id, [])
        lst.append((b.currency, b.amount, b.username))

    def summary(self, round_id: str):
        """Returns (total_usdt, num_bettors) for a round."""
        bets = self._data.get(round_id, [])
        if not bets:
            return None, None
        usdt = sum(amt for cur, amt, _ in bets if cur and cur.upper() in ("USDT", "USD"))
        return (round(usdt, 6) if usdt > 0 else None), len(bets)

    def pop(self, round_id: str):
        """Return and remove bets for a round."""
        return self._data.pop(round_id, [])

    def cleanup(self) -> None:
        if len(self._data) > self._keep * 2:
            for key in sorted(self._data, key=lambda x: int(x or 0))[: -self._keep]:
                del self._data[key]


class CrashCollector:
    def __init__(
        self,
        storage,
        debug_ws: bool = False,
        headless: bool = False,
        login_timeout: int = LOGIN_TIMEOUT_SECONDS,
        dry_run: bool = False,
    ):
        self.storage       = storage
        self.debug_ws      = debug_ws
        self.headless      = headless
        self.login_timeout = login_timeout
        self.dry_run       = dry_run

        self._ws_frames_seen = 0
        self._ws_hits        = 0
        self._dom_hits       = 0
        self._bets           = _BetAccumulator()

        self._debug_path = Path(DEBUG_WS_LOG)
        self._debug_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = _build_logger(debug_ws)

    async def run(self, duration_hours: float = 48.0) -> None:
        loop = asyncio.get_running_loop()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            page = await ctx.new_page()

            def _on_ws(ws: WebSocket) -> None:
                self.log.info("WS opened: %s", ws.url)
                def on_frame(frame_data):
                    loop.create_task(self._handle_frame(frame_data))
                ws.on("framereceived", on_frame)

            page.on("websocket", _on_ws)

            print(f"Opening {GAME_URL} ...")
            await page.goto(GAME_URL, timeout=30_000, wait_until="domcontentloaded")

            if not self.headless:
                print(f"Browser open. Log in if required ({self.login_timeout}s) ...")
                await asyncio.sleep(self.login_timeout)
            else:
                await asyncio.sleep(5)

            start_count = 0 if self.dry_run else self.storage.count()
            last_hit_time = loop.time()
            print(
                f"Collecting for {duration_hours:.1f}h  "
                f"(existing: {start_count:,} rounds)  -- Ctrl-C to stop"
            )

            deadline = loop.time() + duration_hours * 3600
            tick = 0

            while loop.time() < deadline:
                await asyncio.sleep(2)
                tick += 1
                now = loop.time()

                if self._ws_hits > 0:
                    last_hit_time = now

                # DOM fallback activates only when WS produces zero hits after 120s
                if self._ws_hits == 0 and tick > 60:
                    await self._dom_tick(page)

                # Staleness warning: no round for 5+ minutes
                if (self._ws_hits + self._dom_hits) > 0 and (now - last_hit_time) > 300:
                    self.log.warning("No new round in 5 min -- WS may be stale")
                    last_hit_time = now  # suppress repeat warning for 5 more min

                if tick % 150 == 0:
                    total = self._ws_hits + self._dom_hits if self.dry_run else self.storage.count()
                    elapsed_h = (tick * 2) / 3600
                    rate = total / elapsed_h if elapsed_h else 0
                    print(
                        f"  [{datetime.now().strftime('%H:%M:%S')}]"
                        f"  total={total:,}"
                        f"  ws_frames={self._ws_frames_seen}"
                        f"  ws_hits={self._ws_hits}"
                        f"  dom={self._dom_hits}"
                        f"  ~{rate:.0f}/h"
                    )

            await browser.close()

        total_final = (self._ws_hits + self._dom_hits) if self.dry_run else self.storage.count()
        print(f"\nDone. Total rounds: {total_final:,}")

    async def _handle_frame(self, frame_data) -> None:
        try:
            if isinstance(frame_data, dict):
                payload = frame_data.get("payload", "")
            elif hasattr(frame_data, "payload"):
                payload = frame_data.payload
            else:
                payload = str(frame_data)

            # Playwright delivers binary frames as str() repr — restore to bytes.
            if isinstance(payload, str) and payload.startswith(("b'", 'b"')):
                try:
                    payload = ast.literal_eval(payload)
                except Exception:
                    pass

            self._ws_frames_seen += 1

            if self.debug_ws:
                with open(self._debug_path, "a", encoding="utf-8", errors="replace") as fh:
                    fh.write(f"{datetime.now().isoformat()}  {str(payload)[:500]}\n")

            # ── Bet frame (\x01b) ────────────────────────────────────────────
            if isinstance(payload, (bytes, bytearray)):
                bet = _parse_bet_frame(bytes(payload))
                if bet is not None:
                    self._bets.add(bet)
                    if not self.dry_run:
                        self.storage.insert_bet(
                            round_id=bet.round_id,
                            currency=bet.currency,
                            amount=bet.amount,
                            # username intentionally omitted — never stored (privacy)
                        )
                    if self.debug_ws:
                        print(f"  [BET]  round={bet.round_id}  {bet.currency} {bet.amount}", flush=True)
                    return

            # ── Round-end frame (\x02ed / \x02st) ───────────────────────────
            result = parse_frame(payload, require_round_end=not self.debug_ws)
            if result.multiplier is None:
                return

            self._ws_hits += 1

            # Pull accumulated bets for this round
            if result.game_round_id:
                t_bets, n_bettors = self._bets.summary(result.game_round_id)
                # Prefer bet data from accumulator over frame's own fields
                if t_bets is not None:
                    result.total_bets = t_bets
                if n_bettors is not None:
                    result.num_bettors = n_bettors

            self._bets.cleanup()

            if self.dry_run:
                bet_str = f"  bets={result.total_bets:.4f}" if result.total_bets else ""
                hash_str = f"  hash={result.hash[:8]}..." if result.hash else ""
                print(
                    f"  [DRY-WS]  {result.multiplier:.2f}x"
                    f"  event={result.frame_event}"
                    f"  round={result.game_round_id}"
                    f"{bet_str}{hash_str}",
                    flush=True,
                )
                return

            rid = self.storage.insert(
                multiplier=result.multiplier,
                source="ws",
                game_round_id=result.game_round_id,
                total_bets=result.total_bets,
                num_bettors=result.num_bettors,
                frame_event=result.frame_event,
                hash=result.hash,
            )
            if rid is not None:
                bet_str = f"  bets={result.total_bets:.4f}" if result.total_bets else ""
                hash_str = f"  hash={result.hash[:8]}..." if result.hash else ""
                print(f"  [WS]  #{rid:>5}  {result.multiplier:.2f}x{bet_str}{hash_str}", flush=True)
            elif result.hash and result.game_round_id:
                # Round already exists (from \x02ed) — update its hash from \x02st
                self.storage.update_hash(result.game_round_id, result.hash)
            else:
                self.log.debug("Duplicate skipped: game_round_id=%s", result.game_round_id)

        except Exception:
            self.log.error("Error in _handle_frame", exc_info=True)

    async def _dom_tick(self, page: Page) -> None:
        for sel in DOM_SELECTORS:
            try:
                el = await page.query_selector(sel)
                if not el:
                    continue
                text = await el.text_content() or ""
                match = re.search(r"(\d+\.\d{2,})\s*[xX]?", text)
                if not match:
                    continue
                val = float(match.group(1))
                if not (_MIN_M <= val <= _MAX_M):
                    continue
                if val == self.storage.last_multiplier():
                    return  # same round, not yet updated

                self._dom_hits += 1

                if self.dry_run:
                    print(f"  [DRY-DOM]  {val:.2f}x  selector={sel!r}")
                    return

                rid = self.storage.insert(val, source="dom")
                if rid is not None:
                    print(f"  [DOM]  #{rid:>5}  {val:.2f}x")
                return

            except Exception:
                self.log.debug("DOM selector %r failed", sel, exc_info=True)


if __name__ == "__main__":
    import argparse
    from config import DB_PATH

    ap = argparse.ArgumentParser(description="Crash-game collector")
    ap.add_argument("--db",        default=DB_PATH,  help="DuckDB path")
    ap.add_argument("--hours",     type=float, default=48.0, help="Run duration in hours")
    ap.add_argument("--headless",  action="store_true", default=True)
    ap.add_argument("--no-headless", dest="headless", action="store_false")
    ap.add_argument("--debug-ws",  action="store_true")
    ap.add_argument("--dry-run",   action="store_true")
    args = ap.parse_args()

    from storage import CrashStorage
    db = CrashStorage(args.db)
    collector = CrashCollector(
        storage=db,
        debug_ws=args.debug_ws,
        headless=args.headless,
        dry_run=args.dry_run,
    )
    asyncio.run(collector.run(duration_hours=args.hours))
