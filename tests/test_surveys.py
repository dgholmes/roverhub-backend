"""The survey replay store and its routes (survey replay spec §5.5)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from surveys.router import build_surveys_router


def client(tmp_path):
    app = FastAPI()
    app.include_router(build_surveys_router(str(tmp_path)))
    return TestClient(app)


def record(survey_id, started_at="2026-11-18T13:27:12Z", robot_id="rover-jetson", site_id="site-a"):
    return {"survey_id": survey_id, "name": f"Survey {survey_id}", "site_id": site_id,
            "robot_id": robot_id, "started_at": started_at, "stopped_at": None,
            "duration_s": 1355.0}


def take(survey_id, take_id, first_bad_s=None, created_at="2026-11-18T14:00:00Z", has_legs=True):
    return {"take_id": take_id, "survey_id": survey_id,
            "source": "live" if take_id == "original" else "manual",
            "label": take_id, "poses": 12000, "first_bad_s": first_bad_s,
            "take_m": 210.0, "legs_m": 205.0, "has_legs": has_legs, "created_at": created_at}


def test_put_list_get_and_files(tmp_path):
    c = client(tmp_path)
    assert c.put("/surveys/s1", json=record("s1")).status_code == 200
    assert c.put("/surveys/s1/takes/original", json=take("s1", "original", first_bad_s=980.0)).status_code == 200
    assert c.put("/surveys/s1/takes/original/files/paths.json", content=b'{"take":[]}').status_code == 200
    items = c.get("/surveys").json()["items"]
    assert items[0]["take_count"] == 1 and items[0]["first_bad_s"] == 980.0
    assert items[0]["legs_m"] == 205.0
    assert c.get("/surveys/s1").json()["takes"][0]["take_id"] == "original"
    assert c.get("/surveys/s1/takes/original/files/paths.json").content == b'{"take":[]}'


def test_refusals(tmp_path):
    c = client(tmp_path)
    assert c.put("/surveys/s1/takes/original", json=take("s1", "original")).status_code == 404
    c.put("/surveys/s1", json=record("s1"))
    assert c.put("/surveys/s1/takes/manual-a", json=take("s1", "manual-b")).status_code == 400
    for bad in ("other.json", "../survey.json", ".hidden", "take.json"):
        assert c.put(f"/surveys/s1/takes/original/files/{bad}", content=b"x").status_code in (400, 404), bad
    assert c.delete("/surveys/s1/takes/original").status_code == 400


def test_list_is_newest_first_and_filtered(tmp_path):
    c = client(tmp_path)
    c.put("/surveys/a", json=record("a", started_at="2026-11-09T08:00:00Z", robot_id="rover-pi"))
    c.put("/surveys/b", json=record("b", started_at="2026-11-18T13:27:12Z"))
    assert [s["survey_id"] for s in c.get("/surveys").json()["items"]] == ["b", "a"]
    assert [s["survey_id"] for s in c.get("/surveys?robot_id=rover-pi").json()["items"]] == ["a"]


def test_list_filters_by_site(tmp_path):
    c = client(tmp_path)
    c.put("/surveys/a", json=record("a", site_id="site-a"))
    c.put("/surveys/b", json=record("b", site_id="site-b"))
    assert [s["survey_id"] for s in c.get("/surveys?site_id=site-b").json()["items"]] == ["b"]


def test_summary_without_an_original_take(tmp_path):
    c = client(tmp_path)
    c.put("/surveys/s1", json=record("s1"))
    c.put("/surveys/s1/takes/manual-e", json=take("s1", "manual-e", first_bad_s=12.0))
    item = c.get("/surveys").json()["items"][0]
    assert item["take_count"] == 1 and item["first_bad_s"] is None and item["has_legs"] is False
    assert item["legs_m"] is None


def test_takes_sorted_original_first_then_by_created_at(tmp_path):
    c = client(tmp_path)
    c.put("/surveys/s1", json=record("s1"))
    c.put("/surveys/s1/takes/rerun-20261119T050000Z",
          json=take("s1", "rerun-20261119T050000Z", created_at="2026-11-19T05:00:00Z"))
    c.put("/surveys/s1/takes/manual-e", json=take("s1", "manual-e", created_at="2026-11-18T20:00:00Z"))
    c.put("/surveys/s1/takes/original", json=take("s1", "original", created_at="2026-11-19T09:00:00Z"))
    detail = c.get("/surveys/s1").json()
    assert detail["record"]["survey_id"] == "s1"
    assert [t["take_id"] for t in detail["takes"]] == ["original", "manual-e", "rerun-20261119T050000Z"]


def test_unknown_survey_and_missing_file_are_404(tmp_path):
    c = client(tmp_path)
    assert c.get("/surveys/nope").status_code == 404
    c.put("/surveys/s1", json=record("s1"))
    c.put("/surveys/s1/takes/original", json=take("s1", "original"))
    assert c.get("/surveys/s1/takes/original/files/segments.json").status_code == 404
    # A file for a take that has no take.json yet: refused, like a take without a record.
    assert c.put("/surveys/s1/takes/manual-x/files/paths.json", content=b"{}").status_code == 404


def test_body_survey_id_must_match_the_path(tmp_path):
    c = client(tmp_path)
    assert c.put("/surveys/s1", json=record("s2")).status_code == 400
    c.put("/surveys/s1", json=record("s1"))
    assert c.put("/surveys/s1/takes/original", json=take("s2", "original")).status_code == 400


def test_invalid_ids_are_refused(tmp_path):
    c = client(tmp_path)
    c.put("/surveys/s1", json=record("s1"))
    assert c.put("/surveys/s1/takes/not-a-take", json=take("s1", "not-a-take")).status_code in (400, 422)
    assert c.get("/surveys/s1/takes/..%2Fx/files/paths.json").status_code in (400, 404)


def test_delete_a_rerun_take_removes_its_files(tmp_path):
    c = client(tmp_path)
    c.put("/surveys/s1", json=record("s1"))
    tid = "rerun-20261119T050000Z"
    c.put(f"/surveys/s1/takes/{tid}", json=take("s1", tid))
    c.put(f"/surveys/s1/takes/{tid}/files/segments.json", content=b"[]")
    assert c.delete(f"/surveys/s1/takes/{tid}").status_code == 200
    assert c.get("/surveys/s1").json()["takes"] == []
    assert not (tmp_path / "s1" / "takes" / tid).exists()
    assert c.delete(f"/surveys/s1/takes/{tid}").status_code == 404


def test_layout_on_disk(tmp_path):
    c = client(tmp_path)
    c.put("/surveys/s1", json=record("s1"))
    c.put("/surveys/s1/takes/original", json=take("s1", "original"))
    c.put("/surveys/s1/takes/original/files/segments.json", content=b"[]")
    assert (tmp_path / "s1" / "survey.json").is_file()
    assert (tmp_path / "s1" / "takes" / "original" / "take.json").is_file()
    assert (tmp_path / "s1" / "takes" / "original" / "segments.json").read_bytes() == b"[]"


def test_byte_ranges(tmp_path):
    c = client(tmp_path)
    c.put("/surveys/s1", json=record("s1"))
    c.put("/surveys/s1/takes/original", json=take("s1", "original"))
    c.put("/surveys/s1/takes/original/files/cloud.bin", content=b"0123456789")
    r = c.get("/surveys/s1/takes/original/files/cloud.bin", headers={"Range": "bytes=2-5"})
    assert r.status_code == 206 and r.content == b"2345"
    assert r.headers["content-range"] == "bytes 2-5/10"
    assert c.get("/surveys/s1/takes/original/files/cloud.bin",
                 headers={"Range": "bytes=20-"}).status_code == 416


def test_size_cap_413(tmp_path, monkeypatch):
    import surveys.store as store
    monkeypatch.setitem(store.MAX_FILE_BYTES, "segments.json", 4)
    c = client(tmp_path)
    c.put("/surveys/s1", json=record("s1"))
    c.put("/surveys/s1/takes/original", json=take("s1", "original"))
    assert c.put("/surveys/s1/takes/original/files/segments.json", content=b"12345").status_code == 413
    assert c.get("/surveys/s1/takes/original/files/segments.json").status_code == 404


def test_size_caps_are_the_briefs():
    from surveys.store import MAX_FILE_BYTES
    assert MAX_FILE_BYTES == {"paths.json": 32 * 1024 * 1024, "segments.json": 2 * 1024 * 1024,
                              "cloud.bin": 64 * 1024 * 1024}


# --- with sign-in on, through the real app ---------------------------------


def _auth_app(tmp_path, monkeypatch):
    import api.app as backend
    from auth.deps import BridgeBindings
    from auth.tokens import TokenIssuer
    from auth.users import UserStore

    auth = backend.auth
    monkeypatch.setattr(auth, "enabled", True)
    monkeypatch.setattr(auth, "users", UserStore(str(tmp_path / "users.json")))
    monkeypatch.setattr(auth, "issuer", TokenIssuer("test-secret"))
    monkeypatch.setattr(auth, "device_keys", {"rover-a": "device-key-a", "rover-c": "device-key-c"})
    monkeypatch.setattr(auth, "bridges", BridgeBindings())
    monkeypatch.setattr(backend.surveys_router.store, "root", str(tmp_path / "surveys"))
    (tmp_path / "surveys").mkdir()
    backend.registry._robots.clear()
    backend.registry.upsert_from_registration("rover-a", "site-a", "quad")
    backend.registry.upsert_from_registration("rover-c", "site-a", "quad")
    backend.registry.upsert_from_registration("rover-b", "site-b", "quad")
    return backend, auth


def test_devices_write_their_own_surveys_and_people_read_them(tmp_path, monkeypatch):
    backend, auth = _auth_app(tmp_path, monkeypatch)
    try:
        c = TestClient(backend.app)
        dev_a = {"Authorization": "Bearer device-key-a"}
        dev_c = {"Authorization": "Bearer device-key-c"}
        # A device may not write a survey at another site than its robot's.
        assert c.put("/surveys/s1", headers=dev_a,
                     json=record("s1", robot_id="rover-a", site_id="site-b")).status_code == 403
        assert c.put("/surveys/s1", headers=dev_a,
                     json=record("s1", robot_id="rover-a", site_id="site-a")).status_code == 200
        assert c.put("/surveys/s1/takes/original", headers=dev_a, json=take("s1", "original")).status_code == 200
        assert c.put("/surveys/s1/takes/original/files/segments.json", headers=dev_a,
                     content=b"[]").status_code == 200
        # Another robot's key at the same site may not touch rover-a's survey.
        assert c.put("/surveys/s1/takes/original", headers=dev_c, json=take("s1", "original")).status_code == 403
        # Devices do not delete takes.
        assert c.delete("/surveys/s1/takes/original", headers=dev_a).status_code == 403

        auth.users.create(username="vee", display_name="vee", password="correct horse",
                          org_role="member", site_roles={"site-a": "viewer"})
        auth.users.create(username="bee", display_name="bee", password="correct horse",
                          org_role="member", site_roles={"site-b": "viewer"})

        def bearer(name):
            token = c.post("/auth/login", json={"username": name, "password": "correct horse"}).json()["access_token"]
            return {"Authorization": f"Bearer {token}"}

        vee, bee = bearer("vee"), bearer("bee")
        assert [s["survey_id"] for s in c.get("/surveys", headers=vee).json()["items"]] == ["s1"]
        assert c.get("/surveys", headers=bee).json()["items"] == []
        assert c.get("/surveys/s1", headers=vee).status_code == 200
        assert c.get("/surveys/s1", headers=bee).status_code == 403
        assert c.get("/surveys/s1/takes/original/files/segments.json", headers=vee).content == b"[]"
        # A viewer may not write or delete.
        assert c.put("/surveys/s1", headers=vee, json=record("s1", robot_id="rover-a")).status_code == 403
    finally:
        backend.registry._robots.clear()
