"""Playtest QA surface: editable evidence, filable bug reports, a live meter.

The QA audit found the evidence a playtest captures was effectively trapped —
you could hear the bug, see the frame, and still had to retype the whole thing
into a tracker by hand, with no field anywhere to record how to reproduce it.
And the seat that owns reproduction could not see the sessions at all.

Everything here is additive to the `/api/playtest/*` handlers in app.py. It
registers ahead of them (routes are included before app.py defines its own), so
the literal paths below win over `/api/playtest/{session_id}` rather than being
swallowed by its int coercion.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request, Response

from bgate_core.qa import playtest
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


def _fail(exc: Exception) -> api.ApiError:
    """Core raises LookupError/ValueError/RuntimeError; map, never leak a 500."""
    if isinstance(exc, LookupError):
        return api.not_found(str(exc))
    return api.bad_request(str(exc))


# ---------------------------------------------------------------------------
# 1. Notes + repro steps on a feedback item
# ---------------------------------------------------------------------------
@router.patch("/api/playtest/items/{item_id}")
def patch_item(item_id: int, payload: Optional[dict] = None,
               request: Request = None) -> dict:
    """Edit one feedback item: {notes, repro_steps, kind, seat, text}.

    Omitted fields are left alone — the QA seat writing repro steps must not
    clobber the director's re-routing of the same item.
    """
    payload = payload or {}
    fields = {k: payload[k] for k in playtest._ITEM_FIELDS if k in payload}
    if not fields:
        raise api.bad_request(
            "nothing to update — send at least one of "
            f"{list(playtest._ITEM_FIELDS)}")
    try:
        item = playtest.update_item(root(), item_id, **fields)
    except (LookupError, ValueError) as exc:
        raise _fail(exc)
    return api.ok(item, actor=api.current_actor(request))


# ---------------------------------------------------------------------------
# 2. Bug report export
# ---------------------------------------------------------------------------
@router.get("/api/playtest/{session_id}/report")
def session_report(session_id: int, format: str = "md",
                   window_s: float = 4.0, status: str = "promoted"):
    """A filable bug report per promoted item.

    format=md   markdown, for the copy button and for reading.
    format=zip  the markdown plus the frames it links, to attach to a ticket.
    status=all  include items nobody has promoted yet (raw, use with care).
    """
    if format not in ("md", "zip", "json"):
        raise api.bad_request(f"format must be md, zip or json, got {format!r}")
    statuses = None if status == "all" else tuple(
        s.strip() for s in status.split(",") if s.strip())
    try:
        if format == "zip":
            built = playtest.report_zip(root(), session_id, statuses=statuses,
                                        window_s=window_s)
            return Response(
                content=built["bytes"], media_type="application/zip",
                headers={"Content-Disposition":
                         f'attachment; filename="{built["filename"]}"'})
        built = playtest.report(root(), session_id, statuses=statuses,
                                window_s=window_s)
    except LookupError as exc:
        raise api.not_found(str(exc))
    if format == "md":
        return Response(content=built["markdown"],
                        media_type="text/markdown; charset=utf-8")
    return api.ok(built)


@router.get("/api/playtest/items/{item_id}/report")
def item_report(item_id: int, format: str = "md", window_s: float = 4.0):
    """One item's bug report — what the 'copy bug report' button copies."""
    if format not in ("md", "json"):
        raise api.bad_request(f"format must be md or json, got {format!r}")
    try:
        built = playtest.item_report(root(), item_id, window_s=window_s)
    except LookupError as exc:
        raise api.not_found(str(exc))
    if format == "md":
        return Response(content=built["markdown"],
                        media_type="text/markdown; charset=utf-8")
    return api.ok(built)


# ---------------------------------------------------------------------------
# 3. Live mic level
# ---------------------------------------------------------------------------
@router.get("/api/playtest/level")
def live_level(session_id: Optional[int] = None) -> dict:
    """Rolling peak/rms + silent_for_s for the session recording right now.

    Never raises: a dashboard poll must render "not recording" or "your mic is
    dead", not a red panel, whichever it is.
    """
    try:
        return api.ok(playtest.live_level(root(), session_id))
    except Exception as exc:                  # sounddevice absent, etc.
        return api.ok({"ok": False, "recording": False,
                       "reason": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------------------
# 4. Window picker for capture
# ---------------------------------------------------------------------------
@router.get("/api/playtest/windows")
def capture_windows(filter: str = "") -> dict:
    """Windows gdigrab can target, plus which one we would pick unattended.

    Without this the only capture target anyone could choose was the whole
    desktop — every frame of every bug report showing whatever else was open.
    """
    from bgate_adapters import recorder

    r = root()
    hints = playtest.game_window_hints(r)
    try:
        windows = recorder.list_windows(filter)
    except Exception as exc:
        return api.ok({"windows": [], "hints": hints, "suggested": None,
                       "reason": f"could not enumerate windows: {exc}"})
    suggested = None
    try:
        resolved = recorder.resolve_window(None, hints=hints)
        suggested = None if resolved["whole_desktop"] else resolved["title"]
        note = resolved["note"]
    except Exception as exc:
        note = str(exc)
    return api.ok({"windows": windows, "hints": hints,
                   "suggested": suggested, "note": note})


# ---------------------------------------------------------------------------
# 5. The QA seat's evidence stream
# ---------------------------------------------------------------------------
@router.get("/api/playtest/qa-queue")
def qa_queue(page: api.Page = Depends()) -> dict:
    """Sessions + untriaged feedback + promoted bugs still missing repro steps."""
    return api.ok(playtest.qa_queue(root(), limit=page.limit))


@router.post("/api/playtest/items/{item_id}/repro-check")
def repro_check(item_id: int, request: Request = None) -> dict:
    """Queue a parallel 'qa' item: reproduce this bug and write the steps down.

    Idempotent — a second click returns the item the first one made.
    """
    try:
        return api.ok(playtest.queue_repro_check(root(), item_id),
                      actor=api.current_actor(request))
    except (LookupError, ValueError) as exc:
        raise _fail(exc)
