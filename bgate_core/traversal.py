"""Can the player actually GET THERE — proved by driving the player.

FOUR OF SIX CLIMBING ROUTES PASSED AND WERE NOT TRAVERSABLE. The tests measured
vertical rise: the ledge was 1.1 m up, the character's jump clears 1.4 m,
therefore reachable. What none of them measured was the horizontal gap to the
edge, whether the launch surface was big enough to stand on, whether the landing
pad was big enough to land on, how wide the player's own body is, or what the
controller does when you actually hold the stick that way. Geometry answered a
question about geometry. The player's question is about the player.

THEN THE GATE WRITTEN TO CATCH THAT PRODUCED ITS OWN FALSE GREEN, and this is
the sharpest defect the whole benchmark produced, because it happened INSIDE the
fix. The driver accepted arrival on any frame where the body was near the
target — including mid-ballistic-arc during a jump that MISSED. Adding a
grounded check was not enough either: a scripted mantle carries ``is_on_floor``
from before the animation began, so the check passed mid-interpolation, while
the character was being lerped through the air by an animation player.

So arrival is three conditions and all three are required:

    IN THE VOLUME     inside the destination's own Area3D/Area2D, not within
                      N metres of a marker. A radius around a point is a
                      different claim from "the game thinks you are there",
                      and only the second one is what the game acts on.
    SETTLED           grounded AND not inside any scripted or interpolated
                      move — mantle, vault, ledge-grab, carry, cutscene.
    HELD              for :data:`SETTLE_FRAMES` CONSECUTIVE frames. One frame
                      is a sample of a trajectory; N frames is a state.

CONTROLLERS MUST DECLARE WHEN THEY ARE BUSY. There is no way to detect "this
body is being moved by a script rather than by physics" from outside — that is
exactly why the grounded check failed. So the contract is explicit: the player
controller exposes a public query (:data:`BUSY_QUERY`, or any of
:data:`BUSY_ALIASES`) that returns true while a scripted move owns the body. A
controller without one is REFUSED by this harness rather than sampled naively,
because sampling it naively is the bug.

GEOMETRY STAYS, AS EXPLANATION. rise, edge-to-edge gap, launch pad size,
landing pad size and player bounds are all reported — they are what tells a
designer WHY a route failed. They are not the verdict. The verdict is that the
controller went there and stayed.
"""
from __future__ import annotations

import json
import time
from typing import Optional

#: Consecutive frames the settled state must hold. Three is not arbitrary:
#: a mantle's interpolation is many frames long, a landing's grounded-flicker
#: is one, and a ballistic arc passing through the volume is one or two at
#: ordinary speeds. Held for three, at 60 Hz, is 50 ms of actually being there.
SETTLE_FRAMES = 3

#: The public query a controller must expose. Named, not guessed.
BUSY_QUERY = "is_in_scripted_move"

#: Names accepted as that query, in preference order. A project that already
#: had one of these should not have to rename it to be gateable.
BUSY_ALIASES = (BUSY_QUERY, "is_busy", "is_scripted_move_active",
                "is_animation_driving", "is_traversal_busy")

#: Frames a route is driven for before it is abandoned. Bounded on purpose —
#: see the module note in ``enginetests``: an unbounded wait-until-condition
#: loop is indistinguishable from a hang, and three agents were killed after
#: 25 minutes of silence having written nothing.
MAX_FRAMES = 1800

#: How often the driver prints a progress line, in frames. A long run that
#: says nothing is a hang as far as any watcher is concerned.
HEARTBEAT_FRAMES = 120


class ControllerRefused(ValueError):
    """The player controller cannot be driven honestly.

    Raised rather than worked around. A harness that samples a controller it
    cannot ask about scripted movement is a harness that will report a mantle's
    midpoint as an arrival, which is the exact false green this module exists
    to close.
    """


