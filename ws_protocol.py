"""
ws_protocol.py
Shared BCGame /g/cm WS frame parsing -- used by the real bot (bot_realbet.py)
and the live feed publisher (live_feed.py).

Both 'ed' (round end) and 'st' (round stats) frames carry the crash multiplier
in protobuf field 6; field 1 is the game_round_id. NEITHER is a betting-open
signal -- the betting window opening is detected separately via the page
betting-phase probe (window.__realbetBettingPhase), not from these frames.
"""

CM_PREFIX = b"/g/cm"
ED_SUFFIX = b"ed"
ST_SUFFIX = b"st"


def parse_round_frame(raw: bytes):
    """Parse a /g/cm ed|st binary frame.

    Returns (game_round_id: str|None, multiplier: float|None, suffix: str|None).
    suffix is 'ed' / 'st' for a recognized round frame, else None.
    Returns (None, None, None) when the frame is not a /g/cm round frame.
    Never raises -- malformed payloads stop parsing and return what was read.
    """
    if not raw.startswith(CM_PREFIX):
        return None, None, None
    rest = raw[len(CM_PREFIX):]
    if rest.startswith(ED_SUFFIX):
        suffix = "ed"
    elif rest.startswith(ST_SUFFIX):
        suffix = "st"
    else:
        return None, None, None

    data = rest[2:]
    game_round_id = None
    multiplier = None
    pos = 0
    while pos < len(data):
        try:
            tag_byte = data[pos]; pos += 1
            wire_type = tag_byte & 0x07
            field_num = tag_byte >> 3
            if wire_type == 0:  # varint
                val = 0; shift = 0
                while True:
                    b = data[pos]; pos += 1
                    val |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                if field_num == 1:
                    game_round_id = str(val)
                elif field_num == 6:
                    m = val / 100.0
                    if 1.0 <= m <= 10000.0:
                        multiplier = round(m, 2)
            elif wire_type == 2:  # length-delimited -> skip
                ln = 0; shift = 0
                while True:
                    b = data[pos]; pos += 1
                    ln |= (b & 0x7F) << shift
                    if not (b & 0x80):
                        break
                    shift += 7
                pos += ln
            else:
                break
        except Exception:
            break
    return game_round_id, multiplier, suffix
