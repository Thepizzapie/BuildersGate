"""The tech seat's static reads: scene convention, generator contract, tree state.

Everything the tech workspace showed came from the engine — `godot_check_project`
and the export stamp — so between checks the panel had nothing to say, and the
two rules the seat is actually held to ("one editable thing = one named node",
"a tool that rewrites project data ships --check and defaults to dry") were
enforced by nobody and reported by nothing.

ONE ENDPOINT, NOT THREE. The three reads are one scan of the project tree and
one `git status`; splitting them would have the panel open three sockets to
answer one screen. They are cached briefly because a seat left open polls, and
re-walking a 121-scene project every few seconds to redraw four rows that change
when somebody saves a scene is work nobody asked for.
"""
from __future__ import annotations

import time

from fastapi import APIRouter

from bgate_core import gitwork, plumbing
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

# Long enough that a polling panel is nearly free, short enough that saving a
# scene and switching to the tab shows the new count.
_TTL = 15.0
_cache: dict[str, tuple[float, dict]] = {}


@router.get("/api/tech/plumbing")
def tech_plumbing() -> dict:
    r = root()
    key = str(r)
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < _TTL:
        return api.ok(hit[1])

    scenes = plumbing.scene_convention(r)
    generators = plumbing.generator_inventory(r)
    # The header chip says "dirty tree" or nothing at all. A repo git cannot
    # read is not a clean one, so `available` travels with the answer instead of
    # collapsing into False.
    tree = gitwork.dirty(r)
    body = {
        "scenes": scenes,
        "generators": generators,
        "git": {
            "available": tree.get("available", False),
            "dirty": tree.get("dirty", False),
            "changed": len(tree.get("paths") or []),
            "reason": tree.get("reason", ""),
        },
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)),
    }
    _cache[key] = (now, body)
    return api.ok(body)
