## boe_client.gd
## ──────────────
## Block-Offset-Engine client for Godot 4.x
##
## Connects to the GodotAdapter TCP server (default port 7300) and
## decodes incoming UBIE binary frames into usable Godot data.
##
## Wire protocol (matches unreal_adapter.py / godot_adapter.py):
##   [MAGIC 4B "UBIE"][frame_type 1B][payload_len 4B][tick 4B][payload NB]
##
##   frame_type 0x01 = block delta batch   → block_delta_received signal
##   frame_type 0x02 = entity delta batch  → entity_delta_received signal
##   frame_type 0x03 = JSON delta          → json_delta_received signal
##
## Usage — attach to any Node in your scene:
##
##   var client = BOEClient.new()
##   add_child(client)
##   client.connect("block_delta_received",  _on_blocks)
##   client.connect("entity_delta_received", _on_entities)
##   client.connect("json_delta_received",   _on_json)
##   client.connect_to_engine()

extends Node
class_name BOEClient


# ── Signals ────────────────────────────────────────────────────────────────

## Emitted when a block delta batch frame arrives.
## blocks: Array of Dicts { "offset": int, "data": PackedByteArray }
signal block_delta_received(tick: int, blocks: Array)

## Emitted when an entity delta batch arrives.
## entities: Array of Dicts with position/velocity/health/flags fields
signal entity_delta_received(tick: int, entities: Array)

## Emitted when a JSON delta frame arrives (use_binary=False on server).
signal json_delta_received(tick: int, doc: Dictionary)

## Emitted on successful connection.
signal connected_to_engine()

## Emitted when the connection drops.
signal disconnected_from_engine()


# ── Config ─────────────────────────────────────────────────────────────────

@export var host: String = "127.0.0.1"
@export var port: int    = 7300
@export var auto_reconnect: bool   = true
@export var reconnect_delay: float = 2.0


# ── Constants ──────────────────────────────────────────────────────────────

const MAGIC         := "UBIE"
const HEADER_SIZE   := 9    # type(1) + payload_len(4) + tick(4)
const FRAME_PREFIX  := 4    # magic bytes
const TOTAL_HEADER  := FRAME_PREFIX + HEADER_SIZE   # 13 bytes

const FRAME_BLOCK_BATCH  := 0x01
const FRAME_ENTITY_BATCH := 0x02
const FRAME_JSON_DELTA   := 0x03

const BLOCK_DATA_SIZE    := 16   # bd.data is 16 bytes per block
const BLOCK_RECORD_SIZE  := 24   # offset(8) + data(16)


# ── State ──────────────────────────────────────────────────────────────────

var _peer:    StreamPeerTCP = null
var _buf:     PackedByteArray = PackedByteArray()
var _connected: bool = false


# ── Lifecycle ──────────────────────────────────────────────────────────────

func _ready() -> void:
	connect_to_engine()


func _process(_delta: float) -> void:
	if _peer == null:
		return

	_peer.poll()
	var status := _peer.get_status()

	if status == StreamPeerTCP.STATUS_CONNECTED:
		if not _connected:
			_connected = true
			print("[BOEClient] Connected to BOE engine on port ", port)
			emit_signal("connected_to_engine")
		_drain()

	elif status == StreamPeerTCP.STATUS_ERROR or status == StreamPeerTCP.STATUS_NONE:
		if _connected:
			_connected = false
			print("[BOEClient] Disconnected from BOE engine")
			emit_signal("disconnected_from_engine")
			if auto_reconnect:
				await get_tree().create_timer(reconnect_delay).timeout
				connect_to_engine()


# ── Public API ─────────────────────────────────────────────────────────────

func connect_to_engine() -> void:
	_peer = StreamPeerTCP.new()
	_buf  = PackedByteArray()
	var err := _peer.connect_to_host(host, port)
	if err != OK:
		push_error("[BOEClient] connect_to_host failed: %s" % error_string(err))
	else:
		print("[BOEClient] Connecting to %s:%d ..." % [host, port])


func disconnect_from_engine() -> void:
	if _peer:
		_peer.disconnect_from_host()
	_connected = false


# ── Internal ───────────────────────────────────────────────────────────────

func _drain() -> void:
	var available := _peer.get_available_bytes()
	if available > 0:
		var chunk := _peer.get_data(available)
		if chunk[0] == OK:
			_buf.append_array(chunk[1])

	# Parse as many complete frames as possible
	while _buf.size() >= TOTAL_HEADER:
		# Validate magic
		if (char(_buf[0]) + char(_buf[1]) + char(_buf[2]) + char(_buf[3])) != MAGIC:
			push_warning("[BOEClient] Bad magic — resyncing")
			_buf = _buf.slice(1)
			continue

		var frame_type  : int = _buf[4]
		var payload_len : int = _buf.decode_u32(5)   # little-endian
		var tick        : int = _buf.decode_s32(9)   # little-endian signed

		var total_frame := TOTAL_HEADER + payload_len
		if _buf.size() < total_frame:
			break  # wait for more data

		var payload := _buf.slice(TOTAL_HEADER, total_frame)
		_buf = _buf.slice(total_frame)

		match frame_type:
			FRAME_BLOCK_BATCH:
				_parse_block_batch(tick, payload)
			FRAME_ENTITY_BATCH:
				_parse_entity_batch(tick, payload)
			FRAME_JSON_DELTA:
				_parse_json_delta(tick, payload)
			_:
				push_warning("[BOEClient] Unknown frame type: 0x%02X" % frame_type)


func _parse_block_batch(tick: int, payload: PackedByteArray) -> void:
	var blocks := []
	var i := 0
	while i + BLOCK_RECORD_SIZE <= payload.size():
		var offset : int            = payload.decode_u64(i)
		var data   : PackedByteArray = payload.slice(i + 8, i + BLOCK_RECORD_SIZE)
		blocks.append({ "offset": offset, "data": data })
		i += BLOCK_RECORD_SIZE
	emit_signal("block_delta_received", tick, blocks)


func _parse_entity_batch(tick: int, payload: PackedByteArray) -> void:
	var text     := payload.get_string_from_utf8()
	var json_obj := JSON.new()
	var err      := json_obj.parse(text)
	if err != OK:
		push_error("[BOEClient] entity JSON parse error: " + json_obj.get_error_message())
		return
	emit_signal("entity_delta_received", tick, json_obj.data)


func _parse_json_delta(tick: int, payload: PackedByteArray) -> void:
	var text     := payload.get_string_from_utf8()
	var json_obj := JSON.new()
	var err      := json_obj.parse(text)
	if err != OK:
		push_error("[BOEClient] JSON delta parse error: " + json_obj.get_error_message())
		return
	emit_signal("json_delta_received", tick, json_obj.data)
