"""Block-Offset-Engine: Python PoC

Direct byte-offset mapping for game world simulation.
Position maps directly to byte offset in a flat world file.
No abstractions, no databases—just position → offset → block read/write.
"""

import mmap
import struct
import os
from typing import Optional, Tuple


class OffsetConfig:
    """World configuration: total addressable space and block size."""

    def __init__(self, total_size: int, block_size: int):
        if total_size <= 0:
            raise ValueError("total_size must be > 0")
        if block_size <= 0:
            raise ValueError("block_size must be > 0")
        if total_size % block_size != 0:
            raise ValueError("total_size must be aligned to block_size")

        self.total_size = total_size
        self.block_size = block_size


class OffsetResult:
    """Result of offset calculation."""

    def __init__(self, offset: int, valid: bool):
        self.offset = offset
        self.valid = valid


def calculate_offset(config: OffsetConfig, position: int) -> OffsetResult:
    """Calculate byte offset from world position.

    Direct mapping: position IS the byte offset.
    Validates bounds.

    Args:
        config: World configuration
        position: Player position (0-indexed)

    Returns:
        OffsetResult with offset and validity
    """
    if position < 0 or position >= config.total_size:
        return OffsetResult(0, False)

    return OffsetResult(position, True)


def is_aligned(config: OffsetConfig, offset: int) -> bool:
    """Check if offset is aligned to block boundary."""
    return offset % config.block_size == 0


class WorldEngine:
    """High-level world storage engine using offset mapping.

    Maps player position to byte offset in world file.
    Supports direct read/write at any position.
    """

    def __init__(self, world_file: str, config: OffsetConfig):
        """Initialize engine with world file and configuration.

        Args:
            world_file: Path to world storage file
            config: OffsetConfig with world dimensions
        """
        self.world_file = world_file
        self.config = config
        self.mm: Optional[mmap.mmap] = None
        self._ensure_world_file()

    def _ensure_world_file(self) -> None:
        """Create or verify world file exists and is correct size."""
        if os.path.exists(self.world_file):
            file_size = os.path.getsize(self.world_file)
            if file_size != self.config.total_size:
                raise ValueError(
                    f"World file size {file_size} != config size {self.config.total_size}"
                )
        else:
            # Create world file with zeros
            with open(self.world_file, "wb") as f:
                f.write(b"\x00" * self.config.total_size)

    def open(self) -> None:
        """Open world file and create memory map."""
        if self.mm is not None:
            return

        f = open(self.world_file, "r+b")
        self.mm = mmap.mmap(f.fileno(), self.config.total_size)

    def close(self) -> None:
        """Close memory map and file."""
        if self.mm is not None:
            self.mm.close()
            self.mm = None

    def read_at_position(self, position: int, size: int) -> Optional[bytes]:
        """Read block from world at player position.

        Args:
            position: Player position (maps to byte offset)
            size: Number of bytes to read

        Returns:
            Bytes read, or None if out of bounds
        """
        if self.mm is None:
            raise RuntimeError("Engine not open. Call open() first.")

        result = calculate_offset(self.config, position)
        if not result.valid:
            return None

        # Bounds check: position + size must not exceed world
        if result.offset + size > self.config.total_size:
            return None

        self.mm.seek(result.offset)
        return self.mm.read(size)

    def write_at_position(self, position: int, data: bytes) -> bool:
        """Write block to world at player position.

        Args:
            position: Player position (maps to byte offset)
            data: Bytes to write

        Returns:
            True on success, False if out of bounds
        """
        if self.mm is None:
            raise RuntimeError("Engine not open. Call open() first.")

        result = calculate_offset(self.config, position)
        if not result.valid:
            return False

        # Bounds check: position + len(data) must not exceed world
        if result.offset + len(data) > self.config.total_size:
            return False

        self.mm.seek(result.offset)
        self.mm.write(data)
        return True

    def flush(self) -> None:
        """Flush changes to disk."""
        if self.mm is not None:
            self.mm.flush()


class PlayerPosition:
    """Represents a player in the world."""

    def __init__(self, x: int, y: int, z: int):
        self.x = x
        self.y = y
        self.z = z

    def to_offset(self, world_size_x: int, world_size_y: int) -> int:
        """Convert 3D position to linear byte offset.

        Uses row-major ordering: offset = z*sx*sy + y*sx + x
        """
        return (self.z * world_size_x * world_size_y) + (self.y * world_size_x) + self.x

    def __repr__(self) -> str:
        return f"PlayerPosition({self.x}, {self.y}, {self.z})"
