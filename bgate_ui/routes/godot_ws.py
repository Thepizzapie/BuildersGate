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

import hashlib
import os
import shutil
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

# Everything readable is editable EXCEPT .import: the engine generates those on
# scan and rewrites them without asking, so a hand edit is work that silently
# disappears — which reads as "the editor did not save" rather than "that file
# was never yours".
_WRITABLE_SUFFIXES = _CODE_SUFFIXES - {".import"}

# A source file this size is machine-generated, and the editor could not have
# been the thing that produced it — the read path caps at _MAX_READ, so anything
# past this arrived by another route.
_MAX_WRITE = 1_000_000

# Trees that are not the game: the engine's import cache, the tool's own
# backups, build output. Same set screenmap and scenewire prune.
_SKIP_TREES = {".godot", ".bgate_out", ".bgate", ".git", ".asset_work",
               "export", "build", "__pycache__"}

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


def _default_project(r: Path) -> Path:
    """Where res:// points when the caller did not say.

    This used to be a hardcoded ``<root>/game``, which is right for a project
    `bgate init` scaffolded and wrong for every project `bgate adopt` took on,
    whose project.godot sits at the root. On those, every endpoint here 404'd —
    the workspace looked like Godot was missing rather than like the path was.

    Same resolution order as bgate_core.screenmap and scenewire._godot_dir, on
    purpose: two modules that disagree about which directory is the game are
    two modules that hand each other paths the other cannot resolve.
    """
    for cand in (r, r / "game"):
        if (cand / "project.godot").is_file():
            return cand
    hits = sorted(p.parent for p in r.glob("*/project.godot"))
    return hits[0] if hits else (r / "game")


def _project(project_dir: str | None) -> Path:
    r = root()
    p = Path(project_dir).resolve() if project_dir else _default_project(r).resolve()
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
        # .godot was EXPLICITLY allowed here, which meant the file tree walked
        # the engine's import cache — ~2000 files on a real project, none of
        # them yours, all of them .import/.md5 noise. It is the single biggest
        # directory in a Godot project and nothing in this workspace can open
        # anything inside it.
        if e.name.startswith(".") or e.name in _SKIP_TREES:
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


def _in_project(p: Path, rel: str) -> Path:
    target = (p / rel).resolve()
    try:
        target.relative_to(p)
    except ValueError:
        raise HTTPException(403, "path escapes the project")
    return target


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lock_holder(target: Path) -> dict | None:
    """The seat currently holding this path, if any.

    A human editing a file an agent has claimed is the exact collision the lock
    table exists to make visible, and a dashboard that writes straight through
    it would be the one caller in the system that ignores it. The lookup itself
    lives in the core now — the scene editor and the MCP scene tools ask the
    same question, and three copies of it is three chances for one to drift into
    answering "free" when it is not.
    """
    from bgate_core import assets as _assets
    return _assets.lock_holder(root(), target)


@router.get("/api/godot/file")
def godot_file(rel: str, project_dir: str | None = None) -> dict:
    """Read one text file (script/scene/config) for the editor pane."""
    p = _project(project_dir)
    target = _in_project(p, rel)
    if not target.is_file():
        raise HTTPException(404, rel)
    if target.suffix.lower() not in _CODE_SUFFIXES:
        raise HTTPException(415, "not a readable text resource")
    text = target.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > _MAX_READ
    held = _lock_holder(target)
    return {"rel": rel, "bytes": len(text.encode("utf-8")),
            "truncated": truncated, "text": text[:_MAX_READ],
            # The sha is of what the caller actually GOT. Hashing the full text
            # while handing back a prefix would let a truncated read round-trip
            # as an unmodified save and cut the file down to 200KB.
            "sha": None if truncated else _sha(text),
            "writable": target.suffix.lower() in _WRITABLE_SUFFIXES and not truncated,
            "lock": {"seat": held.get("lock_seat"), "owner": held.get("lock_owner")}
                    if held else None}


