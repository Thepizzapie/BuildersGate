"""Headless Godot adapter — build, run, and export from an agent's hands.

Windows binary note, MEASURED not assumed: Godot ships two exes, and the common
claim that the plain ``Godot_v*.exe`` loses stdout when piped is FALSE — verified
on 4.7.1, both binaries deliver identical stdout/stderr through a pipe. The
``_console.exe`` only exists to attach a console WINDOW for interactive
double-clicking; it is a ~200KB launcher that then spawns the real binary.

So we prefer the MAIN exe: same output, one less process between us and the
engine (a wrapper makes kills and timeouts leak grandchildren).

Godot ships as a portable single exe with no installer and no PATH entry, so
discovery has to look in Downloads and the usual per-user program dirs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from glob import escape as glob_escape
from glob import glob
from pathlib import Path
from typing import Optional

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Same rule as blender.py: a child that inherits a stdio MCP server's stdin
# blocks on the client's protocol channel forever. See mcp-subprocess-stdin.
# Godot emits UTF-8. Python's text mode on Windows decodes with the ANSI
# codepage instead, so every non-ASCII character in the engine's output — and in
# anything our own injected scripts print back — arrives mojibaked: an em dash
# came back as "â€”" inside a JSON finding we then handed to a caller.
# errors="replace" so a stray byte degrades one character rather than raising
# mid-result.
_TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}


def _spawn(cmd: list[str], timeout: int, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout,
                          cwd=cwd, stdin=subprocess.DEVNULL,
                          creationflags=_NO_WINDOW, **_TEXT)


_SEARCH_GLOBS = (
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Godot\Godot*.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Godot\Godot*.exe"),
    os.path.expandvars(r"%USERPROFILE%\Downloads\Godot*.exe"),
    r"C:\Program Files\Godot\Godot*.exe",
    "/Applications/Godot.app/Contents/MacOS/Godot",
    "/usr/bin/godot",
    "/usr/local/bin/godot",
)

_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

# A failed unzip leaves a 0-byte .exe that looks installed and dies with a
# baffling "not recognized as a program". Observed on this machine. The threshold
# only needs to reject stubs — the real editor is ~170MB and the console launcher
# ~200KB, so anything under 64KB is debris, not a binary.
_MIN_BYTES = 64_000


class GodotNotFound(RuntimeError):
    pass


def _is_usable(path: str) -> bool:
    try:
        return Path(path).is_file() and Path(path).stat().st_size >= _MIN_BYTES
    except OSError:
        return False


def find_godot(prefer_console: bool = False) -> str:
    """Locate a usable Godot binary. BGATE_GODOT overrides everything.

    Prefers the newest MAIN exe. prefer_console picks the _console.exe launcher
    instead — only useful when a human wants a visible console window; it does
    NOT affect piped stdout (measured, see module docstring).
    """
    override = os.environ.get("BGATE_GODOT")
    if override:
        if not _is_usable(override):
            raise GodotNotFound(
                f"BGATE_GODOT points at a missing or empty file: {override}")
        return override

    found: list[str] = []
    for pattern in _SEARCH_GLOBS:
        if "*" in pattern:
            found.extend(p for p in glob(pattern) if _is_usable(p))
        elif _is_usable(pattern):
            found.append(pattern)

    on_path = shutil.which("godot")
    if on_path and _is_usable(on_path):
        found.append(on_path)

    if not found:
        raise GodotNotFound(
            "Godot not found. It ships as a portable .exe with no installer — "
            "extract it to %LOCALAPPDATA%\\Programs\\Godot, or set BGATE_GODOT. "
            "(A 0-byte .exe from a failed unzip is ignored on purpose.)"
        )

    def rank(path: str) -> tuple:
        name = Path(path).name.lower()
        console = "_console" in name
        match = _VERSION.search(name)
        version = tuple(int(g or 0) for g in match.groups()) if match else (0, 0, 0)
        # Console first when asked, then newest version.
        return (console == prefer_console, version)

    return sorted(found, key=rank)[-1]


def available() -> dict:
    try:
        path = find_godot()
    except GodotNotFound as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, "path": path}


# A binary's version cannot change unless the FILE changes, so the cache is
# keyed on the path and its mtime rather than on a clock. That is exact: a TTL
# would be a guess that is either wrong for a while or re-spawns Godot for no
# reason, and this is now on a path the playtest panel reads.
_VERSION_CACHE: dict[tuple[str, float], str] = {}


def version() -> dict:
    """Which Godot this is, asked of the binary rather than of its filename.

    THE FILENAME IS NOT THE VERSION. `Godot_v4.4.1-stable_win64.exe` is a
    convention, not a guarantee, and export templates are matched against what
    the engine reports about itself - so anything deciding whether a build can
    run has to ask the engine.
    """
    exe = find_godot()
    try:
        key = (exe, os.path.getmtime(exe))
    except OSError:
        key = (exe, 0.0)
    hit = _VERSION_CACHE.get(key)
    if hit is not None:
        return {"path": exe, "version": hit}
    proc = _spawn([exe, "--version"], timeout=60)
    raw = (proc.stdout or proc.stderr or "").strip().splitlines()
    found = raw[-1] if raw else "unknown"
    _VERSION_CACHE[key] = found
    return {"path": exe, "version": found}


# --script requires a SceneTree/MainLoop script. Handing Godot anything else
# does NOT produce a console error: on Windows it raises a BLOCKING OS message
# box ("Can't load the script ... doesn't inherit from SceneTree or MainLoop"),
# even under --headless. The process then hangs until the timeout, the caller
# is told "call quit()" - the wrong hint - and the popup is left on the
# operator's desktop, one per retry. So the inheritance rule is checked HERE,
# before a process exists, and the refusal goes back on the same channel the
# agent actually reads.
_MAINLOOP_BASES = {"SceneTree", "MainLoop"}
# Bases an agent actually writes that positively cannot drive --script. An
# identifier NOT in this set and not in _MAINLOOP_BASES is let through: it may
# be a user class whose chain reaches SceneTree, and a false refusal here would
# block a legitimate run.
_NOT_MAINLOOP_BASES = {
    "Object", "RefCounted", "Resource", "Node", "CanvasItem", "CanvasLayer",
    "Node2D", "Node3D", "Control", "Window", "Area2D", "Area3D",
    "CharacterBody2D", "CharacterBody3D", "RigidBody2D", "RigidBody3D",
    "StaticBody2D", "StaticBody3D", "AnimatableBody2D", "AnimatableBody3D",
    "Sprite2D", "Sprite3D", "AnimatedSprite2D", "Camera2D", "Camera3D",
    "Label", "Button", "Panel", "Timer", "AudioStreamPlayer",
}
_EXTENDS_RE = re.compile(r"^\s*(?:class_name\s+\w+\s+)?extends\s+([\"']?)([^\s\"']+)\1")


def _script_gate(script: str) -> Optional[dict]:
    """Refuse a script --script cannot run, before Godot is spawned."""
    base = None
    for line in script.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        m = _EXTENDS_RE.match(stripped)
        if m:
            quoted, base = m.group(1), m.group(2)
            if quoted:  # extends "res://..." - chain unknowable, let Godot try
                return None
            break
    if base in _MAINLOOP_BASES:
        return None
    if base is not None and base not in _NOT_MAINLOOP_BASES:
        return None  # custom class name - the chain may reach SceneTree
    got = f"`extends {base}`" if base else "no extends clause (implicit RefCounted)"
    return {
        "ok": False,
        "error": f"script cannot run under --script: {got}. "
                 "A headless run must `extends SceneTree` (or MainLoop).",
        "hint": "start the script with `extends SceneTree`, do the work in "
                "`func _init():`, and end with `quit()`. To exercise a Node "
                "script, load it from a SceneTree script instead: "
                "var n = load(\"res://path.gd\").new(); root.add_child(n)",
        "seconds": 0.0,
    }


def run_script(script: str, project_dir: Optional[str] = None,
               timeout: int = 120) -> dict:
    """Run a GDScript file headless (a SceneTree script) and capture output.

    The script must extend SceneTree or MainLoop and call quit(), or it will run
    until the timeout. Returns {ok, stdout, stderr, exit_code, seconds}.
    """
    import tempfile
    import time

    refused = _script_gate(script)
    if refused is not None:
        return refused

    exe = find_godot()
    # The scratch dir is torn down in a finally, INCLUDING on the timeout path.
    # It used to be a bare mkdtemp with no cleanup at all, so every agent script
    # run — and every retry after a timeout, which is when you run the most —
    # left a directory behind in %TEMP% forever.
    tmp = Path(tempfile.mkdtemp(prefix="bgate_godot_"))
    try:
        # Godot only loads scripts from inside a project when --path is given;
        # for a bare script run it reads the file directly.
        script_path = tmp / "agent_script.gd"
        script_path.write_text(script, encoding="utf-8")

        cmd = [exe, "--headless"]
        if project_dir:
            cmd += ["--path", str(project_dir)]
        cmd += ["--script", str(script_path)]

        started = time.monotonic()
        try:
            proc = _spawn(cmd, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"Godot timed out after {timeout}s",
                    "hint": "a SceneTree script must call quit() or it runs forever",
                    "seconds": timeout}
        finally:
            elapsed = round(time.monotonic() - started, 2)

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        return {
            "ok": proc.returncode == 0 and "SCRIPT ERROR" not in stdout + stderr,
            "stdout": stdout[-8000:],
            "stderr": stderr[-4000:],
            "exit_code": proc.returncode,
            "seconds": elapsed,
            "errors": _errors(stdout + stderr),
        }
    finally:
        # ignore_errors: a killed Godot can still hold the .gd open for a beat
        # on Windows, and a failed cleanup must never mask the real result.
        shutil.rmtree(tmp, ignore_errors=True)


def _template_dirs() -> list[Path]:
    """Where Godot 4 keeps downloaded export templates, per platform."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", "")
        roots = [Path(base) / "Godot"] if base else []
    elif sys.platform == "darwin":
        roots = [Path.home() / "Library" / "Application Support" / "Godot"]
    else:
        data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        roots = [Path(data) / "godot", Path(data) / "Godot"]
    return [r / "export_templates" for r in roots]


def export_templates(platform: str = "web") -> dict:
    """Are the export templates for this Godot's version installed?

    Export templates are a SEPARATE ~1GB download from the editor, and without
    them `--export-release` fails with an error most people read as "my preset
    is wrong". It is the single most common reason a web build does not appear,
    so it gets a real probe rather than a comment in a README.

    Returns {available, path, version, reason}. Never raises.
    """
    try:
        found = version().get("version", "")
    except Exception as exc:
        return {"available": False, "path": "", "version": "",
                "reason": f"could not ask Godot its version ({exc})"}

    # "4.7.1.stable.official.a13da4feb" -> "4.7.1.stable"; templates ship under
    # <major.minor.patch>.<status>, and the patch is dropped when it is 0.
    parts = found.split(".")
    wanted: list[str] = []
    if len(parts) >= 4:
        wanted.append(".".join(parts[:4]))
        if parts[2] == "0":
            wanted.append(".".join(parts[:2] + parts[3:4]))

    searched = [str(d) for d in _template_dirs()]
    others: list[str] = []
    for base in _template_dirs():
        if not base.is_dir():
            continue
        for folder in sorted((d for d in base.iterdir() if d.is_dir()),
                             reverse=True):
            hits = sorted(p.name for p in folder.glob(f"{platform}*.zip"))
            if not hits:
                continue
            if folder.name in wanted:
                return {"available": True, "path": str(folder),
                        "version": folder.name, "files": hits, "reason": ""}
            others.append(folder.name)

    # A near miss is worth naming: "you have 4.6.stable, Godot is 4.7.1.stable"
    # is a 30-second fix, while a bare "not installed" sends people to the wrong
    # place. Godot refuses to export against mismatched templates, so this is
    # still unavailable.
    if others:
        return {"available": False, "path": "", "version": found,
                "reason": f"{platform} export templates installed for "
                          f"{', '.join(sorted(set(others)))} but Godot is "
                          f"{found} — Godot refuses to export against a "
                          "mismatched version. Install the matching set "
                          "(Editor > Manage Export Templates)"}
    return {"available": False, "path": "", "version": found,
            "reason": f"no {platform} export templates for Godot {found}. "
                      "Install them from the editor (Editor > Manage Export "
                      "Templates > Download and Install), or drop the "
                      f".tpz contents into one of: {', '.join(searched)}"}


def check_project(project_dir: str, timeout: int = 180) -> dict:
    """Import/validate a project without opening the editor. The 'does it build'."""
    import time

    project = Path(project_dir)
    if not (project / "project.godot").exists():
        return {"ok": False, "error": f"no project.godot in {project_dir}"}

    exe = find_godot()
    started = time.monotonic()
    try:
        # --import builds the .godot cache and reports resource errors, then exits.
        proc = _spawn([exe, "--headless", "--path", str(project), "--import"],
                      timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"import timed out after {timeout}s"}

    output = (proc.stdout or "") + (proc.stderr or "")
    errors = _errors(output)
    return {
        "ok": proc.returncode == 0 and not errors,
        "exit_code": proc.returncode,
        "errors": errors,
        "seconds": round(time.monotonic() - started, 2),
        "output": output[-3000:],
    }


