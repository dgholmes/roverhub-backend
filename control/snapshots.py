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

Media library without a database (2026-10-13): each JPEG is stored with a JSON
sidecar (`<snapshot_id>.json`) holding what was known at upload time, and
`GET /snapshots` lists them newest first. The in-memory index is rebuilt from the
directory when the router is built, so a restart loses nothing as long as the
directory survives -- which is why the default moved from `/tmp` to
`data/snapshots` under this repo (`SNAPSHOTS_DIR` overrides it). This is a
filesystem store, not the storage layer docs/07-tracker.md says is missing: no
retention, no replication, one process.

What the sidecar can and cannot say: today's bridge (robot_bridge/snapshots.py)
uploads the bare JPEG and nothing else. `robot_id` comes from the URL, `site_id`
from the registry if the robot is registered, `received_at` from this server's
clock. `camera` and `captured_at` are accepted as optional form fields so a
future bridge can send them, and are null until one does. `position` is always
null: the upload carries no pose and this backend keeps no pose to look up.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from fastapi import APIRouter, Form, HTTPException, Query, UploadFile
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

logger = logging.getLogger("roverhub.snapshots")

# Persistent by default: under the repo (gitignored), not /tmp, so the library
# survives a restart. SNAPSHOTS_DIR overrides it.
DEFAULT_SNAPSHOTS_DIR = str(Path(__file__).resolve().parents[1] / "data" / "snapshots")

SIDECAR_SUFFIX = ".json"

# "<robot_id>-" + uuid4 (36) + ".jpg" (4): the robot id is everything before the
# last 41 characters, which is how a sidecar-less legacy file is attributed.
_ID_SUFFIX_LEN = 1 + 36 + len(".jpg")

# Optional client-supplied metadata is written to disk and echoed to browsers,
# so it is allowlisted rather than trusted.
CAMERA_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 500