def controller_contract(source: str) -> dict:
    """Does this controller script expose the busy query? {ok, query, why}.

    A source-text check rather than a runtime one because the answer decides
    whether the run happens at all, and spawning an engine to learn that the
    run cannot be trusted is spending to find out you cannot spend.
    """
    text = str(source or "")
    for name in BUSY_ALIASES:
        if f"func {name}(" in text:
            return {"ok": True, "query": name, "why": ""}
    return {
        "ok": False, "query": "",
        "why": ("this controller exposes no public scripted-move query, so a "
                "traversal driver cannot tell 'standing on the ledge' from "
                "'being lerped through the air by a mantle animation'. Add "
                f"`func {BUSY_QUERY}() -> bool:` returning true while any "
                "scripted or interpolated move owns the body (mantle, vault, "
                "ledge-grab, carry, cutscene). Accepted names: "
                + ", ".join(BUSY_ALIASES)),
    }


def require_controller(source: str) -> str:
    """The busy query's name, or raise. The gate's own entry point."""
    got = controller_contract(source)
    if not got["ok"]:
        raise ControllerRefused(got["why"])
    return got["query"]


# ── the route ───────────────────────────────────────────────────────────────

def route(*, name: str, scene: str, launch: str, destination: str,
          inputs: list[dict], player: str = "",
          settle_frames: int = SETTLE_FRAMES,
          max_frames: int = MAX_FRAMES) -> dict:
    """One traversal claim, fully specified. Pure.

    ``launch``       node path of the surface the player STARTS ON. Not a
                     spawn point somewhere convenient — the real launch
                     surface, because half of what a route tests is whether
                     you can stand there to begin with.
    ``destination``  node path of the destination's OWN trigger/arrival
                     volume (Area2D/Area3D). Not a marker with a radius.
    ``inputs``       the actual input program: ``[{"action": "move_forward",
                     "frames": 20}, {"action": "jump", "frames": 1}, ...]``.
                     Real actions through the real input map.
    """
    if not str(destination or "").strip():
        raise ValueError(
            "a route must name the DESTINATION'S OWN arrival volume — an "
            "Area2D/Area3D the game itself uses to know the player is there. "
            "A marker plus a distance threshold is a different claim, and it "
            "is the claim that passed mid-jump on a missed route.")
    if not str(launch or "").strip():
        raise ValueError(
            "a route must name the real launch surface — 'can you even stand "
            "where this jump starts' is half of what it is testing")
    # A STEP MAY HOLD SEVERAL ACTIONS AT ONCE, because a real route does: you
    # do not stop running to jump. An earlier draft pressed exactly one action
    # per step, which cannot express "run and jump" at all — and a harness that
    # cannot express the input the player uses is measuring a different route.
    steps = []
    for step in (inputs or []):
        raw = (step or {}).get("actions")
        if raw is None:
            raw = [(step or {}).get("action")]
        names = [str(a).strip() for a in raw if str(a or "").strip()]
        frames = int((step or {}).get("frames") or 0)
        if not names:
            raise ValueError("every input step needs an action name "
                             "(`action`, or `actions` for a held combination)")
        if frames < 1:
            raise ValueError(f"{names}: frames must be at least 1")
        steps.append({"actions": names, "frames": frames,
                      "action": names[0]})
    if not steps:
        raise ValueError(
            "a route with no inputs proves nothing — drive the controller")
    total = sum(s["frames"] for s in steps)
    ceiling = max(total + 240, min(int(max_frames or MAX_FRAMES), MAX_FRAMES))
    return {
        "name": str(name or "route")[:120],
        "scene": str(scene),
        "launch": str(launch),
        "destination": str(destination),
        "player": str(player or ""),
        "inputs": steps,
        "settle_frames": max(1, int(settle_frames or SETTLE_FRAMES)),
        "max_frames": ceiling,
        "input_frames": total,
    }


# ── grading what came back ──────────────────────────────────────────────────

