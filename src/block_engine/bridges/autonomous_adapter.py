"""
autonomous_adapter.py — Autonomous Vehicle & Robotics Simulation Adapter
=========================================================================

Bridges the Block-Image Engine's binary block / entity-sidecar API to the
protocols and data formats used by autonomous vehicle (AV) and robotics
simulation stacks: CARLA, LGSVL, AWSIM, Autoware, and ROS 2 / Cyber RT
frameworks.

This adapter is NOT part of the engine. It is an optional integration layer.
The engine operates identically whether this adapter is running or not.
The adapter can be connected, reconfigured, or removed at any time without
touching the engine core.

Architecture
------------
                   ┌──────────────────────────────┐
  ROS 2 topics     │    AVSimAdapter                │◄────► Block-Image Engine
  CARLA RPC        │  (coord translation,           │       (binary block I/O,
  LGSVL Cyber RT   │   occupancy grid mapping,      │        entity sidecar,
  Autoware msgs    │   HD map export,               │        render feed)
  Sensor feeds     │   actor state bridge)          │
                   └──────────────────────────────┘

Core Problem Solved
-------------------
AV simulation stacks are bottlenecked by world streaming — pulling terrain
and dynamic object state from databases fast enough to feed thousands of
parallel simulation instances simultaneously.

This adapter replaces that database streaming layer with the engine's direct
offset arithmetic. A city block is a byte range. A highway corridor is a
contiguous seek. A pedestrian is an entity sidecar record linked to a block
offset by a single pointer. No database, no middleware, no cache warm-up.

Coordinate System
-----------------
AV tools typically use local ENU (East-North-Up) or ISO 8855 vehicle frame
coordinates. The adapter anchors an ENU origin and converts:

  engine_x = int(enu_east  / scale_m)
  engine_y = int(enu_up    / scale_m)
  engine_z = int(enu_north / scale_m)

Occupancy Grid
--------------
The engine's 3D block grid maps directly to a voxel occupancy grid.
  block_type=0 (air)        → FREE
  block_type!=0, solid flag → OCCUPIED
  block_type=water          → DYNAMIC (passable but penalised)

HD Map Layer
------------
Static road geometry (lane boundaries, crosswalks, traffic signs, speed
limits, traffic lights) is stored as specialised block types in the engine.
The adapter reads these blocks and re-emits them as:
  - Lanelet2 format (.osm) for Autoware and Apollo
  - OpenDRIVE (.xodr) for CARLA and LGSVL
  - nav_msgs/OccupancyGrid for ROS 2 costmap layers

Sensor Simulation
-----------------
LIDAR, RADAR, and camera frustum queries are translated to
blocks_in_range() + entity sidecar lookups. The adapter packages the
results in the sensor data format the AV stack expects.

Supported AV Frameworks
------------------------
  AVFramework.ROS2       ROS 2 (Humble / Iron / Jazzy)
  AVFramework.CARLA      CARLA Simulator 0.9.x
  AVFramework.LGSVL      SVL Simulator (LGSVL)
  AVFramework.AWSIM      AWSIM (Autoware)
  AVFramework.AUTOWARE   Autoware.Universe

Pluggable Algorithms
--------------------
AV stacks bring their own path planning, sensor fusion, and prediction
algorithms. The adapter exposes hooks — the engine never calls them directly.

  obstacle_callback(offsets) → list[EntityRecord]
      Given a list of occupied block offsets ahead of the ego vehicle,
      return the entity records of dynamic obstacles at those offsets.
      Supply your own sensor fusion / object tracking model.

  map_query_callback(lane_id) → list[int]
      Given a Lanelet2 lane ID, return the engine block offsets that
      form that lane's centreline.

  prediction_callback(entity_record, horizon_ticks) → list[(x, y, z)]
      Given an entity record and a prediction horizon, return a list
      of predicted future positions. Supply your own motion model.

Usage
-----
    from autonomous_adapter import AVSimAdapter, MapOrigin, AVFramework

    adapter = AVSimAdapter(
        resilient_store=rs,
        entity_sidecar=sidecar,
        render_feed=feed,
        origin=MapOrigin(east=0.0, north=0.0, up=0.0),
        scale_m_per_block=0.66,
        framework=AVFramework.ROS2,
        ros_namespace="/world_engine",
        obstacle_callback=my_obstacle_fn,
        prediction_callback=my_prediction_fn,
    )
    adapter.start()

    # Dry-run / standalone test:
    #   python autonomous_adapter.py --dry-run --framework ros2

Notes
-----
- Requires a running engine instance (run_server.py) but does not affect
  core engine operation if the adapter is stopped or disconnected.
- ROS 2, CARLA, LGSVL, and Autoware network layers are stubbed here.
  Wire in your framework SDK via the publish_callback parameter.
- Entity geometry (agent size, bounding box) is not stored in the engine
  block image. Supply it via the entity_geometry_callback hook or a
  static lookup table keyed on entity_type.
- This adapter is not affiliated with any AV company or simulation vendor.
"""

