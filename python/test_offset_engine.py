"""Unit tests for Block-Offset-Engine Python PoC."""

import os
import tempfile
import pytest
from offset_engine import (
    OffsetConfig,
    OffsetResult,
    calculate_offset,
    is_aligned,
    WorldEngine,
    PlayerPosition,
)


class TestOffsetConfig:
    """Tests for OffsetConfig."""

    def test_valid_config(self):
        """Test valid configuration."""
        config = OffsetConfig(4096, 512)
        assert config.total_size == 4096
        assert config.block_size == 512

    def test_invalid_total_size(self):
        """Test that zero total_size raises error."""
        with pytest.raises(ValueError):
            OffsetConfig(0, 512)

    def test_invalid_block_size(self):
        """Test that zero block_size raises error."""
        with pytest.raises(ValueError):
            OffsetConfig(4096, 0)

    def test_misaligned_size(self):
        """Test that unaligned total_size raises error."""
        with pytest.raises(ValueError):
            OffsetConfig(4097, 512)  # 4097 not divisible by 512


class TestOffsetCalculation:
    """Tests for offset calculation."""

    def setup_method(self):
        """Setup test configuration."""
        self.config = OffsetConfig(4096, 512)

    def test_offset_at_zero(self):
        """Test offset calculation at position 0."""
        result = calculate_offset(self.config, 0)
        assert result.valid is True
        assert result.offset == 0

    def test_offset_at_position(self):
        """Test offset calculation at arbitrary position."""
        result = calculate_offset(self.config, 512)
        assert result.valid is True
        assert result.offset == 512

    def test_offset_at_end(self):
        """Test offset calculation at world boundary."""
        result = calculate_offset(self.config, 4095)
        assert result.valid is True
        assert result.offset == 4095

    def test_offset_out_of_bounds(self):
        """Test offset calculation beyond world size."""
        result = calculate_offset(self.config, 4096)
        assert result.valid is False

    def test_offset_negative_position(self):
        """Test offset calculation with negative position."""
        result = calculate_offset(self.config, -1)
        assert result.valid is False


class TestAlignment:
    """Tests for block alignment."""

    def setup_method(self):
        """Setup test configuration."""
        self.config = OffsetConfig(4096, 512)

    def test_aligned_offsets(self):
        """Test that block-aligned offsets are recognized."""
        assert is_aligned(self.config, 0) is True
        assert is_aligned(self.config, 512) is True
        assert is_aligned(self.config, 1024) is True

    def test_unaligned_offsets(self):
        """Test that unaligned offsets are rejected."""
        assert is_aligned(self.config, 1) is False
        assert is_aligned(self.config, 256) is False
        assert is_aligned(self.config, 511) is False


class TestWorldEngine:
    """Tests for WorldEngine."""

    def setup_method(self):
        """Setup test world file."""
        self.temp_dir = tempfile.mkdtemp()
        self.world_file = os.path.join(self.temp_dir, "world.bin")
        self.config = OffsetConfig(4096, 512)
        self.engine = WorldEngine(self.world_file, self.config)

    def teardown_method(self):
        """Cleanup test world file."""
        self.engine.close()
        if os.path.exists(self.world_file):
            os.remove(self.world_file)
        os.rmdir(self.temp_dir)

    def test_world_file_created(self):
        """Test that world file is created."""
        assert os.path.exists(self.world_file)
        assert os.path.getsize(self.world_file) == 4096

    def test_open_close(self):
        """Test opening and closing engine."""
        self.engine.open()
        assert self.engine.mm is not None
        self.engine.close()
        assert self.engine.mm is None

    def test_write_and_read(self):
        """Test writing and reading data."""
        self.engine.open()

        # Write at position 0
        data_write = b"\xAA" * 512
        success = self.engine.write_at_position(0, data_write)
        assert success is True

        # Read from position 0
        data_read = self.engine.read_at_position(0, 512)
        assert data_read == data_write

    def test_write_and_read_at_offset(self):
        """Test writing and reading at arbitrary position."""
        self.engine.open()

        # Write at position 512
        data_write = b"\xBB" * 512
        success = self.engine.write_at_position(512, data_write)
        assert success is True

        # Read from position 512
        data_read = self.engine.read_at_position(512, 512)
        assert data_read == data_write

    def test_read_out_of_bounds(self):
        """Test reading beyond world boundary."""
        self.engine.open()
        data = self.engine.read_at_position(4096, 512)
        assert data is None

    def test_write_out_of_bounds(self):
        """Test writing beyond world boundary."""
        self.engine.open()
        success = self.engine.write_at_position(4096, b"\xCC" * 512)
        assert success is False

    def test_multiple_blocks_independence(self):
        """Test that multiple blocks don't interfere."""
        self.engine.open()

        # Write to block 0
        self.engine.write_at_position(0, b"\xAA" * 512)

        # Write to block 1 (position 512)
        self.engine.write_at_position(512, b"\xBB" * 512)

        # Verify block 0 unchanged
        data0 = self.engine.read_at_position(0, 512)
        assert data0 == b"\xAA" * 512

        # Verify block 1
        data1 = self.engine.read_at_position(512, 512)
        assert data1 == b"\xBB" * 512

    def test_flush(self):
        """Test that flush doesn't crash."""
        self.engine.open()
        self.engine.write_at_position(0, b"\xCC" * 512)
        self.engine.flush()  # Should not raise


class TestPlayerPosition:
    """Tests for PlayerPosition."""

    def test_position_creation(self):
        """Test creating player position."""
        pos = PlayerPosition(10, 20, 30)
        assert pos.x == 10
        assert pos.y == 20
        assert pos.z == 30

    def test_position_to_offset(self):
        """Test converting 3D position to linear offset."""
        # World is 16x16 chunks
        pos = PlayerPosition(5, 3, 2)
        offset = pos.to_offset(16, 16)
        # offset = z*sx*sy + y*sx + x = 2*16*16 + 3*16 + 5 = 512 + 48 + 5 = 565
        assert offset == 565

    def test_position_origin(self):
        """Test position at origin maps to offset 0."""
        pos = PlayerPosition(0, 0, 0)
        offset = pos.to_offset(16, 16)
        assert offset == 0
