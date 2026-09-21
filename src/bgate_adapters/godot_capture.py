"""Photograph every clip of a character IN THE ENGINE, and composite sheets.

blender_animate's proof sheets are Blender renders of a Blender scene. What
the player sees is Godot's import of that file, under Godot's skeleton (a
retargeted rest is a different rest), Godot's materials and Godot's lights -
and the defects between the two were invisible by construction. This runs
the real renderer, same mechanism as godot.screenshot (an injected autoload,
override.cfg, removed in a finally): it builds a stage around the wired
character scene, plays each clip through its AnimationPlayer with the
AnimationTree switched off, seeks N sample times, and saves a frame per
view. PIL then lays them out one sheet per clip - side row over
three-quarter row - so the picture a human judges is the engine's.

Needs a GPU/display, like screenshot(); a window appears for the run.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from bgate_adapters import godot as _godot

CAPTURE_MARK = "BGATE_CLIPSHOT:"
_CAPTURE_SCRIPT = "bgate_clipshot.gd"

_CAPTURE_GD = _godot._INJECT_BANNER + """
extends Node
## Injected by godot_capture: stage, character, one frame per clip per
## sample per view, then quit. Parameters come from the environment.

var out := {"ok": false, "frames": [], "clips": []}
var stage_root: Node3D = null
var body: Node = null
var player: AnimationPlayer = null
var cam: Camera3D = null
var centre := Vector3.ZERO
var reach := 1.0
var views := {}
var clips: Array = []
var samples := 5
var dir := ""

func _ready() -> void:
	call_deferred("_build")

func _find(node: Node, cls: String) -> Node:
	if node == null:
		return null
	if node.is_class(cls):
		return node
	for c in node.get_children():
		var f := _find(c, cls)
		if f != null:
			return f
	return null

func _say() -> void:
	print("__MARK__" + JSON.stringify(out))
	get_tree().quit()

func _build() -> void:
	var scene_path := OS.get_environment("BGATE_CLIP_SCENE")
	dir = OS.get_environment("BGATE_CLIP_DIR")
	samples = maxi(int(OS.get_environment("BGATE_CLIP_SAMPLES")), 2)
	var wanted := OS.get_environment("BGATE_CLIP_LIST")
	var current := get_tree().current_scene
	if current != null:
		current.queue_free()
	stage_root = Node3D.new()
	stage_root.name = "BGateClipStage"
	get_tree().root.add_child(stage_root)
	var env := WorldEnvironment.new()
	var e := Environment.new()
	e.background_mode = Environment.BG_COLOR
	e.background_color = Color(0.16, 0.17, 0.19)
	e.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	e.ambient_light_color = Color(0.75, 0.77, 0.82)
	e.ambient_light_energy = 0.9
	env.environment = e
	stage_root.add_child(env)
	var sun := DirectionalLight3D.new()
	sun.rotation_degrees = Vector3(-50, 35, 0)
	sun.shadow_enabled = true
	stage_root.add_child(sun)
	var floor_body := StaticBody3D.new()
	var floor_shape := CollisionShape3D.new()
	var box := BoxShape3D.new()
	box.size = Vector3(40, 1, 40)
	floor_shape.shape = box
	floor_shape.position.y = -0.5
	floor_body.add_child(floor_shape)
	var floor_mesh := MeshInstance3D.new()
	var pm := PlaneMesh.new()
	pm.size = Vector2(40, 40)
	floor_mesh.mesh = pm
	var fm := StandardMaterial3D.new()
	fm.albedo_color = Color(0.33, 0.35, 0.33)
	floor_mesh.material_override = fm
	floor_body.add_child(floor_mesh)
	stage_root.add_child(floor_body)
	var packed = load(scene_path)
	if packed == null or not (packed is PackedScene):
		out["error"] = "no PackedScene at %s" % scene_path
		_say(); return
	body = packed.instantiate()
	stage_root.add_child(body)
	# The tree must not fight the player for the bones.
	var tree := _find(body, "AnimationTree")
	if tree != null:
		tree.set("active", false)
		tree.set_physics_process(false)
		tree.set_process(false)
	# A controller applying gravity or input is not what is being photographed.
	if body is CharacterBody3D:
		body.set_physics_process(false)
		body.set_process(false)
		body.set_process_input(false)
		body.set_process_unhandled_input(false)
	player = _find(body, "AnimationPlayer") as AnimationPlayer
	if player == null:
		out["error"] = "no AnimationPlayer under %s" % scene_path
		_say(); return
	var aabb := AABB()
	var first := true
	for mi in _meshes(body):
		var b: AABB = mi.global_transform * mi.get_aabb()
		aabb = b if first else aabb.merge(b)
		first = false
	if first:
		aabb = AABB(Vector3(-0.5, 0, -0.5), Vector3(1, 1.8, 1))
	centre = aabb.get_center()
	reach = maxf(aabb.size.length() * 0.5, 0.3)
	var fwd: Vector3 = -body.global_transform.basis.z if body is Node3D else Vector3.FORWARD
	var left := Vector3.UP.cross(fwd).normalized()
	views = {"side": left, "front34": (fwd + left * 0.8).normalized(), "back34": (-fwd - left * 0.8).normalized()}
	cam = Camera3D.new()
	cam.fov = 40.0
	stage_root.add_child(cam)
	cam.current = true
	var all_clips: Array = Array(player.get_animation_list())
	if wanted != "":
		for w in wanted.split(","):
			if all_clips.has(w):
				clips.append(w)
	else:
		for c in all_clips:
			if not String(c).begins_with("RESET"):
				clips.append(c)
	out["clips"] = clips
	if clips.is_empty():
		out["error"] = "no clip to capture among %s" % [all_clips]
		_say(); return
	_capture()

