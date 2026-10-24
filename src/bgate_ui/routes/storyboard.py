"""Storyboard endpoints: the dashboard reads boards here.

Planning, drawing, attaching, reordering and promoting go through the
storyboard_* MCP tools; nothing in the dashboard wrote a board, so those routes
are gone.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from bgate_core.cine import storyboard as _sb
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


def _refuse(exc: Exception) -> HTTPException:
    """A StoryboardError is a refusal the user should read, not a 500.

    These say things like "frame 3 has no image, approving it would mean the
    shot is bought against prose alone" — rendered as a server error that text
    reads as a crash, and the user retries instead of acting on it.
    """
    if isinstance(exc, _sb.StoryboardError):
        return HTTPException(400, str(exc))
    # Anything else is unanticipated, so its text stays out of the body — the
    # StoryboardError branch above is the one a human reads. See api.safe_error.
    return HTTPException(500, api.safe_error(exc))


@router.get("/api/storyboard/boards")
def storyboard_boards(limit: int = 100) -> dict:
    return api.ok({"boards": _sb.boards(root(), limit=limit)})


@router.get("/api/storyboard/board/{name}")
def storyboard_board(name: str) -> dict:
    try:
        return api.ok(_sb.board(root(), name))
    except Exception as exc:
        raise _refuse(exc) from exc
