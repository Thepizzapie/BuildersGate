"""Measured Theora, and the record that a human watched the cut.

The Cut tab used to answer "is this playable" from the file extension, which is
the one thing that cannot see the failure this project has actually shipped: a
libtheora build that writes .ogv files nothing can decode. These two endpoints
are what let that panel say *measured* instead of *assumed*.

Auto-registers via routes/__init__.py — no edit to app.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from bgate_core import cinecheck as _check
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


@router.get("/api/cinematic/theora")
def cinematic_theora(sequence: str = "", limit: int = 50) -> dict:
    """Every kept cut as ffprobe sees it, plus who has watched what.

    `untranscoded` is null rather than 0 when ffprobe is missing. A zero there
    would be the same claim as a real measurement and it is not one.
    """
    return api.ok(_check.survey(root(), sequence=sequence,
                                limit=max(1, min(int(limit), 200))))


@router.post("/api/cinematic/watched")
def cinematic_watched(payload: dict, request: Request) -> dict:
    """Record that somebody opened this cut and looked at it.

    The actor is recorded because the seat's gate is that a HUMAN watched it,
    and "the agent that made it watched it" is a different sentence.
    """
    body = payload or {}
    try:
        artifact_id = int(body.get("artifact_id"))
    except (TypeError, ValueError):
        raise api.bad_request("artifact_id is required") from None
    entry = _check.mark_watched(root(), artifact_id,
                                actor=api.current_actor(request))
    return api.ok({"artifact_id": artifact_id, **entry})
