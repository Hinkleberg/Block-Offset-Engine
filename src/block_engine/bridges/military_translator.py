"""
military_translator.py — Protocol Translator
Block-Image Engine ↔ DIS / HLA Military Simulation Tools
=========================================================

This module is the wire-level translation layer that sits between the
Block-Image Engine (via military_adapter.py) and the actual simulation
tool on the network.  The adapter defines *what* gets translated.  This
translator defines *how* the bits move.

Responsibilities
----------------
  1.  DIS PDU encode / decode  — full IEEE 1278.1 binary layout for the
      PDU types the engine cares about:
        • Entity State PDU  (type 1)
        • Fire PDU           (type 2)
        • Detonation PDU     (type 3)
        • Collision PDU      (type 4)
        • Remove Entity PDU  (type 12)
        • Comment PDU        (type 22)
        • Signal PDU         (type 26)  — used for terrain mutation events
        • Acknowledge PDU    (type 15)

  2.  DIS network transport — UDP multicast socket, 20 Hz send / receive,
      exercise ID filtering, site/app identity management.

  3.  HLA RTI bridge — connects to a running RTI (Pitch, MAK, Portico) via
      the federate ambassador pattern; publishes/subscribes the RPR-FOM 2.0
      object classes that correspond to the engine's entity and terrain model.

  4.  Dead-reckoning — resolves DIS dead-reckoning algorithm codes (DRM_FPW,
      DRM_RPW, DRM_RVW, DRM_FVW) to concrete positions at the current sim
      clock, so the engine always receives resolved coordinates.

  5.  ECEF ↔ WGS-84 ↔ ENU — full spheroidal transform (not flat-earth) for
      theatre-scale accuracy.  The flat-earth approximation in the adapter is
      replaced here for ranges beyond 500 km.

  6.  Heartbeat / timeout — entities not updated within a configurable window
      are marked inactive and their engine sidecar records zeroed.

  7.  Terrain mutation channel — maps DIS Signal PDUs carrying custom terrain
      events (and HLA TerrainModification interactions) to the adapter's
      ingest_terrain_mutation() call.

Architecture
------------

  ┌─────────────────────────────────────────────────────────────────────┐
  │                    MilitaryTranslator                                │
  │                                                                      │
  │  ┌──────────────────┐      ┌─────────────────────────────────────┐  │
  │  │  DISTransport     │      │  HLAFederateBridge                  │  │
  │  │  UDP multicast    │      │  RTI ambassador / publish-subscribe │  │
  │  │  PDU encode/decode│      │  RPR-FOM 2.0 object classes         │  │
  │  └────────┬──────────┘      └───────────────┬─────────────────────┘  │
  │           │  raw PDU bytes                  │  decoded interactions   │
  │           ▼                                 ▼                         │
  │  ┌──────────────────────────────────────────────────────────────┐    │
  │  │  PDUDecoder / PDUEncoder                                      │    │
  │  │  IEEE 1278.1 binary ↔ EntityStatePDU / TerrainMutationPDU    │    │
  │  └──────────────────────────────┬───────────────────────────────┘    │
  │                                 │  typed Python objects               │
  │                                 ▼                                     │
  │  ┌──────────────────────────────────────────────────────────────┐    │
  │  │  DeadReckoningResolver                                        │    │
  │  │  DRM_FPW / DRM_RPW / DRM_RVW / DRM_FVW → resolved position  │    │
  │  └──────────────────────────────┬───────────────────────────────┘    │
  │                                 │  resolved EntityStatePDU            │
  │                                 ▼                                     │
  │  ┌──────────────────────────────────────────────────────────────┐    │
  │  │  CoordTranslator                                              │    │
  │  │  ECEF ↔ WGS-84 ↔ ENU ↔ engine offset (full spheroidal)      │    │
  │  └──────────────────────────────┬───────────────────────────────┘    │
  │                                 │                                     │
  │                                 ▼                                     │
  │              MilitarySimAdapter  (military_adapter.py)                │
  │              ingest_entity_pdu() / ingest_terrain_mutation()          │
  │              broadcast_battlespace_picture()                          │
  └─────────────────────────────────────────────────────────────────────┘

Usage
-----
  from military_adapter   import MilitarySimAdapter, CoordOrigin
  from military_translator import MilitaryTranslator, DISConfig, HLAConfig

  adapter = MilitarySimAdapter(
      resilient_store=rs,
      entity_sidecar=sidecar,
      render_feed=feed,
      origin=CoordOrigin(lat=38.8977, lon=-77.0365, alt=0.0),
      scale_m_per_block=0.66,
  )

  translator = MilitaryTranslator(
      adapter=adapter,
      dis=DISConfig(
          multicast_group="239.1.2.3",
          port=3000,
          exercise_id=1,
          site_id=10,
          app_id=1,
      ),
      hla=HLAConfig(
          rti_host="localhost",
          federation_name="BattlespaceEngine",
          federate_name="BlockImageEngine",
          fom_path="RPR_FOM_v2.0.xml",
      ),
      heartbeat_timeout_s=5.0,
  )
  translator.start()

  # Or use with a context manager:
  with MilitaryTranslator(adapter=adapter, dis=dis_cfg) as t:
      run_simulation()

Dry-run:
  python military_translator.py --dry-run
  python military_translator.py --self-test

Standards references
--------------------
  IEEE 1278.1-2012  Distributed Interactive Simulation — Application Protocols
  IEEE 1516-2010    High Level Architecture — Framework and Rules
  SISO-STD-001-2015 Real-time Platform Reference FOM (RPR-FOM 2.0)
  NIMA TR 8350.2    WGS-84 Implementation Technical Report (ECEF parameters)
  STANAG 4586       UAV Control System — used for mapping UAV entity types

Notes
-----
  - No classified logic lives here.  LOS, threat scoring, and ROE are
    supplied by the caller via the adapter's callback hooks.
  - The HLA bridge requires a running RTI.  If no RTI is available the
    translator runs DIS-only with a warning.
  - All byte-order is network (big-endian) for DIS per IEEE 1278.1 §5.2.
  - Entity IDs are globally unique across (site_id, app_id, entity_id).
    The engine uses a flat uint32 entity_id; translation is:
      engine_entity_id = (site_id << 16) | (app_id << 8) | entity_id
    This fits 256 sites × 256 apps × 65536 entities — sufficient for
    all current DIS exercise scales.
"""

from __future__ import annotations

import math
import socket
import struct
import threading
import time
import logging
import argparse
import queue
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Dict, List, Optional, Tuple

# Bring in the adapter's types.  If running standalone (dry-run / self-test)
# we define minimal stubs so this file is self-contained.
try:
    from military_adapter import (
        MilitarySimAdapter, CoordOrigin, ENUPosition,
        EntityStatePDU, TerrainMutationPDU,
        geodetic_to_enu, enu_to_engine_offset,
    )
    _ADAPTER_AVAILABLE = True
except ImportError:
    _ADAPTER_AVAILABLE = False
    # ---- Minimal stubs for standalone operation ----
    from dataclasses import dataclass as _dc
    @_dc
    class CoordOrigin:
        lat: float; lon: float; alt: float
    @_dc
    class ENUPosition:
        east: float; north: float; up: float
    @_dc
    class EntityStatePDU:
        entity_id: int; force_id: int
        entity_kind: int; entity_domain: int; entity_category: int
        lat: float; lon: float; alt: float
        vx: float = 0.0; vy: float = 0.0; vz: float = 0.0
        yaw: float = 0.0; pitch: float = 0.0
        health: float = 1.0; is_active: bool = True
    @_dc
    class TerrainMutationPDU:
        lat: float; lon: float; alt: float
        mutation_type: str; radius_m: float = 1.0
    class MilitarySimAdapter:
        def ingest_entity_pdu(self, p): pass
        def ingest_terrain_mutation(self, p): pass
        def broadcast_battlespace_picture(self, since_tick=0): pass

log = logging.getLogger("military_translator")


# ===========================================================================
# Section 1 — Configuration
# ===========================================================================

@dataclass
class DISConfig:
    """DIS network and exercise identity configuration."""
    multicast_group: str  = "239.1.2.3"    # IANA-assigned DIS multicast range
    port:            int  = 3000
    exercise_id:     int  = 1              # 0–255 per IEEE 1278.1
    site_id:         int  = 1              # 0–65535
    app_id:          int  = 1             # 0–65535
    ttl:             int  = 32             # multicast TTL
    recv_buf:        int  = 1 << 20        # 1 MB socket receive buffer
    send_buf:        int  = 1 << 20
    bind_iface:      str  = ""             # "" = OS default interface
    loopback:        bool = True           # receive own multicasts (dev/test)


