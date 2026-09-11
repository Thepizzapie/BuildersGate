extends RefCounted
## THE VIADUCT CLOSURE (sector 7): solved, not authored.
##
## Sectors 1-6 are dead-reckoned from turn/length/elevation segments, so they
## do not come back around on their own - sector 6 ends 710 m from the start
## line heading -150 deg, and something has to close the loop exactly (last
## waypoint == first waypoint) whatever those sectors are re-tuned to.
##
## IT USED TO BE A SINGLE CUBIC BEZIER, AND A CUBIC CANNOT DO THIS. Asked to
## reverse ~150 deg over that gap it bends the whole reversal into one spot.
## Measured by running build_track.gd's own walk and its own _bezier() at each
## symmetric handle length:
##
##     handle      Bezier's own min radius     connector length
##     dist/3                 3.13 m                  827 m
##     0.40*dist              4.75 m                  869 m
##     0.50*dist              7.76 m                  939 m
##     0.70*dist             14.14 m                 1097 m
##     0.80*dist             18.56 m                 1183 m   (half the lap)
##
## The road is 12 m wide, so under 6 m the ribbon folds through itself. That is
## what item 47 arrived as: a car stopping dead at offset 1600.8 every run and
## the racing line sitting 2.03 m above its own collider. Deleting the lateral
## handle term (the fix as filed) removed the vertical fold but took the BUILT
## curve's minimum radius only from 5.2 m to 6.04 m - and only because 20 m
## waypoint resampling was SMOOTHING the cusp. Sampling it finer converges on
## the Bezier's real 3.13 m, i.e. makes it worse.
##
## So the closure is a Dubins arc-line-arc instead: turn at a fixed radius, run
## straight, turn onto the line. The minimum radius is the radius you ask for,
## by construction, however the designed sectors move. On the current spec at
## R=40: a 177 deg entry arc (123.6 m), a 682.3 m straight, a 33 deg exit arc
## (23.0 m) - 828.9 m, lap 2389 m, still bible 6's ~2.4 km.
##
## Used by scripts/tools/build_track.gd. Preloaded BY PATH, never by
## class_name: a cross-script class_name lookup hangs a headless run.


## +90 deg (s > 0) or -90 deg (s < 0) about Y, in the x/z plane, in the same
## convention the generator uses everywhere: heading th has direction
## (cos th, sin th), and the vectors here are (x, z) pairs.
static func _rot90(a: Vector2, s: float) -> Vector2:
	if s > 0.0:
		return Vector2(-a.y, a.x)
	return Vector2(a.y, -a.x)


## One Dubins CSC (turn, straight, turn) from (p0, th0) to the origin heading
## +X. s1/s2 are +1 for a left turn, -1 for a right one. Same-side pairs run
## the external tangent between the two turn circles; opposite-side pairs the
## internal one, which needs the centres at least 2R apart - hence ok=false
## rather than a NaN path. Returns lengths and angles, not points.
static func solve_csc(p0: Vector2, th0: float, radius: float, s1: float, s2: float) -> Dictionary:
	var fail := {"ok": false}
	var c1: Vector2 = p0 + Vector2(-sin(th0), cos(th0)) * (s1 * radius)
	var c2: Vector2 = Vector2(0.0, 1.0) * (s2 * radius)
	var v: Vector2 = c2 - c1
	var d: float = v.length()
	var same: bool = absf(s1 - s2) < 0.5
	var u1 := Vector2.ZERO
	var straight := 0.0
	if same:
		if d < 0.001:
			return fail
		u1 = _rot90(v / d, -s1)
		straight = d
	else:
		if d < 2.0 * radius:
			return fail
		var ang: float = acos(clampf(2.0 * radius / d, -1.0, 1.0))
		var base: float = atan2(v.y, v.x)
		var found := false
		for sign_any in [1.0, -1.0]:
			var sgn: float = sign_any
			var a: float = base + sgn * ang
			var cand := Vector2(cos(a), sin(a))
			var seg: Vector2 = v - cand * (2.0 * radius)
			if seg.length() > 0.001 and _rot90(cand, s1).dot(seg.normalized()) > 0.999:
				u1 = cand
				straight = seg.length()
				found = true
				break
		if not found:
			return fail
	var q1: Vector2 = c1 + u1 * radius
	var u2: Vector2 = u1
	if not same:
		u2 = -u1
	var q2: Vector2 = c2 + u2 * radius
	var a1: float = fposmod(((q1 - c1).angle() - (p0 - c1).angle()) * s1, TAU)
	var a2: float = fposmod(((Vector2.ZERO - c2).angle() - (q2 - c2).angle()) * s2, TAU)
	return {
		"ok": true,
		"turn1_deg": rad_to_deg(a1 * s1),
		"arc1": a1 * radius,
		"straight": straight,
		"turn2_deg": rad_to_deg(a2 * s2),
		"arc2": a2 * radius,
		"total": a1 * radius + straight + a2 * radius,
	}


