# HARDWARE_AXIOMS.md
## Block Storage Spatial Engine — Hardware Deployment Contract

> This document defines the hardware axioms the Block Storage Spatial Engine is designed
> against. Every latency target, throughput ceiling, and isolation guarantee in this engine
> is a logical conclusion derived from these axioms — not a measured result from a deployed
> system. These axioms become measured results when a frame matching this specification is
> physically attached and instrumented.

---

## 1. Axiom Class: Storage Array

**Hardware:** Dell PowerMax 2500

The Dell PowerMax 2500 is the storage array class this engine is designed against. It is
the only storage component specified. Every architectural decision in the engine — the
dual-array isolation, the 16-byte block alignment, the direct-offset addressing model, the
async mirror design, the write-path/read-path physical separation — is a logical consequence
of how this array behaves at the frame level.

### 1.1 Array Architecture

The PowerMax 2500 is an end-to-end NVMe array built on a disaggregated scale-out
architecture with an internal 100 Gb/s InfiniBand fabric between nodes.

| Property | Value |
|---|---|
| Internal fabric | InfiniBand 100 Gb/s per port, dual redundant |
| Drive interface | Dual PCIe Gen 3 ×8 NVMe per drive |
| Drive type | Native NVMe TLC/QLC flash |
| Drive capacities | 3.84 TB, 7.68 TB, 15.36 TB, 30.72 TB |
| Maximum drives | 96 NVMe drives per array |
| Node pairs | 1 to 2 (PowerMax 2500) |
| Cache minimum | 896 GB raw DRAM per node pair |
| Cache maximum | 15.36 TB raw DRAM (full 2-node-pair config) |
| Vault strategy | Vault to Flash (NVMe SED flash modules) |
| Maximum raw capacity | ~1.6 PB raw / 8 PBe with 5:1 data reduction |
| Availability rating | 99.9999% (six nines) |

### 1.2 Why This Array Class

The PowerMax 2500 satisfies three properties that are non-negotiable for the engine's
design axioms:

**1. Sub-100 µs NVMe read latency is a hardware property of this array, not a tuning
target.** The array's end-to-end NVMe architecture — no SAS, no SATA, no protocol
translation anywhere in the I/O path — means the 100 µs design target is a conservative
expression of what the hardware delivers, not an optimistic one.

**2. The array's internal InfiniBand fabric can sustain the async mirror forward between
Array A and Array B without any I/O contention reaching either logical volume.** The dual
node-pair configuration with 100 Gb/s internal fabric has sufficient internal bandwidth
to run the write path and the mirror forward simultaneously at full throughput. This is
the hardware basis for the engine's claim that Array B never adds latency to Array A.

**3. The DRAM cache size (up to 15.36 TB) can absorb working sets that matter.** For
spatial workloads with locality — which is every workload this engine is designed for,
because spatial access is inherently local — a large DRAM cache means repeated reads of
hot spatial regions never reach the NVMe drives. This is the hardware basis for the
render feed's read latency being systematically lower than the 100 µs write-path target.

---

## 2. Axiom Class: Storage Volume Configuration

Two logical volumes are required. They must be on physically separate NVMe resources
within the array to realize the dual-array I/O isolation the engine is designed around.

### 2.1 Array A Volume (Write Array)

| Property | Requirement |
|---|---|
| Purpose | Hosts `world.db` (SparseBlockStore) and `state.db` (journal/ResilientStore) |
| I/O profile | Write-dominant: sequential WAL appends + random block writes |
| Access pattern | Sequential write bursts (world_gen), random writes (mutations), random reads (journal replay, integrity scan) |
| SQLite WAL behavior | WAL file grows under write load, checkpointed periodically; volume must sustain sustained sequential write throughput without latency spikes |
| Isolation requirement | Must not share physical NVMe resources with Array B volume |
| RAID recommendation | RAID 5 (8+1) or RAID 5 (12+1) — write performance with single-drive fault tolerance |

### 2.2 Array B Volume (Render/Read Array)

| Property | Requirement |
|---|---|
| Purpose | Hosts `world_render.db` (RenderStore) |
| I/O profile | Read-dominant: random reads at 20 Hz per connected client, sustained over long sessions |
| Access pattern | Random reads within a spatial locality window (view radius per client), low write rate (async mirror forward only) |
| Access locality | High — spatial reads cluster around player/agent positions; DRAM cache hit rate is high under normal operation |
| Isolation requirement | Must not share physical NVMe resources with Array A volume |
| RAID recommendation | RAID 5 (8+1) — read performance optimized |

### 2.3 Ancillary Volumes

`repl_log.db`, `state.db` (if separated), and `entities.db` are low-bandwidth ancillary
files. They may co-locate on either volume depending on operational preference. Entity
sidecar (`entities.db`) is write-heavy during high-frequency entity tick cycles and
benefits from Array A co-location.

---

## 3. Axiom Class: I/O Path

### 3.1 The Frame I/O Model

The engine's I/O model is a **direct-offset block operation**. A frame operation is:

