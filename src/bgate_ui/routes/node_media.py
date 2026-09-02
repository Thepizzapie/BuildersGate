"""What a node SHOWS and what a node COSTS — in one batch read.

A workflow node used to describe its output ("6 frames, 2 variants") and say
nothing about the picture it made or the money it spent. Both facts already
existed — artifact revisions on disk, a spend ledger in SQLite, a price table in
the imagegen adapter — they were just never handed to the canvas.

This is the read side, deliberately ONE endpoint: ``renderBody`` runs on every
paint and must never do I/O, so the canvas fetches this once per load / run tick
and every node body reads the cache synchronously. A per-node endpoint would be
N requests per repaint.

Prices are ``imagegen.IMAGE_PRICE_USD`` verbatim — the same table the adapter
charges from — so an estimate on a node and the charge it turns into cannot
drift. Nothing here invents a number.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

from bgate_adapters import imagegen
from bgate_core.store import db
from bgate_core.board import spend
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

# Mirrors app.py's preview whitelist — a .glb or .blend has no thumbnail, and a
# node asking for one must get the empty state, not a broken <img>.
PREVIEWABLE = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}

MAX_NAMES = 60          # a canvas has tens of nodes, not thousands
MAX_CANDIDATES = 8      # the node shows a strip, not a gallery


def rel_for_preview(project_root: str | os.PathLike[str], path: str) -> str:
    """The root-relative, forward-slashed path ``/api/preview?rel=`` accepts.

    ``/api/preview`` REFUSES an absolute path (it joins onto the root and the
    join wins), so a stored path that happens to be absolute has to be brought
    back inside the project here or the node renders a broken image. Anything
    that escapes the root — an absolute path elsewhere on the machine, a
    ``..`` traversal — is refused with "" and the node falls back to its empty
    state. The return value is never absolute.
    """
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    base = Path(project_root).resolve()
    candidate = Path(raw)
    try:
        target = (candidate if candidate.is_absolute() else base / candidate).resolve()
        rel = target.relative_to(base)
    except (ValueError, OSError):
        return ""          # outside the project — refused, not normalised
    out = rel.as_posix()
    if not out or out.startswith("..") or Path(out).is_absolute():
        return ""
    return out


def _media(row: dict, project_root) -> dict:
    path = row.get("path") or ""
    suffix = Path(str(path)).suffix.lower()
    return {
        "artifact_id": int(row["id"]),
        "logical_name": row["logical_name"],
        "revision": int(row["revision"] or 1),
        "status": row["status"],
        "kind": row["kind"],
        "created_at": row["created_at"],
        # "" means: there is a revision but nothing showable — empty state.
        "rel": rel_for_preview(project_root, path) if suffix in PREVIEWABLE else "",
    }


def _revisions(project_root, names: list[str]) -> dict[str, list[dict]]:
    sql = ("SELECT id, logical_name, revision, path, kind, status, created_at "
           "FROM artifact_revision")
    params: list = []
    if names:
        sql += " WHERE logical_name IN (%s)" % ",".join("?" * len(names))
        params = list(names)
    sql += " ORDER BY logical_name, revision DESC, id DESC"
    out: dict[str, list[dict]] = {}
    try:
        rows = db.connect(project_root).execute(sql, params).fetchall()
    except Exception:
        # A project older than the artifact migration has no table. An empty
        # map is the honest answer; every node renders its empty state.
        return {}
    for row in rows:
        out.setdefault(row["logical_name"], []).append(dict(row))
    return out


def _spend_by_logical(project_root) -> dict[str, float]:
    """One grouped read instead of ``spend.for_logical`` per name — same sum."""
    try:
        rows = db.connect(project_root).execute(
            "SELECT logical_name, COALESCE(SUM(usd), 0) AS usd FROM spend_event "
            "WHERE logical_name != '' GROUP BY logical_name").fetchall()
    except Exception:
        return {}
    return {r["logical_name"]: round(r["usd"], 4) for r in rows}


@router.get("/api/node/media")
def node_media(names: Optional[str] = None, candidates: int = 4) -> dict:
    """Media + money for the logical assets a canvas is showing.

    ``names`` is a comma-separated list of logical asset names (the canvas sends
    what its nodes resolve to); omit it for everything the project knows.

    ``assets[name].latest`` is the newest revision with a previewable file —
    what a generator node paints once a run has produced something. ``candidates``
    is up to N of the newest (the strip a multi-candidate node shows). ``usd`` is
    what has ACTUALLY been spent on that asset (``spend.for_logical``), so a node
    can say "$0.14 spent" instead of guessing forever.
    """
    project = root()
    cap = max(1, min(int(candidates or 4), MAX_CANDIDATES))
    wanted = [n.strip() for n in (names or "").split(",") if n.strip()][:MAX_NAMES]

    by_name = _revisions(project, wanted)
    spent = _spend_by_logical(project)

    assets: dict[str, dict] = {}
    for name in (wanted or list(by_name)[:MAX_NAMES]):
        rows = by_name.get(name, [])
        media = [_media(r, project) for r in rows]
        showable = [m for m in media if m["rel"]]
        assets[name] = {
            "logical_name": name,
            "latest": showable[0] if showable else None,
            "candidates": showable[:cap],
            "revisions": len(media),
            "usd": spent.get(name, 0.0),
        }

    return api.ok({
        "prices": dict(imagegen.IMAGE_PRICE_USD),
        "default_quality": "medium",
        "assets": assets,
        # every logical name the project knows — the canvas uses it to tell a
        # name that has produced nothing yet from a name that is simply a typo
        "names": sorted(_known_names(project)),
        "spend": spend.totals(project),
    })


def _known_names(project_root) -> list[str]:
    try:
        rows = db.connect(project_root).execute(
            "SELECT DISTINCT logical_name FROM artifact_revision").fetchall()
    except Exception:
        return []
    return [r["logical_name"] for r in rows if r["logical_name"]]
