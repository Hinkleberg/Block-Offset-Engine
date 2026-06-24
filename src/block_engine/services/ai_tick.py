"""
engine/ai_tick.py

A very small AI tick that reads entity sidecar cells, chooses an entity, and moves it by manipulating its record.
When moving across tile/region boundaries it creates a mutation via MutationEngine.
"""
import asyncio
import random
import struct

TILE_SIZE = 512

async def ai_loop(sidecar, mutation_engine, world_reader, interval=0.5):
    tick = 0
    while True:
        tick += 1
        # pick a random cell and slot
        cell_idx = random.randrange(0, min(64, sidecar.max_cells))
        slot = random.randrange(0, sidecar.ENTITIES_PER_CELL if hasattr(sidecar, "ENTITIES_PER_CELL") else 8)
        try:
            ent = sidecar.get_entity(cell_idx, slot)
            # interpret first 8 bytes as a little-endian tile index for prototype
            tile_idx = int.from_bytes(ent.data[:8], "little")
            # move +/-1 tile randomly
            new_tile = max(0, tile_idx + random.choice([-1, 0, 1]))
            # write back
            new_bytes = new_tile.to_bytes(8, "little") + ent.data[8:]
            ent = type(ent)(new_bytes)
            sidecar.set_entity(cell_idx, slot, ent)
            # also touch the world tile to create a visible mutation (write tile header)
            tile_payload = (new_tile.to_bytes(8, "little") + b"\x00" * (TILE_SIZE - 8))
            mutation_engine.apply_mutation(new_tile, tile_payload)
            # notify any listeners via mutation engine callbacks (striped in run_server)
        except Exception as e:
            print("ai error", e)
        await asyncio.sleep(interval)
