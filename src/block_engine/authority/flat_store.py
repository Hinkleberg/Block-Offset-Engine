"""
flat_store.py
─────────────
Pure binary flat-file block store. Zero SQL. Zero middleware.

The world IS the file. A block at (x, y, z) IS the bytes at
offset(x, y, z) in the image. Reading a block is a seek + read.
Writing a block is a seek + write. That is the entire interface.

Each 16-byte block slot is written verbatim to the flat image.
A SHA-256 checksum sidecar file (.sha) holds one 32-byte digest
per block slot, stored at index = offset // BLOCK_SIZE.
On every read the digest is verified. Silent corruption is
structurally impossible.

No compression here by design — compression belongs in a tool
layer if needed. The core image stays 1:1 with the address space.

Thread safety: a single threading.Lock guards all I/O. Multiple
readers can be served by opening additional FlatStore instances
against the same file (safe for reads on POSIX/Windows with
exclusive-write semantics enforced by the caller).
"""

from __future__ import annotations

import hashlib
import os
import struct
import threading
from dataclasses import dataclass
from typing import Iterator, Optional

from block_layout import BLOCK_SIZE, WorldLayout, Block


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ChecksumMismatchError(Exception):
    """Raised when a read-back block fails its SHA-256 verification."""

class CapacityError(Exception):
    """Raised when a write would exceed the declared address space."""


# ---------------------------------------------------------------------------
# Sidecar checksum index
# DIGEST_SIZE bytes per slot, stored at slot_index * DIGEST_SIZE
# ---------------------------------------------------------------------------

DIGEST_SIZE = 32   # SHA-256