# Walks an imported scene and reports what the ENGINE actually got — not what
# the exporter claimed. Printed as one JSON line between markers so it survives
# Godot's chatty stdout.
#
# WHAT THIS USED TO MISS, and why each gap let a broken asset pass green:
#
#   * It walked ONLY MeshInstance3D. A rig that failed to arrive as a Skeleton3D
#     — the single most common glTF import failure for a character — produced an
#     identical, cheerful report to a rig that arrived perfectly. Same for an
#     AnimationPlayer: the difference between a character that can idle and a
#     T-pose statue was invisible here.
#   * It reported `mat.resource_name` and nothing else. A material with a NAME
#     is not a material with a TEXTURE. The documented "21 materials and ZERO
#     images" disaster passed this check with 21 green rows.
#   * It reported `mesh.get_aabb()`, which is the Mesh RESOURCE's LOCAL box. It
#     ignores every node transform above it — so a character scaled 40x by its
#     parent node reported its correct unscaled size and passed. The one number
#     that would have caught it was measured before the transform that broke it.
#
# So: transforms are accumulated down the walk (`here = xform * node.transform`)
# rather than read from `global_transform`, which ERR_FAILs on a node that is not
# inside a SceneTree — and an instantiated PackedScene is not.
_INSPECT_GD = """
extends SceneTree

var _skeletons := []
var _players := []
var _attachments := []
var _lights := []
var _bounds := AABB()
var _has_bounds := false

func _mat_info(mat: Material) -> Dictionary:
	var out := {"material": "", "class": "", "albedo_texture": "",
		"albedo_size": [], "has_albedo_texture": false}
	if mat == null:
		return out
	out["material"] = String(mat.resource_name)
	out["class"] = mat.get_class()
	out["resource_path"] = String(mat.resource_path)
	if mat is BaseMaterial3D:
		var tex: Texture2D = (mat as BaseMaterial3D).albedo_texture
		if tex != null:
			out["has_albedo_texture"] = true
			out["albedo_texture"] = (String(tex.resource_path)
				if String(tex.resource_path) != "" else "<embedded>")
			out["albedo_size"] = [tex.get_width(), tex.get_height()]
		var col: Color = (mat as BaseMaterial3D).albedo_color
		out["albedo_color"] = [
			snappedf(col.r, 0.001), snappedf(col.g, 0.001), snappedf(col.b, 0.001)]
	elif mat is ShaderMaterial:
		# A shader can sample anything; we cannot prove a texture either way, so
		# say so instead of reporting a confident false.
		out["has_albedo_texture"] = true
		out["albedo_texture"] = "<shader material — not statically checkable>"
	return out

## Resolved the same way the renderer resolves it: override, then per-surface
## override, then the mesh's own material.
func _surface_material(node: MeshInstance3D, mesh: Mesh, i: int) -> Material:
	if node.material_override != null:
		return node.material_override
	var over: Material = node.get_surface_override_material(i)
	if over != null:
		return over
	return mesh.surface_get_material(i)

func _walk(node: Node, xform: Transform3D, out: Array, path: String) -> void:
	var here := xform
	if node is Node3D:
		here = xform * (node as Node3D).transform

	if node is Skeleton3D:
		var sk := node as Skeleton3D
		var bones := []
		for b in sk.get_bone_count():
			bones.append(String(sk.get_bone_name(b)))
		_skeletons.append({"name": String(node.name), "bones": sk.get_bone_count(),
			"bone_names": bones.slice(0, 32)})

	if node is AnimationPlayer:
		var ap := node as AnimationPlayer
		var clips := []
		for a in ap.get_animation_list():
			var clip: Animation = ap.get_animation(a)
			clips.append({
				"name": String(a),
				"length": (snappedf(clip.length, 0.001) if clip != null else 0.0),
				"tracks": (clip.get_track_count() if clip != null else 0),
				"loop": (clip.loop_mode != Animation.LOOP_NONE
					if clip != null else false),
			})
		_players.append({"name": String(node.name), "animations": clips})

	if node is BoneAttachment3D:
		_attachments.append({"name": String(node.name),
			"bone": String((node as BoneAttachment3D).bone_name)})

	if node is Light3D:
		_lights.append(String(node.name))

	if node is MeshInstance3D and node.mesh != null:
		var mi := node as MeshInstance3D
		var mesh: Mesh = mi.mesh
		var tris := 0
		var verts_total := 0
		var surfaces := []
		for i in mesh.get_surface_count():
			var arrays := mesh.surface_get_arrays(i)
			var verts: PackedVector3Array = arrays[Mesh.ARRAY_VERTEX]
			var idx = arrays[Mesh.ARRAY_INDEX]
			var count: int = (idx.size() if idx != null else verts.size()) / 3
			tris += count
			verts_total += verts.size()
			var entry := _mat_info(_surface_material(mi, mesh, i))
			entry["index"] = i
			entry["tris"] = count
			entry["has_uv"] = arrays[Mesh.ARRAY_TEX_UV] != null
			surfaces.append(entry)

		var shapes := []
		if mesh is ArrayMesh:
			var am := mesh as ArrayMesh
			for s in am.get_blend_shape_count():
				shapes.append(String(am.get_blend_shape_name(s)))

		var local := mesh.get_aabb()
		# THE fix. `here` is every transform between the scene root and this
		# node, so this is the box the player actually sees.
		var world: AABB = here * local
		if _has_bounds:
			_bounds = _bounds.merge(world)
		else:
			_bounds = world
			_has_bounds = true

		out.append({
			"name": String(node.name),
			# Path from the imported scene root. This is the key the .import
			# file's `_subresources` uses ("PATH:<this>"), so per-node importer
			# settings — collider generation above all — need it verbatim.
			"path": path,
			"tris": tris,
			"verts": verts_total,
			"surfaces": surfaces,
			"aabb_size": [local.size.x, local.size.y, local.size.z],
			"aabb_global_size": [snappedf(world.size.x, 0.0001),
				snappedf(world.size.y, 0.0001), snappedf(world.size.z, 0.0001)],
			"aabb_global_position": [snappedf(world.position.x, 0.0001),
				snappedf(world.position.y, 0.0001),
				snappedf(world.position.z, 0.0001)],
			"scale": [snappedf(here.basis.get_scale().x, 0.0001),
				snappedf(here.basis.get_scale().y, 0.0001),
				snappedf(here.basis.get_scale().z, 0.0001)],
			"skinned": mi.skin != null,
			"blend_shapes": shapes,
			"visible": mi.visible,
		})

	# Colliders the importer built. A .glb never carries one; if this list is
	# empty the asset cannot be stood on, walked into, or shot.
	if node is CollisionShape3D or node is CollisionPolygon3D:
		var owner_class := (node.get_parent().get_class()
			if node.get_parent() != null else "")
		var shape_class := ""
		if node is CollisionShape3D and (node as CollisionShape3D).shape != null:
			shape_class = (node as CollisionShape3D).shape.get_class()
		out.append({"collider": String(node.name), "path": path,
			"body": owner_class, "shape": shape_class})

	for child in node.get_children():
		_walk(child, here, out, (String(child.name) if path == ""
			else path + "/" + String(child.name)))

## glTF is METRES, by specification. Godot imports a 180 m character and a
## 0.018 m one without a word of complaint, and both look plausible in an
## isolated Blender render — you only find out when the player walks into an
## ankle or a skyscraper. This is the check nothing else in the pipeline makes.
func _size_check() -> Dictionary:
	if not _has_bounds:
		return {"ok": false, "reason": "no mesh geometry to measure"}
	var lo := float(OS.get_environment("BGATE_SIZE_MIN"))
	var hi := float(OS.get_environment("BGATE_SIZE_MAX"))
	var nominal := float(OS.get_environment("BGATE_SIZE_NOMINAL"))
	if lo <= 0.0:
		lo = 0.05
	if hi <= 0.0:
		hi = 50.0
	if nominal <= 0.0:
		nominal = 1.8
	var s := _bounds.size
	var longest: float = maxf(s.x, maxf(s.y, s.z))
	var out := {
		"metres": [snappedf(s.x, 0.0001), snappedf(s.y, 0.0001),
			snappedf(s.z, 0.0001)],
		"longest_axis_m": snappedf(longest, 0.0001),
		"min_m": lo, "max_m": hi,
		"suggested_scale": snappedf(nominal / maxf(longest, 0.000001), 0.0001),
		"ok": longest >= lo and longest <= hi,
	}
	if longest > hi:
		out["note"] = ("%.3f m across — glTF units are METRES, so this is a " % longest
			+ "building, not a prop. A 100x unit error (cm modelled as m) is "
			+ "the usual cause.")
	elif longest < lo:
		out["note"] = ("%.5f m across — smaller than a coin. Check for a " % longest
			+ "0.01 scale factor on export.")
	return out

func _init():
	var path := OS.get_environment("BGATE_INSPECT")
	var res = load(path)
	if res == null:
		print("BGATE_JSON_START")
		print(JSON.stringify({"ok": false, "error": "engine could not load " + path}))
		print("BGATE_JSON_END")
		quit()
		return
	if not (res is PackedScene):
		print("BGATE_JSON_START")
		print(JSON.stringify({"ok": false,
			"error": "loaded, but not a PackedScene: " + res.get_class()}))
		print("BGATE_JSON_END")
		quit()
		return
	var root: Node = res.instantiate()
	var walked := []
	_walk(root, Transform3D.IDENTITY, walked, "")

	var meshes := []
	var colliders := []
	for w in walked:
		if w.has("collider"):
			colliders.append(w)
		else:
			meshes.append(w)

	var total := 0
	var textureless := []
	var textured := 0
	var surfaces := 0
	var blend_shapes := []
	var skinned := 0
	for m in meshes:
		total += m["tris"]
		if m["skinned"]:
			skinned += 1
		for bs in m["blend_shapes"]:
			if not blend_shapes.has(bs):
				blend_shapes.append(bs)
		for s in m["surfaces"]:
			surfaces += 1
			if s["has_albedo_texture"]:
				textured += 1
			else:
				textureless.append({"mesh": m["name"], "surface": s["index"],
					"material": s["material"]})

	var animations := []
	for p in _players:
		for a in p["animations"]:
			animations.append(a["name"])

	print("BGATE_JSON_START")
	print(JSON.stringify({
		"ok": true,
		"resource": path,
		"root": root.name,
		"root_type": root.get_class(),
		"meshes": meshes,
		"total_tris": total,
		"skeletons": _skeletons,
		"skeleton_count": _skeletons.size(),
		"animation_players": _players,
		"animations": animations,
		"animation_count": animations.size(),
		"bone_attachments": _attachments,
		"lights": _lights,
		"skinned_meshes": skinned,
		"blend_shapes": blend_shapes,
		"colliders": colliders,
		"collider_count": colliders.size(),
		"materials": {
			"surfaces": surfaces,
			"with_albedo_texture": textured,
			"without_albedo_texture": textureless,
		},
		# The headline. A rigged, animated, TEXTURED, correctly-sized asset with
		# a collider is what "delivered" means; anything less is a file on disk.
		"size_check": _size_check(),
	}))
	print("BGATE_JSON_END")
	quit()
"""


def _digest(path: Path) -> str:
    """Short sha256 of a file — enough to say "a different mesh", not a key."""
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def import_asset(project_dir: str, src_path: str, dest_rel: str = "assets",
                 timeout: int = 240, *, allow_overwrite: bool = True) -> dict:
    """Bring an asset into a Godot project and VERIFY the engine loads it.

    Copies src into <project>/<dest_rel>/, triggers a headless import, then loads
    the resource in-engine and reports the meshes Godot actually built. Copying a
    file in is not integration — an asset that imports with zero surfaces is a
    silent failure, so this checks the ENGINE's view, not the file's presence.

    THE DESTINATION IS KEYED ON THE FILENAME ALONE. Two different `hero.glb`,
    from two different Blender output directories, land on the same
    `assets/hero.glb` and the second one wins. Keeping the existing `.import`
    and its uid is correct — every .tscn in the project references that uid, and
    a new one would orphan them all — but the SILENCE was not: the mesh under
    that uid changed and nothing said so. `replaced` in the result now carries
    the evidence (whether the bytes actually differ, and the old size and
    digest). allow_overwrite=False refuses instead, for a caller that would
    rather stop than find out later.
    """
    project = Path(project_dir)
    if not (project / "project.godot").exists():
        return {"ok": False, "error": f"no project.godot in {project_dir}"}
    src = Path(src_path)
    if not src.exists():
        return {"ok": False, "error": f"asset not found: {src_path}"}

    dest_dir = project / dest_rel
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name

    replaced = None
    if dest.exists():
        before, after = _digest(dest), _digest(src)
        replaced = {
            "path": str(dest),
            # The distinction that matters. Re-delivering the SAME .glb after a
            # re-export is the normal iteration loop and is not news; a set of
            # bytes that has changed under an existing uid is.
            "content_changed": before != after,
            "previous_bytes": dest.stat().st_size,
            "previous_sha256": before,
            "new_sha256": after,
            "note": ("a DIFFERENT mesh now lives at this path — the existing "
                     ".import and its uid are kept, so every scene referencing "
                     "it now shows this asset instead"
                     if before != after else
                     "same bytes, so this overwrite changed nothing"),
        }
        if replaced["content_changed"] and not allow_overwrite:
            return {"ok": False,
                    "error": f"{dest.relative_to(project)} already exists with "
                             "different content — pass allow_overwrite=True to "
                             "replace the mesh under its existing uid",
                    "replaced": replaced}

    shutil.copy2(src, dest)

    imported = check_project(str(project), timeout=timeout)
    res_path = "res://" + str(dest.relative_to(project)).replace("\\", "/")
    inspected = inspect_resource(str(project), res_path, timeout=timeout)

    return {
        "ok": bool(inspected.get("ok")),
        "copied_to": str(dest),
        "res_path": res_path,
        # None when nothing was there; a dict naming what was overwritten
        # otherwise. Never absent, so a caller can test one key.
        "replaced": replaced,
        "import": {"ok": imported["ok"], "errors": imported.get("errors", [])},
        "engine_view": inspected,
        # None on anything that is not glTF, or when the file cannot be read.
        # See alpha_mode_report — a silent transparency downgrade that only shows
        # up as a framerate cliff on a forest.
        "alpha_mode": alpha_mode_report(dest),
    }


# The value Godot 4.7 does NOT give you, and the one it gives you instead.
GLTF_MASK = "MASK"
GODOT_MASK_ACTUAL = "DEPTH_PRE_PASS"
GODOT_MASK_EXPECTED = "ALPHA_SCISSOR"


def gltf_json(path: str | os.PathLike[str]) -> Optional[dict]:
    """The glTF JSON for a ``.gltf`` or ``.glb``, or None if it is neither.

    A ``.glb`` is a 12-byte header followed by length-prefixed chunks, the first
    of which is the same JSON a ``.gltf`` holds in the clear. Reading it needs no
    glTF library and no Blender — which matters, because this runs on an import
    path that must not grow a dependency to emit a warning.

    Never raises: a truncated or exotic file gives None, and a missing warning is
    a much smaller problem than an import that fails because of the warner.
    """
    p = Path(path)
    try:
        if p.suffix.lower() == ".gltf":
            return json.loads(p.read_text(encoding="utf-8"))
        if p.suffix.lower() != ".glb":
            return None
        raw = p.read_bytes()
        if len(raw) < 20 or raw[:4] != b"glTF":
            return None
        length = int.from_bytes(raw[12:16], "little")
        if raw[16:20] != b"JSON":
            return None
        return json.loads(raw[20:20 + length].decode("utf-8"))
    except Exception:
        return None


def alpha_mode_report(path: str | os.PathLike[str]) -> Optional[dict]:
    """Which materials use ``alphaMode: MASK``, and what Godot will do with them.

    GODOT 4.7 IMPORTS glTF ``MASK`` AS ``DEPTH_PRE_PASS``, NOT ``ALPHA_SCISSOR``.
    Nothing errors, nothing is logged, and the mesh looks right in a screenshot.
    What changes is which render pass it lands in: scissor is an OPAQUE-pass
    cutout resolved per fragment, while depth-pre-pass puts the surface into the
    SORTED TRANSPARENT pass. On a tree that is hundreds of alpha quads, every one
    of them is then depth-sorted per frame and cannot early-z against its
    neighbours — a framerate cliff on a forest, with no error to search for and
    no visual tell to notice.

    Reported rather than corrected. The fix is a per-material override in the
    sibling ``.import`` file, and choosing it needs to know whether the surface
    is foliage (wants scissor) or genuinely translucent (wants the transparent
    pass) — which this cannot know from the file. Naming it at import time is the
    difference between a five-minute change and an afternoon profiling a forest.

    None for a non-glTF file or one that cannot be parsed; a dict with an empty
    ``materials`` list when the file is glTF and uses no MASK at all, because
    "checked, nothing to say" and "not checked" must not read the same.
    """
    doc = gltf_json(path)
    if doc is None:
        return None
    masked = [str(m.get("name") or f"material {i}")
              for i, m in enumerate(doc.get("materials") or [])
              if str(m.get("alphaMode") or "").upper() == GLTF_MASK]
    if not masked:
        return {"masked_materials": [], "warning": ""}
    return {
        "masked_materials": masked,
        "warning": (
            f"{len(masked)} material(s) use glTF alphaMode:{GLTF_MASK} "
            f"({', '.join(masked[:6])}"
            + (f", +{len(masked) - 6} more" if len(masked) > 6 else "")
            + f"). Godot 4.7 imports that as {GODOT_MASK_ACTUAL}, NOT "
            f"{GODOT_MASK_EXPECTED} — so these surfaces render in the sorted "
            "TRANSPARENT pass instead of as opaque cutouts. Silent, and it costs "
            "real frames once there are hundreds of alpha quads on screen. If "
            "these are foliage or any hard-edged cutout, override the material's "
            f"transparency to {GODOT_MASK_EXPECTED} in the sibling .import "
            "rather than leaving the default."),
    }


