"""
example_dual_array.py
─────────────────────
Demonstrates the dual-array setup in isolation.
Writes blocks through Array A, reads them back from Array B,
prints the health report. No SQL.

Run:
  python example_dual_array.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

from block_layout import Block, BlockType, BlockFlags, WorldLayout
from flat_store import FlatStore
from render_store import RenderStore
from resilient_store import ResilientStore
from mirror_health_monitor import MirrorHealthMonitor


def main() -> None:
    layout = WorldLayout(32, 32, 32)
    print(f"Layout: {layout}")

    with tempfile.TemporaryDirectory() as tmp:
        path_a = os.path.join(tmp, "array_a.img")
        path_b = os.path.join(tmp, "array_b.img")
        jrn    = os.path.join(tmp, "world.jrn")

        store_a = FlatStore(path_a, layout)
        store_b = RenderStore(path_b, layout, primary_fallback=store_a.read_block)
        rs      = ResilientStore(store_a, journal_path=jrn)
        rs.register_mirror(store_b.enqueue_forward_sync)

        monitor = MirrorHealthMonitor(
            primary=rs,
            mirrors={"render_b": store_b},
            lag_warn_threshold=50,
        )
        monitor.start()

        # Write a set of blocks through Array A
        blocks_written = []
        for i in range(64):
            x = i % layout.world_x
            y = (i // layout.world_x) % layout.world_y
            z = 0
            offset = layout.block_offset(x, y, z)
            blk    = Block(block_type=BlockType.STONE, light_level=i % 16,
                           flags=BlockFlags.SOLID, metadata=i)
            rs.write_block(offset, blk.to_bytes())
            blocks_written.append((offset, blk))

        print(f"Wrote {len(blocks_written)} blocks through Array A")

        # Flush Array B
        store_b.flush()
        print(f"Array B mirror_seq={store_b.mirror_write_seq}")

        # Read all written blocks from Array B
        mismatches = 0
        for offset, original in blocks_written:
            data = store_b.read_block(offset)
            readback = Block.from_bytes(data)
            if readback.metadata != original.metadata:
                mismatches += 1

        print(f"Read-back from Array B: {len(blocks_written)} blocks, {mismatches} mismatches")
        assert mismatches == 0

        # Health report
        print(monitor.report())
        print(rs.health_report())

        # Coordinate round-trip
        failures = 0
        for z in range(layout.world_z):
            for y in range(layout.world_y):
                for x in range(layout.world_x):
                    off = layout.block_offset(x, y, z)
                    rt  = layout.offset_to_coord(off)
                    if rt != (x, y, z):
                        failures += 1
        print(f"Coordinate round-trip: {failures} failures (expected 0)")
        assert failures == 0

        monitor.stop()
        store_b.stop()

    print("example_dual_array: all checks passed ✓")


if __name__ == "__main__":
    main()