def grade(route_spec: dict, samples: list[dict]) -> dict:
    """Turn a driver's per-frame samples into a verdict. THE STRICT PART.

    ``samples`` is one dict per frame: ``{frame, inside, grounded, busy,
    position}``. Everything about whether a route passed is decided here rather
    than in GDScript, so the rule has one implementation and a test can feed it
    a synthetic mid-mantle arrival and prove it fails.

    A pass requires ``settle_frames`` CONSECUTIVE samples that are all three of
    inside / grounded / not busy. Anything else is a fail, and the verdict says
    which of the three conditions was the one that never held — because
    "arrived but never settled" and "never arrived" send a designer to
    different files.
    """
    need = max(1, int(route_spec.get("settle_frames") or SETTLE_FRAMES))
    run = 0
    best = 0
    settled_at = -1
    ever_inside = ever_grounded = False
    inside_but_moving = 0
    for sample in samples or []:
        inside = bool(sample.get("inside"))
        grounded = bool(sample.get("grounded"))
        busy = bool(sample.get("busy"))
        ever_inside = ever_inside or inside
        ever_grounded = ever_grounded or grounded
        if inside and (not grounded or busy):
            inside_but_moving += 1
        if inside and grounded and not busy:
            run += 1
            best = max(best, run)
            if run >= need and settled_at < 0:
                settled_at = int(sample.get("frame") or 0)
        else:
            run = 0
    ok = settled_at >= 0
    if ok:
        why = ""
    elif not ever_inside:
        why = ("the player never entered the destination's arrival volume — "
               "the route was not completed at all")
    elif inside_but_moving and best == 0:
        why = (f"the player entered the destination volume on "
               f"{inside_but_moving} frame(s) but was never settled there: "
               "airborne or inside a scripted move every time. THIS IS THE "
               "FALSE GREEN — a ballistic arc through the volume during a "
               "MISSED jump looks exactly like this, and so does the midpoint "
               "of a mantle animation, which carries is_on_floor from before "
               "it began")
    else:
        why = (f"the player settled in the destination for at most {best} "
               f"consecutive frame(s); {need} are required. A single frame is "
               "a sample of a trajectory, not a state")
    return {
        "ok": ok, "why": why,
        "settled_at_frame": settled_at,
        "longest_settled_run": best,
        "required_frames": need,
        "frames_sampled": len(samples or []),
        "ever_inside": ever_inside,
        "ever_grounded": ever_grounded,
        "inside_but_unsettled_frames": inside_but_moving,
    }


