"""
sparse_block_store.py — Block storage with SHA256 integrity, capacity bounds,
                        and non-blocking paginated integrity scan.

Part of the Block-Image Game Engine.

Changes vs v1:
  - World geometry enforced: max_blocks and block_size cap the address space so
    the store models the fixed flat block image described in the README.
  - CapacityError raised on writes that would exceed max_blocks.
  - verify_integrity() is now a generator that yields results one page at a time;
    it never holds a full-table lock and can be driven from a background thread.
  - A dedicated read-only connection is used for integrity scans so live
    reads/writes are not stalled.
  - write_seq (monotonic write-sequence counter) added to every row so callers
    can detect stale reads after a crash.
  - LRU block eviction: when the store is at capacity, the least-recently-read
    block is evicted to make room (configurable via evict_on_full).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
import zlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BlockStoreError(Exception):
    """Base error for all block-store failures."""


class BlockNotFoundError(BlockStoreError):
    """Raised when a requested block offset does not exist in the store."""


class ChecksumMismatchError(BlockStoreError):
    """Raised when stored checksum does not match recomputed checksum on read.

    Attributes:
        offset:   Block offset that triggered the mismatch.
        expected: Checksum recorded at write time.
        actual:   Checksum computed from the bytes read back.
    """

    def __init__(self, offset: int, expected: str, actual: str) -> None:
        self.offset = offset
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Checksum mismatch at offset {offset}: "
            f"expected={expected!r} actual={actual!r}"
        )


class CapacityError(BlockStoreError):
    """Raised when a write would exceed the store's configured block capacity."""

    def __init__(self, offset: int, max_blocks: int) -> None:
        self.offset = offset
        self.max_blocks = max_blocks
        super().__init__(
            f"Offset {offset} exceeds world capacity of {max_blocks} blocks"
        )


# ---------------------------------------------------------------------------
# Metadata container
# ---------------------------------------------------------------------------

@dataclass
class BlockMetadata:
    """Metadata returned by :meth:`SparseBlockStore.get_block_metadata`."""

    offset: int
    size: int                # raw (uncompressed) byte length
    checksum: Optional[str]  # SHA256 hex digest of compressed payload; None = legacy row
    compressed: bool         # whether the payload is zlib-compressed
    timestamp: float         # unix epoch of last write
    write_seq: int           # monotonic write sequence number


# ---------------------------------------------------------------------------
# Integrity scan result (yielded by verify_integrity generator)
# ---------------------------------------------------------------------------

@dataclass
class IntegrityResult:
    """Result for a single block emitted by :meth:`SparseBlockStore.verify_integrity`."""

    offset: int
    status: str   # "ok" | "corrupted" | "unverified"
    checksum: Optional[str] = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
    offset      INTEGER PRIMARY KEY,
    data        BLOB    NOT NULL,
    size        INTEGER NOT NULL,
    checksum    TEXT,
    compressed  INTEGER NOT NULL DEFAULT 1,
    timestamp   REAL    NOT NULL,
    write_seq   INTEGER NOT NULL DEFAULT 0,
    last_read   REAL
);

CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_COMPRESSION_LEVEL  = 6    # zlib level 1-9
_COMPRESS_THRESHOLD = 64   # bytes — skip compression for tiny blocks
_SCAN_PAGE_SIZE     = 256  # rows per page during integrity scan


