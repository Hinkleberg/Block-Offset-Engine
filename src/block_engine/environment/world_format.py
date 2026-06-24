"""
Block-Offset Engine ─ World Image Format
=========================================

The world is a single flat binary file.  No asset database, no filesystem
hierarchy, no streaming middleware.  Every block has a deterministic byte
address derived solely from its (x, y, z) coordinate:

    offset(x, y, z) = HEADER_SIZE
                      + (z * world_x * world_y + y * world_x + x) * BLOCK_SIZE

Layout is ZYX-major (Z outermost → X innermost) so a vertical column
(fixed x, z — varying y) is a contiguous run of bytes.  Reading the full
column from bedrock to sky is a single sequential read of world_y * BLOCK_SIZE
bytes.  No indirection.  No lookup table.  Just arithmetic.

File layout
───────────
  Bytes  0 .. 63          64-byte image header  (HEADER_STRUCT)
  Bytes 64 .. EOF         flat block array       (BLOCK_SIZE bytes each)

Block record — 16 bytes, little-endian, no padding
───────────────────────────────────────────────────
  Byte  0      block_type   uint8   see BlockType enum
  Byte  1      light_level  uint8   high nibble = sky light (0-15)
                                    low  nibble = block light (0-15)
  Byte  2      biome        uint8   see Biome enum
  Byte  3      flags        uint8   BlockFlags bitmask
  Bytes 4-5    elevation    int16   surface Y for this column (world units)
  Byte  6      temperature  int8    climate temperature (-128 .. 127)
  Byte  7      humidity     uint8   climate humidity (0 = arid, 255 = saturated)
  Bytes 8-11   entity_id    uint32  0 = no entity; index into entity sidecar image
  Bytes 12-15  reserved     uint32  future use / cache-line alignment

Header — 64 bytes, little-endian
──────────────────────────────────
  Bytes  0-3    magic       4s      b'BOEI'  (Block Offset Engine Image)
  Bytes  4-7    version     uint32
  Bytes  8-11   world_x     uint32  width  (east-west)
  Bytes 12-15   world_y     uint32  height (bedrock=0 → sky=world_y-1)
  Bytes 16-19   world_z     uint32  depth  (north-south)
  Bytes 20-23   seed        uint32  generation seed
  Bytes 24-27   flags       uint32  world-level flags (reserved)
  Bytes 28-63   reserved    36x     padding, zeroed
"""

from __future__ import annotations

import struct
from enum import IntEnum

import numpy as np


# ── File-level constants ───────────────────────────────────────────────────────

MAGIC          = b'BOEI'
FORMAT_VERSION = 1
BLOCK_SIZE     = 16   # bytes per block record — must stay power-of-2
HEADER_SIZE    = 64   # bytes reserved at the start of every image file


# ── Struct packers (little-endian, explicit layout — no implicit padding) ─────

BLOCK_STRUCT  = struct.Struct('<BBBBhbBII')         # 1+1+1+1+2+1+1+4+4 = 16
HEADER_STRUCT = struct.Struct('<4sIIIIII36x')       # 4+4+4+4+4+4+4+36  = 64

assert BLOCK_STRUCT.size  == BLOCK_SIZE,  "BLOCK_STRUCT must be exactly 16 bytes"
assert HEADER_STRUCT.size == HEADER_SIZE, "HEADER_STRUCT must be exactly 64 bytes"


# ── Numpy dtype — mirrors BLOCK_STRUCT field-for-field ────────────────────────
# Using explicit little-endian specifiers so the dtype is portable across
# architectures.  itemsize == BLOCK_SIZE is asserted below.

NUMPY_BLOCK_DTYPE = np.dtype([
    ('block_type',  np.uint8),
    ('light_level', np.uint8),
    ('biome',       np.uint8),
    ('flags',       np.uint8),
    ('elevation',   '<i2'),
    ('temperature', np.int8),
    ('humidity',    np.uint8),
    ('entity_id',   '<u4'),
    ('reserved',    '<u4'),
])

assert NUMPY_BLOCK_DTYPE.itemsize == BLOCK_SIZE, \
    f"NUMPY_BLOCK_DTYPE.itemsize={NUMPY_BLOCK_DTYPE.itemsize}, expected {BLOCK_SIZE}"


# ── Block types ────────────────────────────────────────────────────────────────

class BlockType(IntEnum):
    AIR       = 0
    STONE     = 1
    DIRT      = 2
    GRASS     = 3
    WATER     = 4
    SAND      = 5
    GRAVEL    = 6
    BEDROCK   = 7
    WOOD      = 8
    LEAVES    = 9
    SNOW      = 10
    ICE       = 11
    COAL_ORE  = 12
    IRON_ORE  = 13
    LAVA      = 14


# ── Biomes ─────────────────────────────────────────────────────────────────────

class Biome(IntEnum):
    PLAINS    = 0
    FOREST    = 1
    DESERT    = 2
    TUNDRA    = 3
    OCEAN     = 4
    MOUNTAINS = 5
    SWAMP     = 6


