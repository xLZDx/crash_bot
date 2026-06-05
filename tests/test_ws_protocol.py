"""Tests for ws_protocol.parse_round_frame (shared WS frame parser)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ws_protocol import parse_round_frame, CM_PREFIX


def _varint(v: int) -> bytes:
    out = bytearray()
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


def _frame(suffix: str, gid: int, mult_x100: int) -> bytes:
    # protobuf: field1 (gid) varint tag 0x08, field6 (mult*100) varint tag 0x30
    body = bytes([0x08]) + _varint(gid) + bytes([0x30]) + _varint(mult_x100)
    return CM_PREFIX + suffix.encode() + body


def test_parse_ed_frame():
    gid, mult, suf = parse_round_frame(_frame("ed", 12345, 210))
    assert gid == "12345" and mult == 2.10 and suf == "ed"


def test_parse_st_frame():
    gid, mult, suf = parse_round_frame(_frame("st", 999, 105))
    assert gid == "999" and mult == 1.05 and suf == "st"


def test_non_cm_frame_returns_none_triple():
    assert parse_round_frame(b"\x04\x02hello world") == (None, None, None)


def test_unknown_suffix_rejected():
    assert parse_round_frame(CM_PREFIX + b"zz" + bytes([0x08, 0x01])) == (None, None, None)


def test_truncated_frame_no_exception():
    # recognized 'ed' but a tag with no following varint -> stops safely
    gid, mult, suf = parse_round_frame(CM_PREFIX + b"ed" + b"\x08")
    assert suf == "ed" and gid is None and mult is None


def test_high_multiplier_rejected():
    # 20000.00x is out of [1, 10000] range -> multiplier stays None, gid still parsed
    gid, mult, suf = parse_round_frame(_frame("ed", 1, 2_000_000))
    assert gid == "1" and mult is None and suf == "ed"


def test_empty_input():
    assert parse_round_frame(b"") == (None, None, None)
