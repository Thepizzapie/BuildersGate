extends Node
## Builders Gate live tuning — F1 over the running game.
##
## Autoloaded as BGateTuner. Press F1 and every `@export` tunable the current
## scene exposes appears with a control bound to the live node: a slider for
## numbers, a checkbox for bools, a text field for the rest. Moving a slider
## moves the running game — there is no apply button, because the whole point is
## to feel the change while you make it.
##
## PERSISTENCE. Values are written to `.bgate/tunables.json` (found by walking up
## from the project directory, the same way the CLI finds its root) and applied
## again at boot. A tuning session therefore survives a restart: you close the
## game, relaunch, and the feel you dialled in is still there. That is the line
## between a toy and something a gameplay engineer will actually use. The file is
## the same one `bgate_core.iterations` already reads as `overrides`, so a tuned
## build is visible to the iteration snapshot rather than being invisible drift.
##
## The on-disk shape:
##
##     {"schema": 1, "updated": <unix>, "values": {
##         "/root/Main/Player": {"gravity": 1400.0, "fall_multiplier": 2.1}}}
##
## Keys are absolute node paths; the value dict is property -> JSON value.
## Vectors and colors are stored as float arrays. Anything the file claims that
## the scene no longer has — a renamed node, a deleted export, a type that
## changed — is skipped, so a stale file can never break a boot.
##
## WHERE IT IS ALIVE. Debug builds, always. A release export is inert: no input
## hook, no file access, no overlay — a shipped game must not carry a developer
## panel. Two escape hatches: the `bgate_tuning` custom feature tag turns it on
## in a non-debug export, and a Web build hosted BY the dashboard turns itself on
## after confirming the same-origin app answers /api/playtest/status (that is the
## /play iframe, which is a dev preview, not a shipped game).

const SCHEMA_VERSION := 1
const TUNABLES_DIR := ".bgate"
const TUNABLES_FILE := "tunables.json"
## What proves a .bgate directory is a project root rather than ~/.bgate, which
## holds the global project registry.
const ROOT_MARKER := "game.db"
## No Builders Gate root above us (a bare Godot project, or a Web build with no
## filesystem at all). user:// still persists, so tuning survives a restart —
## what it must NOT do is create a stray .bgate that hijacks the CLI's root walk.
const FALLBACK_PATH := "user://bgate_tunables.json"
const FEATURE_TAG := "bgate_tuning"
const SAVE_DEBOUNCE := 0.4
const MAX_WALK_UP := 8
const OVERLAY_LAYER := 128
const PANEL_WIDTH := 380.0

const TUNABLE_TYPES: Array[int] = [
	TYPE_BOOL, TYPE_INT, TYPE_FLOAT, TYPE_STRING, TYPE_STRING_NAME,
	TYPE_VECTOR2, TYPE_VECTOR3, TYPE_COLOR,
]

var _armed := false
var _save_path := ""
## node path -> {property: json value}. What is on disk, and what will be.
var _values: Dictionary = {}
## node path -> {property: value the CODE declares}. Captured before overrides
## are applied, so "Reset all" restores the script's defaults and not the
## defaults of whatever the last session happened to leave behind.
var _defaults: Dictionary = {}

var _scene: Node = null
var _layer: CanvasLayer = null
var _list: VBoxContainer = null
var _footer: Label = null
var _open := false
var _dirty := false
var _save_wait := 0.0
var _probe: HTTPRequest = null


func _ready() -> void:
	# The overlay has to keep working if the game pauses itself.
	process_mode = Node.PROCESS_MODE_ALWAYS
	set_process(false)
	set_process_input(false)

	if OS.is_debug_build() or OS.has_feature(FEATURE_TAG):
		_arm()
		return
	if OS.has_feature("web"):
		_probe_dashboard()
	# Otherwise this is a shipped build: stay completely inert. No input hook, no
	# file access, no nodes — nothing here can crash a game a player bought.


func _arm() -> void:
	if _armed:
		return
	_armed = true
	_save_path = _resolve_save_path()
	_values = _load_values()
	set_process_input(true)
	set_process(true)
	_sync_scene.call_deferred()


## --- build gating (web) ------------------------------------------------------

