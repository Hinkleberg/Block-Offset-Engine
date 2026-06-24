"""
tools/o3de/o3de_adapter.py
──────────────────────────
OPTIONAL — Open 3D Engine (O3DE) integration adapter.

O3DE uses a Python scripting subsystem (Editor Python Bindings) and
a C++ Gem architecture. This adapter:

  1. Streams UBIE frames over TCP (same protocol as Unreal/Unity adapters)
  2. Provides a helper that translates block offsets to O3DE entity
     transform coordinates, compatible with O3DE's AZ::Transform / Vector3
  3. Generates a minimal O3DE-compatible JSON manifest for chunk loading

See tools/o3de/O3DE_INTEGRATION.md for the Gem and Python script examples.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
from typing import List, Optional

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "core"))

from block_layout import WorldLayout, BLOCK_SIZE
from render_feed import RenderDelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "unreal"))
from unreal_adapter import (
    _encode_block_batch,
    _encode_entity_batch,
    _encode_json_delta,
)


class O3DEAdapter:
    """
    TCP server streaming UBIE frames to an O3DE Gem component or
    Editor Python script.

    Usage:
        adapter = O3DEAdapter(layout, host="127.0.0.1", port=7300)
        adapter.start()
        feed.connect_client(client_id=30, send_cb=adapter.on_render_delta, view_radius=64)
    """

    def __init__(
        self,
        layout: WorldLayout,
        *,
        host:        str   = "127.0.0.1",
        port:        int   = 7300,
        use_binary:  bool  = True,
        block_scale: float = 0.66,    # metres per block
    ):
        self._layout      = layout
        self._host        = host
        self._port        = port
        self._use_binary  = use_binary
        self._block_scale = block_scale
        self._clients:    List[socket.socket] = []
        self._lock        = threading.Lock()
        self._running     = False
        self._server:     Optional[socket.socket] = None

    def start(self) -> None:
        self._running = True
        self._server  = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self._host, self._port))
        self._server.listen(4)
        self._server.settimeout(1.0)
        t = threading.Thread(target=self._accept_loop, daemon=True, name="o3de-adapter")
        t.start()
        print(f"[O3DEAdapter] Listening on {self._host}:{self._port}")

    def stop(self) -> None:
        self._running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._server.accept()
                print(f"[O3DEAdapter] O3DE client connected from {addr}")
                with self._lock:
                    self._clients.append(conn)
            except socket.timeout:
                continue
            except Exception:
                break

    def on_render_delta(self, delta: RenderDelta) -> None:
        if self._use_binary:
            frames = []
            if delta.block_deltas:
                frames.append(_encode_block_batch(delta.tick, delta.block_deltas))
            if delta.entity_deltas:
                frames.append(_encode_entity_batch(delta.tick, delta.entity_deltas))
        else:
            frames = [_encode_json_delta(delta)]

        if not frames:
            return

        with self._lock:
            dead = []
            for conn in self._clients:
                try:
                    for frame in frames:
                        conn.sendall(frame)
                except Exception:
                    dead.append(conn)
            for conn in dead:
                self._clients.remove(conn)

    def offset_to_o3de_vector(self, offset: int):
        """
        Convert a block byte offset to an (x, y, z) tuple in O3DE world units.
        O3DE uses right-handed +Y up coordinate system.
        Returns (x_m, y_m, z_m) in metres.
        """
        idx = offset // BLOCK_SIZE
        x   = idx % self._layout.world_x
        idx //= self._layout.world_x
        y   = idx % self._layout.world_y
        z   = idx // self._layout.world_y
        s   = self._block_scale
        return (x * s, y * s, z * s)

    def generate_chunk_manifest(self) -> str:
        """
        Generate a JSON manifest of chunk positions for O3DE chunk streaming.
        Compatible with O3DE's StreamingVolumeComponent or custom Gem loaders.
        """
        chunks = []
        cx_max = self._layout.world_x // 16
        cy_max = self._layout.world_y // 16
        cz_max = self._layout.world_z // 16
        s = self._block_scale * 16
        for cz in range(cz_max):
            for cy in range(cy_max):
                for cx in range(cx_max):
                    chunks.append({
                        "chunk_id": f"{cx}_{cy}_{cz}",
                        "world_position": {
                            "x": cx * s,
                            "y": cy * s,
                            "z": cz * s,
                        },
                        "size_metres": s,
                    })
        return json.dumps({"chunks": chunks}, indent=2)

    def __repr__(self) -> str:
        with self._lock:
            n = len(self._clients)
        return f"O3DEAdapter({self._host}:{self._port}, clients={n})"