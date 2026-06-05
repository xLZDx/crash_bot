"""Replay tests for PlaywrightCollector — frame dispatch, dedup, storage wiring."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, call

from playwright_collector import PlaywrightCollector, _SEEN_MAX


# ---------------------------------------------------------------------------
# Binary frame builders (same wire protocol as test_collector.py)
# ---------------------------------------------------------------------------

def _make_varint(n: int) -> bytes:
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
    """Build a minimal /g/cm 'ed' (round-end) binary frame."""
    prefix = b'\x04\x02\x05/g/cm\x02ed'
    return prefix + b'\x08' + _make_varint(round_id) + b'\x30' + _make_varint(crash_x100)


def _make_ping_frame() -> bytes:
    """Build a /g/cm 'pg' (progress ping) frame — should be ignored."""
    return b'\x04\x02\x05/g/cm\x02pg\x08\xc7\x04'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def storage():
    return MagicMock()


@pytest.fixture
def collector(storage):
    return PlaywrightCollector(storage=storage)


# ---------------------------------------------------------------------------
# _on_frame — dispatch (bytes and dict paths)
# ---------------------------------------------------------------------------

def test_on_frame_bytes_calls_storage_insert(collector, storage):
    collector._on_frame(_make_ed_frame(9254394, 301))  # 3.01x
    storage.insert.assert_called_once()


def test_on_frame_bytearray_calls_storage_insert(collector, storage):
    collector._on_frame(bytearray(_make_ed_frame(100, 200)))  # 2.00x
    storage.insert.assert_called_once()


def test_on_frame_dict_with_bytes_payload_calls_storage_insert(collector, storage):
    collector._on_frame({"payload": _make_ed_frame(9000001, 150)})  # 1.50x
    storage.insert.assert_called_once()


def test_on_frame_dict_with_bytearray_payload_calls_storage_insert(collector, storage):
    collector._on_frame({"payload": bytearray(_make_ed_frame(9000002, 250))})  # 2.50x
    storage.insert.assert_called_once()


def test_on_frame_ping_frame_is_ignored(collector, storage):
    collector._on_frame(_make_ping_frame())
    storage.insert.assert_not_called()


def test_on_frame_empty_bytes_does_not_raise_or_insert(collector, storage):
    collector._on_frame(b'')
    storage.insert.assert_not_called()


def test_on_frame_garbage_bytes_does_not_raise_or_insert(collector, storage):
    collector._on_frame(b'\xde\xad\xbe\xef' * 16)
    storage.insert.assert_not_called()


def test_on_frame_dict_with_string_payload_ignored(collector, storage):
    # payload must be bytes/bytearray; a plain string is not decoded
    collector._on_frame({"payload": "not-bytes"})
    storage.insert.assert_not_called()


def test_on_frame_dict_missing_payload_key_ignored(collector, storage):
    collector._on_frame({"other_key": b'\x00'})
    storage.insert.assert_not_called()


def test_on_frame_non_bytes_non_dict_ignored(collector, storage):
    # None, int, etc. should not raise or insert
    for val in (None, 42, "string", [b'\x00']):
        collector._on_frame(val)
    storage.insert.assert_not_called()


# ---------------------------------------------------------------------------
# _on_frame — correct multiplier and round_id propagation
# ---------------------------------------------------------------------------

def test_on_frame_passes_correct_multiplier(collector, storage):
    collector._on_frame(_make_ed_frame(9254394, 301))  # 3.01x
    _, kwargs = storage.insert.call_args
    assert kwargs["multiplier"] == pytest.approx(3.01)


def test_on_frame_passes_correct_game_round_id(collector, storage):
    collector._on_frame(_make_ed_frame(9254394, 301))
    _, kwargs = storage.insert.call_args
    assert kwargs["game_round_id"] == "9254394"


def test_on_frame_instant_bust_stored(collector, storage):
    collector._on_frame(_make_ed_frame(1000001, 100))  # 1.00x
    _, kwargs = storage.insert.call_args
    assert kwargs["multiplier"] == pytest.approx(1.00)


def test_on_frame_high_multiplier_stored(collector, storage):
    collector._on_frame(_make_ed_frame(999999, 21180))  # 211.80x
    _, kwargs = storage.insert.call_args
    assert kwargs["multiplier"] == pytest.approx(211.80)


# ---------------------------------------------------------------------------
# _store_round — storage.insert() call signature
# ---------------------------------------------------------------------------

def test_store_round_passes_required_kwargs(collector, storage):
    collector._store_round(3.45, "round-abc")
    storage.insert.assert_called_once_with(
        multiplier=pytest.approx(3.45),
        source="playwright_ws",
        game_round_id="round-abc",
        frame_event="round_complete",
    )


def test_store_round_increments_rounds_counter(collector, storage):
    assert collector._rounds == 0
    collector._store_round(2.0, "r1")
    collector._store_round(3.0, "r2")
    assert collector._rounds == 2


def test_store_round_does_not_increment_rounds_on_db_error(collector, storage):
    storage.insert.side_effect = RuntimeError("db unavailable")
    collector._store_round(2.0, "r-err")
    assert collector._rounds == 0


# ---------------------------------------------------------------------------
# _store_round — duplicate deduplication via _seen_ids
# ---------------------------------------------------------------------------

def test_store_round_dedup_same_round_id_stored_once(collector, storage):
    collector._store_round(3.01, "r-dup")
    collector._store_round(3.01, "r-dup")
    storage.insert.assert_called_once()


def test_store_round_dedup_two_different_rounds_each_stored(collector, storage):
    collector._store_round(3.01, "r-001")
    collector._store_round(4.55, "r-002")
    assert storage.insert.call_count == 2


def test_store_round_none_round_id_always_stored(collector, storage):
    # round_id=None skips the _seen_ids check — each call goes through
    collector._store_round(2.0, None)
    collector._store_round(3.0, None)
    assert storage.insert.call_count == 2


def test_store_round_dedup_does_not_add_duplicate_to_seen_ids(collector, storage):
    collector._store_round(3.01, "r-dup")
    collector._store_round(3.01, "r-dup")
    assert list(collector._seen_ids.keys()) == ["r-dup"]


# ---------------------------------------------------------------------------
# _store_round — LRU eviction when _seen_ids exceeds _SEEN_MAX
# ---------------------------------------------------------------------------

def test_seen_ids_evicts_oldest_when_capacity_exceeded(collector, storage):
    # Pre-populate _seen_ids to exactly _SEEN_MAX entries
    for i in range(_SEEN_MAX):
        collector._seen_ids[str(i)] = True

    # Adding one more via _store_round should evict the oldest entry ("0")
    collector._store_round(1.5, str(_SEEN_MAX))

    assert len(collector._seen_ids) == _SEEN_MAX
    assert "0" not in collector._seen_ids        # evicted
    assert str(_SEEN_MAX) in collector._seen_ids  # newly added


def test_evicted_round_id_can_be_reinserted(collector, storage):
    # Fill to capacity with IDs "0".."_SEEN_MAX - 1"
    for i in range(_SEEN_MAX):
        collector._seen_ids[str(i)] = True

    # Evict "0" by adding a new one
    collector._store_round(1.5, str(_SEEN_MAX))
    assert "0" not in collector._seen_ids

    # "0" is gone from dedup cache — inserting it again should call storage.insert
    storage.insert.reset_mock()
    collector._store_round(2.0, "0")
    storage.insert.assert_called_once()


def test_seen_ids_never_exceeds_seen_max(collector, storage):
    for i in range(_SEEN_MAX + 50):
        collector._store_round(1.5, str(i))
    assert len(collector._seen_ids) <= _SEEN_MAX