```
coordinate → arithmetic → byte offset → block device read or write at that offset
```

There is no filesystem namespace traversal, no SQL query planner, no object store lookup,
no cache warming step, and no network round-trip between the coordinate and the data. The
SQLite layer is the only indirection, and it is local — SQLite in WAL mode on an NVMe
volume behaves as a structured direct-block-access layer, not a general-purpose database.

### 3.2 Effective I/O Unit

The engine's logical block is **16 bytes**. SQLite's default page size is **4 KB**. The
actual I/O unit the PowerMax 2500 services is therefore a 4 KB page containing up to
256 logical blocks. This is not inefficiency — it is alignment. A 16×16×16 chunk of
blocks occupies exactly 16×16×16×16 = 65,536 bytes = 16 × 4 KB pages, which aligns
precisely to NVMe page boundaries. Chunk reads are therefore a contiguous 16-page read
— a single sequential I/O from the array's perspective.

### 3.3 I/O Path Layers (Array A Write Path)

```
Engine write_block(offset, data)
    │
    ▼
SQLite WAL append (state.db — journal pre-commit)      ← 1 sequential write, 4 KB page
    │
    ▼
SQLite row insert/update (world.db — SparseBlockStore) ← 1 random write, 4 KB page
    │
    ▼
PowerMax 2500 NVMe volume (Array A)                    ← hardware services both writes
    │
    ▼ (async, outside write lock)
SQLite row insert/update (world_render.db — RenderStore) ← 1 random write, 4 KB page
    │
    ▼
PowerMax 2500 NVMe volume (Array B)                    ← hardware services mirror write
```

The write lock is released before the mirror forward fires. From the engine's perspective,
a committed write completes in two sequential I/Os to Array A. The mirror is infrastructure
that runs alongside the write path, not inside it.

### 3.4 I/O Path Layers (Array B Read Path)

```
RenderFeed.tick() → read_block(offset)
    │
    ▼
RenderStore.read_block(offset)
    │
    ├─ DRAM cache hit (PowerMax 2500 DRAM cache) → sub-10 µs
    │
    └─ DRAM cache miss → NVMe flash read → sub-100 µs
    │
    ▼
SHA-256 checksum verification (in-process)
    │
    ▼
Return block data to RenderFeed
```

The read path never touches Array A. The render feed's latency is bounded by PowerMax
DRAM cache hit rate for the working spatial set, not by write-path contention.

---

## 4. Axiom Class: Latency

### 4.1 The 100 µs Design Target

**The 100 µs direct-frame response target is a logical conclusion from the PowerMax 2500's
documented sub-100 µs NVMe read latency, not a measured result from this repository.**

The derivation is:

- Dell PowerMax 2500 rated read latency: **sub-100 µs** on NVMe flash (cache miss path)
- Engine write path overhead above bare hardware: journal WAL append + block store write = 2 × NVMe page writes
- Engine read path overhead above bare hardware: SQLite page read + SHA-256 verification ≈ negligible CPU cycles relative to I/O latency
- Therefore: a frame operation targeting 100 µs is targeting hardware-bounded latency, not software-bounded latency

This is the correct interpretation. The engine's software overhead is not the bottleneck
against this hardware. The hardware latency floor is the bottleneck, and 100 µs is a
conservative expression of it.

### 4.2 Latency Table (Logical Conclusions)

These are hardware-derived logical conclusions. They become measured results when a
named frame with the PowerMax 2500 is instrumented.

| Operation | Path | Expected latency basis |
|---|---|---|
| Block read, DRAM cache hit | Array B, DRAM | Sub-10 µs (DRAM access, PowerMax cache) |
| Block read, NVMe cache miss | Array B, NVMe flash | Sub-100 µs (rated NVMe latency) |
| Block write, local commit | Array A, WAL + block store | ~100–200 µs (2 × NVMe page write) |
| Block write, quorum commit | Array A + replica path | Replica transport dependent |
| Mirror forward (async) | Array A → Array B, internal fabric | ~100 µs (InfiniBand 100 Gb/s internal) |
| Entity sidecar read | Array A co-located | Sub-100 µs |
| Chunk read (16×16×16) | Array B, 16 × 4 KB sequential | Sub-100 µs (sequential NVMe) |

### 4.3 Latency Is Not the Same as Throughput

The 100 µs target is a **per-operation latency target**, not a throughput claim. Throughput
is a function of queue depth and parallelism. See DEPLOYMENT_CONTRACT.md §3 for the
throughput derivation from queue depth.

---

## 5. Axiom Class: Capacity

### 5.1 Engine Capacity on PowerMax 2500

| Deployment | Array A raw | Array B raw | Total raw | Engine address space |
|---|---|---|---|---|
| Minimum (1 node pair, 10 drives) | 38.4 TB | 38.4 TB | 76.8 TB | ~2.4 PB logical at 16B/block |
| Mid-range (1 node pair, 48 drives) | 184.3 TB | 184.3 TB | 368.6 TB | ~11.5 PB logical |
| Full (2 node pairs, 96 drives) | ~768 TB | ~768 TB | ~1.54 PB raw | ~48 PB logical address space |
| Full with 5:1 reduction | 8 PBe | 8 PBe | 8 PBe effective per array | Spatial field at engine resolution |

