extends Node2D
## Runtime for a Builders Gate cutout character.
##
## The scene is pre-baked by the emitter — bones, sprites, z order and the
## animation library are all in the .tscn before this script runs. What this
## adds is the part that cannot be baked: swapping a part's texture at runtime
## and keeping it hanging from the right point, flipping the character without
## corrupting anything, and turning animation method keys into a signal.
##
## THE THREE THINGS IN HERE THAT ARE NOT OBVIOUS:
##
##   1. equip() re-derives the offset from the PIVOT TABLE. Baking one offset
##      into the Sprite2D at emit time works exactly until the first swap, at
##      which point the new part hangs from the old part's pivot — the central
##      feature of a cutout rig, silently broken.
##
##   2. FLIP HAPPENS ON `Visual`, NEVER ON THE ROOT. A negative scale on an
##      ancestor of a CollisionShape2D corrupts the collision shape the moment
##      this scene is placed under a CharacterBody2D, and it does it quietly.
##
##   3. SEEKING RE-FIRES METHOD KEYS. AnimationPlayer.seek() with update=true
##      runs every method key it passes, so a rewind replays every hit event in
##      the clip. The guard below is why a parry that rewinds does not deal
##      damage six times.

signal anim_event(name: StringName)

## texture res:// path -> pivot as a fraction of the texture's own size,
## measured from the bottom-left in DOC space (+y up). Written by the emitter.
@export var part_pivots: Dictionary = {}

## Every texture this rig can wear, as real resources. This array exists so the
## EXPORTER can see them: Godot strips resources that are only ever named by a
## string built at runtime, so equipment loaded by path works in the editor and
## fails in the exported game, which is the worst possible place to find out.
@export var equip_textures: Array[Texture2D] = []

@onready var visual: Node2D = $Visual
@onready var player: AnimationPlayer = $AnimationPlayer

var _seeking := false
var _facing := 1


func _ready() -> void:
	if player and not player.animation_finished.is_connected(_on_finished):
		player.animation_finished.connect(_on_finished)


## Play a clip. Returns false if this rig does not have it, rather than
## asserting — a game asking for "sprint" on a rig that only walks should
## degrade, not crash.
func play(clip: StringName, from_start: bool = true) -> bool:
	if player == null or not player.has_animation(clip):
		return false
	if from_start:
		player.stop()
	player.play(clip)
	return true


func facing() -> int:
	return _facing


## +1 faces the way the art was drawn, -1 mirrors it. Applied to `visual`
## only — see the note at the top of this file about physics ancestors.
func set_facing(direction: int) -> void:
	_facing = -1 if direction < 0 else 1
	if visual:
		visual.scale.x = abs(visual.scale.x) * _facing


## Put `texture` in `slot`, hanging from the pivot recorded for it.
##
## The pivot lookup falls back to the OUTGOING part's pivot rather than to a
## centre, because a wrong-but-close offset reads as a slightly misplaced arm
## and a centred one reads as a limb detached from the body.
func equip(slot: StringName, texture: Texture2D) -> bool:
	var sprite := _slot(slot)
	if sprite == null or texture == null:
		return false
	var pivot := _pivot_for(texture, sprite)
	sprite.texture = texture
	sprite.centered = false
	var size := texture.get_size()
	sprite.offset = Vector2(-pivot.x * size.x, -(1.0 - pivot.y) * size.y)
	sprite.visible = true
	return true


func unequip(slot: StringName) -> bool:
	var sprite := _slot(slot)
	if sprite == null:
		return false
	sprite.visible = false
	return true


## Swap several slots at once — a whole skin, or a set of armour.
func set_skin(skin: Dictionary) -> int:
	var done := 0
	for slot in skin.keys():
		if equip(slot, skin[slot]):
			done += 1
	return done


func has_slot(slot: StringName) -> bool:
	return _slot(slot) != null


## Jump to a time without re-firing every method key between here and there.
func seek_quiet(time: float) -> void:
	_seeking = true
	if player:
		player.seek(time, true)
	_seeking = false


func _slot(slot: StringName) -> Sprite2D:
	if visual == null:
		return null
	# find_child rather than a baked path: a slot hangs off whichever bone the
	# document put it on, and that is per template.
	var found := visual.find_child(String(slot), true, false)
	return found as Sprite2D


func _pivot_for(texture: Texture2D, sprite: Sprite2D) -> Vector2:
	var path := texture.resource_path
	if path != "" and part_pivots.has(path):
		return part_pivots[path]
	if sprite.texture != null:
		var old := sprite.texture.get_size()
		if old.x > 0.0 and old.y > 0.0:
			return Vector2(-sprite.offset.x / old.x,
					1.0 + sprite.offset.y / old.y)
	return Vector2(0.5, 0.5)


## Called by method tracks in the emitted animation library.
func _anim_event(name: StringName) -> void:
	if _seeking:
		return
	anim_event.emit(name)


func _on_finished(_name: StringName) -> void:
	pass
