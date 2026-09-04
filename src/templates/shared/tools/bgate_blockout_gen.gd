extends SceneTree
## BUILDERS GATE BLOCKOUT GENERATOR - a measured 3D graybox from a JSON spec.
##
## Run by the `blockout_generate` MCP tool, which writes the spec to
## res://.bgate_blockout_spec.json and invokes this script headless. Everything
## it emits is NODE-SHAPED and NAMED so a designer can edit the result:
##   Blockout/Rooms/<Room>            Node3D at the room's origin (meta: w, d, height, kind)
##   Blockout/Rooms/<Room>/Floor      StaticBody3D + BoxMesh + BoxShape3D (layer 1)
##   Blockout/Rooms/<Room>/Ceiling    optional, same shape
##   Blockout/Rooms/<Room>/Props/<P>  StaticBody3D boxes resting ON the floor (meta: climbable)
##   Blockout/Walls/Wall_NN           one body per wall run; shared walls emitted ONCE
##   Blockout/Doors/<A>__<B>          Marker3D at the door's centre (meta: width, height, rooms)
##   Blockout/Nav                     NavigationRegion3D with the BAKED mesh
##   Blockout/Markers/Spawn           Marker3D
##   Blockout/Goals/<name>            Area3D + shape - the volume traversal_prove drives to
##   Blockout/Sun, Blockout/WorldEnvironment
##
## LESSONS THIS FILE CARRIES (catnip-fiend 2026-08-24, Corniche 2026-09-04):
## walls between two rooms are ONE wall, doors are cut through it and given a
## lintel; every prop rests on its floor by construction (min.y == floor);
## the navmesh is baked HERE and measured per room, so "clear floor after
## furniture" is a number in the report, not a hope; connectivity is a real
## NavigationServer path from the spawn to every room, not a flood fill of
## rectangles. Every sub-resource is per node, never shared, so a script that
## mutates one later cannot resize its neighbours.
##
## Re-running is idempotent: the same spec gives the same file.

const SPEC_PATH := "res://.bgate_blockout_spec.json"
const REPORT_PATH := "res://.bgate_out/blockout_report.json"
const EPS := 0.005

var spec: Dictionary = {}
var rooms: Array = []          # [{name, kind, x, z, w, d, h, y, node}]
var room_index: Dictionary = {}  # name -> idx
var doors: Array = []          # [{a, b, axis, c, lo, hi, width, height, name, center}]
var player_h := 1.8
var player_r := 0.4
var wall_t := 0.2
var default_h := 3.0
var report: Dictionary = {"ok": true, "errors": [], "warnings": [], "rooms": [], "doors": [], "walls": 0}


# The work runs across the first TWO _process frames, not _init/_initialize:
# in those the root Window is not yet inside the tree, and
# NavigationServer3D.parse_source_geometry_data refuses a root that is not
# ("The root node needs to be inside the SceneTree", zero polygons, measured).
# Frame 1 builds and bakes; frame 2 measures, because a map query made in the
# same frame the map was populated fails "before first map synchronization".
var _frame := 0
var _root: Node3D
var _nav: Dictionary = {}


func _process(_delta: float) -> bool:
	_frame += 1
	if _frame == 1:
		if not _load_spec():
			quit(1)
			return true
		var p: Dictionary = spec.get("player", {})
		player_h = float(p.get("height", 1.8))
		player_r = float(p.get("radius", 0.4))
		wall_t = float(spec.get("wall_thickness", 0.2))
		default_h = float(spec.get("wall_height", 3.0))
		if not _read_rooms():
			_finish(1)
			return true
		_read_doors()
		_root = Node3D.new()
		_root.name = str(spec.get("root_name", "Blockout"))
		get_root().add_child(_root)   # in-tree: navmesh parsing needs it
		_build_rooms(_root)
		_build_walls(_root)
		_build_markers(_root)
		_build_environment(_root)
		_build_camera(_root)
		_nav = _bake(_root)
		return false
	if _frame >= 2:
		if not _nav.is_empty():
			# The map is queryable only after the server has synced it once.
			# map_force_update is not always enough on the frame after the
			# bake (measured: the same spec answered 4/4 rooms reachable on
			# one run and 0/4 with zero-point paths on the next), so wait
			# for a non-zero iteration id, bounded.
			NavigationServer3D.map_force_update(_nav.map)
			if NavigationServer3D.map_get_iteration_id(_nav.map) == 0 and _frame < 60:
				return false
			_measure(_nav)
		var out: String = str(spec.get("out_scene", "res://scenes/blockout/blockout.tscn"))
		get_root().remove_child(_root)
		# Assign the mesh OUT of the tree: an in-tree region attaches to the
		# default map, whose cell size may not match.
		if not _nav.is_empty():
			(_nav.region as NavigationRegion3D).navigation_mesh = _nav.mesh
			NavigationServer3D.free_rid(_nav.rid)
			NavigationServer3D.free_rid(_nav.map)
		_save_scene(_root, out)
		_root.free()
		report["scene"] = out
		print("BLOCKOUT GENERATED: %s, %d rooms, %d doors, %d walls" % [
			out, rooms.size(), doors.size(), int(report["walls"])])
		_finish(0)
	return true


