# DEPLOYMENT_CONTRACT.md
## Block Storage Spatial Engine — Frame Operation Specification

> This document defines what a frame operation is, what the engine requires from the
> deployment environment, and what measurements must be produced before any performance
> claim in this repository becomes a reported result. The storage array is the Dell
> PowerMax 2500 as specified in HARDWARE_AXIOMS.md. The front-end connectivity
> specification is not part of this document.

---

## 1. The Frame Operation

### 1.1 Definition

A **frame operation** is the atomic unit of the engine's I/O model. Every interaction
between the engine and physical storage is expressed as a frame operation. There are
exactly two kinds:

**Frame Read**
```
coordinate (x, y, z)
    → offset = (z × WORLD_X × WORLD_Y + y × WORLD_X + x) × BLOCK_SIZE
    → block device read at byte address [offset, offset + 16)
    → 16-byte block record returned
    → SHA-256 checksum verified
```

**Frame Write**
```
coordinate (x, y, z), block data (16 bytes)
    → offset = (z × WORLD_X × WORLD_Y + y × WORLD_X + x) × BLOCK_SIZE
    → journal pre-commit (WAL append, state.db)
    → block device write at byte address [offset, offset + 16) (world.db)
    → write_seq increment + journal commit
    → async mirror forward to Array B (outside write lock)
```

### 1.2 What a Frame Operation Is Not

A frame operation is not:
- A SQL query
- A cache lookup
- A network request
- A filesystem path resolution
- A chunk streaming operation
- An object graph traversal
- A key-value lookup

The coordinate arithmetic is not a lookup. It is computation — three multiplications and
two additions producing a byte address. That address is the location of the data on
physical storage. There is no layer between the address and the hardware that interprets,
translates, routes, or caches it.

### 1.3 Block Record (16 bytes, fixed)

Every frame operation addresses exactly one block record. The record is always 16 bytes.
The record format is fixed and does not vary by block type, world size, or deployment
configuration.

| Byte offset | Size | Field | Type |
|---|---|---|---|
| 0 | 1 B | block_type | uint8 |
| 1 | 1 B | light_level | uint8 (0–15) |
| 2 | 1 B | flags | uint8 bitmask |
| 3 | 1 B | reserved | uint8 |
| 4 | 4 B | metadata | uint32 |
| 8 | 8 B | entity_hint | uint64 (byte offset into entity sidecar; 0 = none) |

### 1.4 Entity Frame Operation

Entity state is a parallel frame operation class against the sidecar image, not the block
image. An entity frame operation addresses a 64-byte entity record by:

```
entity_id → offset = entity_id × 64 → block device read/write at that offset
```

Entity frame operations never contend with block frame operations. They run on the same
Array A volume (co-located with `world.db` by default) via a separate SQLite connection.
The two write paths are structurally isolated at the connection level.

---

## 2. Deployment Requirements

### 2.1 Physical Requirements

| Requirement | Specification |
|---|---|
| Storage array | Dell PowerMax 2500 (see HARDWARE_AXIOMS.md) |
| Array A volume | Dedicated NVMe LUN on PowerMax 2500, separate physical drives from Array B |
| Array B volume | Dedicated NVMe LUN on PowerMax 2500, separate physical drives from Array A |
| Front-end connectivity | Unspecified — see front-end specification (separate document) |
| Host operating system | Linux preferred; engine is OS-agnostic at the storage layer |

### 2.2 Volume Configuration Requirements

**Array A (world.db, state.db)**

| Parameter | Requirement | Basis |
|---|---|---|
| LUN block size | 4 KB | SQLite default page size alignment |
| SQLite WAL mode | Required | Concurrent read access during writes |
| SQLite page size | 4096 bytes | NVMe page alignment |
| SQLite journal mode | WAL | Required for engine crash safety |
| Filesystem | xfs or ext4 with `noatime` | Minimize metadata write amplification |
| Mount options | `noatime,nodiratime,data=ordered` | Reduce non-payload I/O |
| O_DIRECT | Optional (future) | io_uring stub present in engine; not yet active |

**Array B (world_render.db)**

| Parameter | Requirement | Basis |
|---|---|---|
| LUN block size | 4 KB | SQLite page alignment |
| SQLite WAL mode | Required | Read-only consumer path |
| Read connection | Read-only SQLite connection | RenderStore never writes during read |
| Isolation | No write I/O from render feed | Engine design guarantee |

### 2.3 SQLite Page Size and NVMe Alignment

The engine's logical block is 16 bytes. The actual I/O unit is the SQLite page (4 KB
default). The relationship is:

```
1 SQLite page = 4,096 bytes = 256 logical blocks = 256 × 16 bytes
1 engine chunk = 16×16×16 blocks = 4,096 blocks = 16 SQLite pages = 65,536 bytes
```

A full chunk read is 16 contiguous SQLite pages — a single contiguous I/O at the NVMe
level. The PowerMax 2500's NVMe subsystem services contiguous I/O at maximum sequential
throughput. Chunk reads are therefore the most efficient I/O pattern the engine produces,
and they are the dominant pattern for render feed operations.

