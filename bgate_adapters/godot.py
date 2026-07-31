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

import os
import re
import shutil
import subprocess
import sys
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


def version() -> dict:
    exe = find_godot()
    proc = _spawn([exe, "--version"], timeout=60)
    raw = (proc.stdout or proc.stderr or "").strip().splitlines()
    return {"path": exe, "version": raw[-1] if raw else "unknown"}


def run_script(script: str, project_dir: Optional[str] = None,
               timeout: int = 120) -> dict:
    """Run a GDScript file headless (a SceneTree script) and capture output.

    The script must extend SceneTree or MainLoop and call quit(), or it will run
    until the timeout. Returns {ok, stdout, stderr, exit_code, seconds}.
    """
    import tempfile
    import time

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


def import_asset(project_dir: str, src_path: str, dest_rel: str = "assets",
                 timeout: int = 240) -> dict:
    """Bring an asset into a Godot project and VERIFY the engine loads it.

    Copies src into <project>/<dest_rel>/, triggers a headless import, then loads
    the resource in-engine and reports the meshes Godot actually built. Copying a
    file in is not integration — an asset that imports with zero surfaces is a
    silent failure, so this checks the ENGINE's view, not the file's presence.
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
    shutil.copy2(src, dest)

    imported = check_project(str(project), timeout=timeout)
    res_path = "res://" + str(dest.relative_to(project)).replace("\\", "/")
    inspected = inspect_resource(str(project), res_path, timeout=timeout)

    return {
        "ok": bool(inspected.get("ok")),
        "copied_to": str(dest),
        "res_path": res_path,
        "import": {"ok": imported["ok"], "errors": imported.get("errors", [])},
        "engine_view": inspected,
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

# Two forms, and missing the first one is a real bug rather than a cosmetic
# one: Godot writes the EMPTY dictionary as a single line, `_subresources={}`,
# which is what every freshly-imported asset has. A pattern that only knows the
# multi-line form silently appends a SECOND `_subresources` key instead of
# replacing the first, and which one the engine honours is then down to
# ConfigFile's duplicate-key behaviour.
_SUBRES_RE = re.compile(
    r"^_subresources=\{\}[ \t]*$|^_subresources=\{\n.*?^\}[ \t]*$", re.S | re.M)


def _gd_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"{}"'.format(str(value).replace('"', '\\"'))


def _render_subresources(nodes: dict) -> str:
    """Godot's variant-text form of the `_subresources` dictionary."""
    if not nodes:
        return "_subresources={}"
    blocks = []
    for node_path, opts in nodes.items():
        body = ",\n".join(f'"{k}": {_gd_literal(v)}' for k, v in opts.items())
        blocks.append(f'"PATH:{node_path}": {{\n{body}\n}}')
    return '_subresources={\n"nodes": {\n' + ",\n".join(blocks) + "\n}\n}"


def _purge_import_cache(project: Path, asset: Path) -> list[str]:
    """Drop the cached import products so the next --import genuinely re-runs.

    Godot decides "already imported" from the .md5 sidecar of the SOURCE file.
    Changing only the .import params leaves that md5 identical, so a reimport is
    skipped and the new settings never take — the failure looks exactly like the
    settings being wrong, which is a long afternoon.
    """
    removed = []
    imported = project / ".godot" / "imported"
    if not imported.is_dir():
        return removed
    for path in imported.glob(asset.name + "-*"):
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            pass
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

    The `.import` must already exist, which means the asset has been imported
    once. Returns {ok, path, physics_nodes, purged}.
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

    rendered = _render_subresources(nodes)
    if _SUBRES_RE.search(body):
        body = _SUBRES_RE.sub(lambda _m: rendered, body, count=1)
    else:
        body = body.rstrip() + "\n" + rendered + "\n"

    for key, value in (params or {}).items():
        line = f"{key}={_gd_literal(value)}"
        pattern = re.compile(r"^" + re.escape(key) + r"=.*$", re.M)
        body = (pattern.sub(lambda _m: line, body, count=1)
                if pattern.search(body) else body.rstrip() + "\n" + line + "\n")

    ini.write_text(head + "[params]" + body, encoding="utf-8")
    purged = _purge_import_cache(project, asset) if purge else []
    return {"ok": True, "path": str(ini), "physics_nodes": nodes,
            "purged": purged}


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