func _finish(code: int) -> void:
	report["ok"] = report["errors"].is_empty()
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(REPORT_PATH).get_base_dir())
	var f := FileAccess.open(REPORT_PATH, FileAccess.WRITE)
	if f != null:
		f.store_string(JSON.stringify(report, "  "))
		f.close()
	print("BLOCKOUT REPORT: " + JSON.stringify(report))
	quit(code)


func _fail(msg: String) -> void:
	push_error(msg)
	report["errors"].append(msg)


func _warn(msg: String) -> void:
	push_warning(msg)
	report["warnings"].append(msg)


func _load_spec() -> bool:
	if not FileAccess.file_exists(SPEC_PATH):
		push_error("no spec at %s - blockout_generate writes it" % SPEC_PATH)
		return false
	var parsed = JSON.parse_string(FileAccess.get_file_as_string(SPEC_PATH))
	if not (parsed is Dictionary):
		push_error("spec is not a JSON object")
		return false
	spec = parsed
	return true


# ---------------------------------------------------------------- spec -> rooms

func _read_rooms() -> bool:
	var raw: Array = spec.get("rooms", [])
	if raw.is_empty():
		_fail("spec.rooms is empty - nothing to block out")
		return false
	for i in range(raw.size()):
		var r: Dictionary = raw[i]
		var name := str(r.get("name", "Room_%d" % i)).strip_edges()
		if name == "" or room_index.has(name):
			_fail("room %d: name %s is empty or duplicated" % [i, name])
			return false
		var w := float(r.get("w", 0.0))
		var d := float(r.get("d", 0.0))
		if w <= 0.0 or d <= 0.0:
			_fail("room %s: w and d must be positive metres" % name)
			return false
		var room := {
			"name": name, "kind": str(r.get("kind", "room")),
			"x": float(r.get("x", 0.0)), "z": float(r.get("z", 0.0)),
			"w": w, "d": d, "h": float(r.get("height", default_h)),
			"y": float(r.get("floor_y", 0.0)),
			"ceiling": bool(r.get("ceiling", spec.get("ceiling", false))),
			"props": r.get("props", []),
		}
		room_index[name] = rooms.size()
		rooms.append(room)
	# Overlapping interiors are a spec error, not something to paper over with
	# geometry: two floors in one place z-fight and the navmesh doubles up.
	for i in range(rooms.size()):
		for j in range(i + 1, rooms.size()):
			var a: Dictionary = rooms[i]
			var b: Dictionary = rooms[j]
			var ox: float = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
			var oz: float = min(a.z + a.d, b.z + b.d) - max(a.z, b.z)
			if ox > EPS and oz > EPS and abs(a.y - b.y) < EPS:
				_fail("rooms %s and %s overlap by %.2f x %.2f m - split them or make one a corridor that ENDS at the other's wall" % [a.name, b.name, ox, oz])
				return false
	return true


## The interval where rooms a and b share a wall line, or {} if they do not.
func _shared_edge(a: Dictionary, b: Dictionary) -> Dictionary:
	if abs(a.y - b.y) > EPS:
		return {}
	# a's east against b's west, or the reverse -> a wall along z at x = c
	for pair in [[a.x + a.w, b.x], [b.x + b.w, a.x]]:
		if abs(pair[0] - pair[1]) < EPS:
			var lo: float = max(a.z, b.z)
			var hi: float = min(a.z + a.d, b.z + b.d)
			if hi - lo > EPS:
				return {"axis": "z", "c": pair[0], "lo": lo, "hi": hi}
	for pair in [[a.z + a.d, b.z], [b.z + b.d, a.z]]:
		if abs(pair[0] - pair[1]) < EPS:
			var lo: float = max(a.x, b.x)
			var hi: float = min(a.x + a.w, b.x + b.w)
			if hi - lo > EPS:
				return {"axis": "x", "c": pair[0], "lo": lo, "hi": hi}
	return {}


