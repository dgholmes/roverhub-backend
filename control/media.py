from __future__ import annotations

import os
import re
import uuid

from fastapi import APIRouter, HTTPException, UploadFile

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