def character_scene_text(model_res: str, *, node_name: str,
                         bounds_size, bounds_position,
                         script_res: str = "", model_uid: str = "",
                         camera_height: float = 0.7,
                         body_type: str = "CharacterBody3D") -> str:
    """The .tscn source: model instanced under a body, capsule fitted to it."""
    size_x, size_y, size_z = (float(v) for v in bounds_size)
    pos_x, pos_y, pos_z = (float(v) for v in bounds_position)

    height = max(size_y, 0.05)
    radius = max(max(size_x, size_z) * 0.5, 0.01)
    # Godot rejects a capsule whose radius exceeds half its height; a squat
    # asset (a crate, a turret) hits this immediately.
    radius = min(radius, height * 0.5 - 0.001) if height > 0.03 else radius

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

    steps = len(ext) + 1 + 1  # ext resources + the capsule sub_resource + 1
    lines = [
        f"[gd_scene load_steps={steps} format=3]",
        "",
        *ext,
        "",
        '[sub_resource type="CapsuleShape3D" id="CapsuleShape3D_body"]',
        f"radius = {radius:.4f}",
        f"height = {height:.4f}",
        "",
        f'[node name="{node_name}" type="{body_type}"]',
    ]
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
        '[node name="CollisionShape3D" type="CollisionShape3D" parent="."]',
        'shape = SubResource("CapsuleShape3D_body")',
        "",
        '[node name="Camera3D" type="Camera3D" parent="."]',
        _transform3d(((1, 0, 0), (0, 1, 0), (0, 0, 1)),
                     (0.0, min(camera_height, height * 0.5 - 0.05), 0.0)
                     ).replace("Transform3D", "transform = Transform3D"),
        "",
    ]
    return "\n".join(lines)


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
        # full-screen blur: the character scene carries its own first-person
        # Camera3D (player.gd needs $Camera3D), and Godot makes the FIRST camera
        # to enter the tree current. The subject is instanced before this node,
        # so without an explicit current the screenshot is taken from INSIDE the
        # character's head, looking at the back of its own mesh.
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


# Injected autoload that screenshots the RUNNING game. Uses env for its
# parameters so nothing project-side needs editing.
_SHOT_GD = """
extends Node

func _ready() -> void:
	var at := float(OS.get_environment("BGATE_SHOT_AT"))
	get_tree().create_timer(maxf(at, 0.1)).timeout.connect(_shoot)

func _shoot() -> void:
	var img := get_viewport().get_texture().get_image()
	img.save_png(OS.get_environment("BGATE_SHOT_PATH"))
	print("BGATE_SHOT_SAVED")
	get_tree().quit()
"""


