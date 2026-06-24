"""
tools/run_server.py

Start everything for local testing:
- Loads or creates world.img
- Creates EntitySidecar and MutationEngine
- Starts RenderFeedServer and wires it so mutations notify clients
- Starts ai_loop task
"""
import asyncio
import os
from engine.world_gen import init_world, TILE_SIZE
from engine.entity_sidecar import EntitySidecar
from engine.mutation_engine import MutationEngine
from engine.render_feed import RenderFeedServer
from engine.ai_tick import ai_loop
from engine.lighting_propagator import propagate_light

WORLD_PATH = "world.img"
SIDECAR_PATH = "sidecar.img"
JOURNAL_PATH = "mutation.journal"

async def main():
    # create small world if not present
    if not os.path.exists(WORLD_PATH):
        init_world(WORLD_PATH, num_tiles=1024)
    sidecar = EntitySidecar(SIDECAR_PATH, max_cells=64, cache_size=8)
    mut = MutationEngine(WORLD_PATH, journal_path=JOURNAL_PATH)
    feed = RenderFeedServer()
    await feed.start()

    # helper: world read function
    def world_read(tile_idx: int):
        with open(WORLD_PATH, "rb") as f:
            f.seek(tile_idx * TILE_SIZE)
            return f.read(TILE_SIZE)

    # wrap MutationEngine.apply_mutation to notify render feed & lighting
    original_apply = mut.apply_mutation
    def wrapped_apply(tile_idx, data):
        res = original_apply(tile_idx, data)
        # notify render clients of this tile
        feed.notify_tiles_changed([tile_idx], world_read)
        # propagate lighting to neighbors (synchronous for prototype)
        propagate_light([tile_idx], mut, world_read)
        return res
    mut.apply_mutation = wrapped_apply

    # start AI
    loop = asyncio.get_event_loop()
    ai_task = loop.create_task(ai_loop(sidecar, mut, world_read, interval=0.2))

    # serve forever
    try:
        await asyncio.Event().wait()
    finally:
        ai_task.cancel()
        sidecar.close()
        mut.close()

if __name__ == "__main__":
    asyncio.run(main())
