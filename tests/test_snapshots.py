"""Snapshot capture storage.

SNAPSHOT became a wired command on 2026-08-13: the edge bridge grabs the newest
JPEG frame it already holds for a camera and POSTs it here, then returns the id
in the command's `result` so the operator's client can fetch the image.

Edge-initiated outbound, exactly like audio clips travelling the other way --
the cloud never reaches inbound to the edge.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from control.snapshots import MAX_SNAPSHOT_BYTES, SNAPSHOT_ID_RE, build_snapshot_router

# The first bytes of a real JPEG. Content type is what the endpoint validates,
# but using a plausible payload keeps the tests honest about what travels.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 64


def _client(tmp_path):
    app = FastAPI()
    app.include_router(build_snapshot_router(snapshots_dir=str(tmp_path)))
    return TestClient(app)


def test_upload_stores_the_frame_and_returns_an_id(tmp_path):
    client = _client(tmp_path)

    response = client.post(
        "/robots/robot-dev-01/snapshots",
        files={"file": ("frame.jpg", JPEG, "image/jpeg")},
    )

    assert response.status_code == 200
    body = response.json()
    assert SNAPSHOT_ID_RE.match(body["snapshot_id"])
    assert body["size_bytes"] == len(JPEG)
    assert (tmp_path / body["snapshot_id"]).read_bytes() == JPEG


def test_snapshot_id_carries_the_robot_id_so_captures_are_attributable(tmp_path):
    client = _client(tmp_path)

    response = client.post(
        "/robots/robot-dev-01/snapshots",
        files={"file": ("frame.jpg", JPEG, "image/jpeg")},
    )

    assert response.json()["snapshot_id"].startswith("robot-dev-01-")


def test_get_serves_the_image_back_as_jpeg(tmp_path):
    client = _client(tmp_path)
    snapshot_id = client.post(
        "/robots/robot-dev-01/snapshots",
        files={"file": ("frame.jpg", JPEG, "image/jpeg")},
    ).json()["snapshot_id"]

    response = client.get(f"/snapshots/{snapshot_id}")

    assert response.status_code == 200
    assert response.content == JPEG
    # image/jpeg rather than octet-stream: a browser has to render this inline
    # in the operator's console, which audio clips never needed.
    assert response.headers["content-type"] == "image/jpeg"


def test_upload_rejects_a_non_image_content_type(tmp_path):
    client = _client(tmp_path)

    response = client.post(
        "/robots/robot-dev-01/snapshots",
        files={"file": ("frame.txt", b"not an image", "text/plain")},
    )

    assert response.status_code == 400
    assert list(tmp_path.iterdir()) == []


def test_upload_rejects_a_robot_id_that_could_escape_the_directory(tmp_path):
    """robot_id is concatenated into a filesystem path, and this backend has no
    auth, so an allowlist is the only thing standing between a caller and an
    arbitrary write location."""
    client = _client(tmp_path)

    response = client.post(
        "/robots/..%2F..%2Fetc/snapshots",
        files={"file": ("frame.jpg", JPEG, "image/jpeg")},
    )

    assert response.status_code in (400, 404)
    assert list(tmp_path.iterdir()) == []


def test_upload_rejects_an_oversized_frame(tmp_path):
    client = _client(tmp_path)

    response = client.post(
        "/robots/robot-dev-01/snapshots",
        files={"file": ("frame.jpg", b"\xff" * (MAX_SNAPSHOT_BYTES + 1), "image/jpeg")},
    )

    assert response.status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_get_rejects_an_id_that_is_not_the_shape_upload_produces(tmp_path):
    client = _client(tmp_path)

    assert client.get("/snapshots/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)
    assert client.get("/snapshots/nonsense").status_code == 400


def test_get_reports_a_missing_snapshot_as_not_found(tmp_path):
    client = _client(tmp_path)

    response = client.get("/snapshots/robot-dev-01-00000000-0000-0000-0000-000000000000.jpg")

    assert response.status_code == 404


# --- Media library: GET /snapshots, sidecars, restart survival -------------

from datetime import datetime, timedelta, timezone

T0 = datetime(2026, 10, 13, 9, 0, 0, tzinfo=timezone.utc)


class _Clock:
    """Advances one minute per upload so newest-first ordering is deterministic."""

    def __init__(self):
        self.now = T0

    def __call__(self):
        current = self.now
        self.now += timedelta(minutes=1)
        return current


def _library(tmp_path, sites=None, clock=None):
    app = FastAPI()
    app.include_router(build_snapshot_router(
        snapshots_dir=str(tmp_path),
        site_lookup=(sites or {}).get,
        clock=clock or _Clock(),
    ))
    return TestClient(app)


def _upload(client, robot_id="robot-dev-01", **form):
    response = client.post(
        f"/robots/{robot_id}/snapshots",
        files={"file": ("frame.jpg", JPEG, "image/jpeg")},
        data=form,
    )
    assert response.status_code == 200, response.text
    return response.json()["snapshot_id"]


def test_list_is_empty_before_any_upload(tmp_path):
    body = _library(tmp_path).get("/snapshots").json()

    assert body == {"items": [], "total": 0, "limit": 50, "offset": 0, "next_offset": None}


def test_list_returns_metadata_newest_first_with_a_fetchable_url(tmp_path):
    client = _library(tmp_path, sites={"robot-dev-01": "site-a"})
    first = _upload(client)
    second = _upload(client, camera="front", captured_at="2026-10-13T08:59:59Z")

    items = client.get("/snapshots").json()["items"]

    assert [i["snapshot_id"] for i in items] == [second, first]
    assert items[0] == {
        "snapshot_id": second,
        "robot_id": "robot-dev-01",
        "site_id": "site-a",
        "camera": "front",
        "captured_at": "2026-10-13T08:59:59Z",
        "received_at": "2026-10-13T09:01:00Z",
        "size_bytes": len(JPEG),
        "position": None,
        "url": f"/snapshots/{second}",
    }
    # Today's bridge sends only the JPEG: optional fields stay null.
    assert items[1]["camera"] is None
    assert items[1]["captured_at"] is None
    assert client.get(items[0]["url"]).content == JPEG


def test_list_filters_by_robot_and_site(tmp_path):
    client = _library(tmp_path, sites={"robot-a": "site-1", "robot-b": "site-2"})
    a = _upload(client, "robot-a")
    b = _upload(client, "robot-b")
    unregistered = _upload(client, "robot-c")

    def ids(**params):
        return [i["snapshot_id"] for i in client.get("/snapshots", params=params).json()["items"]]

    assert ids(robot_id="robot-a") == [a]
    assert ids(site_id="site-2") == [b]
    assert ids(robot_id="robot-a", site_id="site-2") == []
    assert client.get("/snapshots", params={"robot_id": "robot-c"}).json()["items"][0]["site_id"] is None
    assert ids() == [unregistered, b, a]


def test_list_filters_by_inclusive_time_range(tmp_path):
    client = _library(tmp_path)
    ids = [_upload(client) for _ in range(4)]  # received 09:00, 09:01, 09:02, 09:03

    def listed(**params):
        return [i["snapshot_id"] for i in client.get("/snapshots", params=params).json()["items"]]

    assert listed(since="2026-10-13T09:01:00Z") == [ids[3], ids[2], ids[1]]
    assert listed(until="2026-10-13T09:01:00+00:00") == [ids[1], ids[0]]
    assert listed(since="2026-10-13T09:01:00Z", until="2026-10-13T09:02:00Z") == [ids[2], ids[1]]
    # A naive timestamp is read as UTC rather than rejected.
    assert listed(since="2026-10-13T09:03:00") == [ids[3]]
    assert client.get("/snapshots", params={"since": "yesterday"}).status_code == 422


def test_list_pages_with_limit_and_offset(tmp_path):
    client = _library(tmp_path)
    ids = [_upload(client) for _ in range(5)][::-1]  # newest first

    page1 = client.get("/snapshots", params={"limit": 2}).json()
    page2 = client.get("/snapshots", params={"limit": 2, "offset": page1["next_offset"]}).json()
    page3 = client.get("/snapshots", params={"limit": 2, "offset": page2["next_offset"]}).json()

    assert [i["snapshot_id"] for i in page1["items"] + page2["items"] + page3["items"]] == ids
    assert page1["total"] == 5
    assert page1["next_offset"] == 2
    assert page3["next_offset"] is None
    assert client.get("/snapshots", params={"limit": 0}).status_code == 422
    assert client.get("/snapshots", params={"limit": 501}).status_code == 422
    assert client.get("/snapshots", params={"offset": -1}).status_code == 422


def test_list_survives_a_restart_over_the_same_directory(tmp_path):
    before = _library(tmp_path, sites={"robot-dev-01": "site-a"})
    first = _upload(before, camera="back")
    second = _upload(before)
    listed_before = before.get("/snapshots").json()

    # A fresh app knows nothing in memory -- and no site lookup -- so anything it
    # lists came from disk.
    after = _library(tmp_path)

    assert after.get("/snapshots").json() == listed_before
    assert [i["snapshot_id"] for i in listed_before["items"]] == [second, first]
    assert after.get(f"/snapshots/{first}").content == JPEG


def test_a_jpeg_without_a_sidecar_is_still_listed_after_restart(tmp_path):
    """Snapshots stored before the library existed have no sidecar; a corrupt
    sidecar must not hide its image either."""
    legacy = "robot-old-00000000-0000-4000-8000-000000000000.jpg"
    (tmp_path / legacy).write_bytes(JPEG)
    corrupt = "robot-old-11111111-1111-4111-8111-111111111111.jpg"
    (tmp_path / corrupt).write_bytes(JPEG)
    (tmp_path / (corrupt + ".json")).write_text("{not json")
    (tmp_path / "stray.txt").write_text("ignored")

    items = _library(tmp_path).get("/snapshots").json()["items"]

    assert {i["snapshot_id"] for i in items} == {legacy, corrupt}
    for item in items:
        assert item["robot_id"] == "robot-old"
        assert item["size_bytes"] == len(JPEG)
        assert item["received_at"].endswith("Z")
        assert item["site_id"] is None
        assert item["position"] is None


def test_upload_rejects_unsafe_optional_metadata(tmp_path):
    client = _library(tmp_path)

    for form in ({"camera": "../front"}, {"captured_at": "not a time"}):
        response = client.post(
            "/robots/robot-dev-01/snapshots",
            files={"file": ("frame.jpg", JPEG, "image/jpeg")},
            data=form,
        )
        assert response.status_code == 400
    assert list(tmp_path.iterdir()) == []


def test_a_failing_site_lookup_does_not_lose_the_snapshot(tmp_path):
    def broken(_robot_id):
        raise RuntimeError("registry unavailable")

    app = FastAPI()
    app.include_router(build_snapshot_router(snapshots_dir=str(tmp_path), site_lookup=broken))
    client = TestClient(app)

    snapshot_id = _upload(client)

    assert client.get("/snapshots").json()["items"][0]["snapshot_id"] == snapshot_id
