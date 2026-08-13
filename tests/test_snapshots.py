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
