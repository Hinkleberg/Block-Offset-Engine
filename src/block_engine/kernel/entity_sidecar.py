"""
entity_sidecar.py
─────────────────
Parallel entity state image. Pure binary flat file. Zero SQL.

Entity state is intentionally separated from the world block image.
Entities update every tick at high frequency; geometry changes slowly.
Mixing the two write patterns would destroy the sequential read
characteristics the render feed depends on.

Format: fixed 64-byte slots addressed by entity_id directly.
    file_offset = entity_id × 64

No index. No join. No query engine. Entity lookup is a single seek.

The block image references entities via the entity_hint field —
a byte offset into this sidecar — so the render feed jumps from
a block read to the entity record with one additional offset lookup.

Slot format (64 bytes):
  [0]   entity_id   u32   (0 = empty slot)
  [4]   entity_type u8
  [5]   flags       u8
  [6-7] reserved    u16
  [8]   x           f32
  [12]  y           f32
  [16]  z           f32
  [20]  vx          f32
  [24]  vy          f32
  [28]  vz          f32
  [32]  yaw         f32
  [36]  pitch       f32
  [40]  health      f32
  [44]  entity_meta f32
  [48]  owner_id    u64
  [56]  last_tick   u64
"""

from __future__ import annotations

import os
import struct
import threading
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterator, List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENTITY_SLOT_SIZE = 64
MAX_ENTITIES_DEFAULT = 65536   # pre-allocate file for this many slots

_FMT = struct.Struct("<IBBxxxxffffffffffff QQ")
# Breakdown: I=entity_id, B=type, B=flags, xxxx=4B reserved,
#   12×f = x,y,z, vx,vy,vz, yaw,pitch, health,meta  — 10 floats × 4 = 40B
# Wait: let me recount to hit 64B total
# I(4) B(1) B(1) x(1) x(1) x(1) x(1)  = 10B header (with 4B pad)
# actually keep it simple with a clean struct:

_SLOT = struct.Struct("<IBB2x" + "f"*10 + "QQ")
# I(4) B(1) B(1) 2x(2) = 8B
# 10×f = 40B
# QQ = 16B
# total = 64B ✓

assert _SLOT.size == 64, f"Slot struct is {_SLOT.size} bytes, expected 64"


class EntityType(IntEnum):
    EMPTY      = 0
    PLAYER     = 1
    MOB        = 2
    ITEM       = 3
    PROJECTILE = 4


class EntityFlags(IntEnum):
    ACTIVE     = 1 << 0
    VISIBLE    = 1 << 1
    COLLIDABLE = 1 << 2


# ---------------------------------------------------------------------------
# EntityRecord
# ---------------------------------------------------------------------------

