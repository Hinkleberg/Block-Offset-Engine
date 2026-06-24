"""
replication_manager.py
──────────────────────
Multi-node block replication with quorum enforcement.
Persistent entry log stored as a binary flat file. Zero SQL.

Log format: fixed-width 64-byte records.
  [offset 8B][seq 8B][node_id 32B (utf-8 padded)][status 1B][_pad 15B]

status byte: 0x01 = SUCCESS, 0x00 = FAIL

The log survives restarts. nodes_with_block() is always accurate
because it is derived entirely from the binary log.
"""

from __future__ import annotations

import os
import struct
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Dict, List, Optional, Set


# ---------------------------------------------------------------------------
# Log record
# ---------------------------------------------------------------------------

_LOG_FMT = struct.Struct("<QQ32sB15x")   # offset, seq, node_id(32B), status, pad
LOG_RECORD_SIZE = _LOG_FMT.size          # 64 bytes

class _Status(IntEnum):
    FAIL    = 0
    SUCCESS = 1


def _encode_node(node_id: str) -> bytes:
    raw = node_id.encode("utf-8")[:32]
    return raw.ljust(32, b"\x00")


def _decode_node(raw: bytes) -> str:
    return raw.rstrip(b"\x00").decode("utf-8")


class _ReplicationLog:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        if not os.path.exists(path):
            open(path, "wb").close()

    def append(self, offset: int, seq: int, node_id: str, success: bool) -> None:
        record = _LOG_FMT.pack(
            offset, seq,
            _encode_node(node_id),
            _Status.SUCCESS if success else _Status.FAIL,
        )
        with self._lock:
            with open(self._path, "ab") as f:
                f.write(record)

    def nodes_with_block(self, offset: int) -> Set[str]:
        """Return set of node_ids that have successfully replicated this offset."""
        result: Set[str] = set()
        size = os.path.getsize(self._path)
        with open(self._path, "rb") as f:
            pos = 0
            while pos + LOG_RECORD_SIZE <= size:
                f.seek(pos)
                raw = f.read(LOG_RECORD_SIZE)
                if len(raw) < LOG_RECORD_SIZE:
                    break
                off, seq, node_raw, status = _LOG_FMT.unpack(raw)
                if off == offset and status == _Status.SUCCESS:
                    result.add(_decode_node(node_raw))
                pos += LOG_RECORD_SIZE
        return result

    def all_offsets_for_node(self, node_id: str) -> Set[int]:
        enc = _encode_node(node_id)
        result: Set[int] = set()
        size = os.path.getsize(self._path)
        with open(self._path, "rb") as f:
            pos = 0
            while pos + LOG_RECORD_SIZE <= size:
                raw = f.read(LOG_RECORD_SIZE)
                if len(raw) < LOG_RECORD_SIZE:
                    break
                off, seq, node_raw, status = _LOG_FMT.unpack(raw)
                if node_raw == enc and status == _Status.SUCCESS:
                    result.add(off)
                pos += LOG_RECORD_SIZE
        return result

    def record_count(self) -> int:
        size = os.path.getsize(self._path)
        return size // LOG_RECORD_SIZE


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

@dataclass
class NodeInfo:
    node_id:        str
    metadata:       dict = field(default_factory=dict)
    healthy:        bool = True
    fail_count:     int  = 0
    last_seen:      float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class QuorumError(Exception):
    pass


# ---------------------------------------------------------------------------
# ReplicationEntry (returned from replicate_block)
# ---------------------------------------------------------------------------

@dataclass
class ReplicationEntry:
    offset:           int
    seq:              int
    successful_nodes: List[str]
    failed_nodes:     List[str]
    quorum_met:       bool


# ---------------------------------------------------------------------------
# ReplicationManager
# ---------------------------------------------------------------------------

SyncCallback = Callable[[str, int, bytes], None]


