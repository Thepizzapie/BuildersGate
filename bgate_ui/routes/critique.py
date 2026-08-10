"""The critique feed — what a seat just made, and what it said about it.

WHY THIS IS AN ENDPOINT AND NOT JUST A PANEL. The console draws a critique card
when new artwork lands, but that only exists while the Agents view is open in a
browser somebody is looking at, and it is welded to the dashboard's own layout.
A stream overlay is a different consumer with different needs: it runs in an OBS
browser source, it wants to own its own presentation (an entry animation, a
sprite, a lower third), and it must keep working when nobody has the dashboard
up at all.

So the data is served on its own, derived from the DATABASE rather than from the
console's per-request phase assembly. That matters for more than tidiness: the
console can only see artifacts belonging to items that are still RUNNING (the
phases it builds are for live agents), so a card missed while the tab was closed
is gone forever. This reads the artifact table, so it answers the same question
after the run has finished.

POLLED WITH A CURSOR, NOT DIFFED. ``since`` is the id of the last revision the
caller showed; the answer says plainly whether there is anything newer. An
overlay that has to compare payloads to work out if it should play its animation
will play it twice the first time the wording of a field changes.

    GET /api/critique              -> the newest, whatever it is
    GET /api/critique?since=41     -> {"new": false} until something beats 41
    GET /api/critique?limit=5      -> the recent run of them, newest first

Nothing here is dashboard-shaped: no HTML, no CSS classes, no assumptions about
where it is drawn. See docs/reference.md for the overlay recipe.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter

from bgate_core import artifacts as _artifacts
from bgate_core import db
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

# A bound on one poll. An overlay wants the newest one; a panel catching up
# wants a handful. Neither wants the project's whole art history, and a caller
# asking for it would be paying to serialise metadata it will not draw.
MAX_LIMIT = 25


def _seat_for(conn, work_item_id) -> str:
    """Which seat produced this, or "" when it was not filed against an item."""
    if not work_item_id:
        return ""
    try:
        row = conn.execute("SELECT seat FROM work_item WHERE id = ?",
                           (int(work_item_id),)).fetchone()
    except Exception:
        return ""
    return (row["seat"] if row else "") or ""


def _relative(project, raw: str) -> str:
    """A project-relative path, whatever shape it was stored in.

    NEVER LET AN ABSOLUTE PATH INTO THE URL. ``metadata.preview`` is written
    absolute by some generators and relative by others, and /api/preview happily
    accepts either — so this looks like housekeeping and is not. THIS FEED IS
    BUILT FOR A STREAM OVERLAY. An absolute path carries the machine's user name
    and directory layout ("C:\\Users\\<someone>\\Desktop\\<unreleased game>"),
    and it would ride into a browser source that is on camera, in its network
    panel, and in any debug output the overlay prints. The dashboard has a whole
    redaction mode devoted to keeping exactly that off the page; a new endpoint
    must not hand it back out through the side door.
    """
    text = str(raw or "").replace("\\", "/")
    if not text:
        return ""
    base = str(project).replace("\\", "/").rstrip("/")
    if text.lower().startswith(base.lower() + "/"):
        return text[len(base) + 1:]
    return text


def _card(project, conn, art: dict) -> dict:
    """One critique, in the shape a renderer actually needs.

    ``preview_url`` is included ready-made. Every consumer would otherwise
    rebuild the same /api/preview link by hand, and the one that gets the
    percent-encoding wrong breaks on the first path with a space in it.
    """
    meta = art.get("metadata") or {}
    qa = meta.get("qa_review") or {}
    rel = _relative(project, art.get("path") or "")
    # The per-revision archived render, when the generator kept one. `path` is
    # the shared live sheet that the NEXT generation overwrites, so for an older
    # revision it is the wrong picture — this is the only one that is stably
    # this revision. See artifacts.archived_render.
    shot = _relative(project, meta.get("preview") or "")
    return {
        "seq": int(art["id"]),
        "artifact_id": int(art["id"]),
        "logical_name": art.get("logical_name") or "",
        "revision": art.get("revision"),
        "kind": art.get("kind") or "",
        "status": art.get("status") or "",
        "producer": art.get("producer") or "",
        "path": rel,
        "preview_url": f"/api/preview?rel={quote(str(shot or rel), safe='')}",
        "work_item_id": art.get("work_item_id"),
        "seat": _seat_for(conn, art.get("work_item_id")),
        "created_at": art.get("created_at"),
        # THE CRITIQUE ITSELF. Absent verdict is not a failing one: an agent
        # that has not judged a candidate yet and an agent that judged it badly
        # must not render the same, so `verdict` is "" rather than "fail".
        "verdict": qa.get("verdict") or "",
        "score": int(qa.get("score") or 0),
        "note": (qa.get("reasons") or "")[:600],
        "reviewer": qa.get("actor") or "",
        # Whether a HUMAN has dispositioned it. An overlay may want to show the
        # ones still waiting differently from the ones already settled.
        "awaiting_human": (art.get("status") or "") == "candidate",
    }


@router.get("/api/critique")
def critique(since: Optional[int] = None, limit: int = 1) -> dict:
    """The newest generated artwork and the judgement attached to it.

    Built for a stream overlay: poll it, compare ``seq`` to what you last drew,
    play your own animation when it moves. ``since`` does that comparison
    server-side — pass the last ``seq`` you rendered and you get
    ``{"new": false}`` until there is genuinely something else to show.
    """
    project = root()
    try:
        want = max(1, min(int(limit or 1), MAX_LIMIT))
    except (TypeError, ValueError):
        want = 1
    conn = db.connect(project)
    # No status filter: an overlay wants to see the render, and whether a human
    # has since approved or rejected it is a field on the card, not a reason to
    # hide it. list_revisions already orders newest first.
    rows = _artifacts.list_revisions(project, limit=want if since is None else MAX_LIMIT)
    cards = [_card(project, conn, a) for a in rows]
    if since is not None:
        try:
            floor = int(since)
        except (TypeError, ValueError):
            floor = 0
        cards = [c for c in cards if c["seq"] > floor][:want]
        if not cards:
            # The cursor is echoed so a caller can keep using one variable, and
            # `new` is explicit so nobody has to infer it from an empty list.
            return api.ok({"new": False, "since": floor, "seq": floor,
                           "critiques": []})
    if not cards:
        return api.ok({"new": False, "seq": 0, "critiques": [],
                       "note": "this project has generated no artwork yet"})
    return api.ok({
        "new": True,
        "seq": cards[0]["seq"],
        "latest": cards[0],
        "critiques": cards,
    })