class SparseBlockStore:
    """SQLite-backed sparse block store with SHA256 integrity verification.

    Args:
        db_path:       Path to the SQLite file.  Created if it does not exist.
        compress:      Enable zlib compression of block payloads (default True).
        max_blocks:    Maximum number of blocks the store will hold.  Corresponds
                       to the flat image's total block count.  None = unbounded
                       (legacy behaviour, not recommended).
        block_size:    Expected byte size of each block.  Used only for capacity
                       documentation; not enforced per-write.
        evict_on_full: If True, evict the LRU block when at capacity instead of
                       raising CapacityError.  Default False.
    """

    def __init__(
        self,
        db_path: str | Path,
        compress: bool = True,
        max_blocks: Optional[int] = None,
        block_size: int = 4096,
        evict_on_full: bool = False,
    ) -> None:
        self._path = Path(db_path)
        self._compress = compress
        self._max_blocks = max_blocks
        self._block_size = block_size
        self._evict_on_full = evict_on_full
        self._write_seq = 0

        self._conn = self._open_write_conn()
        self._read_conn = self._open_read_conn()

        # LRU tracker: offset -> last_read timestamp (OrderedDict keeps insertion order)
        self._lru: OrderedDict[int, float] = OrderedDict()
        self._load_lru_from_db()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _open_write_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.executescript(_SCHEMA)
        conn.commit()
        # Restore write_seq from persisted max
        row = conn.execute("SELECT MAX(write_seq) FROM blocks").fetchone()
        if row and row[0] is not None:
            self._write_seq = row[0]
        return conn

    def _open_read_conn(self) -> sqlite3.Connection:
        """Separate read-only connection used by integrity scans."""
        conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True,
                               check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _load_lru_from_db(self) -> None:
        """Populate the in-memory LRU map from persisted last_read timestamps."""
        rows = self._conn.execute(
            "SELECT offset, COALESCE(last_read, timestamp) FROM blocks ORDER BY COALESCE(last_read, timestamp)"
        ).fetchall()
        for offset, ts in rows:
            self._lru[offset] = ts

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _pack(self, raw: bytes) -> tuple[bytes, bool]:
        if self._compress and len(raw) >= _COMPRESS_THRESHOLD:
            return zlib.compress(raw, level=_COMPRESSION_LEVEL), True
        return raw, False

    @staticmethod
    def _unpack(payload: bytes, compressed: bool) -> bytes:
        if compressed:
            return zlib.decompress(payload)
        return payload

    def _check_capacity(self, offset: int) -> None:
        """Enforce world geometry bounds before a write."""
        if self._max_blocks is None:
            return
        if offset >= self._max_blocks:
            raise CapacityError(offset, self._max_blocks)
        # If already at capacity and this is a new block, evict or raise
        if not self._store_has_offset(offset):
            count = self.block_count()
            if count >= self._max_blocks:
                if self._evict_on_full:
                    self._evict_lru()
                else:
                    raise CapacityError(offset, self._max_blocks)

    def _store_has_offset(self, offset: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM blocks WHERE offset = ?", (offset,)
        ).fetchone()
        return row is not None

    def _evict_lru(self) -> None:
        """Remove the least-recently-read block to make room."""
        if not self._lru:
            return
        lru_offset, _ = next(iter(self._lru.items()))
        self.delete_block(lru_offset)
        log.info("LRU eviction: removed offset=%d", lru_offset)

    def _touch_lru(self, offset: int, ts: float) -> None:
        """Move *offset* to the most-recently-used end of the LRU map."""
        self._lru.pop(offset, None)
        self._lru[offset] = ts

    # ------------------------------------------------------------------
    # Public API — write
    # ------------------------------------------------------------------

    def write_block(self, offset: int, data: bytes) -> str:
        """Write *data* at *offset*, computing and storing a SHA256 checksum.

        Args:
            offset: Block offset (0 ≤ offset < max_blocks if bounded).
            data:   Raw block bytes to store.

        Returns:
            The SHA256 hex digest of the stored (compressed) payload.

        Raises:
            CapacityError: If the write would exceed the configured world size.
        """
        if offset < 0:
            raise ValueError(f"offset must be non-negative, got {offset}")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes or bytearray")

        self._check_capacity(offset)

        payload, compressed = self._pack(bytes(data))
        checksum = self._digest(payload)
        ts = time.time()
        self._write_seq += 1
        seq = self._write_seq

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO blocks (offset, data, size, checksum, compressed, timestamp, write_seq, last_read)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(offset) DO UPDATE SET
                    data       = excluded.data,
                    size       = excluded.size,
                    checksum   = excluded.checksum,
                    compressed = excluded.compressed,
                    timestamp  = excluded.timestamp,
                    write_seq  = excluded.write_seq,
                    last_read  = NULL
                """,
                (offset, payload, len(data), checksum, int(compressed), ts, seq),
            )

        self._touch_lru(offset, ts)
        log.debug("write_block offset=%d size=%d compressed=%s seq=%d checksum=%s",
                  offset, len(data), compressed, seq, checksum[:8])
        return checksum

    # ------------------------------------------------------------------
    # Public API — read
    # ------------------------------------------------------------------

    def read_block(self, offset: int, verify: bool = True) -> bytes:
        """Read and return the block at *offset*.

        Args:
            offset: Block offset to retrieve.
            verify: Recompute SHA256 and raise on mismatch (default True).

        Raises:
            BlockNotFoundError:    If *offset* is not in the store.
            ChecksumMismatchError: If *verify* is True and the payload is corrupt.
        """
        row = self._conn.execute(
            "SELECT data, checksum, compressed FROM blocks WHERE offset = ?",
            (offset,),
        ).fetchone()

        if row is None:
            raise BlockNotFoundError(f"No block at offset {offset}")

        payload, stored_checksum, compressed_flag = row
        compressed = bool(compressed_flag)

        if verify and stored_checksum is not None:
            actual = self._digest(payload)
            if actual != stored_checksum:
                log.error("Checksum mismatch at offset %d", offset)
                raise ChecksumMismatchError(offset, stored_checksum, actual)

        raw = self._unpack(payload, compressed)

        # Update last_read for LRU tracking
        ts = time.time()
        self._conn.execute(
            "UPDATE blocks SET last_read = ? WHERE offset = ?", (ts, offset)
        )
        self._conn.commit()
        self._touch_lru(offset, ts)

        log.debug("read_block offset=%d size=%d verified=%s", offset, len(raw), verify)
        return raw

    def read_block_unverified(self, offset: int) -> bytes:
        """Read block without checksum verification (fast path for trusted reads)."""
        return self.read_block(offset, verify=False)

    # ------------------------------------------------------------------
    # Public API — metadata & integrity
    # ------------------------------------------------------------------

    def get_block_metadata(self, offset: int) -> BlockMetadata:
        """Return metadata for *offset* without loading the full payload.

        Raises:
            BlockNotFoundError: If *offset* is not in the store.
        """
        row = self._conn.execute(
            "SELECT size, checksum, compressed, timestamp, write_seq FROM blocks WHERE offset = ?",
            (offset,),
        ).fetchone()

        if row is None:
            raise BlockNotFoundError(f"No block at offset {offset}")

        size, checksum, compressed_flag, timestamp, write_seq = row
        return BlockMetadata(
            offset=offset,
            size=size,
            checksum=checksum,
            compressed=bool(compressed_flag),
            timestamp=timestamp,
            write_seq=write_seq,
        )

    def verify_integrity(
        self, page_size: int = _SCAN_PAGE_SIZE
    ) -> Generator[IntegrityResult, None, None]:
        """Paginated integrity scan — yields one :class:`IntegrityResult` per block.

        Uses the dedicated read-only connection so live I/O is never stalled.
        Drive from a background thread or consume lazily:

            for result in store.verify_integrity():
                if result.status == "corrupted":
                    handle_corruption(result.offset)

        Args:
            page_size: Number of rows to fetch per SQL query (default 256).
        """
        offset_cursor = -1
        while True:
            rows = self._read_conn.execute(
                "SELECT offset, data, checksum FROM blocks "
                "WHERE offset > ? ORDER BY offset LIMIT ?",
                (offset_cursor, page_size),
            ).fetchall()

            if not rows:
                break

            for offset, payload, stored_checksum in rows:
                if stored_checksum is None:
                    yield IntegrityResult(offset=offset, status="unverified")
                else:
                    actual = self._digest(payload)
                    if actual == stored_checksum:
                        yield IntegrityResult(offset=offset, status="ok",
                                              checksum=stored_checksum)
                    else:
                        log.warning("Integrity failure at offset %d", offset)
                        yield IntegrityResult(offset=offset, status="corrupted",
                                              checksum=stored_checksum)

            offset_cursor = rows[-1][0]

    def verify_integrity_report(self) -> dict[str, list[int]]:
        """Convenience wrapper: drain :meth:`verify_integrity` into a summary dict.

        Returns ``{"ok": [...], "corrupted": [...], "unverified": [...]}``
        """
        report: dict[str, list[int]] = {"ok": [], "corrupted": [], "unverified": []}
        for result in self.verify_integrity():
            report[result.status].append(result.offset)
        log.info(
            "verify_integrity: ok=%d corrupted=%d unverified=%d",
            len(report["ok"]), len(report["corrupted"]), len(report["unverified"]),
        )
        return report

    # ------------------------------------------------------------------
    # Public API — housekeeping
    # ------------------------------------------------------------------

    def delete_block(self, offset: int) -> bool:
        """Delete the block at *offset*.  Returns True if a row was removed."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM blocks WHERE offset = ?", (offset,)
            )
        self._lru.pop(offset, None)
        return cur.rowcount > 0

    def block_exists(self, offset: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM blocks WHERE offset = ?", (offset,)
        ).fetchone()
        return row is not None

    def list_offsets(self) -> list[int]:
        rows = self._conn.execute(
            "SELECT offset FROM blocks ORDER BY offset"
        ).fetchall()
        return [r[0] for r in rows]

    def block_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM blocks").fetchone()
        return row[0]

    def current_write_seq(self) -> int:
        """Return the current monotonic write sequence counter."""
        return self._write_seq

    def close(self) -> None:
        self._conn.close()
        self._read_conn.close()

    def __enter__(self) -> "SparseBlockStore":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __repr__(self) -> str:
        cap = f"/{self._max_blocks}" if self._max_blocks else ""
        return (
            f"SparseBlockStore(path={self._path!r}, "
            f"blocks={self.block_count()}{cap}, "
            f"compress={self._compress})"
        )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys, tempfile

    logging.basicConfig(level=logging.DEBUG)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name

    # --- Capacity enforcement ---
    with SparseBlockStore(db, max_blocks=4, evict_on_full=False) as store:
        for i in range(4):
            store.write_block(i, b"x" * 128)
        try:
            store.write_block(4, b"overflow")
            assert False, "should have raised"
        except CapacityError as e:
            print(f"CapacityError raised correctly: {e}")

    # --- Normal round-trip + integrity scan ---
    import tempfile as tf
    db2 = tf.mktemp(suffix=".db")
    with SparseBlockStore(db2, max_blocks=1024) as store:
        data = b"Hello, Block-Offset-Engine!" * 10
        cksum = store.write_block(0, data)
        print(f"Written offset=0 checksum={cksum[:16]}...")

        result = store.read_block(0)
        assert result == data

        meta = store.get_block_metadata(0)
        print(f"Metadata: {meta}")

        results = list(store.verify_integrity())
        assert all(r.status == "ok" for r in results)
        print(f"Integrity scan: {results}")

        # Simulate hardware corruption
        store._conn.execute("UPDATE blocks SET data = X'DEADBEEF' WHERE offset = 0")
        store._conn.commit()

        try:
            store.read_block(0)
        except ChecksumMismatchError as e:
            print(f"Corruption detected: {e}")

    print("All checks passed.")
    sys.exit(0)