def screenshot(project_dir: str, out_path: str, *, at: float = 1.0,
               scene: Optional[str] = None, timeout: int = 120) -> dict:
    """Run the ACTUAL game briefly and capture the viewport to a PNG.

    This is the 2D feedback loop: headless checks prove the game boots, but an
    agent iterating on look has to SEE the running frame. Needs a GPU/display,
    so a game window appears for ~`at`+1 seconds — the cost of a real frame.

    Mechanism: Godot auto-reads `override.cfg` next to project.godot, and
    autoloads are just settings — so we inject a screenshot autoload there,
    run, and remove it. The project's own files are never modified; if a stale
    override.cfg already exists we refuse rather than clobber it.
    """
    project = Path(project_dir)
    if not (project / "project.godot").exists():
        return {"ok": False, "error": f"no project.godot in {project_dir}"}

    override = project / "override.cfg"
    if override.exists():
        return {"ok": False, "error": "override.cfg already exists in the project — "
                                      "refusing to clobber it; remove it first"}

    shot_script = project / ".bgate_shot.gd"
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        shot_script.write_text(_SHOT_GD, encoding="utf-8")
        override.write_text(
            '[autoload]\nBGateShot="*res://.bgate_shot.gd"\n', encoding="utf-8")

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
                    "output": output[-1500:], "errors": _errors(output)}
        return {"ok": True, "path": str(out), "bytes": out.stat().st_size,
                "at": at, "errors": _errors(output)}
    finally:
        # Never leave the injection behind — a stray override.cfg silently
        # changes how the user's project runs forever after.
        for leftover in (override, shot_script):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            (project / ".bgate_shot.gd.uid").unlink(missing_ok=True)
        except OSError:
            pass


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
_EVIDENCE_GD = """
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
    import json
    import time

    project = Path(project_dir)
    if not (project / "project.godot").exists():
        return {"ok": False, "error": f"no project.godot in {project_dir}"}

    override = project / "override.cfg"
    if override.exists():
        return {"ok": False, "error": "override.cfg already exists in the project — "
                                      "refusing to clobber it; remove it first"}

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    beauty_path = out / "beauty.png"
    overlay_path = out / "overlay.png" if overlay else None
    manifest_path = out / "manifest.json"

    ev_script = project / ".bgate_evidence.gd"
    started = time.monotonic()
    try:
        ev_script.write_text(_EVIDENCE_GD, encoding="utf-8")
        override.write_text(
            '[autoload]\nBGateEvidence="*res://.bgate_evidence.gd"\n',
            encoding="utf-8")

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
            "errors": _errors(output),
        }
    finally:
        for leftover in (override, ev_script):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            (project / ".bgate_evidence.gd.uid").unlink(missing_ok=True)
        except OSError:
            pass


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
                  character_body: str = "CharacterBody3D",
                  screenshot_dir: Optional[str] = None, at: float = 1.2,
                  min_size_m: float = 0.05, max_size_m: Optional[float] = None,
                  nominal_size_m: float = 1.8, timeout: int = 300) -> dict:
    """Take a .glb all the way to a Godot screenshot, and report every step.

    physics   "auto"  — generate colliders for every UNSKINNED mesh. A skinned
                        character gets its capsule from the generated .tscn
                        instead; a trimesh StaticBody3D on a character would
                        turn it into a wall you cannot move.
              "all"   — every mesh, skinned or not.
              "none"  — leave the importer's defaults alone.

    max_size_m  None picks the bound from what the asset IS: 4 m for anything
                skinned (a character over 4 m across is a unit error, not a
                design choice) and 50 m otherwise, because a vehicle or a
                building is legitimately large. Pass a number to be explicit.

    Returns {ok, res_path, scene, preview, screenshot, engine_view, checks,
             steps}. `checks` is the gate: rigged/animated/textured/sized/
             collided, each with the measurement that decided it.
    """
    project = Path(project_dir)
    steps: list[dict] = []

    first = import_asset(project_dir, glb_path, dest_rel=dest_rel, timeout=timeout)
    steps.append({"step": "import", "ok": bool(first.get("ok")),
                  "errors": first.get("import", {}).get("errors", [])})
    if not first.get("ok"):
        return {"ok": False, "error": "the engine could not load the asset",
                "detail": first, "steps": steps}

    asset_rel = str(Path(first["copied_to"]).relative_to(project)).replace(
        "\\", "/")
    res_path = first["res_path"]
    view = first["engine_view"]

    if max_size_m is None:
        skinned_asset = any(m.get("skinned") for m in view.get("meshes", []))
        max_size_m = 4.0 if skinned_asset else 50.0

    def _look(path: str) -> dict:
        return inspect_resource(project_dir, path, timeout=timeout,
                                min_size_m=min_size_m, max_size_m=max_size_m,
                                nominal_size_m=nominal_size_m)

    # import_asset ran with the generic bounds; re-gate against the ones this
    # asset actually earned.
    view = _look(res_path)

    # --- colliders ---------------------------------------------------------
    physics_nodes: dict = {}
    if physics in ("auto", "all"):
        for mesh in view.get("meshes", []):
            if physics == "auto" and mesh.get("skinned"):
                continue
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
                      "note": "every mesh is skinned; the .tscn capsule is the "
                              "collider (physics=all to override)"})

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
    scene_file.write_text(
        character_scene_text(res_path, node_name=node_name, bounds_size=size,
                             bounds_position=origin, script_res=script_res,
                             model_uid=_import_uid(project_dir, asset_rel),
                             body_type=character_body),
        encoding="utf-8")
    scene_res = "res://" + str(scene_file.relative_to(project)).replace("\\", "/")

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
    steps.append({"step": "scenes", "ok": scene_import["ok"],
                  "errors": scene_import.get("errors", []),
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

    checks = _delivery_checks(scene_view if scene_view.get("ok") else view, view)
    return {
        "ok": all(c["ok"] for c in checks if c["required"]) and bool(shot.get("ok")),
        "res_path": res_path,
        "asset_rel": asset_rel,
        "scene": scene_res,
        "scene_file": str(scene_file),
        "preview": preview_res,
        "screenshot": shot.get("path"),
        "import_settings": settings,
        "engine_view": view,
        "scene_view": scene_view,
        "checks": checks,
        "steps": steps,
    }


def _delivery_checks(scene_view: dict, asset_view: dict) -> list[dict]:
    """The gate. Each row names the measurement that decided it.

    `required` marks the ones a shipping asset cannot be without. A prop has no
    rig and no animation and that is fine — those rows report, they do not fail.
    """
    materials = asset_view.get("materials", {}) or {}
    missing = materials.get("without_albedo_texture", []) or []
    size = asset_view.get("size_check", {}) or {}
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
         "ok": scene_view.get("collider_count", 0) > 0,
         "measured": f"{scene_view.get('collider_count', 0)} collision shapes"},
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


def _errors(output: str) -> list[str]:
    """The engine's own error lines, in order. Godot reports failures on
    stdout/stderr and often still exits 0, so this list — not the return code —
    is what 'did it build' actually means."""
    hits = []
    for raw in output.splitlines():
        # --import paints its progress lines; the labels arrive colored too.
        line = _ANSI.sub("", raw).strip()
        if not _ERROR_LINE.match(line):
            continue
        if line not in hits:
            hits.append(line)
    return hits[:20]
