# Development Guide

## Setup

### Prerequisites
- Python 3.9+
- SQLite3
- pip

### Installation

```bash
# Clone the repository
git clone https://github.com/spiritsandcadavers/Block-Storage-Gaming-Engine.git
cd Block-Storage-Gaming-Engine

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Project Structure

```
Block-Storage-Gaming-Engine/
├── morton.py              # Z-order curve encoding
├── address.py             # 3D coordinate validation
├── sparse_block_store.py  # Compressed block storage
├── world_cache.py         # LRU cache system
├── ecs.py                 # Entity-component system
├── physics.py             # Client-side physics
├── journal.py             # Write-ahead log
├── predictive_streamer.py # Trajectory prediction
├── simulation_bubble.py   # Active region management
├── region_server.py       # Regional coordination
├── render_feed.py         # Delta rendering
├── test_v2.py             # Test suite
├── requirements.txt       # Dependencies
├── README.md              # Project documentation
├── LICENSE                # MIT License
└── CONTRIBUTORS.md        # Team information
```

## Running Tests

```bash
# Run all tests
pytest test_v2.py -v

# Run with coverage report
pytest test_v2.py -v --cov

# Run specific test
pytest test_v2.py::test_morton3d -v
```

## Core Concepts

### Morton Encoding
- Converts 3D coordinates to Z-order curve for cache locality
- Enables efficient neighbor traversal
- 21-bit coordinates per dimension (max 2,097,151)

### Sparse Block Store
- SQLite-based persistent storage
- Optional zlib compression (70-80% ratio)
- Thread-safe connection pooling
- Batch transaction support

### World Cache
- LRU eviction policy
- Thread-safe with RLock
- Statistics tracking (hit rate, memory usage)
- Optional eviction callbacks

### ECS System
- Entity creation with components
- Efficient component queries via indexing
- Dynamic component addition/removal
- Thread-safe operations

### Physics Engine
- Verlet integration
- Gravity and friction simulation
- Velocity clamping
- Distance calculations

### Simulation Bubble
- Defines active simulation region
- Load/unload chunk tracking
- Boundary detection
- Infinite world support

### Predictive Streamer
- Trajectory prediction
- Prefetch block calculation
- Adaptive horizon based on speed
- Movement-based trigger detection

### Region Server
- Client subscription management
- Atomic region updates with versioning
- Callback-based event notification
- Thread-safe operations

### Render Feed
- Visibility tracking
- Delta calculation
- Network packet optimization
- Culling management

## Architecture Patterns

### Thread Safety
All modules use `threading.RLock()` for concurrent access:

```python
with self._lock:
    # Critical section
    self.entities[eid] = components
```

### Connection Pooling
Efficient database access via round-robin pooling:

```python
with self._get_connection() as conn:
    result = conn.execute(query, params)
```

### Compression Strategy
Automatic compression for blocks > 256 bytes:

```python
if len(data) > 256:
    compressed = zlib.compress(data, level=6)
    if len(compressed) < len(data):
        to_store = compressed
```

### Error Handling
Comprehensive logging and graceful degradation:

```python
except Exception as e:
    logger.error(f"Operation failed: {e}")
    return None  # Or appropriate fallback
```

## Performance Tuning

### Cache Configuration
```python
cache = WorldCache(max_entries=4096)  # Adjust based on memory
```

### Compression Level
```python
store = SparseBlockStore(compression_level=6)  # 1-9, default 6
```

### Connection Pool Size
```python
store = SparseBlockStore(pool_size=5)  # Increase for higher concurrency
```

### Simulation Bubble Radius
```python
bubble = SimulationBubble(radius=8)  # ~4,000 chunks at radius=8
```

### Prediction Horizon
```python
streamer = PredictiveStreamer(prediction_horizon=5.0)  # Seconds ahead
```

## Debugging

### Enable Logging
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Cache Statistics
```python
stats = cache.get_stats()
print(f"Hit rate: {stats['hit_rate']:.1f}%")
print(f"Memory: {stats['memory_usage']/1024/1024:.1f}MB")
```

### Storage Statistics
```python
stats = store.get_stats()
print(f"Compression ratio: {stats['compression_ratio']:.1f}%")
```

### Journal Recovery
```python
entries = journal.read_entries(limit=100)
for entry in entries:
    print(entry)
```

## Common Tasks

### Adding a New Component Type
```python
from ecs import Component, World

class Health(Component):
    def __init__(self, hp, max_hp):
        self.hp = hp
        self.max_hp = max_hp

world = World()
entity = world.create(health=Health(100, 100))
```

### Querying Entities
```python
# Find all entities with Position and Velocity
moving_entities = world.query(Position, Velocity)

# Get component from entity
pos = world.get_component(entity_id, Position)
```

### Writing Block Data
```python
from morton import morton3d

key = morton3d(x, y, z)
store.write(key, block_data)

# Batch write
blocks = {morton3d(0,0,0): data1, morton3d(1,0,0): data2}
store.batch_write(blocks)
```

### Predicting Player Path
```python
from predictive_streamer import PredictiveStreamer

streamer = PredictiveStreamer()
predicted = streamer.predict(pos, vel, seconds=5.0)
trajectory = streamer.predict_trajectory(pos, vel, num_samples=10)
prefetch = streamer.get_prefetch_blocks(pos, vel)
```

## Troubleshooting

### Tests Failing
```bash
# Verify imports
python -c "import morton; print(morton.morton3d(1,2,3))"

# Check dependencies
pip list | grep pytest

# Run single test with verbose output
pytest test_v2.py::test_integrate -vv
```

### Database Locked
- Ensure connections are properly closed
- Check for long-running transactions
- Increase pool_size if high concurrency

### Memory Issues
- Reduce cache `max_entries`
- Monitor with `cache.get_stats()`
- Check for memory leaks in callbacks

### Performance Degradation
- Profile with `cProfile`
- Check compression ratios with `store.get_stats()`
- Monitor cache hit rates

## Next Steps

1. **Network Layer**: Implement client-server communication
2. **Async I/O**: Integrate asyncio for non-blocking operations
3. **Distributed Transactions**: Multi-region consistency
4. **Profiling**: Add performance monitoring
5. **Benchmarking**: Measure throughput and latency

## Support

For issues or questions:
1. Check existing GitHub Issues
2. Review test cases for usage examples
3. Consult README.md for architecture overview
4. Check docstrings in source code
