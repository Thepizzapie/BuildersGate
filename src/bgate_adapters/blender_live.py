"""A LIVE Blender: the kit running inside a Blender that stays open.

`blender.run_script` launches a fresh headless Blender per call (cold start,
empty scene, nothing survives between calls). This adapter talks instead to
the socket the official Blender MCP add-on opens (Blender Lab, "MCP" extension,
Blender 5.1+): every call executes in the SAME running Blender, so the scene
persists, a modelling session is a conversation of small steps, and the
viewport the human is looking at is the one the agent is building in.

Protocol (the add-on's `mcp_to_blender_server.py`): TCP on localhost:9876, one
request per connection, null-byte-delimited JSON both ways:
    {"type": "execute", "code": "...", "strict_json": false}\0
    -> {"status": "ok", "result": {...}, "stdout": "..."}\0
The code runs in a FRESH namespace each time with `result = {}` pre-bound and
whatever the code assigns to `result` comes back. Scene state persists; Python
state does not - so the kit is installed ONCE as a module (`bgate_kit` in
sys.modules) and each call does `from bgate_kit import *`.

Not a replacement for run_script: exports still go through the same glTF
settings, but nothing is deleted from the live scene to make them (the
headless runner strips lights/cameras from a scene it owns; this one does not
own the scene).
"""

from __future__ import annotations

import ast
import json
import socket
from pathlib import Path
from typing import Optional

from . import _blender_kit as _kit

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876
KIT_MODULE = "bgate_kit"
LIVE_MODULE = "bgate_live"
_MAX_STDOUT = 4000

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "LiveBlenderError", "available", "request", "ensure_kit",
           "run", "export_glb", "view", "scene", "reset"]


class LiveBlenderError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Wire protocol.

def request(code: str, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, strict_json: bool = False,
            timeout: float = 300.0) -> dict:
    """Send one execute request and return the add-on's response dict.

    Raises LiveBlenderError when nothing is listening (the add-on is not
    installed, not started, or on another port) or the response is malformed.
    """
    payload = json.dumps({"type": "execute", "code": code, "strict_json": bool(strict_json)}).encode("utf-8") + b"\0"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect((host, int(port)))
        except OSError as exc:
            raise LiveBlenderError(
                f"no live Blender on {host}:{port} ({exc}). Open Blender, enable the MCP extension and press "
                "'Start MCP Bridge Server' in its preferences (or tick Autostart)."
            ) from exc
        sock.sendall(payload)
        buf = bytearray()
        while b"\0" not in buf:
            try:
                chunk = sock.recv(65536)
            except socket.timeout as exc:
                raise LiveBlenderError(f"live Blender did not answer within {timeout:.0f}s") from exc
            if not chunk:
                break
            buf.extend(chunk)
    finally:
        sock.close()
    if b"\0" not in buf:
        raise LiveBlenderError("live Blender closed the connection without a response")
    raw = bytes(buf[:buf.index(b"\0")])
    try:
        response = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise LiveBlenderError(f"malformed response from live Blender: {raw[:200]!r}") from exc
    if not isinstance(response, dict):
        raise LiveBlenderError(f"unexpected response from live Blender: {response!r}")
    return response


def available(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 3.0) -> dict:
    """Is a live Blender answering? {available, version, kit_loaded, host, port}."""
    try:
        got = request(
            "import bpy, sys\n"
            "result = {'version': bpy.app.version_string, 'background': bpy.app.background,\n"
            f"          'kit_loaded': {KIT_MODULE!r} in sys.modules, 'file': bpy.data.filepath}}\n",
            host=host, port=port, strict_json=True, timeout=timeout)
    except LiveBlenderError as exc:
        return {"available": False, "host": host, "port": port, "error": str(exc)}
    if got.get("status") != "ok":
        return {"available": False, "host": host, "port": port, "error": got.get("message")}
    return {"available": True, "host": host, "port": port, **(got.get("result") or {})}


# ---------------------------------------------------------------------------
# The kit and the live helpers, installed once as modules.