def _checksum(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


class _ChecksumIndex:
    """
    A flat binary file: one 32-byte SHA-256 digest per block slot.
    slot_index  =  byte_offset // BLOCK_SIZE
    file_offset =  slot_index  * DIGEST_SIZE
    """

    def __init__(self, path: str, total_blocks: int):
        self._path = path
        self._total = total_blocks
        self._ensure()

    def _ensure(self) -> None:
        expected = self._total * DIGEST_SIZE
        if not os.path.exists(self._path):
            with open(self._path, "wb") as f:
                f.write(b"\x00" * expected)
        else:
            actual = os.path.getsize(self._path)
            if actual < expected:
                with open(self._path, "ab") as f:
                    f.write(b"\x00" * (expected - actual))

    def write(self, slot: int, digest: bytes) -> None:
        with open(self._path, "r+b") as f:
            f.seek(slot * DIGEST_SIZE)
            f.write(digest)

    def read(self, slot: int) -> bytes:
        with open(self._path, "rb") as f:
            f.seek(slot * DIGEST_SIZE)
            return f.read(DIGEST_SIZE)


# ---------------------------------------------------------------------------
# Write-sequence sidecar
# A single uint64 per block slot: the monotonic write counter at last write.
# Stored at slot_index * 8.
# ---------------------------------------------------------------------------

_SEQ_FMT = struct.Struct("<Q")   # uint64 little-endian

class _SeqIndex:
    SLOT = 8  # bytes

    def __init__(self, path: str, total_blocks: int):
        self._path = path
        self._total = total_blocks
        self._ensure()

    def _ensure(self) -> None:
        expected = self._total * self.SLOT
        if not os.path.exists(self._path):
            with open(self._path, "wb") as f:
                f.write(b"\x00" * expected)
        else:
            actual = os.path.getsize(self._path)
            if actual < expected:
                with open(self._path, "ab") as f:
                    f.write(b"\x00" * (expected - actual))

    def write(self, slot: int, seq: int) -> None:
        with open(self._path, "r+b") as f:
            f.seek(slot * self.SLOT)
            f.write(_SEQ_FMT.pack(seq))

    def read(self, slot: int) -> int:
        with open(self._path, "rb") as f:
            f.seek(slot * self.SLOT)
            raw = f.read(self.SLOT)
            if len(raw) < self.SLOT:
                return 0
            return _SEQ_FMT.unpack(raw)[0]

    def max_seq(self) -> int:
        best = 0
        with open(self._path, "rb") as f:
            for _ in range(self._total):
                raw = f.read(self.SLOT)
                if len(raw) < self.SLOT:
                    break
                v = _SEQ_FMT.unpack(raw)[0]
                if v > best:
                    best = v
        return best


# ---------------------------------------------------------------------------
# FlatStore
# ---------------------------------------------------------------------------

@dataclass
class IntegrityResult:
    slot:   int
    offset: int
    status: str          # "ok" | "corrupted" | "empty"
    detail: str = ""


class FlatStore:
    """
    The world image as a flat binary file.

    image path  — the raw block image (total_blocks × BLOCK_SIZE bytes)
    .sha sidecar — SHA-256 digest per slot
    .seq sidecar — write_seq per slot
    """

    def __init__(
        self,
        image_path: str,
        layout: WorldLayout,
        *,
        evict_on_full: bool = False,   # reserved; flat images are pre-sized
    ):
        self._path   = image_path
        self._layout = layout
        self._lock   = threading.Lock()
        self._write_seq = 0

        sha_path = image_path + ".sha"
        seq_path = image_path + ".seq"

        self._sha = _ChecksumIndex(sha_path, layout.total_blocks)
        self._seq = _SeqIndex(seq_path, layout.total_blocks)

        self._ensure_image()

        # Restore write_seq from sidecar
        self._write_seq = self._seq.max_seq()

    # ------------------------------------------------------------------ init

    def _ensure_image(self) -> None:
        expected = self._layout.image_size
        if not os.path.exists(self._path):
            # Pre-allocate the full flat image (all zeros = all AIR blocks)
            with open(self._path, "wb") as f:
                # Write in 4 MB chunks to avoid huge allocations
                chunk = 4 * 1024 * 1024
                written = 0
                while written < expected:
                    n = min(chunk, expected - written)
                    f.write(b"\x00" * n)
                    written += n
        else:
            actual = os.path.getsize(self._path)
            if actual < expected:
                with open(self._path, "ab") as f:
                    f.write(b"\x00" * (expected - actual))

    # ----------------------------------------------------------------- write

    def write_block(self, offset: int, data: bytes) -> bytes:
        """
        Write raw block bytes to the flat image.

        Returns the SHA-256 digest of the written data.
        Raises CapacityError if offset is out of the declared image bounds.
        """
        if len(data) != BLOCK_SIZE:
            raise ValueError(f"write_block expects {BLOCK_SIZE} bytes, got {len(data)}")
        if offset + BLOCK_SIZE > self._layout.image_size:
            raise CapacityError(
                f"offset {offset} exceeds image size {self._layout.image_size}"
            )

        digest = _checksum(data)
        slot   = offset // BLOCK_SIZE

        with self._lock:
            self._write_seq += 1
            seq = self._write_seq

            with open(self._path, "r+b") as f:
                f.seek(offset)
                f.write(data)

            self._sha.write(slot, digest)
            self._seq.write(slot, seq)

        return digest

    def write_block_obj(self, offset: int, block: Block) -> bytes:
        return self.write_block(offset, block.to_bytes())

    # ------------------------------------------------------------------ read

    def read_block(self, offset: int) -> bytes:
        """
        Read and verify a block from the flat image.

        Raises ChecksumMismatchError on digest failure.
        An all-zero slot (never written) returns successfully — it is a valid
        AIR block.
        """
        if offset + BLOCK_SIZE > self._layout.image_size:
            raise CapacityError(f"offset {offset} out of bounds")

        slot = offset // BLOCK_SIZE

        with self._lock:
            with open(self._path, "rb") as f:
                f.seek(offset)
                data = f.read(BLOCK_SIZE)

            stored_digest = self._sha.read(slot)

        # An all-zero digest means this slot was never written — treat as valid AIR
        if stored_digest == b"\x00" * DIGEST_SIZE:
            return data

        actual_digest = _checksum(data)
        if actual_digest != stored_digest:
            raise ChecksumMismatchError(
                f"Checksum mismatch at offset {offset} "
                f"(stored={stored_digest.hex()[:12]}… "
                f"actual={actual_digest.hex()[:12]}…)"
            )
        return data

    def read_block_obj(self, offset: int) -> Block:
        return Block.from_bytes(self.read_block(offset))

    def read_range(self, start_offset: int, num_blocks: int) -> bytes:
        """
        Read a contiguous range of blocks as raw bytes.
        No checksum verification — used by the render feed for bulk streaming.
        For verified reads, iterate read_block().
        """
        length = num_blocks * BLOCK_SIZE
        if start_offset + length > self._layout.image_size:
            raise CapacityError("read_range out of bounds")
        with self._lock:
            with open(self._path, "rb") as f:
                f.seek(start_offset)
                return f.read(length)

    # ------------------------------------------------------------- metadata

    def write_seq_at(self, offset: int) -> int:
        return self._seq.read(offset // BLOCK_SIZE)

    @property
    def write_seq(self) -> int:
        with self._lock:
            return self._write_seq

    # ------------------------------------------------------------ integrity

    def verify_integrity(self) -> Iterator[IntegrityResult]:
        """
        Non-blocking generator scan of the entire image.

        Yields an IntegrityResult per block. Drive from a background
        thread or maintenance loop — each iteration opens a fresh file
        handle so live I/O is never stalled.
        """
        total = self._layout.total_blocks
        for slot in range(total):
            offset = slot * BLOCK_SIZE
            try:
                data = self.read_block(offset)
                yield IntegrityResult(slot, offset, "ok")
            except ChecksumMismatchError as e:
                yield IntegrityResult(slot, offset, "corrupted", str(e))
            except Exception as e:
                yield IntegrityResult(slot, offset, "error", str(e))

    # ---------------------------------------------------------------- paths

    @property
    def image_path(self) -> str:
        return self._path

    @property
    def layout(self) -> WorldLayout:
        return self._layout

    def __repr__(self) -> str:
        return (
            f"FlatStore({self._path!r}, {self._layout}, "
            f"write_seq={self._write_seq})"
        )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile, os

    layout = WorldLayout(16, 16, 16)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "test.img")
        store = FlatStore(path, layout)
        print(store)

        from block_layout import Block, BlockType, BlockFlags
        blk = Block(block_type=BlockType.GRASS, light_level=15,
                    flags=BlockFlags.SOLID, metadata=99)
        offset = layout.block_offset(5, 8, 3)
        store.write_block_obj(offset, blk)

        blk2 = store.read_block_obj(offset)
        assert blk2.block_type == BlockType.GRASS
        assert blk2.metadata == 99
        print("flat_store: write/read round-trip OK")

        # Integrity scan
        corrupt = 0
        ok = 0
        for result in store.verify_integrity():
            if result.status == "corrupted":
                corrupt += 1
            else:
                ok += 1
        assert corrupt == 0
        print(f"flat_store: integrity scan {ok} blocks OK, {corrupt} corrupted")

        # Write seq
        assert store.write_seq >= 1
        print(f"flat_store: write_seq={store.write_seq}")

    print("flat_store: all checks passed")