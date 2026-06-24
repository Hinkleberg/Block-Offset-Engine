"""
block_layout.py
───────────────
Coordinate ↔ byte-offset arithmetic. The engine's core identity.

No storage. No SQL. No dependencies. Pure integer arithmetic.

The single expression that makes position equal to a physical byte address:

    offset(x, y, z) = (z × WORLD_X × WORLD_Y + y × WORLD_X + x) × BLOCK_SIZE

Everything else in the engine exists to protect and serve this formula.
"""

from __future__ import annotations
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterator, Tuple


# ---------------------------------------------------------------------------
# Block format constants
# ---------------------------------------------------------------------------

BLOCK_SIZE = 16          # bytes per block in the flat image
CHUNK_DIM  = 16          # blocks per chunk axis (16×16×16)
ENTITY_SLOT_SIZE = 64    # bytes per entity record in the sidecar image


class BlockType(IntEnum):
    AIR    = 0
    STONE  = 1
    DIRT   = 2
    GRASS  = 3
    WATER  = 4
    SAND   = 5
    BEDROCK = 6
    IRON_ORE = 7
    GOLD_ORE = 8


class BlockFlags(IntEnum):
    SOLID       = 1 << 0
    TRANSPARENT = 1 << 1
    MODIFIED    = 1 << 2


# ---------------------------------------------------------------------------
# Block record (in-memory representation of the 16-byte flat slot)
# ---------------------------------------------------------------------------

BLOCK_STRUCT = struct.Struct("<BBBBIxxxx")   # block_type, light, flags, reserved, metadata → 12B + 4B entity_hint pad
BLOCK_STRUCT_FULL = struct.Struct("<BBBBIxxxxxxxx")  # matches 16-byte slot exactly

# Layout: [0] block_type u8, [1] light_level u8, [2] flags u8, [3] reserved u8,
#         [4-7] metadata u32, [8-15] entity_hint u64
_PACK = struct.Struct("<BBBBIq")   # 16 bytes total

