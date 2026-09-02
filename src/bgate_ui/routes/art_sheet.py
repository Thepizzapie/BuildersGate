"""The art seat's Sheets panel, as one read.

The workspace used to assemble this client-side out of ``/api/artifacts`` and
guess the rest, which meant the per-frame measurement it wanted did not exist
anywhere and the panel drew a whole-sheet average over a six-frame strip. The
slicing and the auditing belong on this side of the wire — it is Pillow work
over a file the browser cannot even read.

Auto-registers via routes/__init__.py.
"""
from __future__ import annotations

from fastapi import APIRouter

from bgate_core.store import artifacts as _artifacts
from bgate_core.art import artsheet as _artsheet
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


@router.get("/api/art/sheet")
def art_sheet(logical_name: str = "", frames: bool = True) -> dict:
    """The sheet in progress, its per-frame audit, its measurements and its pin.

    ``logical_name`` pins the panel to one family instead of following whatever
    generated last — the seat wants both, because "what am I working on" and
    "what did the board just produce" are the same question until two agents are
    running.
    """
    r = root()
    rows = _artifacts.list_revisions(r, limit=200)
    if logical_name:
        rows = [a for a in rows if a.get("logical_name") == logical_name]
    picked = _artsheet.pick(rows)
    out = _artsheet.report(picked, root=r, slice_frames=frames)
    # The families are what a picker would offer. Cheap here, and the panel
    # cannot derive it without pulling the whole revision table itself.
    seen: list[str] = []
    for a in rows:
        name = a.get("logical_name")
        if (name and name not in seen
                and str(a.get("kind") or "") in _artsheet.ART_KINDS):
            seen.append(str(name))
    out["families"] = seen[:40]
    return api.ok(out)
