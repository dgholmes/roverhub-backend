import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from control.media import ContentLengthLimitMiddleware, MAX_AUDIO_CLIP_BYTES, build_media_router


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


@pytest.mark.asyncio
async def test_content_length_middleware_rejects_oversized_header_without_reading_body():
    """The bounded read() in the handler only limits what's kept after
    Starlette has already spooled the full multipart body to a temp file --
    it does not stop an oversized body from being received off the wire in
    the first place. This test proves ContentLengthLimitMiddleware closes
    that gap: on an oversized Content-Length it must return 413 without
    ever calling receive() (i.e. without reading any of the body) and
    without ever invoking the downstream app/router.
    """
    receive_calls = []

    async def receive():
        receive_calls.append(1)
        return {"type": "http.request", "body": b"", "more_body": False}

    sent_messages = []

    async def send(message):
        sent_messages.append(message)

    downstream_calls = []

    async def downstream_app(scope, receive, send):
        downstream_calls.append(1)
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = ContentLengthLimitMiddleware(downstream_app, max_bytes=10)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/robots/robot-dev-01/audio-clips",
        "headers": [(b"content-length", b"1000000000")],
    }

    await middleware(scope, receive, send)

    assert downstream_calls == []
    assert receive_calls == []
    start_message = next(m for m in sent_messages if m["type"] == "http.response.start")
    assert start_message["status"] == 413


@pytest.mark.asyncio
async def test_content_length_middleware_passes_through_requests_within_limit():
    async def receive():
        return {"type": "http.request", "body": b"ok", "more_body": False}

    sent_messages = []

    async def send(message):
        sent_messages.append(message)

    downstream_calls = []

    async def downstream_app(scope, receive, send):
        downstream_calls.append(1)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = ContentLengthLimitMiddleware(downstream_app, max_bytes=10)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/robots/robot-dev-01/audio-clips",
        "headers": [(b"content-length", b"2")],
    }

    await middleware(scope, receive, send)

    assert downstream_calls == [1]
    start_message = next(m for m in sent_messages if m["type"] == "http.response.start")
    assert start_message["status"] == 200


def test_app_with_middleware_rejects_upload_exceeding_content_length_limit(tmp_path):
    """End-to-end sanity check that the middleware is composable with the
    media router via the same app.add_middleware()/include_router() wiring
    used in api/app.py, and that an over-limit request never reaches the
    handler (no file written).
    """
    app = FastAPI()
    app.include_router(build_media_router(clips_dir=str(tmp_path)))
    app.add_middleware(ContentLengthLimitMiddleware, max_bytes=10)
    client = TestClient(app)

    response = client.post(
        "/robots/robot-dev-01/audio-clips",
        files={"file": ("clip.wav", io.BytesIO(b"this body is well over ten bytes"), "audio/wav")},
    )

    assert response.status_code == 413
    assert list(tmp_path.iterdir()) == []
