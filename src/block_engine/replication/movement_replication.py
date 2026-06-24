"""Transport-agnostic publication of committed spatial transitions."""
class MovementReplication:
    def __init__(self, publish): self.publish=publish
    def publish_commit(self, entity_id, block_offset, tick):
        self.publish({'kind':'movement','entity_id':entity_id,'block_offset':block_offset,'tick':tick})
