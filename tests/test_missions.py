"""File-backed mission library: missions/router.py."""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from missions.router import MAX_MISSION_BYTES, build_missions_router


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
