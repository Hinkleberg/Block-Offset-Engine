# Block-Image Engine

### A spatial compute primitive built on the physics of storage.

I came up with the hairbrained idea that if zelda could move logically through storage, why cant I? I brainedstormed for 3 years, trying to figure out a way to represent storage in that manner. I beat my head against the wall, because there were always a bottleneck somewhere. This Engine fixes that reality, at least in my theory. This is all theory and nothing is concrete. This was purely an idea I had. Living on a prayer. 

Most systems that need to represent space — game worlds, city digital twins, military simulations, scientific grids, disaster models — solve the same problem the same way. They build a database. They add a streaming layer. They add a cache. They add a network protocol. Then they hope the stack is fast enough and pray it doesn't desync under load.

They treat storage as a place to retrieve *data about* space.

This engine treats storage *as* space.

Position is not a key. Position is not a query. Position is a byte offset — a direct physical address on the storage device. Moving through the world is indistinguishable, at the hardware level, from advancing a read across a NVMe. There is no middleware between a coordinate and its data. The physics of the storage array are the physics of the world.

```
offset(x, y, z) = (z × WORLD_X × WORLD_Y  +  y × WORLD_X  +  x) × BLOCK_SIZE
```

That single arithmetic expression is the entire engine's identity. Everything else — crash safety, replication, integrity verification, render isolation, entity state — is infrastructure built to protect and serve it.

What emerges from this inversion is not just a faster game engine. It is a new class of spatial infrastructure: one where continent-scale environments are fully addressable by arithmetic alone, mutations are crash-safe and quorum-enforced, reads and writes are physically isolated so neither can starve the other, and the entire world fits in a single flat image that any agent — a game client, an autonomous vehicle, a rover, a fire simulation — can navigate without touching a database.

---

## Scale

At 16 bytes per block and a block resolution of ~66 cm × 66 cm of real-world ground:

| Storage | Representable Area | Real-World Equivalent |
|---------|-------------------|----------------------|
| 10 TB | ~269,600 km² | Colorado |
| 100 TB | ~2.7 million km² | Western United States |
| 1 PB | ~27 million km² | North America + Europe |
| 9.2 PB | ~248 million km² | Half of Earth's total surface |

9.2 PB at flat-world resolution produces ~575 trillion addressable blocks. A person walking the square world it represents in a straight line at 5 km/h, without stopping, would take 358 years to cross it. In a 3D world with 256 vertical layers, that same 9.2 PB yields a footprint of ~968,000 km² — still larger than Egypt — with full volumetric depth.

This is, to the best of current knowledge, the largest single-image offset-addressable spatial environment ever designed — where position equals a physical byte on storage with no indirection layer between them.

---

## The Architecture in One View

```
┌─────────────────────────────────────────────────────────────────┐
│                        Mutation Engine                           │
│                   (world_gen, run_server)                        │
└────────────────────────────┬────────────────────────────────────┘
                             │  write_block(offset, data)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                       ResilientStore                             │
│   Write: Journal → SparseBlockStore → ReplicationManager        │
│   Read:  Local → Verify → Recover from Replica                  │
│   State: Persisted block_state_index (binary flat image)         │
└──────────────┬──────────────────────┬──────────────────────────┘
               │                      │
  ┌────────────▼──────┐   ┌───────────▼──────────────────┐
  │  SparseBlockStore │   │      ReplicationManager       │
  │  Binary + zlib    │   │      Fan-out to N nodes       │
  │  SHA256 checksums │   │      Quorum enforcement       │
  │  LRU eviction     │   │      Persistent entry log     │
  │  Capacity bounds  │   │      Auto-unhealthy nodes     │
  └───────────────────┘   └──────────────────────────────┘
               │
               │  post-commit async forward (mirror callback)
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                        RenderStore                          Array B
│            read-only interface to render feed            (render array)
│            async block intake + integrity scan                   │
└──────────────────────────┬──────────────────────────────────────┘
                           │  read_block(offset)
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                        RenderFeed                                │
│               delta-only, 20 Hz per client                       │
└──────────────────────────┬──────────────────────────────────────┘
                           │  RenderDelta (block deltas + entity deltas)
                           ▼
                       Thin Client

    EntitySidecar ──────────────────────────────► RenderFeed
    (parallel image, entity state only,
     separate from geometry write path)
```

---

## Key Differentiators

**Storage is the world.** There is no database schema that represents space. Space is represented by the storage device directly. A coordinate is arithmetic. A seek is movement.

**Single unified engine with minimal layers.** Most spatial systems are a coordination problem across five or six layers. This engine collapses that stack into one flat image and a handful of protection layers around it.

**Hardware-agnostic core.** The same engine runs identically over RAM, NVMe, or cloud block storage (EBS, GCS, Azure Disk). The coordinate-to-offset formula is the same regardless of what sits underneath. Plug in the hardware; the world doesn't change.

