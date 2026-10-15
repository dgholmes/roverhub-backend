"""File-backed run records: runs/store.py, runs/router.py."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from runs.router import build_runs_router

MISSION = {
    "mission_id": "m-1", "name": "Patrol", "type": "patrol", "library_status": "draft",
    "creator": "x", "updated_at": "2026-10-15T00:00:00Z", "map_id": "map", "site_id": "site-a",
}


def _run(run_id="20261016-060000-m-1-rvr-01", started_at="2026-10-16T06:00:00Z", **extra):
    return {"run_id": run_id, "mission_id": "m-1", "mission": MISSION, "robot_id": "rvr-01",
            "site_id": "site-a", "started_at": started_at, "started_by": "operator-dev", **extra}


def _event(seq, kind="arrived", run_id="20261016-060000-m-1-rvr-01"):
    return {"run_id": run_id, "seq": seq, "at": f"2026-10-16T06:0{seq}:00Z", "kind": kind}


def _client(tmp_path, changes=None):
    app = FastAPI()
    app.include_router(build_runs_router(str(tmp_path), on_change=(
        (lambda run_id, path: changes.append((run_id, path))) if changes is not None else (lambda r, p: None))))
    return TestClient(app)


def test_put_and_get_run(tmp_path):
    client = _client(tmp_path)
    assert client.put("/runs/20261016-060000-m-1-rvr-01", json=_run()).status_code == 200
    body = client.get("/runs/20261016-060000-m-1-rvr-01").json()
    assert body["run"]["result"] == "running"
    assert body["files"] == [] and body["event_count"] == 0
    assert (tmp_path / "20261016-060000-m-1-rvr-01" / "run.json").is_file()


def test_put_run_id_mismatch_400_and_bad_id_400(tmp_path):
    client = _client(tmp_path)
    assert client.put("/runs/other", json=_run()).status_code == 400
    assert client.put("/runs/a..b:c", json=_run(run_id="a..b:c")).status_code in (400, 422)


def test_unknown_run_404(tmp_path):
    assert _client(tmp_path).get("/runs/nope").status_code == 404


def test_events_are_idempotent_by_seq_and_ordered(tmp_path):
    client = _client(tmp_path)
    client.put("/runs/20261016-060000-m-1-rvr-01", json=_run())
    first = client.post("/runs/20261016-060000-m-1-rvr-01/events", json={"events": [_event(1), _event(0)]})
    assert first.json() == {"added": 2, "total": 2}
    again = client.post("/runs/20261016-060000-m-1-rvr-01/events", json={"events": [_event(1), _event(2)]})
    assert again.json() == {"added": 1, "total": 3}
    seqs = [e["seq"] for e in client.get("/runs/20261016-060000-m-1-rvr-01/events").json()["items"]]
    assert seqs == [0, 1, 2]


def test_events_for_unknown_run_404_and_wrong_run_id_400(tmp_path):
    client = _client(tmp_path)
    assert client.post("/runs/nope/events", json={"events": [_event(0, run_id="nope")]}).status_code == 404
    client.put("/runs/20261016-060000-m-1-rvr-01", json=_run())
    bad = client.post("/runs/20261016-060000-m-1-rvr-01/events", json={"events": [_event(0, run_id="other")]})
    assert bad.status_code == 400


def test_list_filters_and_orders_newest_first(tmp_path):
    client = _client(tmp_path)
    client.put("/runs/r-old", json=_run(run_id="r-old", started_at="2026-10-14T06:00:00Z"))
    client.put("/runs/r-new", json=_run(run_id="r-new", started_at="2026-10-16T06:00:00Z"))
    client.put("/runs/r-other", json=_run(run_id="r-other", site_id="site-b"))
    items = client.get("/runs?site_id=site-a").json()["items"]
    assert [i["run_id"] for i in items] == ["r-new", "r-old"]
    assert client.get("/runs?since=2026-10-15T00:00:00Z&site_id=site-a").json()["total"] == 1
    page = client.get("/runs?limit=1").json()
    assert len(page["items"]) == 1 and page["next_offset"] == 1


def test_put_run_calls_on_change(tmp_path):
    changes = []
    client = _client(tmp_path, changes)
    client.put("/runs/20261016-060000-m-1-rvr-01", json=_run())
    assert changes == [("20261016-060000-m-1-rvr-01", None)]


def test_corrupt_run_json_is_skipped_in_list(tmp_path):
    client = _client(tmp_path)
    client.put("/runs/r-ok", json=_run(run_id="r-ok"))
    (tmp_path / "r-bad").mkdir()
    (tmp_path / "r-bad" / "run.json").write_text("{not json", encoding="utf-8")
    assert [i["run_id"] for i in client.get("/runs").json()["items"]] == ["r-ok"]


def test_concurrent_event_posts_lose_nothing(tmp_path):
    import threading
    client = _client(tmp_path)
    rid = "20261016-060000-m-1-rvr-01"
    client.put(f"/runs/{rid}", json=_run())
    def post(base):
        events = [{"run_id": rid, "seq": base + i, "at": "2026-10-16T06:00:00Z", "kind": "arrived"} for i in range(5)]
        assert client.post(f"/runs/{rid}/events", json={"events": events}).status_code == 200
    threads = [threading.Thread(target=post, args=(n * 5,)) for n in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = [e["seq"] for e in client.get(f"/runs/{rid}/events").json()["items"]]
    assert seqs == list(range(60))


def test_run_dir_rejects_traversal_and_bad_ids(tmp_path):
    import pytest
    from fastapi import HTTPException
    from runs.store import RunStore
    store = RunStore(str(tmp_path / "runs"))
    for bad in [".", "..", "a/b", "a\b", "a:b", "x\n", ""]:
        with pytest.raises(HTTPException) as exc:
            store.run_dir(bad)
        assert exc.value.status_code == 400, bad


def test_run_dir_rejects_symlink_escaping_root(tmp_path):
    import os
    import pytest
    from fastapi import HTTPException
    from runs.store import RunStore
    store = RunStore(str(tmp_path / "runs"))
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, tmp_path / "runs" / "evil", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted")
    with pytest.raises(HTTPException) as exc:
        store.run_dir("evil")
    assert exc.value.status_code == 400


def test_list_rejects_negative_offset_and_out_of_range_limit(tmp_path):
    client = _client(tmp_path)
    client.put("/runs/r-one", json=_run(run_id="r-one"))
    assert client.get("/runs?offset=-1").status_code == 422
    assert client.get("/runs?limit=0").status_code == 422
    assert client.get("/runs?limit=501").status_code == 422
    assert client.get("/runs?limit=500&offset=0").status_code == 200


def test_file_name_with_trailing_newline_is_rejected(tmp_path):
    import pytest
    from fastapi import HTTPException

    from runs.store import RunStore

    store = RunStore(str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        store.file_path("20261016-060000-m-1-rvr-01", "photos/a.jpg\n")
    assert exc.value.status_code == 400
