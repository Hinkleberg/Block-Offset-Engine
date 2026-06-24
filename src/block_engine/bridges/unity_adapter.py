"""
tools/unity/unity_adapter.py
─────────────────────────────
OPTIONAL — Unity integration adapter.

Bridges the Block-Image Engine to Unity via:
  - TCP binary frame stream (same UBIE protocol as Unreal adapter)
  - JSON mode for Unity C# JsonUtility / Newtonsoft.Json consumers
  - Compatible with Unity 2022 LTS, 2023, Unity 6

See tools/unity/UNITY_INTEGRATION.md for the C# component implementation.
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

# Reuse same frame encoding as Unreal adapter
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "unreal"))
from unreal_adapter import (
    _encode_block_batch,
    _encode_entity_batch,
    _encode_json_delta,
)


class UnityAdapter:
    """
    TCP server streaming RenderDelta frames to Unity C# clients.
    Compatible with TcpClient / NetworkStream on the Unity side.

    Usage:
        adapter = UnityAdapter(layout, host="127.0.0.1", port=7200)
        adapter.start()
        feed.connect_client(client_id=50, send_cb=adapter.on_render_delta, view_radius=48)
    """

    def __init__(
        self,
        layout: WorldLayout,
        *,
        host:       str  = "127.0.0.1",
        port:       int  = 7200,
        use_binary: bool = True,
    ):
        self._layout     = layout
        self._host       = host
        self._port       = port
        self._use_binary = use_binary
        self._clients:   List[socket.socket] = []
        self._lock       = threading.Lock()
        self._running    = False
        self._server:    Optional[socket.socket] = None

    def start(self) -> None:
        self._running = True
        self._server  = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self._host, self._port))
        self._server.listen(4)
        self._server.settimeout(1.0)
        t = threading.Thread(target=self._accept_loop, daemon=True, name="unity-adapter")
        t.start()
        print(f"[UnityAdapter] Listening on {self._host}:{self._port}")

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
                print(f"[UnityAdapter] Unity client connected from {addr}")
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

    def __repr__(self) -> str:
        with self._lock:
            n = len(self._clients)
        return f"UnityAdapter({self._host}:{self._port}, clients={n})"