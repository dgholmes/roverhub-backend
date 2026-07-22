from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, HTTPException, UploadFile


def build_media_router(clips_dir: str) -> APIRouter:
    router = APIRouter()
    os.makedirs(clips_dir, exist_ok=True)

    @router.post("/robots/{robot_id}/audio-clips")
    async def upload_audio_clip(robot_id: str, file: UploadFile):
        if not (file.content_type or "").startswith("audio/"):
            raise HTTPException(status_code=400, detail="file must be an audio/* content type")

        extension = os.path.splitext(file.filename or "")[1] or ".wav"
        file_path = os.path.join(clips_dir, f"{robot_id}-{uuid.uuid4()}{extension}")
        contents = await file.read()
        with open(file_path, "wb") as f:
            f.write(contents)

        return {"file_path": file_path}

    return router
