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
def _spawn(cmd: list[str], timeout: int, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          cwd=cwd, stdin=subprocess.DEVNULL,
                          creationflags=_NO_WINDOW)


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
_INSPECT_GD = """
extends SceneTree

func _walk(node: Node, out: Array) -> void:
	if node is MeshInstance3D and node.mesh != null:
		var mesh: Mesh = node.mesh
		var tris := 0
		var surfaces := []
		for i in mesh.get_surface_count():
			var arrays := mesh.surface_get_arrays(i)
			var verts: PackedVector3Array = arrays[Mesh.ARRAY_VERTEX]
			var idx = arrays[Mesh.ARRAY_INDEX]
			var count: int = (idx.size() if idx != null else verts.size()) / 3
			tris += count
			var mat := mesh.surface_get_material(i)
			surfaces.append({
				"index": i,
				"tris": count,
				"has_uv": arrays[Mesh.ARRAY_TEX_UV] != null,
				"material": (mat.resource_name if mat != null else ""),
			})
		var aabb := mesh.get_aabb()
		out.append({
			"name": node.name,
			"tris": tris,
			"surfaces": surfaces,
			"aabb_size": [aabb.size.x, aabb.size.y, aabb.size.z],
		})
	for child in node.get_children():
		_walk(child, out)

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
	var meshes := []
	_walk(root, meshes)
	var total := 0
	for m in meshes:
		total += m["tris"]
	print("BGATE_JSON_START")
	print(JSON.stringify({
		"ok": true,
		"resource": path,
		"root": root.name,
		"root_type": root.get_class(),
		"meshes": meshes,
		"total_tris": total,
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


def inspect_resource(project_dir: str, res_path: str, timeout: int = 180) -> dict:
    """Load a resource IN THE ENGINE and report what it actually became."""
    import json
    import tempfile

    exe = find_godot()
    tmp = Path(tempfile.mkdtemp(prefix="bgate_inspect_"))  # cleaned in the finally
    try:
        script = tmp / "inspect.gd"
        script.write_text(_INSPECT_GD, encoding="utf-8")

        env = {**os.environ, "BGATE_INSPECT": res_path}
        cmd = [exe, "--headless", "--path", str(project_dir),
               "--script", str(script)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL,
                                  env=env, creationflags=_NO_WINDOW)
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
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL,
                                  env=env, creationflags=_NO_WINDOW)
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
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, stdin=subprocess.DEVNULL,
                                  env=env, creationflags=_NO_WINDOW)
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
