"""
engine/render_feed.py

A simple asyncio TCP server that accepts client connections.
Clients subscribe by sending JSON: {"viewport_center_tile": <tile>, "radius_tiles": <r>}
Server sends delta messages for tiles that changed: {"tiles": [{"tile": idx, "payload": base64}], "tick": n}

This is a non-optimized prototype to demonstrate streaming deltas only.
"""
import asyncio
import json
import base64
from typing import Dict, Set, List

class RenderFeedServer:
    def __init__(self, host="127.0.0.1", port=9000):
        self.host = host
        self.port = port
        self.clients = {}  # writer -> subscription dict
        self.subscriptions = {}  # client -> (center, radius)
        self.tile_version = {}  # tile_index -> monotonic version int
        self.tick = 0

    async def start(self):
        self.server = await asyncio.start_server(self.handle_client, self.host, self.port)
        print(f"Render feed listening on {self.host}:{self.port}")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        print("client connected", addr)
        # expect a single json subscription line
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            sub = json.loads(line.decode("utf-8"))
            self.subscriptions[writer] = sub
            self.clients[writer] = (reader, writer)
            while not reader.at_eof():
                await asyncio.sleep(0.1)
            print("client disconnected", addr)
        except Exception:
            writer.close()
            await writer.wait_closed()
        finally:
            if writer in self.subscriptions:
                del self.subscriptions[writer]
            if writer in self.clients:
                del self.clients[writer]

    def notify_tiles_changed(self, tiles: List[int], world_reader: Callable[[int], bytes]):
        # Called by external code when tiles changed
        self.tick += 1
        for reader_writer, sub in list(self.subscriptions.items()):
            writer = reader_writer
            if writer.is_closing():
                continue
            center = sub.get("viewport_center_tile", 0)
            radius = sub.get("radius_tiles", 4)
            # build list of tiles in viewport intersecting changed tiles
            view_range = set(range(max(0, center - radius), center + radius + 1))
            intersect = [t for t in tiles if t in view_range]
            if not intersect:
                continue
            payloads = []
            for t in intersect:
                payload = world_reader(t)
                payload_b64 = base64.b64encode(payload).decode("ascii")
                payloads.append({"tile": t, "payload": payload_b64})
            msg = {"tiles": payloads, "tick": self.tick}
            try:
                writer.write((json.dumps(msg) + "\n").encode("utf-8"))
                asyncio.create_task(writer.drain())
            except Exception:
                pass
