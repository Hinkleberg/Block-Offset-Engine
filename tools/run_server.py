"""
tools/run_server.py
Start everything for local testing:
- Creates WorldLayout, FlatStore, ResilientStore
- Generates world if not present
- Creates EntitySidecar, SpatialIndex, MovementTransaction, MutationEngine
- Starts RenderFeedServer
- Starts ai_loop task
"""
import asyncio
import os
from environment.block_layout import WorldLayout
from authority.flat_store import FlatStore
from authority.resilient_store import ResilientStore
from environment.world_gen import generate
from services.ai_tick import TILE_SIZE, ai_loop
from kernel.entity_sidecar import EntitySidecar
from kernel.spatial_index import SpatialIndex
from kernel.movement_transaction import MovementTransaction
from services.mutation_engine import MutationEngine
from interface.render_feed import RenderFeedServer
from environment.lighting_propagator import propagate_light

WORLD_PATH = "world.img"
SIDECAR_PATH = "sidecar.img"
JOURNAL_PATH = "mutation.journal"

async def main():
    layout = WorldLayout(128, 128, 128)
    flat = FlatStore(WORLD_PATH, layout)
    rs = ResilientStore(local_store=flat, journal_path=JOURNAL_PATH)

    if not os.path.exists(WORLD_PATH):
        generate(layout, rs)

    sidecar = EntitySidecar(SIDECAR_PATH, max_entities=64)
    spatial_index = SpatialIndex()
    movement_transaction = MovementTransaction(layout, sidecar, spatial_index, journal=JOURNAL_PATH)
    mut = MutationEngine(movement_transaction)
    feed = RenderFeedServer()
    await feed.start()

    def world_read(tile_idx: int):
        with open(WORLD_PATH, "rb") as f:
            f.seek(tile_idx * TILE_SIZE)
            return f.read(TILE_SIZE)

    loop = asyncio.get_event_loop()
    ai_task = loop.create_task(ai_loop(sidecar, mut, world_read, interval=0.2))

    try:
        await asyncio.Event().wait()
    finally:
        ai_task.cancel()
        sidecar.close()

if __name__ == "__main__":
    asyncio.run(main())
