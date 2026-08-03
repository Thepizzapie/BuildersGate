"""The asset library endpoint — families on disk, joined to review state.

Two sources, one payload, because they answer different halves of "is this
asset done":

  * bgate_core.library — what is ON DISK: which files form a family, which of
    them are sheets, which carry a rig sidecar, and which screens actually
    reach them (derived from the same scan Atlas uses).
  * artifacts.workspace — what the PIPELINE thinks: approved, candidate,
    rejected, how many revisions, which work item made it.

Neither is sufficient alone. An approved artifact referenced by no scene is not
shipping; a file the game loads every frame may have no artifact row at all
because a human drew it. Joining them on the file path is what lets one tile
say both things.

The scan is not cheap (it walks the tree and reads image headers), so it is
cached briefly and shared — the dashboard polls, and a per-poll rescan of a
few thousand files would be a tax on every other panel.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

from bgate_core import artifacts, library, screenmap
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

CACHE_TTL_S = 20.0
_cache: dict[str, tuple[float, dict]] = {}


def _scan(project_root: Path, force: bool) -> dict:
    key = str(project_root)
    hit = _cache.get(key)
    if hit and not force and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    data = library.scan(project_root,
                        smap=screenmap.scan_cached(project_root, force=force))
    _cache[key] = (time.time(), data)
    return data


def _review_index(project_root: Path) -> dict[str, dict]:
    """Root-relative path -> the review state of the group that owns it.

    Best effort: a project whose database has no artifacts (hand-drawn art, a
    fresh checkout) still has a full library, it just has nothing to review.
    """
    try:
        groups = artifacts.workspace(project_root)
    except Exception:
        return {}
    index: dict[str, dict] = {}
    for group in groups:
        revisions = group.get("revisions") or []
        status = ("approved" if group.get("approved")
                  else "review" if group.get("candidates")
                  else "rejected" if any(r.get("status") == "rejected"
                                         for r in revisions)
                  else "tracked")
        summary = {
            "logical_name": group["logical_name"],
            "status": status,
            "revisions": len(revisions),
            "candidates": len(group.get("candidates") or []),
            "feedback": len(group.get("feedback") or []),
        }
        for rev in revisions:
            path = str(rev.get("path") or "").replace("\\", "/")
            if path:
                index.setdefault(path, summary)
    return index


@router.get("/api/assets/library")
def assets_library(force: bool = False, category: Optional[str] = None,
                   q: Optional[str] = None) -> dict:
    """Every asset family, with sheets, usage, rig state and review state."""
    project_root = root()
    data = _scan(project_root, force)
    review = _review_index(project_root)

    needle = (q or "").strip().lower()
    families = []
    for fam in data["families"]:
        if category and fam["category"] != category:
            continue
        if needle and needle not in f"{fam['label']} {fam['dir']}".lower():
            continue
        fam = dict(fam)
        members = []
        statuses = set()
        for m in fam["members"]:
            m = dict(m)
            m["review"] = review.get(m["rel"])
            if m["review"]:
                statuses.add(m["review"]["status"])
            members.append(m)
        fam["members"] = members
        # One family, one headline status — worst-first, because "two of these
        # nine are still waiting on review" is the fact that should surface.
        fam["review_status"] = ("review" if "review" in statuses
                                else "rejected" if "rejected" in statuses
                                else "approved" if "approved" in statuses
                                else "tracked" if statuses else None)
        fam["reviewable"] = sum(1 for m in members if m["review"])
        families.append(fam)

    return {
        "families": families,
        "stats": {**data["stats"], "shown": len(families)},
        "truncated": data["truncated"],
        "map_error": data["map_error"],
        "cached_for_s": CACHE_TTL_S,
    }


@router.get("/api/assets/family")
def assets_family(key: str) -> dict:
    """One family in full. Same shape as a list entry — the detail IS the entry.

    Kept as its own endpoint so a drawer can refresh after an edit without
    re-scanning the whole tree into the client.
    """
    project_root = root()
    data = _scan(project_root, False)
    review = _review_index(project_root)
    fam = next((f for f in data["families"] if f["key"] == key), None)
    if fam is None:
        raise api.not_found("no such asset family", key=key)
    fam = dict(fam)
    fam["members"] = [{**m, "review": review.get(m["rel"])} for m in fam["members"]]
    return fam
