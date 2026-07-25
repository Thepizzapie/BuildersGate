"""Godot workspace endpoints — the engine surface for the gameplay/tech seats.

The native Godot editor can't be embedded in a web page, so this exposes the
next best thing over the existing headless adapter: browse scenes/scripts,
inspect a resource in-engine, run a GDScript, screenshot a scene, and build-check
the project. project_dir defaults to <root>/game.

The four engine calls are slow — an import can run for minutes — and used to
block the request on a timeout read straight out of the body. Two things changed:
every timeout is clamped to [5, 600] before it reaches the adapter, and each of
those endpoints accepts ``?async=1`` (or ``{"async": true}``) to start a job and
answer 202 ``{job_id}`` instead. The synchronous shape is unchanged, because the
seat JS still calls it.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from bgate_adapters import godot as _godot
from bgate_core import jobs
from bgate_ui.deps import root
from bgate_ui.routes import jobs as jobs_api

router = APIRouter()

_CODE_SUFFIXES = {".gd", ".cfg", ".godot", ".tres", ".tscn", ".import", ".json", ".cs"}
_MAX_READ = 200_000

# A timeout under 5s cannot survive Godot's own startup, and nothing here has a
# legitimate reason to run past 10 minutes.
MIN_TIMEOUT = 5
MAX_TIMEOUT = 600


def clamp_timeout(raw, default: int) -> int:
    """Never trust the timeout in the request body.

    It was passed to the adapter verbatim, so a client could pin an HTTP request
    — and one of the server's threadpool workers — open for as long as it liked.
    Anything unparseable falls back to the endpoint's own default rather than
    failing the call, since a bad timeout is not a reason to refuse the work.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return max(MIN_TIMEOUT, min(value, MAX_TIMEOUT))


def _guard(call: Callable[[], dict]) -> dict:
    """Godot may simply not be installed. That is an answer, not a 500."""
    try:
        return call()
    except _godot.GodotNotFound as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _staged(job_id: int, stage: str, timeout: int, call: Callable[[], dict]) -> dict:
    """Run a blocking engine call with a progress heartbeat behind it.

    The adapter reports nothing until it returns, so the bar is time-based: it
    walks toward 0.9 over the operation's own timeout. That is not precision, but
    it answers the question a spinner cannot — is this five seconds in or three
    minutes in.
    """
    root_dir = root()
    jobs.progress(root_dir, job_id, fraction=0.05, stage=stage)
    stop = threading.Event()
    started = time.monotonic()

    def _beat() -> None:
        while not stop.wait(2.0):
            elapsed = (time.monotonic() - started) / max(1, timeout)
            jobs.progress(root_dir, job_id,
                          fraction=0.05 + 0.85 * min(1.0, elapsed), stage=stage)

    threading.Thread(target=_beat, name=f"job-beat-{job_id}", daemon=True).start()
    try:
        return _guard(call)
    finally:
        stop.set()


def _engine_job(stage: str, timeout: int, call: Callable[[], dict]):
    """Wrap an engine call as a job work function: cancel check, then the call."""
    def work(job_id: int) -> dict:
        if jobs_api.is_cancelled(job_id):
            return jobs_api.cancelled_result("startup")
        result = _staged(job_id, stage, timeout, call)
        jobs.progress(root(), job_id, fraction=1.0,
                      stage="done" if result.get("ok") else "failed")
        return result
    return work


def _async_202(kind: str, stage: str, timeout: int, call: Callable[[], dict],
               *, request_body: dict, request: Request) -> JSONResponse:
    body = jobs_api.start(kind, _engine_job(stage, timeout, call),
                          request_body=request_body, request=request)
    return JSONResponse(status_code=202, content=body)


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
def godot_inspect(payload: dict, request: Request,
                  async_: int = Query(0, alias="async")):
    """Load a resource in-engine (scene tree, meshes, tris)."""
    res = payload.get("res_path")
    if not res:
        raise HTTPException(400, "res_path required")
    p = _project(payload.get("project_dir"))
    timeout = clamp_timeout(payload.get("timeout"), 180)
    call = lambda: _godot.inspect_resource(str(p), res, timeout=timeout)
    if jobs_api.wants_async(payload, async_):
        return _async_202("godot.inspect", f"inspecting {res}", timeout, call,
                          request_body={"res_path": res, "project_dir": str(p),
                                        "timeout": timeout}, request=request)
    return _guard(call)


@router.post("/api/godot/run")
def godot_run(payload: dict, request: Request,
              async_: int = Query(0, alias="async")):
    """Run an arbitrary headless GDScript (must extend SceneTree, call quit())."""
    script = payload.get("script")
    if not script:
        raise HTTPException(400, "script required")
    p = _project(payload.get("project_dir"))
    timeout = clamp_timeout(payload.get("timeout"), 120)
    call = lambda: _godot.run_script(script, str(p), timeout=timeout)
    if jobs_api.wants_async(payload, async_):
        return _async_202("godot.run", "running script", timeout, call,
                          request_body={"project_dir": str(p), "timeout": timeout,
                                        "script_bytes": len(script)},
                          request=request)
    return _guard(call)


@router.post("/api/godot/check")
def godot_check(request: Request, payload: dict | None = None,
                async_: int = Query(0, alias="async")):
    """Headless import/build — 'does it still compile'.

    The slowest thing here by far: a cold import of a project with any real asset
    count is the 90-second case the job model exists for.
    """
    payload = payload or {}
    p = _project(payload.get("project_dir"))
    timeout = clamp_timeout(payload.get("timeout"), 180)
    call = lambda: _godot.check_project(str(p), timeout=timeout)
    if jobs_api.wants_async(payload, async_):
        return _async_202("godot.check", "importing project", timeout, call,
                          request_body={"project_dir": str(p), "timeout": timeout},
                          request=request)
    return _guard(call)


@router.post("/api/godot/screenshot")
def godot_screenshot(request: Request, payload: dict | None = None,
                     async_: int = Query(0, alias="async")):
    """Screenshot a scene; returns a project-relative path for /api/preview."""
    payload = payload or {}
    r = root()
    p = _project(payload.get("project_dir"))
    out_dir = r / ".bgate" / "godot_ws"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "shot.png"
    try:
        at = float(payload.get("at", 1.0))
    except (TypeError, ValueError):
        at = 1.0
    timeout = clamp_timeout(payload.get("timeout"), 120)
    scene = payload.get("scene")

    def call() -> dict:
        res = _godot.screenshot(str(p), str(out), at=at, scene=scene, timeout=timeout)
        if res.get("ok"):
            try:
                res["rel"] = str(Path(res["path"]).resolve()
                                 .relative_to(r.resolve())).replace("\\", "/")
            except (ValueError, KeyError):
                res["rel"] = None
        return res

    if jobs_api.wants_async(payload, async_):
        return _async_202("godot.screenshot", "capturing frame", timeout, call,
                          request_body={"project_dir": str(p), "scene": scene,
                                        "at": at, "timeout": timeout},
                          request=request)
    return _guard(call)
