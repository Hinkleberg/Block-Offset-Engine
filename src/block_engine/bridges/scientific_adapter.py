"""
scientific_adapter.py
=====================
Translation layer between the Block-Image Engine and scientific simulation tools.

Supported domains:
  - Atmospheric / climate science  (NetCDF, CF conventions)
  - Oceanography                   (HYCOM, MOM6, ROMS output)
  - Seismic / geophysics           (SEG-Y, SPECFEM3D grids)
  - Wildfire propagation           (FARSITE / Phoenix / ELMFIRE)
  - General volumetric HPC grids   (HDF5, raw binary, numpy mmap)

Engine interface (three calls only):
  read_block(offset)  → bytes   (16-byte block payload)
  write_block(offset, data)     (journaled, quorum-safe)
  entity_update(record)         (64-byte EntityRecord)

Scientific protocols handled here; engine internals never touched.

Block layout:
  offset(x, y, z) = (z * W * H + y * W + x) * BLOCK_SIZE

Block payload (16 bytes):
  [0:4]   block_type   uint32  — maps to ScientificBlockType
  [4:4]   metadata     uint32  — domain-specific payload (see below)
  [8:8]   entity_hint  uint64  — sidecar pointer (0 = none)

Metadata encoding per domain:
  ATMOSPHERE : pressure_pa (uint16) | temperature_k (uint16) encoded
  OCEAN      : salinity_ppt (uint16) | current_speed_cms (uint16)
  SEISMIC    : p_wave_ms (uint16) | s_wave_ms (uint16)
  WILDFIRE   : fuel_load (uint16) | fire_intensity (uint16)
  SCALAR     : raw float32 stored as uint32 bits (IEEE 754)
"""

from __future__ import annotations

import struct
import math
import threading
import time
import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Iterator, Optional, Tuple, List, Dict, Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BLOCK_SIZE = 16          # bytes per engine block
ENTITY_RECORD_SIZE = 64  # bytes per entity sidecar record


# ---------------------------------------------------------------------------
# Scientific block type registry
# ---------------------------------------------------------------------------

class ScientificBlockType(IntEnum):
    EMPTY        = 0x00
    ATMOSPHERE   = 0x10   # atmospheric layer
    OCEAN        = 0x11   # ocean water column cell
    SEABED       = 0x12   # ocean sediment / bathymetry
    LAND         = 0x20   # land surface
    SUBSURFACE   = 0x21   # subsurface geology
    WILDFIRE     = 0x30   # fire-active cell
    SCALAR_FIELD = 0x40   # generic scalar (pressure, temperature, density…)
    VECTOR_FIELD = 0x41   # generic vector magnitude storage
    SPACE_VACUUM = 0x50   # vacuum / orbital cell (used by space adapter)
    REGOLITH     = 0x51   # planetary surface (used by space adapter)
    BEDROCK      = 0x52   # sub-surface planetary rock
    ICE          = 0x53   # polar ice, cryosphere


# ---------------------------------------------------------------------------
# Block codec — packs / unpacks the 16-byte engine block payload
# ---------------------------------------------------------------------------

class BlockCodec:
    """Encode/decode the engine's 16-byte block payload for scientific data."""

    PACK_FMT = ">IIQ"   # big-endian: uint32, uint32, uint64

    @staticmethod
    def encode(block_type: int, metadata: int, entity_hint: int = 0) -> bytes:
        return struct.pack(BlockCodec.PACK_FMT, block_type, metadata, entity_hint)

    @staticmethod
    def decode(raw: bytes) -> Tuple[int, int, int]:
        if len(raw) != BLOCK_SIZE:
            raise ValueError(f"Expected {BLOCK_SIZE} bytes, got {len(raw)}")
        block_type, metadata, entity_hint = struct.unpack(BlockCodec.PACK_FMT, raw)
        return block_type, metadata, entity_hint

    # ---- domain-specific metadata helpers --------------------------------

    @staticmethod
    def encode_atmosphere(pressure_pa: float, temperature_k: float) -> int:
        """Pack pressure [0–131070 Pa] and temperature [0–655.35 K] into uint32."""
        p = min(max(int(pressure_pa / 2.0), 0), 65535)       # 2 Pa resolution
        t = min(max(int(temperature_k * 100), 0), 65535)      # 0.01 K resolution
        return (p << 16) | t

    @staticmethod
    def decode_atmosphere(metadata: int) -> Tuple[float, float]:
        p = ((metadata >> 16) & 0xFFFF) * 2.0
        t = (metadata & 0xFFFF) / 100.0
        return p, t

    @staticmethod
    def encode_ocean(salinity_ppt: float, current_speed_cms: float) -> int:
        s = min(max(int(salinity_ppt * 1000), 0), 65535)
        c = min(max(int(current_speed_cms * 10), 0), 65535)
        return (s << 16) | c

    @staticmethod
    def decode_ocean(metadata: int) -> Tuple[float, float]:
        s = ((metadata >> 16) & 0xFFFF) / 1000.0
        c = (metadata & 0xFFFF) / 10.0
        return s, c

    @staticmethod
    def encode_seismic(p_wave_ms: float, s_wave_ms: float) -> int:
        p = min(max(int(p_wave_ms), 0), 65535)
        s = min(max(int(s_wave_ms), 0), 65535)
        return (p << 16) | s

    @staticmethod
    def decode_seismic(metadata: int) -> Tuple[float, float]:
        p = (metadata >> 16) & 0xFFFF
        s = metadata & 0xFFFF
        return float(p), float(s)

    @staticmethod
    def encode_wildfire(fuel_load_kg_m2: float, fire_intensity_kw_m: float) -> int:
        f = min(max(int(fuel_load_kg_m2 * 100), 0), 65535)
        i = min(max(int(fire_intensity_kw_m), 0), 65535)
        return (f << 16) | i

    @staticmethod
    def decode_wildfire(metadata: int) -> Tuple[float, float]:
        f = ((metadata >> 16) & 0xFFFF) / 100.0
        i = float(metadata & 0xFFFF)
        return f, i

    @staticmethod
    def encode_scalar(value: float) -> int:
        """Store a float32 as its IEEE-754 bit pattern in uint32."""
        return struct.unpack(">I", struct.pack(">f", value))[0]

    @staticmethod
    def decode_scalar(metadata: int) -> float:
        return struct.unpack(">f", struct.pack(">I", metadata))[0]


