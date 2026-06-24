"""
Morton encoding/decoding for 3D spatial locality optimization.
Enables efficient Z-order curve traversal of 3D space for cache optimization
and NVMe matrix access patterns.
"""

def morton3d(x: int, y: int, z: int) -> int:
    """
    Encode 3D coordinates to Morton code (Z-order curve).
    Supports 21-bit coordinates (0-2,097,151 each dimension).
    
    Args:
        x, y, z: 3D coordinates (21-bit max each)
    
    Returns:
        64-bit Morton code encoding spatial proximity
    """
    x &= 0x1FFFFF
    y &= 0x1FFFFF
    z &= 0x1FFFFF
    return (x << 42) | (y << 21) | z


def demorton3d(code: int) -> tuple[int, int, int]:
    """
    Decode Morton code back to 3D coordinates.
    
    Args:
        code: 64-bit Morton code
    
    Returns:
        Tuple of (x, y, z) coordinates
    """
    x = (code >> 42) & 0x1FFFFF
    y = (code >> 21) & 0x1FFFFF
    z = code & 0x1FFFFF
    return (x, y, z)


def morton_neighbors_3d(code: int) -> list[int]:
    """
    Get Morton codes of 26 neighboring cells (3D neighborhood).
    Essential for prefetching and region streaming.
    
    Args:
        code: Central Morton code
    
    Returns:
        List of neighbor Morton codes
    """
    x, y, z = demorton3d(code)
    neighbors = []
    
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            for dz in [-1, 0, 1]:
                if dx == 0 and dy == 0 and dz == 0:
                    continue
                nx, ny, nz = x + dx, y + dy, z + dz
                if 0 <= nx < 0x200000 and 0 <= ny < 0x200000 and 0 <= nz < 0x200000:
                    neighbors.append(morton3d(nx, ny, nz))
    
    return neighbors
