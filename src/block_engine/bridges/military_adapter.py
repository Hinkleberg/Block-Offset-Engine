"""
military_adapter.py — Defense & Military Simulation Adapter
============================================================

Bridges the Block-Image Engine's binary block / entity-sidecar API to the
DIS (Distributed Interactive Simulation, IEEE 1278) and HLA (High Level
Architecture, IEEE 1516) protocols used by military simulation tools such as
VBS4, OneSAF, JSAF, and JCATS.

This adapter is NOT part of the engine. It is an optional integration layer.
The engine operates identically whether this adapter is running or not.
The adapter can be connected, reconfigured, or removed at any time without
touching the engine core.

Architecture
------------
                   ┌──────────────────────────┐
  DIS / HLA  ◄────►│   MilitarySimAdapter      │◄────► Block-Image Engine
  multicast        │  (coord translation,      │       (binary block I/O,
  PDU stream       │   PDU ↔ EntityRecord,     │        entity sidecar,
                   │   terrain mutation,       │        render feed)
                   │   battlespace picture)    │
                   └──────────────────────────┘

Coordinate Systems
------------------
Military tools use ECEF geodetic (lat/lon/alt in WGS-84) or UTM.
The engine uses a flat integer-offset grid (x, y, z) × BLOCK_SIZE.
The adapter anchors a geodetic origin point and converts using a configurable
metres-per-block scale factor.

  engine_x = int((enu_east  - origin.east)  / scale_m)
  engine_y = int((enu_up    - origin.up)    / scale_m)
  engine_z = int((enu_north - origin.north) / scale_m)

Entity State (DIS PDU ↔ EntityRecord)
--------------------------------------
Incoming DIS Entity State PDUs are decoded and written to the engine's
entity sidecar. Outgoing entity deltas from tick_delta() are serialised
back to DIS Entity State PDUs for multicast broadcast.

Field mapping (DIS → EntityRecord):
  EntityIdentifier.entityID   → entity_id
  EntityType                  → entity_type  (mapped via DIS_ENTITY_TYPE_MAP)
  WorldCoordinates (ECEF)     → (x, y, z)    (converted via ENU anchor)
  VelocityVector              → (vx, vy, vz) (scaled to blocks/tick)
  Orientation (psi/theta/phi) → yaw, pitch
  Appearance                  → flags (active, visible, collidable)
  DeadReckoningParameters     → resolved position at current sim time

Terrain Mutation
----------------
DIS/HLA terrain-modification events (crater PDUs, defilade requests,
obstacle placement) are translated to write_block() calls through the
ResilientStore write path — full journal, quorum enforcement, and Array B
mirror forward included. The battlespace state is always crash-safe.

Battlespace Picture Isolation
------------------------------
Array A  → authoritative battlespace state (write path)
Array B  → common operating picture for commander terminals (read path)
The adapter exposes Array B's render feed as a DIS/HLA multicast stream.
Commander terminals see a consistent, post-quorum picture at all times.

Pluggable Algorithms
--------------------
The adapter exposes hooks for unit-specific algorithms supplied by the
military simulation tool. These are callbacks — the engine calls none of
them directly.

  los_callback(from_offset, to_offset) -> bool
      Line-of-sight check between two block offsets. Supply your own
      terrain masking, atmospheric, and sensor-degradation model.

  threat_callback(entity_record) -> float
      Threat score for a given entity (0.0 = no threat, 1.0 = high threat).
      Supply your own rules-of-engagement and force-identification model.

  terrain_effect_callback(offset, block_type) -> BlockData
      Called before a terrain mutation is committed. Allows the simulation
      to override or annotate the resulting block (e.g. add a crater flag,
      adjust passability metadata).

Usage
-----
    from military_adapter import MilitarySimAdapter, CoordOrigin

    adapter = MilitarySimAdapter(
        resilient_store=rs,
        entity_sidecar=sidecar,
        render_feed=feed,
        origin=CoordOrigin(lat=38.8977, lon=-77.0365, alt=0.0),
        scale_m_per_block=0.66,
        dis_port=3000,
        hla_federation="BattlespaceEngine",
        los_callback=my_los_fn,
        threat_callback=my_threat_fn,
    )
    adapter.start()

    # Dry-run / standalone test:
    #   python military_adapter.py --dry-run --origin-lat 38.8977 --origin-lon -77.0365

Notes
-----
- Requires a running engine instance (run_server.py) but does not affect
  core engine operation if the adapter is stopped or disconnected.
- The DIS/HLA network layer is stubbed here. Wire in your simulation
  network library (OpenDIS, portico, pitch, MAK RTI) via the
  dis_send_callback / hla_publish_callback parameters.
- For classified environments: the adapter itself contains no classified
  logic. Classification-sensitive LOS, threat, and terrain-effect
  algorithms are supplied by the caller via the callback hooks and never
  enter the engine or the adapter source.
- This adapter is not endorsed by or affiliated with any military
  organisation or simulation vendor.
"""

