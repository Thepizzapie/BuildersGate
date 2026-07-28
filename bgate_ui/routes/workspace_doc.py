"""Generic per-seat JSON document endpoints.

Backs any seat workspace that persists free-form state: narrative storyboards,
the art flow map, sound cue sheets, qa bot rosters. One store, keyed (seat, key).
"""
from __future__ import annotations

from fastapi import APIRouter

from bgate_core import workspace as _ws
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


@router.get("/api/workspace/{seat}")
def ws_keys(seat: str) -> dict:
    return {"seat": seat, "docs": _ws.list_keys(root(), seat)}


@router.get("/api/workspace/{seat}/{key}")
def ws_get(seat: str, key: str) -> dict:
    return {"seat": seat, "key": key, "data": _ws.get(root(), seat, key)}


@router.post("/api/workspace/{seat}/{key}")
def ws_set(seat: str, key: str, payload: dict) -> dict:
    data = payload.get("data", payload)
    try:
        return _ws.set(root(), seat, key, data)
    except _ws.StaleWrite as exc:
        # A lost update is a 409 the UI can act on, not a 500. Both versions go
        # out so the page can say what it is about to overwrite instead of
        # silently eating the other tab's afternoon.
        raise api.conflict(str(exc), expected=exc.expected, actual=exc.actual)