func _min_door() -> float:
	var cell := float(spec.get("navmesh", {}).get("cell_size", 0.1))
	return 2.0 * player_r + 2.0 * cell + 0.2


func _read_doors() -> void:
	var default_w := float(spec.get("door_width", maxf(1.0, _min_door())))
	var default_h := float(spec.get("door_height", maxf(2.1, player_h + 0.3)))
	var explicit: Array = spec.get("doors", [])
	var seen: Dictionary = {}
	for i in range(explicit.size()):
		var d: Dictionary = explicit[i]
		var a := str(d.get("from", ""))
		var b := str(d.get("to", ""))
		if not room_index.has(a):
			_fail("door %d: unknown room %s" % [i, a])
			continue
		var ra: Dictionary = rooms[room_index[a]]
		var width := float(d.get("width", default_w))
		var height := float(d.get("height", default_h))
		var at := clampf(float(d.get("at", 0.5)), 0.0, 1.0)
		var edge: Dictionary = {}
		if room_index.has(b):
			edge = _shared_edge(ra, rooms[room_index[str(b)]])
			if edge.is_empty():
				_fail("door %s -> %s: those rooms share no wall" % [a, b])
				continue
		else:
			# A door to the outside: `side` n|s|e|w on room a's own wall.
			var side := str(d.get("side", "s"))
			match side:
				"n": edge = {"axis": "x", "c": ra.z, "lo": ra.x, "hi": ra.x + ra.w}
				"s": edge = {"axis": "x", "c": ra.z + ra.d, "lo": ra.x, "hi": ra.x + ra.w}
				"w": edge = {"axis": "z", "c": ra.x, "lo": ra.z, "hi": ra.z + ra.d}
				"e": edge = {"axis": "z", "c": ra.x + ra.w, "lo": ra.z, "hi": ra.z + ra.d}
				_:
					_fail("door %d: `to` is not a room and `side` is not n|s|e|w" % i)
					continue
			b = "outside"
		_add_door(a, b, edge, width, height, at, ra.y, seen)
	if bool(spec.get("auto_doors", false)):
		for i in range(rooms.size()):
			for j in range(i + 1, rooms.size()):
				var edge := _shared_edge(rooms[i], rooms[j])
				if edge.is_empty():
					continue
				var key := "%s|%s" % [rooms[i].name, rooms[j].name]
				if seen.has(key) or seen.has("%s|%s" % [rooms[j].name, rooms[i].name]):
					continue
				if rooms[i].kind == "corridor" and rooms[j].kind == "corridor":
					# Two corridor pieces meeting: the whole shared span opens,
					# no jambs, no lintel.
					_add_door(rooms[i].name, rooms[j].name, edge, edge.hi - edge.lo - wall_t,
						minf(float(rooms[i].h), float(rooms[j].h)), 0.5, rooms[i].y, seen)
					continue
				_add_door(rooms[i].name, rooms[j].name, edge, default_w, default_h, 0.5, rooms[i].y, seen)


func _add_door(a: String, b: String, edge: Dictionary, width: float, height: float,
		at: float, floor_y: float, seen: Dictionary) -> void:
	var span: float = edge.hi - edge.lo
	if width > span - wall_t:
		_warn("door %s -> %s: %.2f m wide on a %.2f m shared wall; clamped" % [a, b, width, span])
		width = max(span - wall_t, 0.5)
	var lo: float = edge.lo + wall_t * 0.5 + (span - wall_t - width) * at
	var hi: float = lo + width
	var center: Vector3
	if edge.axis == "z":
		center = Vector3(edge.c, floor_y + height * 0.5, (lo + hi) * 0.5)
	else:
		center = Vector3((lo + hi) * 0.5, floor_y + height * 0.5, edge.c)
	var door := {"a": a, "b": b, "axis": edge.axis, "c": edge.c, "lo": lo, "hi": hi,
		"width": width, "height": height, "name": "%s__%s" % [a, b], "center": center,
		"floor_y": floor_y}
	doors.append(door)
	seen["%s|%s" % [a, b]] = true