# ---------------------------------------------------------------------------
# Import settings — the sibling .import file, and why nothing worked without it
# ---------------------------------------------------------------------------
#
# A .glb has no colliders. glTF has no concept of one. Godot's scene importer
# CAN build them, but `generate/physics` defaults to OFF, and the switch lives in
# the sibling `<asset>.glb.import` file — which the old import path never wrote,
# so Godot's defaults stood and there was no CollisionShape3D anywhere in any
# imported asset, ever. The mesh went in, the player walked through it.
#
# The settings are per-node and live under `_subresources`, keyed "PATH:<node
# path from the imported scene root>". You cannot know those paths before the
# first import, which is why delivery is two passes: import once to learn the
# tree, write the settings, purge the cache, import again.

# Godot 4 `physics/shape_type` enum, in the importer's own order.
SHAPE_TYPES = {
    "decompose_convex": 0, "simple_convex": 1, "trimesh": 2,
    "box": 3, "sphere": 4, "cylinder": 5, "capsule": 6,
}
# `physics/body_type`.
BODY_TYPES = {"static": 0, "dynamic": 1, "area": 2}

# Two forms, and missing either one is a real bug rather than a cosmetic one:
# Godot writes the EMPTY dictionary as a single line, `_subresources={}`, which
# is what every freshly-imported asset has, and the populated one is a nested
# multi-line block. Anything that only knows one form silently appends a SECOND
# `_subresources` key instead of replacing the first, and which one the engine
# honours is then down to ConfigFile's duplicate-key behaviour.
#
# THE VALUE IS NESTED, SO IT IS NOT A REGEX JOB. The pattern that used to live
# here (`^_subresources=\{\n.*?^\}[ \t]*$`) is lazy, so it stopped at the FIRST
# line-anchored `}` — the one closing the inner `"PATH:<node>": {` dict, two
# braces short of the end of the value. REPRODUCED: the first delivery is safe
# because a fresh `.import` still has the single-line empty form, and the SECOND
# delivery of the same asset — i.e. every art iteration — replaced the head of
# the block and left `}\n}` stranded after it, giving a 3-open/5-close file that
# Godot's ConfigFile cannot parse at all. Counting braces is the only reading
# that ends where the value ends.
_SUBRES_START = re.compile(r"^_subresources=\{", re.M)


def _subresources_span(body: str) -> Optional[tuple[int, int, int]]:
    """(key_start, value_start, end) of the `_subresources=` entry, or None.

    None when there is no key at all, and ALSO when the block never closes — a
    file that is already unbalanced is one we cannot safely edit, and guessing
    at where the value ends is exactly how it got broken the first time.
    """
    match = _SUBRES_START.search(body)
    if not match:
        return None

    depth = 0
    in_string = False
    value_start = match.end() - 1            # sitting on the opening brace
    i = value_start
    while i < len(body):
        char = body[i]
        if in_string:
            # Node paths are quoted and Godot escapes with a backslash; a `}`
            # inside a quoted key must not close the block.
            if char == "\\":
                i += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                while end < len(body) and body[end] in " \t":
                    end += 1
                return match.start(), value_start, end
        i += 1
    return None


def _replace_subresources(body: str, rendered: str) -> tuple[str, bool]:
    """Swap the whole `_subresources=` value for `rendered`, braces balanced.

    Returns (text, replaced); False means there was nothing safely replaceable.
    """
    span = _subresources_span(body)
    if span is None:
        return body, False
    start, _value_start, end = span
    return body[:start] + rendered + body[end:], True


def _read_subresources(body: str) -> tuple[dict, str]:
    """Parse the existing `_subresources` value. Returns (dict, error).

    Godot's variant-text dictionary is JSON in every form this key takes —
    quoted keys, and values that are strings, ints, floats, bools or arrays of
    those. Parsing it is what lets us MERGE. An error string (with an empty
    dict) means the value used a Variant constructor we cannot read, and the
    caller has to say so instead of silently discarding it.
    """
    span = _subresources_span(body)
    if span is None:
        return {}, ""
    _start, value_start, end = span
    raw = body[value_start:end].strip()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return {}, f"{exc}"
    return (parsed, "") if isinstance(parsed, dict) else (
        {}, f"_subresources is a {type(parsed).__name__}, not a dictionary")


def _gd_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"{}"'.format(str(value).replace('"', '\\"'))


def _gd_value(value) -> str:
    """Godot's variant-text form, nested. Matches the engine's own layout: a
    dictionary opens its brace, then one `"key": value` per line, unindented."""
    if isinstance(value, dict):
        if not value:
            return "{}"
        inner = ",\n".join(f'"{k}": {_gd_value(v)}' for k, v in value.items())
        return "{\n" + inner + "\n}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_gd_value(v) for v in value) + "]"
    return _gd_literal(value)


def _render_subresources(nodes: dict) -> str:
    """Godot's variant-text form of the `_subresources` dictionary."""
    if not nodes:
        return "_subresources={}"
    return _render_subresources_value(
        {"nodes": {f"PATH:{path}": opts for path, opts in nodes.items()}})


def _render_subresources_value(value: dict) -> str:
    """The whole key line, for an already-merged `_subresources` dictionary."""
    return "_subresources=" + _gd_value(value) if value else "_subresources={}"


_DEST_FILES = re.compile(r"^dest_files=\[(.*?)\]", re.M | re.S)


def _purge_import_cache(project: Path, asset: Path,
                        import_text: str = "") -> list[str]:
    """Drop the cached import products so the next --import genuinely re-runs.

    Godot decides "already imported" from the .md5 sidecar of the SOURCE file.
    Changing only the .import params leaves that md5 identical, so a reimport is
    skipped and the new settings never take — the failure looks exactly like the
    settings being wrong, which is a long afternoon.

    WHICH files, though. Godot keys the cache `<basename>-<hash of the res
    path>`, so a `<basename>-*` glob is wrong twice over:

      * `props/crate.glb` and `enemies/crate.glb` share a prefix, and purging
        one dropped the other's cached .scn and .md5. Bounded — delivery
        re-imports immediately and .godot/ is gitignored — so it cost time, not
        data.
      * An asset named `hero[1].glb` turns into a glob with a CHARACTER CLASS in
        it, which cannot match its own entries. The purge then silently
        no-opped, the reimport was skipped, and the new collider settings never
        took: the exact "the settings are wrong" afternoon this function exists
        to prevent, arriving through the function meant to prevent it.

    So the .import file's own `dest_files` is the authority when it is there —
    the engine wrote it and it names the cache entries exactly. The escaped
    prefix glob is the fallback for an .import too old or too fresh to carry it.
    """
    removed = []
    imported = project / ".godot" / "imported"
    if not imported.is_dir():
        return removed

    def drop(path: Path) -> None:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            pass

    declared = _DEST_FILES.search(import_text or "")
    dest_files = (re.findall(r'"([^"]+)"', declared.group(1))
                  if declared else [])
    if dest_files:
        for res in dest_files:
            cached = project / res.replace("res://", "").replace("/", os.sep)
            if cached.exists():
                drop(cached)
            # The .md5 sidecar sits beside the product under the same key, and
            # it is the file Godot reads to decide "already imported" — leaving
            # it behind is leaving the whole bug behind.
            sidecar = cached.with_suffix(".md5")
            if sidecar.exists():
                drop(sidecar)
        return removed

    for path in imported.glob(glob_escape(asset.name) + "-*"):
        drop(path)
    return removed


def write_import_settings(project_dir: str, asset_rel: str, *,
                          physics_nodes: Optional[dict] = None,
                          params: Optional[dict] = None,
                          purge: bool = True) -> dict:
    """Rewrite an asset's sibling `.import` file, preserving what we don't set.

    asset_rel      path of the asset relative to the project ("assets/hero.glb").
    physics_nodes  {node_path: {"body_type": "static", "shape_type": "trimesh"}}
                   — collider generation, per mesh node. This is the whole point.
    params         extra flat `[params]` overrides ("animation/import": True, ...).

    "Preserving what we don't set" was true of the flat `[params]` keys and a
    LIE about `_subresources`: the value was rendered fresh from physics_nodes
    and written over whatever was there. Everything a user sets in Godot's
    Import dock that lands under that key — per-node material extraction,
    animation loop mode and slices, LOD generation, "skip import" on a node —
    was deleted by one godot_deliver_asset, silently, in a file nobody opens.
    So it MERGES now: other top-level keys survive, nodes we do not name survive
    whole, and for a node we do name only the three collider keys are touched.

    The `.import` must already exist, which means the asset has been imported
    once. Returns {ok, path, physics_nodes, purged, preserved_nodes,
    merge_error}. A non-empty merge_error means the existing value used a
    Variant form we cannot parse and was REPLACED rather than merged — the one
    case where the old behaviour survives, and it says so out loud.
    """
    project = Path(project_dir)
    asset = project / asset_rel
    ini = asset.with_suffix(asset.suffix + ".import")
    if not ini.exists():
        return {"ok": False, "error": f"no .import beside {asset_rel} — the asset "
                                      "must be imported once before its settings "
                                      "can be set"}

    text = ini.read_text(encoding="utf-8")
    if "[params]" not in text:
        return {"ok": False, "error": f"{ini.name} has no [params] section"}
    head, body = text.split("[params]", 1)

    nodes: dict = {}
    for node_path, opts in (physics_nodes or {}).items():
        shape = opts.get("shape_type", "trimesh")
        body_type = opts.get("body_type", "static")
        entry = {"generate/physics": True}
        if body_type in BODY_TYPES:
            entry["physics/body_type"] = BODY_TYPES[body_type]
        if shape in SHAPE_TYPES:
            entry["physics/shape_type"] = SHAPE_TYPES[shape]
        nodes[node_path] = entry

    existing, merge_error = _read_subresources(body)
    merged = dict(existing)
    # Only the "nodes" sub-dictionary is ours. Godot puts other keys here too
    # (materials/, animations/ on some importers), and they are none of our
    # business.
    existing_nodes = merged.get("nodes")
    merged_nodes = dict(existing_nodes) if isinstance(existing_nodes, dict) else {}
    preserved = [key for key in merged_nodes
                 if key not in {f"PATH:{p}" for p in nodes}]
    for node_path, entry in nodes.items():
        key = f"PATH:{node_path}"
        settings_for_node = merged_nodes.get(key)
        settings_for_node = (dict(settings_for_node)
                             if isinstance(settings_for_node, dict) else {})
        # Update, never replace: a node can carry "use_external/enabled" from
        # the Import dock's material extraction alongside our collider keys.
        settings_for_node.update(entry)
        merged_nodes[key] = settings_for_node
    if merged_nodes:
        merged["nodes"] = merged_nodes

    rendered = _render_subresources_value(merged)
    body, replaced = _replace_subresources(body, rendered)
    if not replaced:
        body = body.rstrip() + "\n" + rendered + "\n"

    for key, value in (params or {}).items():
        line = f"{key}={_gd_literal(value)}"
        pattern = re.compile(r"^" + re.escape(key) + r"=.*$", re.M)
        body = (pattern.sub(lambda _m: line, body, count=1)
                if pattern.search(body) else body.rstrip() + "\n" + line + "\n")

    ini.write_text(head + "[params]" + body, encoding="utf-8")
    purged = _purge_import_cache(project, asset, text) if purge else []
    return {"ok": True, "path": str(ini), "physics_nodes": nodes,
            "purged": purged,
            # What survived that used to be destroyed. Empty is the normal case
            # on a freshly imported asset; non-empty is the whole point.
            "preserved_nodes": preserved,
            "merge_error": merge_error}


def _import_uid(project_dir: str, asset_rel: str) -> str:
    """The `uid://` Godot assigned this asset, for a stable ext_resource ref."""
    ini = Path(project_dir) / (asset_rel + ".import")
    if not ini.exists():
        return ""
    match = re.search(r'^uid="(uid://[^"]+)"', ini.read_text(encoding="utf-8"),
                      re.M)
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# Scene generation — marrying the imported model to a body that can be played
# ---------------------------------------------------------------------------
#
# templates/3d/scenes/main.tscn ships a CharacterBody3D with a capsule and NO
# MESH, and every imported .glb arrives as a bare Node3D with no body. Neither
# half is a character. Nothing in the pipeline ever joined them, so "get the
# asset into the game" was manual work handed back to the human at the exact
# moment the tooling claimed to be done.
#
# A .glb cannot be edited — Godot re-imports it and discards changes — so the
# script, the collider and the hurtbox have to live in a .tscn that INSTANCES
# it. That .tscn is what this writes, and its capsule is sized from the MEASURED
# global-transform bounds rather than from the template's guess.


def _look_at_basis(eye, target, up=(0.0, 1.0, 0.0)) -> tuple:
    """Column vectors of a Godot camera basis looking from eye at target.

    Godot cameras look down their LOCAL -Z, so the basis Z column points from
    the target back toward the eye.
    """
    import math

    def sub(a, b):
        return (a[0] - b[0], a[1] - b[1], a[2] - b[2])

    def norm(v):
        length = math.sqrt(sum(c * c for c in v)) or 1.0
        return (v[0] / length, v[1] / length, v[2] / length)

    def cross(a, b):
        return (a[1] * b[2] - a[2] * b[1],
                a[2] * b[0] - a[0] * b[2],
                a[0] * b[1] - a[1] * b[0])

    z = norm(sub(eye, target))
    x = norm(cross(up, z))
    y = cross(z, x)
    return x, y, z


def _transform3d(basis_columns, origin) -> str:
    """Godot's 12-float Transform3D literal — basis ROWS, then origin.

    Verified against templates/3d/scenes/main.tscn's Sun: reading those twelve
    floats as rows yields three orthonormal COLUMNS, which is the only reading
    that makes the light point somewhere sensible.
    """
    x, y, z = basis_columns
    nums = [x[0], y[0], z[0], x[1], y[1], z[1], x[2], y[2], z[2],
            origin[0], origin[1], origin[2]]
    # `+ 0.0` folds -0.0 to 0.0; Godot parses "-0" fine but it reads as a bug
    # to anyone diffing the generated scene.
    return "Transform3D({})".format(
        ", ".join(f"{n + 0.0:.6g}" for n in nums))


