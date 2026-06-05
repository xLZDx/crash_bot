import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from storage import CrashStorage


@pytest.fixture
def storage(tmp_path):
    db = CrashStorage(str(tmp_path / "test.duckdb"))
    yield db
    db.close()


def test_insert_returns_id(storage):
    rid = storage.insert(3.45)
    assert rid is not None
    assert rid >= 1


def test_count_empty_then_grows(storage):
    assert storage.count() == 0
    storage.insert(2.0)
    storage.insert(1.5)
    assert storage.count() == 2


def test_last_multiplier_none_when_empty(storage):
    assert storage.last_multiplier() is None


def test_last_multiplier_returns_most_recent(storage):
    storage.insert(3.0)
    storage.insert(5.0)
    assert storage.last_multiplier() == 5.0


def test_upsert_deduplication_on_game_round_id(storage):
    import duckdb
    id1 = storage.insert(3.45, game_round_id="round-001")
    id2 = storage.insert(9.99, game_round_id="round-001")  # duplicate
    assert id1 is not None
    assert id2 is None  # ON CONFLICT DO NOTHING -> no RETURNING row
    assert storage.count() == 1
    with duckdb.connect(storage._path, read_only=True) as conn:
        row = conn.execute(
            "SELECT multiplier FROM rounds WHERE game_round_id='round-001'"
        ).fetchone()
    assert row[0] == 3.45  # first value preserved
    with duckdb.connect(storage._path, read_only=True) as conn:
        hb = conn.execute(
            "SELECT last_round_id, last_game_round_id FROM system_health "
            "WHERE component='collector'"
        ).fetchone()
    assert hb == (id1, "round-001")


def test_null_game_round_id_allows_multiple_rows(storage):
    storage.insert(1.5, game_round_id=None)
    storage.insert(2.5, game_round_id=None)
    assert storage.count() == 2


def test_bet_fields_stored_and_retrieved(storage):
    import duckdb
    storage.insert(2.0, total_bets=150.5, num_bettors=12, frame_event="round_end")
    with duckdb.connect(storage._path, read_only=True) as conn:
        row = conn.execute(
            "SELECT total_bets, num_bettors, frame_event FROM rounds"
        ).fetchone()
    assert row[0] == 150.5
    assert row[1] == 12
    assert row[2] == "round_end"


def test_export_csv_rejects_unsafe_paths(storage, tmp_path):
    storage.insert(2.0)
    with pytest.raises(ValueError):
        storage.export_csv("../../../etc/passwd")
    with pytest.raises(ValueError):
        storage.export_csv("file; DROP TABLE rounds;--")
    with pytest.raises(ValueError):
        storage.export_csv("path'with'quotes")


def test_export_csv_writes_valid_file(storage, tmp_path):
    storage.insert(2.5, game_round_id="r1")
    storage.insert(1.1, game_round_id="r2")
    out = str(tmp_path / "out.csv")
    storage.export_csv(out)
    assert os.path.exists(out)
    content = open(out, encoding="utf-8").read()
    assert "multiplier" in content
    assert "2.5" in content
    assert "1.1" in content


def test_insert_bet_never_stores_username(storage):
    """Usernames must never be persisted — privacy requirement."""
    import duckdb
    storage.insert_bet("round-1", "USDT", 10.0, username="alice")
    with duckdb.connect(storage._path, read_only=True) as conn:
        row = conn.execute("SELECT username FROM bets WHERE round_id='round-1'").fetchone()
    assert row is not None
    assert row[0] is None


def test_purge_stored_usernames_clears_legacy_data(storage):
    """purge_stored_usernames() must NULL any legacy usernames and return the count."""
    import duckdb
    # Bypass the privacy enforcement in insert_bet by writing directly via SQL
    with duckdb.connect(storage._path) as conn:
        conn.execute(
            "INSERT INTO bets (id, round_id, currency, amount, username) "
            "VALUES (nextval('seq_bet_id'), 'r1', 'USDT', 5.0, 'alice')"
        )
        conn.execute(
            "INSERT INTO bets (id, round_id, currency, amount, username) "
            "VALUES (nextval('seq_bet_id'), 'r1', 'USDT', 3.0, 'bob')"
        )
    count = storage.purge_stored_usernames()
    assert count == 2
    with duckdb.connect(storage._path, read_only=True) as conn:
        rows = conn.execute("SELECT username FROM bets").fetchall()
    assert all(row[0] is None for row in rows), "All usernames must be NULL after purge"


def test_purge_stored_usernames_returns_zero_when_already_clean(storage):
    """If no usernames are stored, purge returns 0 and is a no-op."""
    storage.insert_bet("r1", "USDT", 5.0, username="charlie")  # always NULL
    count = storage.purge_stored_usernames()
    assert count == 0


def test_raw_ws_log_flag_is_false_by_default():
    """_LOG_RAW_FRAMES must be False to keep raw frames out of logs by default."""
    import playwright_collector
    assert playwright_collector._LOG_RAW_FRAMES is False


def test_write_component_heartbeat_creates_and_updates_row(storage):
    import duckdb, time
    storage.write_component_heartbeat("training")
    with duckdb.connect(storage._path, read_only=True) as conn:
        row = conn.execute(
            "SELECT component FROM system_health WHERE component='training'"
        ).fetchone()
    assert row is not None

    # Second call updates without error
    time.sleep(0.01)
    storage.write_component_heartbeat("training")
    with duckdb.connect(storage._path, read_only=True) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM system_health WHERE component='training'"
        ).fetchone()[0]
    assert count == 1  # still only one row (upsert)