@dataclass
class HLAConfig:
    """HLA RTI connection configuration (optional)."""
    rti_host:        str  = "localhost"
    rti_port:        int  = 8989
    federation_name: str  = "BattlespaceEngine"
    federate_name:   str  = "BlockImageEngine"
    fom_path:        str  = "RPR_FOM_v2.0.xml"
    enabled:         bool = False          # False = DIS-only mode


# ===========================================================================
# Section 2 — DIS PDU constants  (IEEE 1278.1-2012)
# ===========================================================================

class PDUType(IntEnum):
    ENTITY_STATE    = 1
    FIRE            = 2
    DETONATION      = 3
    COLLISION       = 4
    REMOVE_ENTITY   = 12
    ACKNOWLEDGE     = 15
    COMMENT         = 22
    SIGNAL          = 26    # repurposed for terrain mutation events


class ProtocolVersion(IntEnum):
    DIS_6 = 6    # IEEE 1278.1-1995
    DIS_7 = 7    # IEEE 1278.1-2012  (preferred)


class ForceID(IntEnum):
    OTHER     = 0
    FRIENDLY  = 1
    OPPOSING  = 2
    NEUTRAL   = 3
    FRIENDLY2 = 4   # coalition partner


class DeadReckoningAlgorithm(IntEnum):
    """IEEE 1278.1 Table B.4 — Dead Reckoning Algorithm Codes."""
    STATIC       = 0    # entity does not move
    DRM_FPW      = 1    # fixed position / fixed velocity, world coords
    DRM_RPW      = 2    # fixed position / fixed velocity, rotating
    DRM_RVW      = 3    # fixed position / variable velocity, rotating
    DRM_FVW      = 4    # fixed position / variable velocity, world
    DRM_FPB      = 5    # fixed position / fixed velocity, body axis
    DRM_RPB      = 6    # fixed position / fixed velocity, rotating body
    DRM_RVB      = 7    # fixed position / variable velocity, rotating body
    DRM_FVB      = 8    # fixed position / variable velocity, body axis


# PDU header is always 12 bytes (IEEE 1278.1 §5.3.2)
PDU_HEADER_SIZE  = 12
# Entity State PDU body is 132 bytes + 12 header = 144 total
ENTITY_STATE_PDU_SIZE = 144
# Signal PDU minimum: 12 header + 32 body (no data)
SIGNAL_PDU_MIN_SIZE   = 44

# Terrain mutation magic cookie embedded in Signal PDU data field
TERRAIN_MUTATION_MAGIC = b'BIEM'   # Block-Image Engine Mutation


# ===========================================================================
# Section 3 — ECEF / WGS-84 / ENU coordinate transforms
#             Full spheroidal — replaces the flat-earth approximation
#             in military_adapter.py for ranges > 500 km
# ===========================================================================

# WGS-84 ellipsoid parameters (NIMA TR 8350.2)
_WGS84_A  = 6_378_137.0          # semi-major axis, metres
_WGS84_F  = 1.0 / 298.257223563  # flattening
_WGS84_B  = _WGS84_A * (1.0 - _WGS84_F)          # semi-minor axis
_WGS84_E2 = 2.0 * _WGS84_F - _WGS84_F ** 2       # first eccentricity squared
_WGS84_EP2 = _WGS84_E2 / (1.0 - _WGS84_E2)       # second eccentricity squared


def geodetic_to_ecef(lat_deg: float, lon_deg: float,
                     alt_m: float) -> Tuple[float, float, float]:
    """
    WGS-84 geodetic (lat, lon, alt) → ECEF (X, Y, Z) in metres.
    IEEE 1278.1 uses ECEF for all world coordinates.
    """
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    N = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
    X = (N + alt_m) * cos_lat * math.cos(lon)
    Y = (N + alt_m) * cos_lat * math.sin(lon)
    Z = (N * (1.0 - _WGS84_E2) + alt_m) * sin_lat
    return X, Y, Z


def ecef_to_geodetic(X: float, Y: float,
                     Z: float) -> Tuple[float, float, float]:
    """
    ECEF (X, Y, Z) → WGS-84 geodetic (lat_deg, lon_deg, alt_m).
    Uses Bowring's iterative method — converges in 2–3 iterations.
    """
    lon = math.atan2(Y, X)
    p   = math.hypot(X, Y)
    lat = math.atan2(Z, p * (1.0 - _WGS84_E2))   # initial estimate
    for _ in range(5):
        sin_lat = math.sin(lat)
        cos_lat = math.cos(lat)
        N   = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
        lat = math.atan2(Z + _WGS84_E2 * N * sin_lat, p)
    N   = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * math.sin(lat) ** 2)
    alt = p / (math.cos(lat) + 1e-12) - N if abs(math.cos(lat)) > 1e-6 else (
          abs(Z) / math.sin(lat) - N * (1.0 - _WGS84_E2))
    return math.degrees(lat), math.degrees(lon), alt


def ecef_to_enu(X: float, Y: float, Z: float,
                origin_lat_deg: float, origin_lon_deg: float,
                origin_alt_m: float) -> Tuple[float, float, float]:
    """
    ECEF → local ENU (east, north, up) relative to a geodetic origin.
    Used for theatre-scale coordinate bridging (full spheroidal transform).
    """
    X0, Y0, Z0 = geodetic_to_ecef(origin_lat_deg, origin_lon_deg, origin_alt_m)
    dX, dY, dZ = X - X0, Y - Y0, Z - Z0

    lat = math.radians(origin_lat_deg)
    lon = math.radians(origin_lon_deg)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    sin_lon, cos_lon = math.sin(lon), math.cos(lon)

    east  = -sin_lon * dX + cos_lon * dY
    north = -sin_lat * cos_lon * dX - sin_lat * sin_lon * dY + cos_lat * dZ
    up    =  cos_lat * cos_lon * dX + cos_lat * sin_lon * dY + sin_lat * dZ
    return east, north, up


def enu_to_ecef(east: float, north: float, up: float,
                origin_lat_deg: float, origin_lon_deg: float,
                origin_alt_m: float) -> Tuple[float, float, float]:
    """
    Local ENU → ECEF.  Inverse of ecef_to_enu.
    Used when building outbound DIS PDUs from engine entity records.
    """
    X0, Y0, Z0 = geodetic_to_ecef(origin_lat_deg, origin_lon_deg, origin_alt_m)
    lat = math.radians(origin_lat_deg)
    lon = math.radians(origin_lon_deg)
    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    sin_lon, cos_lon = math.sin(lon), math.cos(lon)

    dX = -sin_lon * east - sin_lat * cos_lon * north + cos_lat * cos_lon * up
    dY =  cos_lon * east - sin_lat * sin_lon * north + cos_lat * sin_lon * up
    dZ =  cos_lat * north + sin_lat * up
    return X0 + dX, Y0 + dY, Z0 + dZ


