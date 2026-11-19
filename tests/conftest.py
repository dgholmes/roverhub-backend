"""Keep the suite independent of this machine's .env and data/.

Loaded before any test module, so before api/app.py is imported. load_dotenv()
never overrides a variable that is already set, so these win over a local .env
that turns sign-in on. The storage directories point at a throwaway folder
because tests that start the app (test_scheduler_wiring.py) run its start-up
pass, which settles records it finds -- it must never find the real ones.
"""
import atexit
import os
import shutil
import tempfile

os.environ["AUTH_ENABLED"] = "false"
os.environ["AUTH_ALLOW_UNKEYED_BRIDGES"] = "false"

_DATA = tempfile.mkdtemp(prefix="roverhub-backend-tests-")
atexit.register(shutil.rmtree, _DATA, ignore_errors=True)
# TTS_CACHE_DIR belongs here with the rest: control/tts.py resolves its default
# at import time, so pointing it at the throwaway folder has to happen before
# anything imports it. The per-test fixture below narrows it further.
for _name in (
    "RUNS_DIR", "TASKS_DIR", "MISSIONS_DIR", "MAPS_DIR", "SNAPSHOTS_DIR",
    "AUDIO_CLIPS_DIR", "TTS_CACHE_DIR",
):
    os.environ[_name] = os.path.join(_DATA, _name.lower())
os.environ["USERS_FILE"] = os.path.join(_DATA, "users.json")
# The app's history store (history/). SQLite in the throwaway folder, never the
# real data/history.sqlite3, and never a Postgres a developer's shell points at:
# the Postgres tests use TEST_DATABASE_URL, each in a schema of its own.
os.environ["HISTORY_DB"] = os.path.join(_DATA, "history.sqlite3")
os.environ.pop("DATABASE_URL", None)

import uuid  # noqa: E402

import pytest  # noqa: E402

_ENGINES = ["sqlite", "postgres"]


@pytest.fixture(params=_ENGINES)
def history_db(request, tmp_path):
    """A fresh, empty history Database on each engine: SQLite always, Postgres
    when TEST_DATABASE_URL is set (each test in a schema of its own, dropped
    afterwards). Unmigrated: tests that want tables call HistoryStore.migrate()."""
    from history.db import Database

    if request.param == "sqlite":
        yield Database(str(tmp_path / "history.sqlite3"))
        return
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url.startswith("postgres"):
        pytest.skip("set TEST_DATABASE_URL=postgresql://... to run the Postgres engine")
    schema = f"t_{uuid.uuid4().hex[:12]}"
    db = Database(url, schema=schema)
    yield db
    db.drop_schema()


@pytest.fixture
def history_store(history_db):
    from history.store import HistoryStore

    store = HistoryStore(history_db)
    store.migrate()
    yield store
    store.close()


@pytest.fixture(autouse=True)
def isolated_tts_cache(tmp_path, monkeypatch):
    """Gives every test its own synthesis cache directory.

    The module-level TTS_CACHE_DIR above already keeps the suite out of the
    working tree's data/tts-cache. This narrows it per test, so one test's
    cached audio can never answer another's request -- a test asserting that a
    synthesiser was called would otherwise pass on a hit left by an earlier one.
    """
    monkeypatch.setenv("TTS_CACHE_DIR", str(tmp_path / "tts-cache"))
