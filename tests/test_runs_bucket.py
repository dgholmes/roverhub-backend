import os

from runs.bucket import BucketCopier, from_env
from runs.store import RunStore
from shared.schemas.runs import MissionRun
from tests.test_runs import _run

RID = "20261016-060000-m-1-rvr-01"


class FakeS3:
    def __init__(self, fail=False):
        self.objects, self.fail = {}, fail

    def upload_file(self, filename, bucket, key):
        if self.fail:
            raise RuntimeError("network down")
        with open(filename, "rb") as h:
            self.objects[(bucket, key)] = h.read()


def _setup(tmp_path, result="running", fail=False):
    store = RunStore(str(tmp_path))
    store.put(MissionRun.model_validate(_run(result=result)))
    os.makedirs(tmp_path / RID / "photos", exist_ok=True)
    (tmp_path / RID / "photos" / "a.jpg").write_bytes(b"jpg")
    s3 = FakeS3(fail)
    return store, s3, BucketCopier(store, s3, "runs-bucket", clock=lambda: "2026-10-16T07:00:00Z")


def test_running_run_is_not_copied(tmp_path):
    store, s3, copier = _setup(tmp_path)
    copier.on_change(RID, None)
    assert s3.objects == {}


def test_finished_run_is_copied_and_stamped(tmp_path):
    store, s3, copier = _setup(tmp_path, result="completed")
    copier.on_change(RID, None)
    keys = {k for (_, k) in s3.objects}
    assert keys == {f"site-a/{RID}/run.json", f"site-a/{RID}/photos/a.jpg"}
    assert store.get(RID).bucket_copied_at == "2026-10-16T07:00:00Z"
    assert b"2026-10-16T07:00:00Z" in s3.objects[("runs-bucket", f"site-a/{RID}/run.json")]


def test_late_file_on_finished_run_is_copied(tmp_path):
    store, s3, copier = _setup(tmp_path, result="completed")
    copier.on_change(RID, None)
    (tmp_path / RID / "photos" / "b.jpg").write_bytes(b"late")
    copier.on_change(RID, "photos/b.jpg")
    assert s3.objects[("runs-bucket", f"site-a/{RID}/photos/b.jpg")] == b"late"


def test_failure_is_logged_not_raised_and_not_stamped(tmp_path):
    store, s3, copier = _setup(tmp_path, result="completed", fail=True)
    copier.on_change(RID, None)
    assert store.get(RID).bucket_copied_at is None


def test_stamp_failure_does_not_raise(tmp_path, monkeypatch):
    store, s3, copier = _setup(tmp_path, result="completed")

    def boom(run):
        raise OSError("disk full")

    monkeypatch.setattr(store, "put", boom)
    copier.on_change(RID, None)


def test_unknown_run_does_not_raise(tmp_path):
    store, s3, copier = _setup(tmp_path, result="completed")
    copier.on_change("20261016-070000-m-9-rvr-01", None)
    assert s3.objects == {}


def test_runs_under_the_stores_run_lock(tmp_path):
    store, s3, copier = _setup(tmp_path, result="completed")
    held = []
    real_get = store.get

    def spy(run_id):
        # RLock has no public owner query; a non-blocking acquire from another
        # thread fails iff someone holds it.
        import threading
        result = []
        t = threading.Thread(target=lambda: result.append(store.lock(run_id).acquire(blocking=False)))
        t.start()
        t.join()
        held.append(not result[0])
        return real_get(run_id)

    store.get = spy
    copier.on_change(RID, None)
    assert held and all(held)


def test_from_env_off_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("RUNS_BUCKET", raising=False)
    assert from_env(RunStore(str(tmp_path))) is None


def test_events_posted_after_stamping_reach_the_bucket(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from runs.router import build_runs_router
    from tests.test_runs import _event

    store, s3, copier = _setup(tmp_path, result="completed")
    app = FastAPI()
    app.include_router(build_runs_router(str(tmp_path), on_change=copier.on_change))
    client = TestClient(app)
    copier.on_change(RID, None)
    assert store.get(RID).bucket_copied_at
    s3.objects.clear()

    added = client.post(f"/runs/{RID}/events", json={"events": [_event(0, run_id=RID)]})
    assert added.json()["added"] == 1
    assert set(s3.objects) == {("runs-bucket", f"site-a/{RID}/events.jsonl"),
                               ("runs-bucket", f"site-a/{RID}/run.json")}
    assert b'"seq":0' in s3.objects[("runs-bucket", f"site-a/{RID}/events.jsonl")].replace(b" ", b"")

    s3.objects.clear()
    client.post(f"/runs/{RID}/events", json={"events": [_event(0, run_id=RID)]})
    assert s3.objects == {}  # nothing new, nothing uploaded


def test_change_on_unstamped_finished_run_does_full_copy(tmp_path):
    store, s3, copier = _setup(tmp_path, result="completed")
    copier.on_change(RID, "events.jsonl")
    keys = {k for (_, k) in s3.objects}
    assert f"site-a/{RID}/photos/a.jpg" in keys and f"site-a/{RID}/run.json" in keys