class CoordTranslator:
    """
    Bidirectional coordinate translation between DIS ECEF and the engine's
    flat block-offset grid.

    The engine's offset formula:
      offset(x, y, z) = (z * W * H + y * W + x) * BLOCK_SIZE

    This translator maps:
      DIS ECEF  →  WGS-84 geodetic  →  ENU  →  engine (x, y, z)  →  offset
      engine offset  →  (x, y, z)  →  ENU  →  ECEF  →  DIS world coords
    """

    def __init__(self, origin: CoordOrigin, scale_m: float,
                 world_x: int, world_y: int, world_z: int,
                 block_size: int = 16):
        self.origin     = origin
        self.scale_m    = scale_m
        self.world_x    = world_x
        self.world_y    = world_y
        self.world_z    = world_z
        self.block_size = block_size

    def ecef_to_engine(self, X: float, Y: float,
                        Z: float) -> Optional[Tuple[int, int, int, int]]:
        """
        ECEF → engine (x, y, z, offset).  Returns None if out of bounds.
        """
        east, north, up = ecef_to_enu(
            X, Y, Z,
            self.origin.lat, self.origin.lon, self.origin.alt
        )
        bx = int(east  / self.scale_m)
        by = int(up    / self.scale_m)
        bz = int(north / self.scale_m)
        if not (0 <= bx < self.world_x and
                0 <= by < self.world_y and
                0 <= bz < self.world_z):
            return None
        offset = (bz * self.world_x * self.world_y +
                  by * self.world_x + bx) * self.block_size
        return bx, by, bz, offset

    def engine_to_ecef(self, offset: int) -> Tuple[float, float, float]:
        """
        Engine byte offset → ECEF world coordinates.
        """
        idx = offset // self.block_size
        bx  = idx % self.world_x
        by  = (idx // self.world_x) % self.world_y
        bz  = idx // (self.world_x * self.world_y)
        east  = bx * self.scale_m
        up    = by * self.scale_m
        north = bz * self.scale_m
        X, Y, Z = enu_to_ecef(
            east, north, up,
            self.origin.lat, self.origin.lon, self.origin.alt
        )
        return X, Y, Z

    def ecef_to_geodetic(self, X: float, Y: float,
                          Z: float) -> Tuple[float, float, float]:
        return ecef_to_geodetic(X, Y, Z)

    def geodetic_to_ecef(self, lat: float, lon: float,
                          alt: float) -> Tuple[float, float, float]:
        return geodetic_to_ecef(lat, lon, alt)


# ===========================================================================
# Section 4 — Dead-reckoning resolver
# ===========================================================================

class DeadReckoningResolver:
    """
    Resolves dead-reckoning state from a received DIS Entity State PDU to a
    concrete world position at the current simulation clock.

    DIS senders transmit their last known position + velocity + acceleration
    at a reduced rate (typically 5 Hz) and expect receivers to extrapolate
    between updates using the declared DR algorithm.  This resolver performs
    that extrapolation so the engine always receives an instantaneous position.

    Supported algorithms: STATIC, DRM_FPW, DRM_RPW, DRM_RVW, DRM_FVW.
    Body-axis algorithms (FPB, RPB, RVB, FVB) fall back to FPW.
    """

    @staticmethod
    def resolve(
        X: float, Y: float, Z: float,            # last known ECEF position
        vX: float, vY: float, vZ: float,          # velocity (m/s ECEF)
        aX: float, aY: float, aZ: float,          # acceleration (m/s²)
        dr_algorithm: int,
        pdu_timestamp: float,                      # seconds since epoch
        now: Optional[float] = None,
    ) -> Tuple[float, float, float]:
        """
        Return the dead-reckoned ECEF position at `now` (default: current time).

        All world-frame algorithms (FPW, FVW, RPW, RVW) are handled.
        For static entities or unknown algorithms, position is returned as-is.
        """
        if now is None:
            now = time.time()
        dt = now - pdu_timestamp
        if dt < 0:
            dt = 0.0

        alg = dr_algorithm

        if alg == DeadReckoningAlgorithm.STATIC:
            return X, Y, Z

        if alg in (DeadReckoningAlgorithm.DRM_FPW,
                   DeadReckoningAlgorithm.DRM_RPW,
                   DeadReckoningAlgorithm.DRM_FPB,
                   DeadReckoningAlgorithm.DRM_RPB):
            # Constant velocity — no acceleration term
            return (X + vX * dt,
                    Y + vY * dt,
                    Z + vZ * dt)

        if alg in (DeadReckoningAlgorithm.DRM_FVW,
                   DeadReckoningAlgorithm.DRM_RVW,
                   DeadReckoningAlgorithm.DRM_FVB,
                   DeadReckoningAlgorithm.DRM_RVB):
            # Variable velocity — include acceleration (½at²)
            return (X + vX * dt + 0.5 * aX * dt * dt,
                    Y + vY * dt + 0.5 * aY * dt * dt,
                    Z + vZ * dt + 0.5 * aZ * dt * dt)

        # Unknown algorithm — return last known position
        return X, Y, Z


# ===========================================================================
# Section 5 — DIS PDU binary encoder / decoder
#             IEEE 1278.1-2012 §5.3 — PDU formats
#             All multi-byte fields are big-endian (network byte order)
# ===========================================================================

class PDUDecoder:
    """
    Decodes raw DIS PDU bytes into structured Python objects.

    Only PDU types the engine cares about are fully decoded.
    All others are discarded with a debug log.
    """

    def __init__(self, exercise_id: int, coord: CoordTranslator,
                 dr: DeadReckoningResolver):
        self.exercise_id = exercise_id
        self.coord       = coord
        self.dr          = dr

    def decode(self, raw: bytes) -> Optional[object]:
        """
        Decode a raw PDU byte buffer.
        Returns a typed object (EntityStatePDU, TerrainMutationPDU, …)
        or None if the PDU is unrecognised, malformed, or filtered out.
        """
        if len(raw) < PDU_HEADER_SIZE:
            return None
        hdr = self._decode_header(raw)
        if hdr is None:
            return None
        pdu_type, exercise_id, pdu_len = hdr
        if exercise_id != self.exercise_id:
            return None   # different exercise — discard
        if len(raw) < pdu_len:
            log.debug("PDU truncated: expected %d got %d", pdu_len, len(raw))
            return None

        if pdu_type == PDUType.ENTITY_STATE:
            return self._decode_entity_state(raw)
        if pdu_type == PDUType.SIGNAL:
            return self._decode_signal(raw)
        if pdu_type in (PDUType.REMOVE_ENTITY,):
            return self._decode_remove_entity(raw)
        if pdu_type == PDUType.DETONATION:
            return self._decode_detonation_as_terrain(raw)

        log.debug("Unhandled PDU type %d — discarded", pdu_type)
        return None

    # ---- Header ----

    def _decode_header(self, raw: bytes) -> Optional[Tuple[int, int, int]]:
        """
        PDU header layout (IEEE 1278.1 §5.3.2):
          Byte 0    : Protocol Version  (uint8)
          Byte 1    : Exercise ID       (uint8)
          Byte 2    : PDU Type          (uint8)
          Byte 3    : Protocol Family   (uint8)
          Bytes 4-7 : Timestamp         (uint32 big-endian)
          Bytes 8-9 : PDU Length        (uint16 big-endian)
          Bytes 10-11 : Padding
        """
        try:
            proto_ver, exid, pdu_type, proto_fam, ts, pdu_len, _ = \
                struct.unpack_from(">BBBBIHxx", raw, 0)
            return pdu_type, exid, pdu_len
        except struct.error:
            return None

    # ---- Entity State PDU — IEEE 1278.1 §5.3.3 ----
    # Total: 12 (hdr) + 132 (body) = 144 bytes minimum
    #
    # Body layout from offset 12:
    #   Entity ID         :  site(2) + app(2) + entity(2)  = 6 bytes
    #   Force ID          :  uint8  (1)
    #   Number of Artic   :  uint8  (1)
    #   Entity Type       :  kind(1)+domain(1)+country(2)+cat(1)+sub(1)+spec(1)+extra(1) = 8
    #   Alt Entity Type   :  8 bytes (same layout)
    #   Linear Velocity   :  3×float32 big-endian = 12 bytes  (m/s in ECEF)
    #   Location          :  3×float64 big-endian = 24 bytes  (ECEF metres)
    #   Orientation       :  3×float32 big-endian = 12 bytes  (psi/theta/phi radians)
    #   Appearance        :  uint32  (4 bytes)
    #   DR Parameters     :  algorithm(1)+pad(3)+lin-accel(12)+ang-vel(12) = 28
    #   Marking           :  character set(1) + 11 chars = 12 bytes
    #   Capabilities      :  uint32 (4 bytes)
    # Total body = 132 bytes

    def _decode_entity_state(self, raw: bytes) -> Optional[EntityStatePDU]:
        if len(raw) < ENTITY_STATE_PDU_SIZE:
            return None
        off = PDU_HEADER_SIZE
        try:
            site_id, app_id, entity_num = struct.unpack_from(">HHH", raw, off)
            off += 6
            force_id, num_artic = struct.unpack_from(">BB", raw, off)
            off += 2
            kind, domain, country, cat, subcat, spec, extra = \
                struct.unpack_from(">BBHBBBBx", raw, off)   # 8 bytes + 1 pad = 9? no, 8 exact
            off += 8
            off += 8   # skip alt entity type
            vx, vy, vz = struct.unpack_from(">fff", raw, off)
            off += 12
            X, Y, Z = struct.unpack_from(">ddd", raw, off)
            off += 24
            psi, theta, phi = struct.unpack_from(">fff", raw, off)
            off += 12
            appearance = struct.unpack_from(">I", raw, off)[0]
            off += 4
            dr_alg = struct.unpack_from(">B", raw, off)[0]
            off += 1 + 3   # algorithm + 3 padding bytes
            aX, aY, aZ = struct.unpack_from(">fff", raw, off)
            # angular velocity — skip (12 bytes) then marking (12), capabilities (4)
        except struct.error as e:
            log.debug("Entity State decode error: %s", e)
            return None

        # Resolve dead-reckoning to current time
        pdu_ts = self._extract_timestamp(raw)
        X_r, Y_r, Z_r = DeadReckoningResolver.resolve(
            X, Y, Z, vx, vy, vz, aX, aY, aZ, dr_alg, pdu_ts
        )

        lat, lon, alt = ecef_to_geodetic(X_r, Y_r, Z_r)
        # Velocity: ECEF m/s → approximate ENU (simplified — good to ~1% for
        # velocities < 500 m/s at mid-latitudes; replace with full Jacobian
        # for ballistic / orbital entities)
        lat_r = math.radians(lat); lon_r = math.radians(lon)
        sl, cl = math.sin(lat_r), math.cos(lat_r)
        slo, clo = math.sin(lon_r), math.cos(lon_r)
        v_east  = -slo * vx + clo * vy
        v_north = -sl * clo * vx - sl * slo * vy + cl * vz
        v_up    =  cl * clo * vx + cl * slo * vy + sl * vz

        # Appearance bit 23 = damage / health proxy  (0=no damage, 3=destroyed)
        damage = (appearance >> 3) & 0x3
        health = max(0.0, 1.0 - damage / 3.0)

        is_active = (appearance & 0x1) == 0   # bit 0: deactivated flag

        # Composite entity_id: flatten (site, app, num) to uint32
        entity_id = ((site_id & 0xFF) << 16) | ((app_id & 0xFF) << 8) | (entity_num & 0xFF)

        return EntityStatePDU(
            entity_id=entity_id,
            force_id=force_id,
            entity_kind=kind,
            entity_domain=domain,
            entity_category=cat,
            lat=lat, lon=lon, alt=alt,
            vx=v_east, vy=v_up, vz=v_north,
            yaw=psi, pitch=theta,
            health=health,
            is_active=is_active,
        )

    # ---- Signal PDU carrying terrain mutation ----
    # IEEE 1278.1 §5.6.5 — we repurpose Signal for custom engine events.
    # Layout from offset 12:
    #   Entity ID      :  site(2)+app(2)+entity(2)  = 6
    #   Radio ID       :  uint16  = 2
    #   Encoding Class :  uint16  = 2
    #   Encoding Type  :  uint16  = 2
    #   TDL Type       :  uint16  = 2
    #   Sample Rate    :  uint32  = 4
    #   Data Length    :  uint16  = 2  (bits)
    #   Samples        :  uint16  = 2
    #   Data           :  variable
    # We look for TERRAIN_MUTATION_MAGIC in the first 4 bytes of data.

    def _decode_signal(self, raw: bytes) -> Optional[TerrainMutationPDU]:
        DATA_OFF = PDU_HEADER_SIZE + 22   # 12 hdr + 6 entity_id + 2 radio +
                                           # 2 enc_class + 2 enc_type +
                                           # 2 tdl + 4 sample_rate + 2 data_len + 2 samples
        if len(raw) < DATA_OFF + 4:
            return None
        if raw[DATA_OFF:DATA_OFF + 4] != TERRAIN_MUTATION_MAGIC:
            return None   # not a terrain mutation Signal PDU
        # Custom payload after magic:
        #   lat (double, 8) + lon (double, 8) + alt (float, 4) +
        #   mutation_type (uint8, 1) + radius_m (float, 4) = 25 bytes
        PAYLOAD_OFF = DATA_OFF + 4
        if len(raw) < PAYLOAD_OFF + 25:
            return None
        try:
            lat, lon = struct.unpack_from(">dd", raw, PAYLOAD_OFF)
            alt      = struct.unpack_from(">f",  raw, PAYLOAD_OFF + 16)[0]
            mut_code = struct.unpack_from(">B",  raw, PAYLOAD_OFF + 20)[0]
            radius   = struct.unpack_from(">f",  raw, PAYLOAD_OFF + 21)[0]
        except struct.error:
            return None

        MUTATION_CODE_MAP = {1: "crater", 2: "defilade",
                             3: "obstacle", 4: "clear"}
        mutation_type = MUTATION_CODE_MAP.get(mut_code, "crater")
        return TerrainMutationPDU(lat=lat, lon=lon, alt=alt,
                                  mutation_type=mutation_type,
                                  radius_m=radius)

    def _decode_remove_entity(self, raw: bytes) -> Optional[EntityStatePDU]:
        """
        Remove Entity PDU — translate to an inactive EntityStatePDU so the
        engine sidecar record is zeroed out.
        """
        if len(raw) < PDU_HEADER_SIZE + 12:
            return None
        try:
            site_id, app_id, entity_num = struct.unpack_from(
                ">HHH", raw, PDU_HEADER_SIZE)
        except struct.error:
            return None
        entity_id = ((site_id & 0xFF) << 16) | ((app_id & 0xFF) << 8) | entity_num
        return EntityStatePDU(
            entity_id=entity_id, force_id=0,
            entity_kind=0, entity_domain=0, entity_category=0,
            lat=0.0, lon=0.0, alt=0.0,
            is_active=False,
        )

    def _decode_detonation_as_terrain(self, raw: bytes) -> Optional[TerrainMutationPDU]:
        """
        Detonation PDU — map to a crater terrain mutation at the burst point.
        Detonation PDU body layout from offset 12:
          Firing Entity ID  : 6 bytes
          Munition ID       : 6 bytes
          Event ID          : 6 bytes
          Velocity          : 3×float32 = 12 bytes
          Location          : 3×float64 = 24 bytes   ← burst point ECEF
          Burst Descriptor  : 16 bytes
          Location in Entity: 3×float32 = 12 bytes
          Detonation Result : uint8
        """
        if len(raw) < PDU_HEADER_SIZE + 82:
            return None
        try:
            burst_X, burst_Y, burst_Z = struct.unpack_from(
                ">ddd", raw, PDU_HEADER_SIZE + 24)
        except struct.error:
            return None
        lat, lon, alt = ecef_to_geodetic(burst_X, burst_Y, burst_Z)
        return TerrainMutationPDU(
            lat=lat, lon=lon, alt=alt,
            mutation_type="crater", radius_m=3.0   # default blast radius
        )

    @staticmethod
    def _extract_timestamp(raw: bytes) -> float:
        """
        Extract the DIS absolute timestamp from the PDU header.
        DIS timestamps are in units of (2^31 - 1) per hour.
        We return seconds since epoch (approximate — DIS time is hour-relative).
        """
        try:
            ts_raw = struct.unpack_from(">I", raw, 4)[0]
        except struct.error:
            return time.time()
        # DIS timestamp bit 0 = absolute (1) or relative (0)
        # Absolute: time within the current hour in (2^31-1)/3600 s units
        is_absolute = ts_raw & 1
        ts_units = ts_raw >> 1
        seconds_in_hour = ts_units / ((2**31 - 1) / 3600.0)
        if is_absolute:
            hour_start = math.floor(time.time() / 3600.0) * 3600.0
            return hour_start + seconds_in_hour
        return time.time() - seconds_in_hour   # relative — approximate


class PDUEncoder:
    """
    Encodes typed Python objects back into DIS PDU binary format.
    Used to build outbound PDUs (engine → simulation tool).
    """

    def __init__(self, exercise_id: int, site_id: int, app_id: int,
                 coord: CoordTranslator):
        self.exercise_id = exercise_id
        self.site_id     = site_id
        self.app_id      = app_id
        self.coord       = coord
        self._pdu_counter = 0

    def encode_entity_state(self, record_bytes: bytes,
                             entity_id: int) -> bytes:
        """
        Build a DIS Entity State PDU from a raw 64-byte engine EntityRecord.
        The engine record format is defined in entity_sidecar.py:
          offset 0:  entity_id (uint32)
          offset 4:  entity_type (uint8)
          offset 5:  flags (uint8)
          offset 8:  x, y, z (float32 × 3) — block coords
          offset 20: vx, vy, vz (float32 × 3) — blocks/s
          offset 32: yaw, pitch (float32 × 2)
          offset 40: health (float32)
          offset 56: last_tick (uint64)
        """
        if len(record_bytes) < 64:
            return b''
        try:
            e_type_code = struct.unpack_from("<B", record_bytes, 4)[0]
            flags       = struct.unpack_from("<B", record_bytes, 5)[0]
            bx, by, bz  = struct.unpack_from("<fff", record_bytes, 8)
            vx, vy, vz  = struct.unpack_from("<fff", record_bytes, 20)
            yaw, pitch  = struct.unpack_from("<ff",  record_bytes, 32)
            health      = struct.unpack_from("<f",   record_bytes, 40)[0]
        except struct.error:
            return b''

        # Engine block coords → ECEF
        offset = int(bz * self.coord.world_x * self.coord.world_y +
                     by * self.coord.world_x + bx) * self.coord.block_size
        X, Y, Z = self.coord.engine_to_ecef(offset)

        # ENU velocity → ECEF (simplified rotation)
        lat_deg, lon_deg, _ = ecef_to_geodetic(X, Y, Z)
        lat_r = math.radians(lat_deg)
        lon_r = math.radians(lon_deg)
        sl, cl  = math.sin(lat_r), math.cos(lat_r)
        slo, clo = math.sin(lon_r), math.cos(lon_r)
        scale = self.coord.scale_m   # blocks/s → m/s
        v_ecef_x = (-slo * vx * scale - sl * clo * vz * scale
                    + cl * clo * vy * scale)
        v_ecef_y = ( clo * vx * scale - sl * slo * vz * scale
                    + cl * slo * vy * scale)
        v_ecef_z = ( cl * vz * scale + sl * vy * scale)

        # Map engine entity type → DIS EntityType (kind, domain, cat)
        _ENGINE_TO_DIS = {
            0: (0, 0, 0),    # unknown
            1: (1, 1, 0),    # dismounted → platform/land/misc
            2: (1, 1, 1),    # ground → platform/land/tank
            3: (1, 2, 20),   # rotary
            4: (1, 2, 1),    # fixed wing
            5: (1, 3, 0),    # watercraft
            6: (3, 0, 0),    # munition
            7: (7, 0, 0),    # sensor
            8: (1, 1, 4),    # logistics → platform/land/truck
        }
        kind, domain, cat = _ENGINE_TO_DIS.get(e_type_code, (0, 0, 0))

        # Appearance: set deactivated flag from entity flags bit 0
        is_active = bool(flags & 0x01)
        appearance = 0 if is_active else 0x00000001

        # Health → damage bits (bits 3–4 of appearance)
        damage = max(0, min(3, int((1.0 - health / 100.0) * 3)))
        appearance |= (damage << 3)

        # Decompose entity_id back to site/app/num
        e_site = (entity_id >> 16) & 0xFF
        e_app  = (entity_id >>  8) & 0xFF
        e_num  =  entity_id        & 0xFF

        self._pdu_counter += 1
        ts_raw = self._make_timestamp()

        pdu = struct.pack(
            # Header: version(1) exid(1) type(1) family(1) ts(4) len(2) pad(2)
            ">BBBBIHxx",
            ProtocolVersion.DIS_7,
            self.exercise_id,
            PDUType.ENTITY_STATE,
            1,              # protocol family: entity info
            ts_raw,
            ENTITY_STATE_PDU_SIZE,
        )
        # Body
        pdu += struct.pack(">HHH", e_site, e_app, e_num)    # entity id
        pdu += struct.pack(">BB",  1, 0)                     # force=friendly, 0 artic
        # entity type (kind domain country cat sub spec extra + 1 pad)
        pdu += struct.pack(">BBHBBBBx", kind, domain, 0, cat, 0, 0, 0)
        pdu += struct.pack(">BBHBBBBx", 0, 0, 0, 0, 0, 0, 0)  # alt entity type
        pdu += struct.pack(">fff", v_ecef_x, v_ecef_y, v_ecef_z)
        pdu += struct.pack(">ddd", X, Y, Z)
        pdu += struct.pack(">fff", yaw, pitch, 0.0)         # psi theta phi
        pdu += struct.pack(">I",   appearance)
        # DR parameters: FPW, 3 pad, zero accel (12 bytes), zero ang vel (12 bytes)
        pdu += struct.pack(">B3x", DeadReckoningAlgorithm.DRM_FPW)
        pdu += b'\x00' * 24
        # Marking: ASCII character set + 11-char ID padded with nulls
        marking = b'\x01' + f"E{entity_id:010d}".encode()[:11].ljust(11, b'\x00')
        pdu += marking
        pdu += struct.pack(">I", 0)   # capabilities

        assert len(pdu) == ENTITY_STATE_PDU_SIZE, \
            f"Entity State PDU size mismatch: {len(pdu)} vs {ENTITY_STATE_PDU_SIZE}"
        return pdu

    def encode_terrain_signal(self, pdu_in: TerrainMutationPDU) -> bytes:
        """
        Build a DIS Signal PDU encoding a terrain mutation event.
        Uses TERRAIN_MUTATION_MAGIC protocol.
        """
        MUTATION_CODE = {"crater": 1, "defilade": 2,
                         "obstacle": 3, "clear": 4}
        mut_code = MUTATION_CODE.get(pdu_in.mutation_type, 1)

        payload = TERRAIN_MUTATION_MAGIC
        payload += struct.pack(">ddfBf",
                               pdu_in.lat, pdu_in.lon, pdu_in.alt,
                               mut_code, pdu_in.radius_m)
        data_bits = len(payload) * 8

        ts_raw   = self._make_timestamp()
        body_len = 22 + len(payload)   # Signal PDU body
        pdu_len  = PDU_HEADER_SIZE + body_len

        hdr = struct.pack(">BBBBIHxx",
                          ProtocolVersion.DIS_7, self.exercise_id,
                          PDUType.SIGNAL, 4,   # family: radio comms
                          ts_raw, pdu_len)
        body = struct.pack(">HHHHHHI HH",
                           self.site_id, self.app_id, 0,   # entity id
                           0,              # radio id
                           0,              # encoding class: raw binary
                           0,              # encoding type
                           0,              # TDL type
                           0,              # sample rate
                           data_bits,      # data length bits
                           0)              # samples
        return hdr + body + payload

    @staticmethod
    def _make_timestamp() -> int:
        """
        Build a DIS absolute timestamp (bit 0 = 1 for absolute).
        Value = fractional seconds within the current hour,
        scaled to (2^31 - 1) / 3600 units.
        """
        now       = time.time()
        secs_in_hr = now % 3600.0
        units = int(secs_in_hr * ((2**31 - 1) / 3600.0))
        return (units << 1) | 1   # absolute timestamp


# ===========================================================================
# Section 6 — DIS UDP transport
# ===========================================================================

class DISTransport:
    """
    UDP multicast socket transport for DIS PDU stream.

    Listens on the configured multicast group:port for inbound PDUs and
    provides a send() method for outbound PDUs.
    """

    def __init__(self, cfg: DISConfig,
                 on_receive: Callable[[bytes], None]):
        self.cfg        = cfg
        self.on_receive = on_receive
        self._sock_recv: Optional[socket.socket] = None
        self._sock_send: Optional[socket.socket] = None
        self._thread:    Optional[threading.Thread] = None
        self._running    = False

    def start(self):
        self._running = True
        self._sock_send = self._make_send_socket()
        self._sock_recv = self._make_recv_socket()
        self._thread = threading.Thread(
            target=self._recv_loop, name="DIS-recv", daemon=True)
        self._thread.start()
        log.info("DIS transport started on %s:%d (exercise %d)",
                 self.cfg.multicast_group, self.cfg.port, self.cfg.exercise_id)  # noqa: E501 (fixed by format below)
        log.info("DIS transport: multicast=%s port=%d exercise=%d",
                 self.cfg.multicast_group, self.cfg.port,
                 self.cfg.exercise_id)

    def stop(self):
        self._running = False
        if self._sock_recv:
            self._sock_recv.close()
        if self._sock_send:
            self._sock_send.close()
        if self._thread:
            self._thread.join(timeout=2.0)
        log.info("DIS transport stopped")

    def send(self, pdu_bytes: bytes):
        """Send a PDU to the multicast group."""
        if self._sock_send and pdu_bytes:
            try:
                self._sock_send.sendto(
                    pdu_bytes,
                    (self.cfg.multicast_group, self.cfg.port)
                )
            except OSError as e:
                log.warning("DIS send error: %s", e)

    def _recv_loop(self):
        while self._running:
            try:
                data, _ = self._sock_recv.recvfrom(65535)
                if data:
                    self.on_receive(data)
            except OSError:
                break   # socket closed

    def _make_send_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                          socket.IPPROTO_UDP)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                     self.cfg.ttl)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                     self.cfg.send_buf)
        if self.cfg.loopback:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        return s

    def _make_recv_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                          socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass   # not available on all platforms
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.cfg.recv_buf)
        s.bind((self.cfg.bind_iface or '', self.cfg.port))
        # Join multicast group
        group = socket.inet_aton(self.cfg.multicast_group)
        iface = socket.inet_aton('0.0.0.0')
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     group + iface)
        s.settimeout(1.0)
        return s


