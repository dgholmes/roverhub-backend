from __future__ import annotations

import os
import re
import uuid

from fastapi import APIRouter, HTTPException, UploadFile
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# MVP cap for a short voice clip; generous but bounds memory/disk use from an
# unauthenticated caller (this backend has no auth by design at this stage).
MAX_AUDIO_CLIP_BYTES = 10 * 1024 * 1024  # 10MB

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

        file_path = os.path.join(clips_dir, f"{robot_id}-{uuid.uuid4()}{extension}")
        with open(file_path, "wb") as f:
            f.write(contents)

        return {"file_path": file_path}

    return router
