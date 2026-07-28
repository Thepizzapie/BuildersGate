"""Everything in the project that can act as a visual reference.

The Reference node offered pinned refs and nothing else, which made the most
obvious anchors in a game unreachable: the sprite sheets already sitting in
``game/assets``. If you want a new animation to match the walk cycle you
already have, the walk cycle is the reference — and the picker could not see it.

Three sources, kept apart because they mean different things:

  pins       the curated set someone deliberately chose as canonical
  sheets     art the game actually loads today — the truth about the style
  artifacts  what the pipeline has produced, newest revision per logical name

The engine resolves all three (see workflows._resolve_ref_source); this is the
list a human picks from.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query

from bgate_core import artifacts as _artifacts
from bgate_core import refs as _refs
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

# Where a game keeps art. Scanning the whole project would drag in .godot
# import caches and every screenshot ever taken.
ART_DIRS = ("game/assets", "assets", "art")

# A picker is for choosing, not for browsing a filesystem. Past a few hundred
# entries it stops being either.
MAX_SHEETS = 250
MAX_ARTIFACTS = 120


def _rel(base: Path, path: Path) -> str:
    try:
        return str(path.relative_to(base)).replace("\\", "/")
    except ValueError:
        return ""


@router.get("/api/refs/sources")
def reference_sources(q: str = Query("", max_length=120)) -> dict:
    """Pins, sheets and artifacts a Reference node can point at."""
    base = Path(str(root())).resolve()
    needle = q.strip().lower()

    pins = []
    try:
        for pin in _refs.list_refs(base):
            name = str(pin.get("name") or "")
            if needle and needle not in name.lower():
                continue
            path = Path(str(pin.get("path") or ""))
            pins.append({
                "value": name, "label": name,
                "kind": str(pin.get("kind") or ""),
                "revision": pin.get("revision") or 1,
                "note": str(pin.get("note") or "")[:160],
                "rel": _rel(base, path),
            })
    except Exception:
        pins = []

    sheets = []
    for folder in ART_DIRS:
        start = base / folder
        if not start.is_dir():
            continue
        for path in sorted(start.rglob("*")):
            if len(sheets) >= MAX_SHEETS:
                break
            if path.suffix.lower() not in IMAGE_SUFFIXES or not path.is_file():
                continue
            rel = _rel(base, path)
            if not rel or (needle and needle not in rel.lower()):
                continue
            sheets.append({
                "value": rel, "label": path.stem, "rel": rel,
                # The folder is the useful discriminator: a dozen sheets share
                # a stem and differ only by which character they belong to.
                "note": str(path.parent.relative_to(base)).replace("\\", "/"),
                "bytes": path.stat().st_size,
            })

    arts = []
    try:
        seen = set()
        for rev in _artifacts.list_revisions(base, limit=400):
            name = str(rev.get("logical_name") or "")
            if not name or name in seen:
                continue
            if needle and needle not in name.lower():
                continue
            seen.add(name)
            path = Path(str(rev.get("path") or ""))
            arts.append({
                "value": name, "label": name,
                "revision": rev.get("revision") or 1,
                "status": str(rev.get("status") or ""),
                "rel": _rel(base, path),
            })
            if len(arts) >= MAX_ARTIFACTS:
                break
    except Exception:
        arts = []

    return api.ok({"pins": pins, "sheets": sheets, "artifacts": arts},
                  counts={"pins": len(pins), "sheets": len(sheets),
                          "artifacts": len(arts)})