from __future__ import annotations

import math
import struct
import threading
import time
import argparse
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Coordinate translation
# ---------------------------------------------------------------------------

@dataclass
class CoordOrigin:
    """Geodetic anchor point that maps to engine offset (0, 0, 0)."""
    lat: float   # degrees WGS-84
    lon: float   # degrees WGS-84
    alt: float   # metres above ellipsoid


@dataclass
class ENUPosition:
    """Local East-North-Up position relative to CoordOrigin."""
    east: float
    north: float
    up: float


def geodetic_to_enu(lat: float, lon: float, alt: float,
                    origin: CoordOrigin) -> ENUPosition:
    """
    Convert WGS-84 geodetic coordinates to local ENU relative to origin.
    Uses a flat-earth approximation valid for areas < ~500 km across.
    For larger theatres, substitute a full ECEF → ENU transform.
    """
    R_EARTH = 6_378_137.0  # metres
    lat_rad = math.radians(lat)
    origin_lat_rad = math.radians(origin.lat)

    dlat = math.radians(lat - origin.lat)
    dlon = math.radians(lon - origin.lon)

    north = dlat * R_EARTH
    east  = dlon * R_EARTH * math.cos(origin_lat_rad)
    up    = alt - origin.alt
    return ENUPosition(east=east, north=north, up=up)


def enu_to_engine_offset(enu: ENUPosition, scale_m: float,
                         world_x: int, world_y: int, world_z: int,
                         block_size: int = 16) -> Optional[int]:
    """
    Convert ENU position to a flat engine byte offset.
    Returns None if the position is outside the world bounds.
    """
    x = int(enu.east  / scale_m)
    y = int(enu.up    / scale_m)
    z = int(enu.north / scale_m)
    if not (0 <= x < world_x and 0 <= y < world_y and 0 <= z < world_z):
        return None
    return (z * world_x * world_y + y * world_x + x) * block_size