# How wide a capsule may be relative to how tall, for something shaped like a
# person. 0.17 puts a 1.8 m humanoid in a 0.61 m wide capsule — inside the
# 0.3-0.4 m radius every shipped FPS controller uses, and clear of a 0.9 m
# doorway with room on both sides.
_HUMANOID_RADIUS_RATIO = 0.17


def _capsule_for_bounds(size_x, size_y, size_z) -> tuple[float, float]:
    """(radius, height) for a capsule that fits the ASSET, not its pose.

    REPRODUCED on a delivered 1.75 m character: the old rule was
    `max(size_x, size_z) * 0.5` over the merged AXIS-ALIGNED bounds of every
    mesh, so an A-pose handed it the ARM SPAN and the capsule came out
    radius=0.8158 — a 1.63 m wide cylinder around a person. She could not fit
    through a human-sized door and stood 0.8 m off every wall. The downward
    clamp below does not catch it: at 1.75 m tall, 0.8158 is still under half
    the height. `has_collider` only counts shapes, so it shipped green.

    Two rules, and both have to hold:

      * Take the SMALLER horizontal extent, not the larger. Limbs inflate one
        horizontal axis far more than the other — arms out along X leave Z
        reading the torso's depth (0.3-0.5 m on a human) — and it is
        span-agnostic, because a character modelled facing +X inflates Z
        instead and the same rule still picks the torso.

      * Cap an UPRIGHT figure at a human proportion of its own height. That is
        the backstop for the pose which inflates BOTH horizontal axes (arms
        forward AND out), which the first rule alone cannot see.

    The cap is gated on "taller than it is wide" because a crate, a car and a
    turret have none of a person's proportions: a 1x1x1 m crate must keep its
    0.5 m radius, and a 4.5 m long vehicle must not be squeezed to a human's.

    Erring SMALL is deliberate. An oversized capsule is invisible and blocks the
    player everywhere; an undersized one lets a corner clip, which is visible,
    local, and not the bug anyone loses an afternoon to.
    """
    height = max(float(size_y), 0.05)
    horizontals = (abs(float(size_x)), abs(float(size_z)))
    radius = max(min(horizontals) * 0.5, 0.01)
    if height > max(horizontals):
        radius = min(radius, height * _HUMANOID_RADIUS_RATIO)
    # Godot rejects a capsule whose radius exceeds half its height; a squat
    # asset (a crate, a turret) hits this immediately.
    if height > 0.03:
        radius = min(radius, height * 0.5 - 0.001)
    return max(radius, 0.01), height