# ---------------------------------------------------------------- geometry

func _material(key: String, fallback: Color) -> Material:
	var mats: Dictionary = spec.get("materials", {})
	if mats.has(key):
		var path := str(mats[key])
		if ResourceLoader.exists(path):
			var m = load(path)
			if m is Material:
				return m
		_warn("materials.%s = %s is not a Material" % [key, path])
	var sm := StandardMaterial3D.new()
	sm.albedo_color = fallback
	sm.roughness = 0.9
	return sm


## A static box: body + mesh + shape, each its own resource. `center` is world.
func _box(node_name: String, center: Vector3, size: Vector3, mat: Material, layer: int = 1) -> StaticBody3D:
	var body := StaticBody3D.new()
	body.name = node_name
	body.collision_layer = 0
	body.set_collision_layer_value(layer, true)
	body.collision_mask = 0
	body.position = center
	var mesh := BoxMesh.new()
	mesh.size = size
	mesh.material = mat
	var mi := MeshInstance3D.new()
	mi.name = "Mesh"
	mi.mesh = mesh
	body.add_child(mi)
	var shape := BoxShape3D.new()
	shape.size = size
	var cs := CollisionShape3D.new()
	cs.name = "Collider"
	cs.shape = shape
	body.add_child(cs)
	return body


func _build_rooms(root: Node3D) -> void:
	var rooms_node := Node3D.new()
	rooms_node.name = "Rooms"
	root.add_child(rooms_node)
	var floor_mat := _material("floor", Color(0.55, 0.55, 0.58))
	var corridor_mat := _material("corridor", Color(0.42, 0.44, 0.50))
	var ceiling_mat := _material("ceiling", Color(0.80, 0.80, 0.82))
	var prop_mat := _material("prop", Color(0.85, 0.55, 0.25))
	var climb_mat := _material("climbable", Color(0.35, 0.70, 0.40))
	for room in rooms:
		var n := Node3D.new()
		n.name = room.name
		n.position = Vector3(room.x, room.y, room.z)
		n.set_meta("w", room.w)
		n.set_meta("d", room.d)
		n.set_meta("height", room.h)
		n.set_meta("kind", room.kind)
		rooms_node.add_child(n)
		room["node"] = n
		var mat := corridor_mat if room.kind == "corridor" else floor_mat
		var floor_box := _box("Floor", Vector3(room.w * 0.5, -wall_t * 0.5, room.d * 0.5),
			Vector3(room.w, wall_t, room.d), mat)
		n.add_child(floor_box)
		if room.ceiling:
			n.add_child(_box("Ceiling", Vector3(room.w * 0.5, room.h + wall_t * 0.5, room.d * 0.5),
				Vector3(room.w, wall_t, room.d), ceiling_mat))
		var props: Array = room.props
		if props.is_empty():
			continue
		var pn := Node3D.new()
		pn.name = "Props"
		n.add_child(pn)
		for i in range(props.size()):
			var p: Dictionary = props[i]
			var pname := str(p.get("name", "Prop_%d" % i))
			var size := Vector3(float(p.get("w", 1.0)), float(p.get("h", 1.0)), float(p.get("d", 1.0)))
			var px := float(p.get("x", 0.0))
			var pz := float(p.get("z", 0.0))
			var climb := bool(p.get("climbable", false))
			if px < -EPS or pz < -EPS or px + size.x > room.w + EPS or pz + size.z > room.d + EPS:
				_warn("prop %s/%s pokes outside its room" % [room.name, pname])
			if size.y > room.h + EPS:
				_warn("prop %s/%s is taller than the room" % [room.name, pname])
			# Resting ON the floor by construction: min.y == room floor.
			var body := _box(pname, Vector3(px + size.x * 0.5, size.y * 0.5, pz + size.z * 0.5),
				size, climb_mat if climb else prop_mat)
			body.set_meta("climbable", climb)
			body.set_meta("top_y", room.y + size.y)
			pn.add_child(body)


