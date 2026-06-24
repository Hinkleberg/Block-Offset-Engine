"""
tools/unreal/unreal_adapter.py
──────────────────────────────
OPTIONAL — Unreal Engine integration adapter.

Bridges the Block-Image Engine to Unreal Engine 5.x via:
  - Blueprint-callable Python API (via unreal.PythonBridgeSubsystem or
    the Unreal Python Plugin)
  - JSON delta stream that Unreal actors can poll or receive via TCP
  - A flat binary stream protocol for high-frequency block updates
    consumable by a custom UE C++ actor component

This adapter DOES NOT modify the core engine in any way.
The engine is hardware/software agnostic — this is a translation layer only.

Requirements (Unreal side):
  - Unreal Engine 5.x with Python Plugin enabled
  - Or any TCP socket listener in a UE5 C++ / Blueprint component

Requirements (Python side):
  - Standard library only (json, struct, socket, threading)

Usage:
    from tools.unreal.unreal_adapter import UnrealAdapter
    from core.block_layout import WorldLayout
    from core.render_feed import RenderDelta

    adapter = UnrealAdapter(layout, host="127.0.0.1", port=7100)
    adapter.start()

    # Wire into the engine's render feed as a send callback:
    feed.connect_client(
        client_id=99,
        send_cb=adapter.on_render_delta,
        view_radius=64,
    )
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
from dataclasses import asdict
from typing import Callable, List, Optional

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "core"))

from block_layout import Block, WorldLayout, BLOCK_SIZE
from render_feed import RenderDelta


# ---------------------------------------------------------------------------
# Wire protocol
# ---------------------------------------------------------------------------
# Binary frame:
#   [MAGIC 4B "UBIE"][frame_type 1B][payload_len 4B][payload NB]
#
# frame_type 0x01 = block delta batch
# frame_type 0x02 = entity delta batch
# frame_type 0x03 = JSON delta (for Blueprint consumers)

_FRAME_MAGIC   = b"UBIE"
_FRAME_HEADER  = struct.Struct("<4sBIi")   # magic, type, payload_len, tick


class FrameType:
    BLOCK_BATCH  = 0x01
    ENTITY_BATCH = 0x02
    JSON_DELTA   = 0x03


def _encode_block_batch(tick: int, deltas) -> bytes:
    """
    Each block: [offset 8B][data 16B] = 24B per block.
    Header: MAGIC(4) + type(1) + payload_len(4) + tick(4) = 13B
    """
    payload = b""
    for bd in deltas:
        payload += struct.pack("<Q", bd.offset) + bd.data
    header  = _FRAME_MAGIC + struct.pack("<BIi", FrameType.BLOCK_BATCH, len(payload), tick)
    return header + payload


def _encode_entity_batch(tick: int, entity_records) -> bytes:
    payload = json.dumps([
        {
            "entity_id":   r.entity_id,
            "entity_type": r.entity_type,
            "x": r.x, "y": r.y, "z": r.z,
            "vx": r.vx, "vy": r.vy, "vz": r.vz,
            "yaw": r.yaw, "pitch": r.pitch,
            "health": r.health,
            "flags": r.flags,
        }
        for r in entity_records
    ]).encode("utf-8")
    header = _FRAME_MAGIC + struct.pack("<BIi", FrameType.ENTITY_BATCH, len(payload), tick)
    return header + payload


def _encode_json_delta(delta: RenderDelta) -> bytes:
    doc = {
        "tick":          delta.tick,
        "client_id":     delta.client_id,
        "block_deltas":  [
            {"offset": bd.offset, "data": bd.data.hex()}
            for bd in delta.block_deltas
        ],
        "entity_deltas": [
            {
                "entity_id":  r.entity_id,
                "x": r.x, "y": r.y, "z": r.z,
                "health":     r.health,
                "last_tick":  r.last_tick,
            }
            for r in delta.entity_deltas
        ],
    }
    payload = json.dumps(doc).encode("utf-8")
    header  = _FRAME_MAGIC + struct.pack("<BIi", FrameType.JSON_DELTA, len(payload), delta.tick)
    return header + payload


# ---------------------------------------------------------------------------
# UnrealAdapter
# ---------------------------------------------------------------------------

class UnrealAdapter:
    """
    TCP server that streams RenderDelta frames to a connected Unreal client.

    The Unreal side connects to host:port and reads frames.
    A UE5 C++ ActorComponent or Blueprint can decode the binary or JSON frames.

    See tools/unreal/UE5_INTEGRATION.md for Unreal-side implementation notes.
    """

    def __init__(
        self,
        layout: WorldLayout,
        *,
        host:        str   = "127.0.0.1",
        port:        int   = 7100,
        use_binary:  bool  = True,
        backlog:     int   = 4,
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
        t = threading.Thread(target=self._accept_loop, daemon=True, name="ue-adapter-accept")
        t.start()
        print(f"[UnrealAdapter] Listening on {self._host}:{self._port}")

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
                print(f"[UnrealAdapter] UE client connected from {addr}")
                with self._lock:
                    self._clients.append(conn)
                threading.Thread(
                    target=self._watch_disconnect, args=(conn,), daemon=True
                ).start()
            except socket.timeout:
                continue
            except Exception:
                break

    def _watch_disconnect(self, conn: socket.socket) -> None:
        try:
            conn.recv(1)   # block until disconnect
        except Exception:
            pass
        with self._lock:
            try:
                self._clients.remove(conn)
            except ValueError:
                pass
        print("[UnrealAdapter] UE client disconnected")

    def on_render_delta(self, delta: RenderDelta) -> None:
        """
        Wire this as the send_cb for a RenderFeed client.
        Broadcasts the delta to all connected Unreal clients.
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
        return f"UnrealAdapter({self._host}:{self._port}, clients={n})"