def character_scene_text(model_res: str, *, node_name: str,
                         bounds_size, bounds_position,
                         script_res: str = "", model_uid: str = "",
                         camera_height: float = 0.7, with_camera: bool = False,
                         with_capsule: bool = True,
                         body_type: str = "CharacterBody3D") -> str:
    """The .tscn source: model instanced under a body, capsule fitted to it.

    with_camera adds a first-person Camera3D at eye height. OFF by default, and
    that default is the fix for an OBSERVED boot failure: a delivered pirate was
    instanced into a level that already had a player camera, Godot made one of
    the two current, and the game booted looking out of the pirate's eye
    sockets. A scene meant to be dropped into a level cannot ship a camera that
    might win. Turn it on only when this scene IS the player — templates/3d's
    player.gd does `@onready var _camera := $Camera3D` and will null-deref on
    the first mouse move without it.

    with_capsule adds the fitted CollisionShape3D on the root. OFF is for an
    asset whose colliders the IMPORTER built from its real geometry: a crate
    used to get an accurate trimesh StaticBody3D inside the imported model AND
    an invisible capsule on the root body, both live at once, and `has_collider`
    counting `> 0` was satisfied by either so it was never surfaced. Exactly one
    collision strategy per asset — see deliver_asset.
    """
    size_x, size_y, size_z = (float(v) for v in bounds_size)
    pos_x, pos_y, pos_z = (float(v) for v in bounds_position)

    radius, height = _capsule_for_bounds(size_x, size_y, size_z)

    # The model's centre goes to the body's origin, because that is where the
    # template's capsule already is and where player.gd's camera offset assumes
    # the eyes are. Getting this wrong buries the character in the floor.
    offset = (-(pos_x + size_x * 0.5), -(pos_y + size_y * 0.5),
              -(pos_z + size_z * 0.5))

    ext = ['[ext_resource type="PackedScene" '
           + (f'uid="{model_uid}" ' if model_uid else "")
           + f'path="{model_res}" id="1_model"]']
    if script_res:
        ext.append(f'[ext_resource type="Script" path="{script_res}" '
                   'id="2_script"]')

    # ext resources + the capsule sub_resource (when there is one) + 1
    steps = len(ext) + (1 if with_capsule else 0) + 1
    lines = [
        f"[gd_scene load_steps={steps} format=3]",
        "",
        *ext,
        "",
    ]
    if with_capsule:
        lines += [
            '[sub_resource type="CapsuleShape3D" id="CapsuleShape3D_body"]',
            f"radius = {radius:.4f}",
            f"height = {height:.4f}",
            "",
        ]
    lines.append(f'[node name="{node_name}" type="{body_type}"]')
    if script_res:
        lines.append('script = ExtResource("2_script")')
    lines += [
        "",
        # The model is a CHILD, never the root: a .glb is re-imported on every
        # change and anything written into it is lost. Everything durable —
        # script, collider, hurtbox — hangs off this body instead.
        '[node name="Model" parent="." instance=ExtResource("1_model")]',
        _transform3d(((1, 0, 0), (0, 1, 0), (0, 0, 1)), offset).replace(
            "Transform3D", "transform = Transform3D"),
        "",
    ]
    if with_capsule:
        lines += [
            '[node name="CollisionShape3D" type="CollisionShape3D" parent="."]',
            'shape = SubResource("CapsuleShape3D_body")',
            "",
        ]
    if with_camera:
        lines += [
            '[node name="Camera3D" type="Camera3D" parent="."]',
            _transform3d(((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                         (0.0, min(camera_height, height * 0.5 - 0.05), 0.0)
                         ).replace("Transform3D", "transform = Transform3D"),
            # Never `current`, even when asked for. Godot makes the first camera
            # into the tree current, so an instanced character used to decide the
            # view for a whole level by accident of node order — that is what put
            # the boot frame inside the pirate's head. The scene that owns the
            # view has to say so, and this one never is that scene.
            "current = false",
            "",
        ]
    return "\n".join(lines)


# Which ext_resource in an existing .tscn is "the model". id="1_model" is what
# character_scene_text writes, but a human who reorganised the scene may have
# renumbered it, so the extension is the fallback identifier.
_MODEL_SUFFIXES = (".glb", ".gltf", ".fbx", ".obj", ".dae", ".escn", ".blend")
_EXT_RESOURCE_LINE = re.compile(r'^\[ext_resource\b[^\]]*\]$', re.M)


def _rewire_model_ext_resource(text: str, model_res: str,
                               model_uid: str = "") -> tuple[str, bool]:
    """Repoint an existing character .tscn at a re-imported model, nothing else.

    Returns (text, rewired). `rewired` is False when the scene has no model
    ext_resource to repoint — the caller has to say so rather than pretend the
    delivery landed, because the visible symptom is the OLD mesh in the game.
    """
    def is_model(line: str) -> bool:
        if 'id="1_model"' in line:
            return True
        path = re.search(r'path="([^"]+)"', line)
        return bool(path) and path.group(1).lower().endswith(_MODEL_SUFFIXES)

    rewired = False

    def replace(match: re.Match) -> str:
        nonlocal rewired
        line = match.group(0)
        if rewired or not is_model(line):
            return line
        rewired = True
        # `(?<![\w])` and not a bare `id="`: uid="uid://..." CONTAINS id=", and
        # matching it renamed the resource to its own uid — caught by the test
        # that asserts the old uid is gone.
        res_id = re.search(r'(?<![\w])id="([^"]+)"', line)
        return ('[ext_resource type="PackedScene" '
                + (f'uid="{model_uid}" ' if model_uid else "")
                + f'path="{model_res}" '
                + f'id="{res_id.group(1) if res_id else "1_model"}"]')

    return _EXT_RESOURCE_LINE.sub(replace, text), rewired


def preview_scene_text(character_res: str, *, longest_axis: float,
                       character_uid: str = "", floor_y: float = 0.0) -> str:
    """A lit stage that frames the character — what the screenshot photographs.

    Deliberately its own scene rather than the game's main scene: the point is to
    see THIS asset, under Godot's renderer, at a framing that does not depend on
    where the level designer happened to put a camera.

    The FLOOR is not decoration. The generated character is a CharacterBody3D
    running the template's player script, which applies gravity from its first
    physics frame — MEASURED: with no floor, the subject had fallen ~10 m out of
    frame by the 1.2 s capture and the screenshot came back as an empty
    background that looked exactly like a failed import. Standing on a floor is
    also the only cheap proof that the collider is real.
    """
    reach = max(float(longest_axis), 0.1)
    # -Z, because that is the side a character's FACE is on. Blender bases face
    # +Y and the glTF exporter maps Blender +Y to glTF -Z, so an eye on +Z
    # photographs the back of the head — MEASURED: the same base rotated 180
    # degrees delivered a screenshot of the front from the old +Z eye. A
    # turnaround catches a bad model; a preview that only ever shows the back
    # is the thing that lets one through.
    # AIM AT THE ORIGIN, and do not "improve" this to the subject's mid-height:
    # character_scene_text already recentres the model onto the body origin
    # (MEASURED: `transform` origin (0, -0.9, 0.0365) for a 1.8 m figure), so
    # the origin IS the middle. Aiming at reach*0.5 aims at the top of the
    # head and drops the legs out of frame — done, photographed, reverted.
    aim = (0.0, 0.0, 0.0)
    # Distance is derived, not guessed: a 40 degree vertical lens sees
    # 2*d*tan(20) of height, so d ~= 2.0*reach frames the figure at about two
    # thirds with margin top and bottom. The eye stays near the subject's own
    # centre height — a raised camera tilting down spends vertical frame on
    # near floor, and the feet are where a scale or float error shows first.
    eye = (reach * 0.6, reach * 0.12, reach * -2.0)
    basis = _look_at_basis(eye, aim)
    sun = _look_at_basis((2.0, 3.0, 2.5), (0.0, 0.0, 0.0))
    ext = ('[ext_resource type="PackedScene" '
           + (f'uid="{character_uid}" ' if character_uid else "")
           + f'path="{character_res}" id="1_char"]')
    span = reach * 10.0
    slab = reach * 0.5
    return "\n".join([
        "[gd_scene load_steps=5 format=3]",
        "",
        ext,
        "",
        '[sub_resource type="Environment" id="Environment_preview"]',
        "background_mode = 1",
        "background_color = Color(0.13, 0.14, 0.17, 1)",
        # Ambient from a COLOUR, not from a sky: without it a single-light
        # render leaves every surface facing away from the sun pure black, and
        # the screenshot reads as a broken import.
        "ambient_light_source = 2",
        "ambient_light_color = Color(0.55, 0.58, 0.68, 1)",
        "ambient_light_energy = 0.55",
        "",
        '[sub_resource type="BoxShape3D" id="BoxShape3D_floor"]',
        f"size = Vector3({span:.4f}, {slab:.4f}, {span:.4f})",
        "",
        '[sub_resource type="BoxMesh" id="BoxMesh_floor"]',
        f"size = Vector3({span:.4f}, {slab:.4f}, {span:.4f})",
        "",
        '[node name="Preview" type="Node3D"]',
        "",
        '[node name="WorldEnvironment" type="WorldEnvironment" parent="."]',
        'environment = SubResource("Environment_preview")',
        "",
        '[node name="Key" type="DirectionalLight3D" parent="."]',
        _transform3d(sun, (0.0, reach * 2.0, 0.0)).replace(
            "Transform3D", "transform = Transform3D"),
        "light_energy = 1.2",
        "shadow_enabled = true",
        "",
        '[node name="Floor" type="StaticBody3D" parent="."]',
        _transform3d(((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                     (0.0, float(floor_y) - slab * 0.5, 0.0)).replace(
            "Transform3D", "transform = Transform3D"),
        "",
        '[node name="FloorShape" type="CollisionShape3D" parent="Floor"]',
        'shape = SubResource("BoxShape3D_floor")',
        "",
        '[node name="FloorMesh" type="MeshInstance3D" parent="Floor"]',
        'mesh = SubResource("BoxMesh_floor")',
        "",
        '[node name="Subject" parent="." instance=ExtResource("1_char")]',
        "",
        '[node name="PreviewCamera" type="Camera3D" parent="."]',
        _transform3d(basis, eye).replace("Transform3D", "transform = Transform3D"),
        # Godot's default 75 degrees is a room-scale lens. MEASURED: at the
        # framing distance it shows 5 m of vertical, so a 1.8 m character can
        # cover at most a third of the picture no matter where the eye stands.
        # 40 degrees is a portrait lens and fills the frame with the subject.
        "fov = 40.0",
        # MEASURED, and the reason the first frame off this path was a
        # full-screen blur: when the character scene still shipped its own
        # first-person Camera3D, Godot made the FIRST camera to enter the tree
        # current, and the subject is instanced before this node — so the
        # screenshot was taken from INSIDE the character's head, looking at the
        # back of its own mesh. The character no longer carries a camera by
        # default, but this stays: with_camera=True and any camera a human adds
        # to the subject would both take the frame back off us silently.
        "current = true",
        f"near = {max(reach * 0.01, 0.01):.4f}",
        f"far = {max(reach * 40.0, 100.0):.1f}",
        "",
    ])


def inspect_resource(project_dir: str, res_path: str, timeout: int = 180, *,
                     min_size_m: float = 0.05, max_size_m: float = 50.0,
                     nominal_size_m: float = 1.8) -> dict:
    """Load a resource IN THE ENGINE and report what it actually became.

    Reports meshes (with GLOBAL-transform bounds), skeletons, animation players
    and their clips, bone attachments, blend shapes, colliders, and which
    material surfaces carry an albedo texture — plus a real-world size check,
    because glTF is metres and Godot imports a 180 m character in silence.

    min/max/nominal_size_m tune that check: the defaults suit a humanoid. A
    tabletop prop set wants a smaller min, a vehicle a larger max.
    """
    import json
    import tempfile

    exe = find_godot()
    tmp = Path(tempfile.mkdtemp(prefix="bgate_inspect_"))  # cleaned in the finally
    try:
        script = tmp / "inspect.gd"
        script.write_text(_INSPECT_GD, encoding="utf-8")

        env = {**os.environ, "BGATE_INSPECT": res_path,
               "BGATE_SIZE_MIN": str(min_size_m),
               "BGATE_SIZE_MAX": str(max_size_m),
               "BGATE_SIZE_NOMINAL": str(nominal_size_m)}
        cmd = [exe, "--headless", "--path", str(project_dir),
               "--script", str(script)]
        try:
            proc = subprocess.run(cmd, capture_output=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL,
                                  env=env, creationflags=_NO_WINDOW, **_TEXT)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"inspect timed out after {timeout}s"}

        output = proc.stdout or ""
        if "BGATE_JSON_START" not in output:
            return {"ok": False, "error": "inspector produced no result",
                    "stdout": output[-1500:], "stderr": (proc.stderr or "")[-800:]}
        blob = output.split("BGATE_JSON_START", 1)[1].split(
            "BGATE_JSON_END", 1)[0].strip()
        try:
            return json.loads(blob)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"unreadable inspector output: {exc}",
                    "raw": blob[:500]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Injection — running the user's game with an autoload it does not contain
# ---------------------------------------------------------------------------
#
# screenshot() and evidence() both work by writing an `override.cfg` and a
# dotfile .gd into the user's PROJECT ROOT, running the game, and deleting them
# in a `finally`. That covers an exception and a timeout. It does not cover the
# MCP server being killed mid-run — and then an autoload stays wired into the
# user's project permanently, every later capture refuses with "override.cfg
# already exists", and nothing in the message says the file is ours or that
# deleting it is safe.
#
# So both files carry this marker on their first line. A leftover we can prove
# is ours gets cleaned and reported; anything else is still refused, because a
# project may legitimately have its own override.cfg and clobbering it would be
# the same class of bug pointed the other way.
_INJECT_MARK = "BGATE-INJECTED"
_INJECT_BANNER = (
    f"# {_INJECT_MARK} — written by Builders Gate for one capture and deleted\n"
    "# straight afterwards. If you are reading this in your project, a capture\n"
    "# was killed mid-run: this file and override.cfg are safe to delete.\n")

# THE MOUSE. Neither capture passes --headless, because on 4.7.1 --headless
# selects the dummy rendering driver and `get_viewport().get_texture()` returns
# null — MEASURED, the run dies with `Parameter "t" is null` at
# dummy/storage/texture_storage.h:110 and no PNG is written at all. So a capture
# has to run the game for real, and templates/3d's player.gd (like every FPS
# controller) grabs the mouse in _ready because DisplayServer is not "headless".
# The user's pointer then belongs to the game for up to the whole timeout.
#
# Releasing it from _process is what works: autoloads enter the tree before the
# main scene, so an autoload _ready loses to the player's _ready, but _process
# runs every frame afterwards and wins. VERIFIED headlessly with a proxy value
# (headless ignores mouse_mode itself): 75 frames, autoload holds the value.
# The cost is that a captured-mouse look control stops responding during a
# capture, which for a still frame is not a cost.
_RELEASE_MOUSE_GD = """
func _process(_delta: float) -> void:
	if Input.mouse_mode != Input.MOUSE_MODE_VISIBLE:
		Input.mouse_mode = Input.MOUSE_MODE_VISIBLE
"""

# Injected autoload that screenshots the RUNNING game. Uses env for its
# parameters so nothing project-side needs editing.
_SHOT_GD = _INJECT_BANNER + """
extends Node

func _ready() -> void:
	var at := float(OS.get_environment("BGATE_SHOT_AT"))
	get_tree().create_timer(maxf(at, 0.1)).timeout.connect(_shoot)
""" + _RELEASE_MOUSE_GD + """
func _shoot() -> void:
	var img := get_viewport().get_texture().get_image()
	img.save_png(OS.get_environment("BGATE_SHOT_PATH"))
	print("BGATE_SHOT_SAVED")
	get_tree().quit()
"""


def clear_injection(project_dir: str) -> dict:
    """Remove a capture injection left behind by a killed run. Ours only.

    Returns {ok, removed, blocked}. `blocked` names an override.cfg that is NOT
    ours — a project's own override.cfg is a legitimate thing to have, and this
    function existing is not a licence to delete it.
    """
    project = Path(project_dir)
    removed: list[str] = []
    blocked: list[str] = []

    override = project / "override.cfg"
    if override.exists():
        try:
            text = override.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if "BGateShot" in text or "BGateEvidence" in text:
            try:
                override.unlink()
                removed.append(override.name)
            except OSError:
                blocked.append(override.name)
        else:
            blocked.append(override.name)

    for name in (".bgate_shot.gd", ".bgate_evidence.gd"):
        script = project / name
        if not script.exists():
            continue
        try:
            head = script.read_text(encoding="utf-8", errors="replace")[:400]
        except OSError:
            head = ""
        if _INJECT_MARK not in head:
            blocked.append(name)
            continue
        for path in (script, project / (name + ".uid")):
            try:
                path.unlink(missing_ok=True)
                if path.name == name:
                    removed.append(name)
            except OSError:
                blocked.append(path.name)

    return {"ok": not blocked, "removed": removed, "blocked": blocked}


def _begin_injection(project: Path, script_name: str, script_body: str,
                     autoload: str) -> dict:
    """Write the override.cfg + autoload pair, cleaning our own leftovers.

    Returns {} on success, or {"error": ...} to be handed straight back. The old
    code guarded override.cfg against clobbering and NOT the .gd beside it, so a
    project that happened to own a `.bgate_shot.gd` had it silently replaced and
    then deleted.
    """
    recovered = clear_injection(str(project))
    if recovered["blocked"]:
        return {"error": "refusing to clobber "
                         + ", ".join(sorted(set(recovered["blocked"])))
                         + f" in {project} — these are not ours; remove or "
                           "rename them first",
                "blocked": recovered["blocked"]}

    (project / script_name).write_text(script_body, encoding="utf-8")
    (project / "override.cfg").write_text(
        f'[autoload]\n{autoload}="*res://{script_name}"\n', encoding="utf-8")
    return {"recovered": recovered["removed"]}


def _end_injection(project: Path, script_name: str) -> None:
    """Never leave the injection behind — a stray override.cfg silently changes
    how the user's project runs forever after."""
    for name in ("override.cfg", script_name, script_name + ".uid"):
        try:
            (project / name).unlink(missing_ok=True)
        except OSError:
            pass


def screenshot(project_dir: str, out_path: str, *, at: float = 1.0,
               scene: Optional[str] = None, timeout: int = 120) -> dict:
    """Run the ACTUAL game briefly and capture the viewport to a PNG.

    This is the 2D feedback loop: headless checks prove the game boots, but an
    agent iterating on look has to SEE the running frame. Needs a GPU/display,
    so a game window appears for ~`at`+1 seconds — the cost of a real frame.

    Mechanism: Godot auto-reads `override.cfg` next to project.godot, and
    autoloads are just settings — so we inject a screenshot autoload there,
    run, and remove it. The project's own files are never modified. A leftover
    from a run that was KILLED (not merely failed — the finally covers that) is
    recognised as ours, cleaned, and reported in `recovered`; anything of the
    user's with those names is refused rather than clobbered.
    """
    project = Path(project_dir)
    if not (project / "project.godot").exists():
        return {"ok": False, "error": f"no project.godot in {project_dir}"}

    started = _begin_injection(project, ".bgate_shot.gd", _SHOT_GD, "BGateShot")
    if started.get("error"):
        return {"ok": False, **started}

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        cmd = [find_godot(), "--path", str(project),
               "--resolution", "1280x720"]
        if scene:
            cmd.append(scene)
        env = {**os.environ, "BGATE_SHOT_PATH": str(out.resolve()),
               "BGATE_SHOT_AT": str(at)}
        try:
            proc = subprocess.run(cmd, capture_output=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL,
                                  env=env, creationflags=_NO_WINDOW, **_TEXT)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"game did not exit within {timeout}s — "
                                          "the shot autoload should quit after capture"}

        output = (proc.stdout or "") + (proc.stderr or "")
        if not out.exists():
            return {"ok": False, "error": "no screenshot produced",
                    "exit_code": proc.returncode,
                    "saved_marker": "BGATE_SHOT_SAVED" in output,
                    "recovered": started.get("recovered") or [],
                    "output": output[-1500:], "errors": _errors(output)}
        return {"ok": True, "path": str(out), "bytes": out.stat().st_size,
                "at": at, "recovered": started.get("recovered") or [],
                "errors": _errors(output)}
    finally:
        _end_injection(project, ".bgate_shot.gd")


# Structured visual evidence — DESIGN.md §9, on the shipped screenshot path.
#
# A beauty frame says what the game LOOKS like. It cannot answer "is the health
# bar showing the same number the fighter actually has", "did the hitbox line up
# with the sprite", or "is that entity on screen at all or just off the edge" —
# and those are the questions an art or QA agent is actually asking when it asks
# for a screenshot. This walks the live tree at capture time and reports what IS
# WHERE, in screen pixels, beside the frame.
#
# Deviation from schemas/evidence_manifest.schema.json, stated rather than
# hidden: that schema requires `sim` and `tick`, which are engine concepts that
# do not exist on this path — there is no simulation id and no tick, only a
# wall-clock capture time. `captured_at_s` stands in for `tick`. Everything else
# (frame, buffers, entities.screen_bounds/visible, ui.screen_bounds/value)
# matches, so a manifest from here is readable by anything written against the
# schema's entity/ui shape.
_EVIDENCE_GD = _INJECT_BANNER + """
extends Node

var _entities := {}
var _ui := {}
var _overlay: CanvasLayer = null
var _boxes: Array = []
var _include_hidden := false

func _ready() -> void:
	_include_hidden = OS.get_environment("BGATE_EV_HIDDEN") == "1"
	var at := float(OS.get_environment("BGATE_EV_AT"))
	get_tree().create_timer(maxf(at, 0.1)).timeout.connect(_capture)

func _process(_delta: float) -> void:
	# See _RELEASE_MOUSE_GD: a capture runs the REAL game, and an FPS controller
	# grabs the pointer in its own _ready. _process runs after that and wins.
	if Input.mouse_mode != Input.MOUSE_MODE_VISIBLE:
		Input.mouse_mode = Input.MOUSE_MODE_VISIBLE

func _capture() -> void:
	# frame_post_draw is the only point the viewport texture is guaranteed to
	# hold the frame that was just presented. A bare get_image() here races the
	# renderer and intermittently returns the PREVIOUS frame — which looks like
	# a one-frame-late screenshot and is maddening to debug.
	await RenderingServer.frame_post_draw
	var beauty := get_viewport().get_texture().get_image()
	beauty.save_png(OS.get_environment("BGATE_EV_BEAUTY"))

	_scan(get_tree().root, 0)

	var overlay_path := OS.get_environment("BGATE_EV_OVERLAY")
	if overlay_path != "":
		_build_overlay()
		await RenderingServer.frame_post_draw
		await RenderingServer.frame_post_draw
		var ov := get_viewport().get_texture().get_image()
		ov.save_png(overlay_path)

	_write_manifest()
	print("BGATE_EV_DONE")
	get_tree().quit()

func _shape_rect(shape: Shape2D) -> Rect2:
	if shape is RectangleShape2D:
		var s: Vector2 = (shape as RectangleShape2D).size
		return Rect2(-s * 0.5, s)
	if shape is CircleShape2D:
		var r: float = (shape as CircleShape2D).radius
		return Rect2(Vector2(-r, -r), Vector2(r * 2.0, r * 2.0))
	if shape is CapsuleShape2D:
		var c := shape as CapsuleShape2D
		return Rect2(Vector2(-c.radius, -c.height * 0.5),
			Vector2(c.radius * 2.0, c.height))
	return Rect2()

## Screen-space rect, or null when the node has no measurable extent.
## Controls are already in screen space. Node2D-family nodes are in canvas
## space, so they need get_global_transform_with_canvas() -- plain
## global_transform ignores the camera and reports world coordinates that look
## like screen coordinates, which is the subtle wrong answer.
func _rect_of(node: Node) -> Variant:
	if node is Control:
		return (node as Control).get_global_rect()
	if node is CollisionShape2D:
		var cs := node as CollisionShape2D
		if cs.shape == null:
			return null
		return cs.get_global_transform_with_canvas() * _shape_rect(cs.shape)
	# AnimatedSprite2D has NO get_rect() in Godot 4 -- Sprite2D does, and the
	# asymmetry is easy to miss. Without this branch the FIGHTERS themselves are
	# absent from the manifest (they are AnimatedSprite2D), which leaves the
	# evidence describing the HUD in detail and the actual game not at all.
	# Measured on haymaker: 27 entities, zero of them a combatant.
	if node is AnimatedSprite2D:
		var a := node as AnimatedSprite2D
		var frames := a.sprite_frames
		if frames == null or not frames.has_animation(a.animation):
			return null
		var tex := frames.get_frame_texture(a.animation, a.frame)
		if tex == null:
			return null
		var size := Vector2(tex.get_size())
		var origin := a.offset - (size * 0.5 if a.centered else Vector2.ZERO)
		return a.get_global_transform_with_canvas() * Rect2(origin, size)
	if node is CanvasItem and node.has_method("get_rect"):
		var ci := node as CanvasItem
		return ci.get_global_transform_with_canvas() * (node.get_rect() as Rect2)
	return null

func _value_of(node: Node) -> Variant:
	if node is Range:
		var r := node as Range
		return {"value": r.value, "max": r.max_value}
	if node is Label:
		return {"text": (node as Label).text}
	if node is RichTextLabel:
		return {"text": (node as RichTextLabel).get_parsed_text()}
	return null

## Unique manifest key. Keying by node.name alone silently OVERWRITES on a
## collision -- two nodes called "Health" (one per fighter, the obvious case)
## would leave one of them missing from the evidence with no error anywhere.
## Names are kept when unique because `{"PlayerHealth": 92}` is what a caller
## wants to write; the full path is the tiebreaker, and is always in `path`.
func _key(node: Node, bag: Dictionary) -> String:
	var name := String(node.name)
	if not bag.has(name):
		return name
	return String(get_tree().root.get_path_to(node))

func _scan(node: Node, depth: int) -> void:
	if depth > 24 or _entities.size() + _ui.size() > 400:
		return
	# Never report our own injected overlay as game content.
	if node == _overlay or node == self:
		return

	var vis: bool = node is CanvasItem and (node as CanvasItem).is_visible_in_tree()

	# A hidden subtree is skipped WHOLE, children included. Two reasons, and the
	# second is the one that bites: a hidden Control has never been laid out, so
	# get_global_rect() returns pre-layout values -- Commodity Brawler's closed
	# F1 tuning panel reports ~90 labels all claiming identical bounds. That is
	# not evidence, it is noise that outnumbers the real content 30:1. Set
	# BGATE_EV_HIDDEN=1 to keep them (for debugging a panel that should be up).
	if node is CanvasItem and not vis and not _include_hidden:
		return

	var rect: Variant = _rect_of(node)
	if rect != null:
		var r: Rect2 = rect
		var bounds := [snappedf(r.position.x, 0.01), snappedf(r.position.y, 0.01),
			snappedf(r.position.x + r.size.x, 0.01),
			snappedf(r.position.y + r.size.y, 0.01)]
		var entry := {
			"screen_bounds": bounds,
			"visible": vis,
			"class": node.get_class(),
			"path": String(get_tree().root.get_path_to(node)),
		}
		if node is CanvasItem:
			entry["z"] = (node as CanvasItem).z_index
		var value: Variant = _value_of(node)
		if value != null:
			entry["value"] = value
			_ui[_key(node, _ui)] = entry
		else:
			if node is CollisionShape2D:
				entry["collision"] = true
			_entities[_key(node, _entities)] = entry
			_boxes.append({"r": r, "vis": vis,
				"col": node is CollisionShape2D})
	for child in node.get_children():
		_scan(child, depth + 1)

func _build_overlay() -> void:
	_overlay = CanvasLayer.new()
	_overlay.layer = 128
	var drawer := _Drawer.new()
	drawer.boxes = _boxes
	drawer.set_anchors_preset(Control.PRESET_FULL_RECT)
	drawer.mouse_filter = Control.MOUSE_FILTER_IGNORE
	_overlay.add_child(drawer)
	get_tree().root.add_child(_overlay)

func _write_manifest() -> void:
	var buffers := ["beauty"]
	if OS.get_environment("BGATE_EV_OVERLAY") != "":
		buffers.append("collision")
		buffers.append("ui_layout")
	# MEASURED, not assumed: bounds come back in VIEWPORT space (the game's own
	# stage — 640x360 for Commodity Brawler), while the PNG is saved at WINDOW
	# resolution (1280x720). Anything drawing manifest bounds onto the frame
	# without this factor is wrong by exactly `scale`, and wrong in a way that
	# looks like a physics bug rather than a units bug. Both sizes ship.
	var vp := get_viewport().get_visible_rect().size
	var win := Vector2(DisplayServer.window_get_size())
	var manifest := {
		"frame": OS.get_environment("BGATE_EV_BEAUTY"),
		"captured_at_s": float(OS.get_environment("BGATE_EV_AT")),
		"buffers": buffers,
		"viewport": [vp.x, vp.y],
		"window": [win.x, win.y],
		"scale": [win.x / maxf(vp.x, 1.0), win.y / maxf(vp.y, 1.0)],
		"bounds_space": "viewport",
		"scene": (get_tree().current_scene.name
			if get_tree().current_scene else ""),
		"entities": _entities,
		"ui": _ui,
	}
	var f := FileAccess.open(OS.get_environment("BGATE_EV_MANIFEST"),
		FileAccess.WRITE)
	if f != null:
		f.store_string(JSON.stringify(manifest, "  "))
		f.close()

class _Drawer extends Control:
	var boxes: Array = []
	func _draw() -> void:
		for b in boxes:
			var r: Rect2 = b["r"]
			# Colour carries meaning: collision shapes are the thing you are
			# usually checking alignment against, so they get the hot colour.
			# (A game that resolves hits by STATE rather than by physics shape
			# — as Commodity Brawler does — simply has no red boxes to draw.)
			var c := Color(1, 0.2, 0.3, 1.0) if b["col"] else Color(0.1, 1, 1, 0.95)
			if not b["vis"]:
				c.a = 0.3
			# 2px, and drawn over a stage that is upscaled 2x to the window, so
			# the stroke survives the scale. At 1px it was legible only on a
			# flat background and vanished over the market-stall art.
			draw_rect(r, c, false, 2.0)
"""


def evidence(project_dir: str, out_dir: str, *, at: float = 1.0,
             scene: Optional[str] = None, overlay: bool = True,
             include_hidden: bool = False, timeout: int = 120) -> dict:
    """Capture a beauty frame PLUS a screen-space manifest of what is where.

    DESIGN.md §9 without the engine: same injection mechanism as screenshot(),
    but the autoload also walks the live tree and reports every measurable node
    as screen-pixel bounds, visibility, and — for Range/Label nodes — its
    runtime VALUE. That last part is what lets a QA agent assert the health bar
    matches the fighter's hp instead of eyeballing a PNG.

    Returns {ok, beauty, overlay, manifest, entities, ui, counts}.
    """
    import time

    project = Path(project_dir)
    if not (project / "project.godot").exists():
        return {"ok": False, "error": f"no project.godot in {project_dir}"}

    injected = _begin_injection(project, ".bgate_evidence.gd", _EVIDENCE_GD,
                                "BGateEvidence")
    if injected.get("error"):
        return {"ok": False, **injected}

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    beauty_path = out / "beauty.png"
    overlay_path = out / "overlay.png" if overlay else None
    manifest_path = out / "manifest.json"

    started = time.monotonic()
    try:
        cmd = [find_godot(), "--path", str(project), "--resolution", "1280x720"]
        if scene:
            cmd.append(scene)
        env = {
            **os.environ,
            "BGATE_EV_AT": str(at),
            "BGATE_EV_BEAUTY": str(beauty_path.resolve()),
            "BGATE_EV_OVERLAY": str(overlay_path.resolve()) if overlay_path else "",
            "BGATE_EV_MANIFEST": str(manifest_path.resolve()),
            "BGATE_EV_HIDDEN": "1" if include_hidden else "",
        }
        try:
            proc = subprocess.run(cmd, capture_output=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL,
                                  env=env, creationflags=_NO_WINDOW, **_TEXT)
        except subprocess.TimeoutExpired:
            return {"ok": False,
                    "error": f"game did not exit within {timeout}s — "
                             "the evidence autoload should quit after capture"}

        output = (proc.stdout or "") + (proc.stderr or "")
        if not manifest_path.exists():
            return {"ok": False, "error": "no evidence manifest produced",
                    "exit_code": proc.returncode,
                    "done_marker": "BGATE_EV_DONE" in output,
                    "beauty_written": beauty_path.exists(),
                    "recovered": injected.get("recovered") or [],
                    "output": output[-1500:], "errors": _errors(output)}

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"unreadable manifest: {exc}"}

        return {
            "ok": True,
            "beauty": str(beauty_path) if beauty_path.exists() else None,
            "overlay": (str(overlay_path)
                        if overlay_path and overlay_path.exists() else None),
            "manifest": str(manifest_path),
            "scene": manifest.get("scene", ""),
            "viewport": manifest.get("viewport"),
            "window": manifest.get("window"),
            # Surfaced at the top level because a caller drawing these bounds
            # onto the beauty PNG needs the factor before it needs anything else.
            "scale": manifest.get("scale"),
            "bounds_space": manifest.get("bounds_space"),
            "buffers": manifest.get("buffers", []),
            "entities": manifest.get("entities", {}),
            "ui": manifest.get("ui", {}),
            "counts": {"entities": len(manifest.get("entities", {})),
                       "ui": len(manifest.get("ui", {}))},
            "seconds": round(time.monotonic() - started, 2),
            # Non-empty means a previous capture was killed and left its
            # autoload wired into this project until now.
            "recovered": injected.get("recovered") or [],
            "errors": _errors(output),
        }
    finally:
        _end_injection(project, ".bgate_evidence.gd")


# ---------------------------------------------------------------------------
# Retarget acceptance: does the engine agree this is a humanoid?
# ---------------------------------------------------------------------------
#
# WHY THE ENGINE HAS TO BE ASKED, AND WHY THE BLENDER SIDE CANNOT ANSWER IT. The
# rig this pipeline builds carries Godot's own SkeletonProfileHumanoid bone names
# — Hips, Spine, LeftUpperArm — and the entire value of that choice is that a
# BoneMap then lets any humanoid clip in the world play on the character. That
# claim was never once tested. Blender will happily export 23 correctly-named
# bones in a FLAT hierarchy, or with the arm chain parented to the hips, or with
# a bone the profile does not know; blender_rig reports 0 unweighted for all of
# them, and the character is unanimatable by anything but a clip authored for it
# specifically, which is the opposite of the point.
#
# Three questions, and they fail independently:
#
#   COVERAGE   is every profile bone present, under the exact name? A missing
#              UpperChest is survivable; a missing Hips is not.
#   HIERARCHY  does rotating a shoulder move the hand? A flat skeleton passes
#              coverage perfectly and propagates nothing, so this is the one
#              check that catches an export that lost its parenting.
#   BINDING    does a clip authored against the profile actually drive it? Built
#              here procedurally rather than shipped as an asset, because a
#              downloaded clip introduces a licence and a download to a step
#              whose entire job is to be a fast, offline yes/no.
#
# And it writes the BoneMap, so the answer is not just "yes" but a resource the
# user's project can immediately point a real animation library at.
RETARGET_MARK = "BGATE_RETARGET:"

_RETARGET_GD = '''extends SceneTree

# A SceneTree SCRIPT DRIVEN ACROSS FRAMES, AND THAT IS NOT A STYLE CHOICE.
# Skeleton3D recomputes its GLOBAL bone transforms on its own notification, not
# inside the call that set a pose. MEASURED here on Godot 4.7: setting
# LeftUpperArm's pose rotation and then calling force_update_all_bone_transforms
# left LeftHand's global pose at exactly its rest origin, and the first version
# of this probe therefore reported a correctly-parented skeleton as unparented —
# 23 bones, every parent index right, "propagates": false. The local pose had
# changed; the global one had not been recomputed yet. So every measurement here
# is taken a FRAME AFTER the pose that caused it.

const MARK := "__MARK__"

var out := {}
var skel: Skeleton3D = null
var stage := 0
var pairs := [["LeftUpperArm", "LeftHand"], ["RightUpperLeg", "RightFoot"]]
var chain := []
var pending := {}
var player: AnimationPlayer = null
var driver := "LeftUpperArm"
var rest_q := Quaternion()
var done := false

func _find_skeleton(node) -> Skeleton3D:
    if node is Skeleton3D:
        return node
    for child in node.get_children():
        var found: Skeleton3D = _find_skeleton(child)
        if found != null:
            return found
    return null

func _say() -> void:
    print(MARK + JSON.stringify(out))
    done = true
    quit()

func _initialize() -> void:
    var res_path := "__RES__"
    var map_path := "__MAP__"
    out = {"ok": false, "res": res_path}

    if not ResourceLoader.exists(res_path):
        out["error"] = "no resource at %s - import the .glb first (godot_import_asset)" % res_path
        _say()
        return
    var packed = ResourceLoader.load(res_path)
    if packed == null or not (packed is PackedScene):
        out["error"] = "%s did not load as a PackedScene" % res_path
        _say()
        return
    var root = packed.instantiate()
    if root == null:
        out["error"] = "could not instantiate %s" % res_path
        _say()
        return
    get_root().add_child(root)

    skel = _find_skeleton(root)
    if skel == null:
        out["error"] = "no Skeleton3D anywhere under %s - this model is not rigged as far as the engine is concerned" % res_path
        _say()
        return

    var profile := SkeletonProfileHumanoid.new()
    var missing := []
    var mapped := []
    for i in range(profile.bone_size):
        var wanted: String = profile.get_bone_name(i)
        if skel.find_bone(wanted) == -1:
            missing.append(wanted)
        else:
            mapped.append(wanted)
    var known := {}
    for m in mapped:
        known[m] = true
    var extra := []
    for b in range(skel.get_bone_count()):
        var bone_name: String = skel.get_bone_name(b)
        if not known.has(bone_name):
            extra.append(bone_name)

    out["skeleton"] = skel.name
    out["skeleton_bones"] = skel.get_bone_count()
    out["profile_bones"] = profile.bone_size
    out["mapped"] = mapped.size()
    out["missing"] = missing
    out["extra"] = extra

    # THE PROFILE IS 56 BONES AND NOTHING IN THIS PIPELINE HAS FINGERS. Judging
    # against the full profile would fail every character this product makes for
    # want of a LeftLittleDistal. What retargeting actually needs is the trunk
    # and the four limbs; fingers, eyes and jaw are refinements a clip leaves
    # alone. `missing` still lists them, because a project that DOES want finger
    # animation needs to know they are not there.
    var essential := ["Hips", "Spine", "Head", "LeftUpperArm", "LeftLowerArm",
                      "LeftHand", "RightUpperArm", "RightLowerArm", "RightHand",
                      "LeftUpperLeg", "LeftLowerLeg", "LeftFoot",
                      "RightUpperLeg", "RightLowerLeg", "RightFoot"]
    var essential_missing := []
    for e in essential:
        if skel.find_bone(e) == -1:
            essential_missing.append(e)
    out["essential_missing"] = essential_missing

    # THE CLIP IS BUILT HERE rather than downloaded, so this step stays offline
    # and licence-free. The NodePath is the part that fails silently: a bone
    # track is "<node>:<bone>" resolved from root_node, and a path resolving to
    # nothing plays happily and moves zero.
    if skel.find_bone(driver) != -1:
        var host = skel.get_parent()
        if host == null:
            host = skel
        var anim := Animation.new()
        anim.length = 1.0
        var track: int = anim.add_track(Animation.TYPE_ROTATION_3D)
        var node_part := "." if host == skel else str(skel.name)
        anim.track_set_path(track, NodePath("%s:%s" % [node_part, driver]))
        # Keys are ABSOLUTE bone-local rotations, which is also what a real
        # humanoid clip stores. Keying the rest rotation at t=0 and rest*60 at
        # t=1 makes the measured delta mean 60 degrees rather than "60 degrees
        # away from whatever the rest happened to be".
        var driver_rest: Quaternion = skel.get_bone_rest(skel.find_bone(driver)).basis.get_rotation_quaternion()
        anim.rotation_track_insert_key(track, 0.0, driver_rest)
        anim.rotation_track_insert_key(track, 1.0, driver_rest * Quaternion(Vector3(1, 0, 0), deg_to_rad(60.0)))
        var lib := AnimationLibrary.new()
        lib.add_animation("probe", anim)
        player = AnimationPlayer.new()
        host.add_child(player)
        player.add_animation_library("", lib)
        player.root_node = player.get_path_to(host)

    if map_path != "":
        var bmap := BoneMap.new()
        bmap.profile = profile
        for mapped_name in mapped:
            bmap.set_skeleton_bone_name(mapped_name, mapped_name)
        var err := ResourceSaver.save(bmap, map_path)
        out["bone_map"] = {"path": map_path, "error": int(err),
                           "written": err == OK, "entries": mapped.size()}

func _process(_delta: float) -> bool:
    if done or skel == null:
        return true

    # Two frames per pair: one to pose, one to read what the pose did.
    @warning_ignore("integer_division")
    var pair_index := stage / 2
    if pair_index < pairs.size():
        var pair = pairs[pair_index]
        if stage % 2 == 0:
            var record := {"driver": pair[0], "tip": pair[1]}
            var parent_i: int = skel.find_bone(pair[0])
            var tip_i: int = skel.find_bone(pair[1])
            if parent_i == -1 or tip_i == -1:
                record["skipped"] = "bone absent"
                chain.append(record)
                stage += 2
                return false
            skel.reset_bone_poses()
            record["before"] = skel.get_bone_global_pose(tip_i).origin
            # COMPOSED ONTO THE REST ROTATION, NOT SUBSTITUTED FOR IT. A bone
            # pose in Godot is the bone's FULL local transform, so setting it to
            # a bare 45 degrees about X throws the rest orientation away and the
            # limb swings by whatever the difference happens to be. MEASURED on
            # a 1.8 m figure: the right foot travelled 1.53 m for a "45 degree"
            # hip, because the leg's rest rotation is a 180 degree flip and the
            # net turn was 135. The boolean survived that; the number in the
            # report did not, and a number nobody can sanity-check is worse than
            # no number.
            var rest_rot: Quaternion = skel.get_bone_rest(parent_i).basis.get_rotation_quaternion()
            skel.set_bone_pose_rotation(parent_i, rest_rot * Quaternion(Vector3(1, 0, 0), deg_to_rad(45.0)))
            pending = record
            stage += 1
            return false
        var tip_j: int = skel.find_bone(pending["tip"])
        var after: Vector3 = skel.get_bone_global_pose(tip_j).origin
        var moved: float = (pending["before"] as Vector3).distance_to(after)
        pending["moved_m"] = snapped(moved, 0.0001)
        # 1 cm. A hand on a 1.8 m figure swings ~0.2 m for a 45 degree shoulder;
        # under a centimetre means the rotation did not propagate at all.
        pending["propagates"] = moved > 0.01
        pending.erase("before")
        chain.append(pending)
        pending = {}
        skel.reset_bone_poses()
        stage += 1
        return false

    var clip_stage := stage - pairs.size() * 2
    if clip_stage == 0:
        out["chain"] = chain
        if player == null:
            out["clip"] = {"clip_bone": driver, "skipped": "no %s to drive" % driver,
                           "drives": false}
            stage += 2
            return false
        skel.reset_bone_poses()
        rest_q = skel.get_bone_pose_rotation(skel.find_bone(driver))
        player.play("probe")
        player.seek(1.0, true)
        stage += 1
        return false
    if clip_stage == 1:
        var posed_q: Quaternion = skel.get_bone_pose_rotation(skel.find_bone(driver))
        var delta_deg: float = rad_to_deg(rest_q.angle_to(posed_q))
        # The clip asks for 60 degrees. Half of it is a generous floor that still
        # separates "the track drove the bone" from "the track resolved to
        # nothing", which is what a wrong NodePath looks like: silent, zero.
        out["clip"] = {"clip_bone": driver, "rotated_deg": snapped(delta_deg, 0.01),
                       "drives": delta_deg > 30.0}
        stage += 1
        return false

    var chain_ok := true
    for c in chain:
        if c.has("propagates") and not c["propagates"]:
            chain_ok = false
    out["chain_ok"] = chain_ok
    out["retargetable"] = ((out["essential_missing"] as Array).is_empty()
                           and chain_ok
                           and (out["clip"] as Dictionary).get("drives", false))
    out["ok"] = true
    _say()
    return true
'''.replace("__MARK__", RETARGET_MARK)


def retarget_check(project_dir: str, res_path: str, *,
                   bone_map_res: str = "", timeout: int = 180) -> dict:
    """Ask Godot whether this rigged model is a humanoid it can retarget onto.

    `res_path` is a res:// path to an already-imported model (godot_import_asset
    puts it there). `bone_map_res` is where to save the BoneMap; "" skips it.

    Returns the engine's own answers: bone coverage against
    SkeletonProfileHumanoid, whether rotating a shoulder moves the hand, whether
    a profile-authored clip drives the skeleton, and `retargetable` — the single
    verdict that matters, because a False there means every humanoid animation
    library in existence is unavailable to this character.
    """
    project = Path(project_dir)
    if not (project / "project.godot").exists():
        return {"ok": False, "error": f"no project.godot in {project_dir}"}
    if not str(res_path).startswith("res://"):
        return {"ok": False, "error": f"res_path must be a res:// path, got {res_path!r}"}
    if bone_map_res and not str(bone_map_res).startswith("res://"):
        return {"ok": False,
                "error": f"bone_map_res must be a res:// path, got {bone_map_res!r}"}

    script = (_RETARGET_GD.replace("__RES__", str(res_path))
              .replace("__MAP__", str(bone_map_res)))
    run = run_script(script, project_dir=str(project), timeout=timeout)
    report = {}
    for line in ((run.get("stdout") or "") + "\n" + (run.get("stderr") or "")).splitlines():
        if line.startswith(RETARGET_MARK):
            try:
                report = json.loads(line[len(RETARGET_MARK):])
            except ValueError:
                report = {}
            break
    if not report:
        return {"ok": False,
                "error": run.get("error") or "no report from Godot",
                "exit_code": run.get("exit_code"),
                "output": ((run.get("stdout") or "")[-1200:]),
                "errors": run.get("errors") or []}
    report["seconds"] = run.get("seconds")
    return report


# ---------------------------------------------------------------------------
# The missing last mile: .glb -> imported -> collided -> instanced -> photographed
# ---------------------------------------------------------------------------
#
# Before this, the 3D pipeline declared victory at the .glb. The only "look at
# it" was an EEVEE render in Blender, of a Blender scene, under Blender lights —
# so every defect that happens between the exporter and the player (a rig that
# did not import, a texture that did not come along, a 40x scale, a missing
# collider, a material Godot resolved differently) was invisible BY
# CONSTRUCTION. There was no step at which the engine's opinion was asked.
#
# This is that step, composed from the pieces that already existed rather than
# reinvented: import_asset copies and imports, write_import_settings turns
# collider generation on, inspect_resource reports the engine's own view of the
# tree, and screenshot() runs the real renderer and captures a frame.


def deliver_asset(project_dir: str, glb_path: str, *, name: Optional[str] = None,
                  dest_rel: str = "assets", scene_rel: str = "scenes",
                  script_res: str = "", physics: str = "auto",
                  shape_type: str = "trimesh", body_type: str = "static",
                  character_body: str = "auto",
                  screenshot_dir: Optional[str] = None, at: float = 1.2,
                  min_size_m: float = 0.05, max_size_m: Optional[float] = None,
                  nominal_size_m: float = 1.8, with_camera: bool = False,
                  overwrite_scene: bool = False, timeout: int = 300) -> dict:
    """Take a .glb all the way to a Godot screenshot, and report every step.

    character_body  "auto" reads the body type off what the mesh IS. Skinned
                (it has a skin, so it has a Skeleton3D and joints) →
                CharacterBody3D. Unskinned → StaticBody3D. Pass a class name to
                override — "RigidBody3D" for a prop that should fall and be
                pushed. Every delivered asset used to be wrapped in
                CharacterBody3D regardless, and a crate is not a character: a
                CharacterBody3D only moves when code calls move_and_slide(), so
                a prop delivered that way never simulates at all.

    physics   "auto"  — the importer builds colliders only for the strategy the
                        body needs: mesh shapes inside a StaticBody3D root,
                        nothing at all under a CharacterBody3D or RigidBody3D
                        root, which get the fitted capsule instead. Exactly one
                        collision strategy per asset — a crate used to get an
                        accurate trimesh AND an invisible capsule on a different
                        body, both live, and `has_collider` counting `> 0` was
                        happy with either so it never surfaced.
              "all"   — every mesh, skinned or not. The caller has asked for the
                        mesh shapes; the root capsule stands down.
              "none"  — leave the importer's defaults alone (no mesh shapes), so
                        the root capsule is the collider.

    max_size_m  None picks the bound from what the asset IS: 4 m for anything
                skinned (a character over 4 m across is a unit error, not a
                design choice) and 50 m otherwise, because a vehicle or a
                building is legitimately large. Pass a number to be explicit.

    with_camera  a first-person Camera3D on the body. Off, because an instanced
                 character with a camera can steal the level's view — see
                 character_scene_text.

    overwrite_scene  redelivering an asset does NOT rewrite an existing
                 <name>.tscn; it repoints that scene's model ext_resource at the
                 new import and leaves the node tree alone. Pass True to throw
                 the hand edits away and regenerate from scratch.

    Returns {ok, res_path, scene, preview, screenshot, engine_view, checks,
             steps}. `checks` is the gate: rigged/animated/textured/sized/
             collided, each with the measurement that decided it.
    """
    project = Path(project_dir)
    steps: list[dict] = []

    first = import_asset(project_dir, glb_path, dest_rel=dest_rel, timeout=timeout)
    steps.append({"step": "import", "ok": bool(first.get("ok")),
                  "errors": first.get("import", {}).get("errors", []),
                  # An overwrite is not an error and must not fail the step —
                  # re-importing an asset you just re-exported is the loop. It
                  # does have to be VISIBLE, which it was not.
                  "replaced": first.get("replaced")})
    if not first.get("ok"):
        return {"ok": False, "error": "the engine could not load the asset",
                "detail": first, "steps": steps}

    asset_rel = str(Path(first["copied_to"]).relative_to(project)).replace(
        "\\", "/")
    res_path = first["res_path"]
    view = first["engine_view"]

    # A skin is what makes an asset a character: it implies the Skeleton3D and
    # the joints, and it is the one signal in the engine's view that separates
    # "a person" from "a thing" without asking the caller to say so.
    skinned_asset = any(m.get("skinned") for m in view.get("meshes", []))
    if max_size_m is None:
        max_size_m = 4.0 if skinned_asset else 50.0

    def _look(path: str) -> dict:
        return inspect_resource(project_dir, path, timeout=timeout,
                                min_size_m=min_size_m, max_size_m=max_size_m,
                                nominal_size_m=nominal_size_m)

    # import_asset ran with the generic bounds; re-gate against the ones this
    # asset actually earned.
    view = _look(res_path)

    # --- the body, and therefore the collider -------------------------------
    #
    # These two decisions are ONE decision. Every asset used to be wrapped in a
    # CharacterBody3D and given a capsule, while unskinned meshes ALSO got a
    # trimesh StaticBody3D built inside the imported model — so a crate arrived
    # as a character that cannot move, carrying an accurate collider and an
    # invisible person-shaped one on a different body at the same time.
    root_body = character_body
    if root_body in ("", "auto"):
        root_body = "CharacterBody3D" if skinned_asset else "StaticBody3D"

    physics_nodes: dict = {}
    if physics == "all":
        generate_for = list(view.get("meshes", []))
    elif physics == "auto" and root_body == "StaticBody3D":
        # A skinned mesh is skipped even here: a trimesh StaticBody3D welded to
        # a deforming character is a wall you cannot move, and it does not
        # follow the animation anyway.
        generate_for = [m for m in view.get("meshes", [])
                        if not m.get("skinned")]
    else:
        # CharacterBody3D and RigidBody3D own their own movement, and a
        # StaticBody3D built INSIDE either of them is a second, independent body
        # that does not travel with it. The root's fitted capsule is the whole
        # collider.
        generate_for = []
    for mesh in generate_for:
        physics_nodes[mesh.get("path") or mesh.get("name")] = {
            "shape_type": shape_type, "body_type": body_type}

    settings = {"ok": True, "skipped": "physics=none"}
    if physics_nodes:
        settings = write_import_settings(project_dir, asset_rel,
                                         physics_nodes=physics_nodes)
        steps.append({"step": "import_settings", "ok": bool(settings.get("ok")),
                      "nodes": list(physics_nodes)})
        if settings.get("ok"):
            reimported = check_project(project_dir, timeout=timeout)
            steps.append({"step": "reimport", "ok": reimported["ok"],
                          "errors": reimported.get("errors", [])})
            view = _look(res_path)
    elif physics == "none":
        steps.append({"step": "import_settings", "ok": True,
                      "note": "physics=none, importer defaults kept"})
    else:
        steps.append({"step": "import_settings", "ok": True,
                      "note": f"{root_body} root: the .tscn capsule is the "
                              "collider, so no mesh shapes were generated "
                              "(physics=all to override)"})

    # Exactly one strategy, named in the result so a caller never has to infer
    # it from a collider count that both strategies satisfy.
    mesh_shapes = bool(physics_nodes) and bool(settings.get("ok"))
    collision = "generated_mesh_shapes" if mesh_shapes else "fitted_capsule"

    # --- the playable scene ------------------------------------------------
    stem = name or Path(glb_path).stem
    node_name = "".join(ch for ch in stem.title().replace("_", "").replace("-", "")
                        if ch.isalnum()) or "Asset"

    size = view.get("size_check", {}).get("metres") or [1.0, 1.0, 1.0]
    origin = [0.0, 0.0, 0.0]
    for mesh in view.get("meshes", []):
        pos = mesh.get("aabb_global_position")
        if pos:
            origin = pos
            break
    # Bounds of the WHOLE asset: size_check already merged every mesh's global
    # box, so the lowest corner is the min over the meshes.
    for mesh in view.get("meshes", []):
        pos = mesh.get("aabb_global_position") or origin
        origin = [min(origin[i], pos[i]) for i in range(3)]

    scenes_dir = project / scene_rel
    scenes_dir.mkdir(parents=True, exist_ok=True)
    scene_file = scenes_dir / f"{stem}.tscn"
    model_uid = _import_uid(project_dir, asset_rel)

    # A delivery used to rewrite this file wholesale every single time, so every
    # hand edit — a script attached, a hurtbox added, a transform nudged — was
    # destroyed by the next redelivery. OBSERVED: during one session the same
    # camera node had to be stripped out of the same scene FIVE times, because
    # each iteration on the .glb put it straight back.
    #
    # Repointing the model ext_resource is chosen over "write only when absent"
    # because a redelivery has to remain USEFUL: the whole point of iterating on
    # a .glb is to see the new mesh in the game, and a scene that is skipped
    # entirely keeps showing the old one. Everything else in the file — the node
    # tree, the capsule, the script, whatever the human added — is theirs.
    scene_action = "written"
    if scene_file.exists() and not overwrite_scene:
        existing = scene_file.read_text(encoding="utf-8")
        rewired_text, rewired = _rewire_model_ext_resource(
            existing, res_path, model_uid)
        scene_action = "rewired" if rewired else "left_alone"
        if rewired_text != existing:
            scene_file.write_text(rewired_text, encoding="utf-8")
    else:
        scene_file.write_text(
            character_scene_text(res_path, node_name=node_name, bounds_size=size,
                                 bounds_position=origin, script_res=script_res,
                                 model_uid=model_uid, with_camera=with_camera,
                                 with_capsule=not mesh_shapes,
                                 body_type=root_body),
            encoding="utf-8")
    scene_res = "res://" + str(scene_file.relative_to(project)).replace("\\", "/")

    # The preview is regenerated unconditionally: it is a photo studio built to
    # frame THIS asset's measured bounds, produced alongside the screenshot and
    # thrown away with it. It is output, not a file anyone is meant to edit.
    preview_file = scenes_dir / f"{stem}_preview.tscn"
    preview_file.write_text(
        preview_scene_text(scene_res,
                           longest_axis=view.get("size_check", {}).get(
                               "longest_axis_m", 2.0),
                           # The capsule is centred on the body origin, so its
                           # bottom — where the feet are — is half a height down.
                           floor_y=-float(size[1]) * 0.5),
        encoding="utf-8")
    preview_res = "res://" + str(preview_file.relative_to(project)).replace(
        "\\", "/")

    scene_import = check_project(project_dir, timeout=timeout)
    steps.append({"step": "scenes",
                  # left_alone is not a failure of the import, but it IS the
                  # answer to "why is the old mesh still in the game" — a scene
                  # with no model ext_resource to repoint cannot be updated
                  # without discarding whatever replaced it.
                  "ok": scene_import["ok"] and scene_action != "left_alone",
                  "errors": scene_import.get("errors", []),
                  "scene_action": scene_action,
                  "note": ("the existing scene has no model ext_resource to "
                           "repoint — it was not touched; pass "
                           "overwrite_scene=True to regenerate it"
                           if scene_action == "left_alone" else
                           "existing scene kept, model ext_resource repointed"
                           if scene_action == "rewired" else ""),
                  "scene": scene_res, "preview": preview_res})

    # The generated .tscn read back THROUGH THE ENGINE. A scene file that looks
    # right in a diff and fails to instantiate is the failure mode this catches.
    scene_view = _look(scene_res)
    steps.append({"step": "scene_loads", "ok": bool(scene_view.get("ok")),
                  "colliders": scene_view.get("collider_count", 0)})

    # --- the frame ---------------------------------------------------------
    shot_dir = Path(screenshot_dir or (project / ".bgate_out" / "3d"))
    shot = screenshot(project_dir, str(shot_dir / f"{stem}.png"), at=at,
                      scene=preview_res, timeout=timeout)
    steps.append({"step": "screenshot", "ok": bool(shot.get("ok")),
                  "path": shot.get("path"), "error": shot.get("error")})

    # The capsule is only OURS to be judged on when we wrote the scene this run
    # and the asset is a character. A rewired scene carries whatever collider
    # the human left in it, and reporting our computed numbers against their
    # file would be a measurement of something that is not on disk.
    judged_capsule = (_capsule_for_bounds(*size[:3])
                      if skinned_asset and scene_action == "written"
                      and not mesh_shapes else None)
    checks = _delivery_checks(scene_view if scene_view.get("ok") else view, view,
                              character_capsule=judged_capsule)
    return {
        "ok": all(c["ok"] for c in checks if c["required"]) and bool(shot.get("ok")),
        "res_path": res_path,
        "asset_rel": asset_rel,
        # None, or what this delivery overwrote at that path. See import_asset.
        "replaced": first.get("replaced"),
        # Which body the asset became and which of the two collision strategies
        # applies. A caller that assumed CharacterBody3D-and-a-capsule for
        # everything — as this function used to hand it — needs both.
        "root_body": root_body,
        "collision": collision,
        "scene": scene_res,
        "scene_file": str(scene_file),
        # written | rewired | left_alone — a caller that assumed it was handed a
        # freshly generated tree needs to know it was handed the human's.
        "scene_action": scene_action,
        "preview": preview_res,
        "screenshot": shot.get("path"),
        "import_settings": settings,
        "engine_view": view,
        "scene_view": scene_view,
        "checks": checks,
        "steps": steps,
    }


def _delivery_checks(scene_view: dict, asset_view: dict,
                     character_capsule: Optional[tuple] = None) -> list[dict]:
    """The gate. Each row names the measurement that decided it.

    `required` marks the ones a shipping asset cannot be without. A prop has no
    rig and no animation and that is fine — those rows report, they do not fail.

    character_capsule is (radius, height) for a capsule THIS run generated for a
    CHARACTER. It folds into has_collider rather than adding a ninth row,
    because callers index these rows by name and the list is a contract. Passing
    it for a crate would be wrong: the absurdity below is defined against a
    person's proportions, and a 1x1x1 m crate legitimately has a capsule as wide
    as it is tall.
    """
    materials = asset_view.get("materials", {}) or {}
    missing = materials.get("without_albedo_texture", []) or []
    size = asset_view.get("size_check", {}) or {}

    # A capsule wider than half the figure it wraps cannot fit through a door
    # built for that figure. REPRODUCED: a 1.75 m character was delivered with
    # radius=0.8158 — 1.63 m across, her own arm span — and it shipped green,
    # because has_collider had only ever COUNTED shapes and never looked at one.
    absurd = ""
    if character_capsule:
        radius, cap_height = (float(v) for v in character_capsule)
        if radius * 2.0 > cap_height * 0.5:
            absurd = (f"capsule is {radius * 2.0:.2f} m across on a "
                      f"{cap_height:.2f} m figure — wider than half its own "
                      "height, so it cannot fit through a door built for it. "
                      "An A-pose arm span in the measured bounds is the usual "
                      "cause.")
    return [
        {"check": "loads_in_engine", "required": True,
         "ok": bool(asset_view.get("ok")),
         "measured": asset_view.get("root_type", "")},
        {"check": "has_geometry", "required": True,
         "ok": asset_view.get("total_tris", 0) > 0,
         "measured": f"{asset_view.get('total_tris', 0)} tris"},
        {"check": "materials_carry_a_texture", "required": True,
         # The "21 materials, ZERO images" disaster, as an assertion. A named
         # material proves nothing; a sampled texture does.
         "ok": materials.get("surfaces", 0) > 0 and not missing,
         "measured": f"{materials.get('with_albedo_texture', 0)}/"
                     f"{materials.get('surfaces', 0)} surfaces textured",
         "detail": missing[:8]},
        {"check": "real_world_size", "required": True,
         "ok": bool(size.get("ok")),
         "measured": f"{size.get('longest_axis_m', 0)} m longest axis",
         "detail": size.get("note", "")},
        {"check": "has_collider", "required": True,
         "ok": scene_view.get("collider_count", 0) > 0 and not absurd,
         "measured": f"{scene_view.get('collider_count', 0)} collision shapes"
                     + (f", capsule r={character_capsule[0]:.4f} "
                        f"h={character_capsule[1]:.4f}"
                        if character_capsule else ""),
         "detail": absurd},
        {"check": "has_skeleton", "required": False,
         "ok": asset_view.get("skeleton_count", 0) > 0,
         "measured": f"{asset_view.get('skeleton_count', 0)} Skeleton3D"},
        {"check": "has_animations", "required": False,
         "ok": asset_view.get("animation_count", 0) > 0,
         "measured": ", ".join(asset_view.get("animations", [])) or "none"},
        {"check": "has_blend_shapes", "required": False,
         "ok": bool(asset_view.get("blend_shapes")),
         "measured": ", ".join(asset_view.get("blend_shapes", [])) or "none"},
    ]


def check_ui_matches(manifest: dict, expect: dict, tolerance: float = 0.5) -> dict:
    """Assert HUD values against expected runtime state — §9's actual payoff.

    `expect` maps a UI node name to the value it should be showing. Numeric
    comparison uses `tolerance` because a health bar tweening toward its target
    is legitimately a hair off for a few frames; an exact match would fail on
    animation rather than on a bug.
    """
    ui = manifest.get("ui", manifest) or {}
    checks = []
    for name, want in expect.items():
        entry = ui.get(name)
        if entry is None:
            checks.append({"ui": name, "ok": False, "error": "not found in manifest"})
            continue
        raw = entry.get("value")
        got = raw.get("value", raw.get("text")) if isinstance(raw, dict) else raw
        if isinstance(want, (int, float)) and isinstance(got, (int, float)):
            ok = abs(float(got) - float(want)) <= tolerance
        else:
            ok = str(got) == str(want)
        checks.append({"ui": name, "ok": ok, "expected": want, "actual": got,
                       "screen_bounds": entry.get("screen_bounds"),
                       "visible": entry.get("visible")})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


# Godot's diagnostics have a FORMAT, and it is worth parsing instead of
# grepping. Measured on 4.7.1 (see tests/test_adapters_adjust.py for the raw
# captures): every engine-reported failure starts its own line with a severity
# label and a colon —
#
#     SCRIPT ERROR: Parse Error: Function "nope()" not found in base self.
#        at: GDScript::reload (C:/.../s.gd:4)
#     ERROR: Cannot open file 'res://does_not_exist.tscn'.
#        at: load (scene/resources/resource_format_text.cpp:1442)
#        GDScript backtrace (most recent call first):
#            [0] _init (C:/.../s.gd:4)
#
# The old substring grep ("invalid", "error:", ...) matched anywhere on a line,
# so a game that prints "checking for invalid input handling" or "no error:
# everything nominal" reported a failing build — verified, both lines survive a
# clean 4.7.1 run. Anchoring on the label kills the false positives without
# loosening the gate: the labels below are the complete set the engine emits,
# and WARNING is deliberately not among them (Godot warns constantly on healthy
# projects, and godot_check_project's ok flag hangs off this list).
#
# Continuation lines ("   at: ...", the backtrace) belong to the error above
# them and are not counted separately — they would triple the noise and push
# real distinct errors past the cap.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_ERROR_LINE = re.compile(
    r"^(?:USER\s+)?(?:SCRIPT|SHADER|SHADER\s+COMPILATION)?\s*ERROR\s*:\s*\S")