**Recommended SQLite page size for production deployment:** 4096 bytes (default). Test
8192 and 16384 on the actual frame to determine optimal alignment for the specific LUN
geometry the PowerMax 2500 presents.

### 2.4 What the Engine Does Not Require

The engine does not require — and must not have inserted between it and the PowerMax 2500:

- A SAN abstraction layer presenting a virtual volume
- A filesystem cache (page cache is acceptable; application-level cache is not needed)
- A middleware caching tier (Memcached, Redis, Varnish, or equivalent)
- A database proxy or connection pooler
- A network file protocol (NFS, SMB, CIFS)
- A streaming chunk manager
- An object storage gateway

Each of these adds latency above the hardware floor. The engine's 100 µs target is derived
from hardware-direct I/O. Any software layer inserted above the NVMe driver and below the
engine's SQLite connection degrades that target.

---

## 3. Throughput Model

### 3.1 Latency-Derived Throughput Ceilings

At the 100 µs design target (serialized, single operation):

| Queue model | Theoretical ceiling | Basis |
|---|---|---|
| 1 serialized queue | 10,000 frame ops/sec | 1 / 100 µs |
| 16 independent queues | 160,000 frame ops/sec | 16 × (1 / 100 µs) |
| 64 independent queues | 640,000 frame ops/sec | 64 × (1 / 100 µs) |

These are latency-derived arithmetic ceilings. They are not IOPS claims against the
PowerMax 2500. The PowerMax 2500 is capable of far more IOPS than this model consumes.
The ceiling is the engine's current queue depth model, not the hardware limit.

The hardware is not the bottleneck in the engine's current queue model. The engine's
throughput ceiling will increase as queue depth and parallelism are expanded. The
PowerMax 2500 has headroom above any queue depth the engine's current Python prototype
can generate.

### 3.2 Read vs Write Throughput Asymmetry

Frame reads and frame writes have different throughput profiles on this hardware:

**Reads (Array B, RenderFeed):**
- Working set for a single player at view_radius=32: ~262,000 blocks = ~4 MB of logical
  data = ~1,000 SQLite pages = ~4 MB of NVMe reads
- At 20 Hz tick rate: 80 MB/sec sustained read throughput per client for a full-radius
  redraw (worst case — delta-only feed means actual read rate is a fraction of this)
- PowerMax 2500 DRAM cache (up to 15.36 TB) absorbs the spatial working set for any
  realistic player/agent count — repeated reads of the same region never reach NVMe flash
- Effective read latency for hot spatial regions: sub-10 µs (DRAM), not sub-100 µs (NVMe)

**Writes (Array A, mutation engine):**
- Mutation rate in the engine's server loop: every 5 ticks at 20 Hz = 4 mutations/sec
  (prototype rate — production rate scales with workload)
- Each mutation: 2 NVMe page writes (WAL + block store) = 2 × 4 KB = 8 KB per mutation
- At 640,000 frame ops/sec (64 queues, 100 µs): 5.12 GB/sec write throughput to Array A
- PowerMax 2500 sustains this without saturation

### 3.3 Mirror Forward Throughput

The async mirror forward from Array A to Array B runs outside the write lock on the
engine's mirror thread. Its throughput must keep pace with the Array A write rate to
prevent `mirror_write_seq` lag from growing unbounded.

| Condition | Mirror behavior |
|---|---|
| Write rate < mirror thread capacity | mirror_write_seq converges within 1 second |
| Write rate = mirror thread capacity | mirror_write_seq lag is bounded and stable |
| Write rate > mirror thread capacity | mirror_write_seq lag grows; MirrorHealthMonitor escalates to DEGRADED |

The PowerMax 2500's internal InfiniBand fabric (100 Gb/s) is not the mirror bottleneck.
The mirror bottleneck is the engine's single async queue thread. In production, the mirror
thread should be expanded to a pool proportional to the sustained write rate.

---

## 4. Operational State Machine

### 4.1 Block State

Every block in Array A has a state in the ResilientStore state machine:

```
PENDING → CLEAN → REPLICATED
                      ↑
              (recovery path)
CLEAN → CORRUPTED → REPLICATED
```

| State | Meaning | Hardware condition |
|---|---|---|
| PENDING | Write journaled, not yet committed to block store | WAL entry exists; NVMe write not yet acknowledged |
| CLEAN | Block written and checksummed in Array A | NVMe write acknowledged; SHA-256 verified |
| REPLICATED | Block confirmed on ≥ required_replicas nodes | Quorum enforced |
| CORRUPTED | Block fails SHA-256 on read | NVMe media error or bit corruption detected |

### 4.2 Mirror Health State

| Status | Condition | Action |
|---|---|---|
| HEALTHY | mirror_write_seq lag < 100 blocks | Normal operation |
| WARNING | lag ≥ 100 blocks | Log; monitor |
| DEGRADED | lag ≥ 500 blocks | Alert; investigate mirror thread |
| OFFLINE | No mirror progress for 30 seconds | Alert; render feed falls back to primary |

