"""The person's side of the feel check: clear a failing scene.

A FEEL FAIL pauses art, level and gameplay dispatch (bgate_core.board.queue
FEEL_HELD_SEATS) until the scene passes or a person clears it here. Human-only:
an agent that could clear the gate would be grading its own design.

Auto-registers via routes/__init__.py. Envelope and errors per bgate_ui/api.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Request

from bgate_core.qa import feel as _feel
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


@router.post("/api/feel/clear")
def feel_clear(request: Request, payload: dict = Body(...)) -> dict:
    """{scene, reason}: waive the failing scene and close its FEEL FAIL item."""
    actor = api.current_actor(request)
    api.require_human(actor, "clear a failing feel check")
    try:
        return api.ok(_feel.clear(root(), str(payload.get("scene") or ""),
                                  by=actor or "human",
                                  reason=str(payload.get("reason") or "")))
    except ValueError as exc:
        raise api.bad_request(str(exc))