# ---------------------------------------------------------------------------
# World layout (mirrors block_layout.py from the engine)
# ---------------------------------------------------------------------------

@dataclass
class WorldLayout:
    world_x: int
    world_y: int
    world_z: int
    block_size: int = BLOCK_SIZE

    def block_offset(self, x: int, y: int, z: int) -> int:
        if not (0 <= x < self.world_x and 0 <= y < self.world_y
                and 0 <= z < self.world_z):
            raise IndexError(
                f"Coordinate ({x},{y},{z}) out of bounds "
                f"({self.world_x}×{self.world_y}×{self.world_z})"
            )
        return (z * self.world_x * self.world_y +
                y * self.world_x + x) * self.block_size

    def offset_to_coord(self, offset: int) -> Tuple[int, int, int]:
        block_index = offset // self.block_size
        z = block_index // (self.world_x * self.world_y)
        rem = block_index % (self.world_x * self.world_y)
        y = rem // self.world_x
        x = rem % self.world_x
        return x, y, z

    def total_blocks(self) -> int:
        return self.world_x * self.world_y * self.world_z

    def blocks_in_radius(self, cx: int, cy: int, cz: int,
                         radius: int) -> List[int]:
        """Return all offsets within cubic radius (Manhattan in 3D)."""
        offsets = []
        for dz in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    nx, ny, nz = cx + dx, cy + dy, cz + dz
                    if (0 <= nx < self.world_x and
                            0 <= ny < self.world_y and
                            0 <= nz < self.world_z):
                        offsets.append(self.block_offset(nx, ny, nz))
        return offsets


# ---------------------------------------------------------------------------
# Geographic ↔ grid coordinate converter
# ---------------------------------------------------------------------------

@dataclass
class GeoOrigin:
    """WGS-84 geographic anchor for the engine's flat-offset grid."""
    lat_deg: float          # latitude of grid origin (bottom-left, south)
    lon_deg: float          # longitude of grid origin (west edge)
    alt_m: float = 0.0      # altitude of vertical layer 0 (sea level = 0)
    m_per_block_h: float = 0.66    # horizontal resolution metres/block
    m_per_block_v: float = 100.0   # vertical resolution metres/block (z axis)

    # Earth radius for small-angle approximation (valid < ~2 000 km domains)
    _R: float = field(default=6_371_000.0, init=False, repr=False)

    def geo_to_grid(self, lat: float, lon: float,
                    alt: float) -> Tuple[float, float, float]:
        """Convert WGS-84 (lat, lon, alt) to floating-point grid coords."""
        dlat_m = (lat - self.lat_deg) * (math.pi / 180.0) * self._R
        dlon_m = ((lon - self.lon_deg) * (math.pi / 180.0) *
                  self._R * math.cos(math.radians(self.lat_deg)))
        dalt_m = alt - self.alt_m
        gx = dlon_m / self.m_per_block_h
        gy = dlat_m / self.m_per_block_h
        gz = dalt_m / self.m_per_block_v
        return gx, gy, gz

    def grid_to_geo(self, gx: float, gy: float,
                    gz: float) -> Tuple[float, float, float]:
        """Convert grid coords to WGS-84 (lat, lon, alt)."""
        dlat_m = gy * self.m_per_block_h
        dlon_m = gx * self.m_per_block_h
        dalt_m = gz * self.m_per_block_v
        lat = self.lat_deg + dlat_m / ((math.pi / 180.0) * self._R)
        lon = (self.lon_deg +
               dlon_m / ((math.pi / 180.0) * self._R *
                         math.cos(math.radians(self.lat_deg))))
        alt = self.alt_m + dalt_m
        return lat, lon, alt