**External tools are optional integrations, not dependencies.** SQL databases, game engines (Unreal, Unity, Godot, O3DE), physics engines, autonomous vehicle simulation stacks, and military simulation frameworks are not part of this engine. They can always be hooked in at a later date via adapter modules that translate between the engine's binary block API and whatever interface the external tool expects. The engine itself remains agnostic — it knows nothing about the tool on the other end of the adapter, and the tool need know nothing about the engine's internals. This is the correct separation. The adapters live at the boundary; the engine lives at the core.

**Extreme efficiency at scale.** Massive persistent worlds with a low hardware footprint. No streaming middleware means no cache warm-up latency, no chunk boundary stalls, no object graph deserialization. Reading the block at `(10, 64, 10)` is `(10 × W × H + 64 × W + 10) × 16`. That's it.

**Crash safety by design, not by policy.** Every write is journaled before it touches the block store. Every read verifies a SHA-256 checksum. Every replication enforces quorum. The engine cannot silently corrupt — it either succeeds verifiably or raises an error.

**Physically isolated read and write paths.** Array A absorbs all writes. Array B — a post-commit, post-quorum async mirror — serves all reads. A burst of world mutations cannot stall the render feed. Array B degradation cannot block writes. Both arrays hold the same flat image schema; Array B simply lags Array A by the async forward window.

---

## Two-Array Design

The dual-array design is the engine's most important operational property and the one most spatial systems get wrong.

Array A (ResilientStore) is the write array. The mutation engine, crash journal, quorum enforcement, and crash recovery all operate here exclusively. The render feed never touches it.

Array B (RenderStore) is the read array. It receives only post-commit, post-quorum blocks forwarded asynchronously from Array A. The render feed reads exclusively from here — zero write-path contention, full I/O throughput for reads.

This separation means a burst of world mutations — a world generator running flat out, an AI tick updating thousands of blocks, a disaster propagation event rewriting a region — never introduces a single frame of latency into what clients see. Writes and reads are physically decoupled at the storage layer, not just at the software layer.

The intended production configuration is two separate NVMe devices — ideally direct-attached to the datacenter rack — with `world.img` on one and `world_render.img` on the other. No table, no query, no intermediary: direct addressing, direct I/O. The `mirror_write_seq` property tracks how far Array B lags Array A in real time. The MirrorHealthMonitor raises status before the render feed ever notices a problem.

---

## What This Engine Can Be Used For

The game is the most intuitive application. It is not the only one, and possibly not the most important one.

Every industry that deals with massive persistent spatial data shares the same underlying problem this engine solves: a world that many agents need to read and write simultaneously, where reads cannot be blocked by writes, where mutations must be crash-safe and auditable, and where the coordinate-to-data lookup must be fast enough to disappear as a bottleneck. Most existing solutions stack a database, a streaming layer, a cache, and a network protocol on top of each other. This engine collapses that entire stack into arithmetic.

### Defense & Military Simulation

Military simulation requires persistent, continent-scale terrain that thousands of simultaneous agents — vehicles, aircraft, infantry units, logistics chains — can read and write in real time. The dual-array design maps directly onto the separation between the authoritative battlespace state (Array A) and the picture individual commanders see (Array B). The crash-safe journal means a simulation survives a power cut mid-exercise and resumes without data loss. Current military simulation engines like VBS4 and OneSAF use heavily sharded databases that introduce latency at chunk boundaries. The flat offset model eliminates that class of problem entirely — a unit's position is a byte offset, a theater of operations is a byte range, and a seek across terrain is a seek across storage.

### Autonomous Vehicle Training

AV companies burn enormous compute on synthetic driving environments. The bottleneck is rarely the GPU — it is the world streaming layer, which must pull terrain and dynamic object state from databases fast enough to feed thousands of parallel simulation instances simultaneously. This engine sidesteps that problem by design. A city block is a byte range. A highway corridor is a contiguous seek. A pedestrian is an entity sidecar record linked to a block offset by a single pointer. The geometry and the dynamic state are physically separated write paths — exactly what a high-frequency simulation environment needs, where the world changes slowly and the agents within it change constantly.

### Disaster Response & Emergency Management

FEMA, wildfire agencies, and flood modelers need to simulate evolving terrain state: fire spreading block by block, floodwater occupying cells, road networks becoming impassable, evacuation corridors opening and closing in real time. The block state machine maps almost directly onto the lifecycle of an affected area: `PENDING → CLEAN → REPLICATED` becomes `unaffected → threatened → confirmed affected → recovered`. The lighting propagator — already a diffusion engine that propagates a value through adjacent blocks — requires only a different physical interpretation to model fire spread or flood inundation. The crash-safe replication means field commanders at different sites see a consistent world state even on degraded or intermittent connectivity.

### Urban Digital Twins

