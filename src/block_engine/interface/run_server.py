"""
run_server.py
─────────────
Server loop: wires mutation engine, render feed, entity sidecar,
and health monitor into a single running process. No SQL.

Usage:
  python run_server.py --array-a world.img --array-b world_render.img \
                       --sidecar entities.ent --size 64 --duration 30
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from block_layout import Block, BlockType, BlockFlags, WorldLayout, BLOCK_SIZE
from flat_store import FlatStore
from render_store import RenderStore
from resilient_store import ResilientStore
from entity_sidecar import EntitySidecar, EntityRecord, EntityType, EntityFlags
from render_feed import RenderFeedServer
from mirror_health_monitor import MirrorHealthMonitor, MirrorStatus


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--array-a",  default="world.img")
    ap.add_argument("--array-b",  default="world_render.img")
    ap.add_argument("--sidecar",  default="entities.ent")
    ap.add_argument("--journal",  default="world.jrn")
    ap.add_argument("--size",     type=int, default=64)
    ap.add_argument("--duration", type=int, default=30,  help="Run seconds (0=forever)")
    args = ap.parse_args()

    layout  = WorldLayout(args.size, args.size, args.size)

    store_a = FlatStore(args.array_a, layout)
    store_b = RenderStore(args.array_b, layout, primary_fallback=store_a.read_block)
    rs      = ResilientStore(store_a, journal_path=args.journal)
    rs.register_mirror(store_b.enqueue_forward_sync)

    sidecar = EntitySidecar(args.sidecar)

    # Synthetic player entity
    player = EntityRecord(
        entity_id=1, entity_type=EntityType.PLAYER,
        flags=EntityFlags.ACTIVE | EntityFlags.VISIBLE,
        x=32.0, y=64.0, z=32.0, health=100.0, last_tick=0,
    )
    sidecar.write_entity(player)

    # Render feed
    received: list = []
    def on_delta(delta: RenderDelta) -> None:
        received.append(delta)

    feed = RenderFeed(layout, store_b, sidecar, tick_rate_hz=20)
    feed.connect_client(
        client_id=1, send_cb=on_delta,
        view_radius=16, initial_x=32.0, initial_y=64.0, initial_z=32.0,
    )
    feed.start()

    # Mirror health monitor
    def on_status(name: str, status: MirrorStatus) -> None:
        print(f"  [mirror] {name} → {status.name}")

    monitor = MirrorHealthMonitor(
        primary=rs,
        mirrors={"render_b": store_b},
        lag_warn_threshold=100,
        lag_degraded_threshold=500,
        on_status_change=on_status,
    )
    monitor.start()

    print(f"Server running — {layout}")
    t0      = time.time()
    tick    = 0
    radius  = layout.world_x // 4

    try:
        while True:
            elapsed = time.time() - t0
            if args.duration > 0 and elapsed > args.duration:
                break

            tick += 1

            # Move synthetic player in a circle
            angle = elapsed * 0.5
            px = layout.world_x / 2 + math.cos(angle) * radius
            pz = layout.world_z / 2 + math.sin(angle) * radius
            py = 64.0
            feed.update_player_position(1, px, py, pz)
            player.x = px; player.y = py; player.z = pz
            player.last_tick = tick
            sidecar.write_entity(player)

            # Simulate block mutation every 5 ticks
            if tick % 5 == 0:
                bx = int(px) % layout.world_x
                by = max(0, int(py) - 1)
                bz = int(pz) % layout.world_z
                offset = layout.block_offset(bx, by, bz)
                blk = Block(block_type=BlockType.AIR, flags=0)
                rs.write_block(offset, blk.to_bytes())

            # Health report every 2s
            if tick % 40 == 0:
                report = rs.health_report()
                print(
                    f"[t={elapsed:.1f}s] tick={tick} "
                    f"seq_A={rs.write_seq} seq_B={store_b.mirror_write_seq} "
                    f"deltas_sent={len(received)} "
                    f"health={report['health']}"
                )

            time.sleep(0.05)   # ~20 Hz server tick

    except KeyboardInterrupt:
        print("\nShutting down…")
    finally:
        feed.stop()
        monitor.stop()
        store_b.flush()
        store_b.stop()
        print(f"Final: seq_A={rs.write_seq} seq_B={store_b.mirror_write_seq} "
              f"deltas_sent={len(received)}")


if __name__ == "__main__":
    main()