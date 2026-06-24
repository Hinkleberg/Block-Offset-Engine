"""
engine/lighting_propagator.py

Tiny lighting propagator: when tile X changes, recompute a trivial 'light' value on neighboring tiles.
This prototype stores a 1-byte light value in the last byte of each tile.
"""
from typing import Callable, List

def propagate_light(changed_tiles: List[int], mutation_engine, world_read):
    neighbors = set()
    for t in changed_tiles:
        for d in (-1, 1):
            n = t + d
            if n >= 0:
                neighbors.add(n)
    mutations = []
    for n in neighbors:
        tile = bytearray(world_read(n))
        # simple rule: light = (tile_index % 256) ^ 0x80  (demo)
        light_val = (n % 256) ^ 0x80
        tile[-1] = light_val
        mutation_engine.apply_mutation(n, bytes(tile))
        mutations.append(n)
    return mutations