Cities like Singapore, Helsinki, and Dubai are building full 3D digital twins of their urban infrastructure. The current tooling — Esri CityEngine, Bentley iTwin — stores these as object graphs with streaming layers and version-controlled changesets. This model inverts that: the city is the storage array. Every building, pipe, cable, road surface, and underground utility is a block at a known offset. Mutation events — a water main break, a building permit approval, a road resurfacing — write through the journal with a full audit trail via `write_seq`. The `write_seq` lag tracking between Array A and Array B becomes a real-time consistency dashboard across city departments: the engineering department's view of a pipe repair and the emergency services department's view of the same street are guaranteed to converge within the async forward window, with health status visible at all times.

### Scientific Simulation

Oceanographers, atmospheric scientists, and geologists all work with massive 3D spatial grids: ocean current models, seismic wave propagation, subsurface geological layers, atmospheric pressure fields. The 3D offset formula `(z × W × H + y × W + x) × 16` maps directly onto any volumetric scientific grid. The SHA-256 checksum per block and the non-blocking integrity scanner give scientific workloads something most HPC storage stacks do not have out of the box: guaranteed silent corruption detection. A flipped bit in simulation output has corrupted published scientific results before. This engine makes that class of failure structurally impossible — a corrupt block is detected on the next read, identified precisely, and recoverable from any replica that holds a clean copy.

### Space Mission Planning

NASA and ESA maintain elevation and surface datasets for Mars, the Moon, and other bodies. Mars's surface is approximately 144 million km² — it fits within the address space of a mid-size deployment of this engine. Rover pathfinding becomes an offset range query. Landing zone hazard analysis is a block read with a radius scan. Multi-mission coordination across different surface sites is exactly the multi-agent spatial mutation model the engine was designed for. The entity sidecar naturally models rovers, landers, orbital assets, and planned traverse paths as parallel state without polluting the surface geometry write path.

### Infrastructure & Utilities

Power grids, gas pipelines, fiber networks, and water systems all share the same fundamental records problem: who has the authoritative current state of this asset, and what changed and when? The replication manager with its persistent entry log and monotonic `write_seq` is a distributed ledger for spatial mutations. Every dig, repair, upgrade, or fault event writes through the journal. The quorum enforcement means no single field crew can create a split-brain state in the network map. The `nodes_with_block()` method always reflects the true replication state because the log survives restarts — there is no reconciliation step after a node comes back online.

### Film & VFX Production

Large-scale VFX environments — a photoreal battlefield, a fantasy continent, a destroyed urban landscape — are currently stored as proprietary scene graphs that different departments check out, modify, and merge through version control systems that were designed for source code, not spatial data. The dual-array design maps naturally onto a production pipeline: the write array is the working environment that artists and simulation departments mutate; the read array is what the renderer and compositing pipeline sees. The async mirror forward is a render farm feed that is never blocked by an artist mid-save. The lighting propagator is a first-class engine citizen rather than a downstream post-process pass, which means lighting state is consistent with geometry state by construction.

---

## Industry Adapters

Each industry that operates on this engine brings its own toolchain, its own wire protocols, and its own simulation algorithms. The engine does not absorb those concerns — it exposes a clean binary block API and lets adapters translate between that API and whatever the industry tool expects. Adapters are thin translation layers, not rewrites. The engine stays fast and agnostic; the adapter handles the impedance mismatch.

All adapters follow the same pattern: implement `read_block(offset) → bytes`, `write_block(offset, data)`, and `entity_update(record)` against the engine's binary API, then speak the industry tool's native protocol on the other side. The engine never knows which adapter is attached. The adapter never needs to understand the engine's internals beyond those three calls.

> **Note:** The adapters listed here (and in the repo) are the correct place to wire in external tools. SQL layers, cloud asset stores, analytics pipelines, and any other system can be added at any time through this mechanism without modifying the engine itself.

---

### military_adapter.py

**Defense & Military Simulation Adapter**

Military simulation tools — VBS4, OneSAF, JSAF, JCATS, and their derivatives — communicate over DIS (Distributed Interactive Simulation, IEEE 1278) and HLA (High Level Architecture, IEEE 1516). These protocols model the battlespace as a collection of entities with PDU (Protocol Data Unit) state packets, not as a flat spatial image. This adapter bridges between the engine's binary block and entity-sidecar API and the DIS/HLA entity-state model.

**Translation responsibilities:**

- **Coordinate system:** Military simulations typically use ECEF (Earth-Centered Earth-Fixed) geodetic coordinates (lat/lon/alt) or UTM. The adapter converts between geodetic and the engine's flat-offset space using a configurable origin anchor and scale factor.
- **Entity PDU → EntityRecord:** Incoming DIS Entity State PDUs are decoded and mapped to the engine's 64-byte `EntityRecord` format. The PDU's `EntityIdentifier` becomes `entity_id`, dead-reckoning state is resolved to a concrete position and written as `(x, y, z, vx, vy, vz)`, and the PDU's `EntityType` enum maps to the engine's `EntityType`.
- **EntityRecord → PDU:** Outgoing entity delta packets from `tick_delta()` are serialised back to DIS Entity State PDUs for broadcast to external simulation participants.
- **Terrain mutation:** Terrain-modification events (crater creation, defilade, obstacle placement) are translated from their DIS/HLA mutation representation to `write_block(offset, data)` calls through the ResilientStore write path, ensuring full journaling and quorum enforcement.
- **Battlespace picture isolation:** Array A is the authoritative battlespace state; Array B is the common operating picture distributed to commander terminals. The adapter exposes Array B's render feed as a DIS/HLA multicast stream.

