"""Storage for camera snapshots captured by the edge bridge.

SNAPSHOT became a wired command on 2026-08-13, once the camera DDS path it had
been waiting on was confirmed working on hardware. The capture itself needs no
new robot capability: `stream_manager` already holds the newest JPEG frame per
camera, encoded by the robot's own camera stack, so a snapshot is that frame.
What it needed was somewhere to put the bytes.

The bridge POSTs the frame here and returns the id in the command's `result`, so
the operator's client can fetch the image. This is the mirror image of audio
clips, which travel cloud -> edge; both directions are edge-initiated, which is
what keeps "the cloud never reaches inbound to the edge" true.

Separate module from `media.py` for the same reason `tts.py` is: that file is
about the audio pipeline, and mixing an image store into it would make neither
easy to read. The two share `ROBOT_ID_RE`, imported rather than re-declared so a
hardening fix to one cannot silently miss the other.

⚠️ Not persisted across a backend restart in any deployment today: `/tmp` by
default, no database. Same limitation as audio clips -- a snapshot is a live
operational artefact here, not an archive. Durable capture history needs the
storage layer that does not exist yet (docs/07-tracker.md).
"""
from __future__ import annotations

import os
import re
import uuid

from fastapi import APIRouter, HTTPException, UploadFile
from starlette.responses import FileResponse

from control.media import ROBOT_ID_RE

# A JPEG frame off the robot's camera measures ~45KB at the resolution the robot
# chooses to send. 4MB is generous enough to survive a firmware that starts
# sending far larger frames, while still bounding what an unauthenticated caller
# can write (this backend has no auth by design at this stage).
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024

# Only JPEG. The robot publishes `CompressedImage` with `format: "jpeg"` (E1) and
# the bridge relays those bytes untouched, so anything else did not come from the
# camera path and has no business being stored as a snapshot. Extension is
# derived from the validated content type, never from the client's filename --
# otherwise a spoofed type paired with "x.php" would put an attacker-chosen
# extension on disk.
CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
}

# Exactly the shape upload produces: "<robot_id>-<uuid4>.jpg". Matched before the
# id is concatenated into a path, because permitting '/', '\' or '..' here would
# be arbitrary file read on a service with no authentication.
SNAPSHOT_ID_RE = re.compile(
    r"^[A-Za-z0-9_-]+-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jpg$"
)

SNAPSHOT_PATH_RE = re.compile(r"^/robots/[^/]+/snapshots$")


def build_snapshot_router(snapshots_dir: str) -> APIRouter:
    router = APIRouter()
    os.makedirs(snapshots_dir, exist_ok=True)

    @router.post("/robots/{robot_id}/snapshots")
    async def upload_snapshot(robot_id: str, file: UploadFile):
        if not ROBOT_ID_RE.match(robot_id):
            raise HTTPException(status_code=400, detail="robot_id contains invalid characters")

        content_type = (file.content_type or "").lower()
        extension = CONTENT_TYPE_EXTENSIONS.get(content_type)
        if extension is None:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported content type '{file.content_type}'; snapshots must be image/jpeg",
            )

        # Read one byte past the limit so an oversized frame is rejected without
        # first buffering an unbounded amount of it.
        contents = await file.read(MAX_SNAPSHOT_BYTES + 1)
        if len(contents) > MAX_SNAPSHOT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"snapshot exceeds maximum size of {MAX_SNAPSHOT_BYTES} bytes",
            )
        if not contents:
            raise HTTPException(status_code=400, detail="snapshot is empty")

        # robot_id is in the id deliberately: a capture is only meaningful
        # attributed to the robot that took it, and the bridge does not have to
        # thread a separate field through the command result to say which.
        snapshot_id = f"{robot_id}-{uuid.uuid4()}{extension}"
        with open(os.path.join(snapshots_dir, snapshot_id), "wb") as handle:
            handle.write(contents)

        return {"snapshot_id": snapshot_id, "size_bytes": len(contents)}

    @router.get("/snapshots/{snapshot_id}")
    def get_snapshot(snapshot_id: str):
        """Serves a stored capture.

        Not scoped under /robots/{robot_id}: the id already carries the robot,
        and an extra path segment is one more place to get validation wrong --
        the same reasoning as GET /audio-clips/{clip_id}.

        Served as image/jpeg rather than octet-stream because a browser renders
        this inline in the operator's console; audio clips are fetched by the
        bridge and never displayed, so they stay a generic download.
        """
        if not SNAPSHOT_ID_RE.match(snapshot_id):
            raise HTTPException(status_code=400, detail="invalid snapshot_id")

        # realpath both sides and confirm containment: the regex should make this
        # unreachable, but a path check that does not depend on the pattern being
        # perfect is the one that still holds after someone edits the pattern.
        root = os.path.realpath(snapshots_dir)
        resolved = os.path.realpath(os.path.join(snapshots_dir, snapshot_id))
        if resolved != root and not resolved.startswith(root + os.sep):
            raise HTTPException(status_code=400, detail="invalid snapshot_id")
        if not os.path.isfile(resolved):
            raise HTTPException(status_code=404, detail="snapshot not found")

        return FileResponse(resolved, media_type="image/jpeg")

    return router