# ── Block flags (bitmask) ──────────────────────────────────────────────────────

class BlockFlags(IntEnum):
    SOLID       = 0b00000001   # blocks movement
    TRANSPARENT = 0b00000010   # light passes through
    LIQUID      = 0b00000100   # fluid behaviour
    INTERACTIVE = 0b00001000   # player can interact
    SURFACE     = 0b00010000   # topmost solid block in column


# ── Per-type default flags ─────────────────────────────────────────────────────

BLOCK_FLAGS_MAP: dict[int, int] = {
    BlockType.AIR:      BlockFlags.TRANSPARENT,
    BlockType.STONE:    BlockFlags.SOLID,
    BlockType.DIRT:     BlockFlags.SOLID,
    BlockType.GRASS:    BlockFlags.SOLID,
    BlockType.WATER:    BlockFlags.LIQUID  | BlockFlags.TRANSPARENT,
    BlockType.SAND:     BlockFlags.SOLID,
    BlockType.GRAVEL:   BlockFlags.SOLID,
    BlockType.BEDROCK:  BlockFlags.SOLID,
    BlockType.WOOD:     BlockFlags.SOLID,
    BlockType.LEAVES:   BlockFlags.SOLID   | BlockFlags.TRANSPARENT,
    BlockType.SNOW:     BlockFlags.SOLID,
    BlockType.ICE:      BlockFlags.SOLID   | BlockFlags.TRANSPARENT,
    BlockType.COAL_ORE: BlockFlags.SOLID,
    BlockType.IRON_ORE: BlockFlags.SOLID,
    BlockType.LAVA:     BlockFlags.LIQUID,
}

# Lookup table indexed by block_type integer → flags byte
# Shape: (256,)  — all undefined block types default to 0 (no flags)
BLOCK_FLAGS_LUT: np.ndarray = np.zeros(256, dtype=np.uint8)
for _bt, _fl in BLOCK_FLAGS_MAP.items():
    BLOCK_FLAGS_LUT[int(_bt)] = int(_fl)


# ── Core addressing formula ────────────────────────────────────────────────────

def block_offset(x: int, y: int, z: int, world_x: int, world_y: int) -> int:
    """
    Return the byte offset of block (x, y, z) within the image file.

    This is the central primitive of the engine: player position in world
    space maps *directly* to a byte address on storage.  Moving through
    the world is physically equivalent to moving a read head across drives.

    Layout:  ZYX-major  →  blocks[z][y][x]  is contiguous across x.
    The vertical column (x, *, z) occupies bytes at stride BLOCK_SIZE * world_x.
    """
    return HEADER_SIZE + (z * world_x * world_y + y * world_x + x) * BLOCK_SIZE


def image_size(world_x: int, world_y: int, world_z: int) -> int:
    """Total byte size of a world image file including the header."""
    return HEADER_SIZE + world_x * world_y * world_z * BLOCK_SIZE


# ── Packing helpers ────────────────────────────────────────────────────────────

def pack_block(
    block_type:  int = BlockType.AIR,
    light_level: int = 0,
    biome:       int = Biome.PLAINS,
    flags:       int = 0,
    elevation:   int = 0,
    temperature: int = 20,
    humidity:    int = 128,
    entity_id:   int = 0,
) -> bytes:
    """Pack one block record into 16 raw bytes."""
    return BLOCK_STRUCT.pack(
        block_type, light_level, biome, flags,
        elevation, temperature, humidity, entity_id, 0,
    )


def unpack_block(raw: bytes) -> dict:
    """Unpack 16 raw bytes into a named dict."""
    (bt, ll, biome, flags, elev, temp, humid, eid, _) = BLOCK_STRUCT.unpack(raw)
    return {
        'block_type':   bt,
        'sky_light':    (ll >> 4) & 0xF,
        'block_light':  ll & 0xF,
        'biome':        biome,
        'flags':        flags,
        'elevation':    elev,
        'temperature':  temp,
        'humidity':     humid,
        'entity_id':    eid,
    }


def pack_header(
    world_x: int,
    world_y: int,
    world_z: int,
    seed:    int,
    flags:   int = 0,
) -> bytes:
    """Pack the 64-byte image file header."""
    return HEADER_STRUCT.pack(
        MAGIC, FORMAT_VERSION,
        world_x, world_y, world_z,
        seed & 0xFFFF_FFFF,
        flags,
    )


def unpack_header(raw: bytes) -> dict:
    """Unpack the first 64 bytes of an image file."""
    magic, version, wx, wy, wz, seed, flags = HEADER_STRUCT.unpack(raw[:HEADER_SIZE])
    if magic != MAGIC:
        raise ValueError(f"Bad magic: {magic!r} (expected {MAGIC!r})")
    return {
        'magic':   magic,
        'version': version,
        'world_x': wx,
        'world_y': wy,
        'world_z': wz,
        'seed':    seed,
        'flags':   flags,
    }