## Wall runs: every room edge on its line, unioned so a shared wall is one
## wall, minus every stretch that runs through another room's interior (a
## corridor that ends flush against a room), minus door openings (which get a
## lintel above the door).
func _build_walls(root: Node3D) -> void:
	var walls_node := Node3D.new()
	walls_node.name = "Walls"
	root.add_child(walls_node)
	var wall_mat := _material("wall", Color(0.78, 0.76, 0.72))
	var lines: Dictionary = {}   # "axis|c|y" -> {axis, c, y, h, intervals: [[lo, hi]]}
	for room in rooms:
		var edges := [
			["z", room.x, room.z, room.z + room.d],
			["z", room.x + room.w, room.z, room.z + room.d],
			["x", room.z, room.x, room.x + room.w],
			["x", room.z + room.d, room.x, room.x + room.w],
		]
		for e in edges:
			var key := "%s|%.3f|%.3f" % [e[0], e[1], room.y]
			if not lines.has(key):
				lines[key] = {"axis": e[0], "c": e[1], "y": room.y, "h": room.h, "intervals": []}
			lines[key].h = max(lines[key].h, room.h)
			lines[key].intervals.append([e[2], e[3]])
	var count := 0
	var keys := lines.keys()
	keys.sort()
	for key in keys:
		var line: Dictionary = lines[key]
		var merged := _union(line.intervals)
		# Cut out every room interior the line passes through.
		var cuts: Array = []
		for room in rooms:
			if abs(room.y - line.y) > EPS:
				continue
			if line.axis == "z" and room.x + EPS < line.c and line.c < room.x + room.w - EPS:
				cuts.append([room.z, room.z + room.d])
			elif line.axis == "x" and room.z + EPS < line.c and line.c < room.z + room.d - EPS:
				cuts.append([room.x, room.x + room.w])
		var lintels: Array = []
		for door in doors:
			if door.axis == line.axis and abs(door.c - line.c) < EPS and abs(door.floor_y - line.y) < EPS:
				cuts.append([door.lo, door.hi])
				lintels.append(door)
		var pieces := _subtract(merged, cuts)
		for piece in pieces:
			if piece[1] - piece[0] < EPS:
				continue
			count += 1
			walls_node.add_child(_wall_piece("Wall_%02d" % count, line, piece[0], piece[1], line.y, line.h, wall_mat,
				_is_run_end(merged, piece[0]), _is_run_end(merged, piece[1])))
		for door in lintels:
			if door.height < line.h - EPS:
				count += 1
				var lintel := _wall_piece("Lintel_%s" % door.name, line, door.lo, door.hi,
					line.y + door.height, line.h - door.height, wall_mat, false, false)
				walls_node.add_child(lintel)
	report["walls"] = count


func _is_run_end(merged: Array, v: float) -> bool:
	for iv in merged:
		if abs(iv[0] - v) < EPS or abs(iv[1] - v) < EPS:
			return true
	return false


func _wall_piece(node_name: String, line: Dictionary, lo: float, hi: float, y0: float, h: float, mat: Material,
		close_lo: bool, close_hi: bool) -> StaticBody3D:
	# Extend by half a thickness ONLY at run ends, so corners close and doors
	# keep their width. Measured: extending at door cuts took 0.2 m off every
	# door and a 1.0 m door stopped passing a 0.8 m agent.
	var a: float = lo - (wall_t * 0.5 if close_lo else 0.0)
	var b: float = hi + (wall_t * 0.5 if close_hi else 0.0)
	var mid: float = (a + b) * 0.5
	var length: float = b - a
	if line.axis == "z":
		return _box(node_name, Vector3(line.c, y0 + h * 0.5, mid), Vector3(wall_t, h, length), mat)
	return _box(node_name, Vector3(mid, y0 + h * 0.5, line.c), Vector3(length, h, wall_t), mat)


func _union(intervals: Array) -> Array:
	var sorted := intervals.duplicate()
	sorted.sort_custom(func(p, q): return p[0] < q[0])
	var out: Array = []
	for iv in sorted:
		if out.is_empty() or iv[0] > out[-1][1] + EPS:
			out.append([iv[0], iv[1]])
		else:
			out[-1][1] = max(out[-1][1], iv[1])
	return out


func _subtract(intervals: Array, cuts: Array) -> Array:
	var out := intervals.duplicate(true)
	for cut in cuts:
		var next: Array = []
		for iv in out:
			if cut[1] <= iv[0] + EPS or cut[0] >= iv[1] - EPS:
				next.append(iv)
				continue
			if cut[0] > iv[0] + EPS:
				next.append([iv[0], cut[0]])
			if cut[1] < iv[1] - EPS:
				next.append([cut[1], iv[1]])
		out = next
	return out


# ---------------------------------------------------------------- markers

