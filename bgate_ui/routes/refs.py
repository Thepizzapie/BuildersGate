"""Reference system endpoints: global project refs, per-task anchored refs, and
the same anchoring hung off a bible section.

Uploads come in as base64 in a JSON body on purpose — FastAPI's multipart form
handling needs python-multipart, and this build takes no new dependencies.
base64 is stdlib.
"""
from __future__ import annotations

import base64
import binascii
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException

from bgate_core import bible_refs as _bible_refs
from bgate_core import refs as _refs
from bgate_core import task_refs as _task_refs
from bgate_core.util import slugify
from bgate_ui.deps import root

router = APIRouter()

_DATA_URL = re.compile(r"^data:image/(?P<ext>[a-zA-Z0-9.+-]+);base64,(?P<b64>.+)$", re.S)
_EXT_OK = {"png", "jpg", "jpeg", "webp", "gif", "svg+xml", "svg"}


# ---- global project references --------------------------------------------

@router.get("/api/refs")
def refs_list(kind: str | None = None) -> dict:
    return {"refs": _refs.list_refs(root(), kind=kind)}


@router.post("/api/refs/pin")
def refs_pin(payload: dict) -> dict:
    """Pin a file already inside the project (by project-relative path) as a
    global reference."""
    r = root()
    rel = (payload.get("path") or "").strip()
    name = (payload.get("name") or "").strip()
    if not rel or not name:
        raise HTTPException(400, "name and path are required")
    src = (r / rel).resolve()
    try:
        src.relative_to(r.resolve())
    except ValueError:
        raise HTTPException(403, "path escapes the project root")
    if not src.is_file():
        raise HTTPException(404, f"no file at {rel}")
    return _refs.pin(r, name, str(src), kind=payload.get("kind", "style"),
                     note=payload.get("note", ""))


@router.post("/api/refs/upload")
def refs_upload(payload: dict) -> dict:
    """Pin an uploaded image (base64 data-URL or raw base64) as a global ref."""
    return _pin_upload(payload)


def _pin_upload(payload: dict) -> dict:
    """Decode a base64 upload and pin it. Shared by the global upload endpoint
    and the bible-section one — an anchor dropped onto a pillar is a pin like
    any other, and two copies of this decoding would be two places for the
    path-traversal guard below to be fixed in only one of."""
    r = root()
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    raw = (payload.get("data") or "").strip()
    ext = (payload.get("ext") or "png").lower().lstrip(".")
    m = _DATA_URL.match(raw)
    if m:
        ext = m.group("ext").lower()
        raw = m.group("b64")
    if ext not in _EXT_OK:
        raise HTTPException(415, f"unsupported image type {ext!r}")
    if ext == "svg+xml":
        ext = "svg"
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "data is not valid base64")
    if not blob:
        raise HTTPException(400, "empty image")
    # The name is user-supplied and was being pasted straight into a filename:
    # "../../.ssh/authorized_keys" wrote wherever it pleased. refs.pin slugifies
    # the pin name itself, so the only unsanitized surface was this staging
    # file — it gets the slug too, in a private mkdtemp nobody else can predict.
    if any(sep in name for sep in ("/", "\\")) or ".." in name:
        raise HTTPException(
            400, "name is a ref label, not a path — no separators or '..'")
    slug = slugify(name)
    staging = Path(tempfile.mkdtemp(prefix="bgate_upload_"))
    tmp = staging / f"{slug}.{ext}"
    try:
        tmp.write_bytes(blob)
        return _refs.pin(r, name, str(tmp), kind=payload.get("kind", "style"),
                         note=payload.get("note", ""))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@router.delete("/api/refs/{name}")
def refs_unpin(name: str) -> dict:
    try:
        return _refs.unpin(root(), name)
    except LookupError as exc:
        raise HTTPException(404, str(exc))


# ---- per-task anchored references ------------------------------------------

@router.get("/api/tasks/{item_id}/refs")
def task_refs_list(item_id: int) -> dict:
    r = root()
    return {"anchored": _task_refs.list_for_task(r, item_id),
            "resolved": _task_refs.resolve_for_task(r, item_id)}


