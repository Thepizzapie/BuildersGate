extends Node
## Builders Gate agent - lets a decision model PLAY the game.
##
## Autoloaded as BGateAgent. OFF unless BGATE_AGENT_URL is set (the loopback
## relay `playtest_agent_run` starts), so a normal launch costs nothing.
##
## Every BGATE_AGENT_EVERY physics frames (default 6 = 10 decisions a second)
## it reads the game, sends {state, options, goal} to the relay, and holds the
## chosen option's input actions until the next decision. The relay holds the
## API key and calls the model; the game never sees a key.
##
## LOCKSTEP (default on, BGATE_AGENT_LOCKSTEP=0 to turn off): the tree pauses
## while a decision is in flight, so a 250 ms model call costs wall time, not
## game time, and a run-and-gun stays playable. Off, the game runs on and the
## previous choice stays held, which is what a human with a reaction time does.
##
## WHAT IT SEES. A node in group "bgate_observer" with `bgate_observe() ->
## Dictionary` owns the answer:
##     {"state": String or Dictionary,      # what the player can see
##      "options": {name: "description"}    # press the action called `name`
##              or {name: {"press": ["move_right", "shoot"], "desc": "..."}},
##      "goal": "beat the boss"}            # optional, overrides the run's goal
## Without one it builds a generic view: the scene, the node in group "player",
## the nearest nodes in groups enemy/boss/hazard/parryable/pickup/
## bgate_observable relative to it, visible label text, and one option per
## non-ui InputMap action plus "idle" and one per visible button.
##
## Every decision is emitted to BGateTelemetry as `agent_decision`.

const GROUPS := ["enemy", "boss", "hazard", "boss_hazard", "parryable",
		"pickup", "bgate_observable"]
const PROPS := ["hp", "health", "lives", "hearts", "max_hp", "phase", "velocity"]
const MAX_SEEN := 12
const MAX_LABELS := 6

var _url := ""
var _every := 6
var _lockstep := true
var _tick := 0
var _inflight := false
var _we_paused := false
var _req: HTTPRequest
var _options := {}
var _held: Array = []
var _pending: Array = []
var _sent_msec := 0


func _ready() -> void:
	_url = OS.get_environment("BGATE_AGENT_URL")
	if _url == "":
		set_physics_process(false)
		return
	process_mode = Node.PROCESS_MODE_ALWAYS
	var every := OS.get_environment("BGATE_AGENT_EVERY")
	if every.is_valid_int() and int(every) > 0:
		_every = int(every)
	_lockstep = OS.get_environment("BGATE_AGENT_LOCKSTEP") != "0"
	_req = HTTPRequest.new()
	_req.timeout = 20.0
	_req.process_mode = Node.PROCESS_MODE_ALWAYS
	add_child(_req)
	_req.request_completed.connect(_on_done)


func _physics_process(_delta: float) -> void:
	if not _pending.is_empty():
		for a in _pending:
			_send(a, true)
		_held = _pending
		_pending = []
	if _inflight:
		return
	if get_tree().paused:
		return  # the game's own pause (a menu): not ours to play through
	_tick += 1
	if _tick % _every != 0:
		return
	var obs := _observe()
	var state = obs.get("state", "")
	if typeof(state) != TYPE_STRING:
		state = JSON.stringify(state)
	_options = _normalise(obs.get("options", {}))
	if _options.is_empty():
		_options = _default_options()
	var described := {}
	for k in _options:
		described[k] = _options[k].get("desc", "")
	var body := {"state": state, "options": described, "frame": _tick,
			"hold_s": float(_every) / float(Engine.physics_ticks_per_second)}
	if obs.has("goal"):
		body["goal"] = obs["goal"]
	_inflight = true
	_sent_msec = Time.get_ticks_msec()
	var err := _req.request(_url, ["Content-Type: application/json"],
			HTTPClient.METHOD_POST, JSON.stringify(body))
	if err != OK:
		_inflight = false
		return
	if _lockstep:
		get_tree().paused = true
		_we_paused = true


func _on_done(_result: int, code: int, _headers: PackedStringArray,
		raw: PackedByteArray) -> void:
	_inflight = false
	if _we_paused:
		get_tree().paused = false
		_we_paused = false
	if code != 200:
		return
	var got = JSON.parse_string(raw.get_string_from_utf8())
	if typeof(got) != TYPE_DICTIONARY:
		return
	var choice: String = str(got.get("choice", ""))
	for a in _held:
		_send(a, false)
	_held = []
	var opt: Dictionary = _options.get(choice, {})
	if opt.has("button"):
		var b = opt["button"]
		if is_instance_valid(b) and b.is_visible_in_tree() and not b.disabled:
			b.emit_signal("pressed")
	# Pressed on the NEXT physics frame, so a repeated choice still makes an
	# edge: just_pressed fires again for jump and parry, and a held move loses
	# one frame in six.
	_pending = opt.get("press", [])
	var tel := get_node_or_null("/root/BGateTelemetry")
	if tel != null and tel.has_method("emit_event"):
		tel.emit_event("agent_decision", {"choice": choice,
				"confidence": got.get("confidence", 0.0),
				"ms": Time.get_ticks_msec() - _sent_msec, "frame": _tick})


