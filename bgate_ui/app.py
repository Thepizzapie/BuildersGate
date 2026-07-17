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

app = FastAPI(title="builders-gate-ui", docs_url=None, redoc_url=None)

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


def serve(port: int = 7788) -> None:
    import uvicorn

    # 127.0.0.1 on purpose: this is a local window into a local store.
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
