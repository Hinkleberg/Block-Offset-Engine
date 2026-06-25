from __future__ import annotations
from dataclasses import dataclass, field
from typing import List

@dataclass
class BlockDelta:
    offset: int
    data:   bytes

@dataclass
class EntityDelta:
    entity_id: int
    x: float
    y: float
    z: float
    metadata: bytes = b""

@dataclass
class RenderDelta:
    tick:           int
    block_deltas:   List[BlockDelta]  = field(default_factory=list)
    entity_deltas:  List[EntityDelta] = field(default_factory=list)
