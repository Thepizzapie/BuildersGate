"""The model editor's API — open a mesh, look at it from every angle, and pin
down where things attach.

spriteedit.py exists because generated sprite art lands 90% right and the fix
for the last 10% is painting the actual pixels, not re-rolling the generator.
The 3D pipeline (bgate_adapters/imageto3d.py, blender.py) has the identical
shape — a generated .glb is usually right and occasionally needs a human to
actually look at it — but there was nowhere in the dashboard to look. This is
that surface, for meshes instead of sheets.

Three things this owns, mirroring the boundaries spriteedit.py draws:

  * BYTES. The model file itself is never rewritten here — unlike a sprite
    sheet, a browser cannot re-encode a .glb into a form worth trusting back
    to disk, so this surface is read-only on the geometry. Look, don't paint.
  * LABELS. Camera framing, per-node visibility/tint, and named attachment
    SOCKETS go to the sidecar (bgate_core.modelmap), the 3D counterpart of
    rigmap's slot anchors — literally the same slot taxonomy, so "main_hand"
    means the same thing on a sprite sheet and on a character mesh.
  * PREVIEWS. A snapshot the browser renders can be saved as a PNG under
    .bgate_out — the same "keep evidence, do not touch the source" pattern
    sprite editing uses for backups, applied to a file this surface cannot
    otherwise produce a thumbnail for at all (see node_media.py's PREVIEWABLE
    comment: a .glb has never had one).

Everything is project-root-relative and refuses to escape it. Nothing here
talks to a model or spends money.
"""
from __future__ import annotations

import base64
import binascii
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import FileResponse

from bgate_core import modelmap
from bgate_ui import api
from bgate_ui.deps import root, safe_under

router = APIRouter()

# Container formats the browser-side viewer can actually load (three.js has a
# built-in loader for each). .fbx and .blend are real assets this project
# generates constantly (see bgate_adapters/blender.py) but three.js ships no
# loader for either — they are LISTED so the picker does not pretend they do
# not exist, just flagged unviewable rather than silently 404ing on open.
VIEWABLE = {".glb", ".gltf", ".obj"}
KNOWN = VIEWABLE | {".fbx", ".blend"}

# Companion files a viewable model can reference, and therefore the only
# other suffixes /api/model3d/raw will serve: a .gltf's external .bin buffer
# and textures, an .obj's .mtl and its textures. This endpoint walks an
# arbitrary caller-supplied relative path, so the allowlist — not the
# directory — is what stops it becoming a general file server.
RAW_SUFFIXES = {".glb", ".gltf", ".bin", ".obj", ".mtl",
                ".png", ".jpg", ".jpeg", ".webp", ".ktx2", ".basis"}

_MEDIA_TYPES = {
    ".glb": "model/gltf-binary", ".gltf": "model/gltf+json",
    ".bin": "application/octet-stream", ".obj": "text/plain",
    ".mtl": "text/plain", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".ktx2": "application/octet-stream", ".basis": "application/octet-stream",
}

# The picker walks the tree; a project with a huge export/ or .godot cache
# must not turn one dropdown into a minute of stat() calls. Same cap and same
# skip-list spriteedit.py uses, for the same reason.
SCAN_CAP = 6000
SKIP_DIRS = {".git", ".godot", "__pycache__", ".bgate_out", ".bgate",
             "node_modules", ".asset_work", "export", "build"}

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
SNAPSHOT_DIRNAME = "model_previews"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _model(rel: str) -> tuple[Path, Path]:
    """Resolve a known 3D model inside the project. Raises on anything else."""
    base = root()
    target = safe_under(base, rel)
    if target.suffix.lower() not in KNOWN:
        raise api.ApiError(415, "not a recognised 3D model format",
                           detail={"rel": rel, "known": sorted(KNOWN)})
    if not target.is_file():
        raise api.not_found(f"no model at {rel}", rel=rel)
    return base, target


def _res_path(project_root: Path, target: Path) -> Optional[str]:
    """The ``res://`` address of a file, if it lives inside the Godot project."""
    for cand in (project_root, project_root / "game"):
        if (cand / "project.godot").is_file():
            try:
                return f"res://{target.relative_to(cand).as_posix()}"
            except ValueError:
                continue
    return None


def _describe(project_root: Path, target: Path) -> dict:
    suffix = target.suffix.lower()
    return {
        "rel": target.relative_to(project_root).as_posix(),
        "name": target.name,
        "ext": suffix,
        "bytes": target.stat().st_size,
        "mtime": int(target.stat().st_mtime),
        "res_path": _res_path(project_root, target),
        "viewable": suffix in VIEWABLE,
        "raw_url": f"/api/model3d/raw/{target.relative_to(project_root).as_posix()}",
        "sidecar": modelmap.sidecar_path(target).relative_to(project_root).as_posix(),
        "model": modelmap.load(target),
        "known_slots": list(modelmap.KNOWN_SLOTS),
    }