class ReplicationManager:
    """
    Fans a block out to N registered nodes via a pluggable sync_callback.
    Quorum is hard-enforced — writes below threshold raise QuorumError.
    Persistent log in a binary flat file; no SQL.
    """

    def __init__(
        self,
        sync_callback: SyncCallback,
        *,
        required_replicas: int = 2,
        failure_threshold: int = 3,
        log_path: str = "repl_log.bin",
    ):
        self._sync      = sync_callback
        self._required  = required_replicas
        self._threshold = failure_threshold
        self._nodes:    Dict[str, NodeInfo] = {}
        self._lock      = threading.Lock()
        self._log       = _ReplicationLog(log_path)

    # ----------------------------------------------------------------- nodes

    def register_node(self, node_id: str, metadata: Optional[dict] = None) -> None:
        with self._lock:
            self._nodes[node_id] = NodeInfo(node_id, metadata or {})

    def deregister_node(self, node_id: str) -> None:
        with self._lock:
            self._nodes.pop(node_id, None)

    def mark_healthy(self, node_id: str) -> None:
        with self._lock:
            if node_id in self._nodes:
                self._nodes[node_id].healthy   = True
                self._nodes[node_id].fail_count = 0

    def mark_unhealthy(self, node_id: str) -> None:
        with self._lock:
            if node_id in self._nodes:
                self._nodes[node_id].healthy = False

    # -------------------------------------------------------------- replicate

    def replicate_block(self, offset: int, data: bytes, seq: int = 0) -> ReplicationEntry:
        with self._lock:
            healthy = [n for n in self._nodes.values() if n.healthy]

        successful: List[str] = []
        failed:     List[str] = []

        for node in healthy:
            try:
                self._sync(node.node_id, offset, data)
                successful.append(node.node_id)
                self._log.append(offset, seq, node.node_id, True)
                with self._lock:
                    if node.node_id in self._nodes:
                        self._nodes[node.node_id].fail_count = 0
                        self._nodes[node.node_id].last_seen  = time.time()
            except Exception:
                failed.append(node.node_id)
                self._log.append(offset, seq, node.node_id, False)
                with self._lock:
                    if node.node_id in self._nodes:
                        n = self._nodes[node.node_id]
                        n.fail_count += 1
                        if n.fail_count >= self._threshold:
                            n.healthy = False

        quorum = len(successful) >= self._required
        entry  = ReplicationEntry(offset, seq, successful, failed, quorum)

        if not quorum:
            raise QuorumError(
                f"Quorum not met at offset {offset}: "
                f"{len(successful)}/{self._required} replicas succeeded"
            )
        return entry

    # --------------------------------------------------------------- queries

    def nodes_with_block(self, offset: int) -> Set[str]:
        return self._log.nodes_with_block(offset)

    # -------------------------------------------------------------- stats

    def statistics(self) -> dict:
        with self._lock:
            return {
                nid: {
                    "healthy":    n.healthy,
                    "fail_count": n.fail_count,
                    "last_seen":  n.last_seen,
                }
                for nid, n in self._nodes.items()
            }

    def health_report(self) -> str:
        stats = self.statistics()
        lines = ["ReplicationManager health:"]
        for nid, s in stats.items():
            status = "HEALTHY" if s["healthy"] else "UNHEALTHY"
            lines.append(f"  {nid}: {status} (fails={s['fail_count']})")
        lines.append(f"  log_records={self._log.record_count()}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from block_layout import Block, BlockType

    def make_sync(store: dict):
        def _sync(node_id, offset, data):
            store.setdefault(node_id, {})[offset] = data
        return _sync

    remote: dict = {}
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tf:
        log_path = tf.name

    try:
        rm = ReplicationManager(
            sync_callback=make_sync(remote),
            required_replicas=2,
            log_path=log_path,
        )
        rm.register_node("node-a", {"host": "10.0.0.1"})
        rm.register_node("node-b", {"host": "10.0.0.2"})
        rm.register_node("node-c", {"host": "10.0.0.3"})

        data = Block(block_type=BlockType.STONE).to_bytes()
        entry = rm.replicate_block(0, data, seq=1)
        assert entry.quorum_met
        print(f"replication_manager: quorum met, nodes={entry.successful_nodes}")

        nodes = rm.nodes_with_block(0)
        assert len(nodes) >= 2
        print(f"replication_manager: nodes_with_block(0) = {nodes}")
        print(rm.health_report())
        print("replication_manager: all checks passed")
    finally:
        os.unlink(log_path)