func _meshes(node: Node) -> Array:
	var out_list := []
	if node is MeshInstance3D:
		out_list.append(node)
	for c in node.get_children():
		out_list += _meshes(c)
	return out_list

func _capture() -> void:
	for clip in clips:
		var anim: Animation = player.get_animation(clip)
		var length: float = anim.length
		for view in views:
			var direction: Vector3 = views[view]
			cam.global_position = centre + direction * reach * 2.6
			cam.look_at(centre, Vector3.UP)
			for i in samples:
				var t: float = length * float(i) / float(samples - 1) if samples > 1 else 0.0
				t = minf(t, maxf(length - 0.001, 0.0))
				player.play(clip)
				player.seek(t, true)
				player.pause()
				await get_tree().process_frame
				await get_tree().process_frame
				await RenderingServer.frame_post_draw
				var img := get_viewport().get_texture().get_image()
				var path := dir.path_join("%s_%s_%02d.png" % [String(clip).replace("/", "-"), view, i])
				img.save_png(path)
				out["frames"].append({"clip": String(clip), "view": view, "sample": i, "t": snappedf(t, 0.001), "path": path})
		player.stop()
	out["ok"] = true
	_say()
""".replace("__MARK__", CAPTURE_MARK)


def capture_clips(project_dir, scene_res: str, out_dir, *, clips: Optional[list] = None,
                  samples: int = 5, size: tuple = (480, 640), timeout: int = 240) -> dict:
    """Run the project with the capture autoload and collect frames + sheets.

    Returns {ok, frames:[{clip, view, sample, t, path}], sheets:[{clip,
    path}], clips, errors}. The character scene must be one the engine
    loads (godot_character_wire's), and the run needs a display.
    """
    project = Path(project_dir)
    if not (project / "project.godot").exists():
        return {"ok": False, "error": f"no project.godot in {project_dir}"}
    if not str(scene_res).startswith("res://"):
        return {"ok": False, "error": "scene_res must be a res:// path"}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    started = _godot._begin_injection(project, _CAPTURE_SCRIPT, _CAPTURE_GD, "BGateClipShot")
    if started.get("error"):
        return {"ok": False, **started}
    try:
        cmd = [_godot.find_godot(), "--path", str(project),
               "--resolution", "%dx%d" % (int(size[0]), int(size[1]))]
        env = {**os.environ, "BGATE_CLIP_SCENE": str(scene_res),
               "BGATE_CLIP_DIR": str(out.resolve()).replace("\\", "/"),
               "BGATE_CLIP_SAMPLES": str(int(samples)),
               "BGATE_CLIP_LIST": ",".join(str(c) for c in (clips or []))}
        try:
            with _godot._enginelock.hold(project, what="godot_clip_capture"):
                proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                      stdin=subprocess.DEVNULL, env=env,
                                      creationflags=_godot._NO_WINDOW, **_godot._TEXT)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"the capture run did not exit within {timeout}s"}
        except _godot._enginelock.EngineBusy as exc:
            return {"ok": False, "error": str(exc), "engine_contended": True}
        text = (proc.stdout or "") + (proc.stderr or "")
        report = None
        for line in text.splitlines():
            if CAPTURE_MARK in line:
                try:
                    report = json.loads(line.split(CAPTURE_MARK, 1)[1])
                except json.JSONDecodeError:
                    report = None
        if report is None:
            return {"ok": False, "error": "the capture printed no report",
                    "exit_code": proc.returncode, "output": text[-1500:],
                    "errors": _godot._errors(text)}
        report["errors"] = _godot._errors(text)
        if not report.get("ok"):
            return report
        report["sheets"] = _sheets(report.get("frames") or [], out)
        return report
    finally:
        _godot._end_injection(project, _CAPTURE_SCRIPT)


def _sheets(frames: list, out: Path) -> list:
    """One PNG per clip: rows per view, columns per sample."""
    from PIL import Image, ImageDraw
    by_clip: dict = {}
    for f in frames:
        by_clip.setdefault(f["clip"], {}).setdefault(f["view"], []).append(f)
    sheets = []
    for clip, views in by_clip.items():
        rows = [sorted(views[v], key=lambda r: r["sample"])
                for v in ("side", "front34", "back34") if v in views]
        if not rows:
            continue
        tiles = [[Image.open(r["path"]).convert("RGB") for r in row] for row in rows]
        w = max(t.width for row in tiles for t in row)
        h = max(t.height for row in tiles for t in row)
        cols = max(len(row) for row in tiles)
        sheet = Image.new("RGB", (cols * w, len(tiles) * h + 24), (30, 30, 34))
        draw = ImageDraw.Draw(sheet)
        draw.text((8, 6), "%s  (engine)" % clip, fill=(230, 230, 230))
        for j, row in enumerate(tiles):
            for i, tile in enumerate(row):
                sheet.paste(tile, (i * w, 24 + j * h))
        path = out / ("%s_engine_sheet.png" % clip.replace("/", "-"))
        sheet.save(path)
        sheets.append({"clip": clip, "path": str(path), "frames": sum(len(r) for r in rows)})
    return sheets