@router.get("/api/model3d/open")
def model_open(rel: str) -> dict:
    """Everything the editor needs on load. The geometry comes from
    ``raw_url``, which GLTFLoader/OBJLoader fetch directly."""
    project_root, target = _model(rel)
    return _describe(project_root, target)


@router.get("/api/model3d/list")
def model_list(limit: int = 300, q: Optional[str] = None) -> dict:
    """Every 3D model in the project, newest first, viewable or not.

    Walks the tree itself rather than reusing the asset library scan, exactly
    like sprite_list: a hand-imported or Blender-exported model that never
    went through a generator has no artifact row, and those are precisely the
    files someone opens this editor to look at.
    """
    project_root = root()
    limit = max(1, min(int(limit), 2000))
    needle = (q or "").strip().lower()
    found = []
    scanned = 0
    for path in project_root.rglob("*"):
        if scanned >= SCAN_CAP:
            break
        if path.suffix.lower() not in KNOWN or not path.is_file():
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        scanned += 1
        rel = path.relative_to(project_root).as_posix()
        if needle and needle not in rel.lower():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append({
            "rel": rel, "name": path.name, "ext": path.suffix.lower(),
            "bytes": stat.st_size, "mtime": int(stat.st_mtime),
            "viewable": path.suffix.lower() in VIEWABLE,
            "annotated": modelmap.sidecar_path(path).is_file(),
        })
    found.sort(key=lambda d: d["mtime"], reverse=True)
    return {"models": found[:limit], "count": len(found[:limit]),
            "total": len(found), "truncated": len(found) > limit,
            "query": needle}


@router.get("/api/model3d/raw/{rel:path}")
def model_raw(rel: str) -> FileResponse:
    """The bytes of a model file or one of its companions.

    Path-shaped rather than a query param so a loader's own relative-URL
    resolution — a .gltf's ``buffer.bin`` and its textures sit beside it —
    works without the viewer having to rewrite every URI it reads out of the
    file: GLTFLoader resolves ``scene.bin`` against the URL it fetched
    ``scene.gltf`` from, and that only lands back inside this project because
    the two share a path prefix here too.
    """
    base = root().resolve()
    target = safe_under(base, rel)
    if target.suffix.lower() not in RAW_SUFFIXES:
        raise api.ApiError(415, "not a servable model asset",
                           detail={"rel": rel, "allowed": sorted(RAW_SUFFIXES)})
    if not target.is_file():
        raise api.not_found(f"no file at {rel}", rel=rel)
    media = _MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(target, media_type=media)


@router.post("/api/model3d/save")
def model_save(payload: dict) -> dict:
    """Save the sidecar — camera framing, node overrides, attachment sockets."""
    rel = str(payload.get("rel") or "")
    project_root, target = _model(rel)
    try:
        saved = modelmap.save(target, payload.get("model") or {})
    except modelmap.ModelError as exc:
        raise api.bad_request(str(exc), rel=rel)
    return api.ok({
        "rel": rel, "model": saved,
        "sidecar": modelmap.sidecar_path(target)
        .relative_to(project_root).as_posix(),
    })


@router.post("/api/model3d/reset")
def model_reset(payload: dict) -> dict:
    """Delete the sidecar — back to an unlabelled, unannotated model. The
    source file is never touched by this surface, so there is nothing else to
    revert."""
    rel = str(payload.get("rel") or "")
    _, target = _model(rel)
    removed = modelmap.delete(target)
    return api.ok({"rel": rel, "removed": removed, "model": modelmap.empty()})


@router.post("/api/model3d/snapshot")
def model_snapshot(payload: dict) -> dict:
    """Save a client-rendered PNG of the current view as the model's preview.

    The only reason this exists: a .glb has never had a thumbnail anywhere in
    this dashboard (see node_media.py's PREVIEWABLE comment), and the editor
    is the one place already holding a rendered frame of it. Written under
    .bgate_out — same convention sprite_backups uses — so it never collides
    with Godot's own importer and never gets mistaken for project content.
    """
    rel = str(payload.get("rel") or "")
    project_root, target = _model(rel)

    raw = str(payload.get("png") or "")
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    if not raw:
        raise api.bad_request("no image data in the payload")
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise api.ApiError(413, "snapshot too large",
                           detail={"bytes": len(raw), "max": MAX_SNAPSHOT_BYTES})
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise api.bad_request(f"snapshot is not valid base64: {exc}")
    if blob[:8] != _PNG_MAGIC:
        raise api.bad_request("snapshot must be a PNG")

    out_dir = project_root / ".bgate_out" / SNAPSHOT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"{target.stem}.{stamp}.png"
    out.write_bytes(blob)
    prev_rel = out.relative_to(project_root).as_posix()
    return api.ok({
        "rel": rel, "preview": prev_rel,
        # /api/preview already serves any root-relative .png — no need for a
        # second image-serving endpoint just for this one.
        "preview_url": f"/api/preview?rel={prev_rel}",
    })
