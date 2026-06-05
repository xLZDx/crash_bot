"""
Real-money relwave_bad_veto_21 martingale bot for BCGame crash.
Run LOCALLY on Windows (VPS IPs are blocked by BCGame for gameplay).

Bank:    $24.83 USDT
Bet:     $0.01 base (BCGame minimum)
Cashout: 2.1x with hot->cold relative-wave veto
Cooldown: 4 consecutive losses -> ~2 min pause
"""
import json
import base64
import sys
import time
import os
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import sys as _sys
import duckdb
from bot_strategy import STRATEGIES, relative_bad_veto_matches
if _sys.platform.startswith("win"):
    SESSION_FILE = Path(r"D:\tmp\bcgame_session.json")
    STATE_FILE   = Path(r"D:\tmp\realbet_state.json")
    LOG_FILE     = Path(r"D:\tmp\realbet.log")
    STOP_FLAG    = Path(r"D:\tmp\realbet_stop.flag")
else:  # VPS Linux
    SESSION_FILE = Path("/root/crash-collector/bcgame_session.json")
    STATE_FILE   = Path("/root/crash-collector/data/realbet_state.json")
    LOG_FILE     = Path("/root/crash-collector/logs/bot_realbet.log")
    STOP_FLAG    = Path("/root/crash-collector/data/bot_stop_realbet.flag")

ROUNDS_DB = STATE_FILE.parent / "crash.duckdb"
AUDIT_FILE = STATE_FILE.parent / "realbet_execution_audit.jsonl"
CAPTURE_FILE = STATE_FILE.parent / "realbet_network_capture.jsonl"
STRATEGY_START_FILE = STATE_FILE.parent / "realbet_strategy_start.json"

# API balance cache (updated by response interceptor)
_balance_from_api: list = [None]
_last_bet_reject: list = [0.0, ""]
# In-memory cache of last WS round result (from framereceived, no DB needed)
_ws_round_cache: dict = {}  # {game_round_id: str -> mult: float, ts: float}
_ws_betting_phase: dict = {}  # {game_round_id: str -> True} when betting phase is open

# WS frame constants (from BCGame /g/cm protocol)
_CM_PREFIX = b"/g/cm"
_ED_SUFFIX = b"ed"
_ST_SUFFIX = b"st"

# Strategy
BANK_USD         = 24.83
BASE_BET_USD     = 0.01      # $0.01 = BCGame minimum
# LOSS_SCALE / MAX_SCALE / CONSEC_TRIGGER / COOLDOWN_ROUNDS are the strategy's
# martingale params and come ONLY from STRATEGY_CFG below (lines ~69-72).
# Do NOT hardcode duplicates here -- a stale copy (e.g. MAX_SCALE=64) is dead
# code (overwritten below) but misleads readers into thinking the cap differs
# from the configured/backtested strategy. Single source of truth = STRATEGY_CFG.
STOP_LOSS_PCT    = 0.50      # stop at -$6
TAKE_PROFIT_PCT  = 1.0       # stop at +$12
INPUT_RETRIES    = 5
ROUND_SETTLE_TIMEOUT = 95
EXECUTOR         = os.environ.get("REALBET_EXECUTOR", "gui").strip().lower()
BET_CURRENCY     = os.environ.get("REALBET_CURRENCY", "USDT").strip().upper()

# relwave_bad_veto_21: skip if a local hot wave has flipped into a cold wave.
STRATEGY_NAME    = "relwave_bad_veto_21"
STRATEGY_CFG     = STRATEGIES[STRATEGY_NAME]
CASHOUT          = STRATEGY_CFG.cashout
LOSS_SCALE       = STRATEGY_CFG.loss_scale
MAX_SCALE        = STRATEGY_CFG.max_scale
CONSEC_TRIGGER   = STRATEGY_CFG.consec_loss_trigger
COOLDOWN_ROUNDS  = STRATEGY_CFG.consec_loss_pause
REL_BASE_LEN     = STRATEGY_CFG.rel_base_len
REL_HIGH_LEN     = STRATEGY_CFG.rel_high_len
REL_LOW_LEN      = STRATEGY_CFG.rel_low_len
REL_HIGH_MARGIN  = STRATEGY_CFG.rel_high_margin
REL_LOW_MARGIN   = STRATEGY_CFG.rel_low_margin
REL_MIN_DELTA    = STRATEGY_CFG.rel_min_delta


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"scale": 1.0, "consec": 0, "cooldown": 0,
            "bets": 0, "wins": 0, "pnl": 0.0}


def _save_state(st):
    STATE_FILE.write_text(json.dumps(st, indent=2))