func _probe_dashboard() -> void:
	## A Web export is exported RELEASE, so is_debug_build() is false even for the
	## dashboard's own /play preview. Ask the origin whether it is the dashboard
	## before arming — the same self-discovery bgate_telemetry.gd uses.
	var origin: Variant = JavaScriptBridge.eval("window.location.origin", true)
	var url := str(origin) if origin != null else ""
	if url.is_empty():
		return
	_probe = HTTPRequest.new()
	_probe.name = "BGateTunerProbe"
	add_child(_probe)
	_probe.request_completed.connect(_on_probe_done)
	if _probe.request(url + "/api/playtest/status") != OK:
		_probe.queue_free()
		_probe = null


func _on_probe_done(_result: int, code: int, _headers: PackedStringArray,
		body: PackedByteArray) -> void:
	if _probe != null:
		_probe.queue_free()
		_probe = null
	if code != 200:
		return
	if typeof(JSON.parse_string(body.get_string_from_utf8())) != TYPE_DICTIONARY:
		return
	_arm()


## --- discovery ---------------------------------------------------------------

func _sync_scene() -> void:
	var tree := get_tree()
	var scene: Node = tree.current_scene if tree != null else null
	if scene == _scene:
		return
	_scene = scene
	_defaults.clear()
	if _layer != null and is_instance_valid(_layer):
		_layer.visible = false
	_open = false
	if _scene == null:
		return

	for entry in _discover(_scene):
		var key: String = entry["path"]
		if not _defaults.has(key):
			_defaults[key] = {}
		_defaults[key][entry["name"]] = entry["node"].get(entry["name"])
	_apply_stored()


func _discover(root: Node) -> Array:
	var found: Array = []
	_walk(root, found)
	return found


func _walk(node: Node, found: Array) -> void:
	if node.get_script() != null:
		for prop in node.get_property_list():
			if not _is_tunable(prop):
				continue
			found.append({
				"node": node,
				"path": String(node.get_path()),
				"name": String(prop["name"]),
				"prop": prop,
			})
	for child in node.get_children():
		_walk(child, found)


func _is_tunable(prop: Dictionary) -> bool:
	## An `@export var` carries SCRIPT_VARIABLE together with the editor/storage
	## bits. A plain `var` carries SCRIPT_VARIABLE alone, which is exactly the
	## thing we must not show: it is not part of the script's declared surface.
	var usage := int(prop.get("usage", 0))
	if (usage & PROPERTY_USAGE_SCRIPT_VARIABLE) == 0:
		return false
	if (usage & PROPERTY_USAGE_EDITOR) == 0 or (usage & PROPERTY_USAGE_STORAGE) == 0:
		return false
	var name := String(prop.get("name", ""))
	if name.is_empty() or name.begins_with("_"):
		return false
	return int(prop.get("type", TYPE_NIL)) in TUNABLE_TYPES


## --- persistence -------------------------------------------------------------

func _resolve_save_path() -> String:
	var explicit := OS.get_environment("BGATE_TUNABLES")
	if explicit != "":
		return explicit
	if OS.has_feature("web"):
		return FALLBACK_PATH
	var probe := ProjectSettings.globalize_path("res://").replace("\\", "/")
	probe = probe.trim_suffix("/")
	for _step in MAX_WALK_UP:
		if probe.is_empty():
			break
		var candidate := probe.path_join(TUNABLES_DIR)
		# The DB is what makes a .bgate a project root. Without that check the
		# walk sails past the game and lands in ~/.bgate, which is the global
		# project REGISTRY — tuning one game would write into all of them.
		if FileAccess.file_exists(candidate.path_join(ROOT_MARKER)):
			return candidate.path_join(TUNABLES_FILE)
		var up := probe.get_base_dir()
		if up == probe or up.is_empty():
			break
		probe = up
	return FALLBACK_PATH


