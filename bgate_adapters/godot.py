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
    tmp = Path(tempfile.mkdtemp(prefix="bgate_godot_"))
    # Godot only loads scripts from inside a project when --path is given; for a
    # bare script run it reads the file directly.
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
    tmp = Path(tempfile.mkdtemp(prefix="bgate_inspect_"))
    script = tmp / "inspect.gd"
    script.write_text(_INSPECT_GD, encoding="utf-8")

    env = {**os.environ, "BGATE_INSPECT": res_path}
    cmd = [exe, "--headless", "--path", str(project_dir), "--script", str(script)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=env,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"inspect timed out after {timeout}s"}

    output = proc.stdout or ""
    if "BGATE_JSON_START" not in output:
        return {"ok": False, "error": "inspector produced no result",
                "stdout": output[-1500:], "stderr": (proc.stderr or "")[-800:]}
    blob = output.split("BGATE_JSON_START", 1)[1].split("BGATE_JSON_END", 1)[0].strip()
    try:
        return json.loads(blob)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"unreadable inspector output: {exc}",
                "raw": blob[:500]}


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


def _errors(output: str) -> list[str]:
    """Godot reports failures on stdout and keeps going; grep them out."""
    hits = []
    for line in output.splitlines():
        low = line.lower()
        if any(marker in low for marker in
               ("script error", "parse error", "error:", "failed to load",
                "can't open", "cannot open", "invalid")):
            clean = line.strip()
            if clean and clean not in hits:
                hits.append(clean)
    return hits[:20]
