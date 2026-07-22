import io

from fastapi import FastAPI
from fastapi.testclient import TestClient

from control.media import build_media_router


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