def _utc(value: datetime) -> datetime:
    """Naive datetimes are taken as UTC so filters never compare naive to aware."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_iso(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _metadata(*, snapshot_id, robot_id, site_id, camera, captured_at, received_at, size_bytes) -> dict:
    return {
        "snapshot_id": snapshot_id,
        "robot_id": robot_id,
        "site_id": site_id,
        "camera": camera,
        "captured_at": _iso(captured_at) if captured_at else None,
        "received_at": _iso(received_at),
        "size_bytes": size_bytes,
        # Always null today: the bridge's upload carries no pose, and this
        # backend keeps none to look up. Present so consumers need no change
        # once one of those exists.
        "position": None,
    }


class SnapshotIndex:
    """In-memory view of the snapshot directory, rebuilt from disk on creation.

    The directory is the source of truth; the index only exists so listing does
    not re-read every sidecar on every request.
    """

    def __init__(self, snapshots_dir: str):
        self._dir = snapshots_dir
        self._lock = threading.Lock()
        self._entries: Dict[str, dict] = {}
        self.rebuild()

    def rebuild(self) -> None:
        entries: Dict[str, dict] = {}
        for name in os.listdir(self._dir):
            if not SNAPSHOT_ID_RE.match(name):
                continue
            path = os.path.join(self._dir, name)
            if not os.path.isfile(path):
                continue
            entries[name] = self._load_sidecar(name) or self._synthesize(name, path)
        with self._lock:
            self._entries = entries

    def _load_sidecar(self, snapshot_id: str) -> Optional[dict]:
        sidecar = os.path.join(self._dir, snapshot_id + SIDECAR_SUFFIX)
        if not os.path.isfile(sidecar):
            return None
        try:
            with open(sidecar, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning("ignoring unreadable snapshot sidecar %s: %s", sidecar, exc)
            return None
        if (
            not isinstance(data, dict)
            or data.get("snapshot_id") != snapshot_id
            or _parse_iso(data.get("received_at")) is None
        ):
            logger.warning("ignoring snapshot sidecar %s: does not describe its image", sidecar)
            return None
        return data

    def _synthesize(self, snapshot_id: str, path: str) -> dict:
        """A JPEG with no usable sidecar -- stored before the library existed, or
        a sidecar write that never landed. Attributable from the id alone."""
        stat = os.stat(path)
        return _metadata(
            snapshot_id=snapshot_id,
            robot_id=snapshot_id[:-_ID_SUFFIX_LEN],
            site_id=None,
            camera=None,
            captured_at=None,
            received_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            size_bytes=stat.st_size,
        )

    def add(self, entry: dict) -> None:
        # Write-then-rename so a crash never leaves a half-written sidecar; if
        # it never lands at all, rebuild() synthesizes the entry from the JPEG.
        sidecar = os.path.join(self._dir, entry["snapshot_id"] + SIDECAR_SUFFIX)
        tmp = sidecar + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(entry, handle)
        os.replace(tmp, sidecar)
        with self._lock:
            self._entries[entry["snapshot_id"]] = entry

    def query(self, robot_id=None, site_id=None, since=None, until=None) -> List[dict]:
        with self._lock:
            entries = list(self._entries.values())
        result = []
        for entry in entries:
            if robot_id is not None and entry.get("robot_id") != robot_id:
                continue
            if site_id is not None and entry.get("site_id") != site_id:
                continue
            received = _parse_iso(entry["received_at"])
            if since is not None and received < since:
                continue
            if until is not None and received > until:
                continue
            result.append(entry)
        # Newest first; the id breaks ties so offset paging is stable.
        result.sort(key=lambda e: (_parse_iso(e["received_at"]), e["snapshot_id"]), reverse=True)
        return result


def build_snapshot_router(
    snapshots_dir: str,
    site_lookup: Optional[Callable[[str], Optional[str]]] = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> APIRouter:
    """`site_lookup` maps a robot_id to its site_id (the registry, in the app):
    the upload itself does not say which site it came from. `clock` stamps
    `received_at`; injectable so ordering is testable."""
    router = APIRouter()
    os.makedirs(snapshots_dir, exist_ok=True)
    index = SnapshotIndex(snapshots_dir)

    @router.post("/robots/{robot_id}/snapshots")
    async def upload_snapshot(
        robot_id: str,
        file: UploadFile,
        camera: Optional[str] = Form(None),
        captured_at: Optional[str] = Form(None),
    ):
        if not ROBOT_ID_RE.match(robot_id):
            raise HTTPException(status_code=400, detail="robot_id contains invalid characters")
        if camera is not None and not CAMERA_RE.match(camera):
            raise HTTPException(status_code=400, detail="camera contains invalid characters")
        captured = None
        if captured_at is not None:
            captured = _parse_iso(captured_at)
            if captured is None:
                raise HTTPException(status_code=400, detail="captured_at must be an ISO 8601 timestamp")

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

        site_id = None
        if site_lookup is not None:
            try:
                site_id = site_lookup(robot_id)
            except Exception:  # a lookup failure must not lose a stored frame
                logger.exception("site lookup failed for %s", robot_id)
        index.add(_metadata(
            snapshot_id=snapshot_id, robot_id=robot_id, site_id=site_id,
            camera=camera, captured_at=captured,
            received_at=clock(), size_bytes=len(contents),
        ))

        return {"snapshot_id": snapshot_id, "size_bytes": len(contents)}

    @router.get("/snapshots")
    def list_snapshots(
        robot_id: Optional[str] = None,
        site_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
        offset: int = Query(0, ge=0),
    ):
        """Newest first by `received_at` -- this server's clock, the one
        timestamp every snapshot has. `since`/`until` are inclusive."""
        matches = index.query(
            robot_id=robot_id, site_id=site_id,
            since=_utc(since) if since else None,
            until=_utc(until) if until else None,
        )
        page = matches[offset:offset + limit]
        return {
            "items": [{**entry, "url": f"/snapshots/{entry['snapshot_id']}"} for entry in page],
            "total": len(matches),
            "limit": limit,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < len(matches) else None,
        }

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
