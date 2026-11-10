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
for _name in ("RUNS_DIR", "TASKS_DIR", "MISSIONS_DIR", "MAPS_DIR", "SNAPSHOTS_DIR", "AUDIO_CLIPS_DIR"):
    os.environ[_name] = os.path.join(_DATA, _name.lower())
os.environ["USERS_FILE"] = os.path.join(_DATA, "users.json")