@router.post("/api/tasks/{item_id}/refs")
def task_refs_add(item_id: int, payload: dict) -> dict:
    ref = (payload.get("ref") or "").strip()
    if not ref:
        raise HTTPException(400, "ref (a pin name or project-relative path) is required")
    try:
        return _task_refs.add(root(), item_id, ref,
                              kind=payload.get("kind", "style"),
                              note=payload.get("note", ""),
                              rank=int(payload.get("rank", 0)))
    except LookupError:
        raise HTTPException(404, f"'{ref}' does not resolve to a ref or file")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/api/tasks/{item_id}/refs")
def task_refs_remove(item_id: int, ref: str) -> dict:
    return _task_refs.remove(root(), item_id, ref)


@router.get("/api/tasks/{item_id}/refs/resolved")
def task_refs_resolved(item_id: int, kind: str | None = None) -> dict:
    """The layered set (task anchors first, then global) an art agent should
    condition on for this task."""
    return {"resolved": _task_refs.resolve_for_task(root(), item_id, kind=kind)}


# ---- bible-section anchored references --------------------------------------
#
# Same three verbs as the task block above, same payload keys, same failures.
# The only difference is what the anchor hangs off: a pillar or a canon
# reference instead of one work item.

@router.get("/api/bible/refs")
def bible_refs_all() -> dict:
    """Every anchored ref in the bible, keyed by section id (as a string, since
    that is what JSON object keys are). One request for the whole World view —
    a per-section fetch is N round trips to draw one page."""
    grouped = _bible_refs.list_all(root())
    return {"by_section": {str(k): v for k, v in grouped.items()}}


@router.get("/api/bible/refs/suggest")
def bible_refs_suggest() -> dict:
    """Pin names people typed INTO bible prose, matched against the real pins.

    Read-only and it stays that way: it proposes, a human accepts one section at
    a time by POSTing the anchor. See bible_refs.suggest_from_titles for why
    nothing here rewrites a title.
    """
    return {"suggestions": _bible_refs.suggest_from_titles(root())}


@router.get("/api/bible/{section_id}/refs")
def bible_refs_list(section_id: int) -> dict:
    r = root()
    return {"anchored": _bible_refs.list_for_section(r, section_id),
            "resolved": _bible_refs.resolve_for_section(r, section_id)}


@router.post("/api/bible/{section_id}/refs")
def bible_refs_add(section_id: int, payload: dict) -> dict:
    ref = (payload.get("ref") or "").strip()
    if not ref:
        raise HTTPException(400, "ref (a pin name or project-relative path) is required")
    try:
        return _bible_refs.add(root(), section_id, ref,
                               kind=payload.get("kind", "style"),
                               note=payload.get("note", ""),
                               rank=int(payload.get("rank", 0)))
    except LookupError as exc:
        # Either the section or the ref is missing — both are 404, and the
        # message says which so the caller is not left guessing.
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/api/bible/{section_id}/refs")
def bible_refs_remove(section_id: int, ref: str) -> dict:
    return _bible_refs.remove(root(), section_id, ref)


@router.post("/api/bible/{section_id}/refs/upload")
def bible_refs_upload(section_id: int, payload: dict) -> dict:
    """Pin an uploaded image AND anchor it to this section in one call.

    Two steps in the UI (pin it, then find it in the list and attach it) is two
    chances to end up with a pin nothing points at. base64-JSON body, same as
    /api/refs/upload — see the module docstring for why it is not multipart.
    """
    pinned = _pin_upload(payload)
    try:
        anchored = _bible_refs.add(root(), section_id, pinned["name"],
                                   kind=payload.get("kind", "style"),
                                   note=payload.get("note", ""),
                                   rank=int(payload.get("rank", 0)))
    except LookupError as exc:
        # The image IS pinned — say so, rather than reporting a bare failure
        # that makes the user upload it a second time.
        raise HTTPException(
            404, f"{exc} (the image was pinned as '{pinned['name']}' and can be "
                 "anchored once the section exists)")
    return {**anchored, "pin": pinned}