def _audit(event: str, **fields):
    try:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "strategy": STRATEGY_NAME,
            **fields,
        }
        with open(AUDIT_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _capture(event: str, **fields):
    try:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        with open(CAPTURE_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _short(value, limit: int = 1600):
    text = "" if value is None else str(value)
    if len(text) > limit:
        return text[:limit] + f"...<truncated {len(text) - limit}>"
    return text


def _payload_capture(payload, limit: int = 2000) -> dict:
    if isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
        return {
            "payload_type": "bytes",
            "payload_len": len(raw),
            "payload_b64": base64.b64encode(raw[:limit]).decode("ascii"),
            "payload_hex": raw[:min(limit, 256)].hex(),
            "truncated": len(raw) > limit,
        }
    return {
        "payload_type": "text",
        "payload": _short(payload, limit),
        "payload_len": len(str(payload or "")),
    }


def _redact_headers(headers: dict | None) -> dict:
    if not headers:
        return {}
    redacted = {}
    for key, value in headers.items():
        lk = key.lower()
        if lk in ("cookie", "authorization", "x-token", "token", "x-csrf-token", "smid"):
            redacted[key] = "<redacted>"
        else:
            redacted[key] = _short(value, 300)
    return redacted


def _capture_interesting_url(url: str) -> bool:
    low = url.lower()
    if any(skip in low for skip in (
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".woff", ".woff2",
        "google", "gstatic", "intercom", "sentry", "analytics", "tracking",
    )):
        return False
    return any(key in low for key in (
        "crash", "game", "bet", "cash", "amount", "balance", "wallet",
        "user", "socket", "ws", "round",
    ))


def _install_page_send_probe(page):
    """Capture browser-side send calls before Playwright sees serialized frames."""
    try:
        page.expose_function("__realbetCapture", lambda row: _capture("page_probe", **row))
        page.add_init_script(
            r"""
(() => {
  if (window.__realbetProbeInstalled) return;
  window.__realbetProbeInstalled = true;

  const interesting = (url) => {
    const low = String(url || '').toLowerCase();
    return ['crash', 'game', 'bet', 'cash', 'amount', 'balance', 'wallet',
            'user', 'socket', 'ws', 'round'].some((k) => low.includes(k));
  };

  const short = (value, limit = 3000) => {
    const text = value == null ? '' : String(value);
    return text.length > limit ? text.slice(0, limit) + `...<truncated ${text.length - limit}>` : text;
  };

  const bytesToB64 = (bytes, limit = 4096) => {
    const slice = bytes.slice(0, Math.min(bytes.length, limit));
    let s = '';
    for (let i = 0; i < slice.length; i += 1) s += String.fromCharCode(slice[i]);
    return btoa(s);
  };

  const describePayload = (payload) => {
    try {
      if (payload instanceof ArrayBuffer) {
        const bytes = new Uint8Array(payload);
        return {payload_type: 'arraybuffer', payload_len: bytes.length, payload_b64: bytesToB64(bytes)};
      }
      if (ArrayBuffer.isView(payload)) {
        const bytes = new Uint8Array(payload.buffer, payload.byteOffset, payload.byteLength);
        return {payload_type: payload.constructor.name, payload_len: bytes.length, payload_b64: bytesToB64(bytes)};
      }
      if (payload instanceof Blob) {
        return {payload_type: 'blob', payload_len: payload.size, mime: payload.type || ''};
      }
      return {payload_type: typeof payload, payload_len: String(payload ?? '').length, payload: short(payload)};
    } catch (err) {
      return {payload_type: 'inspect_error', error: String(err)};
    }
  };

  const parseGamePacket = (payload) => {
    try {
      let bytes = null;
      if (payload instanceof ArrayBuffer) {
        bytes = new Uint8Array(payload);
      } else if (ArrayBuffer.isView(payload)) {
        bytes = new Uint8Array(payload.buffer, payload.byteOffset, payload.byteLength);
      }
      if (!bytes || bytes.length < 8 || bytes[0] !== 4) return null;
      const packetId = bytes[5] || 0;
      let i = 6;
      const pathLen = bytes[i++];
      const path = new TextDecoder().decode(bytes.slice(i, i + pathLen));
      i += pathLen;
      const eventLen = bytes[i++];
      const event = new TextDecoder().decode(bytes.slice(i, i + eventLen));
      return {packet_id: packetId, path, event, payload_len: bytes.length};
    } catch (_) {
      return null;
    }
  };

  const encodeVarint = (value) => {
    let v = Number(value) >>> 0;
    const out = [];
    while (v > 127) {
      out.push((v & 127) | 128);
      v = v >>> 7;
    }
    out.push(v);
    return out;
  };

  const utf8Bytes = (text) => Array.from(new TextEncoder().encode(String(text)));

  const buildTwiceBetPacket = ({packetId, gameRoundId, solAmount, cashoutInt, tickK, currency}) => {
    const cur = currency || 'SOL';
    const curBytes = utf8Bytes(cur);
    const body = [];
    body.push(...encodeVarint((1 << 3) | 0), ...encodeVarint(gameRoundId));
    body.push(...encodeVarint((2 << 3) | 2), curBytes.length, ...curBytes);
    const amountBytes = utf8Bytes(solAmount);
    body.push(...encodeVarint((3 << 3) | 2), ...encodeVarint(amountBytes.length), ...amountBytes);
    body.push(...encodeVarint((4 << 3) | 0), ...encodeVarint(cashoutInt));
    body.push(...encodeVarint((6 << 3) | 0), 1);
    const now = Date.now();
    const field15 = (2 * now + Number(tickK || 2230288382)) >>> 0;
    body.push(...encodeVarint((15 << 3) | 0), ...encodeVarint(field15));
    body.push(...encodeVarint((16 << 3) | 0), 2);
    const packet = [
      4, 0x82, 0, 0, 0, Number(packetId) & 255,
      5, ...utf8Bytes('/g/cm'),
      9, ...utf8Bytes('twice-bet'),
      ...body,
    ];
    return {bytes: new Uint8Array(packet), now, field15};
  };

  window.__realbetDirectTwiceBet = (params) => {
    if (!window.__realbetGameWS || window.__realbetGameWS.readyState !== WebSocket.OPEN) {
      throw new Error('game_ws_not_open');
    }
    const state = window.__realbetWsState || {};
    const packetId = params.packetId ?? (((state.last_packet_id ?? 0) + 1) & 255);
    const built = buildTwiceBetPacket({
      packetId,
      gameRoundId: params.gameRoundId,
      solAmount: params.solAmount,
      cashoutInt: params.cashoutInt,
      tickK: params.tickK,
      currency: params.currency,
    });
    window.__realbetGameWS.send(built.bytes.buffer);
    return {
      packet_id: packetId,
      game_round_id: params.gameRoundId,
      sol_amount: String(params.solAmount),
      cashout_int: params.cashoutInt,
      field15: built.field15,
      browser_now_ms: built.now,
      payload_b64: bytesToB64(built.bytes, 4096),
    };
  };

  const emit = (row) => {
    try {
      if (window.__realbetCapture) {
        window.__realbetCapture({
          page_url: location.href,
          browser_now_ms: Date.now(),
          browser_perf_ms: Math.round(performance.now() * 1000) / 1000,
          stack: short((new Error()).stack, 1200),
          ...row,
        });
      }
    } catch (_) {}
  };

  const origSend = WebSocket.prototype.send;
  WebSocket.prototype.send = function(data) {
    try {
      if (interesting(this.url)) {
        if (String(this.url || '').includes('socketv4') && location.href.includes('/game/crash')) {
          window.__realbetGameWS = this;
        }
        const packet = parseGamePacket(data);
        const payloadInfo = describePayload(data);
        if (packet && packet.path === '/g/cm') {
          window.__realbetWsState = {
            ws_url: this.url,
            ready_state: this.readyState,
            last_packet_id: packet.packet_id,
            last_path: packet.path,
            last_event: packet.event,
            last_payload_b64: payloadInfo.payload_b64 || "",
            last_browser_now_ms: Date.now(),
            last_send_ms: Date.now(),
          };
        }
        emit({kind: 'websocket_send_call', ws_url: this.url, ...payloadInfo});
      }
    } catch (_) {}
    return origSend.apply(this, arguments);
  };

  const origFetch = window.fetch;
  window.fetch = function(input, init) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url);
      if (interesting(url)) {
        emit({
          kind: 'fetch_call',
          url,
          method: (init && init.method) || (input && input.method) || 'GET',
          body: init && init.body ? short(init.body) : '',
        });
      }
    } catch (_) {}
    return origFetch.apply(this, arguments);
  };

  const origOpen = XMLHttpRequest.prototype.open;
  const origXhrSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__realbetMethod = method;
    this.__realbetUrl = url;
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function(body) {
    try {
      if (interesting(this.__realbetUrl)) {
        emit({
          kind: 'xhr_send_call',
          url: this.__realbetUrl,
          method: this.__realbetMethod || '',
          body: short(body),
        });
      }
    } catch (_) {}
    return origXhrSend.apply(this, arguments);
  };
})();
            """
        )
    except Exception as exc:
        _capture("page_probe_install_error", error=str(exc))


def _install_network_capture(page):
    _install_page_send_probe(page)

    def on_request(req):
        try:
            if not _capture_interesting_url(req.url):
                return
            post = None
            try:
                post = req.post_data
            except Exception:
                pass
            _capture(
                "request",
                method=req.method,
                url=req.url,
                resource_type=req.resource_type,
                headers=_redact_headers(req.headers),
                post_data=_short(post),
            )
        except Exception:
            pass

    def on_response(resp):
        try:
            if not _capture_interesting_url(resp.url):
                return
            body = None
            content_type = ""
            try:
                content_type = resp.headers.get("content-type", "")
                if any(t in content_type.lower() for t in ("json", "text")):
                    body = resp.text()
            except Exception:
                body = None
            _capture(
                "response",
                status=resp.status,
                url=resp.url,
                headers=_redact_headers(resp.headers),
                body=_short(body),
            )
        except Exception:
            pass

    def on_websocket(ws):
        try:
            if not _capture_interesting_url(ws.url):
                return
            _capture("websocket_open", url=ws.url)

            def on_sent(payload):
                _capture("ws_sent", url=ws.url, **_payload_capture(payload, 4000))

            def on_received(payload):
                text = _short(payload, 2000)
                low = text.lower()
                if "bet suspended" in low or "insufficient" in low or "bet failed" in low:
                    _last_bet_reject[0] = time.time()
                    _last_bet_reject[1] = text
                # Parse round-end frames directly from WS (bypass DB latency)
                try:
                    raw_bytes = payload if isinstance(payload, bytes) else (
                        payload.encode("latin-1", errors="replace") if isinstance(payload, str) else b""
                    )
                    gid, mult = _parse_ws_round_frame(raw_bytes)
                    if gid and mult is not None:
                        _ws_round_cache[gid] = {"mult": mult, "ts": time.time()}
                        # Keep cache small (last 20 rounds)
                        if len(_ws_round_cache) > 20:
                            oldest = min(_ws_round_cache, key=lambda k: _ws_round_cache[k]["ts"])
                            del _ws_round_cache[oldest]
                except Exception:
                    pass
                if any(k in low for k in ("crash", "bet", "cash", "round", "balance", "game")):
                    _capture("ws_received", url=ws.url, **_payload_capture(payload, 4000))

            ws.on("framesent", on_sent)
            ws.on("framereceived", on_received)
            ws.on("close", lambda: _capture("websocket_close", url=ws.url))
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("websocket", on_websocket)


def _parse_ws_round_frame(raw: bytes) -> tuple[str | None, float | None]:
    """Delegates to ws_protocol.parse_round_frame (shared with live_feed.py).
    Behavior unchanged: returns (game_round_id, multiplier) or (None, None)."""
    from ws_protocol import parse_round_frame
    gid, mult, _suffix = parse_round_frame(raw)
    return gid, mult


def _latest_round_info(retries: int = 6, backoff_s: float = 0.10) -> dict:
    # The collector (main.py collect) holds a brief write-lock on crash.duckdb
    # while it writes each round. A read here that collides with that window
    # raises "Could not set lock ... Conflicting lock is held". Previously that
    # bubbled up as round_error -> the whole bet was skipped (~28% of attempts).
    # The lock is transient, so retry a few times (<=0.6s total, well within the
    # betting window) and only surface round_error if it stays locked throughout.
    last_exc = ""
    for attempt in range(retries):
        try:
            with duckdb.connect(str(ROUNDS_DB), read_only=True) as conn:
                row = conn.execute(
                    """
                    SELECT id, game_round_id, multiplier
                    FROM rounds
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
            if not row:
                return {}
            return {"round_id": int(row[0]), "game_round_id": row[1], "last_mult": float(row[2])}
        except Exception as exc:
            last_exc = str(exc)
            low = last_exc.lower()
            if "lock" in low or "conflicting" in low:
                if attempt < retries - 1:
                    time.sleep(backoff_s)
                    continue
            else:
                # Non-lock error (e.g. corrupt/missing DB): do not spin.
                return {"round_error": last_exc}
    return {"round_error": last_exc}


def _wait_for_settled_round(after_round_id, timeout=ROUND_SETTLE_TIMEOUT,
                             bet_game_round_id=None, page=None):
    """Wait until round result available: JS WS cache -> Python WS cache -> DB."""
    deadline = time.time() + timeout
    target = str(int(bet_game_round_id) + 1) if bet_game_round_id else None
    while time.time() < deadline:
        if target:
            # 1. Python in-memory WS cache (from framereceived)
            if target in _ws_round_cache:
                c = _ws_round_cache[target]
                return {"round_id": None, "game_round_id": target,
                        "last_mult": c["mult"], "source": "ws_cache_py"}
            # 2. JS cache (from injected WebSocket interceptor in page)
            if page:
                try:
                    js_r = page.evaluate("(t) => (window.__realbetRoundCache || {})[t] || null", target)
                    if js_r and js_r.get("mult"):
                        m = float(js_r["mult"])
                        _ws_round_cache[target] = {"mult": m, "ts": time.time()}
                        return {"round_id": None, "game_round_id": target,
                                "last_mult": m, "source": "ws_cache_js"}
                except Exception:
                    pass
        # 3. DB fallback
        info = _latest_round_info()
        rid = info.get("round_id")
        if rid and after_round_id and rid > after_round_id:
            info["source"] = "db"
            return info
        time.sleep(1)
    return {"round_error": f"settle_timeout_after:{after_round_id}"}


def _parse_float(value: str | None) -> float | None:
    try:
        return float(str(value or "").replace(",", "").replace("x", "").strip())
    except Exception:
        return None


def _set_input(page, input_handle, value: str):
    input_handle.scroll_into_view_if_needed()
    try:
        input_handle.fill(value)
    except Exception:
        input_handle.click()
        page.keyboard.press("Control+a")
        page.keyboard.type(value)
    try:
        input_handle.evaluate(
            """(el, value) => {
                el.value = value;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.blur();
            }""",
            value,
        )
    except Exception:
        pass
    page.keyboard.press("Tab")
    page.wait_for_timeout(250)


def _visible_inputs(page) -> list:
    found = []
    for idx, handle in enumerate(page.query_selector_all("input")):
        try:
            box = handle.bounding_box()
            if not box or box.get("width", 0) < 20 or box.get("height", 0) < 10:
                continue
            visible = handle.evaluate("""el => {
                const s = window.getComputedStyle(el);
                return s && s.visibility !== 'hidden' && s.display !== 'none' && !el.disabled;
            }""")
            if not visible:
                continue
            value = handle.input_value()
            found.append({"idx": idx, "handle": handle, "box": box, "value": value})
        except Exception:
            continue
    return found


def _bet_control_inputs(page) -> tuple:
    """Return visible amount/cashout inputs from the game form, left to right."""
    inputs = _visible_inputs(page)
    if len(inputs) < 2:
        return None, None, f"visible_inputs:{[(i['idx'], i['value'], i['box']) for i in inputs]}"

    max_y = max(i["box"].get("y", 0) for i in inputs)
    lower = [i for i in inputs if i["box"].get("y", 0) >= max_y - 120]
    if len(lower) < 2:
        lower = inputs
    lower.sort(key=lambda i: (i["box"].get("y", 0), i["box"].get("x", 0)))
    row_y = lower[-1]["box"].get("y", 0)
    same_row = [i for i in lower if abs(i["box"].get("y", 0) - row_y) < 80]
    same_row.sort(key=lambda i: i["box"].get("x", 0))
    if len(same_row) < 2:
        return None, None, f"input_row:{[(i['idx'], i['value'], i['box']) for i in inputs]}"
    amount = same_row[0]
    cashout = same_row[-1]
    diag = f"amount_idx={amount['idx']} cashout_idx={cashout['idx']} visible={[(i['idx'], i['value'], round(i['box'].get('x',0)), round(i['box'].get('y',0))) for i in inputs]}"
    return amount["handle"], cashout["handle"], diag


def _ensure_bet_controls(page, bet_str: str) -> tuple[bool, str]:
    """Set and verify amount + cashout before click. Fail closed if UI disagrees."""
    cashout_str = str(CASHOUT)
    last = "not_started"
    for attempt in range(INPUT_RETRIES):
        try:
            amount_input, cashout_input, diag = _bet_control_inputs(page)
            if not amount_input or not cashout_input:
                last = diag
                page.wait_for_timeout(400)
                continue

            _set_input(page, amount_input, bet_str)
            _set_input(page, cashout_input, cashout_str)

            amount_val = amount_input.input_value()
            cashout_val = cashout_input.input_value()
            amount = _parse_float(amount_val)
            cashout = _parse_float(cashout_val)
            amount_ok = amount is not None and abs(amount - float(bet_str)) < 0.000001
            cashout_ok = cashout is not None and abs(cashout - CASHOUT) < 0.000001
            if amount_ok and cashout_ok:
                return True, f"amount={amount_val} cashout={cashout_val} {diag}"
            last = f"verify_failed amount={amount_val} cashout={cashout_val} {diag}"
        except Exception as exc:
            last = str(exc)
        page.wait_for_timeout(500)
    return False, last


def _direct_ws_status(page) -> dict:
    try:
        return page.evaluate(
            """() => {
                const s = window.__realbetWsState || {};
                return {
                    has_ws: !!window.__realbetGameWS,
                    ready_state: window.__realbetGameWS ? window.__realbetGameWS.readyState : null,
                    last_packet_id: s.last_packet_id ?? null,
                    last_event: s.last_event || "",
                    last_payload_b64: s.last_payload_b64 || "",
                    last_browser_now_ms: s.last_browser_now_ms || null,
                    last_send_ms: s.last_send_ms || null,
                };
            }"""
        )
    except Exception as exc:
        return {"error": str(exc)}


def _read_estimated_sol_amount(page) -> str | None:
    try:
        return page.evaluate(
            r"""() => {
                const text = document.body ? document.body.innerText : "";
                const matches = [...text.matchAll(/≈\s*([0-9]+(?:\.[0-9]+)?)\s*SOL/g)];
                if (!matches.length) return null;
                return matches[matches.length - 1][1];
            }"""
        )
    except Exception:
        return None


def _recent_usd_to_sol_rate() -> float | None:
    try:
        if not AUDIT_FILE.exists():
            return None
        for line in reversed(AUDIT_FILE.read_text(errors="ignore").splitlines()[-2000:]):
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("event") != "controls_verified":
                continue
            bet = row.get("bet")
            sol = row.get("estimated_sol")
            if bet and sol:
                rate = float(sol) / float(bet)
                if 0.005 <= rate <= 0.03:
                    return rate
    except Exception:
        pass
    return None


def _scale_from_audit() -> float:
    """Derive current martingale scale from last played game results in audit log.
    Counts consecutive losses since last win. Ignores vetoed/skipped rounds.
    """
    try:
        lines = AUDIT_FILE.read_text(errors="ignore").splitlines()
        consec = 0
        for line in reversed(lines[-500:]):
            try:
                r = json.loads(line)
                event = r.get("event", "")
                result = r.get("result", "")
                if event in ("result", "result_db", "result_balance"):
                    if result in ("win",):
                        break  # last played game was win -> stop
                    elif result in ("loss", "unknown_as_loss"):
                        consec += 1
            except Exception:
                pass
        scale = min(LOSS_SCALE ** consec, MAX_SCALE)
        return scale
    except Exception:
        return st_fallback_scale if False else 1.0


def _sol_amount_for_usd_bet(usd_bet: float) -> str | None:
    if BET_CURRENCY == "USDT":
        return f"{float(usd_bet):.6f}"
    rate = _recent_usd_to_sol_rate()
    if rate is None:
        rate = 0.0124  # fallback near the latest observed BCGame SOL conversion
    amount = max(float(usd_bet) * rate, 0.000001)
    return f"{amount:.6f}"


WS_TICK_K = 2230288382


def _enc_varint(value: int) -> bytes:
    out = []
    value = int(value)
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _read_varint(raw: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(raw):
        b = raw[offset]
        offset += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, offset
        shift += 7
    raise ValueError("varint_eof")


def _build_twice_bet_packet(packet_id: int, game_round_id: int, sol_amount: str,
                            cashout: float, browser_now_ms: int,
                            tick_k: int = WS_TICK_K) -> bytes:
    body = b""
    body += _enc_varint((1 << 3) | 0) + _enc_varint(int(game_round_id))
    cur_bytes = BET_CURRENCY.encode("utf-8")
    body += _enc_varint((2 << 3) | 2) + _enc_varint(len(cur_bytes)) + cur_bytes
    amount_bytes = str(sol_amount).encode("utf-8")
    body += _enc_varint((3 << 3) | 2) + _enc_varint(len(amount_bytes)) + amount_bytes
    body += _enc_varint((4 << 3) | 0) + _enc_varint(int(round(cashout * 100)))
    body += _enc_varint((6 << 3) | 0) + _enc_varint(1)
    field15 = (2 * int(browser_now_ms) + int(tick_k)) & 0xFFFFFFFF
    body += _enc_varint((15 << 3) | 0) + _enc_varint(field15)
    body += _enc_varint((16 << 3) | 0) + _enc_varint(2)
    return bytes([4, 0x82, 0, 0, 0, int(packet_id) & 0xFF, 5]) + b"/g/cm" + bytes([9]) + b"twice-bet" + body


def _parse_twice_bet_packet_b64(payload_b64: str | None) -> dict:
    if not payload_b64:
        return {}
    raw = base64.b64decode(payload_b64)
    if b"twice-bet" not in raw or len(raw) < 20:
        return {"raw_len": len(raw), "event": "not_twice_bet"}
    offset = 5
    packet_id = raw[offset]
    offset += 1
    path_len = raw[offset]
    offset += 1
    path = raw[offset:offset + path_len].decode("utf-8", "ignore")
    offset += path_len
    event_len = raw[offset]
    offset += 1
    event = raw[offset:offset + event_len].decode("utf-8", "ignore")
    offset += event_len
    fields = {}
    while offset < len(raw):
        key, offset = _read_varint(raw, offset)
        field = key >> 3
        wire = key & 7
        if wire == 0:
            fields[field], offset = _read_varint(raw, offset)
        elif wire == 2:
            size, offset = _read_varint(raw, offset)
            fields[field] = raw[offset:offset + size].decode("utf-8", "ignore")
            offset += size
        else:
            fields[field] = f"wire_{wire}"
            break
    return {
        "raw_b64": payload_b64,
        "raw_hex": raw.hex(),
        "raw_len": len(raw),
        "packet_id": packet_id,
        "path": path,
        "event": event,
        "game_round_id": fields.get(1),
        "currency": fields.get(2),
        "sol_amount": fields.get(3),
        "cashout_int": fields.get(4),
        "field15": fields.get(15),
        "field16": fields.get(16),
    }


def _ws_shadow_compare(pre_ws: dict, post_ws: dict, round_info: dict, estimated_sol: str | None) -> dict:
    try:
        actual = _parse_twice_bet_packet_b64(post_ws.get("last_payload_b64"))
        browser_now = post_ws.get("last_browser_now_ms")
        if not actual or actual.get("event") != "twice-bet" or not browser_now:
            return {"ok": False, "reason": "no_actual_twice_bet", "post_ws": post_ws}

        def find_match(packet_id, game_round_id, sol_amount, cashout):
            for dt_ms in (0, -1, 1, -5, 5, -10, 10, -25, 25, -50, 50, -80, 80):
                for tick_k in (WS_TICK_K, WS_TICK_K + 2, WS_TICK_K - 2):
                    candidate = _build_twice_bet_packet(
                        packet_id,
                        game_round_id,
                        sol_amount,
                        cashout,
                        int(browser_now) + dt_ms,
                        tick_k,
                    )
                    candidate_b64 = base64.b64encode(candidate).decode("ascii")
                    if candidate_b64 == actual["raw_b64"]:
                        return {
                            "match": True,
                            "b64": candidate_b64,
                            "dt_ms": dt_ms,
                            "tick_k": tick_k,
                        }
            candidate = _build_twice_bet_packet(
                packet_id,
                game_round_id,
                sol_amount,
                cashout,
                int(browser_now),
                WS_TICK_K,
            )
            return {
                "match": False,
                "b64": base64.b64encode(candidate).decode("ascii"),
                "dt_ms": 0,
                "tick_k": WS_TICK_K,
            }

        actual_match = find_match(
            actual["packet_id"],
            actual["game_round_id"],
            actual["sol_amount"],
            (actual["cashout_int"] or 0) / 100,
        )
        rebuilt_actual_match = actual_match["match"]

        pre_packet = pre_ws.get("last_packet_id")
        expected_packet = (int(pre_packet) + 1) & 0xFF if pre_packet is not None else actual["packet_id"]
        expected_game = None
        if round_info.get("game_round_id") is not None:
            expected_game = int(round_info["game_round_id"]) + 1

        expected_match = False
        expected_b64 = None
        expected_dt_ms = None
        expected_tick_k = None
        if expected_game and estimated_sol:
            expected = find_match(
                expected_packet,
                expected_game,
                str(estimated_sol),
                CASHOUT,
            )
            expected_b64 = expected["b64"]
            expected_match = expected["match"]
            expected_dt_ms = expected["dt_ms"]
            expected_tick_k = expected["tick_k"]

        return {
            "ok": rebuilt_actual_match,
            "rebuilt_actual_match": rebuilt_actual_match,
            "rebuilt_actual_dt_ms": actual_match["dt_ms"],
            "rebuilt_actual_tick_k": actual_match["tick_k"],
            "expected_match": expected_match,
            "expected_dt_ms": expected_dt_ms,
            "expected_tick_k": expected_tick_k,
            "expected_packet_id": expected_packet,
            "expected_game_round_id": expected_game,
            "estimated_sol": estimated_sol,
            "actual": {k: v for k, v in actual.items() if k not in ("raw_b64", "raw_hex")},
            "actual_b64": actual.get("raw_b64"),
            "expected_b64": expected_b64,
            "pre_ws": pre_ws,
            "post_ws": post_ws,
        }
    except Exception as exc:
        return {"ok": False, "reason": str(exc), "pre_ws": pre_ws, "post_ws": post_ws}


def _send_direct_ws_bet(page, pre_ws: dict, round_info: dict, estimated_sol: str) -> dict:
    if not estimated_sol:
        raise Exception("direct_ws_missing_estimated_sol")
    if not pre_ws.get("has_ws") or pre_ws.get("ready_state") != 1:
        raise Exception(f"direct_ws_not_ready:{pre_ws}")
    if round_info.get("game_round_id") is None:
        raise Exception(f"direct_ws_missing_game_round:{round_info}")

    target_game_round = int(round_info["game_round_id"]) + 1
    pre_packet = pre_ws.get("last_packet_id")
    packet_id = ((int(pre_packet) + 1) & 0xFF) if pre_packet is not None else None

    sent = page.evaluate(
        """({packetId, gameRoundId, solAmount, cashoutInt, tickK, currency}) => {
            return window.__realbetDirectTwiceBet({
                packetId,
                gameRoundId,
                solAmount,
                cashoutInt,
                tickK,
                currency,
            });
        }""",
        {
            "packetId": packet_id,
            "gameRoundId": target_game_round,
            "solAmount": str(estimated_sol),
            "cashoutInt": int(round(CASHOUT * 100)),
            "tickK": WS_TICK_K,
            "currency": BET_CURRENCY,
        },
    )
    return {
        "target_game_round_id": target_game_round,
        "expected_packet_id": packet_id,
        "estimated_sol": estimated_sol,
        "sent": sent,
    }


def _find_bet_button(page):
    for button in page.query_selector_all("button[class*=button-brand]"):
        box = button.bounding_box()
        text = (button.inner_text() or "").strip()
        if box and box.get("width", 0) > 200:
            return button, text
    return None, ""


def _button_ready(text: str) -> bool:
    return (
        "Bet" in text
        and "Placed" not in text
        and "Connecting" not in text
        and "Cashout" not in text
    )


def _ensure_strategy_start(st: dict, balance_usd: float | None = None):
    try:
        current = None
        if STRATEGY_START_FILE.exists():
            current = json.loads(STRATEGY_START_FILE.read_text())
        if current and current.get("strategy") == STRATEGY_NAME:
            return
        data = {
            "strategy": STRATEGY_NAME,
            "start_ts": datetime.now(timezone.utc).isoformat(),
            "start_pnl": st.get("pnl", 0.0),
            "start_bets": st.get("bets", 0),
            "start_wins": st.get("wins", 0),
            "start_balance_usd": balance_usd,
        }
        STRATEGY_START_FILE.write_text(json.dumps(data, indent=2))
        _audit("strategy_start", **data)
    except Exception:
        pass


def _ratio_ge(values: list[float], threshold: float) -> float:
    if not values:
        return 0.0
    return sum(1 for value in values if value >= threshold) / len(values)


def _recent_multipliers(limit: int) -> list[float]:
    try:
        with duckdb.connect(str(ROUNDS_DB), read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT multiplier
                FROM rounds
                ORDER BY id DESC
                LIMIT ?
                """,
                [limit],
            ).fetchall()
        return [float(row[0]) for row in reversed(rows)]
    except Exception as exc:
        _log(f"VETO data unavailable: {exc} -- allow bet")
        return []


def _relative_bad_veto() -> tuple[bool, str]:
    need = REL_BASE_LEN + REL_HIGH_LEN + REL_LOW_LEN
    values = _recent_multipliers(need)
    if len(values) < need:
        return False, f"not_enough_history:{len(values)}/{need}"

    base = values[:REL_BASE_LEN]
    high = values[REL_BASE_LEN:REL_BASE_LEN + REL_HIGH_LEN]
    low = values[-REL_LOW_LEN:]

    base_ge2 = _ratio_ge(base, 2.0)
    high_ge2 = _ratio_ge(high, 2.0)
    low_ge2 = _ratio_ge(low, 2.0)
    veto = relative_bad_veto_matches(values, STRATEGY_CFG)
    reason = (
        f"base_ge2={base_ge2:.2f} high_ge2={high_ge2:.2f} "
        f"low_ge2={low_ge2:.2f} delta={high_ge2 - low_ge2:.2f}"
    )
    return veto, reason


SNAP_FILE = STATE_FILE.parent / "realbet_snapshots.json"
P24_FILE = STATE_FILE.parent / "realbet_24h_pnl.json"

def _save_snapshot(balance_usd: float, st: dict):
    """Save hourly snapshot for %1h/%12h/%24h calculations."""
    try:
        snaps = json.loads(SNAP_FILE.read_text()) if SNAP_FILE.exists() else []
        snaps.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "balance_usd": balance_usd,
            "pnl": st.get("pnl", 0.0),
            "bets": st.get("bets", 0),
            "wins": st.get("wins", 0),
        })
        # Keep last 200 snapshots (~200 hours)
        SNAP_FILE.write_text(json.dumps(snaps[-200:], indent=2))
    except Exception:
        pass


