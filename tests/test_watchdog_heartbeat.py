"""Tests for watchdog heartbeat granularity additions (PR6)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import duckdb
import pytest

from storage import CrashStorage
import watchdog as wd


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "wdog.duckdb")
    CrashStorage(path).close()
    return path


def test_component_heartbeat_age_returns_inf_when_no_row(db_path, monkeypatch):
    monkeypatch.setattr(wd, "DB_PATH", db_path)
    age = wd._component_heartbeat_age("training")
    assert age == float("inf")


def test_component_heartbeat_age_returns_recent_after_write(db_path, monkeypatch):
    monkeypatch.setattr(wd, "DB_PATH", db_path)
    CrashStorage(db_path).write_component_heartbeat("training")
    age = wd._component_heartbeat_age("training")
    assert 0.0 <= age < 10.0


def test_training_heartbeat_age_delegates_to_training_component(db_path, monkeypatch):
    monkeypatch.setattr(wd, "DB_PATH", db_path)
    assert wd.training_heartbeat_age() == float("inf")
    CrashStorage(db_path).write_component_heartbeat("training")
    assert wd.training_heartbeat_age() < 10.0


def test_collector_heartbeat_age_uses_collector_component(db_path, monkeypatch):
    """collector component is written by storage.insert() — unrelated to training."""
    monkeypatch.setattr(wd, "DB_PATH", db_path)
    # Before any insert the collector heartbeat should be inf
    assert wd.collector_heartbeat_age() == float("inf")
    CrashStorage(db_path).insert(2.0, game_round_id="r1")
    assert wd.collector_heartbeat_age() < 10.0


def test_watchdog_thresholds_are_reasonable():
    assert wd.TRAINING_WARN_SEC > wd.CHECK_SEC
    assert wd.TRAINING_RESTART_SEC > wd.TRAINING_WARN_SEC
    # At TRAIN_INTERVAL_S=60 + ~100s cycle, warn at 600s = ~4 missed full cycles
    assert wd.TRAINING_WARN_SEC >= 300
    assert wd.TRAINING_RESTART_SEC >= 900


def test_component_heartbeat_age_returns_none_on_bad_db(monkeypatch):
    """DB error returns None (not inf) so callers can distinguish from missing-row."""
    monkeypatch.setattr(wd, "DB_PATH", "/nonexistent/path/crash.duckdb")
    age = wd._component_heartbeat_age("training")
    assert age is None


def test_check_training_skips_restart_when_heartbeat_db_unreadable(monkeypatch):
    """When heartbeat returns None (DB error) check_training must NOT kill the process."""
    import psutil
    monkeypatch.setattr(wd, "training_heartbeat_age", lambda: None)
    # Fake a running training process
    fake_proc = type("P", (), {"pid": 99999, "cmdline": lambda self: ["training.py"]})()
    monkeypatch.setattr(wd, "find_procs", lambda tokens: [fake_proc])
    killed = []
    monkeypatch.setattr(wd, "kill_proc_tree", lambda p: killed.append(p))
    monkeypatch.setattr(wd, "start_proc", lambda name: None)
    wd.check_training()
    assert killed == [], "must not kill process when DB is unreadable"
