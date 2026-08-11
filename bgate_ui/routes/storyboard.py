"""Storyboard endpoints: plan a scene, draw its frames, promote it to a cut.

WHY DRAWING IS A JOB AND EVERYTHING ELSE IS NOT. One board frame is an image
model call — tens of seconds, sometimes a minute. Held open on the request, the
browser times out and the user clicks again, which buys a second frame. So
frame generation goes through bgate_core.jobs like every other paid call in the
dashboard, and the rest (planning, attaching, reordering, promoting) answers
inline because it is a database write.

Uploads come in as base64 in a JSON body, matching routes/refs.py — FastAPI's
multipart handling needs python-multipart and this build takes no new
dependencies. base64 is stdlib.
"""
from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from bgate_core import jobs as _jobs
from bgate_core import storyboard as _sb
from bgate_core.util import slugify
from bgate_ui import api
from bgate_ui.deps import root
from bgate_ui.routes import jobs as _jobsroute

router = APIRouter()

FRAME_JOB = "storyboard_frame"

_DATA_URL = re.compile(
    r"^data:image/(?P<ext>[a-zA-Z0-9.+-]+);base64,(?P<b64>.+)$", re.S)
_EXT_OK = {"png", "jpg", "jpeg", "webp", "gif"}


def _refuse(exc: Exception) -> HTTPException:
    """A StoryboardError is a refusal the user should read, not a 500.

    These say things like "frame 3 has no image, approving it would mean the
    shot is bought against prose alone" — rendered as a server error that text
    reads as a crash, and the user retries instead of acting on it.
    """
    if isinstance(exc, _sb.StoryboardError):
        return HTTPException(400, str(exc))
    return HTTPException(500, f"{type(exc).__name__}: {exc}")


# ---- reading ---------------------------------------------------------------

@router.get("/api/storyboard/boards")
def storyboard_boards(limit: int = 100) -> dict:
    return api.ok({"boards": _sb.boards(root(), limit=limit)})


@router.get("/api/storyboard/board/{name}")
def storyboard_board(name: str) -> dict:
    try:
        return api.ok(_sb.board(root(), name))
    except Exception as exc:
        raise _refuse(exc) from exc


@router.get("/api/storyboard/styles")
def storyboard_styles() -> dict:
    """The same preset table the cutscene planner uses. One list, so a board and
    the sequence promoted from it cannot offer different looks."""
    from bgate_core import cinematic as _cine

    return api.ok({"styles": _cine.styles()})


# ---- planning (free) -------------------------------------------------------

@router.post("/api/storyboard/plan")
def storyboard_plan(payload: dict) -> dict:
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    frames = payload.get("frames")
    try:
        return api.ok(_sb.plan(
            root(), name,
            None if frames is None else list(frames),
            premise=payload.get("premise", ""),
            logline=payload.get("logline", ""),
            style=payload.get("style", ""),
            style_note=payload.get("style_note", ""),
            style_refs=payload.get("style_refs"),
            cast_refs=payload.get("cast_refs"),
            aspect_ratio=payload.get("aspect_ratio", "16:9")))
    except Exception as exc:
        raise _refuse(exc) from exc


@router.post("/api/storyboard/script")
def storyboard_script(payload: dict) -> dict:
    """Premise to script and beats. One cheap model call, answered inline —
    it is seconds, not the minute an image takes."""
    name = (payload.get("name") or "").strip()
    premise = (payload.get("premise") or "").strip()
    if not name or not premise:
        raise HTTPException(400, "name and premise are required")
    try:
        return api.ok(_sb.write_script(
            root(), name, premise,
            frames=int(payload.get("frames") or 6),
            style=payload.get("style", ""),
            style_note=payload.get("style_note", ""),
            cast_refs=list(payload.get("cast_refs") or []),
            characters=payload.get("characters", ""),
            aspect_ratio=payload.get("aspect_ratio", "16:9")))
    except Exception as exc:
        raise _refuse(exc) from exc


# ---- frames ----------------------------------------------------------------

@router.post("/api/storyboard/frame/generate")
def storyboard_frame_generate(payload: dict, request: Request = None) -> dict:
    """Draw one frame, in the background. Returns a job id to poll.

    The board and frame are validated BEFORE the job is created, so a typo
    answers 400 immediately instead of becoming a job that fails a minute later
    in a panel the user has already navigated away from.
    """
    r = root()
    name = (payload.get("name") or "").strip()
    try:
        idx = int(payload.get("idx"))
    except (TypeError, ValueError):
        raise HTTPException(400, "idx is required and must be a frame number")
    try:
        board = _sb.board(r, name)
    except Exception as exc:
        raise _refuse(exc) from exc
    if not any(int(f["idx"]) == idx for f in board["frames"]):
        raise HTTPException(404, f"board {board['name']!r} has no frame {idx}")

    opts = {
        "prompt": payload.get("prompt", ""),
        "provider": payload.get("provider", ""),
        "model": payload.get("model", ""),
        "refs": list(payload.get("refs") or []),
        "use_cast": bool(payload.get("use_cast", True)),
        "ref_strength": float(payload.get("ref_strength") or 0.5),
        "quality": payload.get("quality", "medium"),
    }

    def _work(_job_id: int) -> dict:
        return _sb.frame_generate(r, board["name"], idx, **opts)

    out = _jobsroute.start(FRAME_JOB, _work,
                           request_body={"board": board["name"], "idx": idx,
                                         **opts},
                           request=request)
    return {**out, "board": board["name"], "idx": idx}