def _save_24h_pnl(balance_usd: float, st: dict):
    """Persist completed rolling 24h P&L windows separately from snapshots."""
    try:
        now = datetime.now(timezone.utc)
        data = json.loads(P24_FILE.read_text()) if P24_FILE.exists() else {}
        current = data.get("current")
        completed = data.get("completed", [])
        if not current:
            data["current"] = {
                "start_ts": now.isoformat(),
                "start_balance_usd": balance_usd,
                "start_pnl": st.get("pnl", 0.0),
                "start_bets": st.get("bets", 0),
                "start_wins": st.get("wins", 0),
                "strategy": st.get("strategy", STRATEGY_NAME),
            }
            data["completed"] = completed
            P24_FILE.write_text(json.dumps(data, indent=2))
            return

        start = datetime.fromisoformat(current["start_ts"])
        if (now - start).total_seconds() < 24 * 3600:
            return

        completed.append({
            "start_ts": current["start_ts"],
            "end_ts": now.isoformat(),
            "strategy": st.get("strategy", STRATEGY_NAME),
            "start_balance_usd": current.get("start_balance_usd"),
            "end_balance_usd": balance_usd,
            "balance_delta_usd": balance_usd - float(current.get("start_balance_usd", balance_usd)),
            "start_pnl": current.get("start_pnl", 0.0),
            "end_pnl": st.get("pnl", 0.0),
            "pnl_delta_usd": st.get("pnl", 0.0) - float(current.get("start_pnl", st.get("pnl", 0.0))),
            "bets_delta": st.get("bets", 0) - int(current.get("start_bets", st.get("bets", 0))),
            "wins_delta": st.get("wins", 0) - int(current.get("start_wins", st.get("wins", 0))),
        })
        data["completed"] = completed[-365:]
        data["current"] = {
            "start_ts": now.isoformat(),
            "start_balance_usd": balance_usd,
            "start_pnl": st.get("pnl", 0.0),
            "start_bets": st.get("bets", 0),
            "start_wins": st.get("wins", 0),
            "strategy": st.get("strategy", STRATEGY_NAME),
        }
        P24_FILE.write_text(json.dumps(data, indent=2))
        _log(f"24H P&L saved: ${completed[-1]['pnl_delta_usd']:+.4f} "
             f"bets={completed[-1]['bets_delta']} wins={completed[-1]['wins_delta']}")
    except Exception as exc:
        _log(f"24H P&L save error: {exc}")


