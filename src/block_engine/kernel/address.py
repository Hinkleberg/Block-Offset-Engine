"""
BlockAddress system for safe 3D coordinate handling and validation.
Enforces coordinate bounds and provides efficient spatial lookups.
"""

from dataclasses import dataclass
from typing import Optional
from morton import morton3d, demorton3d

# Maximum 21-bit coordinate value
MAX_COORD = 0x1FFFFF  # 2,097,151


@dataclass(frozen=True)
class BlockAddress:
    """Immutable 3D block address with spatial integrity checks."""
    x: int
    y: int
    z: int
    
    def __post_init__(self):
        """Validate coordinate ranges."""
        if not (0 <= self.x <= MAX_COORD):
            raise ValueError(f"X coordinate {self.x} out of valid range [0, {MAX_COORD}]")
        if not (0 <= self.y <= MAX_COORD):
            raise ValueError(f"Y coordinate {self.y} out of valid range [0, {MAX_COORD}]")
        if not (0 <= self.z <= MAX_COORD):
            raise ValueError(f"Z coordinate {self.z} out of valid range [0, {MAX_COORD}]")
    
    @property
    def key(self) -> int:
        """Get Morton code for this address (Z-order curve locality)."""
        return morton3d(self.x, self.y, self.z)
    
    @classmethod
    def from_morton(cls, code: int) -> 'BlockAddress':
        """Reconstruct address from Morton code."""
        x, y, z = demorton3d(code)
        return cls(x, y, z)
    
    def distance_to(self, other: 'BlockAddress') -> float:
        """Euclidean distance to another address."""
        dx = self.x - other.x
        dy = self.y - other.y
        dz = self.z - other.z
        return (dx*dx + dy*dy + dz*dz) ** 0.5
    
    def in_region(self, region_min: 'BlockAddress', region_max: 'BlockAddress') -> bool:
        """Check if this address is within a region bounds."""
        return (region_min.x <= self.x <= region_max.x and
                region_min.y <= self.y <= region_max.y and
                region_min.z <= self.z <= region_max.z)
    
    def __repr__(self) -> str:
        return f"BlockAddress({self.x}, {self.y}, {self.z})"
