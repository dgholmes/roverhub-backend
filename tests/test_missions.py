"""File-backed mission library: missions/router.py."""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from missions.router import MAX_MISSION_BYTES, build_missions_router
from shared.schemas.tasks import MissionTask, TaskOccurrence
from tasks.store import TaskStore


class _Clock:
    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return f"2026-10-15T00:00:{self.n:02d}Z"


def _client(tmp_path, clock=None):
    app = FastAPI()
    app.include_router(build_missions_router(str(tmp_path), clock=clock or _Clock()))
    return TestClient(app)


def _entry(mission_id="m-1", site_id="site-a", **extra):
    return {
        "mission_id": mission_id, "name": f"Patrol {mission_id}", "type": "patrol",
        "kind": "route", "creator": "operator-dev",
        "updated_at": "1999-01-01T00:00:00Z", "map_id": "map-1",
        "site_id": site_id, **extra,
    }


def test_put_creates_and_get_returns_it(tmp_path):
    client = _client(tmp_path)
    put = client.put("/missions/m-1", json=_entry(kind="program", program_state='{"blocks":{}}'))
    assert put.status_code == 200
    assert put.json()["program_state"] == '{"blocks":{}}'
    assert (tmp_path / "m-1.json").is_file()
    assert client.get("/missions/m-1").json() == put.json()


def test_server_sets_updated_at(tmp_path):
    client = _client(tmp_path)
    body = client.put("/missions/m-1", json=_entry()).json()
    assert body["updated_at"] == "2026-10-15T00:00:01Z"
    assert client.get("/missions/m-1").json()["updated_at"] == "2026-10-15T00:00:01Z"


def test_default_clock_stamps_utc_now(tmp_path):
    app = FastAPI()
    app.include_router(build_missions_router(str(tmp_path)))
    body = TestClient(app).put("/missions/m-1", json=_entry()).json()
    assert body["updated_at"].endswith("Z") and body["updated_at"] > "2026"


def test_list_is_newest_first_and_filters_by_site(tmp_path):
    client = _client(tmp_path)
    client.put("/missions/a", json=_entry("a", "site-a"))
    client.put("/missions/b", json=_entry("b", "site-b"))
    client.put("/missions/c", json=_entry("c", "site-a"))
    assert [m["mission_id"] for m in client.get("/missions").json()["items"]] == ["c", "b", "a"]
    site_a = client.get("/missions", params={"site_id": "site-a"}).json()["items"]
    assert [m["mission_id"] for m in site_a] == ["c", "a"]
    assert client.get("/missions", params={"site_id": "nowhere"}).json() == {"items": []}


def test_put_replaces_and_moves_to_the_top(tmp_path):
    client = _client(tmp_path)
    client.put("/missions/a", json=_entry("a"))
    client.put("/missions/b", json=_entry("b"))
    client.put("/missions/a", json={**_entry("a"), "name": "Renamed"})
    items = client.get("/missions").json()["items"]
    assert [m["mission_id"] for m in items] == ["a", "b"]
    assert items[0]["name"] == "Renamed"


def test_survives_a_new_router_on_the_same_directory(tmp_path):
    _client(tmp_path).put("/missions/m-1", json=_entry())
    assert _client(tmp_path).get("/missions/m-1").status_code == 200


def test_delete_then_404(tmp_path):
    client = _client(tmp_path)
    client.put("/missions/m-1", json=_entry())
    assert client.delete("/missions/m-1").status_code == 204
    assert not (tmp_path / "m-1.json").exists()
    assert client.get("/missions/m-1").status_code == 404
    assert client.delete("/missions/m-1").status_code == 404


def test_unknown_mission_is_404(tmp_path):
    assert _client(tmp_path).get("/missions/nope").status_code == 404


def test_bad_ids_are_400(tmp_path):
    client = _client(tmp_path)
    # "." and ".." are normalised away by the HTTP client before they reach
    # the route, so they are covered against the store directly below.
    for bad in ["a%5Cb", "a:b", "a b", "x" * 129]:
        assert client.get(f"/missions/{bad}").status_code == 400, bad
        assert client.put(f"/missions/{bad}", json=_entry(bad)).status_code == 400, bad
        assert client.delete(f"/missions/{bad}").status_code == 400, bad


