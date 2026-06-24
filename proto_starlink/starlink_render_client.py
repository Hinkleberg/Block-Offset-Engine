import asyncio
from starlink_adapter import StarlinkAdapter

class StarlinkRenderClient:
    def __init__(self, client_id: int):
        self.client_id = client_id
        self.adapter = StarlinkAdapter()
        self.last_seq = 0

    async def connect(self):
        await self.adapter.connect()
        print(f"Client {self.client_id} connected via Starlink")

    async def on_delta_received(self, delta: Dict):
        print(f"Received delta for offset {delta.get('offset')} | Size: {len(delta.get('data', b''))}")
        # Apply to local render buffer here
        self.last_seq = delta.get("seq", self.last_seq)

    async def run(self):
        await self.connect()
        # Start listening for deltas (in real impl, use proper streaming)
        while True:
            await asyncio.sleep(1)