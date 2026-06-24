"""
world_gen.py
────────────
Crash-resumable world generator. All writes go through ResilientStore.
Kill mid-generation, restart, and it continues from where it stopped.

Terrain uses a deterministic SHA-256-based noise function — no external
dependencies. Same seed, same world. Suitable for crash-recovery
validation and regression testing.

Terrain layers (bottom to top):
  y < 2              → BEDROCK
  below surface − 4  → STONE (with seeded ore veins: iron, gold)
  surface − 4 to     → DIRT
  surface            → GRASS (or SAND at/below sea level)
  above surface,
  below sea level    → WATER
  above surface      → AIR

Usage:
  python world_gen.py --size 64 --seed 42 \
      --out world.img --array-b world_render.img
"""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys
import time

# Add core dir to path when run directly
sys.path.insert(0, os.path.dirname(__file__))

from block_layout import Block, BlockType, BlockFlags, WorldLayout, BLOCK_SIZE
from flat_store import FlatStore
from render_store import RenderStore
from resilient_store import ResilientStore


# ---------------------------------------------------------------------------
# Deterministic noise (SHA-256 based, no deps)
# ---------------------------------------------------------------------------

def _sha_noise(seed: int, x: int, z: int) -> float:
    """Returns a deterministic float in [0, 1) for (seed, x, z)."""
    key  = struct.pack("<qqq", seed, x, z)
    dg   = hashlib.sha256(key).digest()
    val  = struct.unpack_from("<Q", dg)[0]
    return val / (2**64)


def _surface_height(seed: int, x: int, z: int, world_y: int, sea_level: int) -> int:
    n = _sha_noise(seed, x, z)
    # Range: sea_level - 4  to  sea_level + 8
    return sea_level - 4 + int(n * 12)


def _is_ore(seed: int, x: int, y: int, z: int, ore_type: int) -> bool:
    key = struct.pack("<qqqqq", seed, x, y, z, ore_type)
    dg  = hashlib.sha256(key).digest()
    v   = struct.unpack_from("<Q", dg)[0] / (2**64)
    return v < 0.03   # 3% chance per eligible block


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate(
    layout:    WorldLayout,
    rs:        ResilientStore,
    seed:      int = 42,
    *,
    verbose:   bool = True,
) -> None:
    sea_level = layout.world_y // 4

    total  = layout.world_x * layout.world_z
    done   = 0
    t0     = time.time()

    for z in range(layout.world_z):
        for x in range(layout.world_x):
            surface = _surface_height(seed, x, z, layout.world_y, sea_level)
            surface = max(2, min(layout.world_y - 1, surface))

            for y in range(layout.world_y):
                if y < 2:
                    btype = BlockType.BEDROCK
                    flags = BlockFlags.SOLID
                elif y < surface - 4:
                    if _is_ore(seed, x, y, z, 1):
                        btype = BlockType.IRON_ORE
                    elif _is_ore(seed, x, y, z, 2):
                        btype = BlockType.GOLD_ORE
                    else:
                        btype = BlockType.STONE
                    flags = BlockFlags.SOLID
                elif y < surface:
                    btype = BlockType.DIRT
                    flags = BlockFlags.SOLID
                elif y == surface:
                    btype = BlockType.SAND if surface <= sea_level else BlockType.GRASS
                    flags = BlockFlags.SOLID
                elif y <= sea_level:
                    btype = BlockType.WATER
                    flags = BlockFlags.TRANSPARENT
                else:
                    btype = BlockType.AIR
                    flags = 0

                blk    = Block(block_type=btype, flags=flags, light_level=max(0, 15 - (surface - y)))
                offset = layout.block_offset(x, y, z)
                rs.write_block(offset, blk.to_bytes())

            done += 1
            if verbose and done % 256 == 0:
                pct = done / total * 100
                elapsed = time.time() - t0
                print(f"\r  {pct:.1f}%  ({done}/{total} columns)  {elapsed:.1f}s", end="", flush=True)

    if verbose:
        print(f"\r  100.0%  ({total}/{total} columns)  {time.time()-t0:.1f}s")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Block-Image Engine world generator")
    ap.add_argument("--size",    type=int, default=64,           help="World size (cubic, snapped to 16)")
    ap.add_argument("--seed",    type=int, default=42,           help="Terrain seed")
    ap.add_argument("--out",     default="world.img",            help="Array A image path")
    ap.add_argument("--array-b", default="world_render.img",     help="Array B image path")
    ap.add_argument("--journal", default="world.jrn",            help="Journal path")
    args = ap.parse_args()

    layout = WorldLayout(args.size, args.size, args.size)
    print(f"Generating {layout}")

    store_a = FlatStore(args.out, layout)
    store_b = RenderStore(args.array_b, layout, primary_fallback=store_a.read_block)
    rs      = ResilientStore(store_a, journal_path=args.journal)
    rs.register_mirror(store_b.enqueue_forward_sync)

    generate(layout, rs, seed=args.seed)

    store_b.flush()
    store_b.stop()

    print(f"Done. Array A write_seq={rs.write_seq}, Array B mirror_seq={store_b.mirror_write_seq}")
    print(rs.health_report())


if __name__ == "__main__":
    main()