def _get_snap_near(secs_ago: int) -> dict | None:
    """Get snapshot nearest to N seconds ago."""
    try:
        snaps = json.loads(SNAP_FILE.read_text()) if SNAP_FILE.exists() else []
        from datetime import timedelta
        target = datetime.now(timezone.utc) - timedelta(seconds=secs_ago)
        best = None
        best_diff = float("inf")
        for s in snaps:
            ts = datetime.fromisoformat(s["ts"])
            diff = abs((ts - target).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best = s
        return best
    except Exception:
        return None


def run():
    from playwright.sync_api import sync_playwright, TimeoutError as PwTO

    st = _load_state()
    st["strategy"] = STRATEGY_NAME
    _save_state(st)
    _ensure_strategy_start(st, st.get("last_balance_usd"))
    _log(f"Starting realbet {STRATEGY_NAME} executor={EXECUTOR} | scale={st['scale']} consec={st['consec']} pnl={st['pnl']:+.4f}")

    with sync_playwright() as p:
        # headless=True works on VPS via VPN; headless=False for local Windows
        IS_VPS = not sys.platform.startswith("win")
        browser = p.chromium.launch(
            headless=IS_VPS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-popup-blocking",
            ]
        )
        ctx = browser.new_context(
            storage_state=str(SESSION_FILE),
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        _install_network_capture(page)

        # --- API response interceptor for balance ---
        _api_dump_done = [False]  # log raw structure once

        def _on_api_response(resp):
            url = resp.url.lower()
            # Skip high-frequency realtime endpoint (fires every 2s, not a balance endpoint)
            if 'realtime' in url or 'tracking' in url or 'intercom' in url:
                return
            if not any(k in url for k in (
                    'balance', 'wallet', 'user', 'account', 'asset', 'fund',
                    'currency', 'amount')):
                return
            try:
                data = resp.json()

                # Always dump first balance-API response so we can see the structure
                if not _api_dump_done[0]:
                    _log(f"[API-DUMP] url={resp.url[-70:]}")
                    _log(f"[API-DUMP] data={str(data)[:400]}")
                    _api_dump_done[0] = True

                # BCGame stores balance in SOL (shown as USD in UI).
                # Find the useable currency entry with amount > 0.
                SOL_PRICE = 71.43  # BCGame internal rate (derived from bet conversions: 0.014 SOL/USD)

                def _scan_sol(d, depth=0):
                    """Find SOL (or any useable) balance and return USD equivalent."""
                    if depth > 6:
                        return None
                    if isinstance(d, dict):
                        cur = str(d.get('currencyName', d.get('currency', ''))).upper()
                        use = d.get('useable', False)
                        amt_str = d.get('amount', d.get('generalAmount', '0'))
                        try:
                            amt = float(str(amt_str).replace(',', ''))
                        except Exception:
                            amt = 0.0
                        # USDT/USDC first — direct USD, no conversion needed
                        if cur in ('USDT', 'USDC', 'USD', 'BUSD') and amt > 0.001:
                            return round(amt, 4)
                        # SOL or any useable currency with non-trivial balance
                        if cur == 'SOL' and amt > 0.00001:
                            return round(amt * SOL_PRICE, 4)
                        if use and cur not in ('BCD', 'MATIC', '') and amt > 0.00001:
                            # For non-SOL useable currencies, try to return amount directly
                            # (BCGame might return USD value for stablecoins)
                            if cur in ('USDT', 'USD', 'USDC', 'BUSD'):
                                return round(amt, 4)
                            return round(amt * SOL_PRICE, 4)
                        for v in d.values():
                            r = _scan_sol(v, depth + 1)
                            if r is not None:
                                return r
                    elif isinstance(d, list):
                        # Prefer USDT/USDC first (betting currency), then SOL
                        for item in d:
                            if isinstance(item, dict):
                                cur = str(item.get('currencyName', '')).upper()
                                if cur in ('USDT', 'USDC', 'USD', 'BUSD'):
                                    amt = float(str(item.get('amount', '0') or '0').replace(',', ''))
                                    if amt > 0.001:
                                        return round(amt, 4)
                        # Then SOL
                        for item in d:
                            if isinstance(item, dict):
                                cur = str(item.get('currencyName', '')).upper()
                                if cur == 'SOL':
                                    r = _scan_sol(item, depth + 1)
                                    if r is not None:
                                        return r
                        # Fallback: any useable
                        for item in d:
                            r = _scan_sol(item, depth + 1)
                            if r is not None:
                                return r
                    return None

                _scan_usd = _scan_sol  # alias

                bal = _scan_usd(data)
                if bal is not None:
                    if _balance_from_api[0] != bal:
                        _log(f"[API] balance updated: ${bal:.4f}  url={resp.url[-60:]}")
                    _balance_from_api[0] = bal
                else:
                    _log(f"[API] no USD balance in {resp.url[-50:]}")
            except Exception:
                pass

        page.on('response', _on_api_response)

        # Mask automation fingerprint
        page.add_init_script("""
            Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
            Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
            Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
            window.chrome={runtime:{}};

            // Intercept Socket.IO to capture round results immediately (no DB wait)
            window.__realbetRoundCache = {};
            window.__realbetBettingPhase = {active: false, game_round_id: null};

            const _origXHR = XMLHttpRequest;
            const _origIO  = window.io;

            // Patch WebSocket to intercept binary round-end frames
            const _OrigWS = window.WebSocket;
            window.WebSocket = function(url, protocols) {
                const ws = new _OrigWS(url, protocols);
                ws.addEventListener('message', function(evt) {
                    try {
                        if (evt.data instanceof ArrayBuffer || evt.data instanceof Blob) {
                            const process = (buf) => {
                                const bytes = new Uint8Array(buf);
                                // Find /g/cm path in the frame
                                const path = '/g/cm';
                                const pathBytes = new TextEncoder().encode(path);
                                for (let i = 0; i < bytes.length - pathBytes.length; i++) {
                                    let match = true;
                                    for (let j = 0; j < pathBytes.length; j++) {
                                        if (bytes[i+j] !== pathBytes[j]) { match = false; break; }
                                    }
                                    if (match) {
                                        // Check for ed or st suffix after path
                                        const after = bytes[i + pathBytes.length];
                                        const next  = bytes[i + pathBytes.length + 1];
                                        const byte3 = bytes[i + pathBytes.length + 2] || 0;
                                        const isEd = (after === 2 && next === 0x65 && byte3 === 0x64);
                                        const isSt = (after === 2 && next === 0x73 && byte3 === 0x74);
                                        if (isEd || isSt) {
                                            // Parse protobuf: field1=round_id, field6=mult*100
                                            let pos = i + pathBytes.length + 3; // skip ed
                                            let gid = null; let mult = null;
                                            while (pos < bytes.length) {
                                                const tag = bytes[pos++];
                                                const wt = tag & 7; const fn = tag >> 3;
                                                if (wt === 0) {
                                                    let val = 0; let sh = 0;
                                                    while(true){if(pos>=bytes.length)break;const b=bytes[pos++];val|=(b&127)<<sh;if(!(b&128))break;sh+=7;}
                                                    if (fn === 1) gid = String(val);
                                                    else if (fn === 6) mult = val / 100.0;
                                                } else if (wt === 2) {
                                                    let ln = 0; let sh = 0;
                                                    while(true){if(pos>=bytes.length)break;const b=bytes[pos++];ln|=(b&127)<<sh;if(!(b&128))break;sh+=7;}
                                                    pos += ln;
                                                } else if (wt === 4) {
                                                    /* end group - skip */
                                                } else break;
                                            }
                                            if (gid && mult !== null && mult >= 1.0) {
                                                window.__realbetRoundCache[gid] = {mult: mult, ts: Date.now()};
                                                // Clean old entries
                                                const keys = Object.keys(window.__realbetRoundCache);
                                                if (keys.length > 30) delete window.__realbetRoundCache[keys[0]];
                                                // Signal that betting phase is now open (round just ended)
                                                window.__realbetBettingPhase = {
                                                    active: true,
                                                    game_round_id: gid,
                                                    ts: Date.now()
                                                };
                                            }
                                        }
                                        break;
                                    }
                                }
                            };
                            if (evt.data instanceof Blob) {
                                evt.data.arrayBuffer().then(process);
                            } else {
                                process(evt.data);
                            }
                        }
                    } catch(e) {}
                });
                return ws;
            };
            Object.assign(window.WebSocket, _OrigWS);
        """)

        _log(f"Opening BCGame crash... (headless={IS_VPS})")
        page.goto("https://bcgame61.com/game/crash", timeout=30000)
        page.wait_for_timeout(6000)

        # Accept cookies
        try:
            page.locator('button:has-text("Accept")').click(timeout=2000)
        except Exception:
            pass

        # Wait a bit more for JS to populate localStorage
        page.wait_for_timeout(3000)

        # Verify login — prefer API balance as auth signal
        try:
            acc = json.loads(page.evaluate("() => localStorage.getItem('account')") or "{}")
            uid = acc.get("userId") or acc.get("uid") or 0
            name = acc.get("name", "ivanK1157")
            if uid:
                _log(f"Logged in: {name}  uid={uid}")
            elif _balance_from_api[0] is not None:
                # API returned balance → cookies valid, localStorage just not loaded yet
                _log(f"Logged in via API balance=${_balance_from_api[0]:.2f}  (localStorage empty)")
            else:
                _log("ERROR: not logged in — no uid and no API balance")
                browser.close()
                return
        except Exception as e:
            _log(f"Login check error: {e}")

        # Stop BCGame Auto-tab betting if active (prevents SOL auto-bet running in parallel)
        try:
            page.locator("text=Auto").first.click(timeout=2000)
            page.wait_for_timeout(800)
            # Look for Stop button (active auto-bet shows Stop)
            for stop_text in ("Stop Auto Bet", "Stop", "STOP"):
                try:
                    btn = page.locator(f"button:has-text('{stop_text}')").first
                    if btn.is_visible(timeout=1000):
                        btn.click(timeout=1000)
                        _log(f"[INIT] Stopped BCGame auto-bet ({stop_text})")
                        break
                except Exception:
                    pass
        except Exception:
            pass
        page.wait_for_timeout(500)

        # Switch to Manual tab
        try:
            page.locator("text=Manual").first.click(timeout=3000)
        except Exception:
            pass
        page.wait_for_timeout(1000)

        # Close sidebar by clicking game area
        page.mouse.click(700, 400)
        page.wait_for_timeout(1500)

        # Wait for WebSocket to connect
        _log("Waiting for game connection...")
        for i in range(30):
            try:
                # Find game bet button specifically by scanning all buttons
                all_btns = page.query_selector_all("button[class*=button-brand]")
                found = False
                for b in all_btns:
                    txt = (b.inner_text() or "").strip()
                    bb = b.bounding_box()
                    # Bet button is tall (h≈48) and wide, Deposit is small
                    if bb and bb.get("width", 0) > 200:
                        _log(f"Game button: [{txt[:40]}]")
                        if "Connecting" not in txt:
                            _log("Game ready!")
                        found = True
                        break
                if found:
                    break
                if i % 5 == 0:
                    _log(f"Waiting... ({i*2}s)")
            except Exception:
                pass
            time.sleep(2)
        else:
            _log("Could not connect to game WS after 60s — check your IP/VPN")

        # Set auto cashout: only for EXECUTOR=gui (ws uses WS-packet cashout_int, no UI needed)
        if EXECUTOR != "ws":
            _log(f"Setting auto cashout to {CASHOUT}x...")
            cashout_set = False
            for attempt in range(8):
                try:
                    visible = _visible_inputs(page)
                    _log(f"  Found {len(visible)} visible inputs")
                    _, co, diag = _bet_control_inputs(page)
                    _log(f"  Input diag: {diag[:220]}")
                    if co:
                        _set_input(page, co, str(CASHOUT))
                        val = co.input_value()
                        _log(f"  Cashout = [{val}]")
                        parsed = _parse_float(val)
                        if parsed is not None and abs(parsed - CASHOUT) < 0.000001:
                            _log("Cashout set OK!")
                            cashout_set = True
                            break
                except Exception as e:
                    _log(f"  Cashout attempt {attempt+1}: {e}")
                page.wait_for_timeout(1500)
            if not cashout_set:
                _log("WARNING: Could not set cashout — check manually in browser!")
        else:
            _log(f"EXECUTOR=ws: skipping UI cashout (cashout={CASHOUT}x is in WS packet, avoids BCGame auto-bet)")

        import time as _time
        last_snap_t = 0

        while True:
            # Snapshot every 10 min for time-based P&L tracking
            _now_t = _time.time()
            if _now_t - last_snap_t >= 600:
                _bal = _read_balance(page)
                if _bal:
                    _save_snapshot(_bal, st)
                    _save_24h_pnl(_bal, st)
                    st["last_balance_usd"] = _bal
                    _save_state(st)
                last_snap_t = _now_t

            if STOP_FLAG.exists():
                _log("Stop flag — exiting")
                STOP_FLAG.unlink(missing_ok=True)
                break

            # Check stop-loss / take-profit
            if st["pnl"] <= -(BANK_USD * STOP_LOSS_PCT):
                _log(f"STOP LOSS: pnl={st['pnl']:+.4f}")
                break
            if st["pnl"] >= BANK_USD * TAKE_PROFIT_PCT:
                _log(f"TAKE PROFIT: pnl={st['pnl']:+.4f}")
                break

            # --- STEP 1: wait for betting phase ---
            btn_text = ""
            for _ in range(300):
                try:
                    _, btn_text = _find_bet_button(page)
                except Exception:
                    pass
                if _button_ready(btn_text):
                    break
                time.sleep(0.2)  # fast poll: detect bet window within 200ms
            else:
                _log(f"Timeout waiting for bet phase (btn={btn_text}) — refreshing")
                page.reload(timeout=15000)
                page.wait_for_timeout(6000)
                continue

            # --- STEP 2: cooldown check ---
            if st["cooldown"] > 0:
                _log(f"Cooldown: {st['cooldown']} rounds left — skipping")
                _audit(
                    "cooldown_skip",
                    **_latest_round_info(),
                    cooldown=st["cooldown"],
                    scale=st["scale"],
                    pnl=st["pnl"],
                )
                st["cooldown"] -= 1
                _save_state(st)
                # Wait for round to end and skip
                time.sleep(35)
                continue

            round_info = _latest_round_info()
            current_round_id = round_info.get("round_id")
            if current_round_id and st.get("last_decision_round_id") == current_round_id:
                _audit(
                    "duplicate_round_guard",
                    **round_info,
                    scale=st["scale"],
                    pnl=st["pnl"],
                    last_decision_round_id=st.get("last_decision_round_id"),
                )
                # Poll WS betting phase flag (100ms intervals) instead of sleeping 5s
                _phase_wait_start = time.time()
                while time.time() - _phase_wait_start < 5.0:
                    time.sleep(0.1)
                    try:
                        phase = page.evaluate("() => window.__realbetBettingPhase || {}")
                        if phase.get("active") and phase.get("ts", 0) > _phase_wait_start * 1000:
                            # New betting phase detected via WS — break early!
                            page.evaluate("() => { window.__realbetBettingPhase = {active: false, ts: 0}; }")
                            break
                    except Exception:
                        pass
                continue

            veto, veto_reason = _relative_bad_veto()
            _audit(
                "decision",
                **round_info,
                veto=veto,
                veto_reason=veto_reason,
                scale=st["scale"],
                pnl=st["pnl"],
                cashout=CASHOUT,
            )
            if veto:
                if current_round_id:
                    st["last_decision_round_id"] = current_round_id
                    _save_state(st)
                _log(f"RELWAVE VETO — skipping round ({veto_reason})")
                _audit("veto_skip", **round_info, reason=veto_reason, scale=st["scale"], pnl=st["pnl"])
                time.sleep(35)
                continue

            # --- STEP 3: place bet (scale synced from actual played history) ---
            synced_scale = _scale_from_audit()
            if synced_scale != st["scale"]:
                _log(f"[SCALE SYNC] state={st['scale']:.0f}x -> audit={synced_scale:.0f}x")
                st["scale"] = synced_scale
            bet = round(BASE_BET_USD * st["scale"], 4)
            bet_str = f"{bet:.2f}"
            _log(f"BET ${bet_str} @ {CASHOUT}x  scale={st['scale']:.1f}  pnl={st['pnl']:+.4f}")
            if current_round_id:
                st["last_decision_round_id"] = current_round_id
                _save_state(st)
            ws_status = _direct_ws_status(page)
            _audit(
                "bet_attempt",
                **round_info,
                bet=bet,
                scale=st["scale"],
                pnl=st["pnl"],
                cashout=CASHOUT,
                direct_ws=ws_status,
            )

            bal_before = _read_balance(page)
            try:
                if EXECUTOR == "ws":
                    # Fast path: skip UI filling entirely, send immediately
                    est_sol = _sol_amount_for_usd_bet(bet)
                    _audit("controls_verified", **round_info, bet=bet,
                           controls=f"ws_fast est_sol={est_sol}",
                           controls_ok=True, estimated_sol=est_sol,
                           scale=st["scale"], pnl=st["pnl"], cashout=CASHOUT)
                else:
                    time.sleep(0.8)
                    controls_ok, controls_msg = _ensure_bet_controls(page, bet_str)
                    if not controls_ok:
                        raise Exception(f"Bet controls not stable: {controls_msg}")
                    est_sol = _read_estimated_sol_amount(page) if controls_ok else None
                    _audit("controls_verified", **round_info, bet=bet, controls=controls_msg,
                           controls_ok=controls_ok, estimated_sol=est_sol,
                           scale=st["scale"], pnl=st["pnl"], cashout=CASHOUT)
                    time.sleep(0.3)

                if EXECUTOR == "ws":
                    send_started = time.time()
                    direct_sent = _send_direct_ws_bet(page, ws_status, round_info, est_sol)
                    page.wait_for_timeout(800)
                    if _last_bet_reject[0] >= send_started:
                        raise Exception(f"Bet rejected by server: {_last_bet_reject[1][:160]}")
                    post_ws_status = _direct_ws_status(page)
                    ws_shadow = _ws_shadow_compare(ws_status, post_ws_status, round_info, est_sol)
                    _audit("direct_ws_sent", **round_info, bet=bet, direct=direct_sent,
                           post_ws=post_ws_status, ws_shadow=ws_shadow,
                           scale=st["scale"], pnl=st["pnl"], cashout=CASHOUT)
                    _log(f"Placed via WS. Balance: {f'${bal_before:.4f}' if bal_before else 'unknown'}")
                    _audit("bet_placed", **round_info, bet=bet, balance_before=bal_before,
                           direct_ws=post_ws_status, ws_shadow=ws_shadow,
                           executor="ws", scale=st["scale"], pnl=st["pnl"])
                    post_click_text = "direct_ws"
                else:
                    post_click_text = None

                if EXECUTOR == "ws":
                    pass
                else:
                    # Click the large game button (width>200px = bet button, not Deposit)
                    game_btn, game_btn_text = _find_bet_button(page)
                    if not game_btn:
                        raise Exception("Game bet button not found")
                    if not _button_ready(game_btn_text):
                        raise Exception(f"Game bet button not ready: {game_btn_text[:80]}")
                    click_started = time.time()
                    game_btn.click()
                    post_click_text = ""
                    for _ in range(10):
                        page.wait_for_timeout(250)
                        _, post_click_text = _find_bet_button(page)
                        if _last_bet_reject[0] >= click_started:
                            raise Exception(f"Bet rejected by server: {_last_bet_reject[1][:160]}")
                        if not _button_ready(post_click_text):
                            break
                    if _last_bet_reject[0] >= click_started:
                        raise Exception(f"Bet rejected by server: {_last_bet_reject[1][:160]}")
                    if _button_ready(post_click_text):
                        _audit("bet_click_uncertain", **round_info, bet=bet,
                               button_after=post_click_text[:80], scale=st["scale"], pnl=st["pnl"])
                    post_ws_status = _direct_ws_status(page)
                    ws_shadow = _ws_shadow_compare(ws_status, post_ws_status, round_info, est_sol)
                    _audit("ws_shadow_compare", **round_info, bet=bet, shadow=ws_shadow,
                           scale=st["scale"], pnl=st["pnl"], cashout=CASHOUT)
                    _log(f"Placed. Balance: {f'${bal_before:.4f}' if bal_before else 'unknown'}")
                    _audit("bet_placed", **round_info, bet=bet, balance_before=bal_before,
                           button_after=post_click_text[:80], direct_ws=post_ws_status,
                           ws_shadow=ws_shadow, executor="gui", scale=st["scale"], pnl=st["pnl"])
            except Exception as e:
                err_str = str(e).lower()
                _log(f"Place error: {e} — skipping round")
                _audit("place_error", **round_info, bet=bet, error=str(e), scale=st["scale"], pnl=st["pnl"])
                if "bet suspended" in err_str or "game has started" in err_str:
                    # Strategy C: Bet suspended → skip 1 more round → reset scale to 1
                    st["scale"] = 1.0
                    st["consec"] = 0
                    _log(f"BET SUSPENDED: scale reset to 1.0, skipping 1 extra round")
                    _audit("suspended_reset", **round_info, bet=bet, pnl=st["pnl"])
                    time.sleep(35)  # skip 1 round (~35s)
                else:
                    time.sleep(5)
                continue

            # --- STEP 4: wait for collector to settle the bet round ---
            settled_info = _wait_for_settled_round(current_round_id, bet_game_round_id=round_info.get("game_round_id"), page=page)
            if "round_error" in settled_info:
                _log(f"Round settle unknown — checking balance ({settled_info['round_error']})")
                result = "unknown"
            else:
                settled_mult = settled_info.get("last_mult", 0.0)
                result = "win" if settled_mult >= CASHOUT else "loss"
                _audit("round_settled", **round_info, settled_round=settled_info,
                       result=result, settled_mult=settled_mult, bet=bet, cashout=CASHOUT)

            # --- STEP 5: record result ---
            st["bets"] += 1

            if result == "win":
                profit = bet * (CASHOUT - 1.0)
                st["wins"]  += 1
                st["pnl"]   += profit
                st["scale"]  = 1.0
                st["consec"] = 0
                _log(f"WIN +${profit:.4f}  pnl={st['pnl']:+.4f}  wr={st['wins']}/{st['bets']}")
                _audit("result", **round_info, settled_round=settled_info, result="win",
                       bet=bet, profit=profit, pnl=st["pnl"],
                       wins=st["wins"], bets=st["bets"], scale=st["scale"])
            elif result == "loss":
                st["pnl"]    -= bet
                st["consec"] += 1
                new_scale     = min(st["scale"] * LOSS_SCALE, MAX_SCALE)
                st["scale"]   = new_scale
                _log(f"LOSS -${bet:.4f}  pnl={st['pnl']:+.4f}  consec={st['consec']}  next={new_scale:.1f}x")
                _audit("result", **round_info, settled_round=settled_info, result="loss",
                       bet=bet, profit=-bet, pnl=st["pnl"],
                       consec=st["consec"], bets=st["bets"], scale=st["scale"])
                if st["consec"] >= CONSEC_TRIGGER:
                    _log(f"Cooldown: {CONSEC_TRIGGER} losses -> pause {COOLDOWN_ROUNDS} rounds (scale kept)")
                    st["cooldown"] = COOLDOWN_ROUNDS
                    # No scale reset (variant C: accumulate across cooldowns)
                    st["consec"]   = 0
            else:
                # Look up actual round result in DB by game_round_id
                _log(f"Round result unknown — querying DB for game_round {round_info.get('game_round_id')}")
                try:
                    target_gid = str(int(round_info.get("game_round_id", 0)) + 1)
                    row = None
                    for _retry in range(4):  # 3 retries x 10s = 30s max wait
                        with duckdb.connect(str(ROUNDS_DB), read_only=True) as _conn:
                            row = _conn.execute(
                                "SELECT multiplier FROM rounds WHERE game_round_id = ? LIMIT 1",
                                [target_gid]
                            ).fetchone()
                        if row:
                            break
                        if _retry < 3:
                            _log(f"Round {target_gid} not in DB yet, retry {_retry+1}/3 in 10s...")
                            time.sleep(10)
                    if row:
                        actual_mult = float(row[0])
                        _log(f"DB result: game_round={target_gid} mult={actual_mult:.2f}x")
                        if actual_mult >= CASHOUT:
                            profit = bet * (CASHOUT - 1.0)
                            st["wins"] += 1; st["pnl"] += profit; st["scale"] = 1.0; st["consec"] = 0
                            _log(f"WIN(db) +${profit:.4f}  pnl={st['pnl']:+.4f}  wr={st['wins']}/{st['bets']}")
                            _audit("result_db", **round_info, result="win", bet=bet,
                                   actual_mult=actual_mult, pnl=st["pnl"],
                                   wins=st["wins"], bets=st["bets"], scale=st["scale"])
                        else:
                            st["pnl"] -= bet; st["consec"] += 1
                            new_scale = min(st["scale"] * LOSS_SCALE, MAX_SCALE)
                            st["scale"] = new_scale
                            _log(f"LOSS(db) -${bet:.4f}  mult={actual_mult:.2f}x  pnl={st['pnl']:+.4f}  consec={st['consec']}")
                            _audit("result_db", **round_info, result="loss", bet=bet,
                                   actual_mult=actual_mult, pnl=st["pnl"],
                                   consec=st["consec"], bets=st["bets"], scale=st["scale"])
                            if st["consec"] >= CONSEC_TRIGGER:
                                _log(f"Cooldown: {CONSEC_TRIGGER} losses -> pause {COOLDOWN_ROUNDS} rounds (scale kept)")
                                st["cooldown"] = COOLDOWN_ROUNDS
                                st["consec"] = 0
                    else:
                        # Round not in DB yet — skip without updating state
                        _log(f"Round {target_gid} not in DB — skipping (no state change)")
                        _audit("result_unknown", **round_info, bet=bet, pnl=st["pnl"],
                               bets=st["bets"], scale=st["scale"])
                except Exception as _db_err:
                    _log(f"DB result lookup error: {_db_err} — skipping")

            # Read real balance: DOM scan header (live) + API fallback
            try:
                dom_bal = page.evaluate("""() => {
                    // Find balance near Deposit button (header area only)
                    const btns = Array.from(document.querySelectorAll('button'));
                    const dep = btns.find(b => (b.innerText || '').trim() === 'Deposit');
                    if (dep) {
                        let el = dep.parentElement;
                        for (let i = 0; i < 6; i++) {
                            if (!el) break;
                            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                            let node;
                            while ((node = walker.nextNode())) {
                                const t = node.textContent.trim();
                                const m = t.match(/^\$(\d+\.\d{2})$/);
                                if (m) {
                                    const n = parseFloat(m[1]);
                                    if (n > 0.5 && n < 100000) return n;
                                }
                            }
                            el = el.parentElement;
                        }
                    }
                    return null;
                }""")
                if dom_bal and dom_bal > 0.01:
                    if st.get("last_balance_usd") != dom_bal:
                        _log("[BAL] " + str(round(st.get("last_balance_usd", 0), 4)) + " -> $" + str(dom_bal) + " (live)")
                    st["last_balance_usd"] = dom_bal
                else:
                    fb = _fetch_balance_fresh(page)
                    if fb and fb > 0.01:
                        if st.get("last_balance_usd") != fb:
                            _log("[BAL] " + str(round(st.get("last_balance_usd", 0), 4)) + " -> $" + str(fb) + " (api)")
                        st["last_balance_usd"] = fb
            except Exception:
                pass
            _save_state(st)

        browser.close()
    _log(f"Stopped. pnl={st['pnl']:+.4f}  bets={st['bets']}  wins={st['wins']}")


SOL_PRICE = 71.43  # BCGame internal rate (derived from bet conversions: 0.014 SOL/USD)


def _fetch_balance_fresh(page) -> float | None:
    """Fetch balance directly from BCGame API using page session (no cache)."""
    try:
        result = page.evaluate("""async () => {
            try {
                const resp = await fetch('/api/user/amount/?_=' + Date.now(), {
                    credentials: 'include',
                    cache: 'no-store',
                    headers: {'Content-Type': 'application/json', 'Cache-Control': 'no-cache'}
                });
                const data = await resp.json();
                const currencies = data.data || [];
                // Prefer USDT/USDC — direct USD, no conversion
                for (const c of currencies) {
                    if (['USDT','USDC','USD','BUSD'].includes(c.currencyName)) {
                        const amt = parseFloat(c.amount);
                        if (amt > 0.001) return amt;
                    }
                }
                // Fallback: SOL
                for (const c of currencies) {
                    if (c.currencyName === 'SOL') {
                        const amt = parseFloat(c.amount);
                        if (amt > 0.00001) return amt * """ + str(SOL_PRICE) + """;
                    }
                }
                // Last resort: useable
                for (const c of currencies) {
                    if (c.useable) {
                        const amt = parseFloat(c.amount);
                        if (amt > 0.00001) return amt * """ + str(SOL_PRICE) + """;
                    }
                }
                return null;
            } catch(e) { return null; }
        }""")
        if result and result > 0.01:
            _balance_from_api[0] = round(result, 4)
            return _balance_from_api[0]
    except Exception:
        pass
    return None


def _read_balance(page) -> float | None:
    """Read USD balance — fresh API fetch first, cached fallback."""
    fresh = _fetch_balance_fresh(page)
    if fresh:
        return fresh
    # Fallback: last cached value from interceptor
    if _balance_from_api[0] is not None:
        return _balance_from_api[0]

    # Fallback: DOM scan
    try:
        result = page.evaluate("""() => {
            // Method 1: localStorage account balance
            try {
                const raw = localStorage.getItem('account');
                if (raw) {
                    const acc = JSON.parse(raw);
                    const bal = acc.balance || acc.usdBalance || acc.walletBalance;
                    if (bal && parseFloat(bal) > 0) return parseFloat(bal);
                }
            } catch(e) {}

            // Method 2: wallet display in header buttons (match $12.34 pattern)
            try {
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    const t = btn.innerText || '';
                    const m = t.match(/^\\$([\\d,\\.]+)$/);
                    if (m) {
                        const n = parseFloat(m[1].replace(',',''));
                        if (n > 0.001 && n < 100000) return n;
                    }
                }
            } catch(e) {}

            // Method 3: any span/div near Deposit with numeric pattern
            try {
                const all = document.querySelectorAll('[class*=wallet],[class*=balance],[class*=amount]');
                for (const el of all) {
                    const t = el.innerText || '';
                    const m = t.match(/^[\\$]?([\\d,\\.]+)$/);
                    if (m) {
                        const n = parseFloat(m[1].replace(',',''));
                        if (n > 0.001 && n < 100000) return n;
                    }
                }
            } catch(e) {}
            return null;
        }""")
        return result
    except Exception:
        return None


def _check_round_result(page, username: str) -> str:
    """Check if we won or lost by looking at the players list. Returns 'win','loss','unknown'."""
    try:
        result = page.evaluate(f"""() => {{
            const rows = document.querySelectorAll('tr, [class*=player], [class*=Player]');
            for(const row of rows){{
                const text = row.innerText || '';
                if(text.includes('{username}')){{
                    // If cashout value present (e.g. "2.30x") = win, "-" = loss/in-progress
                    const hasCashout = /[0-9]+\\.[0-9]+x/.test(text);
                    return hasCashout ? 'win' : 'loss';
                }}
            }}
            return 'unknown';
        }}""")
        return result
    except Exception:
        return "unknown"


if __name__ == "__main__":
    LOG_FILE.parent.mkdir(exist_ok=True, parents=True)
    _log("=" * 50)
    _log(f"bot_realbet.py  bank=${BANK_USD}  base_bet=${BASE_BET_USD}  cashout={CASHOUT}x")
    _log(f"Stop: create file {STOP_FLAG}")
    run()