## The solved closure as a fine polyline, stepped with the SAME midpoint rule
## _walk_arc() uses, so what is sampled here is exactly what the walk would
## have produced - not an idealised arc the generator cannot reproduce.
## Elevation is spread over the three legs in proportion to their length, and
## the last point is snapped onto the start line (the analytic solve lands
## within 0.01 m of it) so the lap closes exactly.
static func polyline(p_end: Vector3, h_end: float, sol: Dictionary, step: float = 2.0) -> Array:
	var drop: float = -p_end.y
	var total: float = sol["total"]
	var pts: Array = [p_end]
	var pos: Vector3 = p_end
	var head: float = h_end
	var legs := [
		[float(sol["turn1_deg"]), float(sol["arc1"])],
		[0.0, float(sol["straight"])],
		[float(sol["turn2_deg"]), float(sol["arc2"])],
	]
	for leg in legs:
		var seg_len: float = leg[1]
		if seg_len < 0.001:
			continue
		var steps: int = max(int(ceil(seg_len / step)), 1)
		var dtheta: float = deg_to_rad(leg[0]) / float(steps)
		var step_len: float = seg_len / float(steps)
		var elev_step: float = drop * (seg_len / total) / float(steps)
		for i in range(steps):
			head += dtheta * 0.5
			pos += Vector3(cos(head), 0.0, sin(head)) * step_len
			head += dtheta * 0.5
			pos.y += elev_step
			pts.append(pos)
	pts[pts.size() - 1] = Vector3.ZERO
	return pts


## Does this closure run over the track it is closing around? Segments within
## `skip` of either join are ignored: there the connector is legitimately
## alongside the road it has just left or is about to rejoin, and counting that
## as a crossing would reject every solution.
static func crosses(poly: Array, sector_points: Array, skip: float = 150.0) -> bool:
	if sector_points.is_empty():
		return false
	var join := Vector2(poly[0].x, poly[0].z)
	for i in range(1, poly.size()):
		var a1 := Vector2(poly[i - 1].x, poly[i - 1].z)
		var a2 := Vector2(poly[i].x, poly[i].z)
		if a1.length() < skip or a2.length() < skip:
			continue
		if a1.distance_to(join) < skip or a2.distance_to(join) < skip:
			continue
		for j in range(1, sector_points.size()):
			var b1 := Vector2(sector_points[j - 1].x, sector_points[j - 1].z)
			var b2 := Vector2(sector_points[j].x, sector_points[j].z)
			if b1.length() < skip or b2.length() < skip:
				continue
			if b1.distance_to(join) < skip or b2.distance_to(join) < skip:
				continue
			if Geometry2D.segment_intersects_segment(a1, a2, b1, b2) != null:
				return true
	return false


## Menger radius of the triple around index i - the circle through three
## consecutive points. Straight runs return a huge number, not INF, so callers
## can compare it without special-casing.
static func local_radius(a: Vector3, b: Vector3, c: Vector3) -> float:
	var ab := (b - a).length()
	var bc := (c - b).length()
	var ca := (a - c).length()
	var area2: float = (b - a).cross(c - a).length()
	if area2 < 0.0001 or ab < 0.0001 or bc < 0.0001:
		return 100000.0
	return (ab * bc * ca) / (2.0 * area2)