func _room_point(where: Dictionary, fallback_room: Dictionary) -> Vector3:
	var room: Dictionary = fallback_room
	if where.has("room") and room_index.has(str(where.room)):
		room = rooms[room_index[str(where.room)]]
	var x := float(where.get("x", room.w * 0.5))
	var z := float(where.get("z", room.d * 0.5))
	return Vector3(room.x + x, room.y, room.z + z)


func _build_markers(root: Node3D) -> void:
	var doors_node := Node3D.new()
	doors_node.name = "Doors"
	root.add_child(doors_node)
	for door in doors:
		var m := Marker3D.new()
		m.name = door.name
		m.position = door.center
		m.set_meta("width", door.width)
		m.set_meta("height", door.height)
		m.set_meta("rooms", [door.a, door.b])
		doors_node.add_child(m)
	var markers := Node3D.new()
	markers.name = "Markers"
	root.add_child(markers)
	var spawn := Marker3D.new()
	spawn.name = "Spawn"
	spawn.position = _room_point(spec.get("spawn", {}), rooms[0]) + Vector3(0, 0.05, 0)
	markers.add_child(spawn)
	report["spawn"] = [spawn.position.x, spawn.position.y, spawn.position.z]
	var goals_node := Node3D.new()
	goals_node.name = "Goals"
	root.add_child(goals_node)
	var goals: Array = spec.get("goals", [])
	for i in range(goals.size()):
		var g: Dictionary = goals[i]
		var area := Area3D.new()
		area.name = str(g.get("name", "Goal_%d" % i))
		var radius := float(g.get("radius", 0.6))
		var at := _room_point(g, rooms[-1])
		area.position = at + Vector3(0, float(g.get("y", radius)), 0)
		area.collision_layer = 0
		area.set_collision_layer_value(3, true)
		area.collision_mask = 0
		area.set_collision_mask_value(2, true)
		var shape := SphereShape3D.new()
		shape.radius = radius
		var cs := CollisionShape3D.new()
		cs.name = "Volume"
		cs.shape = shape
		area.add_child(cs)
		goals_node.add_child(area)


func _build_environment(root: Node3D) -> void:
	var env: Dictionary = spec.get("environment", {})
	var sun := DirectionalLight3D.new()
	sun.name = "Sun"
	sun.shadow_enabled = true
	sun.rotation_degrees = Vector3(-float(env.get("sun_elevation_deg", 50.0)), float(env.get("sun_azimuth_deg", 35.0)), 0.0)
	root.add_child(sun)
	var we := WorldEnvironment.new()
	we.name = "WorldEnvironment"
	var environment := Environment.new()
	environment.background_mode = Environment.BG_COLOR
	environment.background_color = Color(0.62, 0.70, 0.80)
	environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	environment.ambient_light_color = Color(0.7, 0.72, 0.78)
	environment.ambient_light_energy = 0.8
	we.environment = environment
	root.add_child(we)


## An overview camera so godot_screenshot / godot_evidence show the layout
## with no player scene in it. `current`, deliberately: this IS the level
## scene, and the gameplay seat's player camera takes over when instanced.
## spec.preview_camera = false leaves it out.
func _build_camera(root: Node3D) -> void:
	if not bool(spec.get("preview_camera", true)):
		return
	var lo := Vector3(INF, INF, INF)
	var hi := Vector3(-INF, -INF, -INF)
	for room in rooms:
		lo = Vector3(minf(lo.x, room.x), minf(lo.y, room.y), minf(lo.z, room.z))
		hi = Vector3(maxf(hi.x, room.x + room.w), maxf(hi.y, room.y + room.h), maxf(hi.z, room.z + room.d))
	var center := (lo + hi) * 0.5
	var span := maxf(hi.x - lo.x, hi.z - lo.z)
	var cam := Camera3D.new()
	cam.name = "PreviewCamera"
	cam.current = true
	cam.fov = 60.0
	# Oblique from the south-west, high enough to see over the walls.
	var dist := span * 1.15 + (hi.y - lo.y)
	cam.far = maxf(200.0, dist * 4.0)
	# Add FIRST: look_at_from_position fails (and returns) on a node outside
	# the tree, which silently left the scene with no camera at all.
	root.add_child(cam)
	cam.look_at_from_position(center + Vector3(-0.45, 1.0, 0.75).normalized() * dist,
		Vector3(center.x, lo.y, center.z), Vector3.UP)


