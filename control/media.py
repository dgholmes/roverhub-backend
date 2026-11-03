from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel, Field

from control.tts import MAX_TTS_CHARS, TtsError, TtsUnavailable, available_engine, synthesize_wav
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

# MVP cap for a short voice clip; generous but bounds memory/disk use from an
# unauthenticated caller (this backend has no auth by design at this stage).
MAX_AUDIO_CLIP_BYTES = 10 * 1024 * 1024  # 10MB

# Persistent by default, like DEFAULT_SNAPSHOTS_DIR: mission announcements
# reference clips for weeks, and /tmp is wiped on reboot. AUDIO_CLIPS_DIR
# overrides it.
DEFAULT_AUDIO_CLIPS_DIR = str(Path(__file__).resolve().parents[1] / "data" / "audio-clips")

# robot_id is concatenated straight into a filesystem path -- allowlist it so
# path-traversal-style separators (including backslashes, which FastAPI's
# default path converter does not block) can never reach os.path.join.
ROBOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Extension is derived from the validated content-type, never from the
# client-supplied filename -- otherwise a spoofed Content-Type: audio/wav
# paired with filename "x.php" would write an arbitrary-extension file to
# disk under the caller's control. Formats match the edge voice pipeline
# (see e7_voice_pub.py: .wav / .flac), plus common audio/mpeg and audio/ogg.
CONTENT_TYPE_EXTENSIONS = {
    # Raw PCM at the robot's required 24kHz/16-bit/mono, decoded in the browser.
    # This is the format SPEAK actually uses: the robot's DDS voice topic takes
    # PCM in streaming mode, and neither edge device has ffmpeg to decode
    # anything else (verified 2026-08-12) -- so decoding happens where a decoder
    # already exists, in the browser. See roverhub-frontend/src/lib/audioPcm.ts.
    "audio/pcm": ".pcm",
    "audio/l16": ".pcm",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/vnd.wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
}

# Matches the path built by build_media_router below -- kept as its own
# constant so ContentLengthLimitMiddleware can target exactly this endpoint
# without hardcoding the route string in two places.
AUDIO_CLIP_PATH_RE = re.compile(r"^/robots/[^/]+/audio-clips$")

# Exactly the shape upload_audio_clip generates: "{robot_id}-{uuid4}{ext}".
# Anchored and character-restricted so no separator or ".." can reach
# os.path.join when a clip is fetched back.
CLIP_ID_RE = re.compile(r"^[A-Za-z0-9_-]+-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.[A-Za-z0-9]{1,5}$")


