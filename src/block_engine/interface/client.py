"""
client.py

Thin client connecting to the render feed server and printing tile deltas.
"""
import asyncio
import json

async def run_client(host="127.0.0.1", port=9000, center=10, radius=4):
    reader, writer = await asyncio.open_connection(host, port)
    sub = {"viewport_center_tile": center, "radius_tiles": radius}
    writer.write((json.dumps(sub) + "\n").encode("utf-8"))
    await writer.drain()
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            msg = json.loads(line.decode("utf-8"))
            print("Received tick", msg.get("tick"), "tiles:", [t["tile"] for t in msg.get("tiles", [])])
    except asyncio.CancelledError:
        pass
    finally:
        writer.close()
        await writer.wait_closed()

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--center", type=int, default=10)
    p.add_argument("--radius", type=int, default=4)
    args = p.parse_args()
    asyncio.run(run_client(args.host, args.port, args.center, args.radius))