```python
from military_adapter import MilitarySimAdapter, CoordOrigin

adapter = MilitarySimAdapter(
    resilient_store=rs,
    entity_sidecar=sidecar,
    render_feed=feed,
    origin=CoordOrigin(lat=38.8977, lon=-77.0365, alt=0.0),  # anchor point
    scale_m_per_block=0.66,          # 66 cm ground resolution
    dis_port=3000,
    hla_federation="BattlespaceEngine",
)
adapter.start()
# Incoming DIS PDUs → engine writes; outgoing render deltas → DIS multicast
```

**Pluggable algorithms:** The adapter exposes hooks for unit-specific algorithms the military simulation tool provides — line-of-sight, radar cross-section, threat assessment. These are callbacks: `los_callback(from_offset, to_offset) → bool`, `threat_callback(entity_record) → float`. The engine calls none of them directly; the adapter wires them in.

---

### autonomous_adapter.py

**Autonomous Vehicle & Robotics Simulation Adapter**

AV simulation tools — CARLA, LGSVL, AWSIM, Waymo's internal stack, and ROS 2-based frameworks — model the world as a combination of HD map lanes, semantic occupancy grids, and dynamic actor state. Their primary bottleneck is world streaming: pulling terrain and dynamic object state from databases fast enough to feed thousands of parallel simulation instances simultaneously. This adapter exposes the engine's binary block and entity-sidecar API as the world data source for AV simulation, replacing the database streaming layer with direct offset arithmetic.

**Translation responsibilities:**

- **Coordinate system:** AV tools typically work in local ENU (East-North-Up) coordinates anchored to a map origin, or in Lanelet2 lane-graph coordinates. The adapter converts ENU ↔ engine offset using a configurable map origin and resolution.
- **Occupancy grid mapping:** The engine's 3D block grid maps directly onto a voxel occupancy grid. `block_type` and `flags.solid` are translated to occupancy values. The adapter materialises occupancy subgrids on demand for the AV stack's sensor simulation modules.
- **ROS 2 / Cyber RT integration:** Entity records from `tick_delta()` are published as `nav_msgs/Odometry`, `derived_object_msgs/ObjectArray`, or `autoware_auto_perception_msgs/TrackedObjects` topics depending on the target framework. Incoming actor updates from the AV simulation are translated back to `EntityRecord` writes.
- **HD map layer:** Static road geometry (lane boundaries, traffic signs, crosswalks) is stored as a specialised block type layer. The adapter reads these blocks and re-emits them as Lanelet2-format map tiles or OpenDRIVE segments for consumption by the AV planner.
- **Sensor simulation feeds:** LIDAR, RADAR, and camera frustum queries are translated to `blocks_in_range()` + entity sidecar lookups. The adapter packages the results in the sensor data format the AV simulation expects (e.g. ROS `sensor_msgs/PointCloud2` for LIDAR).

```python
from autonomous_adapter import AVSimAdapter, MapOrigin, AVFramework

adapter = AVSimAdapter(
    resilient_store=rs,
    entity_sidecar=sidecar,
    render_feed=feed,
    origin=MapOrigin(east=0.0, north=0.0, up=0.0),
    scale_m_per_block=0.66,
    framework=AVFramework.ROS2,      # or CARLA, LGSVL, AWSIM, AUTOWARE
    ros_namespace="/world_engine",
)
adapter.start()
# Block reads → occupancy grid; entity deltas → ROS topics; AV actor state → entity writes
```

**Pluggable algorithms:** AV stacks bring their own path planning, sensor fusion, and prediction algorithms. The adapter exposes callbacks: `obstacle_callback(offsets) → list[EntityRecord]`, `map_query_callback(lane_id) → list[offset]`. The engine never touches these; the adapter wires them into the appropriate block or entity reads.

---

### Additional Adapters (Reference Implementations)

The repo includes reference adapter implementations for other industries. All follow the same pattern — binary block API inward, industry protocol outward:

| Adapter | Tool(s) | Protocol / Format |
|---------|---------|-------------------|
| `godot_adapter.py` | Godot 4.x | GDExtension / WebSocket block stream |
| `unity_adapter.py` | Unity | C# native plugin / UDP block stream |
| `unreal_adapter.py` | Unreal Engine 5 | Unreal plugin / shared memory block bridge |
| `o3de_adapter.py` | O3DE | Gem / EBus block event bridge |
| `sql_store_adapter.py` | Any SQL consumer | Read-only SQL view over block data (analytics, asset pipelines) |

