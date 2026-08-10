"""The playtest notepad: typed evidence, on the recorder's clock.

A playtest captures what you SAY. That is the right default — talking costs no
hands while you are playing — but it is the only channel there has ever been,
and it fails in ways that are not rare: a shared room, a call, a dead mic, and
above all anything with a number or a name in it. Whisper does not hear
"armour 4 should be 40"; it hears "armor for should be forty" and files it as a
note nobody can act on.

These two endpoints let the player type it instead. What lands is not a new kind
of record — playtest.add_note writes the same transcript-segment-plus-feedback-
item pair a spoken remark produces, so the note flows through triage, promote,
merge, dismiss, the bug report and the QA queue with nothing downstream
adapted. See bgate_core.playtest's notepad section for why, and migration 0022
for the one column that distinguishes them.

The frame rides along as a base64 data URL because the browser is the only
thing that can produce it: the game runs as a WASM build in a same-origin
iframe, Godot's web export creates its WebGL2 context with
preserveDrawingBuffer:true, and a canvas readback is therefore both possible
and immediate. The server has no other way to see that frame until ffmpeg
finalises the mp4 at stop.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request

from bgate_core import playtest
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


def _number(payload: dict, key: str) -> Optional[float]:
    """A float from the body, or None. A malformed one is a 400, never a 500."""
    value = payload.get(key)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise api.bad_request(f"{key} must be a number, got {value!r}")


@router.post("/api/playtest/{session_id}/notes")
def add_note(session_id: int, payload: dict, request: Request = None) -> dict:
    """Write a typed note into the session.

    Body: {text, ts?, t?, kind?, seat?, frame?}

      ts     UNIX wall-clock seconds from the browser, captured at the instant
             the notepad OPENED — not when the note was saved. That distinction
             is the whole reason the field is sent rather than read off the
             server clock on arrival: you notice something, hit the hotkey, and
             then spend fifteen seconds typing. Stamped on arrival, every note
             would sit fifteen seconds downstream of the thing it describes,
             which is exactly where the frame and the telemetry are not.
      t      seconds from session start, for a note typed against a finished
             recording at the video playhead.
      frame  base64 image data URL of the game canvas at that same instant.

    An unusable frame does NOT fail the request — add_note keeps the words and
    reports frame_error. The note is the evidence; the picture is a bonus, and
    nobody can retype from memory what they just watched happen.
    """
    text = str(payload.get("text") or "")
    try:
        note = playtest.add_note(
            root(), session_id, text,
            t=_number(payload, "t"), ts=_number(payload, "ts"),
            kind=payload.get("kind") or None,
            seat=payload.get("seat") or None,
            frame=payload.get("frame") or None)
    except LookupError as exc:
        raise api.not_found(str(exc), session_id=session_id)
    except ValueError as exc:
        raise api.bad_request(str(exc), session_id=session_id)
    return api.ok(note, actor=api.current_actor(request))


@router.get("/api/playtest/{session_id}/notes")
def list_notes(session_id: int) -> dict:
    """The typed notes on this session, in clock order.

    The notepad polls this so a page reload mid-session does not look like the
    notes were lost — they are rows in the database the moment Enter is pressed,
    and this is what proves it.
    """
    try:
        return api.ok(playtest.list_notes(root(), session_id))
    except LookupError as exc:
        raise api.not_found(str(exc), session_id=session_id)