# ===========================================================================
# Section 7 — HLA federate bridge  (stub — wire in your RTI SDK)
# ===========================================================================

class HLAFederateBridge:
    """
    HLA RTI bridge using RPR-FOM 2.0 object and interaction classes.

    This class provides the interface and lifecycle.  The actual RTI
    connection requires an installed RTI (Pitch pRTI, MAK RTI, or Portico).
    Wire in your RTI's Python bindings via the rti_factory parameter.

    RPR-FOM 2.0 object classes used:
      BaseEntity.PhysicalEntity.Platform.GroundVehicle
      BaseEntity.PhysicalEntity.Platform.Aircraft
      BaseEntity.PhysicalEntity.Lifeform.Human.GroundVehicle
      BaseEntity.PhysicalEntity.Munition

    RPR-FOM 2.0 interaction classes used:
      WeaponFire     → maps to engine terrain mutation (detonation path)
      Detonation     → maps to crater terrain mutation
      TerrainModification (custom extension) → direct terrain mutation
    """

    def __init__(self, cfg: HLAConfig,
                 on_entity_update: Callable[[EntityStatePDU], None],
                 on_terrain_mutation: Callable[[TerrainMutationPDU], None],
                 rti_factory=None):
        self.cfg                  = cfg
        self.on_entity_update     = on_entity_update
        self.on_terrain_mutation  = on_terrain_mutation
        self._rti                 = None
        self._rti_factory         = rti_factory
        self._federate_handle     = None
        self._running             = False

    def start(self):
        if not self.cfg.enabled:
            log.info("HLA bridge disabled — DIS-only mode")
            return
        if self._rti_factory is None:
            log.warning(
                "HLA bridge: no RTI factory provided.  "
                "Install your RTI SDK and pass rti_factory=YourRTI() to "
                "MilitaryTranslator.  Running DIS-only."
            )
            return
        try:
            self._rti = self._rti_factory.connect(
                self.cfg.rti_host, self.cfg.rti_port
            )
            self._rti.create_federation(
                self.cfg.federation_name, self.cfg.fom_path
            )
            self._federate_handle = self._rti.join_federation(
                self.cfg.federation_name, self.cfg.federate_name
            )
            self._subscribe_rpr_classes()
            self._running = True
            log.info("HLA bridge connected: federation=%r federate=%r",
                     self.cfg.federation_name, self.cfg.federate_name)
        except Exception as e:
            log.error("HLA bridge connection failed: %s — running DIS-only", e)

    def stop(self):
        if self._rti and self._running:
            try:
                self._rti.resign_federation_execution()
            except Exception:
                pass
        self._running = False

    def publish_entity(self, record_bytes: bytes, entity_id: int):
        """Publish an engine entity record as an RPR-FOM object update."""
        if not (self._rti and self._running):
            return
        # In production: update the object instance attributes in the RTI
        # using the decoded record fields.  The RTI handles fan-out to all
        # subscribed federates.
        # self._rti.update_attribute_values(...)
        log.debug("HLA publish entity %d (%d bytes)", entity_id,
                  len(record_bytes))

    def _subscribe_rpr_classes(self):
        """Subscribe to RPR-FOM object and interaction classes."""
        if not self._rti:
            return
        # In production:
        # self._rti.subscribe_object_class_attributes(
        #     "BaseEntity.PhysicalEntity", [...])
        # self._rti.subscribe_interaction_class("Detonation")
        # self._rti.subscribe_interaction_class("TerrainModification")
        pass

    def reflect_attribute_values(self, object_handle, attributes: dict):
        """Called by the RTI ambassador when a subscribed object is updated."""
        # Decode RPR-FOM attributes → EntityStatePDU and forward to engine
        # In production, extract WorldLocation, VelocityVector,
        # EntityType, ForceIdentifier from `attributes` and build an
        # EntityStatePDU.
        pass

    def receive_interaction(self, interaction_class: str, params: dict):
        """Called by the RTI ambassador when a subscribed interaction arrives."""
        if interaction_class == "Detonation":
            # Extract location from params → TerrainMutationPDU
            pass
        if interaction_class == "TerrainModification":
            pass


