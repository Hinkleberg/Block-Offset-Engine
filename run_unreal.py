from __future__ import annotations
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "src", "block_engine"))

from block_engine.environment.block_layout import WorldLayout
from block_engine.authority.flat_store import FlatStore
from block_engine.authority.resilient_store import ResilientStore
from block_engine.interface.render_feed import RenderFeedServer
from block_engine.bridges.unreal_adapter import UnrealAdapter

WORLD_X      = 64
WORLD_Y      = 64
WORLD_Z      = 64
DB_PATH      = "world.db"
JOURNAL_PATH = "world.jrn"
RENDER_HOST  = "127.0.0.1"
RENDER_PORT  = 9000
UNREAL_HOST  = "127.0.0.1"
UNREAL_PORT  = 7100

async def main() -> None:
    print("=== Block-Offset-Engine → Unreal Bridge ===")
    layout = WorldLayout(WORLD_X, WORLD_Y, WORLD_Z)
    print(f"[layout]  {layout}")
    store_a = FlatStore(DB_PATH, layout)
    rs      = ResilientStore(store_a, journal_path=JOURNAL_PATH)
    print(f"[store]   Array A ready → {DB_PATH}")
    adapter = UnrealAdapter(layout, host=UNREAL_HOST, port=UNREAL_PORT)
    adapter.start()
    print(f"[unreal]  Waiting for UE5 on {UNREAL_HOST}:{UNREAL_PORT}")
    feed = RenderFeedServer(host=RENDER_HOST, port=RENDER_PORT)
    await feed.start()
    print(f"[feed]    RenderFeed live on {RENDER_HOST}:{RENDER_PORT}")
    print()
    print("Ready. Connect UE5 TCP socket to 127.0.0.1:7100")
    print("Press Ctrl+C to stop.")
    await asyncio.get_running_loop().create_future()

if __name__ == "__main__":
    asyncio.run(main())