def engine_offset_to_enu(offset: int, scale_m: float,
                          world_x: int, world_y: int,
                          block_size: int = 16) -> ENUPosition:
    """Inverse: engine byte offset → local ENU position."""
    block_index = offset // block_size
    x = block_index % world_x
    y = (block_index // world_x) % world_y
    z = block_index // (world_x * world_y)
    return ENUPosition(
        east  = x * scale_m,
        north = z * scale_m,
        up    = y * scale_m,
    )


# ---------------------------------------------------------------------------
# DIS entity type mapping
# ---------------------------------------------------------------------------

class MilEntityType(IntEnum):
    """Engine-side entity type codes for military objects."""
    UNKNOWN     = 0
    DISMOUNTED  = 1    # infantry / dismounted unit
    GROUND      = 2    # ground vehicle
    ROTARY      = 3    # rotary-wing aircraft
    FIXED_WING  = 4    # fixed-wing aircraft
    WATERCRAFT  = 5
    MUNITION    = 6
    SENSOR      = 7
    LOGISTICS   = 8


# DIS EntityKind / Domain / Category → MilEntityType
# Extend this map to match your simulation's entity type catalogue.
DIS_ENTITY_TYPE_MAP: dict[tuple[int, int, int], MilEntityType] = {
    (1, 1, 0): MilEntityType.GROUND,       # platform, land, misc
    (1, 1, 1): MilEntityType.GROUND,       # platform, land, tank
    (1, 1, 2): MilEntityType.GROUND,       # platform, land, APC
    (1, 2, 0): MilEntityType.FIXED_WING,   # platform, air, misc
    (1, 2, 1): MilEntityType.FIXED_WING,   # platform, air, fighter
    (1, 2, 20): MilEntityType.ROTARY,      # platform, air, rotary
    (1, 3, 0): MilEntityType.WATERCRAFT,   # platform, surface, misc
    (3, 0, 0): MilEntityType.MUNITION,     # munition
    (7, 0, 0): MilEntityType.SENSOR,       # sensor/emitter
}


def dis_kind_to_mil_entity_type(kind: int, domain: int,
                                 category: int) -> MilEntityType:
    return DIS_ENTITY_TYPE_MAP.get(
        (kind, domain, category),
        DIS_ENTITY_TYPE_MAP.get((kind, domain, 0), MilEntityType.UNKNOWN)
    )


# ---------------------------------------------------------------------------
# Stub PDU structures
# (Replace with your DIS/HLA network library's decoded types)
# ---------------------------------------------------------------------------

@dataclass
class EntityStatePDU:
    """
    Minimal DIS Entity State PDU representation.
    In production, deserialise from your DIS network library
    (e.g. open-dis/open-dis-python).
    """
    entity_id: int
    force_id: int                      # 1=friendly, 2=opposing, 3=neutral
    entity_kind: int
    entity_domain: int
    entity_category: int
    lat: float
    lon: float
    alt: float
    vx: float = 0.0                    # m/s east
    vy: float = 0.0                    # m/s up
    vz: float = 0.0                    # m/s north
    yaw: float = 0.0                   # radians
    pitch: float = 0.0
    health: float = 1.0                # 0.0–1.0
    is_active: bool = True


@dataclass
class TerrainMutationPDU:
    """Terrain modification event from DIS/HLA."""
    lat: float
    lon: float
    alt: float
    mutation_type: str   # "crater", "defilade", "obstacle", "clear"
    radius_m: float = 1.0


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class MilitarySimAdapter:
    """
    Adapter between the Block-Image Engine and DIS/HLA military simulation.

    Parameters
    ----------
    resilient_store
        ResilientStore instance (Array A). All terrain mutations write here.
    entity_sidecar
        EntitySidecar instance. Entity state is read and written here.
    render_feed
        RenderFeed instance. Outgoing battlespace picture is sourced here.
    origin
        Geodetic anchor: the real-world point that maps to engine offset 0.
    scale_m_per_block
        Metres per block edge. Default 0.66 m (engine standard resolution).
    world_x, world_y, world_z
        Engine world dimensions in blocks.
    dis_port
        UDP port for incoming DIS PDU stream. Default 3000.
    hla_federation
        HLA federation name for RTI connection. None to disable HLA.
    dis_send_callback
        (offset: int, data: bytes) → None. Supplies serialised DIS PDUs to
        the network layer. Stub: prints to console if not provided.
    hla_publish_callback
        (topic: str, data: bytes) → None. HLA publication hook.
    los_callback
        (from_offset: int, to_offset: int) → bool. Line-of-sight check.
        Default: always True (no masking). Replace with terrain-aware LOS.
    threat_callback
        (entity_id: int, entity_type: int) → float. 0.0–1.0 threat score.
        Default: always 0.0.
    terrain_effect_callback
        (offset: int, mutation_type: str) → bytes | None.
        Override block data before terrain mutation commits.
        Return None to use the default block encoding for the mutation type.
    """

    def __init__(
        self,
        resilient_store,
        entity_sidecar,
        render_feed,
        origin: CoordOrigin,
        scale_m_per_block: float = 0.66,
        world_x: int = 64,
        world_y: int = 64,
        world_z: int = 64,
        dis_port: int = 3000,
        hla_federation: Optional[str] = None,
        dis_send_callback: Optional[Callable] = None,
        hla_publish_callback: Optional[Callable] = None,
        los_callback: Optional[Callable] = None,
        threat_callback: Optional[Callable] = None,
        terrain_effect_callback: Optional[Callable] = None,
    ):
        self.rs            = resilient_store
        self.sidecar       = entity_sidecar
        self.feed          = render_feed
        self.origin        = origin
        self.scale_m       = scale_m_per_block
        self.world_x       = world_x
        self.world_y       = world_y
        self.world_z       = world_z
        self.dis_port      = dis_port
        self.hla_federation = hla_federation

        # Network callbacks — wire in your DIS/HLA library here
        self._dis_send   = dis_send_callback   or self._default_dis_send
        self._hla_pub    = hla_publish_callback or self._default_hla_pub

        # Pluggable algorithm hooks
        self._los_fn     = los_callback         or (lambda a, b: True)
        self._threat_fn  = threat_callback       or (lambda eid, etype: 0.0)
        self._terrain_fn = terrain_effect_callback or (lambda off, mt: None)

        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the adapter's background processing thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop,
                                        name="MilAdapter", daemon=True)
        self._thread.start()
        print(f"[MilitarySimAdapter] Started. DIS port={self.dis_port}, "
              f"HLA federation={self.hla_federation!r}")

    def stop(self):
        """Stop the adapter. Does not affect the engine."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        print("[MilitarySimAdapter] Stopped.")

    def _run_loop(self):
        """
        Main adapter loop. In production, replace the stub tick with:
          - a DIS UDP socket reader that calls ingest_entity_pdu()
          - an HLA federate ambassador that calls ingest_entity_pdu()
          - a tick that calls broadcast_battlespace_picture()
        """
        tick = 0
        while self._running:
            time.sleep(0.05)   # 20 Hz loop matching the engine render feed
            tick += 1
            if tick % 40 == 0:   # every 2 s
                self.broadcast_battlespace_picture(since_tick=tick - 40)

    # ------------------------------------------------------------------
    # Inbound: DIS/HLA → engine
    # ------------------------------------------------------------------

    def ingest_entity_pdu(self, pdu: EntityStatePDU):
        """
        Translate a DIS Entity State PDU to an EntityRecord and write it
        to the engine's entity sidecar.
        """
        enu = geodetic_to_enu(pdu.lat, pdu.lon, pdu.alt, self.origin)
        offset = enu_to_engine_offset(
            enu, self.scale_m, self.world_x, self.world_y, self.world_z
        )
        if offset is None:
            return   # outside world bounds — ignore

        entity_type = dis_kind_to_mil_entity_type(
            pdu.entity_kind, pdu.entity_domain, pdu.entity_category
        )

        # Flags: active=bit0, visible=bit1, collidable=bit2
        flags = 0
        if pdu.is_active:    flags |= 0x01
        if pdu.force_id > 0: flags |= 0x02   # visible to all forces
        flags |= 0x04                          # collidable by default

        # Build a minimal binary EntityRecord (matches entity_sidecar.py format)
        # 64-byte record: entity_id(4) type(1) flags(1) reserved(2)
        #                 x,y,z float32(12) vx,vy,vz float32(12)
        #                 yaw,pitch float32(8) health,meta float32(8)
        #                 owner_id uint64(8) last_tick uint64(8)
        record = struct.pack(
            "<IBBHffffffffffffffQQ",
            pdu.entity_id,
            int(entity_type),
            flags,
            0,                            # reserved
            float(enu.east  / self.scale_m),   # x in blocks
            float(enu.up    / self.scale_m),   # y in blocks
            float(enu.north / self.scale_m),   # z in blocks
            pdu.vx / self.scale_m,             # vx blocks/s
            pdu.vy / self.scale_m,
            pdu.vz / self.scale_m,
            pdu.yaw,
            pdu.pitch,
            pdu.health * 100.0,               # health 0–100
            0.0,                              # metadata
            0,                                # owner_id
            int(time.monotonic() * 20),       # last_tick at 20 Hz
        )
        if self.sidecar is not None:
            self.sidecar.write_entity_raw(pdu.entity_id, record)

    def ingest_terrain_mutation(self, pdu: TerrainMutationPDU):
        """
        Translate a terrain mutation PDU to write_block() calls on the
        ResilientStore (Array A). Full journal + quorum path.
        """
        enu = geodetic_to_enu(pdu.lat, pdu.lon, pdu.alt, self.origin)
        radius_blocks = max(1, int(pdu.radius_m / self.scale_m))

        MUTATION_BLOCK_TYPES = {
            "crater":   0x10,   # custom block_type for crater
            "defilade": 0x11,
            "obstacle": 0x12,
            "clear":    0x01,   # stone (restore to passable terrain)
        }
        block_type = MUTATION_BLOCK_TYPES.get(pdu.mutation_type, 0x10)

        for dz in range(-radius_blocks, radius_blocks + 1):
            for dx in range(-radius_blocks, radius_blocks + 1):
                offset = enu_to_engine_offset(
                    ENUPosition(
                        east  = enu.east  + dx * self.scale_m,
                        north = enu.north + dz * self.scale_m,
                        up    = enu.up,
                    ),
                    self.scale_m, self.world_x, self.world_y, self.world_z
                )
                if offset is None:
                    continue

                # Allow terrain_effect_callback to override block data
                override = self._terrain_fn(offset, pdu.mutation_type)
                if override is not None:
                    block_data = override
                else:
                    # Minimal 16-byte block: type | light | flags | reserved | meta(4) | entity_hint(8)
                    block_data = struct.pack("<BBBBIxxxxxx",
                                             block_type, 0, 0, 0, 0)
                    block_data = block_data.ljust(16, b'\x00')

                if self.rs is not None:
                    self.rs.write_block(offset, block_data)

    # ------------------------------------------------------------------
    # Outbound: engine → DIS/HLA
    # ------------------------------------------------------------------

    def broadcast_battlespace_picture(self, since_tick: int = 0):
        """
        Read entity deltas from the engine sidecar and broadcast them as
        DIS Entity State PDUs. This is the Array B / render feed path —
        the common operating picture for commander terminals.
        """
        if self.sidecar is None:
            return

        deltas = self.sidecar.tick_delta(since_tick=since_tick)
        for record in deltas:
            pdu_bytes = self._entity_record_to_dis_pdu(record)
            self._dis_send(pdu_bytes)

    def query_los(self, from_lat: float, from_lon: float, from_alt: float,
                  to_lat: float,   to_lon: float,   to_alt: float) -> bool:
        """
        Line-of-sight check between two geodetic points.
        Translates to engine offsets and delegates to los_callback.
        """
        enu_from = geodetic_to_enu(from_lat, from_lon, from_alt, self.origin)
        enu_to   = geodetic_to_enu(to_lat,   to_lon,   to_alt,   self.origin)
        off_from = enu_to_engine_offset(enu_from, self.scale_m,
                                         self.world_x, self.world_y, self.world_z)
        off_to   = enu_to_engine_offset(enu_to,   self.scale_m,
                                         self.world_x, self.world_y, self.world_z)
        if off_from is None or off_to is None:
            return False
        return self._los_fn(off_from, off_to)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _entity_record_to_dis_pdu(self, record: bytes) -> bytes:
        """
        Convert a raw 64-byte EntityRecord to a serialised DIS Entity State PDU.
        In production, use your DIS library's PDU builder.
        This stub returns the raw record bytes prefixed with a 4-byte PDU type marker.
        """
        PDU_TYPE_ENTITY_STATE = b'\x01\x00\x01\x01'  # stub header
        return PDU_TYPE_ENTITY_STATE + record

    def _default_dis_send(self, pdu_bytes: bytes):
        """Default DIS send stub — prints PDU summary to console."""
        print(f"[DIS TX] {len(pdu_bytes)} bytes")

    def _default_hla_pub(self, topic: str, data: bytes):
        """Default HLA publish stub — prints topic and size to console."""
        print(f"[HLA PUB] topic={topic!r} size={len(data)}")


# ---------------------------------------------------------------------------
# CLI dry-run
# ---------------------------------------------------------------------------

def _dry_run(args):
    print("=== MilitarySimAdapter dry-run ===")
    origin = CoordOrigin(lat=args.origin_lat, lon=args.origin_lon, alt=0.0)

    adapter = MilitarySimAdapter(
        resilient_store=None,
        entity_sidecar=None,
        render_feed=None,
        origin=origin,
        scale_m_per_block=0.66,
        world_x=512, world_y=64, world_z=512,
    )

    # Test coordinate translation
    test_pdu = EntityStatePDU(
        entity_id=1001, force_id=1,
        entity_kind=1, entity_domain=1, entity_category=1,
        lat=args.origin_lat + 0.001,
        lon=args.origin_lon + 0.001,
        alt=50.0, health=1.0,
    )
    enu = geodetic_to_enu(test_pdu.lat, test_pdu.lon, test_pdu.alt, origin)
    offset = enu_to_engine_offset(enu, 0.66, 512, 64, 512)
    print(f"Test entity lat={test_pdu.lat:.4f} lon={test_pdu.lon:.4f}")
    print(f"  → ENU east={enu.east:.1f}m north={enu.north:.1f}m up={enu.up:.1f}m")
    print(f"  → Engine offset: {offset}")

    # Test terrain mutation (no-op without a real store)
    terrain_pdu = TerrainMutationPDU(
        lat=args.origin_lat + 0.0005,
        lon=args.origin_lon + 0.0005,
        alt=0.0, mutation_type="crater", radius_m=5.0,
    )
    enu_t = geodetic_to_enu(terrain_pdu.lat, terrain_pdu.lon,
                             terrain_pdu.alt, origin)
    t_offset = enu_to_engine_offset(enu_t, 0.66, 512, 64, 512)
    print(f"Terrain mutation '{terrain_pdu.mutation_type}' radius={terrain_pdu.radius_m}m")
    print(f"  → Centre offset: {t_offset}")
    print(f"  → Blocks affected: ~{int(math.pi * (terrain_pdu.radius_m/0.66)**2)}")
    print("=== dry-run complete ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Block-Image Engine — Military Simulation Adapter"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Run coordinate translation tests without a live engine")
    parser.add_argument("--origin-lat", type=float, default=38.8977,
                        help="Anchor latitude (default: Pentagon)")
    parser.add_argument("--origin-lon", type=float, default=-77.0365,
                        help="Anchor longitude")
    args = parser.parse_args()

    if args.dry_run:
        _dry_run(args)
    else:
        print("Supply resilient_store, entity_sidecar, and render_feed "
              "from run_server.py to run the adapter live.")
        print("Use --dry-run for a standalone coordinate translation test.")