# Godot 4 Connector

This is an actual Godot-side tool, not integration notes.

## Install
Copy `godot/addons/block_image_engine` into your Godot project's `addons/` directory, then enable **Block Image Engine Connector** in Project Settings → Plugins. It registers `BlockImageEngine` as an autoload.

## What it does
- Opens a WebSocket transport to a Region Server adapter.
- Sends `move_intent`, never direct transform writes.
- Receives `render_delta` payloads from the engine's render feed.
- Exposes `mutate_world()` for studio tools; mutations remain server-validated and journaled.
- Contains no storage, authority, physics, or replication logic.

## Server integration
Wire `Godot4Bridge` to your existing `RegionServer`, `RenderFeed`, movement resolver, and studio mutation entry point. The bridge is deliberately transport-only so the engine remains hardware/software agnostic.

## Protocol
Client → server: `connect`, `move_intent`, `world_mutation`.
Server → client: `render_delta`, `error`.

A production transport may replace WebSocket with ENet, QUIC, shared memory, or a direct frame adapter without changing engine semantics.
