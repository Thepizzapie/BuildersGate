"""Reference system endpoints: global project refs + per-task anchored refs.

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