func _load_values() -> Dictionary:
	if _save_path.is_empty() or not FileAccess.file_exists(_save_path):
		return {}
	var text := FileAccess.get_file_as_string(_save_path)
	if text.strip_edges().is_empty():
		return {}
	# JSON.new().parse() rather than parse_string(): the latter dumps an engine
	# ERROR on malformed input, and a file the user is free to hand-edit is not
	# an engine fault. One warning is the right amount of noise.
	var reader := JSON.new()
	if reader.parse(text) != OK or typeof(reader.data) != TYPE_DICTIONARY:
		# Corrupt or hand-mangled. Losing the overrides is recoverable; refusing
		# to boot is not.
		push_warning("BGateTuner: ignoring unreadable %s (%s)"
			% [_save_path, reader.get_error_message()])
		return {}
	var values: Variant = (reader.data as Dictionary).get("values")
	if typeof(values) != TYPE_DICTIONARY:
		return {}
	var out: Dictionary = {}
	for key in (values as Dictionary):
		var bucket: Variant = (values as Dictionary)[key]
		if typeof(bucket) == TYPE_DICTIONARY:
			out[String(key)] = (bucket as Dictionary).duplicate()
	return out


func _apply_stored() -> void:
	if _scene == null or _values.is_empty():
		return
	var applied: Dictionary = {}
	for entry in _discover(_scene):
		var path: String = entry["path"]
		var bucket: Variant = _values.get(path)
		if typeof(bucket) != TYPE_DICTIONARY:
			continue
		var name: String = entry["name"]
		if not (bucket as Dictionary).has(name):
			continue
		var coerced: Variant = _coerce((bucket as Dictionary)[name],
			int(entry["prop"]["type"]))
		if coerced == null:
			continue
		entry["node"].set(name, coerced)
		if not applied.has(path):
			applied[path] = {}
		applied[path][name] = _jsonable(coerced)
	if not applied.is_empty():
		# The recorder should know a session ran on a TUNED build — otherwise the
		# numbers in a playtest disagree with the numbers in the source and
		# nobody can tell why.
		_emit("tunables_applied", {"values": applied, "source": _save_path})


func _save() -> void:
	if _save_path.is_empty():
		return
	if not (_save_path.begins_with("user://") or _save_path.begins_with("res://")):
		var dir := _save_path.get_base_dir()
		if not dir.is_empty() and not DirAccess.dir_exists_absolute(dir):
			DirAccess.make_dir_recursive_absolute(dir)
	var file := FileAccess.open(_save_path, FileAccess.WRITE)
	if file == null:
		push_warning("BGateTuner: cannot write %s (%d)"
			% [_save_path, FileAccess.get_open_error()])
		return
	file.store_string(JSON.stringify({
		"schema": SCHEMA_VERSION,
		"updated": Time.get_unix_time_from_system(),
		"values": _values,
	}, "\t"))
	file.close()


func _record(path: String, name: String, value: Variant) -> void:
	var encoded: Variant = _jsonable(value)
	if encoded == null:
		return
	if typeof(_values.get(path)) != TYPE_DICTIONARY:
		_values[path] = {}
	(_values[path] as Dictionary)[name] = encoded
	# Debounced: a slider drag is hundreds of changes and none of them deserve
	# their own disk write.
	_dirty = true
	_save_wait = SAVE_DEBOUNCE


func _emit(kind: String, data: Dictionary) -> void:
	var telemetry := get_node_or_null(^"/root/BGateTelemetry")
	if telemetry != null and telemetry.has_method("emit_event"):
		telemetry.emit_event(kind, data)


## --- value marshalling -------------------------------------------------------

func _coerce(value: Variant, type: int) -> Variant:
	## JSON has no int/float distinction and no vectors, so every stored value is
	## re-typed against what the script actually declares. A mismatch returns
	## null and the override is dropped rather than assigned as garbage.
	match type:
		TYPE_BOOL:
			if typeof(value) in [TYPE_BOOL, TYPE_INT, TYPE_FLOAT]:
				return bool(value)
		TYPE_INT:
			if typeof(value) in [TYPE_BOOL, TYPE_INT, TYPE_FLOAT]:
				return int(value)
		TYPE_FLOAT:
			if typeof(value) in [TYPE_BOOL, TYPE_INT, TYPE_FLOAT]:
				return float(value)
		TYPE_STRING:
			if typeof(value) == TYPE_STRING:
				return String(value)
		TYPE_STRING_NAME:
			if typeof(value) == TYPE_STRING:
				return StringName(value)
		TYPE_VECTOR2:
			var v2 := _floats(value, 2)
			if not v2.is_empty():
				return Vector2(v2[0], v2[1])
		TYPE_VECTOR3:
			var v3 := _floats(value, 3)
			if not v3.is_empty():
				return Vector3(v3[0], v3[1], v3[2])
		TYPE_COLOR:
			var c := _floats(value, 4)
			if not c.is_empty():
				return Color(c[0], c[1], c[2], c[3])
	return null


