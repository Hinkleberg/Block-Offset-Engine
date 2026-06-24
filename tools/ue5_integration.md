# Unreal Engine 5.x Integration

This tool connects the Block-Image Engine to Unreal Engine 5 via a TCP
frame stream. The core engine is unchanged; this adapter is entirely optional.

---

## Architecture

```
Block-Image Engine (Python)
  └─ RenderFeed
       └─ UnrealAdapter.on_render_delta()   ← send_cb
            └─ TCP socket → UE5 ActorComponent
                              └─ decode frames
                              └─ update UE5 world
```

The engine streams block and entity deltas at 20 Hz.
Unreal applies them to its own world representation.
The engine does not know or care what renderer is on the other end.

---

## Frame Format

All frames:
```
[MAGIC 4B "UBIE"][frame_type 1B][payload_len 4B][tick 4B][payload NB]
```

### Frame Type 0x01 — Block Batch (binary)
```
payload: repeated [offset u64 8B][block_data 16B]
```
One entry per changed block in this tick's view frustum.
`offset` is the same byte offset formula: `(z×W×H + y×W + x) × 16`

### Frame Type 0x02 — Entity Batch (JSON UTF-8)
```json
[{"entity_id":1,"entity_type":1,"x":32.0,"y":64.0,"z":32.0,
  "vx":0.0,"vy":0.0,"vz":0.0,"yaw":0.0,"pitch":0.0,
  "health":100.0,"flags":3}, ...]
```

### Frame Type 0x03 — JSON Delta (Blueprint-friendly)
Full delta as JSON — block offsets as hex strings, entities as objects.

---

## UE5 C++ Component (outline)

```cpp
// BlockImageReceiverComponent.h
UCLASS()
class UBlockImageReceiverComponent : public UActorComponent
{
    GENERATED_BODY()
public:
    UPROPERTY(EditAnywhere) FString EngineHost = "127.0.0.1";
    UPROPERTY(EditAnywhere) int32   EnginePort = 7100;

    virtual void BeginPlay() override;
    virtual void TickComponent(float DeltaTime, ...) override;

private:
    FSocket* Socket = nullptr;
    TArray<uint8> RecvBuffer;

    void ConnectToEngine();
    void ProcessFrames();
    void ApplyBlockDelta(int64 Offset, const TArray<uint8>& Data);
    void ApplyEntityDelta(const FString& JsonPayload);

    // Converts engine byte offset to UE5 world position
    FVector OffsetToWorld(int64 Offset) const;
};
```

**OffsetToWorld** maps the engine's byte offset back to (x,y,z) using:
```
idx = offset / 16
x   = idx % WORLD_X
idx /= WORLD_X
y   = idx % WORLD_Y
z   = idx / WORLD_Y
```
Then scale to UE5 units (e.g. × 66 cm per block).

---

## Blueprint Integration

Enable the Unreal Python Plugin. In your Level Blueprint:

```python
# Called from UE5 Python console or Blueprint Python node
import socket, struct, json, threading

HOST, PORT = "127.0.0.1", 7100
sock = socket.socket()
sock.connect((HOST, PORT))

def recv_loop():
    HEADER = 13  # 4+1+4+4
    while True:
        hdr = sock.recv(HEADER)
        magic, ftype, plen, tick = struct.unpack("<4sBIi", hdr)
        payload = sock.recv(plen)
        if ftype == 0x03:
            delta = json.loads(payload)
            # update_world(delta)  ← your Blueprint call

t = threading.Thread(target=recv_loop, daemon=True)
t.start()
```

---

## Connecting the Adapter

```python
from core.block_layout import WorldLayout
from core.render_store import RenderStore
from core.entity_sidecar import EntitySidecar
from core.render_feed import RenderFeed
from tools.unreal.unreal_adapter import UnrealAdapter

layout  = WorldLayout(64, 64, 64)
# ... set up store_b, sidecar ...

adapter = UnrealAdapter(layout, host="127.0.0.1", port=7100)
adapter.start()

feed = RenderFeed(layout, store_b, sidecar, tick_rate_hz=20)
feed.connect_client(
    client_id=99,
    send_cb=adapter.on_render_delta,
    view_radius=64,
)
feed.start()
```

---

## Notes

- The engine is **not** a UE5 plugin. It is a standalone process that streams
  deltas over TCP. UE5 consumes the stream via a C++ component or Blueprint.
- Block resolution is ~66 cm × 66 cm of real-world ground at 16 bytes/block.
  Scale your UE5 meshes accordingly.
- The engine's coordinate system: +X east, +Y up, +Z south. Remap to UE5's
  left-handed system (+X forward, +Y right, +Z up) in your component.
- UE5 version tested against: 5.3, 5.4. The TCP protocol has no UE version
  dependency; any version with socket support works.