# ---------------------------------------------------------------- navmesh + measurement

func _bake(root: Node3D) -> Dictionary:
	var nav_spec: Dictionary = spec.get("navmesh", {})
	var region := NavigationRegion3D.new()
	region.name = "Nav"
	root.add_child(region)
	var nm := NavigationMesh.new()
	nm.agent_radius = float(nav_spec.get("agent_radius", player_r))
	nm.agent_height = float(nav_spec.get("agent_height", player_h))
	nm.agent_max_climb = float(nav_spec.get("agent_max_climb", 0.3))
	# Godot's default map cell is 0.25; a finer bake needs the project's
	# default map to match or every region errors at load. When the spec asks
	# for one, the project setting is written and the report says so.
	var default_cell := float(ProjectSettings.get_setting("navigation/3d/default_cell_size", 0.25))
	var default_cell_h := float(ProjectSettings.get_setting("navigation/3d/default_cell_height", 0.25))
	nm.cell_size = float(nav_spec.get("cell_size", 0.1))
	nm.cell_height = float(nav_spec.get("cell_height", 0.1))
	if abs(nm.cell_size - default_cell) > 1e-6 or abs(nm.cell_height - default_cell_h) > 1e-6:
		ProjectSettings.set_setting("navigation/3d/default_cell_size", nm.cell_size)
		ProjectSettings.set_setting("navigation/3d/default_cell_height", nm.cell_height)
		var perr := ProjectSettings.save()
		if perr == OK:
			report["project_settings_written"] = {"navigation/3d/default_cell_size": nm.cell_size,
				"navigation/3d/default_cell_height": nm.cell_height}
		else:
			_fail("navmesh cell_size %.3f differs from the project default %.3f and project.godot could not be written (%d)" % [nm.cell_size, default_cell, perr])
	nm.geometry_parsed_geometry_type = NavigationMesh.PARSED_GEOMETRY_STATIC_COLLIDERS
	nm.geometry_collision_mask = 1
	nm.geometry_source_geometry_mode = NavigationMesh.SOURCE_GEOMETRY_ROOT_NODE_CHILDREN
	var source := NavigationMeshSourceGeometryData3D.new()
	NavigationServer3D.parse_source_geometry_data(nm, source, root)
	NavigationServer3D.bake_from_source_geometry_data(nm, source)
	if nm.get_polygon_count() == 0:
		_fail("navmesh baked to zero polygons - floors missing or agent_radius %.2f wider than every room" % nm.agent_radius)
		return {}
	var map := NavigationServer3D.map_create()
	NavigationServer3D.map_set_cell_size(map, nm.cell_size)
	NavigationServer3D.map_set_cell_height(map, nm.cell_height)
	NavigationServer3D.map_set_active(map, true)
	var rid := NavigationServer3D.region_create()
	NavigationServer3D.region_set_map(rid, map)
	NavigationServer3D.region_set_navigation_mesh(rid, nm)
	NavigationServer3D.map_force_update(map)
	return {"region": region, "mesh": nm, "map": map, "rid": rid}


