import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from collector import parse_frame, _parse_bet_frame, BetResult


def test_crash_point_key_with_round_end_event():
    r = parse_frame('{"type": "round_end", "crash_point": 3.45}')
    assert r.multiplier == 3.45
    assert r.frame_event == "round_end"


def test_camel_case_crash_point():
    r = parse_frame('{"event": "crash", "crashPoint": 7.77}')
    assert r.multiplier == 7.77


def test_nested_multiplier():
    r = parse_frame('{"type": "bust", "data": {"crash_rate": 2.10}}')
    assert r.multiplier == pytest.approx(2.10)


def test_bets_array_summed():
    payload = '{"event": "round_end", "crash_point": 1.5, "bets": [{"amount": 10.0}, {"amount": 5.5}]}'
    r = parse_frame(payload)
    assert r.multiplier == 1.5
    assert r.total_bets == pytest.approx(15.5)


def test_total_bets_direct_key():
    payload = '{"type": "crashed", "crash_point": 4.0, "totalBets": 200.0, "playerCount": 15}'
    r = parse_frame(payload)
    assert r.multiplier == 4.0
    assert r.total_bets == 200.0
    assert r.num_bettors == 15


def test_chat_message_ignored_when_require_round_end():
    r = parse_frame('{"type": "chat", "crash_point": 1.23}', require_round_end=True)
    assert r.multiplier is None


def test_ping_ignored_when_require_round_end():
    r = parse_frame('{"type": "ping", "multiplier": 2.0}', require_round_end=True)
    assert r.multiplier is None


def test_no_event_type_still_processed():
    r = parse_frame('{"crash_point": 5.5}', require_round_end=True)
    assert r.multiplier == 5.5


def test_multiplier_below_range_rejected():
    r = parse_frame('{"type": "round_end", "crash_point": 0.5}')
    assert r.multiplier is None


def test_multiplier_above_range_rejected():
    r = parse_frame('{"type": "round_end", "crash_point": 200001.0}')
    assert r.multiplier is None


def test_game_round_id_extracted():
    payload = '{"type": "round_end", "round_id": "abc-123", "crash_point": 2.5}'
    r = parse_frame(payload)
    assert r.multiplier == 2.5
    assert r.game_round_id == "abc-123"


def test_round_number_in_text_not_captured_as_multiplier():
    """'#4521' must not be mistaken for a multiplier."""
    r = parse_frame("#4521")
    assert r.multiplier is None


def test_short_x_string_captured():
    r = parse_frame("2.45x", require_round_end=False)
    assert r.multiplier == 2.45


def test_bytes_payload_decoded():
    r = parse_frame(b'{"type": "crashed", "crash_point": 1.01}')
    assert r.multiplier == 1.01


def test_empty_bets_array_gives_none_not_zero():
    payload = '{"type": "round_end", "crash_point": 3.0, "bets": []}'
    r = parse_frame(payload)
    assert r.multiplier == 3.0
    assert r.total_bets is None


def test_invalid_json_returns_no_result():
    r = parse_frame("not json at all")
    assert r.multiplier is None


def test_negative_bets_rejected():
    payload = '{"type": "round_end", "crash_point": 2.0, "totalBets": -5.0}'
    r = parse_frame(payload)
    assert r.multiplier == 2.0
    assert r.total_bets is None


def test_all_event_type_field_names_recognized():
    for field in ("type", "event", "action"):
        payload = f'{{"{field}": "round_end", "crash_point": 1.5}}'
        r = parse_frame(payload)
        assert r.multiplier == 1.5, f"field={field!r} not recognized"


def test_require_round_end_false_passes_all():
    r = parse_frame('{"type": "chat", "crash_point": 1.23}', require_round_end=False)
    assert r.multiplier == 1.23


# ---------------------------------------------------------------------------
# Binary /g/cm protobuf round-end frames (bcgame protocol)
# ---------------------------------------------------------------------------

def _make_varint(n: int) -> bytes:
    """Encode n as a protobuf varint."""
    parts = []
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            parts.append(b | 0x80)
        else:
            parts.append(b)
            break
    return bytes(parts)


def _make_ed_frame(round_id: int, crash_x100: int) -> bytes:
    """Build a minimal /g/cm ed binary frame."""
    prefix = b'\x04\x02\x05/g/cm\x02ed'
    field1 = b'\x08' + _make_varint(round_id)   # tag field1 varint
    field6 = b'\x30' + _make_varint(crash_x100)  # tag field6 (0x30) varint
    return prefix + field1 + field6


