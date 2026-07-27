"""Shared dependencies for the dashboard's route modules.

app.py historically held every endpoint. The per-seat workspaces add a lot of
new endpoints, so those live in bgate_ui/routes/*.py (auto-registered — see
routes/__init__.py). Anything a router needs from the app lives here to avoid a
circular import back into app.py.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException

from bgate_core import db

STATIC = Path(__file__).with_name("static")

# Only ever serve images, and only from inside the project (mirrors app.py).
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}


def root() -> Path:
    """The active project root — BGATE_ROOT, else walk up from cwd, else
    whatever `bgate use` last pointed at. Same order as the MCP server's
    _root(); the `bgate use` pointer is last so that running the dashboard from
    inside a project still means that project."""
    override = os.environ.get("BGATE_ROOT")
    if override:
        return Path(override)
    resolved = db.resolve_root()
    if resolved is None:
        from bgate_core import project
        resolved = project.active_root()
    if resolved is None:
        raise HTTPException(503, "no .bgate project at or above the cwd — "
                                 "run the dashboard from inside a game project, "
                                 "or pick one with `bgate use <dir>`")
    return resolved


def safe_under(root_dir: Path, rel: str, *, must_be_image: bool = False) -> Path:
    """Resolve a project-relative path, refusing anything that escapes root."""
    target = (root_dir / rel).resolve()
    try:
        target.relative_to(root_dir.resolve())
    except ValueError:
        raise HTTPException(403, "path escapes the project root")
    if must_be_image and target.suffix.lower() not in IMAGE_SUFFIXES:
        raise HTTPException(415, "not an image")
    return target