@dataclass
class EntityRecord:
    entity_id:   int         = 0
    entity_type: int         = EntityType.EMPTY
    flags:       int         = 0
    x:           float       = 0.0
    y:           float       = 0.0
    z:           float       = 0.0
    vx:          float       = 0.0
    vy:          float       = 0.0
    vz:          float       = 0.0
    yaw:         float       = 0.0
    pitch:       float       = 0.0
    health:      float       = 0.0
    metadata:    float       = 0.0
    owner_id:    int         = 0
    last_tick:   int         = 0

    def to_bytes(self) -> bytes:
        return _SLOT.pack(
            self.entity_id, self.entity_type & 0xFF, self.flags & 0xFF,
            self.x, self.y, self.z,
            self.vx, self.vy, self.vz,
            self.yaw, self.pitch,
            self.health, self.metadata,
            self.owner_id, self.last_tick,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "EntityRecord":
        (eid, etype, flags,
         x, y, z, vx, vy, vz, yaw, pitch, health, meta,
         owner, tick) = _SLOT.unpack(raw)
        return cls(eid, etype, flags, x, y, z, vx, vy, vz,
                   yaw, pitch, health, meta, owner, tick)

    @property
    def is_empty(self) -> bool:
        return self.entity_id == 0

    def entity_sidecar_offset(self) -> int:
        """Byte offset of this entity's slot in the sidecar file."""
        return self.entity_id * ENTITY_SLOT_SIZE


# ---------------------------------------------------------------------------
# EntitySidecar
# ---------------------------------------------------------------------------

class EntitySidecar:
    """
    Parallel entity state flat image.
    All operations are O(1): one seek per read/write.
    """

    def __init__(self, path: str, max_entities: int = MAX_ENTITIES_DEFAULT):
        self._path = path
        self._max  = max_entities
        self._lock = threading.Lock()
        self._ensure()

    def _ensure(self) -> None:
        expected = self._max * ENTITY_SLOT_SIZE
        if not os.path.exists(self._path):
            with open(self._path, "wb") as f:
                chunk = 1024 * 1024
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

    # ---------------------------------------------------------------- write

    def write_entity(self, rec: EntityRecord) -> None:
        if rec.entity_id == 0:
            raise ValueError("entity_id 0 is reserved for empty slots")
        if rec.entity_id >= self._max:
            raise ValueError(f"entity_id {rec.entity_id} exceeds max {self._max}")
        offset = rec.entity_id * ENTITY_SLOT_SIZE
        with self._lock:
            with open(self._path, "r+b") as f:
                f.seek(offset)
                f.write(rec.to_bytes())

    # ----------------------------------------------------------------- read

    def read_entity(self, entity_id: int) -> Optional[EntityRecord]:
        if entity_id == 0 or entity_id >= self._max:
            return None
        offset = entity_id * ENTITY_SLOT_SIZE
        with self._lock:
            with open(self._path, "rb") as f:
                f.seek(offset)
                raw = f.read(ENTITY_SLOT_SIZE)
        if len(raw) < ENTITY_SLOT_SIZE:
            return None
        rec = EntityRecord.from_bytes(raw)
        return None if rec.is_empty else rec

    # --------------------------------------------------------------- delete

    def delete_entity(self, entity_id: int) -> None:
        if entity_id == 0 or entity_id >= self._max:
            return
        offset = entity_id * ENTITY_SLOT_SIZE
        with self._lock:
            with open(self._path, "r+b") as f:
                f.seek(offset)
                f.write(b"\x00" * ENTITY_SLOT_SIZE)

    # ---------------------------------------------------------------- alloc

    def allocate_id(self) -> int:
        """Return lowest unused (zeroed) entity slot id."""
        with self._lock:
            with open(self._path, "rb") as f:
                for eid in range(1, self._max):
                    f.seek(eid * ENTITY_SLOT_SIZE)
                    raw = f.read(ENTITY_SLOT_SIZE)
                    if raw == b"\x00" * ENTITY_SLOT_SIZE:
                        return eid
        raise RuntimeError("No free entity slots")

    # --------------------------------------------------------------- delta

    def tick_delta(self, since_tick: int) -> List[EntityRecord]:
        """All entities updated after since_tick. Used by render feed."""
        results: List[EntityRecord] = []
        with self._lock:
            with open(self._path, "rb") as f:
                for eid in range(1, self._max):
                    f.seek(eid * ENTITY_SLOT_SIZE)
                    raw = f.read(ENTITY_SLOT_SIZE)
                    if len(raw) < ENTITY_SLOT_SIZE:
                        break
                    if raw == b"\x00" * ENTITY_SLOT_SIZE:
                        continue
                    rec = EntityRecord.from_bytes(raw)
                    if not rec.is_empty and rec.last_tick > since_tick:
                        results.append(rec)
        return results

    # ----------------------------------------------------------- spatial

    def entities_near(self, x: float, y: float, z: float, radius: float) -> List[EntityRecord]:
        """
        Linear scan spatial query. Replace with R-tree or spatial hash
        for production entity counts above a few thousand.
        """
        r2 = radius * radius
        results: List[EntityRecord] = []
        with self._lock:
            with open(self._path, "rb") as f:
                for eid in range(1, self._max):
                    f.seek(eid * ENTITY_SLOT_SIZE)
                    raw = f.read(ENTITY_SLOT_SIZE)
                    if len(raw) < ENTITY_SLOT_SIZE:
                        break
                    if raw == b"\x00" * ENTITY_SLOT_SIZE:
                        continue
                    rec = EntityRecord.from_bytes(raw)
                    if rec.is_empty:
                        continue
                    dx = rec.x - x; dy = rec.y - y; dz = rec.z - z
                    if dx*dx + dy*dy + dz*dz <= r2:
                        results.append(rec)
        return results

    def __repr__(self) -> str:
        return f"EntitySidecar({self._path!r}, max={self._max})"


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile, os

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "entities.ent")
        sc = EntitySidecar(path, max_entities=256)
        print(sc)

        rec = EntityRecord(
            entity_id=1, entity_type=EntityType.PLAYER,
            flags=EntityFlags.ACTIVE | EntityFlags.VISIBLE,
            x=32.0, y=64.0, z=32.0,
            health=100.0, last_tick=42,
        )
        sc.write_entity(rec)

        read_back = sc.read_entity(1)
        assert read_back is not None
        assert read_back.x == 32.0
        assert read_back.health == 100.0
        print(f"entity_sidecar: read_entity OK — {read_back}")

        delta = sc.tick_delta(since_tick=40)
        assert len(delta) == 1
        print(f"entity_sidecar: tick_delta since 40 → {len(delta)} entities")

        nearby = sc.entities_near(32.0, 64.0, 32.0, radius=10.0)
        assert len(nearby) == 1
        print(f"entity_sidecar: entities_near → {len(nearby)} entities")

        sc.delete_entity(1)
        assert sc.read_entity(1) is None
        print("entity_sidecar: delete OK")

        print("entity_sidecar: all checks passed")