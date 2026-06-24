# Godot 4.x Integration

Connects the Block-Image Engine to Godot 4 via a TCP frame stream.

---

## Architecture

```
Block-Image Engine (Python)
  └─ RenderFeed
       └─ GodotAdapter.on_render_delta()
            └─ TCP → Godot StreamPeerTCP
                        └─ BlockImageReceiver.gd
                        └─ update VoxelTerrain / MeshInstance3D
```

---

## GDScript Component

```gdscript
# BlockImageReceiver.gd
extends Node

const ENGINE_HOST := "127.0.0.1"
const ENGINE_PORT := 7400
const BLOCK_SCALE := 0.66   # metres per block
const WORLD_X := 64
const WORLD_Y := 64
const WORLD_Z := 64

var _stream := StreamPeerTCP.new()
var _connected := false

func _ready() -> void:
    _stream.connect_to_host(ENGINE_HOST, ENGINE_PORT)

func _process(_delta: float) -> void:
    _stream.poll()
    if not _connected:
        if _stream.get_status() == StreamPeerTCP.STATUS_CONNECTED:
            _connected = true
            print("Connected to Block-Image Engine")
        return

    while _stream.get_available_bytes() >= 13:
        var magic := _stream.get_data(4)[1]
        if magic != PackedByteArray([85, 66, 73, 69]):  # "UBIE"
            push_error("Frame desync")
            return
        var frame_type := _stream.get_u8()
        var payload_len := _stream.get_32()
        var tick := _stream.get_32()

        if _stream.get_available_bytes() < payload_len:
            break  # wait for more data

        var payload := _stream.get_data(payload_len)[1]

        if frame_type == 0x01:
            _parse_block_batch(payload, tick)
        elif frame_type == 0x02:
            _parse_entity_batch(payload, tick)
        elif frame_type == 0x03:
            _parse_json_delta(payload, tick)

func _parse_block_batch(payload: PackedByteArray, tick: int) -> void:
    var stride := 24  # offset(8) + data(16)
    var i := 0
    while i + stride <= payload.size():
        var offset := payload.decode_s64(i)
        var data   := payload.slice(i + 8, i + 24)
        var block_type := data[0]

        var idx := offset / 16
        var x := idx % WORLD_X
        idx /= WORLD_X
        var y := idx % WORLD_Y
        var z := idx / WORLD_Y

        var world_pos := Vector3(x * BLOCK_SCALE, y * BLOCK_SCALE, z * BLOCK_SCALE)
        _apply_block(world_pos, block_type)
        i += stride

func _apply_block(pos: Vector3, block_type: int) -> void:
    # Update your VoxelTerrain, GridMap, or custom mesh builder here
    pass

func _parse_entity_batch(payload: PackedByteArray, _tick: int) -> void:
    var json_str := payload.get_string_from_utf8()
    var entities := JSON.parse_string(json_str)
    if entities == null:
        return
    for ent in entities:
        var pos := Vector3(ent["x"] * BLOCK_SCALE,
                           ent["y"] * BLOCK_SCALE,
                           ent["z"] * BLOCK_SCALE)
        # Update entity Node3D position here
        pass

func _parse_json_delta(payload: PackedByteArray, _tick: int) -> void:
    var json_str := payload.get_string_from_utf8()
    var delta := JSON.parse_string(json_str)
    if delta == null:
        return
    for bd in delta["block_deltas"]:
        var offset := int(bd["offset"])
        # decode hex data: bd["data"]
    for ent in delta["entity_deltas"]:
        pass
```

---

## Connecting the Adapter

```python
from core.block_layout import WorldLayout
from core.render_feed import RenderFeed
from tools.godot.godot_adapter import GodotAdapter

layout  = WorldLayout(64, 64, 64)
adapter = GodotAdapter(layout, host="127.0.0.1", port=7400)
adapter.start()

feed = RenderFeed(layout, store_b, sidecar, tick_rate_hz=20)
feed.connect_client(client_id=40, send_cb=adapter.on_render_delta, view_radius=48)
feed.start()
```

---

## Notes

- Godot 4 `StreamPeerTCP` is used in polling mode (_process). No threading needed on the Godot side.
- Coordinate system: engine +X east, +Y up, +Z south maps cleanly to Godot's +X right, +Y up, +Z back. A simple `Vector3(x, y, -z)` remaps if needed.
- Block scale: 0.66 m per block. Adjust `BLOCK_SCALE` to suit your scene units.
- Compatible with Godot 4.0, 4.1, 4.2, 4.3.
- For voxel terrain, consider pairing with the Godot Voxel Tools plugin (Zylann/godot_voxel), using this engine as the authoritative data source and streaming diffs into the VoxelTerrain node.