### 4.3 Array Health State

| Status | Condition |
|---|---|
| HEALTHY | All writes acking, quorum met, mirror converging |
| DEGRADED | Quorum met but replica count below desired; or mirror lag growing |
| CRITICAL | Quorum not met; writes failing |

---

## 5. I/O Demographics — Required Measurements

The following measurements must be produced before any throughput or latency number in
this repository is reported as a result rather than a design target. These measurements
require a physically attached Dell PowerMax 2500 with the volume configuration in §2.

### 5.1 Latency Measurements

| Metric | Required breakdown |
|---|---|
| p50 read latency | Array B, DRAM cache hit path |
| p95 read latency | Array B, DRAM cache hit path |
| p99 read latency | Array B, NVMe cache miss path |
| p99.9 read latency | Array B, NVMe cache miss path |
| p50 write latency | Array A, WAL + block store, local commit |
| p95 write latency | Array A, WAL + block store, local commit |
| p99 write latency | Array A, under sustained mutation load |
| Mirror forward latency | Array A commit → Array B mirror_write_seq increment |

All latency measurements must specify: command size (16 B logical / 4 KB physical),
queue depth, read/write mix, cache state (warm or cold), and percentile.

### 5.2 Throughput Measurements

| Metric | Required breakdown |
|---|---|
| Frame read IOPS | Queue depth: 1, 16, 64 |
| Frame write IOPS | Queue depth: 1, 16, 64 |
| Mixed read/write IOPS | 70/30 and 50/50 read/write mix |
| Chunk read throughput | 16×16×16 chunk, contiguous read, bytes/sec |
| Mirror forward throughput | Blocks/sec sustained, Array A → Array B |
| Render feed throughput | Clients × view_radius × 20 Hz, bytes/sec to client |

### 5.3 Locality Measurements

| Metric | Description |
|---|---|
| Same-block repeated read | Latency for N reads of identical offset (cache saturation) |
| Adjacent-block sequential read | Latency for sequential offset reads within one chunk |
| Radius scan | Latency for `blocks_in_range(cx, cy, cz, radius=32)` at cold and warm cache |
| Long traversal | Latency per block for a linear path crossing N chunks |
| Cross-chunk jump | Latency differential between in-chunk and cross-chunk reads |

### 5.4 Durability Measurements

| Metric | Description |
|---|---|
| Journal replay time | Time from process restart to first write_block() available |
| Crash recovery completeness | Block count post-recovery == expected block count |
| Checksum detection rate | Injected corrupt block detected on first read: 100% required |
| Mirror convergence time | Time from last write to mirror_write_seq == Array A write_seq |

### 5.5 Reporting Format

All measurements must be reported with:
- Hardware: Dell PowerMax 2500 (firmware version, node pair count, drive count, cache size)
- Front-end: [specified separately]
- Volume configuration: LUN block size, SQLite page size, filesystem, mount options
- Test tool: fio / custom benchmark / engine benchmark.py
- Workload: described in I/O terms (command size, queue depth, read/write ratio, duration)
- Environment: host CPU, RAM, OS, driver version

Until this table is populated from a physically attached and instrumented frame, the only
numerical statements in this repository are:
1. Logical capacity calculations derived from the 16-byte block model
2. The 100 µs direct-frame response target derived from PowerMax 2500 hardware axioms
3. Latency-derived throughput ceilings from §3.1

---

## 6. What This Contract Guarantees to a Hardware Partner

If a Dell PowerMax 2500 is presented to this engine with the volume configuration in §2
and the front-end connectivity per the separate specification, the engine guarantees:

1. **Every coordinate maps to exactly one unique byte offset.** The mapping is
   deterministic, collision-free, and reversible. `offset_to_coord(block_offset(x,y,z))
   == (x,y,z)` for all valid coordinates. This is proven by the coordinate round-trip
   test in the 26-test suite.

2. **No write is silently lost.** Every write is journaled before it touches the block
   store. If the process dies between the journal entry and the block store write, the
   journal replay on restart produces an identical block. The PowerMax Vault to Flash
   guarantees the journal entry itself survives power loss.

3. **No corrupt block is silently returned.** Every read verifies SHA-256. A block that
   fails checksum triggers the recovery path before data is returned to the caller. The
   caller never sees bad data.

4. **Array B never delays Array A.** The mirror forward runs outside the write lock on a
   separate thread. Array A write latency is not a function of Array B state. A degraded
   Array B is invisible to the write path.

5. **The engine introduces no software layer between the coordinate and the NVMe.**
   SQLite in WAL mode on a directly-mounted NVMe LUN is the only software between the
   engine's `block_offset()` arithmetic and the PowerMax 2500's NVMe controller.

6. **The engine is ready to measure.** The I/O demographics table in §5 is the test plan.
   Every row in that table has a corresponding code path in the engine that can be
   instrumented the moment a frame is attached.

---

*This document defines the deployment contract. Measurements become possible when a frame
matching the specification in §2 is physically attached. The engine is ready. The hardware
is the remaining variable.*
