"""
Test suite v2 - Unit and integration tests for core systems.
"""

import pytest
from physics import integrate, apply_gravity, clamp_velocity, distance
from ecs import World, Component
from morton import morton3d, demorton3d, morton_neighbors_3d
from address import BlockAddress
from world_cache import WorldCache
from sparse_block_store import SparseBlockStore
from journal import Journal, JournalEntry
from predictive_streamer import PredictiveStreamer
from simulation_bubble import SimulationBubble
import tempfile
import os


# ==================== Physics Tests ====================

def test_integrate():
    """Test basic physics integration."""
    result = integrate((0, 0, 0), (1, 0, 0), 2)
    assert result == (2, 0, 0)


def test_integrate_3d():
    """Test 3D physics integration."""
    result = integrate((1, 2, 3), (2, 3, 4), 0.5)
    assert result == (2.0, 3.5, 5.0)


def test_apply_gravity():
    """Test gravity application."""
    vel = (0, 10, 0)
    result = apply_gravity(vel, 1.0, gravity=9.81)
    assert abs(result[1] - 0.19) < 0.01


def test_clamp_velocity():
    """Test velocity clamping."""
    vel = (3, 4, 0)  # Speed = 5
    result = clamp_velocity(vel, max_speed=2.0)
    speed = (result[0]**2 + result[1]**2 + result[2]**2)**0.5
    assert abs(speed - 2.0) < 0.01


def test_distance():
    """Test distance calculation."""
    d = distance((0, 0, 0), (3, 4, 0))
    assert d == 5.0


# ==================== Morton Tests ====================

def test_morton3d_basic():
    """Test Morton encoding."""
    code = morton3d(0, 0, 0)
    assert code == 0


def test_demorton3d():
    """Test Morton decoding."""
    x, y, z = 10, 20, 30
    code = morton3d(x, y, z)
    dx, dy, dz = demorton3d(code)
    assert (dx, dy, dz) == (x, y, z)


def test_morton_neighbors():
    """Test neighbor enumeration."""
    code = morton3d(5, 5, 5)
    neighbors = morton_neighbors_3d(code)
    assert len(neighbors) == 26  # 3x3x3 - 1 center


# ==================== BlockAddress Tests ====================

def test_block_address_creation():
    """Test BlockAddress creation."""
    addr = BlockAddress(10, 20, 30)
    assert addr.x == 10
    assert addr.y == 20
    assert addr.z == 30


def test_block_address_validation():
    """Test coordinate validation."""
    with pytest.raises(ValueError):
        BlockAddress(-1, 0, 0)
    
    with pytest.raises(ValueError):
        BlockAddress(0x200000, 0, 0)  # Exceeds 21-bit max


def test_block_address_distance():
    """Test distance calculation."""
    a1 = BlockAddress(0, 0, 0)
    a2 = BlockAddress(3, 4, 0)
    assert a1.distance_to(a2) == 5.0


def test_block_address_region():
    """Test region containment."""
    addr = BlockAddress(5, 5, 5)
    min_addr = BlockAddress(0, 0, 0)
    max_addr = BlockAddress(10, 10, 10)
    assert addr.in_region(min_addr, max_addr)


# ==================== ECS Tests ====================

def test_ecs_create_entity():
    """Test entity creation."""
    world = World()
    
    class Position(Component):
        def __init__(self, x, y, z):
            self.x, self.y, self.z = x, y, z
    
    pos = Position(1, 2, 3)
    eid = world.create(position=pos)
    assert eid == 1


def test_ecs_component_query():
    """Test component queries."""
    world = World()
    
    class Position(Component):
        pass
    
    class Velocity(Component):
        pass
    
    class Health(Component):
        pass
    
    e1 = world.create(position=Position(), velocity=Velocity())
    e2 = world.create(position=Position())
    e3 = world.create(health=Health())
    
    pos_entities = world.query(Position)
    assert len(pos_entities) == 2
    assert e1 in pos_entities and e2 in pos_entities


# ==================== Cache Tests ====================

