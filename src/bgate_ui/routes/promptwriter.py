"""Improve a prompt, in place.

This started life as a whole node — a "prompt writer" you wired into a model
card. That was ceremony: the model card already has a prompt box, so a node
whose only job was to fill in that box added a wire, a run lifecycle, a status
and a failure mode to a problem that is one button.

Before that it was worse: an AGENT step, so clicking run queued a Claude Code
session with a seat and a lane hook to rewrite a sentence.

It is one call now, and it belongs next to the field it edits.
"""
from __future__ import annotations

from fastapi import APIRouter

from bgate_core.art import promptwriter as _pw
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


@router.get("/api/prompt")
def prompt_status() -> dict:
    """Is prompt improvement available? Presence of a key, no spend."""
    try:
        project = str(root())
    except Exception:
        project = ""
    return api.ok(_pw.available(project or None))


@router.post("/api/prompt/expand")
def expand(payload: dict) -> dict:
    """Rewrite a rough note into an image prompt.

    Cheap and synchronous on purpose — the caller is a person staring at a text
    box, so anything that returns a job id here is the wrong shape.
    """
    text = str(payload.get("text") or "").strip()
    if not text:
        raise api.bad_request("nothing to improve — write a rough note first")
    try:
        project = str(root())
    except Exception:
        project = ""
    written = _pw.expand(text,
                         subject=str(payload.get("subject") or ""),
                         task_kind=str(payload.get("task_kind") or ""),
                         root=project or None)
    if not written.get("ok"):
        raise api.ApiError(502, written.get("error", "prompt improvement failed"),
                           code="prompt_failed")
    return api.ok(written)
