"""The dashboard backend — read-only over the project's SQLite store.

One page, polled JSON, no build step, no node, no CDN. The UI is a window, not a
control panel: every mutation stays in the MCP tools where it's attributable to
a seat. The only state this server holds is which project root it watches.

Run: bgate serve [--port 7788]   (from anywhere inside a project, or BGATE_ROOT)
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from bgate_core import activity, assets, bible, db, lore, playtest, project, seats
from bgate_core import queue as _queue
from bgate_ui import dispatch as _dispatch

app = FastAPI(title="builders-gate-ui", docs_url=None, redoc_url=None)


@app.middleware("http")
async def _coi_headers(request, call_next):
    """Cross-origin isolation on every response — the embedded WASM game build
    (/play) needs SharedArrayBuffer, which needs these on the whole origin."""
    response = await call_next(request)
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    return response

_STATIC = Path(__file__).with_name("static")

# Only ever serve images, and only from inside the project. The preview endpoint
# takes root-relative paths; anything that escapes the root is refused.
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".svg"}


def _root() -> Path:
    override = os.environ.get("BGATE_ROOT")
    if override:
        return Path(override)
    root = db.resolve_root()
    if root is None:
        raise HTTPException(503, "no .bgate project at or above the cwd — "
                                 "run the dashboard from inside a game project")
    return root


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (_STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/state")
def state() -> dict:
    """Everything the dashboard shows, one poll."""
    root = _root()
    conn = db.connect(root)

    try:
        proj = project.get(root)
    except LookupError:
        raise HTTPException(503, f"no project initialized at {root}")

    seat_table = seats.roles_for(root)
    locked = assets.list_assets(root, locked_only=True)
    locks_by_seat: dict[str, list] = {}
    for entry in locked:
        locks_by_seat.setdefault(entry["lock_seat"], []).append(
            {"path": entry["path"], "since": entry["lock_at"], "kind": entry["kind"]})

    latest_by_seat: dict[str, dict] = {}
    for event in activity.recent(root, limit=200):
        if event["seat"] and event["seat"] not in latest_by_seat:
            latest_by_seat[event["seat"]] = {
                "summary": event["summary"], "kind": event["kind"],
                "at": event["created_at"]}

    feedback_counts: dict[str, int] = {}
    for row in conn.execute(
            "SELECT seat, count(*) AS n FROM playtest_item "
            "WHERE status = 'promoted' GROUP BY seat"):
        feedback_counts[row["seat"]] = row["n"]

    previews_dir = root / ".bgate" / "previews"
    previews = []
    if previews_dir.is_dir():
        files = sorted(previews_dir.glob("*.png"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:24]
        previews = [{"rel": str(p.relative_to(root)).replace("\\", "/"),
                     "name": p.stem,
                     "mtime": int(p.stat().st_mtime)} for p in files]

    return {
        "project": proj,
        "root": str(root),
        "seats": [
            {
                **cfg,
                "locks": locks_by_seat.get(role, []),
                "last_activity": latest_by_seat.get(role),
                "promoted_feedback": feedback_counts.get(role, 0),
            }
            for role, cfg in seat_table.items()
        ],
        "assets": assets.list_assets(root),
        "verify": assets.verify(root),
        "bible": bible.overview(root),
        "lore": {
            "canon": lore.list_entities(root, status="canon"),
            "draft": lore.list_entities(root, status="draft"),
        },
        "sessions": playtest.list_sessions(root)[:10],
        "notes": seats.read_notes(root, limit=15),
        "previews": previews,
    }


@app.get("/api/activity")
def activity_feed(after_id: int = 0, limit: int = 60) -> dict:
    """The ticker. Poll with the last seen id for cheap incremental reads."""
    return {"events": activity.recent(_root(), limit=limit, after_id=after_id)}


@app.get("/api/preview")
def preview(rel: str) -> FileResponse:
    """Serve one image from inside the project. Root-relative paths only."""
    root = _root().resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(403, "path escapes the project root")
    if target.suffix.lower() not in _IMAGE_SUFFIXES:
        raise HTTPException(415, "images only")
    if not target.is_file():
        raise HTTPException(404, f"no image at {rel}")
    return FileResponse(target)


# ---------------------------------------------------------------------------
# The queue + dispatch: orchestration lives here now
# ---------------------------------------------------------------------------
@app.get("/api/queue")
def queue_list(status: Optional[str] = None) -> dict:
    root = _root()
    synced = _queue.sync_promoted(root)  # promoted playtest feedback flows in
    return {"items": _queue.list_items(root, status=status),
            "synced_from_playtest": synced["created"]}


@app.post("/api/queue")
def queue_add(payload: dict) -> dict:
    return _queue.add(_root(), payload["seat"], payload["title"],
                      brief=payload.get("brief", ""),
                      priority=int(payload.get("priority", 0)))


@app.post("/api/queue/{item_id}/dispatch")
def queue_dispatch(item_id: int, payload: Optional[dict] = None) -> dict:
    payload = payload or {}
    return _dispatch.dispatch(str(_root()), item_id,
                              model=payload.get("model") or None)


@app.post("/api/queue/{item_id}/stop")
def queue_stop(item_id: int) -> dict:
    return _dispatch.stop(item_id)


@app.post("/api/queue/import-orbit")
def queue_import_orbit() -> dict:
    return _queue.import_orbit(_root())


@app.get("/api/agents")
def agents() -> dict:
    return {"agents": _dispatch.status(str(_root()))}


@app.get("/api/agent-log/{item_id}")
def agent_log(item_id: int, tail: int = 60) -> dict:
    path = _root() / ".bgate" / "agents" / f"item-{item_id}.log"
    if not path.is_file():
        return {"lines": []}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"lines": lines[-tail:]}


# ---------------------------------------------------------------------------
# Play the game inside the app
# ---------------------------------------------------------------------------
@app.get("/play/{file_path:path}")
def play_files(file_path: str = "") -> FileResponse:
    """Serve the WASM build inside the dashboard origin (COI comes from the
    middleware). /play/ -> index.html."""
    root = _root().resolve()
    web = (root / "export" / "web").resolve()
    if not web.is_dir():
        raise HTTPException(404, "no web build — export it first (tech seat)")
    target = (web / (file_path or "index.html")).resolve()
    try:
        target.relative_to(web)
    except ValueError:
        raise HTTPException(403, "path escapes the build dir")
    if not target.is_file():
        raise HTTPException(404, file_path)
    return FileResponse(target)


def serve(port: int = 7788) -> None:
    import uvicorn

    # 127.0.0.1 on purpose: this is a local window into a local store.
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