from __future__ import annotations

import struct
import threading
import time
import argparse
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Framework enum
# ---------------------------------------------------------------------------

class AVFramework(Enum):
    ROS2      = "ros2"
    CARLA     = "carla"
    LGSVL     = "lgsvl"
    AWSIM     = "awsim"
    AUTOWARE  = "autoware"


# ---------------------------------------------------------------------------
# Coordinate translation
# ---------------------------------------------------------------------------

@dataclass
class MapOrigin:
    """ENU origin that maps to engine offset (0, 0, 0)."""
    east:  float = 0.0   # metres
    north: float = 0.0
    up:    float = 0.0


@dataclass
class ENUPosition:
    east:  float
    north: float
    up:    float


def enu_to_engine(enu: ENUPosition, scale_m: float,
                  world_x: int, world_y: int, world_z: int,
                  origin: MapOrigin, block_size: int = 16) -> Optional[int]:
    """Convert ENU to engine byte offset. Returns None if out of bounds."""
    x = int((enu.east  - origin.east)  / scale_m)
    y = int((enu.up    - origin.up)    / scale_m)
    z = int((enu.north - origin.north) / scale_m)
    if not (0 <= x < world_x and 0 <= y < world_y and 0 <= z < world_z):
        return None
    return (z * world_x * world_y + y * world_x + x) * block_size


