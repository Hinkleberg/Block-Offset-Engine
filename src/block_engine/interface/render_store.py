"""
render_store.py — Array B: Render-dedicated storage interface.

Receives post-commit, post-quorum block forwards from ResilientStore (Array A)
via an async queue. Exposes a read-only interface to the render feed. The render
feed never touches Array A; all reads come here, with zero write-path contention.

Architecture position:

    ResilientStore (Array A)
        │  post-commit async forward
        ▼
    RenderStore   (Array B)   ◄── render feed reads only from here
        │
        ▼
    Render Feed (delta-only)

Design notes:
  - The flat block image schema on Array B is identical to Array A.
  - The forward intake is a single-row upsert; it never blocks the render read path.
  - mirror_write_seq tracks how far Array B lags behind Array A's write_seq.
  - On integrity failure for a block, falls back to the primary_fallback callable
    (supplied by ResilientStore) so the render feed is never interrupted.
  - Multiple RenderStore instances can be registered on one ResilientStore for
    the "2 arrays" case; each tracks its own lag independently.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS blocks (
    offset      INTEGER PRIMARY KEY,
    data        BLOB    NOT NULL,
    checksum    TEXT    NOT NULL,
    write_seq   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_seq ON blocks(write_seq);
"""


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# ForwardEntry — unit of work in the async forward queue
# ---------------------------------------------------------------------------

@dataclass(order=True)
class ForwardEntry:
    write_seq: int
    offset: int = field(compare=False)
    data: bytes = field(compare=False)


# ---------------------------------------------------------------------------
# RenderStore
# ---------------------------------------------------------------------------

