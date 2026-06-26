# GODOT_INTEGRATION.md
# Block-Offset-Engine — Godot 4.x Integration

## Architecture

```
BOE Engine (Python)
    └── GodotAdapter (port 7300)
            └── TCP → StreamPeerTCP
                    └── BOEClient.gd (Godot scene node)
                            ├── signal block_delta_received
                            ├── signal entity_delta_received
                            └── signal json_delta_received
```

Port convention:
- Unreal → 7100
- Unity  → 7200
- Godot  → 7300

---

## Python Side — Start the Adapter

In your engine bootstrap (e.g. `run_server.py`):

```python
from tools.godot.godot_adapter import GodotAdapter

adapter = GodotAdapter(layout, host="127.0.0.1", port=7300)
adapter.start()

feed.connect_client(
    client_id=75,
    send_cb=adapter.on_render_delta,
    view_radius=48,
)
```

---

## Godot Side — Scene Setup

1. Copy `boe_client.gd` into your Godot project (e.g. `res://src/boe_client.gd`)
2. Add a Node to your scene, attach `boe_client.gd` as its script
3. Set `host` and `port` in the Inspector (or leave defaults: 127.0.0.1:7300)
4. Connect signals to your game logic

```gdscript
# In your main scene script
func _ready() -> void:
    var boe := $BOEClient
    boe.block_delta_received.connect(_on_blocks)
    boe.entity_delta_received.connect(_on_entities)

func _on_blocks(tick: int, blocks: Array) -> void:
    for b in blocks:
        var byte_offset: int          = b["offset"]
        var raw_data:    PackedByteArray = b["data"]
        # Map offset → world position using your BLOCK_SIZE constant
        # e.g. var world_pos = Vector3(offset_to_xyz(byte_offset))
        pass

func _on_entities(tick: int, entities: Array) -> void:
    for e in entities:
        # e.g. e["entity_id"], e["x"], e["y"], e["z"], e["health"]
        pass
```

---

## Binary Frame Format (UBIE protocol)

```
[MAGIC 4B "UBIE"][frame_type 1B][payload_len 4B LE][tick 4B LE signed][payload]

frame_type 0x01 — Block batch
    payload: N × 24 bytes
        [offset 8B uint64 LE][data 16B raw block]

frame_type 0x02 — Entity batch
    payload: UTF-8 JSON array of entity records

frame_type 0x03 — JSON delta (use_binary=False on server)
    payload: UTF-8 JSON object with tick, block_deltas, entity_deltas
```

---

## Switching to JSON Mode

If you want human-readable frames for debugging:

```python
adapter = GodotAdapter(layout, port=7300, use_binary=False)
```

On the Godot side, connect `json_delta_received` instead:

```gdscript
boe.json_delta_received.connect(_on_json)

func _on_json(tick: int, doc: Dictionary) -> void:
    print(tick, doc["block_deltas"])
```

---

## Tested With
- Godot 4.7-stable (Linux x86_64)
- Python 3.12
- Ubuntu 24.04