def _runner_functions(names: tuple[str, ...]) -> str:
    """Source of named top-level functions from _blender_runner.py, by AST.

    The runner is a Blender-side script (imports bpy at top level) so it
    cannot be imported here; its export/report helpers are lifted as text.
    """
    runner = Path(__file__).with_name("_blender_runner.py")
    source = runner.read_text(encoding="utf-8")
    tree = ast.parse(source)
    out = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            seg = ast.get_source_segment(source, node)
            if seg:
                out.append(seg)
    missing = set(names) - {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    if missing:
        raise LiveBlenderError(f"runner helpers not found: {sorted(missing)}")
    return "\n\n".join(out)


_LIVE_HELPERS = r'''
import os as _os

def export_glb(path):
    """Export the live scene as a game .glb WITHOUT touching the scene."""
    _flatten_procedural_inputs()
    flags = _export_flags()
    has_shapes = bool(flags["shape_keys"])
    kwargs = {
        "filepath": path, "export_format": "GLB", "export_apply": not has_shapes, "export_yup": True,
        "use_selection": False, "export_materials": "EXPORT", "export_cameras": False, "export_lights": False,
        "export_animations": bool(flags["actions"]), "export_morph": has_shapes,
        "export_skins": bool(flags["armatures"]),
    }
    known = set(bpy.ops.export_scene.gltf.get_rna_type().properties.keys())
    kwargs = {k: v for k, v in kwargs.items() if k in known or k == "filepath"}
    _os.makedirs(_os.path.dirname(_os.path.abspath(path)) or ".", exist_ok=True)
    bpy.ops.export_scene.gltf(**kwargs)
    return {"exported": _os.path.exists(path), "path": path, "shape_keys": has_shapes,
            "modifiers_applied_by_exporter": not has_shapes}


def view(path, width=1280, height=720):
    """Capture what the human sees: the first 3D viewport, through the
    viewport (OpenGL) render. Falls back to a camera render when there is no
    3D view (background mode)."""
    scene = bpy.context.scene
    scene.render.image_settings.file_format = "PNG"
    scene.render.resolution_x, scene.render.resolution_y = int(width), int(height)
    scene.render.resolution_percentage = 100
    scene.render.filepath = path
    _os.makedirs(_os.path.dirname(_os.path.abspath(path)) or ".", exist_ok=True)
    wm = bpy.context.window_manager
    for window in (wm.windows if wm else []):
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None:
                continue
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.render.opengl(write_still=True, view_context=True)
            return {"captured": _os.path.exists(path), "path": path, "how": "viewport"}
    if not scene.camera:
        return {"captured": False, "path": path, "how": "none",
                "reason": "no 3D viewport and no camera - open a 3D view or add a camera"}
    bpy.ops.render.render(write_still=True)
    return {"captured": _os.path.exists(path), "path": path, "how": "camera", "engine": scene.render.engine}


def report():
    return _scene_report()
'''


def _install_code() -> str:
    """Code that installs the kit and the live helpers as modules, once."""
    helpers = _runner_functions(("_mesh_stats", "_scene_report", "_flatten_procedural_inputs", "_export_flags"))
    return (
        "import sys, types, bpy\n"
        f"if {KIT_MODULE!r} not in sys.modules:\n"
        f"    _m = types.ModuleType({KIT_MODULE!r})\n"
        "    _m.__dict__['bpy'] = bpy\n"
        f"    exec(compile({_kit.KIT!r}, '<bgate_kit>', 'exec'), _m.__dict__)\n"
        f"    sys.modules[{KIT_MODULE!r}] = _m\n"
        f"if {LIVE_MODULE!r} not in sys.modules:\n"
        f"    _l = types.ModuleType({LIVE_MODULE!r})\n"
        "    _l.__dict__['bpy'] = bpy\n"
        f"    exec(compile({(helpers + _LIVE_HELPERS)!r}, '<bgate_live>', 'exec'), _l.__dict__)\n"
        f"    sys.modules[{LIVE_MODULE!r}] = _l\n"
        f"result = {{'kit_loaded': True, 'kit_functions': len([k for k in sys.modules[{KIT_MODULE!r}].__dict__ "
        "if k.startswith('bg_')])}\n"
    )


def ensure_kit(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 120.0) -> dict:
    """Install the kit into the live session if it is not there yet."""
    probe = request(f"import sys\nresult = {{'loaded': {KIT_MODULE!r} in sys.modules and {LIVE_MODULE!r} in sys.modules}}\n",
                    host=host, port=port, strict_json=True, timeout=timeout)
    if probe.get("status") == "ok" and (probe.get("result") or {}).get("loaded"):
        return {"kit_loaded": True, "installed_now": False}
    got = request(_install_code(), host=host, port=port, strict_json=True, timeout=timeout)
    if got.get("status") != "ok":
        raise LiveBlenderError("installing the kit in the live Blender failed: %s" % (got.get("message") or got))
    return {**(got.get("result") or {}), "installed_now": True}


# ---------------------------------------------------------------------------
# Running scripts.

def _wrap(script: str, export_glb_path: Optional[str], view_path: Optional[str], report: bool) -> str:
    # The add-on executes on a timer, whose bpy.context has no window, area or
    # region. Object-mode operators the kit leans on (join, modifier_apply,
    # convert, shade_smooth) poll against that context, so the body runs under
    # a temp_override of the first 3D view when the GUI has one.
    lines = [
        "import bpy, contextlib",
        f"from {KIT_MODULE} import *",
        f"import {LIVE_MODULE} as _bgl",
        "def __bg_override():",
        "    wm = bpy.context.window_manager",
        "    for window in (wm.windows if wm else []):",
        "        for area in window.screen.areas:",
        "            if area.type == 'VIEW_3D':",
        "                region = next((r for r in area.regions if r.type == 'WINDOW'), None)",
        "                if region is not None:",
        "                    return bpy.context.temp_override(window=window, area=area, region=region)",
        "    return contextlib.nullcontext()",
        "__bg_user = {}",
        "def __bg_body():",
        "    global result",
    ]
    for line in script.splitlines() or [""]:
        lines.append("    " + line)
    lines += [
        "    return result",
        "with __bg_override():",
        "    __bg_user = __bg_body()",
        "result = {'user': __bg_user if isinstance(__bg_user, dict) else {'value': repr(__bg_user)}}",
    ]
    if report:
        lines.append("result['scene'] = _bgl.report()")
    if export_glb_path:
        lines.append(f"result['export'] = _bgl.export_glb({str(export_glb_path)!r})")
    if view_path:
        lines.append(f"result['view'] = _bgl.view({str(view_path)!r})")
    return "\n".join(lines) + "\n"


def run(script: str, *, export_glb: Optional[str] = None, view: Optional[str] = None, report: bool = True,
        host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 300.0) -> dict:
    """Run a kit script in the live Blender. The scene persists between calls.

    Returns {ok, result (what the script put in `result`), scene, export, view,
    stdout, error}. `bg_wipe()` is NOT called for you - the point of the live
    session is that what is there stays there.
    """
    try:
        installed = ensure_kit(host=host, port=port, timeout=timeout)
    except LiveBlenderError as exc:
        return {"ok": False, "error": str(exc), "live": False}
    code = _wrap(script, export_glb, view, report)
    try:
        got = request(code, host=host, port=port, strict_json=False, timeout=timeout)
    except LiveBlenderError as exc:
        return {"ok": False, "error": str(exc), "live": True}
    out = {"ok": got.get("status") == "ok", "live": True, "kit_installed_now": installed.get("installed_now", False)}
    stdout = str(got.get("stdout") or "")
    if stdout:
        out["stdout"] = stdout[-_MAX_STDOUT:]
    if got.get("stderr"):
        out["stderr"] = str(got["stderr"])[-_MAX_STDOUT:]
    if out["ok"]:
        res = got.get("result") or {}
        out["result"] = res.get("user", {})
        for key in ("scene", "export", "view"):
            if key in res:
                out[key] = res[key]
        if export_glb and not (out.get("export") or {}).get("exported"):
            out["ok"] = False
            out["error"] = "the export wrote no file"
    else:
        message = str(got.get("message") or "live Blender reported an error")
        out["error"] = message.strip().splitlines()[-1] if message.strip() else message
        out["traceback"] = message[-1500:]
    return out


def export_glb(out_path, **kw) -> dict:
    """Export the live scene as it stands."""
    return run("pass", export_glb=str(out_path), report=True, **kw)


def view(out_path, **kw) -> dict:
    """Capture the live viewport (what the human sees) to a PNG."""
    return run("pass", view=str(out_path), report=False, **kw)


def scene(**kw) -> dict:
    """The live scene's report, nothing else."""
    return run("pass", report=True, **kw)


def reset(**kw) -> dict:
    """Empty the live scene (bg_wipe) - the one destructive call, by name."""
    return run("bg_wipe()\nresult = {'wiped': True}", report=True, **kw)