class RenderStore:
    """
    Array B: render-dedicated storage.

    Parameters
    ----------
    db_path:
        Path to the SQLite file that backs Array B. Should be on a separate
        physical device from Array A.
    primary_fallback:
        Callable(offset) -> bytes | None. Called when a block read fails
        integrity checks locally. Typically points at ResilientStore.read_block().
    forward_queue_maxsize:
        Cap on the async forward queue. If the queue fills (Array B is very
        behind), forwards are dropped with a warning rather than back-pressuring
        the mutation engine on Array A.
    integrity_scan_interval:
        Seconds between background integrity scans of Array B.
    """

    def __init__(
        self,
        db_path: str | Path,
        primary_fallback: Optional[Callable[[int], Optional[bytes]]] = None,
        forward_queue_maxsize: int = 4096,
        integrity_scan_interval: float = 60.0,
    ) -> None:
        self._db_path = Path(db_path)
        self._primary_fallback = primary_fallback
        self._integrity_scan_interval = integrity_scan_interval

        self._conn = self._open_db()
        self._lock = threading.Lock()

        # Sequence tracking
        self._mirror_write_seq: int = self._load_max_seq()

        # Async forward queue (filled by Array A's write path, drained by worker)
        self._forward_queue: asyncio.Queue[ForwardEntry] = asyncio.Queue(
            maxsize=forward_queue_maxsize
        )
        self._queue_maxsize = forward_queue_maxsize

        # Background threads
        self._drain_thread = threading.Thread(
            target=self._drain_loop, name="render-store-drain", daemon=True
        )
        self._scan_thread = threading.Thread(
            target=self._scan_loop, name="render-store-scan", daemon=True
        )
        self._stop_event = threading.Event()

        self._drain_thread.start()
        self._scan_thread.start()

        logger.info(
            "RenderStore online — db=%s  mirror_write_seq=%d",
            self._db_path,
            self._mirror_write_seq,
        )

    # ------------------------------------------------------------------
    # Public read interface (render feed only touches these)
    # ------------------------------------------------------------------

    def read_block(self, offset: int) -> Optional[bytes]:
        """
        Read a block by byte offset. Returns None if the block has not yet
        been forwarded from Array A (i.e. lag window) or if it has never
        been written.

        On checksum failure, logs a warning and falls back to the primary
        array (if a fallback is registered) so the render feed keeps running.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT data, checksum FROM blocks WHERE offset = ?", (offset,)
            ).fetchone()

        if row is None:
            return None

        data, stored_checksum = row
        if _checksum(data) != stored_checksum:
            logger.warning(
                "RenderStore integrity failure at offset=%d — falling back to primary",
                offset,
            )
            if self._primary_fallback:
                return self._primary_fallback(offset)
            return None

        return bytes(data)

    def read_range(self, start_offset: int, length: int) -> dict[int, bytes]:
        """
        Read all blocks within [start_offset, start_offset + length).
        Returns a dict of {offset: data} for blocks present in Array B.
        Missing offsets (lag window) are simply absent from the result.
        """
        end_offset = start_offset + length
        with self._lock:
            rows = self._conn.execute(
                "SELECT offset, data, checksum FROM blocks "
                "WHERE offset >= ? AND offset < ?",
                (start_offset, end_offset),
            ).fetchall()

        result: dict[int, bytes] = {}
        for offset, data, stored_checksum in rows:
            if _checksum(data) == stored_checksum:
                result[offset] = bytes(data)
            else:
                logger.warning(
                    "RenderStore integrity failure at offset=%d in range read", offset
                )
                if self._primary_fallback:
                    fb = self._primary_fallback(offset)
                    if fb is not None:
                        result[offset] = fb

        return result

    @property
    def mirror_write_seq(self) -> int:
        """Current highest write_seq applied to Array B."""
        return self._mirror_write_seq

    # ------------------------------------------------------------------
    # Forward intake (called by ResilientStore after journal commit + quorum)
    # ------------------------------------------------------------------

    def enqueue_forward(self, offset: int, data: bytes, write_seq: int) -> bool:
        """
        Non-blocking enqueue of a committed block write from Array A.

        Returns True if enqueued, False if the queue is full (Array B is
        lagging heavily; the block will be skipped and caught on next
        integrity scan or re-forward).
        """
        entry = ForwardEntry(write_seq=write_seq, offset=offset, data=data)
        try:
            self._forward_queue.put_nowait(entry)
            return True
        except asyncio.QueueFull:
            logger.warning(
                "RenderStore forward queue full (maxsize=%d) — "
                "dropping write_seq=%d offset=%d",
                self._queue_maxsize,
                write_seq,
                offset,
            )
            return False

    # Thread-safe synchronous variant for callers that aren't in an async context
    def enqueue_forward_sync(self, offset: int, data: bytes, write_seq: int) -> bool:
        """Synchronous wrapper around enqueue_forward for non-async callers."""
        entry = ForwardEntry(write_seq=write_seq, offset=offset, data=data)
        # asyncio.Queue is not thread-safe from outside the event loop when
        # using put_nowait; use a threading.Queue bridge instead (see _drain_loop).
        return self._thread_enqueue(entry)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._stop_event.set()
        self._drain_thread.join(timeout=5.0)
        self._scan_thread.join(timeout=5.0)
        with self._lock:
            self._conn.close()
        logger.info("RenderStore closed — db=%s", self._db_path)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _open_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_DDL)
        conn.commit()
        return conn

    def _load_max_seq(self) -> int:
        row = self._conn.execute(
            "SELECT MAX(write_seq) FROM blocks"
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    # Thread-safe queue bridge
    # We use a plain threading.Queue internally so the drain thread (non-async)
    # can consume entries without needing an event loop.
    _thread_queue: "threading.Queue[ForwardEntry | None]"

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

    # Override __init__ to set up the thread queue bridge
    def _setup_thread_queue(self, maxsize: int) -> None:
        import queue
        self._tqueue: "queue.Queue[ForwardEntry | None]" = __import__("queue").Queue(
            maxsize=maxsize
        )

    def _thread_enqueue(self, entry: ForwardEntry) -> bool:
        try:
            self._tqueue.put_nowait(entry)
            return True
        except Exception:
            logger.warning(
                "RenderStore thread queue full — dropping write_seq=%d offset=%d",
                entry.write_seq,
                entry.offset,
            )
            return False

    def _drain_loop(self) -> None:
        """Background thread: drain the thread queue and apply blocks to Array B."""
        import queue as _queue
        # Lazy-init the thread queue here so it happens in the right thread context
        self._tqueue: "_queue.Queue[ForwardEntry | None]" = _queue.Queue(
            maxsize=self._queue_maxsize
        )
        while not self._stop_event.is_set():
            try:
                entry: Optional[ForwardEntry] = self._tqueue.get(timeout=0.05)
            except _queue.Empty:
                continue
            if entry is None:
                break
            self._apply_forward(entry)

    def _apply_forward(self, entry: ForwardEntry) -> None:
        checksum = _checksum(entry.data)
        with self._lock:
            self._conn.execute(
                "INSERT INTO blocks (offset, data, checksum, write_seq) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(offset) DO UPDATE SET "
                "data=excluded.data, checksum=excluded.checksum, write_seq=excluded.write_seq "
                "WHERE excluded.write_seq > blocks.write_seq",
                (entry.offset, entry.data, checksum, entry.write_seq),
            )
            self._conn.commit()
            if entry.write_seq > self._mirror_write_seq:
                self._mirror_write_seq = entry.write_seq

    def _scan_loop(self) -> None:
        """Background thread: periodic integrity scan of all blocks in Array B."""
        while not self._stop_event.is_set():
            time.sleep(self._integrity_scan_interval)
            self._run_integrity_scan()

    def _run_integrity_scan(self) -> None:
        corrupt: list[int] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT offset, data, checksum FROM blocks"
            ).fetchall()

        for offset, data, stored_checksum in rows:
            if _checksum(data) != stored_checksum:
                corrupt.append(offset)

        if corrupt:
            logger.error(
                "RenderStore integrity scan found %d corrupt block(s): %s",
                len(corrupt),
                corrupt[:20],  # cap log line length
            )
        else:
            logger.debug(
                "RenderStore integrity scan clean — %d blocks checked", len(rows)
            )
