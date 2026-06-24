@tool
extends EditorPlugin
func _enter_tree():
    add_autoload_singleton("BlockImageEngine", "res://addons/block_image_engine/block_image_engine_client.gd")
func _exit_tree():
    remove_autoload_singleton("BlockImageEngine")