The engine's 16-byte block at 0.66 m × 0.66 m resolution means the address space at full
array capacity exceeds the engine's scale table in the README. The hardware is not the
spatial ceiling.

### 5.2 Block Alignment to PowerMax NVMe Page Geometry

| Engine unit | Size | NVMe alignment |
|---|---|---|
| Logical block | 16 bytes | 256 blocks per 4 KB NVMe page |
| SQLite page | 4 KB | 1:1 NVMe page alignment |
| Engine chunk | 16×16×16 blocks = 65,536 bytes | 16 × 4 KB NVMe pages, contiguous |
| Chunk read | 65,536 bytes sequential | Single sequential I/O to PowerMax |

This alignment is not accidental. The chunk boundary coinciding with NVMe page boundaries
means the most common spatial I/O pattern — reading all blocks in a local region — is
always a contiguous sequential read at the hardware level. The PowerMax 2500's NVMe
subsystem services sequential reads at maximum throughput.

---

## 6. Axiom Class: Durability and Crash Safety

### 6.1 Hardware Durability Basis

The PowerMax 2500's durability guarantees underpin the engine's crash safety model:

- **Vault to Flash** — in-flight writes are protected by NVMe SED flash vault modules on
  power loss. A write that has been acknowledged by the array is durable even if power is
  cut between the acknowledgement and the physical NAND write completing.
- **Dual-ported NVMe drives** — every drive has two independent I/O channels with automatic
  failover. A single channel failure is invisible to the engine.
- **Continuous data integrity checks** — the PowerMax performs background media scan and
  data integrity verification independently of the engine's own SHA-256 checksums. The
  engine's checksums and the array's hardware integrity layer are complementary, not
  redundant — they catch different failure classes.

### 6.2 Engine Durability Layers on This Hardware

| Layer | What it catches | Hardware dependency |
|---|---|---|
| SQLite WAL journal | Incomplete write on process crash | PowerMax Vault to Flash |
| SHA-256 per-block checksum | Silent bit corruption in NAND | Complementary to PowerMax media scan |
| Quorum enforcement | Node unavailability, replica loss | Independent of array hardware |
| Mirror write_seq tracking | Array B lag, render staleness | InfiniBand internal fabric reliability |
| PowerMax hardware integrity | Media errors, controller faults | PowerMax 2500 hardware |

The engine cannot silently corrupt on this hardware. A block that fails SHA-256 on read
triggers the recovery path. A block that the array fails to write triggers the journal
replay path. Both failure modes are handled before the caller sees bad data.

---

## 7. Axiom Class: Front-End Connectivity

**The front-end connectivity specification is not defined in this document.**

The engine's storage layer is hardware-agnostic at the block interface. The PowerMax 2500
supports the following front-end protocols, all of which are valid physical interfaces for
the front-end connection:

- 64 Gb/s FC / NVMe/FC (up to 64 ports per array)
- 32 Gb/s FC / NVMe/FC / FICON (up to 64 ports per array)
- 100 Gb/s Ethernet / iSCSI / NVMe/TCP (up to 32 ports per array)
- 25 Gb/s Ethernet / iSCSI / NVMe/TCP (up to 64 ports per array)

The specific protocol, port configuration, HBA type, fabric topology, and host-side driver
stack used to connect the front-end to the PowerMax 2500 are defined in a separate
specification. That specification is not part of this repository.

What is guaranteed by the engine's design: **the front-end connection does not add a
software layer between the coordinate arithmetic and the storage operation.** The
front-end is a physical transport, not a logical middleware layer. Whatever protocol
carries the I/O, the I/O itself is a direct-offset block operation against the PowerMax
2500 NVMe volumes described in §2.

---

## 8. Summary: What These Axioms Guarantee

| Claim | Axiom basis | Status |
|---|---|---|
| 100 µs frame operation latency | PowerMax 2500 sub-100 µs NVMe read latency | Design target — requires frame measurement to confirm |
| Zero write/read contention | Physically separate Array A and Array B volumes on separate NVMe resources | Logical conclusion from §2 volume isolation |
| No silent corruption | SHA-256 per block + PowerMax hardware integrity | Structural guarantee from engine design + hardware |
| Mirror forward never delays writes | InfiniBand 100 Gb/s internal fabric + async forward outside write lock | Logical conclusion from §1.2 and engine async design |
| Crash-safe writes | SQLite WAL journal + PowerMax Vault to Flash | Structural guarantee from §6 |
| Six nines availability | PowerMax 2500 99.9999% rated availability | Hardware specification |

---

*No hardware, SAN frame, or production backend is attached to this repository at present.
This document defines the axioms the engine is designed against. Measurements become
possible when a frame matching this specification is attached and instrumented per
DEPLOYMENT_CONTRACT.md.*
