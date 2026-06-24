"""Journaled authoritative movement transaction over direct block addresses."""
from dataclasses import dataclass
@dataclass(frozen=True)
class MoveIntent:
    entity_id:int; x:float; y:float; z:float; tick:int; expected_revision:int=0
class MovementTransaction:
    def __init__(self, layout, sidecar, spatial_index, journal=None): self.layout,self.sidecar,self.index,self.journal=layout,sidecar,spatial_index,journal
    def commit(self, intent):
        rec=self.sidecar.read_entity(intent.entity_id)
        if rec is None: raise KeyError(intent.entity_id)
        old=self.index.locate(intent.entity_id)
        target=self.layout.player_offset(intent.x,intent.y,intent.z)
        if self.journal: self.journal.append(('MOVE_PREPARE',intent.entity_id,old,target,intent.tick))
        rec.x,rec.y,rec.z,rec.last_tick=intent.x,intent.y,intent.z,intent.tick
        self.sidecar.write_entity(rec); self.index.move(intent.entity_id,target)
        if self.journal: self.journal.append(('MOVE_COMMIT',intent.entity_id,target,intent.tick))
        return target