_AT_LINE = re.compile(r"^at:\s")

# ENGINE NOISE THAT IS NOT A PROJECT FAULT — matched on the message AND on the
# source frame Godot attributes it to. Each entry is (message, C++ frame
# substring) and BOTH must match, which is the whole safety argument: the same
# words from a different frame stay fatal.
#
# 1. `Parameter "t" is null.` from servers/rendering/dummy/.
#
#    Godot 4.4 grew an editor-thumbnail step in the scene importer
#    (ResourceImporterScene::_generate_editor_preview_for_scene, upstream PR
#    #96544) that renders every imported 3D scene into a viewport and reads the
#    result back with ViewportTexture::get_image(). Under --headless the
#    RenderingServer is the DUMMY implementation, which owns no textures, so the
#    readback trips ERR_FAIL_NULL_V and prints:
#
#        ERROR: Parameter "t" is null.
#           at: texture_2d_get (servers/rendering/dummy/storage/texture_storage.h:107)
#
#    Upstream godotengine/godot#108994; regressed in 4.4, absent in 4.3, fixed
#    by PR #109116. MEASURED here on 4.4.1-stable, and it is benign on three
#    independent counts:
#
#      * It is content-blind. A 1740-byte default cube with no material, no UV
#        and no texture produces it exactly as a real asset does — so it cannot
#        be describing anything about the mesh.
#      * It fires only on the pass that actually reimports. The steady-state
#        second --import over the same project is clean.
#      * The imported resource is correct anyway. inspect_resource on the .glb
#        that "failed" reports its full tri count, its UVs and its material.
#
#    What it costs is a FileSystem-dock thumbnail nobody is looking at in a
#    headless run. Without this rule, on 4.4.x every 3D project containing any
#    glTF file reports a failing build forever.
#
#    THE FRAME IS LOAD-BEARING, not decoration. The dummy directory only
#    compiles into the headless RenderingServer stub, so an error attributed
#    there is by construction an artifact of running without a display. The same
#    message from a real renderer is genuinely fatal and MUST still be reported —
#    see the screenshot capture above, which deliberately does not pass
#    --headless precisely because this error there means no PNG was written.
#    Matching the frame is what keeps those two cases apart.
#
#    If Godot ever emits the message without a frame we report it as an error,
#    which is the correct way to be wrong: the failure mode is noise, not
#    silence.
_BENIGN = (
    ('ERROR: Parameter "t" is null.', "servers/rendering/dummy/"),
)


