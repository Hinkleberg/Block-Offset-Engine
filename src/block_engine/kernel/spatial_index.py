"""Direct-address spatial membership index; no database/query dependency."""
from collections import defaultdict
class SpatialIndex:
    def __init__(self): self._by_block=defaultdict(set); self._entity={}
    def locate(self, entity_id): return self._entity.get(entity_id)
    def move(self, entity_id, block_offset):
        old=self._entity.get(entity_id)
        if old is not None: self._by_block[old].discard(entity_id)
        self._entity[entity_id]=block_offset; self._by_block[block_offset].add(entity_id)
        return old, block_offset
    def entities_at(self, block_offset): return frozenset(self._by_block.get(block_offset, ()))
