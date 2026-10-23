"""Helpers the file-backed routers share (missions/router.py, tasks/router.py).

Extracted so the one block that stops an unauthenticated caller writing an
unbounded file exists once: a fix to it must not have to be remembered in every
router that copied it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, Request


def now_iso() -> str:
    """The default `clock` of those routers: UTC now, ISO 8601, 'Z'-suffixed."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def read_bounded_body(request: Request, limit: int, noun: str) -> bytes:
    """The request body, or 413 once it exceeds `limit` bytes. Read by hand, and
    before pydantic parses anything, so the cap applies to what is *sent*, not to
    what validates. `noun` ("mission", "task") names the thing in the error.

    Two layers, because each catches what the other cannot: a declared
    Content-Length is refused before a byte is read, and the running count stops
    a chunked body, which declares no length at all."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"{noun} exceeds {limit} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status_code=413, detail=f"{noun} exceeds {limit} bytes")
    return bytes(body)