def _errors(output: str) -> list[str]:
    """The engine's own error lines, in order. Godot reports failures on
    stdout/stderr and often still exits 0, so this list — not the return code —
    is what 'did it build' actually means.

    Drops the entries in _BENIGN, each of which is pinned to the engine source
    frame that proves it is a headless-only artifact rather than a fault in the
    project. Read that table before adding to it — a message alone is never
    enough to call an error benign.
    """
    lines = [_ANSI.sub("", raw).strip() for raw in output.splitlines()]
    hits: list[str] = []
    for i, line in enumerate(lines):
        # --import paints its progress lines; the labels arrive colored too.
        if not _ERROR_LINE.match(line):
            continue
        # Godot prints "   at: <func> (<file>:<line>)" directly under the
        # message, so the frame is the next line or there is none.
        after = lines[i + 1] if i + 1 < len(lines) else ""
        frame = after if _AT_LINE.match(after) else ""
        if any(line == msg and where in frame for msg, where in _BENIGN):
            continue
        if line not in hits:
            hits.append(line)
    return hits[:20]

# ---------------------------------------------------------------------------
# TileSets — the only referee that counts
# ---------------------------------------------------------------------------
# bgate_core.tilemap can now WRITE a TileSet as well as parse one, and those
# two agreeing proves nothing: they were written against the same reading of a
# format neither of them owns. No Godot-authored TileSet existed on the machine
# to check against, so the engine itself is the ground truth — this loads the
# resource the way the game will and reports what Godot actually built.
_TILESET_GD = """
extends SceneTree

func _init() -> void:
    var path := OS.get_environment("BGATE_TILESET")
    var out := {"ok": false, "path": path}
    var res = load(path)
    if res == null:
        out["error"] = "Godot could not load the resource at all"
    elif not (res is TileSet):
        out["error"] = "loaded, but it is a %s, not a TileSet" % res.get_class()
    else:
        var ts: TileSet = res
        var sources := {}
        for i in range(ts.get_source_count()):
            var sid := ts.get_source_id(i)
            var src := ts.get_source(sid)
            var coords := []
            if src is TileSetAtlasSource:
                var atlas: TileSetAtlasSource = src
                for k in range(atlas.get_tiles_count()):
                    var c := atlas.get_tile_id(k)
                    coords.append([c.x, c.y])
            coords.sort_custom(func(a, b): return a[1] < b[1] or (a[1] == b[1] and a[0] < b[0]))
            sources[str(sid)] = {"class": src.get_class(), "tiles": coords}
        out["ok"] = true
        out["tile_size"] = [ts.tile_size.x, ts.tile_size.y]
        out["tile_shape"] = int(ts.tile_shape)
        out["source_count"] = ts.get_source_count()
        out["sources"] = sources
    print("BGATE_JSON_START")
    print(JSON.stringify(out))
    print("BGATE_JSON_END")
    quit()
"""


