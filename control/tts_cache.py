"""A small on-disk cache for synthesised announcements.

MeloTTS runs at roughly 0.3x realtime on CPU, so a six-second Korean
announcement costs about two seconds to synthesise, and operators repeat the
same handful of sentences all day. Caching makes every repeat free, which is
what keeps a neural engine affordable on the live path at all.

Keyed on the text AND the engine. The same sentence through a different voice is
different audio, so an engine swap must miss rather than serve yesterday's voice
out of the cache.

Deliberately files on disk rather than a table: this backend still has no
database (see the root CLAUDE.md), and the audio-clip store next door already
established the pattern. It is also the right shape for the job -- the cache is
pure derived data, so losing it costs one re-synthesis and nothing else.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from collections.abc import Callable

logger = logging.getLogger("backend.control.tts_cache")

# Bounded so a long-lived backend cannot fill its disk with one-off
# announcements. At roughly 100-300KB per clip, 500 entries is tens of MB.
DEFAULT_MAX_ENTRIES = 500

# How old a leftover `.part` file must be before the cache removes it. Long
# enough that a synthesis in flight in another worker is never disturbed:
# SYNTH_TIMEOUT_S bounds one at 30s, so an hour is far beyond any live write.
STALE_TEMP_AGE_S = 3_600


def cache_key(text: str, engine: str) -> str:
    """A stable filename-safe key for this text through this engine.

    The engine is part of the key, not metadata beside it, so there is no way to
    read an entry without matching on it.
    """
    digest = hashlib.sha256()
    # A separator that cannot occur in either field, so ("ab", "c") and
    # ("a", "bc") cannot collide.
    digest.update(engine.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(text.encode("utf-8"))
    return digest.hexdigest()


class TtsCache:
    """Synthesised WAVs on disk, keyed by `cache_key`, bounded by entry count."""

    def __init__(self, directory: str, max_entries: int = DEFAULT_MAX_ENTRIES):
        self.directory = directory
        self.max_entries = max(0, int(max_entries))

    def _path(self, key: str) -> str:
        return os.path.join(self.directory, f"{key}.wav")

    def get(self, key: str) -> bytes | None:
        """The cached audio, or None for a miss.

        A zero-length file counts as a miss rather than an error. `put` writes
        atomically so a truncated entry should be impossible, but an empty one
        that somehow exists must be re-synthesised instead of poisoning this key
        forever and serving silence to an operator.
        """
        path = self._path(key)
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            return None
        if not data:
            return None
        # Mark it as recently used, so eviction drops the announcements nobody
        # says any more rather than the ones said every morning. Best effort:
        # a read-only cache directory should still serve hits.
        try:
            os.utime(path)
        except OSError:
            pass
        return data

    def put(self, key: str, wav: bytes) -> None:
        """Stores `wav` under `key`, then prunes back to `max_entries`.

        Written to a temporary file and renamed, because os.replace is atomic:
        a reader never sees a half-written entry, and two workers synthesising
        the same announcement at once cannot interleave into a corrupt file.
        """
        os.makedirs(self.directory, exist_ok=True)
        handle, temp_path = tempfile.mkstemp(dir=self.directory, suffix=".part")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(wav)
            os.replace(temp_path, self._path(key))
        except OSError:
            # A cache that cannot be written must not fail the announcement.
            logger.warning("could not write tts cache entry %s", key, exc_info=True)
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            return
        self._prune(protect=key)

    def get_or_create(self, key: str, produce: Callable[[], bytes]) -> bytes:
        """Returns the cached audio, synthesising it via `produce` on a miss.

        A `produce` that raises propagates and caches nothing, so a transient
        engine failure is retried next time rather than remembered.
        """
        hit = self.get(key)
        if hit is not None:
            return hit
        wav = produce()
        self.put(key, wav)
        return wav

    def _prune(self, protect: str | None = None) -> None:
        """Drops the least recently used entries until the cache fits.

        `protect` is never evicted. Without it, the entry just written could be
        the one dropped: filesystem timestamps are coarse (and coarsest on
        Windows), so a burst of writes can share an mtime and sort arbitrarily.
        """
        if self.max_entries <= 0:
            return
        try:
            entries = os.listdir(self.directory)
        except OSError:
            return

        self._sweep_temporaries(entries)

        names = [n for n in entries if n.endswith(".wav")]
        if len(names) <= self.max_entries:
            return

        protected = f"{protect}.wav" if protect else None

        def age(name: str) -> float:
            try:
                return os.path.getmtime(os.path.join(self.directory, name))
            except OSError:
                return 0.0

        oldest_first = sorted((n for n in names if n != protected), key=age)
        for name in oldest_first[: len(names) - self.max_entries]:
            try:
                os.unlink(os.path.join(self.directory, name))
            except OSError:
                pass

    def _sweep_temporaries(self, entries: list[str]) -> None:
        """Removes stale `.part` files left by an interrupted write.

        `put` renames atomically and cleans up after itself, so one of these
        should never exist -- but a process killed mid-write leaves one, and on
        Windows a handle held by an indexer or scanner can defeat the cleanup.
        Nothing else would ever remove them, so the cache sweeps its own.

        Only ones older than STALE_TEMP_AGE_S. A second worker may have a write
        in flight right now, and deleting its temporary file would make its
        rename fail and silently stop it caching.
        """
        cutoff = time.time() - STALE_TEMP_AGE_S
        for name in entries:
            if not name.endswith(".part"):
                continue
            path = os.path.join(self.directory, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.unlink(path)
            except OSError:
                pass
