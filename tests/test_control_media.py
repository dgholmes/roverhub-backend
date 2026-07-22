import io

from fastapi import FastAPI
from fastapi.testclient import TestClient

from control.media import MAX_AUDIO_CLIP_BYTES, build_media_router


def test_upload_audio_clip_saves_file_and_returns_path(tmp_path):
    app = FastAPI()
    app.include_router(build_media_router(clips_dir=str(tmp_path)))
    client = TestClient(app)

    response = client.post(
        "/robots/robot-dev-01/audio-clips",
        files={"file": ("clip.wav", io.BytesIO(b"fake-wav-bytes"), "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["file_path"].endswith(".wav")
    with open(body["file_path"], "rb") as f:
        assert f.read() == b"fake-wav-bytes"


def test_upload_rejects_non_audio_content_type(tmp_path):
    app = FastAPI()
    app.include_router(build_media_router(clips_dir=str(tmp_path)))
    client = TestClient(app)

    response = client.post(
        "/robots/robot-dev-01/audio-clips",
        files={"file": ("clip.txt", io.BytesIO(b"not audio"), "text/plain")},
    )

    assert response.status_code == 400


def test_upload_rejects_oversized_clip(tmp_path):
    app = FastAPI()
    app.include_router(build_media_router(clips_dir=str(tmp_path)))
    client = TestClient(app)

    oversized = b"a" * (MAX_AUDIO_CLIP_BYTES + 1)
    response = client.post(
        "/robots/robot-dev-01/audio-clips",
        files={"file": ("clip.wav", io.BytesIO(oversized), "audio/wav")},
    )

    assert response.status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_upload_normalizes_extension_from_content_type_not_filename(tmp_path):
    app = FastAPI()
    app.include_router(build_media_router(clips_dir=str(tmp_path)))
    client = TestClient(app)

    response = client.post(
        "/robots/robot-dev-01/audio-clips",
        files={"file": ("clip.php", io.BytesIO(b"fake-wav-bytes"), "audio/wav")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["file_path"].endswith(".wav")
    assert not body["file_path"].endswith(".php")


def test_upload_rejects_unmapped_content_type(tmp_path):
    app = FastAPI()
    app.include_router(build_media_router(clips_dir=str(tmp_path)))
    client = TestClient(app)

    response = client.post(
        "/robots/robot-dev-01/audio-clips",
        files={"file": ("clip.bin", io.BytesIO(b"not a known audio type"), "audio/x-made-up")},
    )

    assert response.status_code == 400


def test_upload_rejects_invalid_robot_id(tmp_path):
    app = FastAPI()
    app.include_router(build_media_router(clips_dir=str(tmp_path)))
    client = TestClient(app)

    response = client.post(
        "/robots/..%5C..%5Ctemp%5Cevil/audio-clips",
        files={"file": ("clip.wav", io.BytesIO(b"fake-wav-bytes"), "audio/wav")},
    )

    assert response.status_code == 400
    assert list(tmp_path.iterdir()) == []
