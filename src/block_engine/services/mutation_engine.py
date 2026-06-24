"""Studio plug-in mutation gateway; routes typed in-world mutations without owning storage."""
class MutationEngine:
    def __init__(self, movement_transaction, movement_resolver=None, movement_replication=None):
        self.movement_transaction=movement_transaction; self.movement_resolver=movement_resolver; self.movement_replication=movement_replication
    def move(self, record, intent):
        if self.movement_resolver: intent=self.movement_resolver.resolve(record,intent)
        offset=self.movement_transaction.commit(intent)
        if self.movement_replication: self.movement_replication.publish_commit(intent.entity_id,offset,intent.tick)
        return offset