> **These tools are not part of the engine.** They are optional integrations. The engine works identically whether zero adapters or ten are attached. Adapters are the correct place for industry-specific logic, protocol translation, and algorithmic hooks — not the engine core.

---

## Modules

### block_layout.py

Coordinate ↔ byte-offset arithmetic. The engine's core identity — the single expression that makes position equal to a physical byte address on the storage device.

- `block_offset(x, y, z)` — O(1), branch-free, integer-only. The only function that fundamentally distinguishes this engine from a generic key-value store.
- `offset_to_coord(offset)` — exact inverse, used for round-trip validation.
- `chunk_offset(cx, cy, cz)` — maps chunk coordinates to the byte offset of a chunk's first block; chunks are 16×16×16 blocks and align to NVMe page boundaries.
- `player_offset(px, py, pz)` — converts floating-point player position to the byte offset of the occupied block; evaluated every tick.
- `blocks_in_range(cx, cy, cz, radius)` — returns all byte offsets within a cubic radius; used by the render feed to determine view-frustum block set.

```python
from block_layout import WorldLayout, Block, BlockType

layout = WorldLayout(64, 64, 64)
offset = layout.block_offset(10, 64, 10)      # O(1) arithmetic
coord  = layout.offset_to_coord(offset)        # round-trip verification
print(layout)  # WorldLayout(64×64×64 blocks, image=4.0 MB)
```

### sparse_block_store.py

Binary flat-image sparse block store. The engine operates directly on binary block I/O — no database layer sits between a coordinate and its bytes. Every block is zlib-compressed and SHA-256-checksummed on write; the digest is verified on every read. Silent corruption is structurally impossible.

- `ChecksumMismatchError` raised on corrupt reads — the engine never silently returns bad data.
- `verify_integrity()` — paginated generator scan over the flat image using a dedicated read-only file handle; never blocks live I/O. Drive it from a background thread or maintenance loop.
- `get_block_metadata()` — returns checksum, compression flag, timestamp, and write sequence number without loading the payload.
- World geometry enforcement — `max_blocks` and `block_size` cap the address space to match the flat image dimensions. `CapacityError` raised on overflow.
- LRU eviction — least-recently-read block evicted when the store is at capacity (`evict_on_full=True`).
- Monotonic `write_seq` field persisted per block record; restored from the image on startup for stale-read detection.
- Separate write and read-only file handles: the write handle owns all mutations; the read-only handle serves integrity scans concurrently without locking.

```python
from sparse_block_store import SparseBlockStore, ChecksumMismatchError, CapacityError

store = SparseBlockStore("world.img", max_blocks=65536, block_size=4096)
checksum = store.write_block(0, data)
raw = store.read_block(0)                   # verifies checksum automatically

for result in store.verify_integrity():     # non-blocking generator
    if result.status == "corrupted":
        handle(result.offset)
```

### replication_manager.py

Multi-node block replication with quorum enforcement and a persistent binary entry log.

- `register_node` / `deregister_node` — dynamic node registry with per-node metadata.
- `replicate_block()` — fans the block out to all healthy nodes via a pluggable `sync_callback`; raises `QuorumError` if `successful_nodes < required_replicas`.
- Quorum is hard-enforced — writes below threshold are never silently accepted.
- Persistent replication log — a binary append-only log records which nodes hold which blocks by offset; survives restarts so `nodes_with_block()` is always accurate. No SQL engine is involved in the replication path — the log is a flat binary structure with a fixed record size, written and read with direct byte addressing. This keeps the replication path consistent with the storage model: offset in, offset out.
- Auto-unhealthy — a node is automatically marked unhealthy after `failure_threshold` consecutive failures (default 3); no external health-checker required.
- `mark_healthy()` / `mark_unhealthy()` — manual override for external health monitors.
- `statistics()` and `health_report()` — per-node and aggregate monitoring snapshots.

```python
from replication_manager import ReplicationManager, QuorumError

def my_sync(node_id, offset, data):
    remote_nodes[node_id].put_block(offset, data)

rm = ReplicationManager(sync_callback=my_sync, required_replicas=2,
                        log_path="repl_log.bin")
rm.register_node("node-a", {"host": "10.0.0.1", "port": 7001})
rm.register_node("node-b", {"host": "10.0.0.2", "port": 7001})
rm.register_node("node-c", {"host": "10.0.0.3", "port": 7001})

try:
    entry = rm.replicate_block(42, data)
    print(entry.successful_nodes, entry.quorum_met)
except QuorumError as e:
    print(f"Durability threshold not met: {e}")
```

### resilient_store.py

The integration layer. Combines crash safety, integrity verification, replication, and async mirror fan-out to Array B into a single coherent write and read path.

