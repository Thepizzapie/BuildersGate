"""Godot workspace endpoints — the engine surface for the gameplay/tech seats.

The native Godot editor can't be embedded in a web page, so this exposes the
next best thing over the existing headless adapter: browse scenes/scripts,
inspect a resource in-engine, run a GDScript, screenshot a scene, and build-check
the project. project_dir defaults to <root>/game.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from bgate_adapters import godot as _godot
from bgate_ui.deps import root

router = APIRouter()

_CODE_SUFFIXES = {".gd", ".cfg", ".godot", ".tres", ".tscn", ".import", ".json", ".cs"}
_MAX_READ = 200_000


def _project(project_dir: str | None) -> Path:
    r = root()
    p = Path(project_dir).resolve() if project_dir else (r / "game").resolve()
    try:
        p.relative_to(r.resolve())
    except ValueError:
        raise HTTPException(403, "project_dir escapes the project root")
    if not p.is_dir():
        raise HTTPException(404, f"no godot project at {p}")
    return p


def _tree(base: Path, root_dir: Path, want: set[str], depth: int = 0) -> list[dict]:
    out = []
    if depth > 8:
        return out
    try:
        entries = sorted(base.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    except OSError:
        return out
    for e in entries:
        if e.name.startswith(".") and e.name not in (".godot",):
            continue
        if e.is_dir():
            kids = _tree(e, root_dir, want, depth + 1)
            if kids:
                out.append({"name": e.name, "dir": True, "children": kids})
        elif e.suffix.lower() in want:
            out.append({"name": e.name, "dir": False,
                        "rel": str(e.relative_to(root_dir)).replace("\\", "/"),
                        "bytes": e.stat().st_size})
    return out


@router.get("/api/godot/status")
def godot_status() -> dict:
    info = _godot.available()
    if info.get("available"):
        try:
            info["version"] = _godot.version().get("version", "")
        except Exception:
            info["version"] = ""
    try:
        info["project"] = str(_project(None))
    except HTTPException:
        info["project"] = None
    return info


@router.get("/api/godot/files")
def godot_files(project_dir: str | None = None, kind: str = "all") -> dict:
    """The project's scenes/scripts/resources as a tree for the workspace nav."""
    p = _project(project_dir)
    want = {".tscn": {".tscn"}, ".gd": {".gd", ".cs"}}.get(kind)
    if want is None:
        want = _CODE_SUFFIXES
    return {"project": str(p), "tree": _tree(p, p, want)}


@router.get("/api/godot/file")
def godot_file(rel: str, project_dir: str | None = None) -> dict:
    """Read one text file (script/scene/config) for the editor pane."""
    p = _project(project_dir)
    target = (p / rel).resolve()
    try:
        target.relative_to(p)
    except ValueError:
        raise HTTPException(403, "path escapes the project")
    if not target.is_file():
        raise HTTPException(404, rel)
    if target.suffix.lower() not in _CODE_SUFFIXES:
        raise HTTPException(415, "not a readable text resource")
    text = target.read_text(encoding="utf-8", errors="replace")
    return {"rel": rel, "bytes": len(text.encode("utf-8")),
            "truncated": len(text) > _MAX_READ, "text": text[:_MAX_READ]}


@router.post("/api/godot/inspect")
def godot_inspect(payload: dict) -> dict:
    """Load a resource in-engine (scene tree, meshes, tris)."""
    res = payload.get("res_path")
    if not res:
        raise HTTPException(400, "res_path required")
    p = _project(payload.get("project_dir"))
    return _godot.inspect_resource(str(p), res, timeout=int(payload.get("timeout", 180)))


@router.post("/api/godot/run")
def godot_run(payload: dict) -> dict:
    """Run an arbitrary headless GDScript (must extend SceneTree, call quit())."""
    script = payload.get("script")
    if not script:
        raise HTTPException(400, "script required")
    p = _project(payload.get("project_dir"))
    return _godot.run_script(script, str(p), timeout=int(payload.get("timeout", 120)))


@router.post("/api/godot/check")
def godot_check(payload: dict | None = None) -> dict:
    """Headless import/build — 'does it still compile'."""
    payload = payload or {}
    p = _project(payload.get("project_dir"))
    return _godot.check_project(str(p), timeout=int(payload.get("timeout", 180)))


@router.post("/api/godot/screenshot")
def godot_screenshot(payload: dict | None = None) -> dict:
    """Screenshot a scene; returns a project-relative path for /api/preview."""
    payload = payload or {}
    r = root()
    p = _project(payload.get("project_dir"))
    out_dir = r / ".bgate" / "godot_ws"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "shot.png"
    res = _godot.screenshot(str(p), str(out), at=float(payload.get("at", 1.0)),
                            scene=payload.get("scene"),
                            timeout=int(payload.get("timeout", 120)))
    if res.get("ok"):
        try:
            res["rel"] = str(Path(res["path"]).resolve().relative_to(r.resolve())).replace("\\", "/")
        except (ValueError, KeyError):
            res["rel"] = None
    return res
