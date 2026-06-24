# Unity Integration

Connects the Block-Image Engine to Unity 2022 LTS / 2023 / Unity 6
via a TCP frame stream. The core engine is unchanged.

---

## Architecture

```
Block-Image Engine (Python)
  └─ RenderFeed
       └─ UnityAdapter.on_render_delta()
            └─ TCP → Unity C# BlockImageReceiver MonoBehaviour
                        └─ decode frames
                        └─ update chunk meshes / entity GameObjects
```

---

## Frame Format

See `tools/unreal/UE5_INTEGRATION.md` — the wire format is identical.
`UBIE` magic, frame types 0x01 (block batch), 0x02 (entity batch), 0x03 (JSON).

---

## Unity C# Component

```csharp
// BlockImageReceiver.cs
using System;
using System.Net.Sockets;
using System.Threading;
using System.Collections.Concurrent;
using UnityEngine;

public class BlockImageReceiver : MonoBehaviour
{
    public string EngineHost = "127.0.0.1";
    public int    EnginePort = 7200;
    public int    WorldX = 64, WorldY = 64, WorldZ = 64;
    public float  BlockScale = 0.66f;   // metres per block

    private TcpClient _client;
    private NetworkStream _stream;
    private Thread _recvThread;
    private ConcurrentQueue<Action> _mainThreadQueue = new();

    void Start()
    {
        _recvThread = new Thread(ReceiveLoop) { IsBackground = true };
        _recvThread.Start();
    }

    void Update()
    {
        while (_mainThreadQueue.TryDequeue(out var action))
            action();
    }

    void ReceiveLoop()
    {
        _client = new TcpClient(EngineHost, EnginePort);
        _stream = _client.GetStream();
        byte[] hdr = new byte[13];  // MAGIC(4)+type(1)+plen(4)+tick(4)

        while (true)
        {
            ReadFull(_stream, hdr, 13);
            if (hdr[0] != 'U' || hdr[1] != 'B' || hdr[2] != 'I' || hdr[3] != 'E')
                break;  // desync

            byte  frameType = hdr[4];
            int   payloadLen = BitConverter.ToInt32(hdr, 5);
            int   tick       = BitConverter.ToInt32(hdr, 9);
            byte[] payload   = new byte[payloadLen];
            ReadFull(_stream, payload, payloadLen);

            if (frameType == 0x01)
                ParseBlockBatch(payload, tick);
            else if (frameType == 0x02)
                ParseEntityBatch(payload, tick);
        }
    }

    void ParseBlockBatch(byte[] payload, int tick)
    {
        int stride = 8 + 16;  // offset(8) + block_data(16)
        for (int i = 0; i + stride <= payload.Length; i += stride)
        {
            long offset    = BitConverter.ToInt64(payload, i);
            byte[] data    = new byte[16];
            Buffer.BlockCopy(payload, i + 8, data, 0, 16);

            long idx = offset / 16;
            int x = (int)(idx % WorldX);
            idx /= WorldX;
            int y = (int)(idx % WorldY);
            int z = (int)(idx / WorldY);

            byte blockType = data[0];
            // Enqueue mesh update on main thread
            int cx = x, cy = y, cz = z, bt = blockType;
            _mainThreadQueue.Enqueue(() => ApplyBlock(cx, cy, cz, bt));
        }
    }

    void ApplyBlock(int x, int y, int z, int blockType)
    {
        // Map blockType → your Unity prefab/material
        // Update chunk mesh builder here
        Vector3 worldPos = new Vector3(x * BlockScale, y * BlockScale, z * BlockScale);
        Debug.Log($"Block {blockType} at {worldPos}");
    }

    void ParseEntityBatch(byte[] payload, int tick)
    {
        string json = System.Text.Encoding.UTF8.GetString(payload);
        // Use JsonUtility or Newtonsoft.Json to deserialise
        // Update entity GameObjects on main thread
    }

    static void ReadFull(NetworkStream s, byte[] buf, int count)
    {
        int read = 0;
        while (read < count)
            read += s.Read(buf, read, count - read);
    }

    void OnDestroy()
    {
        _recvThread?.Interrupt();
        _client?.Close();
    }
}
```

---

## Connecting the Adapter

```python
from core.block_layout import WorldLayout
from core.render_feed import RenderFeed
from tools.unity.unity_adapter import UnityAdapter

layout  = WorldLayout(64, 64, 64)
adapter = UnityAdapter(layout, host="127.0.0.1", port=7200)
adapter.start()

feed = RenderFeed(layout, store_b, sidecar, tick_rate_hz=20)
feed.connect_client(client_id=50, send_cb=adapter.on_render_delta, view_radius=48)
feed.start()
```

---

## Notes

- Coordinate mapping: engine +Y is up. Unity is also +Y up. ✓
- Block scale: at 16 bytes/block the real-world ground resolution is ~66 cm.
  Set `BlockScale = 0.66f` (metres) or `6.6f` (decimetres) as suits your scene.
- Run the engine process separately from the Unity Editor. The adapter auto-reconnects.
- Tested with Unity 2022.3 LTS, 2023.2, Unity 6 Preview.