**Write flow:**
1. Journal the write intent (crash-safe pre-commit).
2. Write to local SparseBlockStore (compressed + checksummed).
3. Confirm write via `write_seq` read-back (read-your-writes guarantee).
4. Fan out to replicas via ReplicationManager (quorum enforced).
5. Commit the journal entry.
6. Async forward to all registered RenderStore mirrors — fires outside the write lock, never adds latency to the mutation path.

**Read flow:**
1. Read from local store with checksum verification.
2. On `ChecksumMismatchError` → attempt recovery from known replica nodes.
3. On successful recovery → overwrite the corrupt local block; return data.
4. If all replicas fail → raise `CorruptBlockError`.

**Crash recovery:** Journal replay cross-checks the local store on startup. If the block exists and is intact, the journal entry is auto-committed. If the block is missing or corrupt, the offset is queued in `pending_replay` for caller re-issue. Recovered blocks are forwarded to mirrors so Array B stays consistent after a crash.

Block states: `PENDING → CLEAN → SYNCING → REPLICATED` (or `CORRUPTED`)
Health states: `HEALTHY / DEGRADED / CRITICAL`

```python
from resilient_store import ResilientStore, BlockState, CorruptBlockError
from sparse_block_store import SparseBlockStore
from replication_manager import ReplicationManager, QuorumError

local = SparseBlockStore("world.img", max_blocks=65536)
rm    = ReplicationManager(sync_callback=my_sync, required_replicas=2,
                           log_path="repl_log.bin")

rs = ResilientStore(
    local_store=local,
    replication_manager=rm,
    state_path="state.bin",
    recovery_callback=my_recover,   # (node_id, offset) -> bytes
)

try:
    record = rs.write_block(offset, data)
except QuorumError:
    pass  # block is locally durable; replication did not meet quorum

data = rs.read_block(offset)        # auto-recovers on corruption

for offset in rs.pending_replay:    # after a crash
    rs.write_block(offset, original_data[offset])

print(rs.health())          # HEALTHY / DEGRADED / CRITICAL
print(rs.health_report())   # full snapshot
```

### render_store.py

Array B: render-dedicated storage. Receives post-commit, post-quorum block forwards from ResilientStore via an async queue. Exposes a read-only interface to the render feed. The render feed never touches Array A.

- `enqueue_forward_sync(offset, data, write_seq)` — non-blocking; drops to a background drain thread. Never back-pressures Array A.
- `read_block(offset)` / `read_range(start, length)` — read-only. On checksum failure, transparently falls back to the `primary_fallback` callable so the render feed is never interrupted.
- `mirror_write_seq` property — tracks how far Array B lags behind Array A; consumed by MirrorHealthMonitor.
- Own background integrity scan loop independent of Array A.
- Multiple RenderStore instances can be registered on one ResilientStore for redundant render arrays.

```python
from render_store import RenderStore

render = RenderStore(
    img_path="world_render.img",
    primary_fallback=primary.read_block,
)
primary.register_mirror(render.enqueue_forward_sync)
block = render.read_block(offset)   # render feed reads only from here
```

### mirror_health_monitor.py

Watches lag between Array A (`write_seq`) and one or more Array B mirrors (`mirror_write_seq`). Raises status before the render feed ever notices a problem.

| Status | Condition |
|--------|-----------|
| HEALTHY | lag < `lag_warn_threshold` (default 100 blocks) |
| WARNING | lag ≥ warn threshold |
| DEGRADED | lag ≥ `lag_degraded_threshold` (default 500 blocks) |
| OFFLINE | no mirror progress for `stale_timeout` seconds (default 30s) |

```python
from mirror_health_monitor import MirrorHealthMonitor, MirrorStatus

monitor = MirrorHealthMonitor(
    primary=primary,
    mirrors={"render_b": render},
    lag_warn_threshold=100,
    lag_degraded_threshold=500,
    on_status_change=lambda name, status: print(f"{name} → {status.name}"),
)
monitor.start()
```

### entity_sidecar.py

Parallel entity state image. Entity state is intentionally separated from the world block image — entities update every tick at high frequency; geometry changes slowly. Mixing the two write patterns would destroy the sequential read characteristics the render feed depends on.

- Fixed 64-byte `EntityRecord` slots addressed by `entity_id` directly: `offset = entity_id × 64`.
- The block image references entities via the `entity_hint` field — a byte offset into the sidecar — so the render feed jumps from a block read to the entity record with one additional offset lookup, no join, no query.
- `write_entity()` / `read_entity()` / `delete_entity()` — O(1) upsert and lookup.
- `tick_delta(since_tick)` — all entities updated after a given engine tick; used by the render feed to build entity deltas.
- `entities_near(x, y, z, radius)` — spatial query for AI tick and render feed view frustum.
- `allocate_id()` — returns the lowest unused entity slot.