@router.post("/api/storyboard/frame/attach")
def storyboard_frame_attach(payload: dict) -> dict:
    """Attach an image already in the project, or a pinned reference."""
    try:
        return api.ok(_sb.frame_attach(
            root(), (payload.get("name") or "").strip(),
            int(payload.get("idx")),
            image=payload.get("image", ""), ref=payload.get("ref", ""),
            approve=bool(payload.get("approve"))))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "name and idx are required") from exc
    except Exception as exc:
        raise _refuse(exc) from exc


@router.post("/api/storyboard/frame/upload")
def storyboard_frame_upload(payload: dict) -> dict:
    """Drop an image straight onto a frame. base64 data-URL or raw base64.

    The file lands inside the board's own directory rather than being pinned:
    a frame the author drew for THIS scene is not a project-wide reference, and
    pinning it would put a one-off panel in every seat's brief. Pin it through
    /api/refs/upload if it genuinely is a reference.
    """
    r = root()
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    try:
        idx = int(payload.get("idx"))
    except (TypeError, ValueError):
        raise HTTPException(400, "idx is required and must be a frame number")

    raw = (payload.get("data") or "").strip()
    ext = (payload.get("ext") or "png").lower().lstrip(".")
    match = _DATA_URL.match(raw)
    if match:
        ext = match.group("ext").lower()
        raw = match.group("b64")
    if ext == "jpeg":
        ext = "jpg"
    if ext not in _EXT_OK:
        raise HTTPException(415, f"unsupported image type {ext!r}")
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "data is not valid base64") from exc
    if not blob:
        raise HTTPException(400, "empty image")

    try:
        board = _sb.board(r, name)
        frame = next(f for f in board["frames"] if int(f["idx"]) == idx)
    except StopIteration as exc:
        raise HTTPException(404, f"no frame {idx} on this board") from exc
    except Exception as exc:
        raise _refuse(exc) from exc

    # The destination is built from the board's own slug and the frame index,
    # never from anything in the payload — the filename is not a user surface.
    slug = frame.get("slug") or f"frame{idx:02d}"
    rel = (f"{_sb.BOARD_DIRNAME}/{board['name']}/"
           f"{idx:02d}-{slugify(slug) or 'frame'}-drawn.{ext}")
    dest = Path(r) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)

    try:
        return api.ok(_sb.frame_attach(r, board["name"], idx, image=rel,
                                      approve=bool(payload.get("approve"))))
    except Exception as exc:
        raise _refuse(exc) from exc


@router.post("/api/storyboard/frame/set")
def storyboard_frame_set(payload: dict) -> dict:
    fields = {k: payload[k] for k in
              ("beat", "action", "camera", "dialogue", "duration", "note",
               "status", "slug") if k in payload and payload[k] is not None}
    try:
        return api.ok(_sb.frame_set(root(), (payload.get("name") or "").strip(),
                                    int(payload.get("idx")), **fields))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "name and idx are required") from exc
    except Exception as exc:
        raise _refuse(exc) from exc


@router.post("/api/storyboard/frame/add")
def storyboard_frame_add(payload: dict) -> dict:
    after = payload.get("after")
    try:
        return api.ok(_sb.frame_add(
            root(), (payload.get("name") or "").strip(),
            beat=payload.get("beat", ""), action=payload.get("action", ""),
            camera=payload.get("camera", ""),
            dialogue=payload.get("dialogue", ""),
            duration=payload.get("duration", 5),
            after=None if after is None else int(after)))
    except Exception as exc:
        raise _refuse(exc) from exc


@router.post("/api/storyboard/frame/cut")
def storyboard_frame_cut(payload: dict) -> dict:
    try:
        return api.ok(_sb.frame_cut(root(), (payload.get("name") or "").strip(),
                                    int(payload.get("idx"))))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "name and idx are required") from exc
    except Exception as exc:
        raise _refuse(exc) from exc


@router.post("/api/storyboard/reorder")
def storyboard_reorder(payload: dict) -> dict:
    try:
        return api.ok(_sb.frame_reorder(root(), (payload.get("name") or "").strip(),
                                        list(payload.get("order") or [])))
    except Exception as exc:
        raise _refuse(exc) from exc


# ---- the line --------------------------------------------------------------

@router.post("/api/storyboard/promote")
def storyboard_promote(payload: dict) -> dict:
    """Board to shot list. Everything after this costs video money."""
    try:
        return api.ok(_sb.promote(
            root(), (payload.get("name") or "").strip(),
            sequence_name=payload.get("sequence_name", ""),
            model=payload.get("model", ""),
            resolution=payload.get("resolution", "720p"),
            allow_unanchored=bool(payload.get("allow_unanchored"))))
    except Exception as exc:
        raise _refuse(exc) from exc


@router.post("/api/storyboard/delete")
def storyboard_delete(payload: dict) -> dict:
    try:
        return api.ok(_sb.delete(root(), (payload.get("name") or "").strip(),
                                 drop_images=bool(payload.get("drop_images"))))
    except Exception as exc:
        raise _refuse(exc) from exc


@router.get("/api/storyboard/jobs")
def storyboard_jobs(limit: int = 20) -> dict:
    return api.ok({"jobs": _jobs.list_jobs(root(), kind=FRAME_JOB, limit=limit)})