@router.post("/api/godot/file")
def godot_file_write(payload: dict) -> dict:
    """Save one text file back into the game project.

    The dashboard has been able to READ every script and scene since the Godot
    workspace shipped, and could not write a byte — which is the whole reason
    the engine had to stay open next to it. Three things guard the write, in
    the order they can bite:

      * ``base_sha`` is what the editor loaded. If the bytes on disk no longer
        hash to it, somebody (an agent, the engine, a git checkout) changed the
        file underneath the tab, and saving would silently discard their work.
        409, with the current hash, so the UI can offer a reload.
      * a held lock is refused with 423 rather than merged, because the holder
        may be an agent mid-edit that will write its own copy over this one.
        ``force`` is available and is a deliberate act.
      * the previous bytes always land in .bgate_out/edits/ first. Ctrl-Z does
        not survive a page reload, and this file is one the engine also owns.
    """
    rel = str(payload.get("rel") or "").strip()
    if not rel:
        raise HTTPException(400, "rel required")
    text = payload.get("text")
    if not isinstance(text, str):
        raise HTTPException(400, "text must be a string")

    p = _project(payload.get("project_dir"))
    target = _in_project(p, rel)
    if target.suffix.lower() not in _WRITABLE_SUFFIXES:
        raise HTTPException(415, f"{target.suffix or 'this file'} is not editable here")
    if not target.is_file():
        # Creating a file is a different act with different consequences (an
        # empty .gd attached to nothing, a .tscn the engine will not import),
        # and nothing in this editor asks for it yet.
        raise HTTPException(404, f"{rel} does not exist — this endpoint only edits")

    # A browser hands back \r\n on a platform whose engine writes \n. Left
    # alone, the first save from the dashboard rewrites every line of the file
    # and buries the one-line change in a whole-file diff.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text.encode("utf-8")) > _MAX_WRITE:
        raise HTTPException(413, f"refusing to write more than {_MAX_WRITE} bytes")

    current = target.read_text(encoding="utf-8", errors="replace")
    if len(current) > _MAX_READ:
        raise HTTPException(409, "this file is served truncated and cannot be saved whole")

    base = payload.get("base_sha")
    if base and str(base) != _sha(current):
        raise HTTPException(409, {
            "message": "the file changed on disk since it was opened",
            "rel": rel, "sha": _sha(current)})

    held = _lock_holder(target)
    if held and not payload.get("force"):
        raise HTTPException(423, {
            "message": f"{rel} is locked by the {held.get('lock_seat')} seat",
            "rel": rel, "seat": held.get("lock_seat"),
            "owner": held.get("lock_owner")})

    if current == text:
        return {"written": False, "rel": rel, "sha": _sha(current),
                "bytes": len(text.encode("utf-8")), "backup": None,
                "unchanged": True}

    r = root()
    bdir = r / ".bgate_out" / "edits" / time.strftime("%Y%m%d-%H%M%S")
    # From the RESOLVED target, not the caller's `rel`. An absolute or
    # drive-qualified `rel` that still lands inside the project passes the
    # containment check above, and joining that raw string onto bdir would put
    # the backup somewhere else entirely — or overwrite it.
    backup = bdir / target.relative_to(p)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, backup)

    tmp = target.with_name(target.name + ".bgate-tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, target)

    try:
        from bgate_core import events
        events.emit(r, "file.edited", rel,
                    {"bytes": len(text.encode("utf-8")),
                     "forced": bool(held and payload.get("force"))})
    except Exception:
        pass
    # The Atlas graph is derived from exactly the files this just wrote, and it
    # is cached — without this, editing a script and switching to the map shows
    # the map from before the edit, which reads as a failed save.
    try:
        from bgate_core import screenmap
        screenmap.invalidate(r)
    except Exception:
        pass

    return {"written": True, "rel": rel, "sha": _sha(text),
            "bytes": len(text.encode("utf-8")),
            "backup": str(backup.relative_to(r)).replace("\\", "/"),
            "unchanged": False}


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
