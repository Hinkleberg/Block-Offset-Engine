"""Pure movement intent resolver. Physics/collision adapters may replace this policy."""
class MovementResolver:
    def resolve(self, record, intent):
        return intent