def test_binary_ed_frame_round_301():
    # round 9254394, crash 3.01x (301 in wire)
    frame = _make_ed_frame(9254394, 301)
    r = parse_frame(frame)
    assert r.multiplier == pytest.approx(3.01)
    assert r.game_round_id == "9254394"
    assert r.frame_event == "binary_ed"


def test_binary_ed_frame_instant_bust():
    frame = _make_ed_frame(1000001, 100)  # 1.00x exactly
    r = parse_frame(frame)
    assert r.multiplier == pytest.approx(1.00)


def test_binary_ed_frame_high_multiplier():
    frame = _make_ed_frame(9999999, 21180)  # 211.80x
    r = parse_frame(frame)
    assert r.multiplier == pytest.approx(211.80)
    assert r.game_round_id == "9999999"


def test_binary_st_frame_also_parsed():
    prefix = b'\x04\x02\x05/g/cm\x02st'
    payload = prefix + b'\x08' + _make_varint(9254394) + b'\x30' + _make_varint(301)
    r = parse_frame(payload)
    assert r.multiplier == pytest.approx(3.01)
    assert r.frame_event == "binary_st"


def test_binary_st_frame_extracts_hash():
    pf_hash = "397f87670020fcc74b929c41dca647922be91206f35db0bd9bf27a8596bf2d33"
    prefix = b'\x04\x02\x05/g/cm\x02st'
    hash_bytes = pf_hash.encode("ascii")
    hash_field = b'\x3a' + bytes([len(hash_bytes)]) + hash_bytes  # field7, len-delimited
    payload = prefix + b'\x08' + _make_varint(9254394) + b'\x30' + _make_varint(301) + hash_field
    r = parse_frame(payload)
    assert r.multiplier == pytest.approx(3.01)
    assert r.hash == pf_hash


def test_binary_ed_frame_no_hash():
    frame = _make_ed_frame(9254394, 301)
    r = parse_frame(frame)
    assert r.hash is None


def test_binary_non_ed_frame_ignored():
    # \x02pg (game-clock ping) must not produce a result
    frame = b'\x04\x02\x05/g/cm\x02pg\x08\xc7\x04'
    r = parse_frame(frame)
    assert r.multiplier is None


def test_binary_ed_out_of_range_rejected():
    # crash_x100 = 0 → 0.00x < _MIN_M
    frame = _make_ed_frame(1, 0)
    r = parse_frame(frame)
    assert r.multiplier is None


def test_binary_ed_extra_fields_ignored():
    frame = _make_ed_frame(100, 134)
    frame += b'\x3a\x04test'  # field7, 4 bytes "test"
    r = parse_frame(frame)
    assert r.multiplier == pytest.approx(1.34)


# ---------------------------------------------------------------------------
# Bet frame (\x01b) parsing
# ---------------------------------------------------------------------------

def _make_pb_string(field_num: int, s: str) -> bytes:
    """Encode a length-delimited protobuf field."""
    tag = (field_num << 3) | 2
    b = s.encode("utf-8")
    return bytes([tag, len(b)]) + b


def _make_bet_frame(round_id: int, currency: str, amount_str: str, username: str = "") -> bytes:
    prefix = b'\x04\x02\x05/g/cm\x01b'
    f1 = b'\x08' + _make_varint(round_id)
    f2 = _make_pb_string(2, currency)
    f3 = _make_pb_string(3, amount_str)
    f5 = _make_pb_string(5, username) if username else b''
    return prefix + f1 + f2 + f3 + f5


def test_bet_frame_parsed():
    frame = _make_bet_frame(9254395, "USDT", "0.0001", "alice")
    b = _parse_bet_frame(frame)
    assert b is not None
    assert b.round_id == "9254395"
    assert b.currency == "USDT"
    assert b.amount == pytest.approx(0.0001)
    assert b.username == "alice"


def test_bet_frame_no_username():
    frame = _make_bet_frame(1234, "BTC", "0.05")
    b = _parse_bet_frame(frame)
    assert b is not None
    assert b.currency == "BTC"
    assert b.amount == pytest.approx(0.05)
    assert b.username in ("", None)


def test_non_bet_frame_returns_none():
    # ed frame should not parse as a bet
    frame = _make_ed_frame(100, 200)
    assert _parse_bet_frame(frame) is None


def test_bet_frame_real_sample():
    # Real sample from ws_debug.log
    raw = b'\x04\x02\x05/g/cm\x01b\x08\xfb\xeb\xb4\x04\x12\x04USDT\x1a\x060.0001 \x88\xd2\xc22*\nmehope7721'
    b = _parse_bet_frame(raw)
    assert b is not None
    assert b.round_id == "9254395"
    assert b.currency == "USDT"
    assert b.amount == pytest.approx(0.0001)
