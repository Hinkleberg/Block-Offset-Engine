"""
Simulation Bubble - Define active simulation region around player.
Only blocks within this region are actively simulated on frontend.
Enables infinite world simulation with bounded compute.
"""

from typing import List, Tuple, Set
import logging

logger = logging.getLogger(__name__)


class SimulationBubble:
    """
    Defines the region around player where simulation is active.
    Chunks outside this bubble are dormant on client (state held on backend).
    """
    
    def __init__(self, radius: int = 8):
        """
        Initialize simulation bubble.
        
        Args:
            radius: Chunk radius around player center
                   Total coverage = (2*radius + 1)^3 chunks
        """
        self.radius = radius
        self._cache: Set[Tuple[int, int, int]] = set()
    
    def chunks(self, cx: int, cy: int, cz: int) -> List[Tuple[int, int, int]]:
        """
        Get all chunks within simulation bubble.
        
        Args:
            cx, cy, cz: Center chunk coordinates
        
        Returns:
            List of (x, y, z) chunk coordinates
        """
        chunks = []
        for x in range(cx - self.radius, cx + self.radius + 1):
            for y in range(cy - self.radius, cy + self.radius + 1):
                for z in range(cz - self.radius, cz + self.radius + 1):
                    chunks.append((x, y, z))
        
        self._cache = set(chunks)
        return chunks
    
    def contains_chunk(self, cx: int, cy: int, cz: int, 
                      bubble_cx: int, bubble_cy: int, bubble_cz: int) -> bool:
        """
        Check if chunk is within bubble around center.
        
        Args:
            cx, cy, cz: Chunk to test
            bubble_cx, bubble_cy, bubble_cz: Bubble center
        
        Returns:
            True if chunk is in bubble
        """
        dx = abs(cx - bubble_cx)
        dy = abs(cy - bubble_cy)
        dz = abs(cz - bubble_cz)
        
        return dx <= self.radius and dy <= self.radius and dz <= self.radius
    
    def get_boundary_chunks(self, cx: int, cy: int, cz: int) -> List[Tuple[int, int, int]]:
        """Get chunks on the boundary of the bubble (for loading)."""
        boundary = []
        
        for x in range(cx - self.radius, cx + self.radius + 1):
            for y in range(cy - self.radius, cy + self.radius + 1):
                for z in range(cz - self.radius, cz + self.radius + 1):
                    dx = abs(x - cx)
                    dy = abs(y - cy)
                    dz = abs(z - cz)
                    
                    # On boundary if at least one dimension equals radius
                    if dx == self.radius or dy == self.radius or dz == self.radius:
                        boundary.append((x, y, z))
        
        return boundary
    
    def get_unload_chunks(self, old_cx: int, old_cy: int, old_cz: int,
                         new_cx: int, new_cy: int, new_cz: int) -> List[Tuple[int, int, int]]:
        """
        Get chunks to unload when bubble moves.
        
        Args:
            old_cx, old_cy, old_cz: Previous bubble center
            new_cx, new_cy, new_cz: New bubble center
        
        Returns:
            List of chunks that are leaving the bubble
        """
        old_chunks = set(self.chunks(old_cx, old_cy, old_cz))
        new_chunks = set(self.chunks(new_cx, new_cy, new_cz))
        
        return list(old_chunks - new_chunks)
    
    def get_load_chunks(self, old_cx: int, old_cy: int, old_cz: int,
                       new_cx: int, new_cy: int, new_cz: int) -> List[Tuple[int, int, int]]:
        """
        Get chunks to load when bubble moves.
        
        Args:
            old_cx, old_cy, old_cz: Previous bubble center
            new_cx, new_cy, new_cz: New bubble center
        
        Returns:
            List of chunks entering the bubble
        """
        old_chunks = set(self.chunks(old_cx, old_cy, old_cz))
        new_chunks = set(self.chunks(new_cx, new_cy, new_cz))
        
        return list(new_chunks - old_chunks)
    
    def bubble_volume(self) -> int:
        """Get total number of chunks in bubble."""
        side = 2 * self.radius + 1
        return side ** 3
    
    def distance_to_boundary(self, cx: int, cy: int, cz: int,
                            bubble_cx: int, bubble_cy: int, bubble_cz: int) -> int:
        """
        Get distance from chunk to nearest bubble boundary.
        Negative = outside bubble, positive = inside bubble
        """
        dx = abs(cx - bubble_cx)
        dy = abs(cy - bubble_cy)
        dz = abs(cz - bubble_cz)
        
        max_dist = max(dx, dy, dz)
        
        if max_dist <= self.radius:
            return self.radius - max_dist
        else:
            return -(max_dist - self.radius)