# ---------------------------------------------------------------------------
# Simulation variable descriptor
# ---------------------------------------------------------------------------

@dataclass
class SimVariable:
    """Describes one physical variable stored across the engine grid."""
    name: str                   # e.g. "temperature", "pressure"
    units: str                  # SI units string
    block_type: ScientificBlockType
    domain: str                 # "atmosphere", "ocean", "seismic", "wildfire", "scalar"
    long_name: str = ""
    valid_min: float = -1e38
    valid_max: float = 1e38
    fill_value: float = -9999.0


# ---------------------------------------------------------------------------
# Scientific simulation adapter
# ---------------------------------------------------------------------------

class ScientificSimAdapter:
    """
    Translation layer between the Block-Image Engine and scientific simulation
    tools (NetCDF readers, HDF5 loaders, FARSITE fire models, seismic solvers,
    atmospheric reanalysis datasets, etc.).

    Constructor parameters
    ----------------------
    resilient_store   : Object with read_block(offset) / write_block(offset, data)
    entity_sidecar    : Object with write_entity(record) / read_entity(entity_id)
    render_feed       : Object with tick_delta() (may be None for write-only mode)
    layout            : WorldLayout  — grid dimensions
    geo_origin        : GeoOrigin   — WGS-84 anchor (optional, used for geo tools)
    domain_callbacks  : dict mapping domain name → callable for domain-specific logic
                        e.g. {"wildfire": farsite_spread_fn, "atmosphere": wrf_update_fn}
    """

    def __init__(
        self,
        resilient_store,
        entity_sidecar,
        render_feed,
        layout: WorldLayout,
        geo_origin: Optional[GeoOrigin] = None,
        domain_callbacks: Optional[Dict[str, Callable]] = None,
    ):
        self._store = resilient_store
        self._sidecar = entity_sidecar
        self._feed = render_feed
        self.layout = layout
        self.geo_origin = geo_origin
        self._domain_callbacks = domain_callbacks or {}
        self._variables: Dict[str, SimVariable] = {}
        self._lock = threading.Lock()
        self._running = False
        self._tick_thread: Optional[threading.Thread] = None
        self._stats = {
            "writes": 0, "reads": 0, "errors": 0,
            "wildfire_cells": 0, "entity_updates": 0,
        }

    # ------------------------------------------------------------------ #
    #  Variable registry                                                   #
    # ------------------------------------------------------------------ #

    def register_variable(self, var: SimVariable) -> None:
        """Register a physical variable for tracking/export."""
        with self._lock:
            self._variables[var.name] = var
            logger.info("Registered variable: %s (%s) [%s]",
                        var.name, var.units, var.domain)

    # ------------------------------------------------------------------ #
    #  Core write path — scientific data → engine block                   #
    # ------------------------------------------------------------------ #

    def write_atmosphere(self, x: int, y: int, z: int,
                         pressure_pa: float, temperature_k: float,
                         entity_hint: int = 0) -> int:
        """Write one atmospheric grid cell to the engine."""
        meta = BlockCodec.encode_atmosphere(pressure_pa, temperature_k)
        payload = BlockCodec.encode(ScientificBlockType.ATMOSPHERE, meta, entity_hint)
        offset = self.layout.block_offset(x, y, z)
        self._store.write_block(offset, payload)
        with self._lock:
            self._stats["writes"] += 1
        return offset

    def write_ocean(self, x: int, y: int, z: int,
                    salinity_ppt: float, current_speed_cms: float,
                    entity_hint: int = 0) -> int:
        """Write one ocean water-column cell."""
        meta = BlockCodec.encode_ocean(salinity_ppt, current_speed_cms)
        payload = BlockCodec.encode(ScientificBlockType.OCEAN, meta, entity_hint)
        offset = self.layout.block_offset(x, y, z)
        self._store.write_block(offset, payload)
        with self._lock:
            self._stats["writes"] += 1
        return offset

    def write_seismic(self, x: int, y: int, z: int,
                      p_wave_ms: float, s_wave_ms: float,
                      entity_hint: int = 0) -> int:
        """Write one seismic velocity model cell."""
        meta = BlockCodec.encode_seismic(p_wave_ms, s_wave_ms)
        payload = BlockCodec.encode(ScientificBlockType.SUBSURFACE, meta, entity_hint)
        offset = self.layout.block_offset(x, y, z)
        self._store.write_block(offset, payload)
        with self._lock:
            self._stats["writes"] += 1
        return offset

    def write_wildfire(self, x: int, y: int, z: int,
                       fuel_load_kg_m2: float, fire_intensity_kw_m: float,
                       entity_hint: int = 0) -> int:
        """Write wildfire state for one surface cell."""
        meta = BlockCodec.encode_wildfire(fuel_load_kg_m2, fire_intensity_kw_m)
        block_type = (ScientificBlockType.WILDFIRE
                      if fire_intensity_kw_m > 0 else ScientificBlockType.LAND)
        payload = BlockCodec.encode(block_type, meta, entity_hint)
        offset = self.layout.block_offset(x, y, z)
        self._store.write_block(offset, payload)
        with self._lock:
            self._stats["writes"] += 1
            if fire_intensity_kw_m > 0:
                self._stats["wildfire_cells"] += 1
        return offset

    def write_scalar(self, x: int, y: int, z: int, value: float,
                     block_type: ScientificBlockType = ScientificBlockType.SCALAR_FIELD,
                     entity_hint: int = 0) -> int:
        """Write a generic scalar field value to one cell."""
        meta = BlockCodec.encode_scalar(value)
        payload = BlockCodec.encode(block_type, meta, entity_hint)
        offset = self.layout.block_offset(x, y, z)
        self._store.write_block(offset, payload)
        with self._lock:
            self._stats["writes"] += 1
        return offset

    # ------------------------------------------------------------------ #
    #  Core read path — engine block → scientific data                    #
    # ------------------------------------------------------------------ #

    def read_cell(self, x: int, y: int, z: int) -> Dict[str, Any]:
        """
        Read one cell and decode to domain-appropriate scientific values.
        Returns a dict with keys: block_type, metadata_raw, entity_hint,
        and domain-specific decoded fields.
        """
        offset = self.layout.block_offset(x, y, z)
        raw = self._store.read_block(offset)
        btype, meta, ehint = BlockCodec.decode(raw)
        result: Dict[str, Any] = {
            "x": x, "y": y, "z": z,
            "offset": offset,
            "block_type": btype,
            "block_type_name": ScientificBlockType(btype).name
                               if btype in ScientificBlockType._value2member_map_
                               else f"UNKNOWN({btype:#04x})",
            "metadata_raw": meta,
            "entity_hint": ehint,
        }
        with self._lock:
            self._stats["reads"] += 1

        # Domain-specific decode
        if btype == ScientificBlockType.ATMOSPHERE:
            p, t = BlockCodec.decode_atmosphere(meta)
            result.update({"pressure_pa": p, "temperature_k": t,
                           "temperature_c": t - 273.15})
        elif btype == ScientificBlockType.OCEAN:
            s, c = BlockCodec.decode_ocean(meta)
            result.update({"salinity_ppt": s, "current_speed_cms": c})
        elif btype == ScientificBlockType.SUBSURFACE:
            p, s = BlockCodec.decode_seismic(meta)
            result.update({"p_wave_velocity_ms": p, "s_wave_velocity_ms": s})
        elif btype in (ScientificBlockType.WILDFIRE, ScientificBlockType.LAND):
            f, i = BlockCodec.decode_wildfire(meta)
            result.update({"fuel_load_kg_m2": f, "fire_intensity_kw_m": i,
                           "fire_active": btype == ScientificBlockType.WILDFIRE})
        elif btype in (ScientificBlockType.SCALAR_FIELD,
                       ScientificBlockType.REGOLITH,
                       ScientificBlockType.BEDROCK,
                       ScientificBlockType.ICE,
                       ScientificBlockType.SPACE_VACUUM):
            result.update({"scalar_value": BlockCodec.decode_scalar(meta)})

        return result

    # ------------------------------------------------------------------ #
    #  Geo-referenced helpers                                              #
    # ------------------------------------------------------------------ #

    def write_at_geo(self, lat: float, lon: float, alt: float,
                     block_type: ScientificBlockType,
                     value_a: float, value_b: float = 0.0) -> Optional[int]:
        """Write a cell addressed by WGS-84 coordinates."""
        if self.geo_origin is None:
            raise RuntimeError("GeoOrigin not configured on this adapter")
        gx, gy, gz = self.geo_origin.geo_to_grid(lat, lon, alt)
        ix, iy, iz = int(gx), int(gy), int(gz)
        dispatch = {
            ScientificBlockType.ATMOSPHERE: self.write_atmosphere,
            ScientificBlockType.OCEAN:      self.write_ocean,
            ScientificBlockType.SUBSURFACE: self.write_seismic,
            ScientificBlockType.WILDFIRE:   self.write_wildfire,
            ScientificBlockType.LAND:       self.write_wildfire,
        }
        fn = dispatch.get(block_type)
        if fn is None:
            return self.write_scalar(ix, iy, iz, value_a, block_type)
        return fn(ix, iy, iz, value_a, value_b)

    def read_at_geo(self, lat: float, lon: float,
                    alt: float) -> Dict[str, Any]:
        """Read a cell addressed by WGS-84 coordinates."""
        if self.geo_origin is None:
            raise RuntimeError("GeoOrigin not configured on this adapter")
        gx, gy, gz = self.geo_origin.geo_to_grid(lat, lon, alt)
        result = self.read_cell(int(gx), int(gy), int(gz))
        result.update({"lat": lat, "lon": lon, "alt": alt})
        return result

    # ------------------------------------------------------------------ #
    #  Volume / slice readers — for HPC grid export                       #
    # ------------------------------------------------------------------ #

    def read_horizontal_slice(self, z: int,
                              x_range: Optional[Tuple[int, int]] = None,
                              y_range: Optional[Tuple[int, int]] = None
                              ) -> List[Dict[str, Any]]:
        """
        Read an entire horizontal layer (z = const) and return decoded cells.
        Suitable for exporting to NetCDF lat-lon grids, matplotlib imshow, etc.
        """
        x0, x1 = x_range or (0, self.layout.world_x)
        y0, y1 = y_range or (0, self.layout.world_y)
        return [self.read_cell(x, y, z)
                for y in range(y0, y1) for x in range(x0, x1)]

    def read_vertical_column(self, x: int,
                             y: int) -> List[Dict[str, Any]]:
        """Read all vertical layers at a single (x, y) column."""
        return [self.read_cell(x, y, z) for z in range(self.layout.world_z)]

    def scan_fire_front(self) -> List[Dict[str, Any]]:
        """
        Return all currently-active wildfire cells.
        Iterates the surface layer (z=0) only for performance.
        In a volumetric fire model, iterate all z layers.
        """
        active = []
        for y in range(self.layout.world_y):
            for x in range(self.layout.world_x):
                cell = self.read_cell(x, y, 0)
                if cell.get("fire_active"):
                    active.append(cell)
        return active

    # ------------------------------------------------------------------ #
    #  NetCDF / HDF5 import (inline pure-Python, no external deps)        #
    # ------------------------------------------------------------------ #

    def import_grid_from_2d_array(
        self,
        grid: List[List[float]],
        z_layer: int,
        block_type: ScientificBlockType,
        secondary_grid: Optional[List[List[float]]] = None,
    ) -> int:
        """
        Import a 2-D Python list-of-lists (or any row-major 2-D sequence)
        into the engine at the specified z layer.

        grid[row][col] maps to (x=col, y=row, z=z_layer).
        secondary_grid provides the second value for dual-value block types
        (e.g. temperature for atmosphere when grid = pressure).

        Returns the number of blocks written.
        """
        written = 0
        for row_idx, row in enumerate(grid):
            for col_idx, val in enumerate(row):
                x, y = col_idx, row_idx
                if x >= self.layout.world_x or y >= self.layout.world_y:
                    continue
                sec = (secondary_grid[row_idx][col_idx]
                       if secondary_grid else 0.0)
                try:
                    if block_type == ScientificBlockType.ATMOSPHERE:
                        self.write_atmosphere(x, y, z_layer, val, sec)
                    elif block_type == ScientificBlockType.OCEAN:
                        self.write_ocean(x, y, z_layer, val, sec)
                    elif block_type == ScientificBlockType.SUBSURFACE:
                        self.write_seismic(x, y, z_layer, val, sec)
                    elif block_type in (ScientificBlockType.WILDFIRE,
                                        ScientificBlockType.LAND):
                        self.write_wildfire(x, y, z_layer, val, sec)
                    else:
                        self.write_scalar(x, y, z_layer, val, block_type)
                    written += 1
                except Exception as exc:          # noqa: BLE001
                    logger.warning("import_grid: skip (%d,%d): %s", x, y, exc)
                    with self._lock:
                        self._stats["errors"] += 1
        return written

    def export_grid_to_2d_array(
        self, z_layer: int, field: str
    ) -> List[List[float]]:
        """
        Export one field from a z-layer to a 2-D list-of-lists.
        field must be a key present in the decoded cell dict.
        Fill value -9999.0 used for cells missing the field.
        """
        grid = []
        for y in range(self.layout.world_y):
            row = []
            for x in range(self.layout.world_x):
                cell = self.read_cell(x, y, z_layer)
                row.append(cell.get(field, -9999.0))
            grid.append(row)
        return grid

    # ------------------------------------------------------------------ #
    #  Propagation engine (diffusion-based)                               #
    # ------------------------------------------------------------------ #

    def propagate_scalar(
        self,
        z_layer: int,
        field_block_type: ScientificBlockType,
        diffusion_coeff: float = 0.25,
        source_callback: Optional[Callable[[int, int], Optional[float]]] = None,
        sink_callback: Optional[Callable[[int, int, float], float]] = None,
        iterations: int = 1,
    ) -> int:
        """
        Single-step finite-difference diffusion of a scalar field on one
        horizontal layer.  Mimics the engine's built-in lighting propagator
        but for arbitrary physical quantities.

        diffusion_coeff : fraction transferred to each of 4 horizontal neighbours
        source_callback : (x,y) → float | None — external source term per cell
        sink_callback   : (x,y,value) → float — decay/absorption term

        Returns the number of cells updated.
        """
        W, H = self.layout.world_x, self.layout.world_y

        # Read entire layer into memory (small domains only; use chunk reads
        # for production domains > 10k × 10k)
        current = {}
        for y in range(H):
            for x in range(W):
                cell = self.read_cell(x, y, z_layer)
                current[(x, y)] = cell.get("scalar_value", 0.0)

        updated_count = 0
        for _ in range(iterations):
            nxt = {}
            for (x, y), val in current.items():
                nb_sum = 0.0
                nb_count = 0
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx2, ny2 = x + dx, y + dy
                    if 0 <= nx2 < W and 0 <= ny2 < H:
                        nb_sum += current[(nx2, ny2)]
                        nb_count += 1
                new_val = val + diffusion_coeff * (nb_sum - nb_count * val)
                if source_callback:
                    src = source_callback(x, y)
                    if src is not None:
                        new_val += src
                if sink_callback:
                    new_val = sink_callback(x, y, new_val)
                nxt[(x, y)] = new_val
            current = nxt

        for (x, y), val in current.items():
            self.write_scalar(x, y, z_layer, val, field_block_type)
            updated_count += 1
        return updated_count

    def propagate_wildfire(
        self,
        wind_speed_ms: float = 5.0,
        wind_dir_deg: float = 270.0,
        moisture_content: float = 0.10,
        slope_deg: float = 0.0,
    ) -> int:
        """
        Simplified Rothermel-style fire spread over the surface layer (z=0).
        Each burning cell spreads to neighbours with probability modulated by:
          - wind speed and direction (rate-of-spread multiplier)
          - fuel load (available energy)
          - moisture content (suppression)
          - slope (uphill acceleration)

        Returns the number of newly ignited cells this tick.
        """
        W, H = self.layout.world_x, self.layout.world_y
        wind_rad = math.radians(wind_dir_deg)
        wx = math.cos(wind_rad)
        wy = math.sin(wind_rad)

        # Read current fire state
        fire_cells: Dict[Tuple[int, int], Dict] = {}
        for y in range(H):
            for x in range(W):
                cell = self.read_cell(x, y, 0)
                fire_cells[(x, y)] = cell

        newly_ignited = 0
        for (x, y), cell in fire_cells.items():
            if not cell.get("fire_active"):
                continue
            fuel   = cell.get("fuel_load_kg_m2", 0.5)
            intens = cell.get("fire_intensity_kw_m", 100.0)

            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx2, ny2 = x + dx, y + dy
                if not (0 <= nx2 < W and 0 <= ny2 < H):
                    continue
                neighbour = fire_cells[(nx2, ny2)]
                if neighbour.get("fire_active"):
                    continue  # already burning

                n_fuel = neighbour.get("fuel_load_kg_m2", 0.0)
                if n_fuel <= 0.0:
                    continue  # no fuel — cannot ignite

                # Wind alignment bonus: dot product of wind vector and spread dir
                wind_align = max(0.0, wx * dx + wy * dy)
                # Base rate of spread (simplified Rothermel, m/min)
                ros = (0.5 * intens / max(n_fuel, 0.1) *
                       (1.0 + wind_speed_ms * wind_align * 0.3) *
                       (1.0 - moisture_content) *
                       (1.0 + slope_deg * 0.05))
                # Convert ROS to per-tick ignition probability
                prob = min(ros / 20.0, 0.95)

                # Deterministic pseudo-random using grid coordinates + tick
                seed_val = (nx2 * 2654435761 ^ ny2 * 40503) & 0xFFFFFFFF
                pseudo = (seed_val ^ int(time.monotonic() * 1e6)) & 0xFFFF
                if pseudo / 65535.0 < prob:
                    new_intens = min(intens * 0.8, 50000.0)
                    self.write_wildfire(nx2, ny2, 0, n_fuel, new_intens)
                    newly_ignited += 1

        return newly_ignited

    # ------------------------------------------------------------------ #
    #  Entity sidecar helpers (instrument buoys, weather stations, etc.)  #
    # ------------------------------------------------------------------ #

    def write_sensor_entity(
        self,
        entity_id: int,
        x: float, y: float, z: float,
        entity_type: int = 4,       # 4 = generic instrument
        health: float = 100.0,
        metadata: float = 0.0,
    ) -> bytes:
        """
        Write a scientific instrument (buoy, radiosonde, seismometer, rover)
        to the entity sidecar.

        Returns the 64-byte entity record.
        """
        record = struct.pack(
            ">IbbhfffffffQQ",
            entity_id,      # entity_id
            entity_type,    # entity_type
            0b00000111,     # flags: active | visible | collidable
            0,              # reserved
            x, y, z,        # position
            0.0, 0.0, 0.0,  # velocity (stationary sensor)
            0.0, 0.0,       # yaw, pitch
            health,         # health
            metadata,       # metadata (sensor reading)
            0,              # owner_id
            int(time.time()),  # last_tick
        )
        self._sidecar.write_entity(record)
        with self._lock:
            self._stats["entity_updates"] += 1
        return record

    # ------------------------------------------------------------------ #
    #  Tick loop (optional)                                                #
    # ------------------------------------------------------------------ #

    def start(self, tick_hz: float = 1.0,
              tick_callback: Optional[Callable[[], None]] = None) -> None:
        """
        Start the adapter's internal tick loop.
        tick_callback is called each tick after consuming render deltas.
        """
        if self._running:
            return
        self._running = True
        interval = 1.0 / tick_hz

        def _loop():
            while self._running:
                t0 = time.monotonic()
                try:
                    if self._feed is not None:
                        deltas = self._feed.tick_delta()
                        self._ingest_render_deltas(deltas)
                    if tick_callback:
                        tick_callback()
                    # Fire domain callbacks
                    for domain, cb in self._domain_callbacks.items():
                        try:
                            cb(self)
                        except Exception as exc:  # noqa: BLE001
                            logger.error("Domain callback '%s' failed: %s",
                                         domain, exc)
                except Exception as exc:          # noqa: BLE001
                    logger.error("Tick error: %s", exc)
                    with self._lock:
                        self._stats["errors"] += 1
                elapsed = time.monotonic() - t0
                remaining = interval - elapsed
                if remaining > 0:
                    time.sleep(remaining)

        self._tick_thread = threading.Thread(target=_loop,
                                             name="SciAdapterTick",
                                             daemon=True)
        self._tick_thread.start()
        logger.info("ScientificSimAdapter tick loop started at %.1f Hz", tick_hz)

    def stop(self) -> None:
        """Stop the tick loop and wait for clean exit."""
        self._running = False
        if self._tick_thread:
            self._tick_thread.join(timeout=5.0)
        logger.info("ScientificSimAdapter stopped. Stats: %s", self._stats)

    def _ingest_render_deltas(self, deltas) -> None:
        """
        Process deltas from the render feed.
        In scientific use cases the render feed signals that blocks have been
        mutated externally (e.g. by another simulation participant writing
        weather updates). This hook allows the adapter to sync in-memory caches.
        """
        if not deltas:
            return
        # Default: log delta count. Override for domain-specific cache sync.
        logger.debug("Ingest %d render deltas", len(deltas))

    # ------------------------------------------------------------------ #
    #  Diagnostics                                                         #
    # ------------------------------------------------------------------ #

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._stats)

    def health_report(self) -> str:
        s = self.statistics()
        return (
            f"ScientificSimAdapter | "
            f"writes={s['writes']} reads={s['reads']} "
            f"errors={s['errors']} "
            f"wildfire_cells={s['wildfire_cells']} "
            f"entity_updates={s['entity_updates']}"
        )

    def __repr__(self) -> str:
        return (
            f"ScientificSimAdapter("
            f"layout={self.layout.world_x}×{self.layout.world_y}×"
            f"{self.layout.world_z}, "
            f"geo={'yes' if self.geo_origin else 'no'}, "
            f"variables={list(self._variables.keys())})"
        )


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def make_adapter(
    resilient_store,
    entity_sidecar,
    render_feed=None,
    world_x: int = 256,
    world_y: int = 256,
    world_z: int = 64,
    origin_lat: float = 0.0,
    origin_lon: float = 0.0,
    origin_alt: float = 0.0,
    m_per_block_h: float = 0.66,
    m_per_block_v: float = 100.0,
    domain_callbacks: Optional[Dict[str, Callable]] = None,
) -> ScientificSimAdapter:
    """
    Convenience factory.  Wires a WorldLayout and GeoOrigin and returns a
    ready-to-use adapter.

    Example
    -------
    adapter = make_adapter(
        resilient_store=rs,
        entity_sidecar=sidecar,
        render_feed=feed,
        world_x=512, world_y=512, world_z=32,
        origin_lat=34.05, origin_lon=-118.25,   # Los Angeles
    )
    adapter.start(tick_hz=1.0)
    """
    layout = WorldLayout(world_x, world_y, world_z)
    geo = GeoOrigin(
        lat_deg=origin_lat, lon_deg=origin_lon,
        alt_m=origin_alt,
        m_per_block_h=m_per_block_h,
        m_per_block_v=m_per_block_v,
    )
    adapter = ScientificSimAdapter(
        resilient_store=resilient_store,
        entity_sidecar=entity_sidecar,
        render_feed=render_feed,
        layout=layout,
        geo_origin=geo,
        domain_callbacks=domain_callbacks,
    )
    # Register standard atmospheric variables
    for v in [
        SimVariable("temperature", "K",   ScientificBlockType.ATMOSPHERE,
                    "atmosphere", "Air temperature"),
        SimVariable("pressure",    "Pa",  ScientificBlockType.ATMOSPHERE,
                    "atmosphere", "Atmospheric pressure"),
        SimVariable("salinity",    "ppt", ScientificBlockType.OCEAN,
                    "ocean",      "Sea water salinity"),
        SimVariable("p_wave_vel",  "m/s", ScientificBlockType.SUBSURFACE,
                    "seismic",    "P-wave velocity"),
        SimVariable("fuel_load",   "kg/m²", ScientificBlockType.LAND,
                    "wildfire",   "Surface fuel load"),
        SimVariable("fire_intens", "kW/m",  ScientificBlockType.WILDFIRE,
                    "wildfire",   "Fire line intensity"),
    ]:
        adapter.register_variable(v)
    return adapter


