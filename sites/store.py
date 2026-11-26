"""File-backed site store (2026-11-26).

One `<site_id>.json` per site under data/sites/ (SITES_DIR overrides it), each
a `Site` (shared/schemas/sites.py) exactly as validated. No database
(docs/07-tracker.md), so -- like missions (missions/router.py) -- the directory
is the source of truth and is re-read on every call: an organisation has a
handful of sites, and an in-memory index would only be a second copy to keep
coherent.

Writes are atomic (a temp file in the same directory, then os.replace), so a
crash leaves the old record or the new one, never half of either. One lock
covers every check-then-write, because sites are written from two threads: the
request handlers, and paho's network thread, which creates a record the first
time a robot registers at a site nobody has made (`ensure`).

Before this store, a site existed only as an ID that a robot registration, a
map or a user's role mentioned. `backfill` gives each such ID a record at
startup (name = the ID, timezone UTC), so nothing that worked before breaks and
an admin renames it.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from pydantic import ValidationError

from shared.schemas.sites import Site, SiteCreate, SiteUpdate

logger = logging.getLogger("roverhub.sites")

# Persistent by default, under the repo (gitignored via data/), like
# DEFAULT_MISSIONS_DIR. SITES_DIR overrides it.
DEFAULT_SITES_DIR = str(Path(__file__).resolve().parents[1] / "data" / "sites")

SUFFIX = ".json"
# What a record made from an ID alone gets: an admin renames it later.
BACKFILL_TIMEZONE = "UTC"

# SITE_ID_PATTERN (shared/schemas/commands.py) is a topic-level rule. These are
# the file-name rules on top of it, because the ID is a file name here: no "."
# or "..", no trailing "." (Windows drops it, so "a." would be the file "a"),
# and no Windows device name ("con", "nul.json", ...), as in maps/store.py.
_SITE_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_WINDOWS_DEVICES = re.compile(r"(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", re.IGNORECASE)


def storable_site_id(site_id: object) -> bool:
    """Whether `site_id` can name a file in the store."""
    return (isinstance(site_id, str) and bool(_SITE_FILE_ID_RE.fullmatch(site_id))
            and site_id not in (".", "..") and not site_id.endswith(".")
            and not _WINDOWS_DEVICES.fullmatch(site_id))


def site_id_from_name(name: str) -> Optional[str]:
    """A slug for a new site's ID: "PICT Square" -> "pict-square". Accents are
    folded to ASCII; anything else becomes "-". None when nothing is left (a
    name entirely in Hangul, say), and the caller picks an ID instead."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    slug = slug[:64].strip("-")
    return slug if storable_site_id(slug) else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SiteExists(Exception):
    """A create named an ID that is taken (`existing` is the one holding it,
    which differs from the requested ID only in case)."""

    def __init__(self, site_id: str, existing: str):
        super().__init__(site_id)
        self.site_id = site_id
        self.existing = existing


class SiteIdNotStorable(ValueError):
    pass


