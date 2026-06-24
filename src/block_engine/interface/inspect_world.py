"""
Block-Offset Engine ─ World Inspector
=======================================

Reads a world image and prints:
  • File header (dimensions, seed, version)
  • Block-type distribution (sampled or full scan)
  • Biome distribution
  • ASCII cross-section at the world's mid-Z plane

Usage
─────
    python -m tools.inspect_world world.img
    python -m tools.inspect_world world.img --full-scan
    python -m tools.inspect_world world.img --x 64 --z 64   # specific column
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np

from engine.world_format import (
    HEADER_SIZE, NUMPY_BLOCK_DTYPE,
    BlockType, Biome,
    unpack_header,
)

# ASCII characters for cross-section render (block_type → char)
RENDER_CHARS: dict[int, str] = {
    BlockType.AIR:       ' ',
    BlockType.STONE:     '░',
    BlockType.DIRT:      '▒',
    BlockType.GRASS:     '▓',
    BlockType.WATER:     '~',
    BlockType.SAND:      '.',
    BlockType.GRAVEL:    ':',
    BlockType.BEDROCK:   '█',
    BlockType.WOOD:      'T',
    BlockType.LEAVES:    '*',
    BlockType.SNOW:      '°',
    BlockType.ICE:       '≈',
    BlockType.COAL_ORE:  'c',
    BlockType.IRON_ORE:  'i',
    BlockType.LAVA:      '!',
}


def _open_world(path: Path):
    """Read header and return (header_dict, memmap)."""
    with open(path, 'rb') as f:
        raw_hdr = f.read(HEADER_SIZE)
    hdr = unpack_header(raw_hdr)

    mm = np.memmap(
        path,
        dtype=NUMPY_BLOCK_DTYPE,
        mode='r',
        offset=HEADER_SIZE,
        shape=(hdr['world_z'], hdr['world_y'], hdr['world_x']),
    )
    return hdr, mm


def _print_header(hdr: dict, path: Path) -> None:
    size_mb = path.stat().st_size / 1_048_576
    print("╔══════════════════════════════════════════════════╗")
    print("║        Block-Offset Engine — World Image         ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"  File     : {path}")
    print(f"  Size     : {size_mb:.1f} MB")
    print(f"  Magic    : {hdr['magic']!r}")
    print(f"  Version  : {hdr['version']}")
    print(f"  World X  : {hdr['world_x']}  (east-west)")
    print(f"  World Y  : {hdr['world_y']}  (height, bedrock→sky)")
    print(f"  World Z  : {hdr['world_z']}  (north-south)")
    print(f"  Seed     : {hdr['seed']}")
    total = hdr['world_x'] * hdr['world_y'] * hdr['world_z']
    print(f"  Blocks   : {total:,}")


def _block_distribution(mm: np.ndarray, sample: int = 500_000) -> Counter:
    """Return block-type counts from a random sample (or full scan if sample=0)."""
    total = mm.size
    if sample and total > sample:
        idx = np.random.default_rng(0).integers(0, total, sample)
        flat = mm.reshape(-1)
        types = flat['block_type'][idx]
    else:
        types = mm['block_type'].ravel()
    return Counter(int(t) for t in types)


def _biome_distribution(mm: np.ndarray) -> Counter:
    """Count biome types across the XZ surface layer."""
    # Use the top z-slice of y=midpoint as a cheap biome census
    mid_y = mm.shape[1] // 2
    biomes = mm[:, mid_y, :]['biome'].ravel()
    return Counter(int(b) for b in biomes)


def _ascii_section(mm: np.ndarray, z: int, width: int = 80) -> str:
    """
    Render a vertical XY cross-section at world z=z.
    Samples 'width' columns evenly across world_x, displays all world_y rows.
    Y is flipped so sky is at the top.
    """
    world_z, world_y, world_x = mm.shape
    z = max(0, min(z, world_z - 1))

    xs = np.linspace(0, world_x - 1, min(width, world_x), dtype=int)
    slice_data = mm[z, :, :][:, xs]   # (world_y, len(xs))

    lines = []
    for y in range(world_y - 1, -1, -1):
        row = ''.join(RENDER_CHARS.get(int(bt), '?')
                      for bt in slice_data[y]['block_type'])
        lines.append(f"{y:3d} │{row}│")
    lines.append("    └" + "─" * len(xs) + "┘")
    lines.append(f"    Cross-section at z={z}  (x: 0 → {world_x-1})")
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description='Inspect a Block-Offset Engine world image')
    p.add_argument('image',       help='Path to world.img')
    p.add_argument('--full-scan', action='store_true',
                   help='Count every block (slow for large worlds)')
    p.add_argument('--z',  type=int, default=None, help='Z-layer for cross-section')
    p.add_argument('--width', type=int, default=80, help='Width of ASCII render')
    p.add_argument('--no-render', action='store_true', help='Skip ASCII cross-section')
    args = p.parse_args(argv)

    path = Path(args.image)
    if not path.exists():
        print(f"Error: {path} not found", file=sys.stderr)
        sys.exit(1)

    hdr, mm = _open_world(path)
    _print_header(hdr, path)

    # Block distribution
    sample = 0 if args.full_scan else 500_000
    print(f"\n{'Block distribution':─<50} ({'full' if not sample else f'~{sample:,} sample'})")
    dist = _block_distribution(mm, sample)
    total_sampled = sum(dist.values())
    for bt in BlockType:
        count = dist.get(int(bt), 0)
        pct   = count / total_sampled * 100 if total_sampled else 0
        bar   = '█' * int(pct / 2)
        print(f"  {bt.name:<12} {count:>10,}  {pct:5.1f}%  {bar}")

    # Biome distribution
    print(f"\n{'Biome distribution (XZ surface sample)':─<50}")
    bdist = _biome_distribution(mm)
    total_b = sum(bdist.values())
    for bm in Biome:
        count = bdist.get(int(bm), 0)
        pct   = count / total_b * 100 if total_b else 0
        bar   = '█' * int(pct / 3)
        print(f"  {bm.name:<12} {count:>10,}  {pct:5.1f}%  {bar}")

    # ASCII cross-section
    if not args.no_render:
        z = args.z if args.z is not None else hdr['world_z'] // 2
        print(f"\n{'Cross-section':─<50} (z={z})")
        print(_ascii_section(mm, z, width=args.width))


if __name__ == '__main__':
    main()