def test_dot_ids_are_rejected_by_the_store(tmp_path):
    import pytest
    from fastapi import HTTPException

    from missions.router import MissionStore

    store = MissionStore(str(tmp_path))
    for bad in [".", "..", "../x", "a/b", "a\b", ""]:
        with pytest.raises(HTTPException) as exc:
            store.get(bad)
        assert exc.value.status_code == 400, bad


def test_traversal_never_escapes_the_directory(tmp_path):
    store = tmp_path / "missions"
    client = _client(store)
    response = client.put("/missions/..%2Fescaped", json=_entry("../escaped"))
    assert response.status_code in (400, 404)
    assert not (tmp_path / "escaped.json").exists()


def test_mismatched_body_id_is_400(tmp_path):
    client = _client(tmp_path)
    assert client.put("/missions/m-1", json=_entry("m-2")).status_code == 400
    assert list(tmp_path.iterdir()) == []


def test_invalid_body_is_422(tmp_path):
    client = _client(tmp_path)
    bad = _entry()
    del bad["map_id"]
    assert client.put("/missions/m-1", json=bad).status_code == 422
    assert client.put("/missions/m-1", content=b"not json",
                      headers={"content-type": "application/json"}).status_code == 422


def test_oversized_body_is_413(tmp_path):
    client = _client(tmp_path)
    huge = _entry(kind="program", program_state="x" * (MAX_MISSION_BYTES + 1))
    assert client.put("/missions/m-1", json=huge).status_code == 413
    assert not (tmp_path / "m-1.json").exists()


def test_corrupt_files_are_skipped_not_500(tmp_path, caplog):
    client = _client(tmp_path)
    client.put("/missions/good", json=_entry("good"))
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "invalid.json").write_text(json.dumps({"mission_id": "invalid"}), encoding="utf-8")
    (tmp_path / "liar.json").write_text(json.dumps(_entry("other")), encoding="utf-8")
    response = client.get("/missions")
    assert response.status_code == 200
    assert [m["mission_id"] for m in response.json()["items"]] == ["good"]
    assert "skipping" in caplog.text


def test_no_temp_files_left_behind(tmp_path):
    client = _client(tmp_path)
    client.put("/missions/m-1", json=_entry())
    client.put("/missions/m-1", json=_entry())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m-1.json"]


def test_a_create_is_stored_at_version_1_and_each_later_write_increments(tmp_path):
    client = _client(tmp_path)
    created = client.put("/missions/m-1", json=_entry()).json()  # the body's default version is 1
    assert created["version"] == 1                               # a first version, not 2
    second = client.put("/missions/m-1", json={**_entry(), "version": created["version"]}).json()
    assert second["version"] == 2
    third = client.put("/missions/m-1", json={**_entry(), "version": second["version"]}).json()
    assert third["version"] == 3
    assert client.get("/missions/m-1").json()["version"] == 3
    # And it is what is on disk, for anyone opening the file.
    assert json.loads((tmp_path / "m-1.json").read_text(encoding="utf-8"))["version"] == 3


def test_a_create_is_stored_at_version_1_whatever_version_the_body_carries(tmp_path):
    client = _client(tmp_path)
    created = client.put("/missions/m-1", json={**_entry(), "version": 99}).json()
    assert created["version"] == 1
    assert json.loads((tmp_path / "m-1.json").read_text(encoding="utf-8"))["version"] == 1


def test_put_with_stale_version_returns_409_and_the_stored_mission(tmp_path):
    client = _client(tmp_path)
    created = client.put("/missions/m-1", json=_entry()).json()                     # stored at 1
    winner = client.put("/missions/m-1", json={**_entry(), "name": "Winner", "version": created["version"]})
    assert winner.status_code == 200 and winner.json()["version"] == 2
    # A second writer who also read version 1 now holds a stale copy. The body is
    # otherwise valid and names the right id, so only the version can refuse it.
    loser = client.put("/missions/m-1", json={**_entry(), "name": "Loser", "version": created["version"]})
    assert loser.status_code == 409
    assert loser.json() == winner.json()  # the stored mission, so the console can show what changed
    stored = client.get("/missions/m-1").json()
    assert stored["name"] == "Winner" and stored["version"] == 2  # the loser wrote nothing
    assert stored["updated_at"] == winner.json()["updated_at"]    # not even a timestamp