class ContentLengthLimitMiddleware:
    """Raw ASGI middleware that rejects oversized audio-clip uploads by
    declared ``Content-Length`` before Starlette's ``MultiPartParser`` ever
    runs.

    Why this exists: FastAPI resolves the ``file: UploadFile`` parameter by
    running Starlette's multipart parser *before* the endpoint function body
    executes, and that parser has no size cap of its own -- it fully
    receives and spools the request body (to a temp file) regardless of
    size. The bounded ``file.read(MAX_AUDIO_CLIP_BYTES + 1)`` check inside
    ``upload_audio_clip`` only stops an oversized file from being
    permanently written to ``clips_dir``; by itself it does not stop the
    server from receiving/buffering an oversized body over the network in
    the first place. This middleware runs ahead of routing and body
    parsing entirely -- on a ``Content-Length`` that already exceeds the
    limit, it returns 413 immediately and never calls ``receive()``, so no
    request body is read off the wire at all.

    Deliberately implemented as a plain ASGI callable rather than with
    Starlette's ``BaseHTTPMiddleware`` (which itself buffers the whole
    request body into memory to build a ``Request`` object -- exactly the
    behavior we're trying to avoid here).

    Note this only catches a truthful, oversized ``Content-Length`` header
    sent up front; a chunked-transfer request with no ``Content-Length`` (or
    a lying one) still relies on the post-parse handler check. Closing that
    fully would require streaming/incremental multipart parsing with a hard
    cutoff, which is out of scope for this single-dev-machine MVP media
    relay -- see the fix report for this task.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") == "POST":
            if AUDIO_CLIP_PATH_RE.match(scope.get("path", "")):
                content_length = self._content_length(scope)
                if content_length is not None and content_length > self.max_bytes:
                    response = JSONResponse(
                        status_code=413,
                        content={
                            "detail": (
                                f"audio clip exceeds maximum size of {self.max_bytes} bytes"
                            )
                        },
                    )
                    await response(scope, receive, send)
                    return

        await self.app(scope, receive, send)

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        for key, value in scope.get("headers") or []:
            if key == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return None
        return None


class TtsRequest(BaseModel):
    # Bounded at the model so an oversized body is rejected before synthesis is
    # attempted; tts.py enforces the same limit for non-HTTP callers.
    text: str = Field(min_length=1, max_length=MAX_TTS_CHARS)


def build_media_router(clips_dir: str) -> APIRouter:
    router = APIRouter()
    os.makedirs(clips_dir, exist_ok=True)

    @router.post("/robots/{robot_id}/audio-clips")
    async def upload_audio_clip(robot_id: str, file: UploadFile):
        if not ROBOT_ID_RE.match(robot_id):
            raise HTTPException(status_code=400, detail="robot_id contains invalid characters")

        content_type = (file.content_type or "").lower()
        extension = CONTENT_TYPE_EXTENSIONS.get(content_type)
        if extension is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unsupported content type '{file.content_type}'; must be one of "
                    f"{sorted(set(CONTENT_TYPE_EXTENSIONS.values()))}"
                ),
            )

        # Bounded read: ask for one byte more than the limit so an oversized
        # upload can be rejected without buffering an unbounded amount first.
        # This is a defense-in-depth backstop, not the primary size guard --
        # ContentLengthLimitMiddleware (see above) is what stops an oversized
        # body from being received/spooled by Starlette's multipart parser
        # in the first place, when the client sends a truthful Content-Length.
        contents = await file.read(MAX_AUDIO_CLIP_BYTES + 1)
        if len(contents) > MAX_AUDIO_CLIP_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"audio clip exceeds maximum size of {MAX_AUDIO_CLIP_BYTES} bytes",
            )

        clip_id = f"{robot_id}-{uuid.uuid4()}{extension}"
        file_path = os.path.join(clips_dir, clip_id)
        with open(file_path, "wb") as f:
            f.write(contents)

        # `clip_id` is what SPEAK carries, not `file_path`. A filesystem path is
        # meaningless to the two machines that come after this one: the edge
        # bridge has no access to the cloud's disk, and the robot resolves
        # `VoiceCmd(type="file")` paths against its OWN disk. Sending a path was
        # why uploaded clips silently never played -- the bytes never left this
        # host. The bridge now GETs the clip below and streams the PCM instead.
        #
        # `file_path` stays in the response for the mock provider's shape and for
        # debugging, but nothing downstream should route on it.
        return {"clip_id": clip_id, "file_path": file_path, "size_bytes": len(contents)}

    @router.get("/tts/capability")
    def tts_capability():
        """Lets the UI hide or explain the Say tab instead of offering a button
        that can only fail on a host with no engine."""
        engine = available_engine()
        return {"available": engine is not None, "engine": engine, "max_chars": MAX_TTS_CHARS}

    @router.post("/tts")
    async def synthesize_speech(request: TtsRequest):
        """Text in, WAV out.

        Deliberately does not store anything or create a clip. The browser decodes
        this WAV to the robot's PCM through exactly the same path an uploaded file
        takes and then posts it to the audio-clips endpoint, so a spoken
        announcement and an uploaded file are the same thing downstream -- one clip
        store, one edge cache, one volume control, one streaming path.
        """
        try:
            wav = synthesize_wav(request.text)
        except TtsUnavailable as exc:
            # 501: the request is fine, this deployment cannot serve it.
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except TtsError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return Response(content=wav, media_type="audio/wav")

    @router.get("/audio-clips/{clip_id}")
    def get_audio_clip(clip_id: str):
        """Serves a stored clip so the edge bridge can fetch its bytes.

        Deliberately not scoped under /robots/{robot_id}: the bridge knows the
        clip_id from the command and nothing is gained by making it re-derive a
        robot id, while an extra path segment is another place to get validation
        wrong.

        clip_id is validated against exactly the shape upload produces. It is
        concatenated into a filesystem path, so anything permitting '/', '\' or
        '..' would be arbitrary file read on a backend that has no auth at all.
        """
        if not CLIP_ID_RE.match(clip_id):
            raise HTTPException(status_code=400, detail="invalid clip_id")

        file_path = os.path.join(clips_dir, clip_id)
        # realpath both sides: even with the regex, resolve and confirm the result
        # is inside clips_dir rather than trusting the pattern alone.
        clips_root = os.path.realpath(clips_dir)
        resolved = os.path.realpath(file_path)
        if resolved != clips_root and not resolved.startswith(clips_root + os.sep):
            raise HTTPException(status_code=400, detail="invalid clip_id")
        if not os.path.isfile(resolved):
            raise HTTPException(status_code=404, detail="clip not found")

        return FileResponse(resolved, media_type="application/octet-stream")

    return router