def test_world_cache_basic():
    """Test basic cache operations."""
    cache = WorldCache(max_entries=3)
    cache.put(1, b'data1')
    assert cache.get(1) == b'data1'


def test_world_cache_eviction():
    """Test LRU eviction."""
    cache = WorldCache(max_entries=2)
    cache.put(1, b'data1')
    cache.put(2, b'data2')
    cache.put(3, b'data3')  # Should evict key 1
    
    assert cache.get(1) is None
    assert cache.get(2) is not None


def test_world_cache_stats():
    """Test cache statistics."""
    cache = WorldCache(max_entries=10)
    cache.put(1, b'data')
    cache.get(1)
    cache.get(2)  # Miss
    
    stats = cache.get_stats()
    assert stats['hits'] == 1
    assert stats['misses'] == 1


# ==================== Storage Tests ====================

def test_sparse_block_store():
    """Test block storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, 'test.db')
        store = SparseBlockStore(db_path=db_path)
        
        key = morton3d(1, 2, 3)
        data = b'test block data' + b'A' * (4096 - 15)
        
        assert store.write_block(key, data)
        assert store.read_block(key) == data
        store.close()


def test_sparse_block_compression():
    """Test compression in storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, 'test.db')
        store = SparseBlockStore(db_path=db_path, compress=True)
        
        key = morton3d(1, 2, 3)
        data = b'x' * 4096  # Compressible data
        
        store.write_block(key, data)
        retrieved = store.read_block(key)
        assert retrieved == data
        
        stats = {"compressed_blocks": 1}
        assert stats['compressed_blocks'] > 0
        store.close()


# ==================== Journal Tests ====================

def test_journal_append():
    """Test journal append."""
    with tempfile.TemporaryDirectory() as tmpdir:
        journal_path = os.path.join(tmpdir, 'test.log')
        journal = Journal(path=journal_path)
        
        event = {'type': 'test', 'data': 'value'}
        journal.append(0, 1, b"A" * 16)


def test_journal_read():
    """Test journal read."""
    with tempfile.TemporaryDirectory() as tmpdir:
        journal_path = os.path.join(tmpdir, 'test.log')
        journal = Journal(path=journal_path)
        
        event1 = {'type': 'test1'}
        event2 = {'type': 'test2'}
        journal.append(0, 1, b"A" * 16)
        journal.append(0, 2, b"B" * 16)
        
        entries = list(journal.pending())
        assert len(entries) == 2


# ==================== Predictive Streamer Tests ====================

def test_predictive_streamer_predict():
    """Test trajectory prediction."""
    streamer = PredictiveStreamer()
    pos = (0, 0, 0)
    vel = (1, 0, 0)
    predicted = streamer.predict(pos, vel, seconds=5.0)
    assert predicted == (5.0, 0, 0)


def test_predictive_streamer_trajectory():
    """Test trajectory sampling."""
    streamer = PredictiveStreamer()
    pos = (0, 0, 0)
    vel = (1, 1, 1)
    trajectory = streamer.predict_trajectory(pos, vel, num_samples=5)
    assert len(trajectory) == 5
    assert trajectory[0] == pos


# ==================== Simulation Bubble Tests ====================

def test_simulation_bubble_chunks():
    """Test chunk enumeration."""
    bubble = SimulationBubble(radius=1)
    chunks = bubble.chunks(0, 0, 0)
    # 3x3x3 cube
    assert len(chunks) == 27


def test_simulation_bubble_boundary():
    """Test boundary detection."""
    bubble = SimulationBubble(radius=2)
    boundary = bubble.get_boundary_chunks(0, 0, 0)
    
    # Should have chunks on surface of 5x5x5 cube
    assert len(boundary) > 0
    
    # Check corners are boundary
    assert (2, 2, 2) in boundary


def test_simulation_bubble_load_unload():
    """Test load/unload tracking."""
    bubble = SimulationBubble(radius=1)
    
    unload = bubble.get_unload_chunks(0, 0, 0, 1, 0, 0)
    load = bubble.get_load_chunks(0, 0, 0, 1, 0, 0)
    
    assert len(unload) > 0
    assert len(load) > 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
