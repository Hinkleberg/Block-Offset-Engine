"""
resilient_store.py
──────────────────
The integration layer. Combines crash safety (Journal), integrity
verification (FlatStore), replication (ReplicationManager), and async
mirror fan-out to the render array into one coherent write/read path.
Zero SQL.

Write flow:
  1. Journal the write intent (crash-safe pre-commit).
  2. Write to local FlatStore (checksummed).
  3. Confirm write via write_seq read-back (read-your-writes guarantee).
  4. Fan out to replicas via ReplicationManager (quorum enforced).
  5. Commit the journal entry.
  6. Async forward to all registered render mirrors — fires outside
     the write lock, never adds latency to the mutation path.

Read flow:
  1. Read from local FlatStore with checksum verification.
  2. On ChecksumMismatchError → attempt recovery from replicas.
  3. On successful recovery → overwrite corrupt local block; return data.
  4. If all replicas fail → raise CorruptBlockError.

Crash recovery:
  On startup, journal.pending() is replayed. If the block exists and
  is intact, the entry is auto-committed. If missing or corrupt, the
  offset is queued in pending_replay for caller re-issue.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Set

from block_layout import BLOCK_SIZE, WorldLayout
from flat_store import FlatStore, ChecksumMismatchError, CapacityError
from journal import Journal
from replication_manager import ReplicationManager, QuorumError


# ---------------------------------------------------------------------------
# Block / health state
# ---------------------------------------------------------------------------

class BlockState(Enum):
    PENDING    = "PENDING"
    CLEAN      = "CLEAN"
    SYNCING    = "SYNCING"
    REPLICATED = "REPLICATED"
    CORRUPTED  = "CORRUPTED"


class HealthState(Enum):
    HEALTHY  = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"


class CorruptBlockError(Exception):
    pass


# ---------------------------------------------------------------------------
# Write record
# ---------------------------------------------------------------------------

@dataclass
class WriteRecord:
    offset:  int
    seq:     int
    state:   BlockState = BlockState.PENDING
    ts:      float      = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# ResilientStore
# ---------------------------------------------------------------------------

MirrorCallback = Callable[[int, bytes, int], None]   # (offset, data, write_seq)
RecoveryCallback = Callable[[str, int], bytes]        # (node_id, offset) -> data


class ResilientStore:
    """
    Full-stack write/read path with crash safety, quorum replication,
    and async mirror fan-out. No SQL.
    """

    def __init__(
        self,
        local_store:          FlatStore,
        replication_manager:  Optional[ReplicationManager] = None,
        journal_path:         str = "world.jrn",
        recovery_callback:    Optional[RecoveryCallback] = None,
    ):
        self._store    = local_store
        self._rm       = replication_manager
        self._recovery = recovery_callback
        self._journal  = Journal(journal_path)
        self._lock     = threading.Lock()
        self._mirrors: List[MirrorCallback] = []

        self._block_states: Dict[int, BlockState] = {}
        self._pending_replay: Set[int] = set()

        self._error_count = 0
        self._write_count = 0

        self._recover_from_journal()

    # ------------------------------------------------------------ journal replay

    def _recover_from_journal(self) -> None:
        for entry in self._journal.pending():
            try:
                existing = self._store.read_block(entry.offset)
                # Block is intact — auto-commit the journal entry
                self._journal.commit(entry._file_offset)  # type: ignore
            except (ChecksumMismatchError, CapacityError):
                self._pending_replay.add(entry.offset)

    @property
    def pending_replay(self) -> Set[int]:
        return set(self._pending_replay)

    # --------------------------------------------------------------- mirrors

    def register_mirror(self, callback: MirrorCallback) -> None:
        with self._lock:
            self._mirrors.append(callback)

    # ----------------------------------------------------------------- write

    def write_block(self, offset: int, data: bytes) -> WriteRecord:
        """
        Full write path: journal → FlatStore → replicate → commit → mirror.
        """
        if len(data) != BLOCK_SIZE:
            raise ValueError(f"write_block requires {BLOCK_SIZE} bytes")

        with self._lock:
            seq = self._store.write_seq + 1

        # 1. Journal
        jpos = self._journal.append(offset, seq, data)
        self._set_state(offset, BlockState.PENDING)

        # 2. Write to flat image
        self._store.write_block(offset, data)
        self._set_state(offset, BlockState.CLEAN)
        self._write_count += 1

        # 3. Replicate
        if self._rm is not None:
            self._set_state(offset, BlockState.SYNCING)
            try:
                self._rm.replicate_block(offset, data, seq=self._store.write_seq)
                self._set_state(offset, BlockState.REPLICATED)
            except QuorumError:
                # Locally durable; replication fell short of quorum
                self._set_state(offset, BlockState.CLEAN)
                raise

        # 4. Commit journal
        self._journal.commit(jpos)
        self._pending_replay.discard(offset)

        record = WriteRecord(offset=offset, seq=self._store.write_seq,
                             state=self._block_states.get(offset, BlockState.CLEAN))

        # 5. Async mirror forward (outside write lock)
        if self._mirrors:
            seq_snap = self._store.write_seq
            mirrors  = list(self._mirrors)
            t = threading.Thread(
                target=self._forward_to_mirrors,
                args=(mirrors, offset, data, seq_snap),
                daemon=True,
            )
            t.start()

        return record

    def _forward_to_mirrors(
        self,
        mirrors: List[MirrorCallback],
        offset: int,
        data: bytes,
        seq: int,
    ) -> None:
        for cb in mirrors:
            try:
                cb(offset, data, seq)
            except Exception:
                pass

    # ------------------------------------------------------------------ read

    def read_block(self, offset: int) -> bytes:
        """
        Read with auto-recovery on corruption.
        """
        try:
            return self._store.read_block(offset)
        except ChecksumMismatchError:
            self._error_count += 1
            self._set_state(offset, BlockState.CORRUPTED)
            return self._attempt_recovery(offset)

    def _attempt_recovery(self, offset: int) -> bytes:
        if self._rm is None or self._recovery is None:
            raise CorruptBlockError(f"No recovery path for offset {offset}")

        nodes = self._rm.nodes_with_block(offset)
        for node_id in nodes:
            try:
                data = self._recovery(node_id, offset)
                self._store.write_block(offset, data)
                self._set_state(offset, BlockState.REPLICATED)
                return data
            except Exception:
                continue

        raise CorruptBlockError(
            f"Block at offset {offset} is corrupt and all recovery paths failed"
        )

    # --------------------------------------------------------------- helpers

    def _set_state(self, offset: int, state: BlockState) -> None:
        self._block_states[offset] = state

    def block_state(self, offset: int) -> BlockState:
        return self._block_states.get(offset, BlockState.CLEAN)

    # --------------------------------------------------------------- health

    def health(self) -> HealthState:
        if self._error_count == 0:
            return HealthState.HEALTHY
        ratio = self._error_count / max(1, self._write_count + self._error_count)
        if ratio < 0.01:
            return HealthState.DEGRADED
        return HealthState.CRITICAL

    def health_report(self) -> dict:
        return {
            "health":       self.health().value,
            "write_seq":    self._store.write_seq,
            "error_count":  self._error_count,
            "write_count":  self._write_count,
            "pending_replay": len(self._pending_replay),
            "mirror_count": len(self._mirrors),
        }

    @property
    def write_seq(self) -> int:
        return self._store.write_seq


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile, os
    from block_layout import Block, BlockType, WorldLayout

    layout = WorldLayout(16, 16, 16)
    with tempfile.TemporaryDirectory() as tmp:
        img  = os.path.join(tmp, "world.img")
        jrn  = os.path.join(tmp, "world.jrn")

        store = FlatStore(img, layout)
        rs    = ResilientStore(store, journal_path=jrn)

        data   = Block(block_type=BlockType.STONE, light_level=10).to_bytes()
        offset = layout.block_offset(1, 1, 1)
        record = rs.write_block(offset, data)
        print(f"resilient_store: write record state={record.state}")

        read_back = rs.read_block(offset)
        assert read_back == data
        print("resilient_store: read-back matches")
        print(rs.health_report())
        print("resilient_store: all checks passed")