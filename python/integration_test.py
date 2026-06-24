"""Integration test: World simulation with multiple players.

Demonstrates the core concept:
- Player position maps directly to byte offset
- No database, no abstractions
- Just flat file I/O
"""

import os
import tempfile
from offset_engine import OffsetConfig, WorldEngine, PlayerPosition


def test_world_simulation():
    """Simulate a world with multiple players moving and leaving traces."""

    # Setup
    temp_dir = tempfile.mkdtemp()
    world_file = os.path.join(temp_dir, "world.bin")

    # World: 1MB, 4KB blocks
    config = OffsetConfig(1024 * 1024, 4096)
    engine = WorldEngine(world_file, config)
    engine.open()

    print("✓ World initialized (1MB, 4KB blocks)")

    # Player 1 at position (0, 0, 0)
    player1 = PlayerPosition(0, 0, 0)
    offset1 = player1.to_offset(256, 256)

    # Write player1 data
    player1_data = b"P1" + b"\x00" * (4096 - 2)
    engine.write_at_position(offset1, player1_data)
    print(f"✓ Player1 at {player1} (offset {offset1}) wrote 4KB block")

    # Player 2 at position (100, 50, 0)
    player2 = PlayerPosition(100, 50, 0)
    offset2 = player2.to_offset(256, 256)

    # Write player2 data
    player2_data = b"P2" + b"\x00" * (4096 - 2)
    engine.write_at_position(offset2, player2_data)
    print(f"✓ Player2 at {player2} (offset {offset2}) wrote 4KB block")

    # Verify player1 data still intact
    verify1 = engine.read_at_position(offset1, 4096)
    assert verify1[:2] == b"P1", "Player1 data corrupted!"
    print(f"✓ Player1 data verified (no cross-contamination)")

    # Verify player2 data
    verify2 = engine.read_at_position(offset2, 4096)
    assert verify2[:2] == b"P2", "Player2 data corrupted!"
    print(f"✓ Player2 data verified")

    # Player 2 moves to (200, 100, 0)
    player2_new = PlayerPosition(200, 100, 0)
    offset2_new = player2_new.to_offset(256, 256)

    engine.write_at_position(offset2_new, player2_data)
    print(f"✓ Player2 moved to {player2_new} (offset {offset2_new})")

    # Old position should be zeros (overwritten by world)
    old_data = engine.read_at_position(offset2, 4096)
    assert old_data[:2] != b"P2", "Old player2 position should be cleared!"
    print(f"✓ Old Player2 position cleared")

    # Flush to disk
    engine.flush()
    engine.close()
    print(f"✓ World flushed and closed")

    # Reopen and verify persistence
    engine2 = WorldEngine(world_file, config)
    engine2.open()

    persist1 = engine2.read_at_position(offset1, 4096)
    assert persist1[:2] == b"P1", "Player1 data not persisted!"

    persist2 = engine2.read_at_position(offset2_new, 4096)
    assert persist2[:2] == b"P2", "Player2 data not persisted!"

    engine2.close()
    print(f"✓ Data persisted across close/reopen cycle")

    # Cleanup
    os.remove(world_file)
    os.rmdir(temp_dir)

    print("\n✅ Integration test PASSED")
    print("   Position → Byte Offset → Block I/O works end-to-end")


if __name__ == "__main__":
    test_world_simulation()
