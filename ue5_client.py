import socket
import struct
import threading
import unreal

HOST = "127.0.0.1"
PORT = 7100
BLOCK_SIZE_CM = 66.0  # 0.66m in UE5 cm units

FRAME_MAGIC = b"UBIE"
HEADER_SIZE = 13  # 4 magic + 1 type + 4 payload_len + 4 tick

spawned_blocks = {}

def spawn_block(x, y, z):
    coords = (x, y, z)
    if coords in spawned_blocks:
        return
    loc = unreal.Vector(x * BLOCK_SIZE_CM, y * BLOCK_SIZE_CM, z * BLOCK_SIZE_CM)
    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.StaticMeshActor, loc
    )
    mesh = unreal.load_asset("/Engine/BasicShapes/Cube.Cube")
    actor.static_mesh_component.set_static_mesh(mesh)
    actor.set_actor_scale3d(unreal.Vector(0.66, 0.66, 0.66))
    spawned_blocks[coords] = actor

def parse_block_batch(payload, tick):
    offset = 0
    while offset + 24 <= len(payload):
        blk_offset = struct.unpack_from("<Q", payload, offset)[0]
        offset += 8
        data = payload[offset:offset+16]
        offset += 16
        # Convert flat offset to x,y,z (64x64x64 world)
        W = 64
        z = blk_offset // (W * W)
        y = (blk_offset % (W * W)) // W
        x = blk_offset % W
        unreal.log(f"Block at ({x},{y},{z})")
        spawn_block(x, y, z)

def listen():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((HOST, PORT))
    unreal.log(f"Connected to Block Engine on {HOST}:{PORT}")
    buf = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        while len(buf) >= HEADER_SIZE:
            if buf[:4] != FRAME_MAGIC:
                break
            frame_type = buf[4]
            payload_len = struct.unpack_from("<I", buf, 5)[0]
            tick = struct.unpack_from("<i", buf, 9)[0]
            total = HEADER_SIZE + payload_len
            if len(buf) < total:
                break
            payload = buf[HEADER_SIZE:total]
            buf = buf[total:]
            if frame_type == 0x01:
                parse_block_batch(payload, tick)

t = threading.Thread(target=listen, daemon=True)
t.start()
unreal.log("Block Engine client started.")