def inspect_tileset(project_dir: str, res_path: str,
                    timeout: int = 180) -> dict:
    """Load a TileSet IN THE ENGINE and report what Godot actually built.

    ``res_path`` is a ``res://`` path inside ``project_dir``. Returns
    ``{ok, tile_size, tile_shape, source_count, sources: {id: {class, tiles}}}``
    — enough for a writer to assert that every coordinate it emitted came back,
    which is the check that catches a format misconception rather than a typo.
    """
    import json
    import tempfile

    exe = find_godot()
    tmp = Path(tempfile.mkdtemp(prefix="bgate_tileset_"))
    try:
        script = tmp / "inspect_tileset.gd"
        script.write_text(_TILESET_GD, encoding="utf-8")
        env = {**os.environ, "BGATE_TILESET": res_path}
        cmd = [exe, "--headless", "--path", str(project_dir),
               "--script", str(script)]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL, env=env,
                                  creationflags=_NO_WINDOW, **_TEXT)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"inspect timed out after {timeout}s"}
        output = proc.stdout or ""
        if "BGATE_JSON_START" not in output:
            return {"ok": False, "error": "inspector produced no result",
                    "stdout": output[-1200:], "stderr": (proc.stderr or "")[-800:]}
        blob = output.split("BGATE_JSON_START", 1)[1].split(
            "BGATE_JSON_END", 1)[0].strip()
        try:
            return json.loads(blob)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"unreadable inspector output: {exc}",
                    "raw": blob[:500]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