func _floats(value: Variant, want: int) -> Array:
	if typeof(value) != TYPE_ARRAY:
		return []
	var arr: Array = value
	if arr.size() != want:
		return []
	var out: Array = []
	for item in arr:
		if not (typeof(item) in [TYPE_INT, TYPE_FLOAT]):
			return []
		out.append(float(item))
	return out


func _jsonable(value: Variant) -> Variant:
	match typeof(value):
		TYPE_BOOL, TYPE_INT, TYPE_FLOAT, TYPE_STRING:
			return value
		TYPE_STRING_NAME:
			return String(value)
		TYPE_VECTOR2:
			return [(value as Vector2).x, (value as Vector2).y]
		TYPE_VECTOR3:
			return [(value as Vector3).x, (value as Vector3).y, (value as Vector3).z]
		TYPE_COLOR:
			var col := value as Color
			return [col.r, col.g, col.b, col.a]
	return null


## --- input + frame -----------------------------------------------------------

func _input(event: InputEvent) -> void:
	if not _armed:
		return
	if not (event is InputEventKey):
		return
	var key := event as InputEventKey
	# No input-map action: F1 must work in a project that never declared one.
	if key.pressed and not key.echo and key.keycode == KEY_F1:
		toggle()
		get_viewport().set_input_as_handled()


func _process(delta: float) -> void:
	var tree := get_tree()
	if tree != null and tree.current_scene != _scene:
		# change_scene_to_* swapped the world out. Rediscover, and re-apply the
		# overrides to the nodes that just appeared.
		_sync_scene()
	if _dirty:
		_save_wait -= delta
		if _save_wait <= 0.0:
			_dirty = false
			_save()


func _notification(what: int) -> void:
	if what == NOTIFICATION_WM_CLOSE_REQUEST or what == NOTIFICATION_PREDELETE:
		if _armed and _dirty:
			_dirty = false
			_save()


## --- overlay -----------------------------------------------------------------

## Toggle the panel. Public so a game can bind its own key as well as F1.
func toggle() -> void:
	if not _armed:
		return
	if _open:
		_open = false
		if _layer != null and is_instance_valid(_layer):
			_layer.visible = false
		return
	if _scene == null:
		_sync_scene()
	_ensure_layer()
	_populate()
	_layer.visible = true
	_open = true


func is_open() -> bool:
	return _open


## Restore every discovered tunable to the value its script declares, and drop
## the saved overrides. The undo for a tuning session that went nowhere.
func reset_all() -> void:
	for path in _defaults:
		var node := get_node_or_null(NodePath(path))
		if node == null:
			continue
		for name in (_defaults[path] as Dictionary):
			node.set(name, (_defaults[path] as Dictionary)[name])
	_values.clear()
	_dirty = false
	_save()
	if _open:
		_populate()


func _ensure_layer() -> void:
	if _layer != null and is_instance_valid(_layer):
		return
	_layer = CanvasLayer.new()
	_layer.name = "BGateTunerLayer"
	_layer.layer = OVERLAY_LAYER
	_layer.visible = false
	add_child(_layer)

	var frame := Control.new()
	frame.set_anchors_preset(Control.PRESET_FULL_RECT)
	frame.mouse_filter = Control.MOUSE_FILTER_IGNORE
	frame.process_mode = Node.PROCESS_MODE_ALWAYS
	_layer.add_child(frame)

	var panel := PanelContainer.new()
	panel.anchor_left = 0.0
	panel.anchor_top = 0.0
	panel.anchor_right = 0.0
	panel.anchor_bottom = 1.0
	panel.offset_left = 12.0
	panel.offset_top = 12.0
	panel.offset_right = 12.0 + PANEL_WIDTH
	panel.offset_bottom = -12.0
	panel.add_theme_stylebox_override("panel", _panel_style())
	frame.add_child(panel)

	var column := VBoxContainer.new()
	column.add_theme_constant_override("separation", 8)
	panel.add_child(column)

	var head := HBoxContainer.new()
	column.add_child(head)
	var title := Label.new()
	title.text = "Live tuning"
	title.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	head.add_child(title)
	var reset := Button.new()
	reset.text = "Reset all"
	reset.tooltip_text = "Restore the values the scripts declare"
	reset.pressed.connect(reset_all)
	head.add_child(reset)

	var scroll := ScrollContainer.new()
	scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	column.add_child(scroll)

	_list = VBoxContainer.new()
	_list.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_list.add_theme_constant_override("separation", 4)
	scroll.add_child(_list)

	_footer = Label.new()
	_footer.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	_footer.add_theme_font_size_override("font_size", 11)
	_footer.add_theme_color_override("font_color", Color(0.55, 0.6, 0.68))
	column.add_child(_footer)


