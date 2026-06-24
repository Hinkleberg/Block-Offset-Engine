"""
tools/godot/godot_adapter.py
─────────────────────────────
OPTIONAL — Godot 4.x integration adapter.

Bridges the Block-Image Engine to Godot via:
  - TCP binary frame stream (UBIE protocol) consumable by Godot's
    StreamPeerTCP / PacketPeerStream
  - JSON mode for GDScript-friendly parsing
  - A GDScript stub for connecting and decoding frames

See tools/godot/GODOT_INTEGRATION.md for the GDScript implementation.
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


class GodotAdapter:
    """
    TCP server streaming UBIE frames to Godot 4.x clients.
    GDScript decodes via StreamPeerTCP; see GODOT_INTEGRATION.md.

    Usage:
        adapter = GodotAdapter(layout, host="127.0.0.1", port=7400)
        adapter.start()
        feed.connect_client(client_id=40, send_cb=adapter.on_render_delta, view_radius=48)
    """

    def __init__(
        self,
        layout: WorldLayout,
        *,
        host:        str   = "127.0.0.1",
        port:        int   = 7400,
        use_binary:  bool  = True,
        block_scale: float = 0.66,
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
        t = threading.Thread(target=self._accept_loop, daemon=True, name="godot-adapter")
        t.start()
        print(f"[GodotAdapter] Listening on {self._host}:{self._port}")

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
                print(f"[GodotAdapter] Godot client connected from {addr}")
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

    def offset_to_godot_vector3(self, offset: int):
        """
        Convert a block byte offset to a Godot Vector3-compatible tuple (x, y, z)
        in metres. Godot 4 uses +Y up, right-handed coordinate system.
        """
        idx = offset // BLOCK_SIZE
        x   = idx % self._layout.world_x
        idx //= self._layout.world_x
        y   = idx % self._layout.world_y
        z   = idx // self._layout.world_y
        s   = self._block_scale
        return (x * s, y * s, z * s)

    def __repr__(self) -> str:
        with self._lock:
            n = len(self._clients)
        return f"GodotAdapter({self._host}:{self._port}, clients={n})"