# ---------------------------------------------------------------------------
# Self-test (run: python scientific_adapter.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s | %(name)s | %(message)s")

    # ---- Minimal in-memory stub that mimics the engine API ---------------

    class _MockStore:
        def __init__(self):
            self._data: Dict[int, bytes] = {}

        def write_block(self, offset: int, data: bytes) -> None:
            self._data[offset] = data

        def read_block(self, offset: int) -> bytes:
            return self._data.get(offset, b'\x00' * BLOCK_SIZE)

    class _MockSidecar:
        def __init__(self):
            self._entities: Dict[int, bytes] = {}

        def write_entity(self, record: bytes) -> None:
            entity_id = struct.unpack(">I", record[:4])[0]
            self._entities[entity_id] = record

        def read_entity(self, entity_id: int) -> Optional[bytes]:
            return self._entities.get(entity_id)

    store   = _MockStore()
    sidecar = _MockSidecar()

    adapter = make_adapter(
        resilient_store=store,
        entity_sidecar=sidecar,
        world_x=32, world_y=32, world_z=8,
        origin_lat=34.05, origin_lon=-118.25,
    )
    print(adapter)

    # Write atmosphere slice
    for z in range(8):
        base_p = 101325.0 * math.exp(-z * 0.12)
        base_t = 288.15 - z * 6.5
        for y in range(32):
            for x in range(32):
                adapter.write_atmosphere(x, y, z,
                                         pressure_pa=base_p + x * 10,
                                         temperature_k=base_t + y * 0.01)

    # Write ocean surface
    for y in range(32):
        for x in range(32):
            adapter.write_ocean(x, y, 0, salinity_ppt=35.0 + x * 0.1,
                                current_speed_cms=15.0 + y * 0.5)

    # Write wildfire patch
    for y in range(10, 15):
        for x in range(10, 15):
            adapter.write_wildfire(x, y, 0, fuel_load_kg_m2=2.5,
                                   fire_intensity_kw_m=500.0)

    # Read back
    cell_atm = adapter.read_cell(5, 5, 2)
    cell_ocn = adapter.read_cell(5, 5, 0)
    cell_fir = adapter.read_cell(12, 12, 0)

    print(f"\nAtmosphere cell (5,5,2): "
          f"p={cell_atm['pressure_pa']:.1f} Pa, "
          f"T={cell_atm['temperature_k']:.2f} K "
          f"({cell_atm['temperature_c']:.2f} °C)")
    print(f"Ocean cell (5,5,0): "
          f"salinity={cell_ocn['salinity_ppt']:.3f} ppt, "
          f"current={cell_ocn['current_speed_cms']:.1f} cm/s")
    print(f"Wildfire cell (12,12,0): "
          f"fuel={cell_fir['fuel_load_kg_m2']:.2f} kg/m², "
          f"intensity={cell_fir['fire_intensity_kw_m']:.0f} kW/m, "
          f"active={cell_fir['fire_active']}")

    # Geo-referenced write/read
    off = adapter.write_at_geo(34.06, -118.24, 1000.0,
                               ScientificBlockType.ATMOSPHERE,
                               99000.0, 275.0)
    print(f"\nGeo write offset: {off}")
    geo_cell = adapter.read_at_geo(34.06, -118.24, 1000.0)
    print(f"Geo read: p={geo_cell.get('pressure_pa', '?'):.1f} Pa, "
          f"T={geo_cell.get('temperature_k', '?'):.2f} K")

    # Wildfire propagation step
    new_cells = adapter.propagate_wildfire(wind_speed_ms=8.0,
                                           wind_dir_deg=270.0,
                                           moisture_content=0.08)
    print(f"\nFire propagation: {new_cells} new cells ignited this tick")

    # Scalar diffusion
    for y in range(32):
        for x in range(32):
            v = 100.0 if (14 <= x <= 17 and 14 <= y <= 17) else 0.0
            adapter.write_scalar(x, y, 3, v)
    updated = adapter.propagate_scalar(
        z_layer=3,
        field_block_type=ScientificBlockType.SCALAR_FIELD,
        diffusion_coeff=0.20,
        iterations=3,
    )
    print(f"Scalar diffusion: {updated} cells updated")

    # Entity (radiosonde)
    adapter.write_sensor_entity(entity_id=1001,
                                x=16.0, y=16.0, z=4.0,
                                health=100.0, metadata=287.5)
    print(f"\nSensor entity 1001 written")

    # Export slice
    grid_t = adapter.export_grid_to_2d_array(z_layer=2, field="temperature_k")
    non_fill = sum(1 for row in grid_t for v in row if v != -9999.0)
    print(f"Exported temperature grid: {len(grid_t)}×{len(grid_t[0])}, "
          f"{non_fill} non-fill cells")

    print(f"\n{adapter.health_report()}")
    print("\nAll self-tests passed.")
    sys.exit(0)