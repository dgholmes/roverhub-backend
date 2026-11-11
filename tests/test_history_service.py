"""history/service.py: building history from the environment never takes the
backend down; it either works or says why it does not."""
import sys

import pytest

from history.service import History
from history.store import LATEST_MIGRATION, HistoryStore
from history.db import Database


def test_sqlite_is_the_default_and_starts_migrated(tmp_path):
    history = History.from_env({"HISTORY_DB": str(tmp_path / "h.sqlite3")})
    assert history.available and history.engine == "sqlite"
    assert history.store.level() == LATEST_MIGRATION
    history.store.close()


def test_a_history_db_in_a_folder_that_does_not_exist_turns_history_off(tmp_path):
    history = History.from_env({"HISTORY_DB": str(tmp_path / "missing" / "h.sqlite3")})
    assert not history.available
    assert "does not exist" in history.error
    # Everything that would record something is a no-op, not an error.
    history.observe("telemetry", "site-a", "rover-a", {"battery_percent": 50})
    history.audit.record("map.status", "map-1", "active")
    assert history.flush() is True


def test_an_unreachable_postgres_turns_history_off_with_the_reason():
    pytest.importorskip("psycopg")
    history = History.from_env({"DATABASE_URL": "postgresql://nobody:secret@127.0.0.1:1/none"})
    assert not history.available
    assert "secret" not in history.error and "127.0.0.1" in history.error


def test_a_database_from_a_newer_backend_turns_history_off(tmp_path):
    path = str(tmp_path / "h.sqlite3")
    store = HistoryStore(Database(path))
    store.migrate()
    store.execute_raw("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                      (LATEST_MIGRATION + 1, "future", 0.0))
    store.close()
    history = History.from_env({"HISTORY_DB": path})
    assert not history.available
    assert f"migration {LATEST_MIGRATION + 1}" in history.error


def test_a_bad_setting_turns_history_off_rather_than_guessing(tmp_path):
    history = History.from_env({"HISTORY_DB": str(tmp_path / "h.sqlite3"), "HISTORY_RETENTION_DAYS": "forever"})
    assert not history.available
    assert history.error == "HISTORY_RETENTION_DAYS must be a number above 0, got 'forever'"


def test_postgres_without_psycopg_says_what_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)   # import psycopg -> ImportError
    history = History.from_env({"DATABASE_URL": "postgresql://u:p@127.0.0.1:1/none"})
    assert not history.available
    assert history.error == ("DATABASE_URL names Postgres but psycopg is not installed; "
                             "pip install -r requirements-postgres.txt")