func _panel_style() -> StyleBoxFlat:
	var style := StyleBoxFlat.new()
	style.bg_color = Color(0.04, 0.05, 0.07, 0.93)
	style.border_color = Color(0.16, 0.2, 0.25)
	style.set_border_width_all(1)
	style.set_corner_radius_all(8)
	style.set_content_margin_all(10)
	return style


func _populate() -> void:
	if _list == null:
		return
	for child in _list.get_children():
		_list.remove_child(child)
		child.queue_free()

	var entries: Array = _discover(_scene) if _scene != null else []
	_footer.text = "F1 closes · saved to %s" % _save_path
	if entries.is_empty():
		var empty := Label.new()
		empty.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
		empty.text = "No @export tunables in this scene. Export a variable and it "
		empty.text += "shows up here."
		_list.add_child(empty)
		return

	var group := ""
	for entry in entries:
		var path: String = entry["path"]
		if path != group:
			group = path
			_list.add_child(_group_header(path))
		var row := _row_for(entry)
		if row != null:
			_list.add_child(row)


func _group_header(path: String) -> Control:
	var label := Label.new()
	label.text = path
	label.add_theme_font_size_override("font_size", 11)
	label.add_theme_color_override("font_color", Color(0.42, 0.62, 0.72))
	label.clip_text = true
	return label


func _row_for(entry: Dictionary) -> Control:
	var type := int(entry["prop"]["type"])
	match type:
		TYPE_BOOL:
			return _bool_row(entry)
		TYPE_INT, TYPE_FLOAT:
			return _numeric_row(entry, type == TYPE_INT)
	return _text_row(entry, type)


func _row_shell(name: String) -> HBoxContainer:
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 6)
	var label := Label.new()
	label.text = name
	label.custom_minimum_size = Vector2(130.0, 0.0)
	label.add_theme_font_size_override("font_size", 12)
	label.clip_text = true
	label.tooltip_text = name
	row.add_child(label)
	return row


func _numeric_row(entry: Dictionary, is_int: bool) -> Control:
	var node: Node = entry["node"]
	var name: String = entry["name"]
	var path: String = entry["path"]
	var value := float(node.get(name))
	var bounds := _bounds(entry["prop"], value, is_int)

	var row := _row_shell(name)
	var slider := HSlider.new()
	slider.min_value = bounds["min"]
	slider.max_value = bounds["max"]
	slider.step = bounds["step"]
	slider.value = value
	slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	slider.size_flags_vertical = Control.SIZE_SHRINK_CENTER
	slider.custom_minimum_size = Vector2(120.0, 0.0)
	row.add_child(slider)

	var readout := Label.new()
	readout.text = _fmt(value, is_int)
	readout.custom_minimum_size = Vector2(62.0, 0.0)
	readout.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	readout.add_theme_font_size_override("font_size", 12)
	row.add_child(readout)

	slider.value_changed.connect(func(raw: float) -> void:
		if not is_instance_valid(node):
			return
		var applied: Variant = int(round(raw)) if is_int else raw
		node.set(name, applied)
		readout.text = _fmt(float(applied), is_int)
		_record(path, name, applied)
	)
	return row


func _bool_row(entry: Dictionary) -> Control:
	var node: Node = entry["node"]
	var name: String = entry["name"]
	var path: String = entry["path"]
	var row := _row_shell(name)
	var check := CheckBox.new()
	check.button_pressed = bool(node.get(name))
	check.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(check)
	check.toggled.connect(func(on: bool) -> void:
		if not is_instance_valid(node):
			return
		node.set(name, on)
		_record(path, name, on)
	)
	return row