def verdict(route_spec: dict, driver_result: dict,
            geometry: Optional[dict] = None) -> dict:
    """The whole answer: the strict grade, plus geometry as EXPLANATION.

    ``geometry`` is never consulted for the verdict. It is attached because a
    failed route needs a reason a designer can act on, and "landing pad is
    0.4 m across against a 0.6 m player" is that reason.
    """
    samples = driver_result.get("samples") or []
    got = grade(route_spec, samples)
    out = {
        "route": route_spec.get("name", ""),
        "scene": route_spec.get("scene", ""),
        "launch": route_spec.get("launch", ""),
        "destination": route_spec.get("destination", ""),
        "driver_ok": bool(driver_result.get("ok")),
        "controller_query": driver_result.get("busy_query", ""),
        **got,
        "metrics": dict(geometry or driver_result.get("metrics") or {}),
        "metrics_note": ("rise, gap, pad sizes and player bounds are "
                         "EXPLANATORY. The verdict above is the controller "
                         "having gone there and stayed; these numbers say why "
                         "it did or did not."),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not driver_result.get("ok"):
        out["ok"] = False
        out["why"] = (str(driver_result.get("error") or "the driver did not "
                          "complete") + (f" — {got['why']}" if got["why"] else ""))
    # A FALL AFTER A PROVEN ARRIVAL IS NOT A FAILED ROUTE. The driver stops as
    # soon as the settled state holds, so `fell` at this point means the body
    # left the world BEFORE getting there — or that a route which did arrive was
    # driven on past its goal by an older driver. Reported either way; only
    # allowed to fail a route the samples did not already prove.
    for key in ("fell", "stuck", "died"):
        if driver_result.get(key):
            out[key] = True
            if not got["ok"]:
                out["ok"] = False
                out["why"] = (out.get("why") or "") + f" (driver reported {key})"
    return out


# ── the driver itself ───────────────────────────────────────────────────────

_DRIVER_GD = '''extends Node
# Builders Gate traversal driver — generated, not authored. Runs as the main
# scene so the project's autoloads resolve exactly as they do in the game.

const SPEC := __SPEC__

var _samples: Array = []
var _frame: int = 0
var _step: int = 0
var _step_frame: int = 0
var _player: Node = null
var _volume: Node = null
var _busy_query: String = ""
var _fell: bool = false
var _start_y: float = 0.0
var _done: bool = false
# THE ROUTE STARTS WHEN THE PLAYER IS STANDING, NOT WHEN THE SCENE LOADS. A
# body dropped in above its launch surface spends the first frames falling, and
# an input program that begins there tests the placement rather than the route:
# the jump is swallowed because is_on_floor() is still false. Settle first.
var _settling: int = 0
# THE DRIVER STOPS THE MOMENT THE ROUTE IS PROVEN. Without this the input
# program runs on past the goal and the character walks off the far side of the
# landing pad, so a route that DID arrive reports "fell". The grading is still
# done in Python over the samples (traversal.grade) — this counter only decides
# when to stop driving, and it uses the same three conditions so it cannot stop
# early on a weaker one.
var _settled_run: int = 0
var _metrics: Dictionary = {}


func _ready() -> void:
	var packed := load(SPEC["scene"])
	if packed == null:
		_finish(false, "could not load scene %s" % SPEC["scene"])
		return
	add_child(packed.instantiate())
	await get_tree().process_frame
	_player = get_node_or_null(SPEC["player"]) if SPEC["player"] != "" else _find_player()
	if _player == null:
		_finish(false, "no player body found (player=%s)" % SPEC["player"])
		return
	_volume = get_node_or_null(SPEC["destination"])
	if _volume == null:
		_finish(false, "destination volume %s does not exist — a route must "
			% SPEC["destination"] + "terminate in the destination's OWN trigger area")
		return
	for name in SPEC["busy_aliases"]:
		if _player.has_method(name):
			_busy_query = name
			break
	if _busy_query == "":
		_finish(false, "controller exposes no scripted-move query; refusing to "
			+ "sample it naively (accepted: %s)" % str(SPEC["busy_aliases"]))
		return
	var launch := get_node_or_null(SPEC["launch"])
	if launch == null:
		_finish(false, "launch surface %s does not exist" % SPEC["launch"])
		return
	_measure(launch)
	_place_on(launch)
	_start_y = _height_of(_player)
	print("BGATE_TRAVERSAL_START %s" % SPEC["name"])


# THE NUMBERS THAT EXPLAIN A FAILURE, measured off the real nodes at the real
# moment — never the verdict. The four-of-six false green happened because
# geometry was ANSWERING; here it only describes. What none of the original
# tests measured: the horizontal edge gap, whether the launch pad is big enough
# to stand on, whether the landing pad is big enough to land on, and how wide
# the player's own body is.
func _measure(launch: Node) -> void:
	_metrics = {
		"launch_pad": _extent(launch),
		"landing_pad": _extent(_volume),
		"player_bounds": _extent(_player),
	}
	var a = _metrics["launch_pad"]
	var b = _metrics["landing_pad"]
	if a.has("centre") and b.has("centre"):
		var ac = a["centre"]
		var bc = b["centre"]
		if ac is Vector3 and bc is Vector3:
			_metrics["rise"] = snappedf(bc.y - ac.y, 0.0001)
			_metrics["centre_gap"] = snappedf(
				Vector2(bc.x - ac.x, bc.z - ac.z).length(), 0.0001)
			_metrics["edge_gap"] = snappedf(maxf(0.0,
				_metrics["centre_gap"] - (a["size"].x + b["size"].x) * 0.5), 0.0001)
		elif ac is Vector2 and bc is Vector2:
			_metrics["rise"] = snappedf(ac.y - bc.y, 0.0001)
			_metrics["centre_gap"] = snappedf(absf(bc.x - ac.x), 0.0001)
			_metrics["edge_gap"] = snappedf(maxf(0.0,
				_metrics["centre_gap"] - (a["size"].x + b["size"].x) * 0.5), 0.0001)
	_metrics["note"] = ("EXPLANATORY ONLY. The verdict is whether the "
		+ "controller went there and stayed; these say why it did or did not.")


func _extent(node: Node) -> Dictionary:
	# The COLLISION shape where there is one — a route is about what the physics
	# server thinks the body and the ledge are, not about what the art looks
	# like. Falls back to the node's own position when nothing is measurable,
	# and says which it used rather than presenting a guess as a measurement.
	if node == null:
		return {}
	for child in _shapes_of(node):
		var shape = child.shape
		if shape == null:
			continue
		var rect = shape.get_rect() if shape.has_method("get_rect") else null
		if rect != null:
			return {"centre": child.global_position, "size": rect.size,
				"source": "CollisionShape2D:%s" % shape.get_class()}
		if shape.has_method("get_debug_mesh"):
			var aabb: AABB = shape.get_debug_mesh().get_aabb()
			return {"centre": child.global_position, "size": aabb.size,
				"source": "CollisionShape3D:%s" % shape.get_class()}
	if node is Node3D:
		return {"centre": (node as Node3D).global_position,
			"size": Vector3.ZERO, "source": "position only (no collision shape)"}
	if node is Node2D:
		return {"centre": (node as Node2D).global_position,
			"size": Vector2.ZERO, "source": "position only (no collision shape)"}
	return {}


func _shapes_of(node: Node) -> Array:
	var out: Array = []
	for child in node.get_children():
		if child is CollisionShape2D or child is CollisionShape3D:
			out.append(child)
	return out


func _find_player() -> Node:
	for group in ["player", "Player"]:
		var found := get_tree().get_first_node_in_group(group)
		if found != null:
			return found
	return null


func _height_of(node: Node) -> float:
	if node is Node3D:
		return (node as Node3D).global_position.y
	if node is Node2D:
		return -(node as Node2D).global_position.y
	return 0.0


func _place_on(surface: Node) -> void:
	if _player is Node3D and surface is Node3D:
		(_player as Node3D).global_position = (surface as Node3D).global_position \\
			+ Vector3(0, 1.0, 0)
	elif _player is Node2D and surface is Node2D:
		(_player as Node2D).global_position = (surface as Node2D).global_position \\
			+ Vector2(0, -16)


func _inside() -> bool:
	if _volume.has_method("get_overlapping_bodies"):
		for body in _volume.get_overlapping_bodies():
			if body == _player or _player.is_ancestor_of(body):
				return true
	return false


func _grounded() -> bool:
	if _player.has_method("is_on_floor"):
		return bool(_player.call("is_on_floor"))
	return false


func _physics_process(_delta: float) -> void:
	if _done or not is_instance_valid(_player) or not is_instance_valid(_volume):
		return
	# LAND ON THE LAUNCH SURFACE FIRST. Nothing about the route is being tested
	# while the body is still falling onto the ledge it starts from, and an
	# input program spent during that fall is an input program the controller
	# ignored. Bounded so a launch surface that cannot be stood on FAILS rather
	# than hanging - "can you even stand where this jump starts" is half of what
	# a route is.
	if _settling < SPEC["settle_grace"]:
		_settling += 1
		if not _grounded():
			if _height_of(_player) < _start_y - SPEC["fall_limit"]:
				_fell = true
				_finish(true, "the player fell off the LAUNCH surface before "
					+ "the route began - %s cannot be stood on" % SPEC["launch"])
			return
		_settling = SPEC["settle_grace"]
		_start_y = _height_of(_player)
	_frame += 1
	if _frame % SPEC["heartbeat"] == 0:
		print("BGATE_TRAVERSAL_TICK frame=%d step=%d/%d" % [
			_frame, _step, SPEC["inputs"].size()])
	_drive()
	var busy := bool(_player.call(_busy_query))
	var inside := _inside()
	var grounded := _grounded()
	_samples.append({
		"frame": _frame,
		"inside": inside,
		"grounded": grounded,
		"busy": busy,
	})
	if inside and grounded and not busy:
		_settled_run += 1
		if _settled_run >= int(SPEC["settle_frames"]):
			_finish(true, "")
			return
	else:
		_settled_run = 0
	if _height_of(_player) < _start_y - SPEC["fall_limit"]:
		_fell = true
		_finish(true, "")
		return
	if _frame >= SPEC["max_frames"]:
		_finish(true, "")


func _drive() -> void:
	_release_all()
	if _step >= SPEC["inputs"].size():
		return
	var step: Dictionary = SPEC["inputs"][_step]
	for action in step["actions"]:
		Input.action_press(action)
	_step_frame += 1
	if _step_frame >= int(step["frames"]):
		_step += 1
		_step_frame = 0


func _release_all() -> void:
	for step in SPEC["inputs"]:
		for action in step["actions"]:
			if Input.is_action_pressed(action):
				Input.action_release(action)


func _finish(ok: bool, error: String) -> void:
	if _done:
		return
	_done = true
	var payload := {
		"ok": ok, "error": error, "fell": _fell,
		"busy_query": _busy_query, "samples": _samples,
		"frames": _frame, "metrics": _metrics,
	}
	var out := SPEC["out"]
	if out != "":
		var handle := FileAccess.open(out, FileAccess.WRITE)
		if handle != null:
			handle.store_string(JSON.stringify(payload))
			handle.close()
	print("BGATE_TRAVERSAL_DONE %s" % JSON.stringify({
		"ok": ok, "error": error, "fell": _fell, "frames": _frame}))
	get_tree().quit()
'''


def _looks_3d(route_spec: dict) -> bool:
    """Is this a 3D route? Decides the fall limit's UNIT.

    Metres and pixels are three orders of magnitude apart, and a 6-unit fall
    limit that is right for a 3D character fires on a 2D character stepping off
    a kerb. Inferred from the node paths rather than asked for, because the
    caller already told us where things are and asking twice is how the two
    answers disagree.
    """
    text = " ".join(str(route_spec.get(k) or "") for k in
                    ("scene", "launch", "destination", "player")).lower()
    return "3d" in text or "/world/" in text


def driver_source(route_spec: dict, out_path: str) -> str:
    """The GDScript that drives one route. Deterministic for a given spec."""
    spec = {
        "name": route_spec["name"],
        "scene": route_spec["scene"],
        "launch": route_spec["launch"],
        "destination": route_spec["destination"],
        "player": route_spec.get("player") or "",
        "inputs": route_spec["inputs"],
        "max_frames": int(route_spec["max_frames"]),
        "settle_frames": int(route_spec["settle_frames"]),
        "heartbeat": HEARTBEAT_FRAMES,
        "fall_limit": 6.0 if _looks_3d(route_spec) else 400.0,
        "settle_grace": 90,
        "busy_aliases": list(BUSY_ALIASES),
        "out": str(out_path or ""),
    }
    return _DRIVER_GD.replace("__SPEC__", json.dumps(spec))