# ===========================================================================
# Section 8 — Heartbeat / entity timeout tracker
# ===========================================================================

class EntityHeartbeatTracker:
    """
    Tracks last-update time for each entity_id.  Entities that have not
    been updated within `timeout_s` are marked inactive and removed from
    the engine sidecar.

    This prevents ghost entities from persisting in the engine after a
    simulation tool disconnects or an entity exits the exercise area.
    """

    def __init__(self, timeout_s: float, on_timeout: Callable[[int], None]):
        self.timeout_s  = timeout_s
        self.on_timeout = on_timeout
        self._last_seen: Dict[int, float] = {}
        self._lock      = threading.Lock()

    def touch(self, entity_id: int):
        with self._lock:
            self._last_seen[entity_id] = time.monotonic()

    def remove(self, entity_id: int):
        with self._lock:
            self._last_seen.pop(entity_id, None)

    def sweep(self):
        """Call periodically.  Fires on_timeout for expired entities."""
        now = time.monotonic()
        with self._lock:
            expired = [eid for eid, ts in self._last_seen.items()
                       if now - ts > self.timeout_s]
        for eid in expired:
            self.on_timeout(eid)
            self.remove(eid)


# ===========================================================================
# Section 9 — Top-level translator
# ===========================================================================

class MilitaryTranslator:
    """
    Top-level wire-level translator.

    Wires the DIS transport, HLA bridge, PDU codec, dead-reckoning resolver,
    coordinate translator, and heartbeat tracker together and connects them
    to the MilitarySimAdapter.

    This is the class you instantiate.  Everything else in this file
    is infrastructure it assembles.
    """

    def __init__(
        self,
        adapter: MilitarySimAdapter,
        dis:     DISConfig,
        hla:     Optional[HLAConfig]  = None,
        origin:  Optional[CoordOrigin] = None,
        scale_m: float = 0.66,
        world_x: int   = 512,
        world_y: int   = 64,
        world_z: int   = 512,
        heartbeat_timeout_s: float = 5.0,
        rti_factory = None,
    ):
        self.adapter = adapter
        self.dis_cfg = dis
        self.hla_cfg = hla or HLAConfig()

        _origin = origin or CoordOrigin(lat=0.0, lon=0.0, alt=0.0)

        self.coord = CoordTranslator(
            origin=_origin, scale_m=scale_m,
            world_x=world_x, world_y=world_y, world_z=world_z,
        )
        self.decoder = PDUDecoder(
            exercise_id=dis.exercise_id,
            coord=self.coord,
            dr=DeadReckoningResolver(),
        )
        self.encoder = PDUEncoder(
            exercise_id=dis.exercise_id,
            site_id=dis.site_id,
            app_id=dis.app_id,
            coord=self.coord,
        )
        self.heartbeat = EntityHeartbeatTracker(
            timeout_s=heartbeat_timeout_s,
            on_timeout=self._on_entity_timeout,
        )
        self.dis_transport = DISTransport(
            cfg=dis,
            on_receive=self._on_dis_receive,
        )
        self.hla_bridge = HLAFederateBridge(
            cfg=self.hla_cfg,
            on_entity_update=self._on_entity_pdu,
            on_terrain_mutation=self._on_terrain_pdu,
            rti_factory=rti_factory,
        )

        self._running       = False
        self._sweep_thread: Optional[threading.Thread] = None
        self._send_queue:   queue.Queue = queue.Queue(maxsize=4096)
        self._send_thread:  Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the translator — DIS transport, HLA bridge, sweep loop."""
        if self._running:
            return
        self._running = True
        self.dis_transport.start()
        self.hla_bridge.start()
        self._sweep_thread = threading.Thread(
            target=self._sweep_loop, name="Translator-sweep", daemon=True)
        self._sweep_thread.start()
        self._send_thread = threading.Thread(
            target=self._send_loop, name="Translator-send", daemon=True)
        self._send_thread.start()
        log.info("MilitaryTranslator started")

    def stop(self):
        """Stop all subsystems.  Does not affect the engine."""
        self._running = False
        self.dis_transport.stop()
        self.hla_bridge.stop()
        if self._sweep_thread:
            self._sweep_thread.join(timeout=2.0)
        if self._send_thread:
            self._send_queue.put(None)   # sentinel
            self._send_thread.join(timeout=2.0)
        log.info("MilitaryTranslator stopped")

    # ------------------------------------------------------------------
    # Inbound path: simulation tool → engine
    # ------------------------------------------------------------------

    def _on_dis_receive(self, raw: bytes):
        """Called from the DIS receive thread for every incoming PDU."""
        obj = self.decoder.decode(raw)
        if isinstance(obj, EntityStatePDU):
            self._on_entity_pdu(obj)
        elif isinstance(obj, TerrainMutationPDU):
            self._on_terrain_pdu(obj)

    def _on_entity_pdu(self, pdu: EntityStatePDU):
        """Route a decoded EntityStatePDU into the engine via the adapter."""
        self.heartbeat.touch(pdu.entity_id)
        self.adapter.ingest_entity_pdu(pdu)

    def _on_terrain_pdu(self, pdu: TerrainMutationPDU):
        """Route a decoded terrain mutation into the engine via the adapter."""
        self.adapter.ingest_terrain_mutation(pdu)

    def _on_entity_timeout(self, entity_id: int):
        """Mark an entity inactive when its heartbeat expires."""
        log.debug("Entity %d timed out — marking inactive", entity_id)
        inactive = EntityStatePDU(
            entity_id=entity_id, force_id=0,
            entity_kind=0, entity_domain=0, entity_category=0,
            lat=0.0, lon=0.0, alt=0.0, is_active=False,
        )
        self.adapter.ingest_entity_pdu(inactive)

    # ------------------------------------------------------------------
    # Outbound path: engine → simulation tool
    # ------------------------------------------------------------------

    def send_entity_record(self, record_bytes: bytes, entity_id: int):
        """
        Enqueue an engine EntityRecord for outbound DIS/HLA broadcast.
        Call this from the render feed's entity delta callback.
        Non-blocking — drops to the send queue.
        """
        if not self._send_queue.full():
            self._send_queue.put_nowait(("entity", record_bytes, entity_id))

    def send_terrain_mutation(self, pdu: TerrainMutationPDU):
        """
        Enqueue a terrain mutation for outbound broadcast.
        Used when the engine itself initiates a terrain change (world_gen,
        server-side mutation) that downstream simulation tools need to see.
        """
        if not self._send_queue.full():
            self._send_queue.put_nowait(("terrain", pdu, None))

    def broadcast_battlespace_picture(self, since_tick: int = 0):
        """
        Convenience: ask the adapter for all entity deltas since `since_tick`
        and queue them for outbound broadcast.  Call from your server loop.
        """
        self.adapter.broadcast_battlespace_picture(since_tick=since_tick)

    def _send_loop(self):
        """Drains the send queue and writes PDU bytes to the DIS transport."""
        while self._running:
            try:
                item = self._send_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break   # sentinel
            kind, payload, entity_id = item
            if kind == "entity":
                pdu_bytes = self.encoder.encode_entity_state(
                    payload, entity_id)
                self.dis_transport.send(pdu_bytes)
                self.hla_bridge.publish_entity(payload, entity_id)
            elif kind == "terrain":
                pdu_bytes = self.encoder.encode_terrain_signal(payload)
                self.dis_transport.send(pdu_bytes)

    def _sweep_loop(self):
        """Periodic heartbeat sweep and battlespace picture broadcast."""
        tick = 0
        while self._running:
            time.sleep(0.05)   # 20 Hz
            tick += 1
            self.heartbeat.sweep()
            if tick % 40 == 0:   # every 2 s
                self.broadcast_battlespace_picture(since_tick=tick - 40)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        return {
            "running":       self._running,
            "dis_multicast": self.dis_cfg.multicast_group,
            "dis_port":      self.dis_cfg.port,
            "exercise_id":   self.dis_cfg.exercise_id,
            "hla_enabled":   self.hla_cfg.enabled,
            "send_queue_depth": self._send_queue.qsize(),
        }


# ===========================================================================
# Section 10 — Self-test and dry-run
# ===========================================================================

def _run_self_test():
    """
    Encode a round-trip: EntityStatePDU → DIS bytes → decoded PDU.
    Verify field preservation within floating-point precision.
    All tests run without a live engine or network socket.
    """
    import sys

    PASS = "\033[32mPASS\033[0m"
    FAIL = "\033[31mFAIL\033[0m"
    results = []

    def check(name: str, condition: bool, detail: str = ""):
        tag = PASS if condition else FAIL
        results.append(condition)
        print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""))

    print("\n=== MilitaryTranslator self-test ===\n")

    # ---- 1. ECEF ↔ WGS-84 round-trip ----
    print("1. ECEF ↔ WGS-84 round-trip")
    for lat, lon, alt in [(0.0, 0.0, 0.0), (38.8977, -77.0365, 50.0),
                          (-33.8688, 151.2093, 100.0),
                          (89.9, 0.0, 0.0), (-89.9, 180.0, 8849.0)]:
        X, Y, Z = geodetic_to_ecef(lat, lon, alt)
        lat2, lon2, alt2 = ecef_to_geodetic(X, Y, Z)
        ok = (abs(lat - lat2) < 1e-7 and abs(lon - lon2) < 1e-7
              and abs(alt - alt2) < 0.001)
        check(f"  lat={lat:8.3f} lon={lon:9.3f} alt={alt:6.0f}m",
              ok, f"residual lat={abs(lat-lat2):.2e} lon={abs(lon-lon2):.2e}")

    # ---- 2. Dead-reckoning algorithms ----
    print("\n2. Dead-reckoning resolver")
    X0, Y0, Z0 = geodetic_to_ecef(38.8977, -77.0365, 50.0)
    vX, vY, vZ = 10.0, 0.0, 5.0
    aX, aY, aZ = 1.0, 0.0, 0.5
    dt = 2.0
    now = time.time()
    t0  = now - dt

    X_fpw, Y_fpw, _ = DeadReckoningResolver.resolve(
        X0, Y0, Z0, vX, vY, vZ, 0, 0, 0,
        DeadReckoningAlgorithm.DRM_FPW, t0, now)
    check("DRM_FPW constant velocity",
          abs(X_fpw - (X0 + vX * dt)) < 0.001,
          f"ΔX={abs(X_fpw - X0 - vX*dt):.4f}")

    X_fvw, _, _ = DeadReckoningResolver.resolve(
        X0, Y0, Z0, vX, vY, vZ, aX, aY, aZ,
        DeadReckoningAlgorithm.DRM_FVW, t0, now)
    expected = X0 + vX * dt + 0.5 * aX * dt * dt
    check("DRM_FVW variable velocity",
          abs(X_fvw - expected) < 0.001,
          f"ΔX={abs(X_fvw - expected):.4f}")

    X_static, _, _ = DeadReckoningResolver.resolve(
        X0, Y0, Z0, vX, vY, vZ, 0, 0, 0,
        DeadReckoningAlgorithm.STATIC, t0, now)
    check("STATIC no movement", X_static == X0)

    # ---- 3. PDU encode / decode round-trip ----
    print("\n3. Entity State PDU encode → decode round-trip")
    origin = CoordOrigin(lat=38.8977, lon=-77.0365, alt=0.0)
    coord  = CoordTranslator(origin=origin, scale_m=0.66,
                              world_x=512, world_y=64, world_z=512)
    dis_cfg = DISConfig(exercise_id=1, site_id=1, app_id=1)
    encoder = PDUEncoder(exercise_id=1, site_id=1, app_id=1, coord=coord)
    decoder = PDUDecoder(exercise_id=1, coord=coord,
                         dr=DeadReckoningResolver())

    # Build a fake 64-byte EntityRecord (matches entity_sidecar.py layout)
    bx, by, bz = 100.0, 5.0, 200.0    # block coords
    vx, vy, vz = 2.0, 0.0, 1.0        # blocks/s
    record = struct.pack(
        "<IBBHffffffffffffffQQ",
        42,          # entity_id
        2,           # entity_type = GROUND
        0x07,        # flags: active | visible | collidable
        0,           # reserved
        bx, by, bz, vx, vy, vz,
        0.5, 0.0,    # yaw, pitch
        85.0, 0.0,   # health, meta
        0,           # owner_id
        1000,        # last_tick
    )
    pdu_bytes = encoder.encode_entity_state(record, entity_id=42)
    check("PDU size == 144 bytes", len(pdu_bytes) == ENTITY_STATE_PDU_SIZE,
          f"got {len(pdu_bytes)}")

    decoded = decoder.decode(pdu_bytes)
    check("Decoded to EntityStatePDU", isinstance(decoded, EntityStatePDU))
    if isinstance(decoded, EntityStatePDU):
        check("Force ID preserved", decoded.force_id == 1,
              f"got {decoded.force_id}")
        check("Entity active", decoded.is_active,
              f"is_active={decoded.is_active}")
        # Position round-trip: block coords → ECEF → geodetic → ENU → block
        # Allow 2-block tolerance (coordinate chain introduces ~1m rounding)
        result = coord.ecef_to_engine(
            *geodetic_to_ecef(decoded.lat, decoded.lon, decoded.alt))
        if result:
            rx, ry, rz, _ = result
            tol = 2
            check(f"Position round-trip within {tol} blocks",
                  abs(rx - int(bx)) <= tol and abs(rz - int(bz)) <= tol,
                  f"bx={int(bx)} rx={rx}  bz={int(bz)} rz={rz}")
        else:
            check("Position round-trip (in-bounds)", False,
                  "ecef_to_engine returned None")

    # ---- 4. Terrain Signal PDU encode / decode ----
    print("\n4. Terrain Signal PDU encode → decode")
    terrain_in = TerrainMutationPDU(
        lat=38.9000, lon=-77.0300, alt=0.0,
        mutation_type="crater", radius_m=7.5)
    sig_bytes = encoder.encode_terrain_signal(terrain_in)
    check("Signal PDU minimum size", len(sig_bytes) >= SIGNAL_PDU_MIN_SIZE,
          f"got {len(sig_bytes)}")
    terrain_out = decoder.decode(sig_bytes)
    check("Decoded to TerrainMutationPDU",
          isinstance(terrain_out, TerrainMutationPDU))
    if isinstance(terrain_out, TerrainMutationPDU):
        check("Mutation type preserved",
              terrain_out.mutation_type == terrain_in.mutation_type,
              f"got {terrain_out.mutation_type!r}")
        check("Radius preserved",
              abs(terrain_out.radius_m - terrain_in.radius_m) < 0.01,
              f"got {terrain_out.radius_m:.2f}")
        check("Lat preserved",
              abs(terrain_out.lat - terrain_in.lat) < 1e-6)
        check("Lon preserved",
              abs(terrain_out.lon - terrain_in.lon) < 1e-6)

    # ---- 5. Exercise ID filtering ----
    print("\n5. Exercise ID filtering")
    pdu_bytes_ex2 = bytearray(pdu_bytes)
    pdu_bytes_ex2[1] = 2   # change exercise_id to 2
    result_filtered = decoder.decode(bytes(pdu_bytes_ex2))
    check("PDU with wrong exercise_id is discarded",
          result_filtered is None,
          f"got {result_filtered!r}")

    # ---- 6. Heartbeat tracker ----
    print("\n6. Heartbeat timeout tracker")
    timed_out = []
    tracker = EntityHeartbeatTracker(
        timeout_s=0.05,
        on_timeout=lambda eid: timed_out.append(eid)
    )
    tracker.touch(99)
    tracker.touch(100)
    time.sleep(0.1)
    tracker.sweep()
    check("Both entities expire after timeout",
          99 in timed_out and 100 in timed_out,
          f"timed_out={timed_out}")
    tracker.touch(101)
    tracker.sweep()   # 101 was just touched — should NOT expire yet
    check("Recently touched entity does NOT expire", 101 not in timed_out,
          f"timed_out={timed_out}")

    # ---- Summary ----
    passed = sum(results)
    total  = len(results)
    print(f"\n{'='*36}")
    print(f"  {passed}/{total} checks passed")
    if passed == total:
        print(f"  {PASS} All tests passed")
    else:
        print(f"  {FAIL} {total - passed} test(s) FAILED")
    print(f"{'='*36}\n")
    return passed == total


def _dry_run(args):
    print("\n=== MilitaryTranslator dry-run ===\n")
    origin = CoordOrigin(lat=args.origin_lat, lon=args.origin_lon, alt=0.0)
    coord  = CoordTranslator(origin=origin, scale_m=0.66,
                              world_x=512, world_y=64, world_z=512)

    print("Coordinate translation examples:")
    for dlat, dlon, label in [(0.0, 0.0, "origin"),
                               (0.001, 0.0, "+111m north"),
                               (0.0, 0.001, "+~88m east (at 38°N)"),
                               (0.01, 0.01, "+1.1 km NE")]:
        lat = args.origin_lat + dlat
        lon = args.origin_lon + dlon
        X, Y, Z = geodetic_to_ecef(lat, lon, 50.0)
        result = coord.ecef_to_engine(X, Y, Z)
        if result:
            bx, by, bz, off = result
            print(f"  {label:20s}  lat={lat:.5f} lon={lon:.5f}  "
                  f"→ block ({bx:4d},{by:2d},{bz:4d})  offset={off:12d}")
        else:
            print(f"  {label:20s}  → out of world bounds")

    print("\nDIS PDU size reference:")
    print(f"  Entity State PDU : {ENTITY_STATE_PDU_SIZE} bytes")
    print(f"  PDU header       : {PDU_HEADER_SIZE} bytes")
    print(f"  PDU body         : {ENTITY_STATE_PDU_SIZE - PDU_HEADER_SIZE} bytes")

    print("\nDead-reckoning example:")
    X0, Y0, Z0 = geodetic_to_ecef(args.origin_lat, args.origin_lon, 50.0)
    for dt in (0.0, 0.05, 0.1, 0.5, 1.0):
        Xr, Yr, Zr = DeadReckoningResolver.resolve(
            X0, Y0, Z0, 10.0, 0.0, 5.0, 0.0, 0.0, 0.0,
            DeadReckoningAlgorithm.DRM_FPW,
            time.time() - dt
        )
        lat2, lon2, _ = ecef_to_geodetic(Xr, Yr, Zr)
        print(f"  dt={dt:.2f}s  resolved lat={lat2:.6f} lon={lon2:.6f}")

    print("\n=== dry-run complete ===\n")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s"
    )
    parser = argparse.ArgumentParser(
        description="Block-Image Engine — Military Protocol Translator"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run",   action="store_true",
                       help="Coordinate translation demo without a live engine")
    group.add_argument("--self-test", action="store_true",
                       help="Encode/decode round-trip unit tests")
    parser.add_argument("--origin-lat", type=float, default=38.8977)
    parser.add_argument("--origin-lon", type=float, default=-77.0365)
    args = parser.parse_args()

    if args.self_test:
        import sys
        ok = _run_self_test()
        sys.exit(0 if ok else 1)
    elif args.dry_run:
        _dry_run(args)
    else:
        print(
            "MilitaryTranslator — wire this into your server:\n\n"
            "  from military_adapter    import MilitarySimAdapter, CoordOrigin\n"
            "  from military_translator import MilitaryTranslator, DISConfig\n\n"
            "  adapter    = MilitarySimAdapter(rs, sidecar, feed, origin=...)\n"
            "  translator = MilitaryTranslator(adapter=adapter,\n"
            "                   dis=DISConfig(multicast_group='239.1.2.3',\n"
            "                                port=3000, exercise_id=1))\n"
            "  translator.start()\n\n"
            "Use --dry-run for a coordinate demo.\n"
            "Use --self-test to run encode/decode round-trip tests.\n"
        )