func _text_row(entry: Dictionary, type: int) -> Control:
	var node: Node = entry["node"]
	var name: String = entry["name"]
	var path: String = entry["path"]
	var row := _row_shell(name)
	var edit := LineEdit.new()
	edit.text = _text_of(node.get(name), type)
	edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	edit.add_theme_font_size_override("font_size", 12)
	edit.tooltip_text = _hint_for(type)
	row.add_child(edit)

	var commit := func() -> void:
		if not is_instance_valid(node):
			return
		var parsed: Variant = _parse_text(edit.text, type)
		if parsed == null:
			# Unparseable — snap back to what the game actually holds rather than
			# leaving a lie in the box.
			edit.text = _text_of(node.get(name), type)
			return
		node.set(name, parsed)
		_record(path, name, parsed)
	edit.text_submitted.connect(func(_t: String) -> void: commit.call())
	edit.focus_exited.connect(func() -> void: commit.call())
	return row


func _bounds(prop: Dictionary, value: float, is_int: bool) -> Dictionary:
	## @export_range is the author telling us the real limits — use them. With no
	## hint, bracket the current value: 0 to twice it (sign-aware), which puts the
	## shipped value in the middle of a range that can reach zero and double.
	var low := 0.0
	var high := 0.0
	var step := 1.0 if is_int else 0.0
	var hint := int(prop.get("hint", PROPERTY_HINT_NONE))
	var parts := String(prop.get("hint_string", "")).split(",", false)
	if hint == PROPERTY_HINT_RANGE and parts.size() >= 2 \
			and parts[0].is_valid_float() and parts[1].is_valid_float():
		low = float(parts[0])
		high = float(parts[1])
		if parts.size() >= 3 and parts[2].is_valid_float():
			step = float(parts[2])
	elif is_zero_approx(value):
		low = -10.0 if is_int else -1.0
		high = 10.0 if is_int else 1.0
	else:
		low = minf(0.0, value * 2.0)
		high = maxf(0.0, value * 2.0)

	# A value outside its own slider is a control that cannot represent the game.
	low = minf(low, value)
	high = maxf(high, value)
	if high <= low:
		high = low + (1.0 if is_int else 0.001)
	if step <= 0.0:
		step = 1.0 if is_int else maxf((high - low) / 200.0, 0.0001)
	return {"min": low, "max": high, "step": step}


func _fmt(value: float, is_int: bool) -> String:
	if is_int:
		return str(int(round(value)))
	return String.num(value, 4)


func _text_of(value: Variant, type: int) -> String:
	match type:
		TYPE_VECTOR2:
			var v2 := value as Vector2
			return "%s, %s" % [String.num(v2.x, 4), String.num(v2.y, 4)]
		TYPE_VECTOR3:
			var v3 := value as Vector3
			return "%s, %s, %s" % [String.num(v3.x, 4), String.num(v3.y, 4),
				String.num(v3.z, 4)]
		TYPE_COLOR:
			return "#" + (value as Color).to_html(true)
	return String(value)


func _hint_for(type: int) -> String:
	match type:
		TYPE_VECTOR2:
			return "x, y"
		TYPE_VECTOR3:
			return "x, y, z"
		TYPE_COLOR:
			return "#rrggbbaa"
	return "text"


func _parse_text(text: String, type: int) -> Variant:
	match type:
		TYPE_STRING:
			return text
		TYPE_STRING_NAME:
			return StringName(text)
		TYPE_VECTOR2:
			var v2 := _parse_floats(text, 2)
			if not v2.is_empty():
				return Vector2(v2[0], v2[1])
		TYPE_VECTOR3:
			var v3 := _parse_floats(text, 3)
			if not v3.is_empty():
				return Vector3(v3[0], v3[1], v3[2])
		TYPE_COLOR:
			var html := text.strip_edges()
			if Color.html_is_valid(html):
				return Color.html(html)
	return null


func _parse_floats(text: String, want: int) -> Array:
	var parts := text.split(",", false)
	if parts.size() != want:
		return []
	var out: Array = []
	for part in parts:
		var token := part.strip_edges()
		if not token.is_valid_float():
			return []
		out.append(float(token))
	return out