## Both halves of a press. action_press feeds Input.is_action_pressed polling;
## the InputEventAction reaches _input/_unhandled_input, which is how menus and
## doors listen (measured: a desk that enters on event.is_action_pressed
## ignored 673 polled presses).
func _send(action: String, down: bool) -> void:
	if not InputMap.has_action(action):
		return
	if down:
		Input.action_press(action)
	else:
		Input.action_release(action)
	var ev := InputEventAction.new()
	ev.action = action
	ev.pressed = down
	ev.strength = 1.0 if down else 0.0
	Input.parse_input_event(ev)


func _normalise(raw) -> Dictionary:
	var out := {}
	if typeof(raw) != TYPE_DICTIONARY:
		return out
	for k in raw:
		var v = raw[k]
		if typeof(v) == TYPE_DICTIONARY:
			out[str(k)] = {"press": v.get("press", [str(k)]), "desc": str(v.get("desc", ""))}
		else:
			out[str(k)] = {"press": [str(k)], "desc": str(v)}
	return out


func _observe() -> Dictionary:
	for n in get_tree().get_nodes_in_group("bgate_observer"):
		if n.has_method("bgate_observe"):
			var d = n.bgate_observe()
			# {} means "not mine to describe" (a menu): fall through.
			if typeof(d) == TYPE_DICTIONARY and d.has("state"):
				return d
	return {"state": _generic_state()}


func _generic_state() -> Dictionary:
	var scene := get_tree().current_scene
	var out := {"scene": scene.name if scene else ""}
	var player = get_tree().get_first_node_in_group("player")
	var origin := Vector2.ZERO
	var origin3 := Vector3.ZERO
	if player != null:
		out["player"] = _describe(player, null)
		if player is Node2D:
			origin = player.global_position
		elif player is Node3D:
			origin3 = player.global_position
	var seen := []
	for g in GROUPS:
		for n in get_tree().get_nodes_in_group(g):
			if n == player or not (n is Node2D or n is Node3D):
				continue
			if n is CanvasItem and not n.is_visible_in_tree():
				continue
			var d := _describe(n, player)
			d["group"] = g
			seen.append(d)
	seen.sort_custom(func(a, b): return a.get("dist", 0.0) < b.get("dist", 0.0))
	out["nearby"] = seen.slice(0, MAX_SEEN)
	var labels := []
	if scene != null:
		for l in scene.find_children("*", "Label", true, false):
			if l.is_visible_in_tree() and l.text.strip_edges() != "":
				labels.append(l.text.strip_edges().left(60))
				if labels.size() >= MAX_LABELS:
					break
	if not labels.is_empty():
		out["text_on_screen"] = labels
	return out


func _describe(n: Node, rel_to) -> Dictionary:
	var d := {"name": str(n.name)}
	if n is Node2D:
		var p: Vector2 = n.global_position
		if rel_to is Node2D:
			var off: Vector2 = p - rel_to.global_position
			d["dx"] = roundi(off.x)
			d["dy"] = roundi(off.y)
			d["dist"] = roundi(off.length())
		else:
			d["x"] = roundi(p.x)
			d["y"] = roundi(p.y)
		if n is CharacterBody2D:
			d["on_floor"] = n.is_on_floor()
	elif n is Node3D:
		var p3: Vector3 = n.global_position
		if rel_to is Node3D:
			var off3: Vector3 = p3 - rel_to.global_position
			d["dx"] = snappedf(off3.x, 0.1)
			d["dy"] = snappedf(off3.y, 0.1)
			d["dz"] = snappedf(off3.z, 0.1)
			d["dist"] = snappedf(off3.length(), 0.1)
		else:
			d["pos"] = [snappedf(p3.x, 0.1), snappedf(p3.y, 0.1), snappedf(p3.z, 0.1)]
		if n is CharacterBody3D:
			d["on_floor"] = n.is_on_floor()
	for prop in PROPS:
		if prop in n:
			var v = n.get(prop)
			if v is Vector2:
				d[prop] = [roundi(v.x), roundi(v.y)]
			elif v is Vector3:
				d[prop] = [snappedf(v.x, 0.1), snappedf(v.y, 0.1), snappedf(v.z, 0.1)]
			elif typeof(v) in [TYPE_INT, TYPE_FLOAT, TYPE_BOOL, TYPE_STRING]:
				d[prop] = v
	return d


func _default_options() -> Dictionary:
	var out := {"idle": {"press": [], "desc": "press nothing"}}
	for a in InputMap.get_actions():
		var s := str(a)
		if s.begins_with("ui_") or s.begins_with("bgate_"):
			continue
		out[s] = {"press": [s], "desc": "hold " + s.replace("_", " ")}
	var scene := get_tree().current_scene
	if scene != null:
		for b in scene.find_children("*", "BaseButton", true, false):
			if b.is_visible_in_tree() and not b.disabled:
				var label := str(b.get("text")) if "text" in b else str(b.name)
				if label.strip_edges() == "":
					label = str(b.name)
				out["button:" + label.left(40)] = {"press": [], "button": b,
						"desc": "click the '" + label.left(40) + "' button"}
	return out