class SiteStore:
    def __init__(self, sites_dir: str, clock: Callable[[], datetime] = _now):
        self._dir = sites_dir
        self._clock = clock
        self._lock = threading.RLock()
        os.makedirs(sites_dir, exist_ok=True)

    # --- reading -------------------------------------------------------------

    def _path(self, site_id: str) -> Optional[str]:
        if not storable_site_id(site_id):
            return None
        # Containment that does not depend on the regex being perfect.
        root = os.path.realpath(self._dir)
        resolved = os.path.realpath(os.path.join(self._dir, site_id + SUFFIX))
        return resolved if os.path.dirname(resolved) == root else None

    def _load(self, path: str, site_id: str) -> Optional[Site]:
        try:
            with open(path, encoding="utf-8") as handle:
                site = Site.model_validate_json(handle.read())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:  # ValidationError is a ValueError
            logger.warning("skipping unreadable site file %s: %s", path, exc)
            return None
        if site.site_id != site_id:
            # Also what a case-insensitive file system returns for "Site-A"
            # when "site-a" is stored: not this site.
            logger.debug("site file %s holds site_id %r", path, site.site_id)
            return None
        return site

    def _ids(self) -> List[str]:
        try:
            names = os.listdir(self._dir)
        except FileNotFoundError:
            return []
        return [name[: -len(SUFFIX)] for name in names
                if name.endswith(SUFFIX) and storable_site_id(name[: -len(SUFFIX)])]

    def get(self, site_id: str) -> Optional[Site]:
        path = self._path(site_id)
        return self._load(path, site_id) if path is not None else None

    def list(self) -> List[Site]:
        sites = []
        for site_id in self._ids():
            site = self.get(site_id)
            if site is not None:
                sites.append(site)
        sites.sort(key=lambda s: (s.name.casefold(), s.site_id))
        return sites

    # --- writing -------------------------------------------------------------

    def _write(self, site: Site) -> None:
        path = self._path(site.site_id)
        if path is None:
            raise SiteIdNotStorable(site.site_id)
        # ".<random>.tmp" is never listed (it does not end in .json).
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(site.model_dump_json(indent=2))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _taken(self, site_id: str) -> Optional[str]:
        """The stored ID that `site_id` collides with, ignoring case: on
        Windows (the dev machine) "Site-A" and "site-a" are one file, and
        anywhere two sites told apart only by case are a mistake waiting."""
        wanted = site_id.casefold()
        return next((existing for existing in self._ids() if existing.casefold() == wanted), None)

    def create(self, body: SiteCreate, site_id: str) -> Site:
        """Store a new site under `site_id` (the body's, or one the router
        derived). SiteExists if the ID is taken, SiteIdNotStorable if it cannot
        be a file name."""
        if not storable_site_id(site_id):
            raise SiteIdNotStorable(site_id)
        with self._lock:
            existing = self._taken(site_id)
            if existing is not None:
                raise SiteExists(site_id, existing)
            now = self._clock()
            site = Site(site_id=site_id, name=body.name, timezone=body.timezone, address=body.address,
                        location=body.location, created_at=now, updated_at=now)
            self._write(site)
            return site

    def update(self, site_id: str, update: SiteUpdate) -> Optional[Site]:
        """Merge `update` into the stored site: fields it sets (explicit nulls
        included) replace, the rest stay. None if there is no such site. An
        update that sets nothing changes nothing, not even updated_at."""
        with self._lock:
            site = self.get(site_id)
            if site is None:
                return None
            changes = update.changes()
            if not changes:
                return site
            merged = Site.model_validate({**site.model_dump(mode="json"), **changes,
                                          "site_id": site.site_id, "created_at": site.created_at,
                                          "updated_at": self._clock()})
            self._write(merged)
            return merged

    def delete(self, site_id: str) -> bool:
        path = self._path(site_id)
        if path is None:
            return False
        with self._lock:
            if self.get(site_id) is None:
                return False
            try:
                os.unlink(path)
                return True
            except FileNotFoundError:
                return False

    def ensure(self, site_id: str) -> Optional[Site]:
        """A record for `site_id` if there is none: name = the ID, timezone
        UTC. Returns the new record, or None when one existed already or the ID
        cannot be stored (logged). Never raises: it runs on paho's thread when
        a robot registers, and must never cost that registration."""
        try:
            if self.get(site_id) is not None:
                return None
            if not storable_site_id(site_id):
                logger.warning("site %r cannot be stored as a file; it has no site record", site_id)
                return None
            body = SiteCreate(site_id=site_id, name=site_id, timezone=BACKFILL_TIMEZONE)
            with self._lock:
                if self._taken(site_id) is not None:
                    return None
                return self.create(body, site_id)
        except SiteExists:
            return None
        except (ValidationError, SiteIdNotStorable, OSError) as exc:
            logger.warning("could not make a site record for %r: %s", site_id, exc)
            return None
        except Exception:
            logger.exception("could not make a site record for %r", site_id)
            return None

    def backfill(self, site_ids: Iterable[Optional[str]]) -> List[Site]:
        """`ensure` for each ID (None and duplicates skipped); the records it
        made, in the order given."""
        made: List[Site] = []
        seen = set()
        for site_id in site_ids:
            if not site_id or site_id in seen:
                continue
            seen.add(site_id)
            site = self.ensure(site_id)
            if site is not None:
                made.append(site)
        return made


def new_site_id(name: str, given: Optional[str]) -> str:
    """The ID a POST stores a site under: the body's, else a slug of the name,
    else (a name with nothing ASCII in it) "site-" and eight random hex digits."""
    if given:
        return given
    return site_id_from_name(name) or f"site-{uuid.uuid4().hex[:8]}"


__all__ = ["BACKFILL_TIMEZONE", "DEFAULT_SITES_DIR", "SiteExists", "SiteIdNotStorable", "SiteStore",
           "new_site_id", "site_id_from_name", "storable_site_id"]