def test_put_with_a_version_ahead_of_the_stored_one_is_also_409(tmp_path):
    client = _client(tmp_path)
    client.put("/missions/m-1", json=_entry())
    assert client.put("/missions/m-1", json={**_entry(), "version": 99}).status_code == 409


# -- deleting a mission cascades to its tasks and their queue (2026-10-26) -----

def _cascade_client(tmp_path):
    app = FastAPI()
    app.include_router(build_missions_router(
        str(tmp_path / "missions"), clock=_Clock(), tasks_dir=str(tmp_path / "tasks"),
    ))
    return TestClient(app), TaskStore(str(tmp_path / "tasks"))


def _task(task_id, mission_id):
    return MissionTask(
        task_id=task_id, mission_id=mission_id, site_id="site-a", robot_id="robot-1",
        created_by="operator-dev", created_at="2026-10-23T00:00:00Z",
        updated_at="2026-10-23T00:00:00Z",
    )


def _occ(task_id, mission_id, due_at, **extra):
    compact = due_at.replace("-", "").replace(":", "")
    return TaskOccurrence(
        occurrence_id=f"{task_id}-{compact}", task_id=task_id, mission_id=mission_id,
        site_id="site-a", robot_id="robot-1", due_at=due_at, **extra,
    )


def test_deleting_a_mission_deletes_its_tasks_and_their_unstarted_occurrences(tmp_path, caplog):
    client, tasks = _cascade_client(tmp_path)
    client.put("/missions/m-1", json=_entry("m-1"))
    client.put("/missions/m-2", json=_entry("m-2"))
    for task_id, mission_id in (("t-1", "m-1"), ("t-2", "m-1"), ("t-3", "m-2")):
        tasks.put_task(_task(task_id, mission_id))
        tasks.put_occurrence(_occ(task_id, mission_id, "2026-10-25T09:00:00Z"))

    with caplog.at_level("WARNING", logger="roverhub.missions"):
        assert client.delete("/missions/m-1").status_code == 204

    assert [t.task_id for t in tasks.list_tasks()] == ["t-3"]
    assert [o.task_id for o in tasks.list_occurrences()] == ["t-3"]
    assert client.get("/missions/m-2").status_code == 200
    assert "m-1" in caplog.text and "t-1" in caplog.text and "t-2" in caplog.text


def test_deleting_a_mission_keeps_its_runs_history(tmp_path):
    client, tasks = _cascade_client(tmp_path)
    client.put("/missions/m-1", json=_entry("m-1"))
    tasks.put_task(_task("t-1", "m-1"))
    ran = _occ("t-1", "m-1", "2026-10-22T09:00:00Z", status="succeeded", run_id="r-1")
    flying = _occ("t-1", "m-1", "2026-10-23T09:00:00Z", status="running", run_id="r-2")
    for occ in (ran, flying):
        tasks.put_occurrence(occ)

    assert client.delete("/missions/m-1").status_code == 204

    assert tasks.get_task("t-1") is None
    assert {o.occurrence_id for o in tasks.list_occurrences()} == {ran.occurrence_id, flying.occurrence_id}


def test_deleting_an_unknown_mission_cleans_leftover_tasks_and_is_still_404(tmp_path):
    client, tasks = _cascade_client(tmp_path)
    tasks.put_task(_task("t-1", "gone"))
    tasks.put_occurrence(_occ("t-1", "gone", "2026-10-25T09:00:00Z"))

    assert client.delete("/missions/gone").status_code == 404
    assert tasks.list_tasks() == [] and tasks.list_occurrences() == []
    assert client.delete("/missions/gone").status_code == 404


def test_without_a_tasks_dir_deleting_a_mission_touches_no_tasks(tmp_path):
    # The default must not reach for the real data/tasks.
    client = _client(tmp_path)
    client.put("/missions/m-1", json=_entry())
    assert client.delete("/missions/m-1").status_code == 204