```python
from entity_sidecar import EntitySidecar, EntityRecord, EntityType, EntityFlags

sidecar = EntitySidecar("entities.img")
rec = EntityRecord(
    entity_id=1, entity_type=EntityType.PLAYER,
    flags=EntityFlags.ACTIVE | EntityFlags.VISIBLE,
    x=32.0, y=64.0, z=32.0, health=100.0, last_tick=42,
)
sidecar.write_entity(rec)
delta = sidecar.tick_delta(since_tick=40)   # entities changed since tick 40
```

### render_feed.py

Delta-only render feed. The only consumer of Array B. Computes the minimal set of changed blocks and entities a client needs to update its local world view — never sends full world state after the initial connection.

- Per-client `ClientView` tracks `last_block_seq`, `last_entity_tick`, current position, and view radius.
- Each tick: reads blocks in view radius from Array B with `write_seq > last_block_seq`; reads entity records from the sidecar with `tick > last_entity_tick`; packages both into a `RenderDelta`.
- `connect_client()` / `disconnect_client()` / `update_player_position()` — live client management.
- `RenderDelta` is transport-agnostic — serialise over any wire protocol.

```python
from render_feed import RenderFeed

feed = RenderFeed(layout, render_store, entity_sidecar, tick_rate_hz=20)
feed.connect_client(client_id=1, send_cb=my_send, view_radius=32,
                    initial_x=32.0, initial_y=64.0, initial_z=32.0)
feed.start()
feed.update_player_position(1, new_x, new_y, new_z)
```

### world_gen.py

Generates the initial flat block image chunk by chunk. All writes go through ResilientStore — journal, quorum, mirror forward — so generation is crash-resumable at any point. Kill the process mid-generation, restart, and it continues from where it stopped.

Terrain layers (bottom to top):

- `y < 2` → BEDROCK
- Below `surface − 4` → STONE (with seeded ore veins: iron and gold)
- `surface − 4` to `surface` → DIRT
- `surface` → GRASS (or SAND if at or below sea level)
- Above surface, below sea level → WATER
- Above surface → AIR

Terrain uses a deterministic SHA-256-based noise function — no external dependencies. The same seed always produces the same world, which makes crash-recovery validation straightforward: regenerate and diff.

```
python world_gen.py --size 64 --seed 42 --out world.img --array-b world_render.img
```

### run_server.py

Server loop: wires mutation engine, render feed, entity sidecar, and health monitor into a single running process.

- Synthetic player entity moves in a circle, evaluating `player_offset()` every tick.
- Block mutations (simulated mining) fire through Array A every 5 ticks.
- Render feed delivers deltas to connected clients at 20 Hz.
- Health report prints every 2 seconds showing Array A/B `write_seq` lag.

```
python run_server.py --array-a world.img --array-b world_render.img \
                     --sidecar entities.img --size 64 --duration 30
```

---

## Block State Machine

```
             write_block()
PENDING ──────────────────► CLEAN
   ▲                           │
   │  (crash replay,           │ replicate_block() quorum met
   │   block missing)          ▼
   │                      REPLICATED ◄── recovery_callback succeeds
   │
   │  replicate_block() quorum NOT met
CLEAN ◄──────────────────────────
   │
   │  read_block() checksum fail
   ▼
CORRUPTED
   │
   │  recovery_callback succeeds
   ▼
REPLICATED
```

---

## Block Format

Each block occupies exactly 16 bytes in the flat image. 16-byte alignment means every block offset is a power-of-2 multiple. NVMe page boundaries and chunk boundaries coincide for 16×16×16 chunk reads.

| Offset | Size | Field | Notes |
|--------|------|-------|-------|
| 0 | 1 B | block_type | uint8: 0=air, 1=stone, 2=dirt, 3=grass, 4=water … |
| 1 | 1 B | light_level | uint8: 0–15 |
| 2 | 1 B | flags | bit0=solid, bit1=transparent, bit2=modified |
| 3 | 1 B | reserved | |
| 4 | 4 B | metadata | uint32, type-specific payload |
| 8 | 8 B | entity_hint | uint64 byte offset into entity sidecar; 0 = no entity |

---

## Entity Record Format

Entity state lives in a parallel sidecar image, never in the block image. Each slot is 64 bytes, addressed directly by `entity_id × 64` — no index, no join.

| Offset | Size | Field |
|--------|------|-------|
| 0 | 4 B | entity_id (0 = empty slot) |
| 4 | 1 B | entity_type (0=empty, 1=player, 2=mob, 3=item, 4=projectile) |
| 5 | 1 B | flags (bit0=active, bit1=visible, bit2=collidable) |
| 6 | 2 B | reserved |
| 8 | 12 B | x, y, z (float32 position) |
| 20 | 12 B | vx, vy, vz (float32 velocity) |
| 32 | 8 B | yaw, pitch (float32) |
| 40 | 8 B | health, metadata (float32) |
| 48 | 8 B | owner_id (uint64) |
| 56 | 8 B | last_tick (uint64) |

