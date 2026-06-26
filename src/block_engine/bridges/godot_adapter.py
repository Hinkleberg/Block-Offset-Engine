"""
tools/godot/godot_adapter.py
─────────────────────────────
OPTIONAL — Godot Engine integration adapter.

Bridges the Block-Image Engine to Godot 4.x via:
  - TCP binary frame stream (same UBIE protocol as Unreal/Unity adapters)
  - JSON mode for GDScript consumers that prefer readability over speed
  - Compatible with Godot 4.6+, GDScript and C# (.NET) project types

See tools/godot/GODOT_INTEGRATION.md for the GDScript component implementation.

Usage:
    from tools.godot.godot_adapter import GodotAdapter
    from core.block_layout import WorldLayout

    adapter = GodotAdapter(layout, host="127.0.0.1", port=7300)
    adapter.start()

    feed.connect_client(
        client_id=75,
        send_cb=adapter.on_render_delta,
        view_radius=48,
    )
"""

from __future__ import annotations

import socket
import threading
from typing import List, Optional

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "core"))

from block_layout import WorldLayout, BLOCK_SIZE
from render_delta import RenderDelta

# Reuse same frame encoding as Unreal/Unity adapters
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "unreal"))
from unreal_adapter import (
    _encode_block_batch,
    _encode_entity_batch,
    _encode_json_delta,
)


class GodotAdapter:
    """
    TCP server streaming RenderDelta frames to Godot 4.x clients.
    Compatible with Godot's StreamPeerTCP / PacketPeerStream on the GDScript side.

    Port convention:
        Unreal → 7100
        Unity  → 7200
        Godot  → 7300

    Usage:
        adapter = GodotAdapter(layout, host="127.0.0.1", port=7300)
        adapter.start()
        feed.connect_client(client_id=75, send_cb=adapter.on_render_delta, view_radius=48)
    """

    def __init__(
        self,
        layout:     WorldLayout,
        *,
        host:       str  = "127.0.0.1",
        port:       int  = 7300,
        use_binary: bool = True,
        backlog:    int  = 4,
    ):
        self._layout     = layout
        self._host       = host
        self._port       = port
        self._use_binary = use_binary
        self._backlog    = backlog
        self._clients:   List[socket.socket] = []
        self._lock       = threading.Lock()
        self._running    = False
        self._server:    Optional[socket.socket] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._server  = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self._host, self._port))
        self._server.listen(self._backlog)
        self._server.settimeout(1.0)
        t = threading.Thread(
            target=self._accept_loop, daemon=True, name="godot-adapter-accept"
        )
        t.start()
        print(f"[GodotAdapter] Listening on {self._host}:{self._port}")

    def stop(self) -> None:
        self._running = False
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, addr = self._server.accept()
                print(f"[GodotAdapter] Godot client connected from {addr}")
                with self._lock:
                    self._clients.append(conn)
                threading.Thread(
                    target=self._watch_disconnect,
                    args=(conn,),
                    daemon=True,
                    name="godot-adapter-watch",
                ).start()
            except socket.timeout:
                continue
            except Exception:
                break

    def _watch_disconnect(self, conn: socket.socket) -> None:
        try:
            conn.recv(1)  # block until client drops
        except Exception:
            pass
        with self._lock:
            try:
                self._clients.remove(conn)
            except ValueError:
                pass
        print("[GodotAdapter] Godot client disconnected")

    # ------------------------------------------------------------------
    # Render feed callback
    # ------------------------------------------------------------------

    def on_render_delta(self, delta: RenderDelta) -> None:
        """
        Wire this as the send_cb for a RenderFeed client.
        Broadcasts the delta to all connected Godot clients.
        """
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
        return f"GodotAdapter({self._host}:{self._port}, clients={n})"