@dataclass
class Block:
    block_type:   int = BlockType.AIR
    light_level:  int = 0
    flags:        int = 0
    reserved:     int = 0
    metadata:     int = 0
    entity_hint:  int = 0          # byte offset into entity sidecar; 0 = none

    def to_bytes(self) -> bytes:
        return _PACK.pack(
            self.block_type & 0xFF,
            self.light_level & 0xFF,
            self.flags & 0xFF,
            self.reserved & 0xFF,
            self.metadata & 0xFFFFFFFF,
            self.entity_hint,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Block":
        if len(raw) != BLOCK_SIZE:
            raise ValueError(f"Block.from_bytes expects {BLOCK_SIZE} bytes, got {len(raw)}")
        bt, ll, fl, rv, md, eh = _PACK.unpack(raw)
        return cls(bt, ll, fl, rv, md, eh)

    @classmethod
    def empty(cls) -> "Block":
        return cls()


# ---------------------------------------------------------------------------
# World layout: the arithmetic core
# ---------------------------------------------------------------------------

class WorldLayout:
    """
    Immutable coordinate geometry for a flat block image.

    All methods are O(1), branch-free, integer-only.
    No I/O. No state. Safe to share across threads.
    """

    __slots__ = ("world_x", "world_y", "world_z", "total_blocks", "image_size")

    def __init__(self, world_x: int, world_y: int, world_z: int):
        # Snap to chunk boundaries
        self.world_x = _snap(world_x)
        self.world_y = _snap(world_y)
        self.world_z = _snap(world_z)
        self.total_blocks = self.world_x * self.world_y * self.world_z
        self.image_size   = self.total_blocks * BLOCK_SIZE

    # ------------------------------------------------------------------ core

    def block_offset(self, x: int, y: int, z: int) -> int:
        """
        THE formula. Position IS a byte offset.

            offset = (z × W × H  +  y × W  +  x) × BLOCK_SIZE
        """
        self._validate(x, y, z)
        return (z * self.world_x * self.world_y + y * self.world_x + x) * BLOCK_SIZE

    def offset_to_coord(self, offset: int) -> Tuple[int, int, int]:
        """Exact inverse of block_offset. Used for round-trip validation."""
        if offset % BLOCK_SIZE:
            raise ValueError("offset is not aligned to BLOCK_SIZE")
        idx = offset // BLOCK_SIZE
        x   = idx % self.world_x
        idx //= self.world_x
        y   = idx % self.world_y
        z   = idx // self.world_y
        return x, y, z

    # ---------------------------------------------------------------- chunks

    def chunk_offset(self, cx: int, cy: int, cz: int) -> int:
        """Byte offset of a chunk's first block. Chunks are 16×16×16 blocks."""
        return self.block_offset(cx * CHUNK_DIM, cy * CHUNK_DIM, cz * CHUNK_DIM)

    def chunk_coords(self, x: int, y: int, z: int) -> Tuple[int, int, int]:
        return x // CHUNK_DIM, y // CHUNK_DIM, z // CHUNK_DIM

    # --------------------------------------------------------------- player

    def player_offset(self, px: float, py: float, pz: float) -> int:
        """Floating-point player position → byte offset of occupied block."""
        return self.block_offset(int(px), int(py), int(pz))

    # ----------------------------------------------------------------- range

    def blocks_in_range(
        self, cx: int, cy: int, cz: int, radius: int
    ) -> Iterator[int]:
        """
        All byte offsets within a cubic radius of (cx, cy, cz).
        Used by the render feed to determine the view-frustum block set.
        """
        x0 = max(0, cx - radius); x1 = min(self.world_x - 1, cx + radius)
        y0 = max(0, cy - radius); y1 = min(self.world_y - 1, cy + radius)
        z0 = max(0, cz - radius); z1 = min(self.world_z - 1, cz + radius)
        for z in range(z0, z1 + 1):
            for y in range(y0, y1 + 1):
                for x in range(x0, x1 + 1):
                    yield self.block_offset(x, y, z)

    def is_valid(self, x: int, y: int, z: int) -> bool:
        return 0 <= x < self.world_x and 0 <= y < self.world_y and 0 <= z < self.world_z

    def _validate(self, x: int, y: int, z: int) -> None:
        if not self.is_valid(x, y, z):
            raise ValueError(
                f"Coordinate ({x},{y},{z}) out of bounds "
                f"({self.world_x}×{self.world_y}×{self.world_z})"
            )

    def __repr__(self) -> str:
        mb = self.image_size / (1024 * 1024)
        return (
            f"WorldLayout({self.world_x}×{self.world_y}×{self.world_z} blocks, "
            f"image={mb:.1f} MB)"
        )


def _snap(n: int) -> int:
    """Round up to nearest CHUNK_DIM multiple."""
    return ((n + CHUNK_DIM - 1) // CHUNK_DIM) * CHUNK_DIM


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    layout = WorldLayout(64, 64, 64)
    print(layout)

    for coord in [(0,0,0), (1,0,0), (0,1,0), (0,0,1), (10,64,10), (63,63,63)]:
        x, y, z = coord
        if not layout.is_valid(x, y, z):
            continue
        off = layout.block_offset(x, y, z)
        rt  = layout.offset_to_coord(off)
        assert rt == (x, y, z), f"Round-trip failed: {coord} → {off} → {rt}"
    print("block_layout: all round-trip checks passed")

    blk = Block(block_type=BlockType.GRASS, light_level=15, flags=BlockFlags.SOLID, metadata=42)
    raw = blk.to_bytes()
    assert len(raw) == BLOCK_SIZE
    blk2 = Block.from_bytes(raw)
    assert blk2.block_type == BlockType.GRASS
    assert blk2.light_level == 15
    print("block_layout: Block pack/unpack passed")