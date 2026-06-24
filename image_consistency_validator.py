"""
image_consistency_validator.py
──────────────────────────────
Validates that Array A (ResilientStore) and Array B (RenderStore) represent the same spatial state across the entire flat image address space.

This is distinct from block-level integrity checking (SHA-256 per block, which SparseBlockStore already performs on every read). This module - does the *image as a whole* agree with itself across both buffers at
every offset?*

A block pass its own checksum check and still be inconsistent with its counterpart in the other array. If the mirror forward has not delivered the data, if a crash occurred mid-forward, or if the consistency window is wider than
expected. This validator closes that gap.

Consistency is defined as:
    For every written offset O:
        array_a.write_seq(O) == array_b.write_seq(O)
               array_a.checksum(O)  == array_b.checksum(O)

The validator never holds locks on either array. It uses read-only metadata queries (get_block_metadata) so it cannot stall live I/O. Drive it from a background thread or a scheduled maintenance loop.

Classes
-------
ConsistencyResult       Named result for a single offset comparison.
ImageConsistencyReport  Aggregate report for a full or partial sweep.
ImageConsistencyValidator
                        Paginated sweep across the full image address space.

Usage
-----
    from image_consistency_validator import ImageConsistencyValidator

    validator = ImageConsistencyValidator(
        primary=resilient_store,       # Array A  (ResilientStore)
        mirror=render_store,           # Array B  (RenderStore)
        layout=world_layout,           # WorldLayout — defines valid address space
        on_inconsistency=my_callback,  # optional: called for each mismatch found
    )

    for result in validator.sweep():
        if not result.consistent:
            print(f"offset {result.offset}: {result.reason}")

    report = validator.full_report()
    print(report)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Generator, Iterator, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────

class InconsistencyReason(Enum):
    SEQ_MISMATCH      = auto()   # write_seq differs between arrays
    CHECKSUM_MISMATCH = auto()   # SHA-256 digest differs between arrays
    MISSING_IN_MIRROR = auto()   # block present in Array A, absent in Array B
    MISSING_IN_PRIMARY= auto()   # block present in Array B, absent in Array A
    READ_ERROR_PRIMARY= auto()   # metadata read failed on Array A
    READ_ERROR_MIRROR = auto()   # metadata read failed on Array B


@dataclass(frozen=True)
class ConsistencyResult:
    """Result for a single offset comparison between Array A and Array B."""
    offset: int
    consistent: bool
    reason: Optional[InconsistencyReason] = None
    primary_seq: Optional[int] = None
    mirror_seq: Optional[int] = None
    primary_checksum: Optional[str] = None
    mirror_checksum: Optional[str] = None

    def __str__(self) -> str:
        if self.consistent:
            return f"offset={self.offset} OK seq={self.primary_seq}"
        return (
            f"offset={self.offset} INCONSISTENT reason={self.reason.name} "
            f"primary_seq={self.primary_seq} mirror_seq={self.mirror_seq}"
        )


@dataclass
class ImageConsistencyReport:
    """Aggregate report produced by a full or partial image sweep."""
    sweep_start:        float = field(default_factory=time.monotonic)
    sweep_end:          Optional[float] = None
    offsets_checked:    int = 0
    offsets_consistent: int = 0
    offsets_inconsistent: int = 0
    offsets_skipped:    int = 0     # offsets not yet written in either array
    inconsistencies:    list[ConsistencyResult] = field(default_factory=list)

    # Per-reason breakdown
    seq_mismatches:       int = 0
    checksum_mismatches:  int = 0
    missing_in_mirror:    int = 0
    missing_in_primary:   int = 0
    read_errors:          int = 0

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.sweep_end is None:
            return None
        return self.sweep_end - self.sweep_start

    @property
    def consistency_ratio(self) -> float:
        if self.offsets_checked == 0:
            return 1.0
        return self.offsets_consistent / self.offsets_checked

    @property
    def image_consistent(self) -> bool:
        return self.offsets_inconsistent == 0

    def _tally(self, result: ConsistencyResult) -> None:
        self.offsets_checked += 1
        if result.consistent:
            self.offsets_consistent += 1
            return
        self.offsets_inconsistent += 1
        self.inconsistencies.append(result)
        r = result.reason
        if r == InconsistencyReason.SEQ_MISMATCH:
            self.seq_mismatches += 1
        elif r == InconsistencyReason.CHECKSUM_MISMATCH:
            self.checksum_mismatches += 1
        elif r == InconsistencyReason.MISSING_IN_MIRROR:
            self.missing_in_mirror += 1
        elif r == InconsistencyReason.MISSING_IN_PRIMARY:
            self.missing_in_primary += 1
        elif r in (InconsistencyReason.READ_ERROR_PRIMARY,
                   InconsistencyReason.READ_ERROR_MIRROR):
            self.read_errors += 1

    def __str__(self) -> str:
        status = "CONSISTENT" if self.image_consistent else "INCONSISTENT"
        dur    = f"{self.duration_seconds:.2f}s" if self.duration_seconds else "in progress"
        return (
            f"ImageConsistencyReport [{status}] {dur}\n"
            f"  checked={self.offsets_checked} "
            f"consistent={self.offsets_consistent} "
            f"inconsistent={self.offsets_inconsistent} "
            f"skipped={self.offsets_skipped}\n"
            f"  seq_mismatches={self.seq_mismatches} "
            f"checksum_mismatches={self.checksum_mismatches} "
            f"missing_in_mirror={self.missing_in_mirror} "
            f"missing_in_primary={self.missing_in_primary} "
            f"read_errors={self.read_errors}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Validator
# ─────────────────────────────────────────────────────────────────────────────

class ImageConsistencyValidator:
    """
    Sweep the full image address space. compare Array A against Array B
    at every offset using read-only metadata queries.

    The sweep is a paginated generator — it yields one ConsistencyResult per
    written offset and never holds a lock across iterations. Drive it from a
    background thread or a low-priority maintenance loop. It will not stall
    reads or writes under any load condition.

    Parameters
    ----------
    primary : ResilientStore
        Array A. Must expose get_block_metadata(offset) and write_seq property.
    mirror : RenderStore
        Array B. Must expose get_block_metadata(offset) and mirror_write_seq
        property.
    layout : WorldLayout
        Defines the valid address space (block_count, block_size).
    page_size : int
        Number of offsets evaluated per iteration before yielding. Controls
        CPU time slice per step. Default 256.
    on_inconsistency : callable, optional
        Called with each ConsistencyResult where consistent=False. Useful for
        real-time alerting without consuming the generator.
    """

    def __init__(
        self,
        primary,
        mirror,
        layout,
        page_size: int = 256,
        on_inconsistency: Optional[Callable[[ConsistencyResult], None]] = None,
    ) -> None:
        self._primary          = primary
        self._mirror           = mirror
        self._layout           = layout
        self._page_size        = page_size
        self._on_inconsistency = on_inconsistency
        self._lock             = threading.Lock()
        self._last_report: Optional[ImageConsistencyReport] = None

    # ── public ───────────────────────────────────────────────────────────────

    def sweep(
        self,
        start_offset: int = 0,
        end_offset: Optional[int] = None,
    ) -> Generator[ConsistencyResult, None, None]:
        """
        Paginated generator that yields one ConsistencyResult per offset in
        [start_offset, end_offset). end_offset defaults to the full image size.

        Example
        -------
            for result in validator.sweep():
                if not result.consistent:
                    handle(result)
        """
        block_size  = self._layout.block_size
        total_blocks = self._layout.total_blocks
        end          = end_offset if end_offset is not None else total_blocks * block_size

        offset = start_offset
        while offset < end:
            result = self._compare_offset(offset)
            if result is not None:
                if not result.consistent and self._on_inconsistency:
                    self._on_inconsistency(result)
                yield result
            offset += block_size

    def full_report(
        self,
        start_offset: int = 0,
        end_offset: Optional[int] = None,
    ) -> ImageConsistencyReport:
        """
        Run a complete sweep and return an ImageConsistencyReport.
        Blocks until the sweep is complete. For background use, drive sweep()
        directly from a thread.
        """
        report = ImageConsistencyReport()
        for result in self.sweep(start_offset, end_offset):
            report._tally(result)
        report.sweep_end = time.monotonic()
        with self._lock:
            self._last_report = report
        return report

    def seq_delta(self) -> int:
        """
        Return the current write_seq gap between Array A and Array B.
        This is a point-in-time snapshot, not a sweep.
        """
        try:
            primary_seq = self._primary.write_seq
        except Exception:
            primary_seq = 0
        try:
            mirror_seq = self._mirror.mirror_write_seq
        except Exception:
            mirror_seq = 0
        return max(0, primary_seq - mirror_seq)

    @property
    def last_report(self) -> Optional[ImageConsistencyReport]:
        """The most recent full_report result, or None if never run."""
        with self._lock:
            return self._last_report

    # ── internal ─────────────────────────────────────────────────────────────

    def _compare_offset(self, offset: int) -> Optional[ConsistencyResult]:
        """
        Compare the metadata for a single offset between Array A and Array B.
        Returns None, if the block has not been written to either array.
        """
        primary_meta = self._read_meta(self._primary, offset, is_primary=True)
        mirror_meta  = self._read_meta(self._mirror,  offset, is_primary=False)

        # Read errors
        if isinstance(primary_meta, InconsistencyReason):
            return ConsistencyResult(
                offset=offset, consistent=False, reason=primary_meta,
            )
        if isinstance(mirror_meta, InconsistencyReason):
            return ConsistencyResult(
                offset=offset, consistent=False, reason=mirror_meta,
            )

        # Both absent — block not yet written, skip
        if primary_meta is None and mirror_meta is None:
            return None

        # Present in A, absent in B
        if primary_meta is not None and mirror_meta is None:
            return ConsistencyResult(
                offset=offset,
                consistent=False,
                reason=InconsistencyReason.MISSING_IN_MIRROR,
                primary_seq=primary_meta.get("write_seq"),
                mirror_seq=None,
                primary_checksum=primary_meta.get("checksum"),
                mirror_checksum=None,
            )

        # Present in B, absent in A
        if primary_meta is None and mirror_meta is not None:
            return ConsistencyResult(
                offset=offset,
                consistent=False,
                reason=InconsistencyReason.MISSING_IN_PRIMARY,
                primary_seq=None,
                mirror_seq=mirror_meta.get("write_seq"),
                primary_checksum=None,
                mirror_checksum=mirror_meta.get("checksum"),
            )

        p_seq  = primary_meta.get("write_seq")
        m_seq  = mirror_meta.get("write_seq")
        p_csum = primary_meta.get("checksum")
        m_csum = mirror_meta.get("checksum")

        # Sequence mismatch (mirror is behind or diverged)
        if p_seq != m_seq:
            return ConsistencyResult(
                offset=offset,
                consistent=False,
                reason=InconsistencyReason.SEQ_MISMATCH,
                primary_seq=p_seq,
                mirror_seq=m_seq,
                primary_checksum=p_csum,
                mirror_checksum=m_csum,
            )

        # Checksum mismatch (same seq, different content — corruption indicator)
        if p_csum and m_csum and p_csum != m_csum:
            return ConsistencyResult(
                offset=offset,
                consistent=False,
                reason=InconsistencyReason.CHECKSUM_MISMATCH,
                primary_seq=p_seq,
                mirror_seq=m_seq,
                primary_checksum=p_csum,
                mirror_checksum=m_csum,
            )

        return ConsistencyResult(
            offset=offset,
            consistent=True,
            primary_seq=p_seq,
            mirror_seq=m_seq,
            primary_checksum=p_csum,
            mirror_checksum=m_csum,
        )

    def _read_meta(self, store, offset: int, is_primary: bool):
        """
        Returns a metadata dict, None (block absent), or an
        InconsistencyReason on read error.
        """
        try:
            meta = store.get_block_metadata(offset)
            return meta  # None if block not present
        except Exception:
            return (
                InconsistencyReason.READ_ERROR_PRIMARY if is_primary
                else InconsistencyReason.READ_ERROR_MIRROR
            )


# ─────────────────────────────────────────────────────────────────────────────
# Self-test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import hashlib

    print("image_consistency_validator.py — self-test")

    # ── Minimal stubs ─────────────────────────────────────────────────────────

    class _Layout:
        block_size   = 16
        total_blocks = 8
        # world is 8 blocks × 16 bytes = 128 bytes

    class _FakeStore:
        def __init__(self, name: str):
            self.name   = name
            self._meta: dict[int, dict] = {}
            self.write_seq       = 0
            self.mirror_write_seq= 0

        def write(self, offset: int, seq: int, checksum: str) -> None:
            self._meta[offset] = {"write_seq": seq, "checksum": checksum}

        def get_block_metadata(self, offset: int):
            return self._meta.get(offset)   # None if not present

    def _csum(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    layout  = _Layout()
    primary = _FakeStore("ArrayA")
    mirror  = _FakeStore("ArrayB")

    # Write 6 of 8 blocks to both arrays — consistent
    for i in range(6):
        off   = i * 16
        data  = bytes([i] * 16)
        csum  = _csum(data)
        primary.write(off, seq=i + 1, checksum=csum)
        mirror.write(off,  seq=i + 1, checksum=csum)
    primary.write_seq       = 6
    mirror.mirror_write_seq = 6

    # Introduce inconsistencies on offsets 96 and 112
    primary.write(96,  seq=7, checksum=_csum(b'\xAA' * 16))
    # mirror missing offset 96 entirely → MISSING_IN_MIRROR

    primary.write(112, seq=8, checksum=_csum(b'\xBB' * 16))
    mirror.write(112,  seq=8, checksum=_csum(b'\xCC' * 16))  # different data → CHECKSUM_MISMATCH

    primary.write_seq = 8

    validator = ImageConsistencyValidator(primary, mirror, layout)
    report    = validator.full_report()

    print(report)
    print(f"  seq_delta = {validator.seq_delta()}")
    for r in report.inconsistencies:
        print(f"  {r}")

    assert not report.image_consistent
    assert report.offsets_consistent   == 6
    assert report.offsets_inconsistent == 2
    assert report.missing_in_mirror    == 1
    assert report.checksum_mismatches  == 1

    print("PASS")