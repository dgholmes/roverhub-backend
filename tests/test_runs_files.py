from fastapi import FastAPI
from fastapi.testclient import TestClient

from runs.router import build_runs_router
from tests.test_runs import _run

RID = "20261016-060000-m-1-rvr-01"


def _client(tmp_path, changes=None):
    app = FastAPI()
    app.include_router(build_runs_router(str(tmp_path), on_change=(
        (lambda r, p: changes.append((r, p))) if changes is not None else (lambda r, p: None))))
    c = TestClient(app)
    c.put(f"/runs/{RID}", json=_run())
    return c


def test_upload_and_download_photo(tmp_path):
    changes = []
    c = _client(tmp_path, changes)
    r = c.put(f"/runs/{RID}/files/photos/02_north-fence_front_060412.jpg", content=b"\xff\xd8jpeg")
    assert r.status_code == 200 and r.json()["size_bytes"] == 6
    assert c.get(f"/runs/{RID}/files/photos/02_north-fence_front_060412.jpg").content == b"\xff\xd8jpeg"
    assert changes[-1] == (RID, "photos/02_north-fence_front_060412.jpg")
    assert c.get(f"/runs/{RID}").json()["files"] == [
        {"path": "photos/02_north-fence_front_060412.jpg", "size_bytes": 6}]


def test_rejects_paths_outside_the_layout(tmp_path):
    c = _client(tmp_path)
    for bad in ["run.json", "events.jsonl", "photos/../run.json", "other/x.jpg",
                "photos/sub/x.jpg", "photos/.hidden", "photos/a:b.jpg", "video/", "/etc/passwd"]:
        assert c.put(f"/runs/{RID}/files/{bad}", content=b"x").status_code in (400, 404), bad
    assert (tmp_path / RID / "run.json").read_bytes() != b"x"


def test_upload_to_unknown_run_404(tmp_path):
    c = _client(tmp_path)
    unknown = "20261016-070000-m-1-rvr-01"
    assert c.put(f"/runs/{unknown}/files/photos/a.jpg", content=b"x").status_code == 404


def test_size_limit_413(tmp_path, monkeypatch):
    import runs.store as store
    monkeypatch.setitem(store.MAX_FILE_BYTES, "photos", 4)
    c = _client(tmp_path)
    assert c.put(f"/runs/{RID}/files/photos/a.jpg", content=b"12345").status_code == 413
    assert c.get(f"/runs/{RID}/files/photos/a.jpg").status_code == 404


def test_range_request_returns_206(tmp_path):
    c = _client(tmp_path)
    c.put(f"/runs/{RID}/files/video/run_front.mp4", content=b"0123456789")
    r = c.get(f"/runs/{RID}/files/video/run_front.mp4", headers={"Range": "bytes=2-5"})
    assert r.status_code == 206
    assert r.content == b"2345"
    assert r.headers["content-range"] == "bytes 2-5/10"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "video/mp4"
    tail = c.get(f"/runs/{RID}/files/video/run_front.mp4", headers={"Range": "bytes=7-"})
    assert tail.content == b"789"
    bad = c.get(f"/runs/{RID}/files/video/run_front.mp4", headers={"Range": "bytes=20-30"})
    assert bad.status_code == 416


def test_file_upload_hook_runs_off_the_event_loop(tmp_path):
    import asyncio
    import threading
    seen = []

    def hook(run_id, path):
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        seen.append((path, on_loop, threading.current_thread()))

    app = FastAPI()
    app.include_router(build_runs_router(str(tmp_path), on_change=hook))
    c2 = TestClient(app)
    c2.put(f"/runs/{RID}", json=_run())
    assert c2.put(f"/runs/{RID}/files/photos/a.jpg", content=b"x").status_code in (200, 201, 204)
    path, on_loop, _ = [s for s in seen if s[0]][-1]
    assert path == "photos/a.jpg" and on_loop is False
