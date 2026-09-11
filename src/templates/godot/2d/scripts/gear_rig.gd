extends Node2D
## GearRig — a fighter is a BODY sprite plus N gear layers that animate in
## LOCKSTEP with it. The body AnimatedSprite2D is the single clock; every
## equipped layer mirrors its animation and frame each tick, so a helmet, a
## cuirass, and a sword all advance together with zero per-layer animation code.
##
## This is the 2D escape from the combinatorial trap. The body's animations are
## authored ONCE. A gear variant only has to exist as a layer keyed to the same
## animation names — it is never re-timed or re-rendered per combination.
## equip(slot, frames) swaps a slot's SpriteFrames and it immediately rides the
## current animation; that is the whole cost of "diverse gear."
##
## Two art conventions both work through the same slots:
##   * aligned sheet — a full-character-canvas SpriteFrames with only the gear
##     drawn (the sprite pipeline's native output). offset 0; overlays 1:1.
##   * anchored icon — a small item icon (item_to_spriteframes) placed at a slot
##     anchor so a static weapon sits in the hand. Pass an offset to equip().
##
## Facing flips the whole rig, so every layer's offset mirrors together and a
## right-hand weapon lands in the correct hand when the fighter turns.

## The equip slots, in draw order low → high. Matches bgate_core/items.py slots.
const SLOTS := ["feet", "body", "head", "off_hand", "main_hand"]

## slot → child node name. The body DRIVER is "Base"; the body-ARMOR layer is
## "BodyGear" — kept distinct so the armor slot never shadows the driver.
const NODE_FOR := {
	"feet": "Feet", "body": "BodyGear", "head": "Head",
	"off_hand": "OffHand", "main_hand": "MainHand",
}

## Z ordering over the body (the Base sprite sits at z 0).
const SLOT_Z := {
	"feet": -1, "body": 1, "head": 5, "off_hand": 8, "main_hand": 10,
}

@onready var _body: AnimatedSprite2D = $Base
var _layers := {}            # slot -> AnimatedSprite2D
var _offsets := {}           # slot -> {anim: Array[Vector2|null] per frame}
var _base_offset := {}       # slot -> Vector2 (the static fallback)
var _facing_right := true


func _ready() -> void:
	for slot in SLOTS:
		var node := get_node_or_null(NODE_FOR[slot]) as AnimatedSprite2D
		if node:
			node.z_index = SLOT_Z.get(slot, 0)
			node.visible = false          # empty until something is equipped
			_layers[slot] = node


## Assign the body's own animation set. Everything else follows this clock.
func set_base(frames: SpriteFrames, start_anim := &"idle") -> void:
	_body.sprite_frames = frames
	if frames and frames.has_animation(start_anim):
		_body.play(start_anim)


## Drive the whole rig by name. Layers follow in _process — never call play on
## them directly, or they drift off the body's frame.
func play(anim: StringName) -> void:
	if _body.sprite_frames and _body.sprite_frames.has_animation(anim):
		_body.play(anim)


func set_facing(right: bool) -> void:
	_facing_right = right
	scale.x = 1.0 if right else -1.0


## `offsets` is the parsed <name>_offsets.json the sprite pipeline writes from
## rig-sidecar anchors (Aseprite slices or spriteedit labels): {"cell": [w, h],
## "animations": {anim: [[x, y] | null, ...]}} with [x, y] in CELL pixels, one
## entry per frame in play order. A null frame (no anchor authored there) falls
## back to the static `offset`, so partial coverage degrades to today's
## behaviour instead of to a weapon snapping to the origin.
func equip(slot: String, frames: SpriteFrames, offset := Vector2.ZERO,
		offsets := {}) -> void:
	var layer: AnimatedSprite2D = _layers.get(slot)
	if layer == null:
		push_warning("GearRig: no layer for slot '%s'" % slot)
		return
	layer.sprite_frames = frames
	layer.position = offset
	layer.visible = true
	_base_offset[slot] = offset
	_offsets.erase(slot)
	var anims: Dictionary = offsets.get("animations", {})
	var cell: Array = offsets.get("cell", [])
	if anims.size() > 0 and cell.size() == 2:
		# Anchor coords are cell-local (origin the cell's top-left); layer
		# positions are relative to the rig origin, which sits at the centred
		# body cell's middle — so the conversion is one subtraction, done once.
		var half := Vector2(float(cell[0]) / 2.0, float(cell[1]) / 2.0)
		var table := {}
		for anim in anims:
			var entries: Array = anims[anim]
			var converted := []
			for entry in entries:
				if entry is Array and entry.size() == 2:
					converted.append(Vector2(float(entry[0]), float(entry[1])) - half)
				else:
					converted.append(null)
			table[anim] = converted
		_offsets[slot] = table


func unequip(slot: String) -> void:
	var layer: AnimatedSprite2D = _layers.get(slot)
	if layer:
		layer.visible = false
		layer.sprite_frames = null
	_offsets.erase(slot)
	_base_offset.erase(slot)


func is_equipped(slot: String) -> bool:
	var layer: AnimatedSprite2D = _layers.get(slot)
	return layer != null and layer.visible and layer.sprite_frames != null


func _process(_dt: float) -> void:
	# The body is the clock. Every equipped layer shows the body's current
	# animation and frame — IF it defines that animation. A layer missing the
	# current anim hides its visual for that anim (a helmet slot with no crouch
	# frame) instead of freezing on a stale frame, and reappears the moment an
	# animation it does define plays again.
	if _body.sprite_frames == null:
		return
	var anim := _body.animation
	var frame := _body.frame
	for slot in _layers:
		var layer: AnimatedSprite2D = _layers[slot]
		if not layer.visible or layer.sprite_frames == null:
			continue
		if layer.sprite_frames.has_animation(anim):
			if layer.animation != anim:
				layer.animation = anim
			var count := layer.sprite_frames.get_frame_count(anim)
			layer.frame = min(frame, max(0, count - 1))
			layer.self_modulate.a = 1.0
			# Per-frame anchor, when this slot carries an offsets table for
			# the current animation — the weapon FOLLOWS THE HAND instead of
			# hovering at one average position for the whole swing.
			var table: Dictionary = _offsets.get(slot, {})
			if table.has(anim):
				var entries: Array = table[anim]
				var idx: int = min(layer.frame, entries.size() - 1)
				if idx >= 0 and entries[idx] != null:
					layer.position = entries[idx]
				else:
					layer.position = _base_offset.get(slot, Vector2.ZERO)
		else:
			layer.self_modulate.a = 0.0
