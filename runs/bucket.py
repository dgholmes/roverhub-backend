"""Copy finished runs to an S3 or S3-compatible bucket (run-records spec §7.2).

Off unless RUNS_BUCKET is set. RUNS_BUCKET_ENDPOINT points boto3 at a non-AWS
provider (MinIO, NAVER Cloud, ...). Credentials come only from the standard
AWS environment variables / config; nothing here accepts them from a request.
Copies run synchronously in the request that triggered them; acceptable at
this scale (one site's runs) and revisited with the database work.

on_change holds the store's per-run lock for the whole copy (uploads included),
so the bucket_copied_at stamp cannot race PUT /runs. It never raises: the
caller is a request handler that has already written the data.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Callable, Optional

from .store import EVENTS_FILE, RUN_FILE, RunStore

logger = logging.getLogger("roverhub.runs.bucket")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class BucketCopier:
    def __init__(self, store: RunStore, client, bucket: str, clock: Callable[[], str] = _now_iso):
        self.store, self.client, self.bucket, self.clock = store, client, bucket, clock

    def _upload(self, run_id: str, site_id: str, rel: str) -> None:
        full = os.path.join(self.store.run_dir(run_id), *rel.split("/"))
        if os.path.isfile(full):
            self.client.upload_file(full, self.bucket, f"{site_id}/{run_id}/{rel}")

    def on_change(self, run_id: str, path: Optional[str]) -> None:
        try:
            with self.store.lock(run_id):
                self._copy(run_id, path)
        except Exception as exc:  # noqa: BLE001 - a copy failure must never fail the request
            logger.warning("bucket copy of %s failed, will retry on next change: %s", run_id, exc)

    def _copy(self, run_id: str, path: Optional[str]) -> None:
        run = self.store.get(run_id)
        if run is None or run.result == "running":
            return
        rels = [path] if path and run.bucket_copied_at else \
            [RUN_FILE, EVENTS_FILE] + [f["path"] for f in self.store.files(run_id)]
        for rel in rels:
            self._upload(run_id, run.site_id, rel)
        self.store.put(run.model_copy(update={"bucket_copied_at": self.clock()}))
        self._upload(run_id, run.site_id, RUN_FILE)


def from_env(store: RunStore) -> Optional[BucketCopier]:
    bucket = os.environ.get("RUNS_BUCKET")
    if not bucket:
        return None
    import boto3  # optional dependency, only when a bucket is configured

    client = boto3.client("s3", endpoint_url=os.environ.get("RUNS_BUCKET_ENDPOINT") or None)
    return BucketCopier(store, client, bucket)
