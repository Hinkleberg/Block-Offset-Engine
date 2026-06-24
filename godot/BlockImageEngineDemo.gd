extends Node3D
## Attach to a scene root after enabling the addon.
func _ready():
    BlockImageEngine.render_delta_received.connect(_on_render_delta)
    BlockImageEngine.connect_engine("ws://127.0.0.1:8765", 1, 32, global_position)
func _physics_process(_delta):
    var direction = Vector3(Input.get_axis("move_left","move_right"),0,Input.get_axis("move_forward","move_back"))
    BlockImageEngine.send_move_intent(direction * 5.0, Engine.get_physics_frames(), 0)
func _on_render_delta(delta: Dictionary):
    # Convert block/entity deltas into Godot nodes, MultiMesh instances, or GPU buffers.
    pass
