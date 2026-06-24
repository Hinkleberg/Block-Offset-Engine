"""Godot 4 protocol bridge; transport adapter only, never storage authority."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable

@dataclass
class Godot4Bridge:
    connect_client: Callable[..., Any]
    update_move: Callable[..., Any]
    submit_mutation: Callable[..., Any]

    def handle(self, message: dict, send: Callable[[dict], None]) -> None:
        op = message.get("op")
        if op == "connect":
            pos = message.get("position", [0,0,0])
            self.connect_client(message["client_id"], message.get("view_radius", 32), pos, send)
        elif op == "move_intent":
            self.update_move(message)
        elif op == "world_mutation":
            self.submit_mutation(message["mutation"])
        else:
            send({"op":"error","message":f"unsupported operation: {op}"})