func _measure(nav: Dictionary) -> void:
	var nm: NavigationMesh = nav.mesh
	var map: RID = nav.map
	var polys := nm.get_polygon_count()
	# Per-room walkable area: sum of polygon areas whose centroid is inside the
	# room footprint. This is the "clear floor after furniture" number.
	var verts := nm.get_vertices()
	var nav_area := 0.0
	var per_room: Array = []
	for room in rooms:
		per_room.append(0.0)
	for i in range(polys):
		var poly := nm.get_polygon(i)
		if poly.size() < 3:
			continue
		var area := 0.0
		var centroid := Vector3.ZERO
		for k in range(1, poly.size() - 1):
			var a: Vector3 = verts[poly[0]]
			var b: Vector3 = verts[poly[k]]
			var c: Vector3 = verts[poly[k + 1]]
			area += (b - a).cross(c - a).length() * 0.5
		for idx in poly:
			centroid += verts[idx]
		centroid /= poly.size()
		nav_area += area
		for r in range(rooms.size()):
			var room: Dictionary = rooms[r]
			if centroid.x >= room.x - EPS and centroid.x <= room.x + room.w + EPS \
					and centroid.z >= room.z - EPS and centroid.z <= room.z + room.d + EPS \
					and abs(centroid.y - room.y) < 0.5:
				per_room[r] += area
				break
	report["navmesh"] = {"polygons": polys, "area_m2": nav_area,
		"agent_radius": nm.agent_radius, "agent_height": nm.agent_height,
		"cell_size": nm.cell_size}
	# What a corridor or door must measure for the voxel bake to leave a
	# walkable strip: the agent, one erosion cell each side, and the wall's
	# intrusion. Narrower than this is a real geometry fact, not a bake quirk.
	var min_clear: float = 2.0 * player_r + 2.0 * nm.cell_size + wall_t + 0.2
	report["navmesh"]["min_corridor_m"] = min_clear
	report["navmesh"]["min_door_m"] = min_clear - wall_t
	var spawn_arr: Array = report["spawn"]
	var spawn := Vector3(spawn_arr[0], spawn_arr[1], spawn_arr[2])
	var spawn_on := NavigationServer3D.map_get_closest_point(map, spawn)
	if spawn_on.distance_to(spawn) > 0.5:
		_fail("spawn at %s is %.2f m from the nearest walkable point" % [spawn, spawn_on.distance_to(spawn)])
	for r in range(rooms.size()):
		var room: Dictionary = rooms[r]
		var floor_area: float = room.w * room.d
		var coverage: float = per_room[r] / floor_area if floor_area > 0.0 else 0.0
		var center := Vector3(room.x + room.w * 0.5, room.y, room.z + room.d * 0.5)
		var target := NavigationServer3D.map_get_closest_point(map, center)
		var path := NavigationServer3D.map_get_path(map, spawn_on, target, true)
		var reached: bool = path.size() > 0 and path[path.size() - 1].distance_to(target) < 0.25 \
			and target.distance_to(center) < maxf(float(room.w), float(room.d))
		var path_len := 0.0
		for k in range(1, path.size()):
			path_len += path[k].distance_to(path[k - 1])
		var row := {"name": room.name, "kind": room.kind, "w": room.w, "d": room.d, "height": room.h,
			"floor_m2": floor_area, "walkable_m2": per_room[r], "coverage": coverage,
			"reachable_from_spawn": reached, "path_m": path_len if reached else -1.0,
			"path_points": path.size(),
			"path_end_gap": path[path.size() - 1].distance_to(target) if path.size() > 0 else -1.0,
			"target_offset": target.distance_to(center)}
		if room.h < player_h + 0.1:
			_fail("room %s: height %.2f m under the %.2f m player" % [room.name, room.h, player_h])
		if room.kind == "corridor" and minf(float(room.w), float(room.d)) < min_clear - 1e-4:
			_fail("corridor %s: %.2f m wide; the %.2f m agent needs %.2f (2r + 2 cells + wall + 0.2)" % [room.name, minf(float(room.w), float(room.d)), 2.0 * player_r, min_clear])
		if per_room[r] <= 0.0:
			_fail("room %s: no walkable floor after props at agent radius %.2f" % [room.name, nm.agent_radius])
		elif coverage < 0.35 and room.kind != "corridor" and floor_area > 6.0:
			_warn("room %s: only %.0f%% of the floor is walkable after props" % [room.name, coverage * 100.0])
		if not reached:
			_fail("room %s is not reachable from the spawn on the navmesh - a door is missing or too narrow" % room.name)
		report["rooms"].append(row)
	for door in doors:
		var row := {"name": door.name, "rooms": [door.a, door.b], "width": door.width, "height": door.height}
		if door.width < min_clear - wall_t - 1e-4:
			_fail("door %s: %.2f m wide; the %.2f m agent needs %.2f (2r + 2 cells + 0.2)" % [door.name, door.width, 2.0 * player_r, min_clear - wall_t])
		if door.height < player_h + 0.05:
			_fail("door %s: %.2f m high under the %.2f m player" % [door.name, door.height, player_h])
		report["doors"].append(row)


# ---------------------------------------------------------------- save

func _set_owners(node: Node, root: Node) -> void:
	for child in node.get_children():
		child.owner = root
		if child.scene_file_path != "":
			continue
		_set_owners(child, root)


func _save_scene(root: Node, path: String) -> void:
	_set_owners(root, root)
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(path).get_base_dir())
	var packed := PackedScene.new()
	var err := packed.pack(root)
	if err != OK:
		_fail("pack failed: %d" % err)
		return
	var save_err := ResourceSaver.save(packed, path)
	if save_err != OK:
		_fail("save failed: %d" % save_err)
