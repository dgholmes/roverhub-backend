"""The one thread that writes history.

MQTT handlers and request threads call submit(), which only appends to an
in-memory queue and never blocks or raises. The writer thread takes batches of
up to `batch_rows` (or whatever arrived within `flush_s`) and writes each in one
transaction.

When it cannot keep up -- the queue is full, or the database is away -- it drops
the least valuable rows first, and counts what it drops:
  * telemetry and perception status are dropped oldest first;
  * incidents and audit rows are kept past the queue's size, up to twice it,
    and only then is the oldest of them dropped.
A batch that fails to write goes back to the front of the queue and is tried
again after a backoff (doubling, capped), so a database restart costs nothing
that fits in the queue. Problems are logged at most once a minute.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from history.records import RECORD_TYPES

logger = logging.getLogger("roverhub.history")

DAY_S = 86_400.0
LOG_EVERY_S = 60.0


class _Throttled:
    def __init__(self, every_s: float = LOG_EVERY_S):
        self._every = every_s
        self._last: Dict[str, float] = {}

    def warn(self, key: str, message: str, *args, exc_info=False) -> None:
        now = time.monotonic()
        if now - self._last.get(key, -1e18) >= self._every:
            self._last[key] = now
            logger.warning(message, *args, exc_info=exc_info)


class HistoryWriter:
    def __init__(self, store, max_queue: int = 20_000, batch_rows: int = 200, flush_s: float = 0.5,
                 retention_days: float = 30.0, clock: Callable[[], float] = time.time,
                 backoff_s: Tuple[float, float] = (0.5, 30.0)):
        self._store = store
        self._max = max(1, max_queue)
        self._batch_rows = batch_rows
        self._flush_s = flush_s
        self._retention_days = retention_days
        self._clock = clock
        self._backoff = backoff_s
        self._delay = 0.0
        self._cond = threading.Condition()
        self._bulk: Deque[Any] = deque()
        self._vital: Deque[Any] = deque()
        self._busy = False
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        self._last_prune: Optional[float] = None
        self._log = _Throttled()
        self.dropped: Dict[str, int] = {}
        self.written = 0
        self.write_errors = 0
        self.connected: Optional[bool] = None
        self.last_write_at: Optional[float] = None
        self.last_error: Optional[str] = None

    # -- the producer side (any thread) ---------------------------------------------

    def _drop(self, kind: str) -> None:
        self.dropped[kind] = self.dropped.get(kind, 0) + 1
        self._log.warn(f"drop:{kind}", "history: queue full, dropping %s rows (%d so far); the database "
                       "is slow or away", kind, self.dropped[kind])

    def _size(self) -> int:
        return len(self._bulk) + len(self._vital)

    def submit(self, record) -> None:
        """Queue one record. Never blocks, never raises."""
        if not isinstance(record, RECORD_TYPES):
            return
        try:
            with self._cond:
                self._admit(record, front=False)
                if self._size() >= self._batch_rows:
                    self._cond.notify_all()
        except Exception:
            pass

    def _admit(self, record, front: bool) -> None:
        """Caller holds the condition."""
        if self._size() >= self._max:
            if self._bulk:
                oldest = self._bulk.popleft()
                self._drop(oldest.kind)
            elif not record.vital:
                self._drop(record.kind)
                return
            elif len(self._vital) >= 2 * self._max:
                oldest = self._vital.popleft()
                self._drop(oldest.kind)
        queue = self._vital if record.vital else self._bulk
        if front:
            queue.appendleft(record)
        else:
            queue.append(record)

    # -- the consumer side (the writer thread, or flush) -----------------------------------

    def _take(self) -> List[Any]:
        """Caller holds the condition. Vital rows first, then bulk."""
        batch = []
        while self._vital and len(batch) < self._batch_rows:
            batch.append(self._vital.popleft())
        while self._bulk and len(batch) < self._batch_rows:
            batch.append(self._bulk.popleft())
        return batch

    def drain_once(self) -> bool:
        """Write one batch. True if it was written (or there was nothing to
        write); False if the database refused it, in which case it is back at
        the front of the queue."""
        with self._cond:
            batch = self._take()
            self._busy = bool(batch)
        if not batch:
            return True
        try:
            self._store.write(batch)
        except Exception as exc:
            self.write_errors += 1
            self.connected = False
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._log.warn("write", "history: could not write %d rows (%s); will retry", len(batch),
                           self.last_error)
            with self._cond:
                for record in reversed(batch):
                    self._admit(record, front=True)
                self._busy = False
                self._cond.notify_all()
            low, high = self._backoff
            self._delay = min(high, max(low, self._delay * 2 or low))
            return False
        with self._cond:
            if self.connected is False:
                logger.warning("history: the database is back; writing again")
            self.connected = True
            self.written += len(batch)
            self.last_write_at = self._clock()
            self._delay = 0.0
            self._busy = False
            self._cond.notify_all()
        return True

    def maybe_prune(self) -> None:
        now = self._clock()
        if self._last_prune is not None and now - self._last_prune < DAY_S:
            return
        self._last_prune = now
        try:
            pruned = self._store.prune(now, self._retention_days)
            if any(pruned.values()):
                logger.warning("history: pruned rows older than %g days: %s", self._retention_days, pruned)
        except Exception:
            self._last_prune = now - DAY_S + 300.0  # try again in five minutes
            self._log.warn("prune", "history: retention pruning failed; will retry", exc_info=True)

    def _run(self) -> None:
        self.maybe_prune()
        while True:
            with self._cond:
                if not self._stopping and self._size() < self._batch_rows:
                    self._cond.wait(self._flush_s)
                if self._stopping and self._size() == 0:
                    return
            ok = self.drain_once()
            if not ok:
                with self._cond:
                    if self._stopping:
                        return
                    self._cond.wait(self._delay)
            self.maybe_prune()

    # -- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="history-writer", daemon=True)
        self._thread.start()

    def flush(self, timeout_s: float = 5.0) -> bool:
        """Until everything queued is written. With the thread running it waits
        for it; without one it writes from the calling thread. False on timeout."""
        deadline = time.monotonic() + timeout_s
        if self._thread is None or not self._thread.is_alive():
            while time.monotonic() < deadline:
                with self._cond:
                    if self._size() == 0:
                        return True
                if not self.drain_once():
                    time.sleep(min(self._delay, max(0.0, deadline - time.monotonic())))
            return False
        with self._cond:
            self._cond.notify_all()
            while self._size() or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.05))
                self._cond.notify_all()
        return True

    def stop(self, timeout_s: float = 2.0) -> None:
        """Flush what is queued, within `timeout_s`, then end the thread."""
        thread = self._thread
        if thread is None:
            return
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        thread.join(timeout_s)
        if thread.is_alive():
            logger.warning("history: writer did not finish within %g s; %d rows not written",
                           timeout_s, self._size())
        self._thread = None

    def stats(self) -> Dict[str, Any]:
        with self._cond:
            depth = self._size()
        return {"queue_depth": depth, "dropped": dict(self.dropped), "written": self.written,
                "write_errors": self.write_errors, "connected": self.connected,
                "last_write_at": self.last_write_at, "last_error": self.last_error}
