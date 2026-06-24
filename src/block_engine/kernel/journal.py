"""
journal.py
──────────
Crash-safe write-ahead journal. Pure binary. Zero SQL.

Before any block touches the flat image, the intent is journaled here.
On restart, any un-committed entry is replayed. The engine cannot
silently lose a write.

Journal file format (binary, append-only):
  [ENTRY_MAGIC 4B][entry_len 4B][offset 8B][seq 8B][data BLOCK_SIZE B][CRC32 4B]

Entry is COMMITTED by overwriting byte[0:4] with COMMIT_MAGIC.
Any entry not starting with COMMIT_MAGIC on startup is a pending replay.

File layout is fixed-width so scan and seek are O(1) per entry.
"""

from __future__ import annotations

import os
import struct
import threading
import zlib
from dataclasses import dataclass
from typing import Iterator, List, Optional

from block_layout import BLOCK_SIZE


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENTRY_MAGIC  = b"JRNL"
COMMIT_MAGIC = b"DONE"
ABORT_MAGIC  = b"ABRT"

# Header: 4B magic + 4B entry_len + 8B offset + 8B seq = 24B
# Payload: BLOCK_SIZE bytes
# Footer: 4B CRC32
HEADER_FMT = struct.Struct("<4sIQQ")   # magic, entry_len, offset, seq
FOOTER_FMT = struct.Struct("<I")       # CRC32

HEADER_SIZE  = HEADER_FMT.size          # 24
FOOTER_SIZE  = FOOTER_FMT.size          # 4
ENTRY_SIZE   = HEADER_SIZE + BLOCK_SIZE + FOOTER_SIZE   # 44 + 16 = fixed


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

@dataclass
class JournalEntry:
    offset:    int
    seq:       int
    data:      bytes
    committed: bool = False

    @property
    def file_offset(self) -> int:
        """Set by Journal after appending; where this entry lives in the journal."""
        return getattr(self, "_file_offset", -1)


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

class Journal:
    """
    Append-only write-ahead journal over a plain binary file.

    Usage pattern:
        entry_pos = journal.append(offset, seq, data)   # pre-commit
        flat_store.write_block(offset, data)            # actual write
        journal.commit(entry_pos)                       # mark done

    On startup:
        for entry in journal.pending():
            flat_store.write_block(entry.offset, entry.data)
            journal.commit(entry._file_offset)
        journal.compact()   # optional: truncate committed entries
    """

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._ensure()

    def _ensure(self) -> None:
        if not os.path.exists(self._path):
            open(self._path, "wb").close()

    # ---------------------------------------------------------------- append

    def append(self, offset: int, seq: int, data: bytes) -> int:
        """
        Write a pending journal entry.
        Returns the file offset of the entry (used for commit/abort).
        """
        if len(data) != BLOCK_SIZE:
            raise ValueError(f"Journal entry data must be {BLOCK_SIZE} bytes")

        payload  = data
        crc_data = HEADER_FMT.pack(ENTRY_MAGIC, ENTRY_SIZE, offset, seq) + payload
        crc      = zlib.crc32(crc_data) & 0xFFFFFFFF

        raw = crc_data + FOOTER_FMT.pack(crc)

        with self._lock:
            with open(self._path, "ab") as f:
                pos = f.tell()
                f.write(raw)
        return pos

    # --------------------------------------------------------------- commit

    def commit(self, entry_file_offset: int) -> None:
        """Overwrite the magic bytes of an entry with COMMIT_MAGIC."""
        with self._lock:
            with open(self._path, "r+b") as f:
                f.seek(entry_file_offset)
                f.write(COMMIT_MAGIC)

    def abort(self, entry_file_offset: int) -> None:
        """Mark entry as aborted (will be skipped on replay)."""
        with self._lock:
            with open(self._path, "r+b") as f:
                f.seek(entry_file_offset)
                f.write(ABORT_MAGIC)

    # --------------------------------------------------------------- replay

    def pending(self) -> Iterator[JournalEntry]:
        """
        Yield all un-committed, non-aborted entries.
        Call on startup before accepting any new writes.
        """
        size = os.path.getsize(self._path)
        pos  = 0

        with open(self._path, "rb") as f:
            while pos + ENTRY_SIZE <= size:
                f.seek(pos)
                raw = f.read(ENTRY_SIZE)
                if len(raw) < ENTRY_SIZE:
                    break

                magic = raw[:4]
                if magic == COMMIT_MAGIC or magic == ABORT_MAGIC:
                    pos += ENTRY_SIZE
                    continue
                if magic != ENTRY_MAGIC:
                    # Truncated or corrupt entry — stop scan
                    break

                _, entry_len, offset, seq = HEADER_FMT.unpack(raw[:HEADER_SIZE])
                data = raw[HEADER_SIZE : HEADER_SIZE + BLOCK_SIZE]
                stored_crc = FOOTER_FMT.unpack(raw[HEADER_SIZE + BLOCK_SIZE:])[0]

                # Verify CRC
                actual_crc = zlib.crc32(raw[:HEADER_SIZE + BLOCK_SIZE]) & 0xFFFFFFFF
                if actual_crc != stored_crc:
                    pos += ENTRY_SIZE
                    continue

                entry = JournalEntry(offset=offset, seq=seq, data=data)
                entry._file_offset = pos  # type: ignore[attr-defined]
                yield entry

                pos += ENTRY_SIZE

    # -------------------------------------------------------------- compact

    def compact(self) -> int:
        """
        Rewrite the journal keeping only un-committed entries.
        Returns the number of entries retained.
        Safe to call during normal operation.
        """
        pending_entries: List[bytes] = []
        size = os.path.getsize(self._path)
        pos  = 0

        with open(self._path, "rb") as f:
            while pos + ENTRY_SIZE <= size:
                f.seek(pos)
                raw = f.read(ENTRY_SIZE)
                if len(raw) < ENTRY_SIZE:
                    break
                magic = raw[:4]
                if magic == ENTRY_MAGIC:
                    pending_entries.append(raw)
                pos += ENTRY_SIZE

        with self._lock:
            with open(self._path, "wb") as f:
                for raw in pending_entries:
                    f.write(raw)

        return len(pending_entries)

    @property
    def path(self) -> str:
        return self._path

    def __repr__(self) -> str:
        size = os.path.getsize(self._path) if os.path.exists(self._path) else 0
        return f"Journal({self._path!r}, {size // ENTRY_SIZE} entries)"


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile, os
    from block_layout import Block, BlockType

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "test.jrn")
        j = Journal(path)
        print(j)

        data = Block(block_type=BlockType.STONE).to_bytes()

        # Write and commit
        pos1 = j.append(0, 1, data)
        j.commit(pos1)

        # Write but do NOT commit (simulates crash)
        pos2 = j.append(16, 2, data)

        # Replay
        pending = list(j.pending())
        assert len(pending) == 1, f"Expected 1 pending, got {len(pending)}"
        assert pending[0].offset == 16
        j.commit(pending[0]._file_offset)

        # All committed now
        assert list(j.pending()) == []

        # Compact
        retained = j.compact()
        assert retained == 0
        print(f"journal: compact retained {retained} entries")

        print("journal: all checks passed")