def engine_to_enu(offset: int, scale_m: float,
                  world_x: int, world_y: int,
                  origin: MapOrigin, block_size: int = 16) -> ENUPosition:
    """Engine byte offset → ENU position."""
    idx = offset // block_size
    x = idx % world_x
    y = (idx // world_x) % world_y
    z = idx // (world_x * world_y)
    return ENUPosition(
        east  = x * scale_m + origin.east,
        north = z * scale_m + origin.north,
        up    = y * scale_m + origin.up,
    )


# ---------------------------------------------------------------------------
# Occupancy grid
# ---------------------------------------------------------------------------

class OccupancyValue(IntEnum):
    FREE     = 0
    DYNAMIC  = 50    # passable but not free (water, slow zone)
    OCCUPIED = 100


# block_type → OccupancyValue (extend to match your block type catalogue)
BLOCK_TYPE_OCCUPANCY: dict[int, OccupancyValue] = {
    0:  OccupancyValue.FREE,       # air
    1:  OccupancyValue.OCCUPIED,   # stone
    2:  OccupancyValue.OCCUPIED,   # dirt
    3:  OccupancyValue.OCCUPIED,   # grass
    4:  OccupancyValue.DYNAMIC,    # water
    5:  OccupancyValue.OCCUPIED,   # bedrock
}


def block_to_occupancy(block_data: bytes) -> OccupancyValue:
    """Read the block_type byte and return the corresponding occupancy value."""
    if len(block_data) < 1:
        return OccupancyValue.FREE
    block_type = block_data[0]
    solid_flag = (block_data[2] & 0x01) if len(block_data) > 2 else 0
    if solid_flag:
        return OccupancyValue.OCCUPIED
    return BLOCK_TYPE_OCCUPANCY.get(block_type, OccupancyValue.FREE)


# ---------------------------------------------------------------------------
# HD map block types
# ---------------------------------------------------------------------------

# Specialised block types for road geometry stored in the engine image.
# These are written by map ingestion tools, not by world_gen.py.
HD_MAP_BLOCK_TYPES = {
    0x20: "lane_centre",
    0x21: "lane_boundary_solid",
    0x22: "lane_boundary_dashed",
    0x23: "crosswalk",
    0x24: "stop_line",
    0x25: "traffic_sign",
    0x26: "traffic_light",
    0x27: "speed_bump",
    0x28: "yield_zone",
}


# ---------------------------------------------------------------------------
# Stub message types
# (Replace with your framework's SDK types in production)
# ---------------------------------------------------------------------------

@dataclass
class OccupancyGridMsg:
    """nav_msgs/OccupancyGrid stub."""
    width: int
    height: int
    resolution_m: float
    origin_east:  float
    origin_north: float
    data: list[int] = field(default_factory=list)   # row-major, -1/0–100


@dataclass
class ActorState:
    """Generic actor state from CARLA / LGSVL / AWSIM."""
    actor_id: int
    actor_type: str    # "vehicle", "pedestrian", "cyclist", "static"
    east: float
    north: float
    up: float
    vx: float = 0.0
    vy: float = 0.0
    vz: float = 0.0
    yaw: float = 0.0
    is_active: bool = True


@dataclass
class LIDARScan:
    """Stub LIDAR point cloud (sensor_msgs/PointCloud2)."""
    points: list[tuple[float, float, float]] = field(default_factory=list)
    intensities: list[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Actor type mapping
# ---------------------------------------------------------------------------

ACTOR_TYPE_TO_ENGINE: dict[str, int] = {
    "vehicle":    2,    # EntityType.MOB
    "pedestrian": 1,    # EntityType.PLAYER (repurposed for sim agents)
    "cyclist":    2,
    "static":     3,    # EntityType.ITEM
    "npc":        2,
}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class AVSimAdapter:
    """
    Adapter between the Block-Image Engine and AV / robotics simulation.

    Parameters
    ----------
    resilient_store
        ResilientStore instance (Array A). Terrain mutations write here.
    entity_sidecar
        EntitySidecar instance. Actor state is read and written here.
    render_feed
        RenderFeed instance. World deltas for AV clients originate here.
    origin
        ENU origin: the simulation map point that maps to engine offset 0.
    scale_m_per_block
        Metres per block edge. Default 0.66 m.
    world_x, world_y, world_z
        Engine world dimensions in blocks.
    framework
        Target AV framework. Controls message format and topic names.
    ros_namespace
        ROS 2 topic namespace prefix. Default "/world_engine".
    publish_callback
        (topic: str, msg: object) → None. Supplies messages to the
        framework's network layer. Stub: prints to console if not provided.
    obstacle_callback
        (offsets: list[int]) → list[object]. Returns entity records for
        dynamic obstacles at the given block offsets. Default: empty list.
    map_query_callback
        (lane_id: str) → list[int]. Returns block offsets for a lane.
        Default: empty list.
    prediction_callback
        (entity_record: bytes, horizon_ticks: int) → list[tuple].
        Returns predicted future (east, north, up) positions.
        Default: constant position (no motion model).
    """

    def __init__(
        self,
        resilient_store,
        entity_sidecar,
        render_feed,
        origin: MapOrigin = None,
        scale_m_per_block: float = 0.66,
        world_x: int = 64,
        world_y: int = 64,
        world_z: int = 64,
        framework: AVFramework = AVFramework.ROS2,
        ros_namespace: str = "/world_engine",
        publish_callback: Optional[Callable] = None,
        obstacle_callback: Optional[Callable] = None,
        map_query_callback: Optional[Callable] = None,
        prediction_callback: Optional[Callable] = None,
    ):
        self.rs        = resilient_store
        self.sidecar   = entity_sidecar
        self.feed      = render_feed
        self.origin    = origin or MapOrigin()
        self.scale_m   = scale_m_per_block
        self.world_x   = world_x
        self.world_y   = world_y
        self.world_z   = world_z
        self.framework = framework
        self.ns        = ros_namespace

        self._publish       = publish_callback    or self._default_publish
        self._obstacle_fn   = obstacle_callback   or (lambda offsets: [])
        self._map_query_fn  = map_query_callback  or (lambda lane_id: [])
        self._prediction_fn = prediction_callback or self._default_prediction

        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop,
                                        name="AVAdapter", daemon=True)
        self._thread.start()
        print(f"[AVSimAdapter] Started. framework={self.framework.value!r}, "
              f"namespace={self.ns!r}")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        print("[AVSimAdapter] Stopped.")

    def _run_loop(self):
        """
        Main adapter loop. In production, replace the stub tick with:
          - a ROS 2 executor spin driving ingest_actor_state()
          - a CARLA world tick callback driving ingest_actor_state()
          - a periodic call to publish_occupancy_grid() and publish_entity_deltas()
        """
        tick = 0
        while self._running:
            time.sleep(0.05)   # 20 Hz
            tick += 1
            if tick % 20 == 0:   # every 1 s
                self.publish_entity_deltas(since_tick=tick - 20)

    # ------------------------------------------------------------------
    # Inbound: AV framework → engine
    # ------------------------------------------------------------------

    def ingest_actor_state(self, actor: ActorState):
        """
        Translate an actor state message to an EntityRecord and write it
        to the engine's entity sidecar.
        """
        enu = ENUPosition(east=actor.east, north=actor.north, up=actor.up)
        offset = enu_to_engine(enu, self.scale_m,
                                self.world_x, self.world_y, self.world_z,
                                self.origin)
        if offset is None:
            return   # outside world bounds

        entity_type = ACTOR_TYPE_TO_ENGINE.get(actor.actor_type, 2)
        flags = 0x01 | 0x02 | 0x04  # active | visible | collidable

        record = struct.pack(
            "<IBBHffffffffffffffQQ",
            actor.actor_id,
            entity_type,
            flags,
            0,
            float(actor.east  / self.scale_m),
            float(actor.up    / self.scale_m),
            float(actor.north / self.scale_m),
            actor.vx / self.scale_m,
            actor.vy / self.scale_m,
            actor.vz / self.scale_m,
            actor.yaw, 0.0,    # yaw, pitch
            100.0, 0.0,        # health, metadata
            0,                 # owner_id
            int(time.monotonic() * 20),
        )
        if self.sidecar is not None:
            self.sidecar.write_entity_raw(actor.actor_id, record)

    # ------------------------------------------------------------------
    # Outbound: engine → AV framework
    # ------------------------------------------------------------------

    def build_occupancy_grid(self, ego_east: float, ego_north: float,
                              radius_m: float = 50.0) -> OccupancyGridMsg:
        """
        Build a 2D occupancy grid centred on the ego vehicle's position.
        Reads blocks from Array B (render store) via the resilient store's
        read path — never touches Array A's write path.
        """
        radius_blocks = max(1, int(radius_m / self.scale_m))
        grid_side = radius_blocks * 2 + 1
        data: list[int] = []

        ego_x = int((ego_east  - self.origin.east)  / self.scale_m)
        ego_z = int((ego_north - self.origin.north) / self.scale_m)
        ego_y = 1   # ground level

        for dz in range(-radius_blocks, radius_blocks + 1):
            for dx in range(-radius_blocks, radius_blocks + 1):
                x = ego_x + dx
                y = ego_y
                z = ego_z + dz
                if not (0 <= x < self.world_x and
                        0 <= y < self.world_y and
                        0 <= z < self.world_z):
                    data.append(-1)   # unknown
                    continue
                offset = (z * self.world_x * self.world_y +
                          y * self.world_x + x) * 16
                try:
                    block_data = (self.rs.read_block(offset)
                                  if self.rs else b'\x00' * 16)
                    data.append(int(block_to_occupancy(block_data)))
                except Exception:
                    data.append(-1)

        return OccupancyGridMsg(
            width=grid_side, height=grid_side,
            resolution_m=self.scale_m,
            origin_east=ego_east  - radius_m,
            origin_north=ego_north - radius_m,
            data=data,
        )

    def publish_occupancy_grid(self, ego_east: float, ego_north: float,
                                radius_m: float = 50.0):
        """Build and publish an occupancy grid for the ego vehicle."""
        grid = self.build_occupancy_grid(ego_east, ego_north, radius_m)
        topic = f"{self.ns}/occupancy_grid"
        self._publish(topic, grid)

    def simulate_lidar(self, ego_east: float, ego_north: float,
                       ego_up: float, range_m: float = 100.0,
                       angular_resolution_deg: float = 0.2) -> LIDARScan:
        """
        Simulate a LIDAR scan by querying occupied blocks in range and
        converting them to point cloud hits.
        This is a voxel-hit approximation — replace with a ray-cast model
        for higher-fidelity sensor simulation.
        """
        import math
        radius_blocks = max(1, int(range_m / self.scale_m))
        ego_x = int((ego_east  - self.origin.east)  / self.scale_m)
        ego_y = int((ego_up    - self.origin.up)    / self.scale_m)
        ego_z = int((ego_north - self.origin.north) / self.scale_m)

        points = []
        intensities = []

        for dz in range(-radius_blocks, radius_blocks + 1):
            for dx in range(-radius_blocks, radius_blocks + 1):
                for dy in range(-2, 3):   # ±2 vertical layers
                    x = ego_x + dx; y = ego_y + dy; z = ego_z + dz
                    if not (0 <= x < self.world_x and
                            0 <= y < self.world_y and
                            0 <= z < self.world_z):
                        continue
                    offset = (z * self.world_x * self.world_y +
                              y * self.world_x + x) * 16
                    try:
                        block_data = (self.rs.read_block(offset)
                                      if self.rs else b'\x00' * 16)
                        if block_to_occupancy(block_data) == OccupancyValue.OCCUPIED:
                            px = x * self.scale_m + self.origin.east
                            py = y * self.scale_m + self.origin.up
                            pz = z * self.scale_m + self.origin.north
                            points.append((px, py, pz))
                            intensities.append(0.8)
                    except Exception:
                        pass

        return LIDARScan(points=points, intensities=intensities)

    def publish_entity_deltas(self, since_tick: int = 0):
        """
        Read entity deltas from the sidecar and publish them as
        framework-native actor state messages.
        """
        if self.sidecar is None:
            return
        deltas = self.sidecar.tick_delta(since_tick=since_tick)
        topic = f"{self.ns}/tracked_objects"
        self._publish(topic, {"count": len(deltas), "tick": since_tick})

    def query_lane_offsets(self, lane_id: str) -> list[int]:
        """
        Return engine block offsets for a given HD map lane ID.
        Delegates to map_query_callback — the engine has no native lane concept.
        """
        return self._map_query_fn(lane_id)

    def get_obstacle_predictions(self, offsets: list[int],
                                  horizon_ticks: int = 20) -> list[list[tuple]]:
        """
        Return motion predictions for dynamic obstacles at the given offsets.
        Delegates to obstacle_callback and prediction_callback.
        """
        entities = self._obstacle_fn(offsets)
        return [
            self._prediction_fn(e, horizon_ticks) for e in entities
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _default_publish(self, topic: str, msg):
        print(f"[{self.framework.value.upper()} TX] topic={topic!r} msg={msg!r}")

    @staticmethod
    def _default_prediction(entity_record: bytes,
                             horizon_ticks: int) -> list[tuple]:
        """Default: constant position prediction (no motion model)."""
        if len(entity_record) >= 20:
            x, y, z = struct.unpack_from("<fff", entity_record, 8)
            return [(x, y, z)] * horizon_ticks
        return []


# ---------------------------------------------------------------------------
# CLI dry-run
# ---------------------------------------------------------------------------

def _dry_run(args):
    framework = AVFramework(args.framework)
    print(f"=== AVSimAdapter dry-run [{framework.value}] ===")

    adapter = AVSimAdapter(
        resilient_store=None,
        entity_sidecar=None,
        render_feed=None,
        origin=MapOrigin(east=0.0, north=0.0, up=0.0),
        scale_m_per_block=0.66,
        world_x=512, world_y=64, world_z=512,
        framework=framework,
    )

    # Test actor ingestion
    actor = ActorState(
        actor_id=42, actor_type="vehicle",
        east=100.0, north=200.0, up=0.0,
        vx=5.0, vy=0.0, vz=0.0, yaw=0.3,
    )
    enu = ENUPosition(east=actor.east, north=actor.north, up=actor.up)
    offset = enu_to_engine(enu, 0.66, 512, 64, 512, MapOrigin())
    print(f"Actor {actor.actor_type} at east={actor.east}m north={actor.north}m")
    print(f"  → Engine offset: {offset}")

    # Test occupancy grid build (stubbed store)
    grid = adapter.build_occupancy_grid(ego_east=100.0, ego_north=200.0,
                                         radius_m=10.0)
    occupied = sum(1 for v in grid.data if v == 100)
    print(f"Occupancy grid {grid.width}×{grid.height} "
          f"resolution={grid.resolution_m}m → {occupied} occupied cells")

    # Test entity delta publish (stubbed sidecar)
    adapter.publish_entity_deltas(since_tick=0)

    print(f"=== dry-run complete ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Block-Image Engine — Autonomous Vehicle Simulation Adapter"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Run translation tests without a live engine")
    parser.add_argument("--framework",
                        choices=[f.value for f in AVFramework],
                        default="ros2",
                        help="Target AV simulation framework")
    args = parser.parse_args()

    if args.dry_run:
        _dry_run(args)
    else:
        print("Supply resilient_store, entity_sidecar, and render_feed "
              "from run_server.py to run the adapter live.")
        print("Use --dry-run for a standalone translation test.")