---

## Flat Image Files

The engine operates on binary flat image files. There is no database layer. Each file is a direct-addressed binary store — a coordinate becomes a byte offset via arithmetic, and the read or write lands at that offset on the physical device. The `.db` extension is a legacy convention from early prototyping; these files are not SQLite databases and require no SQL engine to operate.

| File | Purpose |
|------|---------|
| world.img | Array A local block store (SparseBlockStore) |
| world_render.img | Array B render block store (RenderStore) |
| repl_log.bin | Persistent replication entry log (ReplicationManager) |
| state.bin | Block state index + write-ahead journal (ResilientStore) |
| entities.img | Entity sidecar (EntitySidecar) |

Files may be co-located or placed on separate physical volumes. The intended production configuration is `world.img` and `world_render.img` on separate NVMe devices — ideally directly connected to the datacenter rack — to fully realize the dual-array I/O isolation. No network storage, no filesystem overhead, no SQL parse layer: the storage array **is** the world.

> **External tool note:** SQL databases, ORMs, and query layers are not part of this engine and are not required for any core function. If a downstream system — analytics, observability, an asset pipeline — requires SQL access to block data, a read-only SQL adapter can be wired in externally without touching the engine's write path. The engine stays agnostic; the adapter bridges the gap. See `sql_store_adapter.py` for a reference implementation of this pattern.

---

## Getting Started

**1. Install dependencies:**

```
pip install -r requirements.txt
```

Standard library only for the storage layer — `zlib`, `hashlib`, `struct`, `threading`. No third-party packages required. No SQL engine. No ORM. Direct binary I/O only.

**2. Generate a world:**

```
python world_gen.py --size 64 --seed 42 --out world.img --array-b world_render.img
```

Writes a 64×64×64 block world through the full mutation engine stack. Both Array A and Array B are populated. Size is snapped to the nearest 16-block chunk boundary.

**3. Run the server:**

```
python run_server.py --array-a world.img --array-b world_render.img --sidecar entities.img --size 64
```

Runs at 20 Hz. Health report every 2 seconds. Ctrl-C for clean shutdown.

**4. Run the thin client:**

```
python client.py
```

**5. Run the dual-array wiring example:**

```
python example_dual_array.py
```

Demonstrates the dual-array setup in isolation — writes blocks through Array A, reads them back from Array B, prints the health report.

**6. Run module self-tests:**

```
python sparse_block_store.py
python replication_manager.py
python resilient_store.py
```

**7. Run an industry adapter (optional):**

```
python military_adapter.py --dry-run --origin-lat 38.8977 --origin-lon -77.0365
python autonomous_adapter.py --dry-run --framework ros2
```

Adapters are self-contained. They require a running engine instance (`run_server.py`) but do not affect the engine's core operation if disconnected.

---

## Design Notes

- The storage layer has no network I/O and no SQL dependency. Wire in your transport by supplying `sync_callback` and `recovery_callback` to ReplicationManager and ResilientStore. Wire in external tools via the adapter pattern described in the Industry Adapters section.
- `verify_integrity()` is a generator — drive it from a background thread or a low-priority maintenance loop. It will not stall reads or writes under any load condition.
- `max_blocks` should match the total block count of your flat world image so the engine enforces the same address space geometry as the underlying storage array.
- The async mirror forward in ResilientStore fires outside the write lock. Array B never adds latency to mutation throughput regardless of mirror count.
- Hardware I/O (`io_uring`, `O_DIRECT`) is stubbed for future integration. The logic layer is complete and hardware-independent.
- Entity spatial queries (`entities_near`) are linear scans in this prototype. Replace with an R-tree or spatial hash for production entity counts above a few thousand.
- Terrain noise uses SHA-256 digests as a portable, dependency-free substitute for Perlin/Simplex noise. Same seed, same world — deterministic for crash-recovery validation and regression testing.

---

## Proof-of-Concept Validation Checklist

- [ ] `world_gen` completes without error for `--size 64`
- [ ] Array A `write_seq` equals total block count after generation
- [ ] Array B `mirror_write_seq` converges to Array A `write_seq` within 1 second of generation completing
- [ ] Server loop runs for 10s with zero mirror DEGRADED events
- [ ] Kill server mid-generation, restart — journal replay produces identical block image
- [ ] `example_dual_array.py` reads all written blocks from Array B after writing through Array A
- [ ] Coordinate round-trip: `offset_to_coord(block_offset(x, y, z)) == (x, y, z)` for all valid coordinates
- [ ] Module self-tests pass: `sparse_block_store.py`, `replication_manager.py`, `resilient_store.py`

---

*This is a research prototype. The architecture is complete and the storage layer is production-quality. The game client, terrain generator, and simulation modules are proof-of-concept scaffolding to demonstrate the primitive. See module docstrings for detailed design notes and assumptions.*
