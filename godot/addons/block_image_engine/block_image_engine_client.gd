extends Node
## Transport-agnostic Godot 4 connector. The engine remains authoritative.
signal connected
signal disconnected(reason: String)
signal render_delta_received(delta: Dictionary)
signal engine_error(message: String)

var _peer: WebSocketPeer
var _url := ""
var _connected := false
var _last_entity_tick := 0
var _last_block_seq := 0

func connect_engine(url: String, client_id: int, view_radius: int, position: Vector3) -> Error:
    _url = url
    _peer = WebSocketPeer.new()
    var err = _peer.connect_to_url(url)
    if err != OK: return err
    _connected = false
    set_meta("join_payload", {"op":"connect","client_id":client_id,"view_radius":view_radius,
        "position":[position.x,position.y,position.z]})
    return OK

func disconnect_engine() -> void:
    if _peer: _peer.close()
    _connected = false

func send_move_intent(desired_velocity: Vector3, input_sequence: int, expected_revision: int) -> void:
    _send({"op":"move_intent","input_sequence":input_sequence,
        "desired_velocity":[desired_velocity.x,desired_velocity.y,desired_velocity.z],
        "expected_revision":expected_revision})

func mutate_world(mutation: Dictionary) -> void:
    ## Studio/plugin mutation entry point; server validates and journals it.
    _send({"op":"world_mutation","mutation":mutation})

func _process(_delta: float) -> void:
    if not _peer: return
    _peer.poll()
    var state := _peer.get_ready_state()
    if state == WebSocketPeer.STATE_OPEN:
        if not _connected:
            _connected = true
            _send(get_meta("join_payload"))
            connected.emit()
        while _peer.get_available_packet_count() > 0:
            var packet = _peer.get_packet().get_string_from_utf8()
            var message = JSON.parse_string(packet)
            if typeof(message) == TYPE_DICTIONARY: _receive(message)
    elif state == WebSocketPeer.STATE_CLOSED and _connected:
        _connected = false
        disconnected.emit(_peer.get_close_reason())

func _send(message: Dictionary) -> void:
    if _peer and _peer.get_ready_state() == WebSocketPeer.STATE_OPEN:
        _peer.send_text(JSON.stringify(message))

func _receive(message: Dictionary) -> void:
    match message.get("op", ""):
        "render_delta":
            _last_entity_tick = max(_last_entity_tick, int(message.get("entity_tick", 0)))
            _last_block_seq = max(_last_block_seq, int(message.get("block_seq", 0)))
            render_delta_received.emit(message)
        "error": engine_error.emit(str(message.get("message", "unknown engine error")))