## Solve the closure and hand back the waypoints to emit, evenly spaced and
## ending exactly on the start line.
##
## All four turn-side combinations are solved at `radius`; the shortest one
## that does not cross sectors 1-6 wins, so re-tuning a sector re-picks the
## shape instead of silently producing a cusp.
##
## SPEED FOLLOWS THE SHAPE, NOT THE OFFSET. The old connector lerped 46 -> 22
## m/s down its whole length, which suited a curve that only got tighter; this
## one is a near-hairpin, then 682 m of straight, then a gentle bend, so a
## single lerp would have the AI crawling the straight and arriving at the arcs
## far too fast. Corner speed is sqrt(lat_accel * radius) - lat_accel is
## back-derived from the designed hairpin (13 m/s at ~20 m) - and then a
## backwards pass caps each waypoint at what `brake` can shed before the next.
##
## Returns {ok, points, speeds, sol, checkpoint_index} - `points` ends on
## Vector3.ZERO, and `checkpoint_index` is where Checkpoint_07 goes (~90%).
static func build(p_end: Vector3, h_end: float, sector_points: Array, opts: Dictionary = {}) -> Dictionary:
	var radius: float = opts.get("radius", 40.0)
	var spacing: float = opts.get("spacing", 14.0)
	var lat_accel: float = opts.get("lat_accel", 8.5)
	var brake: float = opts.get("brake", 12.0)
	var top_speed: float = opts.get("top_speed", 55.0)
	var min_speed: float = opts.get("min_speed", 14.0)
	var line_speed: float = opts.get("line_speed", 58.0)
	var after_line: Vector3 = opts.get("after_line", Vector3(40.0, 0.0, 0.0))

	var best: Dictionary = {}
	var best_poly: Array = []
	for s1_any in [-1.0, 1.0]:
		var s1: float = s1_any
		for s2_any in [-1.0, 1.0]:
			var s2: float = s2_any
			var sol: Dictionary = solve_csc(Vector2(p_end.x, p_end.z), h_end, radius, s1, s2)
			if not sol["ok"]:
				continue
			var poly: Array = polyline(p_end, h_end, sol)
			if crosses(poly, sector_points):
				continue
			if best.is_empty() or float(sol["total"]) < float(best["total"]):
				best = sol
				best_poly = poly
	if best.is_empty():
		return {"ok": false}

	# resample the fine polyline by arc length: uniform spacing, and the last
	# waypoint lands ON the line rather than a stub away from it (a short final
	# chord is what collapses a baked forward vector and makes
	# sample_baked_with_rotation() error with "the target vector can't be zero")
	var cum := PackedFloat32Array()
	cum.append(0.0)
	var total := 0.0
	for i in range(1, best_poly.size()):
		total += (best_poly[i] as Vector3).distance_to(best_poly[i - 1])
		cum.append(total)
	var steps: int = max(int(round(total / spacing)), 4)
	var pts: Array = []
	var j := 0
	for s in range(1, steps + 1):
		var want: float = total * float(s) / float(steps)
		while j < cum.size() - 2 and cum[j + 1] < want:
			j += 1
		var seg: float = cum[j + 1] - cum[j]
		var f := 0.0
		if seg > 0.0001:
			f = clampf((want - cum[j]) / seg, 0.0, 1.0)
		pts.append((best_poly[j] as Vector3).lerp(best_poly[j + 1], f))
	pts[pts.size() - 1] = Vector3.ZERO

	var speeds := PackedFloat32Array()
	for i in range(pts.size()):
		var a: Vector3 = p_end if i == 0 else pts[i - 1]
		var c: Vector3 = after_line if i == pts.size() - 1 else pts[i + 1]
		var r: float = local_radius(a, pts[i], c)
		speeds.append(clampf(sqrt(lat_accel * r), min_speed, top_speed))
	speeds[speeds.size() - 1] = line_speed
	for i in range(pts.size() - 2, -1, -1):
		var d: float = (pts[i + 1] as Vector3).distance_to(pts[i])
		speeds[i] = minf(speeds[i], sqrt(speeds[i + 1] * speeds[i + 1] + 2.0 * brake * d))

	return {
		"ok": true,
		"points": pts,
		"speeds": speeds,
		"sol": best,
		"checkpoint_index": clampi(int(round(float(pts.size()) * 0.9)) - 1, 0, pts.size() - 1),
	}


## THE GATE. Walks the BUILT curve - not the spec that produced it - and
## reports the tightest centreline radius per sector. This is what would have
## caught item 47 before it shipped: every check the generator had passed
## (waypoint count, lap length, project import) is green on a track with a 5 m
## cusp in it, because none of them looked at curvature.
##
## `sectors` is one sector name per curve point. Returns
## {order, worst} where worst[sector] = [radius, offset].
static func curvature_report(curve: Curve3D, sectors: PackedStringArray) -> Dictionary:
	var worst: Dictionary = {}
	var order: Array = []
	var pts: Array = []
	var offs: Array = []
	var owners: Array = []
	var cum := 0.0
	var subdiv := 20
	for i in range(curve.point_count - 1):
		var owner: String = sectors[i] if i < sectors.size() else "?"
		for k in range(subdiv):
			var p: Vector3 = curve.sample(i, float(k) / float(subdiv))
			if not pts.is_empty():
				cum += (p - pts[pts.size() - 1]).length()
			pts.append(p)
			offs.append(cum)
			owners.append(owner)
	for i in range(1, pts.size() - 1):
		var r: float = local_radius(pts[i - 1], pts[i], pts[i + 1])
		var owner: String = owners[i]
		if not worst.has(owner):
			worst[owner] = [r, offs[i]]
			order.append(owner)
		elif r < float(worst[owner][0]):
			worst[owner] = [r, offs[i]]
	return {"order": order, "worst": worst}
