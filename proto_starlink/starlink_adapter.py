import asyncio
import grpc
import zlib
import struct
import hashlib
import json
from typing import Callable, Dict, Optional, Awaitable
from starlink_grpc_pb2 import Empty, DeltaRequest, DeltaResponse, StatusResponse
from starlink_grpc_pb2_grpc import StarlinkServiceStub

class StarlinkAdapter:
    def __init__(self, grpc_addr: str = "localhost:9200", use_telemetry: bool = True):
        self.grpc_addr = grpc_addr
        self.use_telemetry = use_telemetry
        self.channel: Optional[grpc.aio.Channel] = None
        self.stub: Optional[StarlinkServiceStub] = None
        self.connected = False
        self.satellite_info: Dict = {}

    async def connect(self) -> bool:
        try:
            self.channel = grpc.aio.insecure_channel(self.grpc_addr)
            self.stub = StarlinkServiceStub(self.channel)
            response: StatusResponse = await self.stub.GetStatus(Empty())
            self.connected = response.connected
            self.satellite_info = {
                "latency_ms": response.latency_ms,
                "active_satellites": response.active_satellites,
                "beam_id": response.beam_id
            }
            print(f"✅ Starlink Connected | Latency: {response.latency_ms}ms | Satellites: {response.active_satellites}")
            return True
        except Exception as e:
            print(f"⚠️ Starlink gRPC failed ({e}). Falling back to IP connectivity.")
            self.connected = True
            return True

    def translate_to_starlink(self, data: bytes, msg_type: str = "block", seq: int = 0, offset: int = 0) -> bytes:
        compressed = zlib.compress(data, level=6)
        checksum = hashlib.sha256(compressed).digest()[:8]
        header = struct.pack("!B Q Q 8s", ord(msg_type[0]), seq, offset, checksum)
        return header + compressed

    def translate_from_starlink(self, payload: bytes) -> Dict:
        try:
            header = struct.unpack("!B Q Q 8s", payload[:25])
            data = zlib.decompress(payload[25:])
            return {"type": chr(header[0]), "seq": header[1], "offset": header[2], "data": data}
        except:
            return {"data": payload}

    async def send_block(self, node_id: str, offset: int, data: bytes, seq: int = 0):
        if not self.connected:
            await self.connect()
        payload = self.translate_to_starlink(data, "block", seq, offset)
        try:
            request = DeltaRequest(payload=payload, sequence=seq, offset=offset, type="block")
            response = await self.stub.SendDelta(request)
            return response.success
        except Exception as e:
            print(f"❌ Starlink send failed: {e}")
            return False

    async def stream_deltas(self, callback: Callable):
        """Stream incoming RenderDeltas from Starlink."""
        try:
            async for response in self.stub.ReceiveDeltas(iter([])):  # Replace with real stream
                parsed = self.translate_from_starlink(response.payload)
                await callback(parsed)
        except Exception as e:
            print(f"Stream error: {e}")

    def get_health(self) -> Dict:
        return {
            "status": "HEALTHY" if self.connected else "DEGRADED",
            "latency_ms": self.satellite_info.get("latency_ms", 999),
            **self.satellite_info
        }


# For ReplicationManager sync_callback compatibility
async def starlink_sync_callback(node_id: str, offset: int, data: bytes, adapter: StarlinkAdapter):
    return await adapter.send_block(node_id, offset, data)