"""File-backed survey replay store (survey replay spec §5.5).

One directory per survey under SURVEYS_DIR, laid out as the spec's §5.5:

    <survey_id>/survey.json                       the survey record
    <survey_id>/takes/<take_id>/take.json         take metadata and status
    <survey_id>/takes/<take_id>/paths.json        the take's path and the legs
    <survey_id>/takes/<take_id>/segments.json     per 20 s segment
    <survey_id>/takes/<take_id>/cloud.bin         the take's points (step 2)

No database: the directory is re-read on every request, as runs/store.py does,
whose atomic and streamed writes are copied here rather than imported (the two
stores share no layout, and a change to one must not move the other).
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import AsyncIterator, List, Optional

from fastapi import HTTPException

from shared.schemas.survey_replay import (
    SURVEY_ID_RE, TAKE_ID_RE, ReplayTake, SurveyRecord, SurveySummary,
)

logger = logging.getLogger("roverhub.surveys")

DEFAULT_SURVEYS_DIR = str(Path(__file__).resolve().parents[1] / "data" / "surveys")
SURVEY_FILE = "survey.json"
TAKES_DIR = "takes"
TAKE_FILE = "take.json"
ORIGINAL_TAKE = "original"
# The files a take may hold besides take.json, and the most each may be. Only
# these names: never take.json (that is PUT .../takes/{take_id}), never a
# nested path, never anything else.
MAX_FILE_BYTES = {
    "paths.json": 32 * 1024 * 1024,
    "segments.json": 2 * 1024 * 1024,
    "cloud.bin": 64 * 1024 * 1024,  # step 2
}
CONTENT_TYPES = {".json": "application/json", ".bin": "application/octet-stream"}


def _atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _take_order(take: ReplayTake):
    # The original first, then the rest by when they were made.
    return (take.take_id != ORIGINAL_TAKE, take.created_at, take.take_id)


class SurveyStore:
    def __init__(self, surveys_dir: str):
        self.root = surveys_dir
        os.makedirs(surveys_dir, exist_ok=True)
        self._locks: dict = {}
        self._locks_guard = threading.Lock()

    def lock(self, survey_id: str) -> threading.RLock:
        """Per-survey lock: endpoints run on a threadpool, and a take written
        while its survey is read must not be seen half-made."""
        with self._locks_guard:
            return self._locks.setdefault(survey_id, threading.RLock())

    # --- paths ---------------------------------------------------------------

    def survey_dir(self, survey_id: str) -> str:
        if not SURVEY_ID_RE.match(survey_id):
            raise HTTPException(status_code=400, detail="invalid survey_id")
        root = os.path.realpath(self.root)
        resolved = os.path.realpath(os.path.join(self.root, survey_id))
        if os.path.dirname(resolved) != root:
            raise HTTPException(status_code=400, detail="invalid survey_id")
        return resolved

    def take_dir(self, survey_id: str, take_id: str) -> str:
        if not TAKE_ID_RE.match(take_id):
            raise HTTPException(status_code=400, detail="invalid take_id")
        takes = os.path.join(self.survey_dir(survey_id), TAKES_DIR)
        resolved = os.path.realpath(os.path.join(takes, take_id))
        if os.path.dirname(resolved) != os.path.realpath(takes):
            raise HTTPException(status_code=400, detail="invalid take_id")
        return resolved

    def file_path(self, survey_id: str, take_id: str, name: str) -> str:
        """Only the allowed file names, directly in the take's folder."""
        if name not in MAX_FILE_BYTES:
            raise HTTPException(status_code=400, detail="invalid file name")
        folder = self.take_dir(survey_id, take_id)
        resolved = os.path.realpath(os.path.join(folder, name))
        if os.path.dirname(resolved) != folder:
            raise HTTPException(status_code=400, detail="invalid file name")
        return resolved

    # --- records -------------------------------------------------------------

    def get(self, survey_id: str) -> Optional[SurveyRecord]:
        path = os.path.join(self.survey_dir(survey_id), SURVEY_FILE)
        try:
            with open(path, encoding="utf-8") as handle:
                return SurveyRecord.model_validate_json(handle.read())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("skipping unreadable survey %s: %s", survey_id, exc)
            return None

    def put(self, record: SurveyRecord) -> None:
        directory = self.survey_dir(record.survey_id)
        with self.lock(record.survey_id):
            os.makedirs(directory, exist_ok=True)
            _atomic_write(os.path.join(directory, SURVEY_FILE), record.model_dump_json(indent=2))

    def list(self, site_id: Optional[str] = None, robot_id: Optional[str] = None) -> List[SurveySummary]:
        out = []
        for name in os.listdir(self.root):
            if not SURVEY_ID_RE.match(name) or not os.path.isdir(os.path.join(self.root, name)):
                continue
            record = self.get(name)
            if record is None or record.survey_id != name:
                continue
            if site_id and record.site_id != site_id:
                continue
            if robot_id and record.robot_id != robot_id:
                continue
            takes = self.takes(name)
            original = next((t for t in takes if t.take_id == ORIGINAL_TAKE), None)
            out.append(SurveySummary(
                **record.model_dump(),
                take_count=len(takes),
                first_bad_s=original.first_bad_s if original is not None else None,
                has_legs=original.has_legs if original is not None else False,
                legs_m=original.legs_m if original is not None else None,
            ))
        out.sort(key=lambda s: (s.started_at, s.survey_id), reverse=True)
        return out

    # --- takes ---------------------------------------------------------------

    def get_take(self, survey_id: str, take_id: str) -> Optional[ReplayTake]:
        path = os.path.join(self.take_dir(survey_id, take_id), TAKE_FILE)
        try:
            with open(path, encoding="utf-8") as handle:
                return ReplayTake.model_validate_json(handle.read())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("skipping unreadable take %s/%s: %s", survey_id, take_id, exc)
            return None

    def takes(self, survey_id: str) -> List[ReplayTake]:
        folder = os.path.join(self.survey_dir(survey_id), TAKES_DIR)
        if not os.path.isdir(folder):
            return []
        out = []
        for name in os.listdir(folder):
            if not TAKE_ID_RE.match(name) or not os.path.isdir(os.path.join(folder, name)):
                continue
            take = self.get_take(survey_id, name)
            if take is None or take.take_id != name or take.survey_id != survey_id:
                continue
            out.append(take)
        out.sort(key=_take_order)
        return out

    def put_take(self, take: ReplayTake) -> None:
        directory = self.take_dir(take.survey_id, take.take_id)
        with self.lock(take.survey_id):
            os.makedirs(directory, exist_ok=True)
            _atomic_write(os.path.join(directory, TAKE_FILE), take.model_dump_json(indent=2))

    def delete_take(self, survey_id: str, take_id: str) -> bool:
        """Remove a take and its files. False when there was none. The
        original is never deleted: it is the survey as recorded."""
        if take_id == ORIGINAL_TAKE:
            raise HTTPException(status_code=400, detail="the original take cannot be deleted")
        directory = self.take_dir(survey_id, take_id)
        with self.lock(survey_id):
            if not os.path.isdir(directory):
                return False
            shutil.rmtree(directory)
        return True

    async def write_file(self, survey_id: str, take_id: str, name: str,
                         chunks: AsyncIterator[bytes], declared: Optional[int]) -> int:
        path = self.file_path(survey_id, take_id, name)
        limit = MAX_FILE_BYTES[name]
        if declared is not None and declared > limit:
            raise HTTPException(status_code=413, detail=f"file exceeds {limit} bytes")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".", suffix=".tmp")
        size = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(status_code=413, detail=f"file exceeds {limit} bytes")
                    handle.write(chunk)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return size
