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
    SOCKETS go to the sidecar (bgate_core.three_d.modelmap), the 3D counterpart of
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
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import FileResponse

from bgate_core.three_d import modelmap
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
        # VERSIONED, so a rewritten file is a new URL. The loader fetches this
        # directly, and a browser that has seen the path before hands back the
        # old bytes: measured - the owner was re-exported three times and the
        # viewer kept drawing the model from a week earlier after a refresh.
        "raw_url": _raw_url(project_root, target),
        "sidecar": modelmap.sidecar_path(target).relative_to(project_root).as_posix(),
        "model": modelmap.load(target),
        "known_slots": list(modelmap.KNOWN_SLOTS),
    }


def _raw_url(project_root: Path, target: Path) -> str:
    """The geometry URL, stamped with the file's mtime and size."""
    rel = target.relative_to(project_root).as_posix()
    try:
        stat = target.stat()
        return f"/api/model3d/raw/{rel}?v={int(stat.st_mtime)}-{stat.st_size}"
    except OSError:
        return f"/api/model3d/raw/{rel}"


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
    # NO HEURISTIC CACHING. With Last-Modified and no Cache-Control a browser
    # may serve this from cache without asking, and a plain reload only
    # revalidates the document - not the .glb the loader fetches. Measured:
    # the owner was re-exported three times and the viewer kept drawing the
    # week-old model after every F5. no-cache = always revalidate (ETag).
    return FileResponse(target, media_type=media,
                        headers={"Cache-Control": "no-cache"})


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


# ── the draft-to-asset pipeline ───────────────────────────────────────────
#
# WHAT THIS IS FOR, in the generator's own words. bgate_adapters/krea.py says a
# generated mesh comes back as "geometry and texture with NO RIG, so it is a
# draft that still owes the pipeline a CLEAN, a SCALE, an ORIENTATION and a
# SKELETON before it is an asset", and generate_3d's success payload lists the
# same five next_steps. Every one of those was a Blender tool an operator had
# to go find; none of them was in the one surface that already had the model
# open. This section is those steps, wired to the viewer.
#
# THE BYTES RULE FROM THE MODULE DOCSTRING STILL HOLDS, with one door cut in
# it. Nothing here rewrites a file the caller did not name: a bake WRITES A NEW
# FILE, and replacing the original is an explicit flag that copies the original
# into .bgate_out/model_backups first. The browser still cannot re-encode a
# .glb — Blender does it, headless, through bgate_adapters.blender.run_script,
# which is the same path blender_combine and blender_rig already take.
#
# NOTHING IS REIMPLEMENTED HERE. The measurements are bg_stats / bg_shells /
# bg_nonmanifold / bg_axes / bg_facing and the transforms are bg_weld /
# bg_apply, all of them already in _blender_kit.KIT and spliced into every
# run_script(kit=True). What this file adds is the two scripts that call them
# and the JSON shape the panel reads.
#
# AXES ARE CONVERTED, because the two halves of this feature do not agree.
# Blender is Z-up with +Y forward (BG_FORWARD); the viewer, glTF and Godot are
# Y-up with -Z forward. Reporting Blender's triple to a panel drawn over a
# three.js scene would put the model's height in the field labelled "depth".
# _gltf3 does the swap ONCE, server-side, so every number below is in the same
# frame as the thing on screen.

BAKE_SUFFIX = ".baked.glb"
BACKUP_DIRNAME = "model_backups"
IMPORTABLE = {".glb", ".gltf", ".obj", ".fbx", ".blend"}

INSPECT_TIMEOUT = 300
BAKE_TIMEOUT = 900

# One Blender at a time. Each of these spawns a real headless process that
# reads a multi-megabyte mesh; two impatient clicks on a 20-second inspect
# should queue, not race two Blenders onto the same box. Held for the whole
# subprocess, which is why every caller below is a sync def — FastAPI runs
# those on the threadpool, so a blocked bake does not stall the event loop and
# the rest of the dashboard keeps polling.
_blender_lock = threading.Lock()

# Keyed by path + mtime + size, so a bake (new mtime) always re-measures and a
# re-open of an untouched model is free. Bounded because the picker can walk a
# project with hundreds of models and each entry holds a parts list.
_inspect_cache: "dict[str, dict]" = {}
_INSPECT_CACHE_MAX = 64

# Shared preamble. Both scripts import the same formats and speak the same
# axis convention, and a second copy of _gltf3 that disagreed by a sign would
# be a bug nobody could see.
_BLENDER_PRELUDE = r'''
import bpy, json, math, os, traceback
from mathutils import Matrix, Vector

P = json.loads(r"""__PAYLOAD__""")
UP = 2          # Blender Z
FWD = 1         # Blender +Y == BG_FORWARD == glTF -Z


def _tris(o):
    return sum(len(p.vertices) - 2 for p in o.data.polygons)


def _import(path, ext):
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".obj":
        try:
            bpy.ops.wm.obj_import(filepath=path)
        except AttributeError:
            bpy.ops.import_scene.obj(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)


def _gltf3(v):
    """A Blender xyz as the viewer/Godot sees it. Z-up +Y fwd -> Y-up -Z fwd."""
    return [round(v[0], 5), round(v[2], 5), round(-v[1], 5)]


def _world_box():
    """One AABB over every mesh, in Blender space. (lo, hi) or None."""
    bpy.context.view_layer.update()
    lo = [1e30, 1e30, 1e30]
    hi = [-1e30, -1e30, -1e30]
    seen = False
    for o in bpy.context.scene.objects:
        if o.type != "MESH" or not o.data.vertices:
            continue
        seen = True
        m = o.matrix_world
        for corner in o.bound_box:
            p = m @ Vector(corner)
            for i in range(3):
                lo[i] = min(lo[i], p[i])
                hi[i] = max(hi[i], p[i])
    return (lo, hi) if seen else None


def _measure():
    """The compact block the panel prints as before/after. Blender-free of
    opinions: counts, an AABB, and where the origin sits inside it."""
    box = _world_box()
    lo, hi = box if box else ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
    dims = [hi[i] - lo[i] for i in range(3)]
    centre = [(hi[i] + lo[i]) / 2.0 for i in range(3)]
    tris = verts = shells = nonmanifold = ngons = 0
    meshes = 0
    for o in bpy.context.scene.objects:
        if o.type != "MESH":
            continue
        meshes += 1
        tris += _tris(o)
        verts += len(o.data.vertices)
        shells += bg_shells(o)
        nonmanifold += bg_nonmanifold(o)
        ngons += sum(1 for f in o.data.polygons if len(f.vertices) > 4)
    gmin = _gltf3([lo[0], hi[FWD], lo[UP]])
    gmax = _gltf3([hi[0], lo[FWD], hi[UP]])
    gcen = _gltf3(centre)
    return {
        "meshes": meshes, "tris": tris, "verts": verts, "shells": shells,
        "nonmanifold": nonmanifold, "ngons": ngons,
        "height": round(dims[UP], 5),
        "dims": [round(dims[0], 5), round(dims[UP], 5), round(dims[FWD], 5)],
        "bbox_min": gmin, "bbox_max": gmax, "centre": gcen,
        # WHERE THE FILE'S OWN ZERO SITS INSIDE THE MESH, in viewer axes and
        # in metres: off the footprint centre in x and z, and ABOVE THE
        # LOWEST POINT in y. A y of 0.9 on a 1.8 m figure means the origin is
        # at the waist, which is the defect this reading exists to catch —
        # that mesh cannot be dropped on a floor tile.
        "origin_in_box": [round(-gcen[0], 5), round(-gmin[1], 5),
                          round(-gcen[2], 5)],
    }


def _write(report):
    with open(P["report"], "w", encoding="utf-8") as fh:
        json.dump(report, fh)
'''

# THE RIG KIT IS NOT IN THE DEFAULT PRELUDE. run_script appends
# blender._RIG_SOURCE only when it is exporting a .glb, and it appends it
# AFTER the caller's script, so a script that calls bgate_rig_dump at its own
# top level cannot see it. blender.combine solves this by prepending the same
# source itself; so does everything below that reads or writes a bone.
def _rig_source() -> str:
    return _blender()._RIG_SOURCE


_SKELETON_BODY = r'''
report = {"ok": False}
try:
    if P["ext"] != ".blend":
        bg_wipe()
        _import(P["path"], P["ext"])
    bpy.context.view_layer.update()
    kept, dropped = bgate_drop_shapes(list(bpy.context.scene.objects))
    arms = [o for o in kept if o.type == "ARMATURE"]
    meshes = [o for o in kept if o.type == "MESH"]

    # WHICH VERTICES EACH BONE ACTUALLY MOVES, not how many groups exist. A
    # bind that created all 23 vertex groups and filled none of them is the
    # documented failure (blender.rig: 64,878 of 64,878 unweighted with every
    # other check green), so a joint editor that showed the group list would
    # draw a skeleton attached to a mesh it does not touch.
    influence = {}
    for mesh in meshes:
        names = [g.name for g in mesh.vertex_groups]
        for vert in mesh.data.vertices:
            for entry in vert.groups:
                if entry.weight <= 0.0 or entry.group >= len(names):
                    continue
                key = names[entry.group]
                influence[key] = influence.get(key, 0) + 1

    out = []
    for arm in arms:
        world = arm.matrix_world
        deform = {b.name: bool(b.use_deform) for b in arm.data.bones}
        bones = []
        state = bgate_rig_enter(arm)
        try:
            for bone in arm.data.edit_bones:
                bones.append({
                    "name": bone.name,
                    "parent": bone.parent.name if bone.parent else "",
                    # glTF frame, so the panel can draw these straight over the
                    # mesh the viewer already loaded. Through the object's own
                    # matrix first: an armature that is not at the origin would
                    # otherwise draw its skeleton beside the body.
                    "head": _gltf3(world @ bone.head),
                    "tail": _gltf3(world @ bone.tail),
                    "roll": round(bone.roll, 6),
                    "length": round(bone.length, 6),
                    "connect": bool(bone.use_connect),
                    "deform": deform.get(bone.name, True),
                    "influences": influence.get(bone.name, 0),
                })
        finally:
            bgate_rig_leave(state)
        out.append({"name": arm.name, "bones": bones,
                    "at_origin": bool(world.translation.length < 1e-6)})

    report = {"ok": True, "armatures": out, "meshes": len(meshes),
              "dropped_shapes": dropped, "measure": _measure()}
except Exception as exc:
    report = {"ok": False, "error": str(exc),
              "traceback": traceback.format_exc()[-1500:]}
_write(report)
print("skeleton done")
'''


# Moving a joint invalidates the bind that was solved around the old one, so
# this is ONE script and not two: apply the edits, re-solve the weights in the
# same session, export. Split, a caller could ship a skeleton whose weights
# belong to a different skeleton.
_BONES_EDIT_BODY = r'''
report = {"ok": False}
try:
    if P["ext"] != ".blend":
        bg_wipe()
        _import(P["path"], P["ext"])
    bpy.context.view_layer.update()
    kept, dropped = bgate_drop_shapes(list(bpy.context.scene.objects))
    arms = [o for o in kept if o.type == "ARMATURE"]
    meshes = [o for o in kept if o.type == "MESH"]
    if not arms:
        raise RuntimeError("no armature in this file - fit a skeleton first")
    want = P.get("armature") or ""
    arm = next((a for a in arms if a.name == want), None) if want else arms[0]
    if arm is None:
        raise RuntimeError("no armature called %r; this file has %s"
                           % (want, ", ".join(a.name for a in arms)))

    into = arm.matrix_world.inverted()

    def _blend3(v):
        """The viewer's xyz back into Blender. Y-up -Z fwd -> Z-up +Y fwd."""
        return Vector((float(v[0]), -float(v[2]), float(v[1])))

    edits = P.get("bones") or {}
    applied, missing = [], []
    moved = 0.0
    state = bgate_rig_enter(arm)
    try:
        present = {b.name for b in arm.data.edit_bones}
        missing = sorted(n for n in edits if n not in present)
        for name in sorted(edits):
            bone = arm.data.edit_bones.get(name)
            if bone is None:
                continue
            spec = edits[name] or {}
            was_head, was_tail = bone.head.copy(), bone.tail.copy()
            if spec.get("head") is not None:
                target = into @ _blend3(spec["head"])
                moved = max(moved, (target - bone.head).length)
                bone.head = target
            if spec.get("tail") is not None:
                target = into @ _blend3(spec["tail"])
                moved = max(moved, (target - bone.tail).length)
                bone.tail = target
            if spec.get("roll") is not None:
                bone.roll = float(spec["roll"])
            # A ZERO-LENGTH BONE IS DELETED BY BLENDER when edit mode closes,
            # silently, and the export then carries a skeleton with a hole in
            # it. Refuse the whole edit rather than ship that.
            if bone.length < 1e-5:
                raise RuntimeError(
                    "%s would be %.6f m long; a bone that short is removed "
                    "when edit mode closes" % (name, bone.length))
            applied.append({
                "name": name,
                "head": _gltf3(arm.matrix_world @ bone.head),
                "tail": _gltf3(arm.matrix_world @ bone.tail),
                "roll": round(bone.roll, 6),
                "was_head": _gltf3(arm.matrix_world @ was_head),
                "was_tail": _gltf3(arm.matrix_world @ was_tail)})
    finally:
        bgate_rig_leave(state)

    report["applied"] = applied
    report["missing"] = missing
    report["max_move"] = round(moved, 6)
    report["armature"] = arm.name
    report["bones"] = len(arm.data.bones)

    # REBIND, AND PROVE IT THE ONLY WAY THAT WORKS. parent_set returns cleanly
    # and creates every vertex group whether or not one weight was written;
    # the unweighted count is the only witness. Same two-attempt ladder rig()
    # uses: heat first because it deforms properly, envelope as the fallback.
    if P.get("rebind") and meshes:
        attempts = []
        for how in ("ARMATURE_AUTO", "ARMATURE_ENVELOPE"):
            for mesh in meshes:
                for vg in mesh.vertex_groups[:]:
                    mesh.vertex_groups.remove(vg)
                for mod in [m for m in mesh.modifiers if m.type == "ARMATURE"]:
                    mesh.modifiers.remove(mod)
            bpy.ops.object.select_all(action="DESELECT")
            for mesh in meshes:
                mesh.select_set(True)
            arm.select_set(True)
            bpy.context.view_layer.objects.active = arm
            err = ""
            try:
                with bpy.context.temp_override(
                        object=arm, active_object=arm,
                        selected_objects=meshes + [arm],
                        selected_editable_objects=meshes + [arm]):
                    bpy.ops.object.parent_set(type=how)
            except Exception as exc:
                err = str(exc)[:160]
            bpy.context.view_layer.update()
            total = sum(len(m.data.vertices) for m in meshes)
            loose = sum(1 for m in meshes for v in m.data.vertices
                        if not v.groups)
            attempts.append({"bind": how, "verts": total, "unweighted": loose,
                             "pct": round(100.0 * loose / max(total, 1), 3),
                             "error": err})
            if not err and loose <= max(8, total * P["tolerance"]):
                break
        best = min(attempts, key=lambda a: a["unweighted"])
        report["attempts"] = attempts
        report["bound_with"] = best["bind"]
        report["unweighted"] = best["unweighted"]
        report["unweighted_pct"] = best["pct"]
        report["rebound"] = True
        report["rigged"] = bool(best["unweighted"] <=
                                max(8, best["verts"] * P["tolerance"]))
    else:
        report["rebound"] = False
        # The weights were solved around the OLD joint positions and are still
        # attached to them. Right for a roll or a tail tidy, wrong for a moved
        # head, and the caller is the one who knows which this was.
        report["rigged"] = None

    report["ok"] = True
except Exception as exc:
    report = {"ok": False, "error": str(exc),
              "traceback": traceback.format_exc()[-1500:]}
_write(report)
print("bones done")
'''


_INSPECT_SCRIPT = _BLENDER_PRELUDE + r'''
report = {"ok": False}
try:
    if P["ext"] != ".blend":
        bg_wipe()
        _import(P["path"], P["ext"])
    bpy.context.view_layer.update()
    scene = bpy.context.scene
    # THE IMPORTER'S BONE SHAPE IS NOT THE MODEL. io_scene_gltf2 builds a
    # 42-vertex "Icosphere" at the origin for any rigged .glb and leaves it
    # parentless (see blender.bgate_drop_shapes). Measured here: a 1.75 m
    # one-mesh character reported "2 / 3 objects, 2.713 m" and SCALE TO UNIT
    # offered to shrink him to fit - the runner drops it before export, this
    # panel never did.
    shapes = set()
    for rig in [o for o in scene.objects if o.type == "ARMATURE"]:
        for posed in rig.pose.bones:
            if posed.custom_shape is not None:
                shapes.add(posed.custom_shape.name)
    for o in [o for o in scene.objects
              if o.type == "MESH" and o.parent is None and o.name in shapes]:
        bpy.data.objects.remove(o, do_unlink=True)
    meshes = [o for o in scene.objects if o.type == "MESH"]
    arms = [o for o in scene.objects if o.type == "ARMATURE"]

    parts, no_uv, flipped, unapplied = [], 0, 0, 0
    for o in meshes:
        s = bg_stats(o)
        uv = len(o.data.uv_layers)
        if not uv:
            no_uv += 1
        if s.get("flipped", 0) > 0:
            flipped += s["flipped"]
        if any(abs(v - 1.0) > 1e-4 for v in o.scale):
            unapplied += 1
        if len(parts) < 40:
            parts.append({
                "name": o.name[:48], "tris": _tris(o), "verts": s["verts"],
                "shells": bg_shells(o), "nonmanifold": s["nonmanifold"],
                "ngons": s["ngons"], "loose": s["loose"],
                "flipped": s["flipped"], "uv_layers": uv,
                "materials": len(o.data.materials),
                "groups": len(o.vertex_groups),
                "dims": [round(s["dims"][0], 4), round(s["dims"][UP], 4),
                         round(s["dims"][FWD], 4)],
                "scale": [round(v, 4) for v in o.scale],
            })

    images = []
    for im in bpy.data.images:
        if im.name == "Render Result" or not im.users:
            continue
        images.append({"name": im.name[:40], "w": im.size[0], "h": im.size[1],
                       "channels": im.channels})
    images.sort(key=lambda d: -(d["w"] * d["h"]))

    skeletons = []
    for a in arms:
        names = [b.name for b in a.data.bones]
        deform = sum(1 for b in a.data.bones if b.use_deform)
        skeletons.append({"name": a.name[:40], "bones": len(names),
                          "deform_bones": deform, "bone_names": names[:24]})

    # bg_axes/bg_facing read ONE object, and they are the pipeline's own
    # readers — the same pair bg_orient consults before it agrees to rotate
    # anything. Given to the biggest mesh, which on a generated draft is the
    # subject and not a stray shell.
    biggest = max(meshes, key=_tris) if meshes else None
    axes = bg_axes(biggest, kind=P.get("kind", "humanoid")) if biggest else {}
    facing = bg_facing(biggest, axes) if biggest else {}

    m = _measure()
    report = {
        "ok": True, "measure": m, "parts": parts,
        "objects": len(scene.objects), "armatures": len(arms),
        "materials": len(bpy.data.materials),
        "images": images[:16], "image_count": len(images),
        "texture_pixels": sum(i["w"] * i["h"] for i in images),
        "skeletons": skeletons,
        "no_uv_meshes": no_uv, "flipped_faces": flipped,
        "unapplied_scale": unapplied,
        "vertex_groups": sum(len(o.vertex_groups) for o in meshes),
        "shape_keys": sum(1 for o in meshes
                          if o.data.shape_keys and
                          len(o.data.shape_keys.key_blocks) > 1),
        "animations": len(bpy.data.actions),
        "axes": {"certainty": axes.get("certainty"),
                 "forward_axis": axes.get("forward"),
                 "lateral_axis": axes.get("lateral"),
                 "kind": axes.get("kind")},
        "facing": {"sign": facing.get("sign"),
                   "strength": facing.get("strength"),
                   "confident": bool(facing.get("confident")),
                   "verdict": facing.get("verdict", "")},
    }
except Exception as exc:
    report = {"ok": False, "error": str(exc),
              "traceback": traceback.format_exc()[-1500:]}
_write(report)
print("inspect done")
'''

_BAKE_SCRIPT = _BLENDER_PRELUDE + r'''
OPS = P["ops"]
steps = []


def _roots():
    return [o for o in bpy.context.scene.objects if o.parent is None]


def _xform(M):
    """Compose a world transform onto every root. Children ride along on their
    parent's matrix, which is why this must not touch them: applying the same
    matrix to a parent and its child scales the child twice."""
    bpy.context.view_layer.update()
    for o in _roots():
        o.matrix_world = M @ o.matrix_world
    bpy.context.view_layer.update()


report = {"ok": False}
try:
    bg_wipe()
    _import(P["path"], P["ext"])
    bpy.context.view_layer.update()
    before = _measure()
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    rigged = any(o.type == "ARMATURE" for o in bpy.context.scene.objects)

    # ── CLEAN ─────────────────────────────────────────────────────────────
    # First, because every measurement after it changes. bg_weld's docstring
    # is the reason the merge distance is a fraction of the object's own
    # diagonal and not a constant: one setting has to serve a 0.2 m cap and a
    # 2 m figure, and merging harder manufactures the non-manifold edges that
    # stop the decimator later.
    if OPS.get("weld"):
        welded = {"shells_before": before["shells"], "meshes": 0}
        for o in meshes:
            r = bg_weld(o, fraction=float(OPS.get("weld_fraction") or 0.0006))
            welded["meshes"] += 1
            welded["last"] = r
        after_weld = _measure()
        welded["shells_after"] = after_weld["shells"]
        welded["nonmanifold_after"] = after_weld["nonmanifold"]
        steps.append({"op": "weld", **welded})

    if OPS.get("join") and len(meshes) > 1:
        was = len(meshes)
        joined = bg_join(meshes, meshes[0].name)
        meshes = [joined]
        steps.append({"op": "join", "into": joined.name[:40],
                      "was": was, "meshes_now": 1})

    budget = int(OPS.get("decimate") or 0)
    if budget > 0:
        got = []
        total = sum(_tris(o) for o in meshes) or 1
        for o in meshes:
            tris = _tris(o)
            share = max(1, int(budget * tris / total))
            if tris <= share:
                continue
            bg_only(o)
            mod = o.modifiers.new("bgate_decimate", "DECIMATE")
            mod.ratio = max(0.005, share / float(tris))
            bpy.ops.object.modifier_apply(modifier=mod.name)
            got.append({"mesh": o.name[:40], "from": tris, "to": _tris(o)})
        steps.append({"op": "decimate", "budget": budget, "meshes": got,
                      "tris_now": sum(_tris(o) for o in
                                      bpy.context.scene.objects
                                      if o.type == "MESH")})

    # ── SCALE TO UNIT ─────────────────────────────────────────────────────
    # The project convention is metres and 1.8 m is an adult (BG_HUMAN_HEIGHT
    # in _blender_base). A generated mesh arrives at whatever the generator
    # felt like, which is the single most common defect in the pipeline.
    height = float(OPS.get("height") or 0)
    if height > 0:
        box = _world_box()
        if box:
            was = box[1][UP] - box[0][UP]
            if was > 1e-9:
                k = height / was
                _xform(Matrix.Scale(k, 4))
                steps.append({"op": "scale", "from_height": round(was, 5),
                              "to_height": round(height, 5),
                              "factor": round(k, 6)})

    # ── ORIENT ────────────────────────────────────────────────────────────
    # Quarter turns about world up, which is what a generated mesh actually
    # needs — bg_orient's docstring: it arrives roughly axis-aligned and
    # turned by a quarter or a half. A tilt is not corrected and pretending
    # otherwise would rotate a crate onto a different footprint for no reason.
    turns = int(OPS.get("turns") or 0) % 4
    if turns:
        box = _world_box()
        pivot = Vector(((box[0][0] + box[1][0]) / 2.0,
                        (box[0][FWD] + box[1][FWD]) / 2.0, 0.0)) if box \
            else Vector((0.0, 0.0, 0.0))
        R = (Matrix.Translation(pivot) @
             Matrix.Rotation(math.radians(90.0 * turns), 4, "Z") @
             Matrix.Translation(-pivot))
        _xform(R)
        steps.append({"op": "orient", "turned_deg": 90 * turns})

    # ── ORIGIN / PIVOT ────────────────────────────────────────────────────
    # A mesh whose origin is in its chest cannot be dropped on a floor tile.
    # "feet" is the placement convention every Godot node uses: centred on the
    # footprint, sitting on y=0.
    origin = str(OPS.get("origin") or "keep")
    if origin in ("feet", "centre"):
        box = _world_box()
        if box:
            lo, hi = box
            cx = (lo[0] + hi[0]) / 2.0
            cy = (lo[FWD] + hi[FWD]) / 2.0
            cz = (lo[UP] + hi[UP]) / 2.0 if origin == "centre" else lo[UP]
            delta = Vector((-cx, -cy, -cz))
            _xform(Matrix.Translation(delta))
            steps.append({"op": "origin", "mode": origin,
                          "moved": _gltf3([delta[0], delta[FWD], delta[UP]])})

    # ── BAKE ──────────────────────────────────────────────────────────────
    # A transform left on the node is a transform Godot's importer, a collider
    # generator and every downstream check each get to interpret differently.
    # Baking it into the vertices makes the file say one thing.
    #
    # EXCEPT ON A RIGGED MESH. Applying a scale to mesh vertices while the
    # armature keeps its own leaves the bind matrices describing a body that
    # no longer exists. A rigged model keeps its node transform and this says
    # so out loud rather than quietly producing a broken skin.
    if OPS.get("bake", True) and not rigged and \
            (height > 0 or turns or origin in ("feet", "centre")):
        try:
            bpy.ops.object.select_all(action="DESELECT")
            for o in bpy.context.scene.objects:
                if o.type == "MESH":
                    o.select_set(True)
                    bpy.context.view_layer.objects.active = o
            bpy.ops.object.make_single_user(object=True, obdata=True)
            bpy.ops.object.transform_apply(location=True, rotation=True,
                                           scale=True)
            steps.append({"op": "bake", "into": "vertices"})
        except Exception as exc:
            steps.append({"op": "bake", "into": "node", "error": str(exc)})
    elif rigged:
        steps.append({"op": "bake", "into": "node",
                      "note": "armature present - transform stays on the "
                              "root node so the skin bind survives"})

    bpy.context.view_layer.update()
    report = {"ok": True, "before": before, "after": _measure(),
              "steps": steps, "rigged": rigged}
except Exception as exc:
    report = {"ok": False, "error": str(exc), "steps": steps,
              "traceback": traceback.format_exc()[-1500:]}
_write(report)
print("bake done")
'''


def _blender():
    """Imported at call time, not at module import. bgate_adapters.blender
    reaches for an executable and a temp dir; a dashboard that cannot find
    Blender must still serve every other route on this file."""
    from bgate_adapters import blender as mod
    return mod


def _run(script: str, payload: dict, timeout: int,
         export_glb: Optional[str] = None) -> dict:
    """One headless Blender, one JSON report.

    The report comes back through a FILE the caller names, not through the
    marked-stdout convention the other adapters use: _blender_runner truncates
    stdout to the last 4000 characters, and a per-mesh breakdown of a 40-piece
    generated draft goes past that without trying.
    """
    mod = _blender()
    fd, report_path = tempfile.mkstemp(prefix="bgate_minspect_", suffix=".json")
    os.close(fd)
    payload = dict(payload)
    payload["report"] = report_path.replace("\\", "/")
    body = script.replace("__PAYLOAD__", json.dumps(payload))
    try:
        with _blender_lock:
            res = mod.run_script(body, timeout=timeout, record=False,
                                 export_glb=export_glb)
        try:
            report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {"ok": False,
                      "error": (res.get("error")
                                or "Blender produced no report"),
                      "blender": (res.get("print") or "")[-800:]}
        report["seconds"] = res.get("seconds")
        if not res.get("ok") and report.get("ok"):
            report["ok"] = False
            report["error"] = res.get("error") or "Blender exited badly"
        return report
    finally:
        try:
            os.unlink(report_path)
        except OSError:
            pass


def _importable(target: Path) -> None:
    if target.suffix.lower() not in IMPORTABLE:
        raise api.ApiError(415, "Blender cannot open this format",
                           detail={"ext": target.suffix.lower(),
                                   "importable": sorted(IMPORTABLE)})


@router.get("/api/model3d/inspect")
def model_inspect(rel: str, kind: str = "humanoid", refresh: int = 0) -> dict:
    """Real numbers on the mesh: counts, an AABB in project units, shells,
    non-manifold edges, UVs, textures and whether there is a skeleton.

    Everything here is measured by Blender, because there is no other honest
    source. three.js can count triangles in the browser but it counts what the
    LOADER built — a shell count, a non-manifold edge and an unapplied object
    scale are all properties of the authored mesh, and the loader has already
    flattened or dropped every one of them by the time the viewer sees it.
    """
    project_root, target = _model(rel)
    _importable(target)
    stat = target.stat()
    key = f"{target}|{int(stat.st_mtime)}|{stat.st_size}|{kind}"
    if not refresh and key in _inspect_cache:
        return api.ok({**_inspect_cache[key], "cached": True})

    mod = _blender()
    avail = mod.available()
    if not avail.get("available"):
        raise api.ApiError(503, "Blender is not installed or not on the path",
                           detail=avail)

    report = _run(_INSPECT_SCRIPT, {
        "path": str(target).replace("\\", "/"),
        "ext": target.suffix.lower(), "kind": kind,
    }, INSPECT_TIMEOUT)
    if not report.get("ok"):
        raise api.ApiError(502, "Blender could not read this model",
                           detail={k: report.get(k) for k in
                                   ("error", "traceback", "blender")})
    out = {"rel": rel, "name": target.name, "bytes": stat.st_size,
           "res_path": _res_path(project_root, target),
           "human_height": 1.8, **report, "cached": False}
    if len(_inspect_cache) >= _INSPECT_CACHE_MAX:
        _inspect_cache.clear()
    _inspect_cache[key] = out
    return api.ok(out)


@router.post("/api/model3d/bake")
def model_bake(payload: dict) -> dict:
    """Clean / scale / orient / re-origin a draft, and write the result.

    WRITES A NEW FILE. The default output is ``<stem>.baked.glb`` beside the
    original, because a generated draft is worth keeping next to what it
    became — a bake that silently overwrote its input would leave nothing to
    compare against when the numbers came out wrong. ``replace`` overwrites the
    original and copies it into .bgate_out/model_backups first; that is the
    same keep-evidence convention model_snapshot and sprite_backups use.
    """
    rel = str(payload.get("rel") or "")
    project_root, target = _model(rel)
    _importable(target)
    ops = payload.get("ops") or {}
    if not isinstance(ops, dict):
        raise api.bad_request("ops must be an object")
    wanted = any([ops.get("weld"), ops.get("join"),
                  int(ops.get("decimate") or 0) > 0,
                  float(ops.get("height") or 0) > 0,
                  int(ops.get("turns") or 0) % 4,
                  str(ops.get("origin") or "keep") in ("feet", "centre")])
    if not wanted:
        raise api.bad_request("nothing to do - pick at least one operation")

    mod = _blender()
    avail = mod.available()
    if not avail.get("available"):
        raise api.ApiError(503, "Blender is not installed or not on the path",
                           detail=avail)

    replace = bool(payload.get("replace"))
    out_rel = str(payload.get("out") or "").strip()
    if out_rel:
        out_path = safe_under(project_root, out_rel)
        if out_path.suffix.lower() != ".glb":
            raise api.bad_request("the bake output must be a .glb")
    elif replace and target.suffix.lower() == ".glb":
        out_path = target
    else:
        out_path = target.with_name(target.stem + BAKE_SUFFIX)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Blender writes the .glb itself, and writing it straight over the file it
    # is still reading is not a thing that ends well. Every run exports to a
    # scratch path; the move onto the real one happens here, afterwards.
    scratch = tempfile.mkdtemp(prefix="bgate_bake_")
    staged = Path(scratch) / "baked.glb"
    try:
        report = _run(_BAKE_SCRIPT,
                      {"path": str(target).replace("\\", "/"),
                       "ext": target.suffix.lower(), "ops": ops},
                      BAKE_TIMEOUT, export_glb=str(staged))
        if not report.get("ok"):
            raise api.ApiError(502, "the bake failed inside Blender",
                               detail={k: report.get(k) for k in
                                       ("error", "traceback", "steps",
                                        "blender")})
        if not staged.is_file() or not staged.stat().st_size:
            raise api.ApiError(502, "Blender ran but exported nothing",
                               detail={"steps": report.get("steps")})

        backup_rel = None
        if out_path == target:
            backup_dir = project_root / ".bgate_out" / BACKUP_DIRNAME
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = backup_dir / f"{target.stem}.{stamp}{target.suffix}"
            shutil.copy2(target, backup)
            backup_rel = backup.relative_to(project_root).as_posix()
        shutil.move(str(staged), str(out_path))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    _inspect_cache.clear()
    rel_out = out_path.relative_to(project_root).as_posix()
    return api.ok({
        "rel": rel, "out": rel_out,
        "raw_url": _raw_url(project_root, out_path),
        "replaced": out_path == target, "backup": backup_rel,
        "bytes": out_path.stat().st_size,
        "before": report.get("before"), "after": report.get("after"),
        "steps": report.get("steps"), "rigged": report.get("rigged"),
        "seconds": report.get("seconds"),
    })


# ── the fifth debt: a skeleton ────────────────────────────────────────────
#
# The four above turn a draft into a clean, correctly-sized, correctly-facing
# mesh. It is still not a character: krea.py's list ends "weight to a skeleton,
# then combine or deliver", and until that happens the file cannot be animated,
# retargeted or dropped into an AnimationPlayer. Everything below is already
# implemented in bgate_adapters — this exposes the chain in the order it has to
# be run, because a rig nobody checked is the failure mode this pipeline
# actually produces: it binds, it reports success, and the elbow tears the
# first time something bends it.
#
#   fit      blender.rig            adopt, fit the 23-bone template, bind
#   weights  blender.weight_islands is a bone pulling vertices it should not
#   flex     blender.flex           bend it and LOOK, which is the only check
#                                   that catches what the numbers do not
#   engine   godot.retarget_check   will Godot's own retargeter take it
#
# None of it costs money. All of it is a local Blender or a local Godot.

RIG_SUFFIX = ".rigged.glb"
FLEX_DIRNAME = "model_flex"
RIG_TIMEOUT = 1200
WEIGHTS_TIMEOUT = 600
FLEX_TIMEOUT = 900
ANIM_SUFFIX = ".anim.glb"
ANIM_DIRNAME = "model_anim"
# Longer than the rig's, and deliberately: a run authors, exports and then
# RENDERS every clip from four views. Six clips at six proof frames is
# twenty-four renders before the support gate reads the file back.
ANIM_TIMEOUT = 2400
MAX_CLIPS = 12
MAX_PROOF_FRAMES = 24


@router.get("/api/model3d/rig_template")
def model_rig_template() -> dict:
    """The skeleton this pipeline fits, named bone by bone.

    Reads a file off disk and nothing else — no Blender, no cost — so the
    panel can say what "fit a skeleton" is about to produce BEFORE anyone
    commits twenty minutes of GPU to finding out.
    """
    mod = _blender()
    tpl = mod.humanoid_template()
    return api.ok({
        "ok": bool(tpl.get("ok")), "bones": tpl.get("bones") or [],
        "bone_count": len(tpl.get("bones") or []),
        "reason": tpl.get("reason") or "",
        "pose_clause": tpl.get("pose_clause") or "",
        # bg_axes' vocabulary, not a skeleton choice: it decides how the adopt
        # step reads the mesh's forward before it rotates anything. The
        # skeleton fitted is the humanoid one either way, which is worth
        # saying out loud in the panel.
        "kinds": ["humanoid", "long", "none"],
        "default_height": 1.8,
    })


@router.post("/api/model3d/rig")
def model_rig(payload: dict) -> dict:
    """Adopt, fit and bind a skeleton. Writes ``<stem>.rigged.glb``.

    Straight through to bgate_adapters.blender.rig, which is the same call
    blender_rig makes over MCP — the audit, the symmetry check and the
    coverage verdict all come back untouched, because a surface that
    summarised them would be deciding for the reader which failures matter.
    """
    rel = str(payload.get("rel") or "")
    project_root, target = _model(rel)
    _importable(target)
    mod = _blender()
    if not mod.available().get("available"):
        raise api.ApiError(503, "Blender is not installed or not on the path",
                           detail=mod.available())

    kind = str(payload.get("kind") or "humanoid")
    if kind not in ("humanoid", "long", "none"):
        raise api.bad_request("kind must be humanoid, long or none")
    height = float(payload.get("height") or 1.8)
    if not 0.05 <= height <= 50:
        raise api.bad_request("height must be between 0.05 and 50 metres")
    budget = max(0, int(payload.get("budget") or 0))
    out = target.with_name(target.stem + RIG_SUFFIX)

    with _blender_lock:
        report = mod.rig(str(target), str(out), kind=kind, height=height,
                         budget=budget, orient=bool(payload.get("orient", True)),
                         timeout=RIG_TIMEOUT)
    if not report.get("ok"):
        raise api.ApiError(502, "the rig failed inside Blender",
                           detail={k: report.get(k) for k in
                                   ("error", "reason", "adopt", "attempts")})
    _inspect_cache.clear()
    out_rel = (out.relative_to(project_root).as_posix() if out.is_file()
               else None)
    return api.ok({
        "rel": rel, "out": out_rel,
        "bytes": out.stat().st_size if out.is_file() else 0,
        "rigged": bool(report.get("rigged")),
        "bones": report.get("bones"),
        "bone_names": report.get("bone_names") or [],
        "bound_with": report.get("bound_with"),
        "unweighted": report.get("unweighted"),
        "unweighted_pct": report.get("unweighted_pct"),
        "adopt": report.get("adopt"), "audit": report.get("audit"),
        "fit": report.get("fit"), "coverage": report.get("coverage"),
        "attempts": report.get("attempts") or [],
        "reason": report.get("reason") or "",
        "seconds": report.get("seconds"),
    })


@router.post("/api/model3d/weights")
def model_weights(payload: dict) -> dict:
    """Is any bone pulling vertices it has no business pulling.

    weight_islands counts, per bone, how many DISCONNECTED islands of mesh it
    influences. One is correct. Several means the bind heat jumped a gap — a
    thigh bone that also owns a patch of the other thigh — and that is the
    defect that only shows up when something bends.
    """
    rel = str(payload.get("rel") or "")
    _, target = _model(rel)
    _importable(target)
    mod = _blender()
    if not mod.available().get("available"):
        raise api.ApiError(503, "Blender is not installed or not on the path",
                           detail=mod.available())
    with _blender_lock:
        report = mod.weight_islands(str(target),
                                    threshold=float(payload.get("threshold")
                                                    or 0.02),
                                    timeout=WEIGHTS_TIMEOUT)
    if not report.get("ok"):
        raise api.ApiError(502, "the weight check failed inside Blender",
                           detail={"error": report.get("error")})
    verdict = mod.weight_islands_verdict(
        report, min_bleed_vertices=int(payload.get("min_bleed_vertices") or 3))
    bones = report.get("bones") or {}
    # Worst first: the panel has room for a handful of rows and the handful
    # worth showing is not the alphabetical one.
    worst = sorted(
        ({"bone": name, **stats} for name, stats in bones.items()),
        key=lambda b: (-int(b.get("islands") or 0),
                       -int(b.get("bleed_vertices") or 0)))[:12]
    return api.ok({
        "rel": rel, "verdict": verdict, "worst": worst,
        "deform_bones": report.get("deform_bones"),
        "mesh_shells": report.get("mesh_shells"),
        "bone_count": len(bones), "seconds": report.get("seconds"),
    })


@router.post("/api/model3d/flex")
def model_flex(payload: dict) -> dict:
    """Bend it and look. Six poses, rendered, with a verdict per pose.

    THE ONE CHECK THAT IS A PICTURE. Weight numbers say a bone owns two
    islands; they do not say the shoulder collapses. flex drives each joint to
    a real angle, measures volume loss, pinching and new self-intersections,
    and renders the result — so a bad bind stops being a statistic. Renders go
    under .bgate_out, the same place snapshots do, and come back as
    /api/preview URLs the panel can just show.
    """
    rel = str(payload.get("rel") or "")
    project_root, target = _model(rel)
    _importable(target)
    mod = _blender()
    if not mod.available().get("available"):
        raise api.ApiError(503, "Blender is not installed or not on the path",
                           detail=mod.available())
    out_dir = project_root / ".bgate_out" / FLEX_DIRNAME / target.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    with _blender_lock:
        report = mod.flex(str(target), str(out_dir), stem="flex",
                          render=bool(payload.get("render", True)),
                          timeout=FLEX_TIMEOUT)
    if not report.get("ok"):
        raise api.ApiError(502, "the deform test failed inside Blender",
                           detail={"error": report.get("error"),
                                   "rest": report.get("rest")})
    poses = []
    for pose in report.get("poses") or []:
        shot = pose.get("render")
        url = None
        if shot:
            try:
                shot_rel = Path(shot).relative_to(project_root).as_posix()
                url = f"/api/preview?rel={shot_rel}"
            except ValueError:
                url = None
        poses.append({**pose, "render_url": url})
    return api.ok({"rel": rel, "rest": report.get("rest"), "poses": poses,
                   "verdict": report.get("verdict"),
                   "seconds": report.get("seconds")})


BONES_SUFFIX = ".bones.glb"
SKELETON_TIMEOUT = 300
BONES_TIMEOUT = 1800
# Two endpoints closer together than this are ONE joint. Blender writes a
# child's head and its parent's tail to the same coordinate when the bones are
# connected, and a rig that survived a glTF round trip carries them back at
# float precision rather than bit-identical.
JOINT_EPS = 1e-4
# A drag further than this many times the model's height is a slip, not an
# edit. The gizmo works in world units and a stray drag against a distant
# camera plane can throw a wrist into the next postcode.
MAX_JOINT_MOVE = 0.5


def _number(payload: dict, key: str, default):
    """A numeric field, where ZERO IS A VALUE AND NOT AN ABSENCE.

    `float(payload.get(k) or d)` reads naturally and is wrong for every field
    whose zero means something: min_bleed 0 became 3, threshold 0 became 0.02
    and fps 0 became 30, so a caller asking for the one thing the bound check
    exists to refuse got a silent substitution instead of a 400.
    """
    value = payload.get(key)
    if value is None or value == "":
        return default
    try:
        return type(default)(value)
    except (TypeError, ValueError):
        raise api.bad_request(f"{key} is not a number", **{key: value}) from None


def _joints(bones: list) -> list:
    """Bone endpoints grouped into the joints a person actually drags.

    A SKELETON EDITOR THAT MOVED ONE BONE'S HEAD WOULD TEAR THE RIG. An elbow
    is the forearm's head AND the upper arm's tail, written to the same
    coordinate; moving one of the two leaves a gap the exporter keeps and the
    engine renders as a limb that comes apart when it bends. So the unit of
    editing here is the joint, and every endpoint sitting on it moves together.
    """
    joints: list = []
    for bone in bones:
        for end in ("head", "tail"):
            point = bone.get(end) or [0.0, 0.0, 0.0]
            hit = None
            for joint in joints:
                if all(abs(joint["position"][i] - point[i]) <= JOINT_EPS
                       for i in range(3)):
                    hit = joint
                    break
            if hit is None:
                hit = {"id": "j%d" % len(joints),
                       "position": [round(float(c), 6) for c in point],
                       "ends": []}
                joints.append(hit)
            hit["ends"].append({"bone": bone["name"], "end": end})
    for joint in joints:
        # The name a person recognises. A joint is usually one bone's head and
        # its parent's tail; the head owns the name (an elbow is where the
        # forearm starts), and a pure tail is a leaf tip.
        heads = [e["bone"] for e in joint["ends"] if e["end"] == "head"]
        joint["label"] = heads[0] if heads else (
            joint["ends"][0]["bone"] + " tip")
        joint["leaf"] = not heads
    return joints


@router.get("/api/model3d/skeleton")
def model_skeleton(rel: str) -> dict:
    """Every bone, where it starts and ends, and what it actually moves.

    THE FIRST THING IN THIS PRODUCT THAT TREATS THE SKELETON AS EDITABLE. The
    rig step fits a fixed 23-bone template and the panel could report how many
    bones came back; where any one of them SAT was answerable only by opening
    Blender. Positions come back in the viewer's own frame (glTF, Y-up, -Z
    forward), so the panel draws them over the mesh it already loaded without
    a second opinion about which way is up.

    `influences` per bone is vertices actually weighted to it, not the size of
    the vertex-group list: a bind that made every group and filled none of
    them is the failure this pipeline has measured, and a skeleton drawn as
    connected to a mesh it does not move would hide exactly that.
    """
    project_root, target = _model(rel)
    _importable(target)
    mod = _blender()
    if not mod.available().get("available"):
        raise api.ApiError(503, "Blender is not installed or not on the path",
                           detail=mod.available())
    report = _run(_rig_source() + "\n" + _BLENDER_PRELUDE + _SKELETON_BODY,
                  {"path": str(target), "ext": target.suffix.lower()},
                  SKELETON_TIMEOUT)
    if not report.get("ok"):
        raise api.ApiError(502, "reading the skeleton failed inside Blender",
                           detail={"error": report.get("error")})
    arms = report.get("armatures") or []
    for arm in arms:
        arm["joints"] = _joints(arm.get("bones") or [])
    height = float(((report.get("measure") or {}).get("height")) or 0.0)
    return api.ok({
        "rel": rel, "armatures": arms, "count": len(arms),
        "height": height,
        # What a drag is allowed to be, decided from the model rather than
        # from a constant that means something different on a mug and a tower.
        "max_move": round(height * MAX_JOINT_MOVE, 4) if height else 0.0,
        "seconds": report.get("seconds"),
    })


def _bone_edits(payload: dict, limit: float) -> dict:
    """Turn the panel's joint drags into per-bone head/tail writes.

    Validated here rather than in Blender for the usual reason: a NaN in a
    coordinate is worth a 400 in a millisecond, and inside Blender it is a
    corrupted armature exported over a file the caller wanted kept.
    """
    raw = payload.get("bones")
    if not isinstance(raw, dict) or not raw:
        raise api.bad_request("bones must be a non-empty object keyed by name")
    if len(raw) > 512:
        raise api.bad_request("too many bones in one edit", asked=len(raw))
    out: dict = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise api.bad_request("a bone edit needs a bone name")
        if not isinstance(spec, dict):
            raise api.bad_request(f"the edit for {name!r} must be an object")
        clean: dict = {}
        for end in ("head", "tail"):
            if spec.get(end) is None:
                continue
            point = spec[end]
            if (not isinstance(point, (list, tuple)) or len(point) != 3):
                raise api.bad_request(f"{name}.{end} must be [x, y, z]",
                                      bone=name)
            try:
                triple = [float(c) for c in point]
            except (TypeError, ValueError):
                raise api.bad_request(f"{name}.{end} is not three numbers",
                                      bone=name) from None
            for c in triple:
                # NaN fails every comparison including its own, which is what
                # makes it worth naming: a bound check alone would let it past.
                if c != c or abs(c) > 1e6:
                    raise api.bad_request(
                        f"{name}.{end} is not a finite coordinate", bone=name)
            clean[end] = triple
        if spec.get("roll") is not None:
            try:
                roll = float(spec["roll"])
            except (TypeError, ValueError):
                raise api.bad_request(f"{name}.roll is not a number",
                                      bone=name) from None
            if roll != roll or abs(roll) > 100:
                raise api.bad_request(f"{name}.roll is out of range", bone=name)
            clean["roll"] = roll
        if not clean:
            raise api.bad_request(f"the edit for {name!r} changes nothing",
                                  bone=name)
        was = spec.get("was") or {}
        for end in ("head", "tail"):
            if end in clean and isinstance(was.get(end), (list, tuple)) \
                    and len(was[end]) == 3 and limit > 0:
                try:
                    delta = sum((float(clean[end][i]) - float(was[end][i])) ** 2
                                for i in range(3)) ** 0.5
                except (TypeError, ValueError):
                    continue
                if delta > limit:
                    raise api.bad_request(
                        f"{name}.{end} moved {delta:.3f} m, past the "
                        f"{limit:.3f} m this model allows in one edit",
                        bone=name, moved=round(delta, 4), limit=limit)
        out[name] = clean
    return out


@router.post("/api/model3d/skeleton")
def model_edit_skeleton(payload: dict) -> dict:
    """Write moved joints back into the armature, re-bind, and prove it.

    Writes ``<stem>.bones.glb``. The draft is never touched, the same way the
    bake and the rig leave theirs alone.

    REBINDING IS THE DEFAULT AND SHOULD BE. Weights were solved by heat around
    the joint positions that existed when the bind ran; move an elbow two
    centimetres and every weight near it now belongs to a skeleton that is no
    longer there. ``rebind: false`` is for the edits where that is not true —
    a roll, a leaf tail grown to reach the geometry — and it costs the caller
    an explicit decision because getting it wrong ships a character that tears
    at exactly one joint.
    """
    rel = str(payload.get("rel") or "")
    project_root, target = _model(rel)
    _importable(target)

    limit = 0.0
    try:
        limit = float(payload.get("limit") or 0.0)
    except (TypeError, ValueError):
        limit = 0.0
    edits = _bone_edits(payload, limit)
    rebind = bool(payload.get("rebind", True))
    tolerance = _number(payload, "tolerance", 0.01)
    if not 0.0 <= tolerance <= 0.5:
        raise api.bad_request("tolerance must be between 0 and 0.5",
                              tolerance=tolerance)

    mod = _blender()
    if not mod.available().get("available"):
        raise api.ApiError(503, "Blender is not installed or not on the path",
                           detail=mod.available())

    out = target.with_name(target.stem + BONES_SUFFIX)
    report = _run(_rig_source() + "\n" + _BLENDER_PRELUDE + _BONES_EDIT_BODY,
                  {"path": str(target), "ext": target.suffix.lower(),
                   "armature": str(payload.get("armature") or ""),
                   "bones": edits, "rebind": rebind, "tolerance": tolerance},
                  BONES_TIMEOUT, export_glb=str(out))
    if not report.get("ok"):
        # A refused edit must not leave the half-written export standing: the
        # runner exports whatever the script left behind, refusal or not, and
        # a .glb on disk after a failed run reads as delivered work.
        try:
            out.unlink()
        except OSError:
            pass
        raise api.ApiError(502, "the skeleton edit failed inside Blender",
                           detail={"error": report.get("error"),
                                   "missing": report.get("missing")})
    _inspect_cache.clear()
    out_rel = (out.relative_to(project_root).as_posix() if out.is_file()
               else None)
    return api.ok({
        "rel": rel, "out": out_rel,
        "bytes": out.stat().st_size if out.is_file() else 0,
        "armature": report.get("armature"), "bones": report.get("bones"),
        "applied": report.get("applied") or [],
        "missing": report.get("missing") or [],
        "max_move": report.get("max_move"),
        "rebound": report.get("rebound"), "rigged": report.get("rigged"),
        "bound_with": report.get("bound_with"),
        "unweighted": report.get("unweighted"),
        "unweighted_pct": report.get("unweighted_pct"),
        "attempts": report.get("attempts") or [],
        "seconds": report.get("seconds"),
    })


# One pass of weight smoothing touches a vertex's edge neighbours; more than a
# few turns a local fix into a general blur that undoes the bind everywhere.
MAX_SMOOTH = 4
WEIGHTS_REPAIR_SUFFIX = ".weights.glb"
WEIGHTS_REPAIR_TIMEOUT = 1200

# THE REPAIR IS THE OTHER HALF OF A CHECK THAT ONLY EVER ACCUSED. weight_islands
# has been able to say "LeftUpperLeg owns a patch of the other thigh, 214
# vertices" since it landed, and the only thing anyone could do about it was
# open Blender and paint. Every stray vertex it names already has an answer
# sitting in the geometry: the bone whose segment actually passes nearest to
# it. That is not a guess, it is the same nearest-bone rule an envelope bind
# uses, applied only to the vertices the gate flagged and to nothing else.
_WEIGHTS_REPAIR_BODY = r'''
report = {"ok": False}


def _islands(mesh, members):
    """Connected groups of `members`, walked over the mesh's own edges."""
    adjacent = {}
    for edge in mesh.data.edges:
        a, b = edge.vertices
        if a in members and b in members:
            adjacent.setdefault(a, []).append(b)
            adjacent.setdefault(b, []).append(a)
    seen, groups = set(), []
    for start in members:
        if start in seen:
            continue
        stack, group = [start], []
        seen.add(start)
        while stack:
            here = stack.pop()
            group.append(here)
            for nxt in adjacent.get(here, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        groups.append(group)
    groups.sort(key=len, reverse=True)
    return groups


def _seg_distance(p, a, b):
    ab = b - a
    length = ab.length_squared
    if length < 1e-12:
        return (p - a).length
    t = max(0.0, min(1.0, (p - a).dot(ab) / length))
    return (p - (a + ab * t)).length


try:
    if P["ext"] != ".blend":
        bg_wipe()
        _import(P["path"], P["ext"])
    bpy.context.view_layer.update()
    kept, dropped = bgate_drop_shapes(list(bpy.context.scene.objects))
    arms = [o for o in kept if o.type == "ARMATURE"]
    meshes = [o for o in kept if o.type == "MESH" and o.vertex_groups]
    if not arms:
        raise RuntimeError("no armature in this file - fit a skeleton first")
    if not meshes:
        raise RuntimeError("no skinned mesh in this file - nothing to repair")
    arm = arms[0]

    # Bone segments in WORLD space, which is the frame the vertices are
    # measured in below. A rig whose object is not at the origin would
    # otherwise be compared against geometry standing somewhere else.
    segments = {}
    for bone in arm.data.bones:
        if not bone.use_deform:
            continue
        segments[bone.name] = (arm.matrix_world @ bone.head_local,
                               arm.matrix_world @ bone.tail_local)

    only = set(P.get("bones") or [])
    threshold = float(P["threshold"])
    min_bleed = int(P["min_bleed"])
    mode = P.get("mode") or "nearest"
    fixed, skipped, touched_all = [], [], set()

    for mesh in meshes:
        groups = {g.name: g for g in mesh.vertex_groups}
        shells = len(_islands(mesh, set(range(len(mesh.data.vertices)))))
        index_of = {g.name: g.index for g in mesh.vertex_groups}
        world = mesh.matrix_world

        for name in sorted(groups):
            if name not in segments:
                continue                       # not a deform bone
            if only and name not in only:
                continue
            gi = index_of[name]
            members = set()
            for vert in mesh.data.vertices:
                for entry in vert.groups:
                    if entry.group == gi and entry.weight > threshold:
                        members.add(vert.index)
                        break
            if len(members) < 2:
                continue
            parts = _islands(mesh, members)
            # SPANNING SEVERAL SHELLS IS NOT A FAULT. A character built from
            # separate pieces legitimately has a bone touching each of them;
            # only a split INSIDE one connected piece of surface is paint that
            # went somewhere it was not meant to.
            if len(parts) <= shells:
                continue
            stray = [i for part in parts[shells:] for i in part]
            if len(stray) < min_bleed:
                skipped.append({"bone": name, "stray": len(stray),
                                "why": "below the bleed threshold"})
                continue

            moved = {}
            for vi in stray:
                vert = mesh.data.vertices[vi]
                point = world @ vert.co
                groups[name].remove([vi])
                if mode == "drop":
                    moved["(dropped)"] = moved.get("(dropped)", 0) + 1
                else:
                    best, best_d = "", 1e30
                    for other, (head, tail) in segments.items():
                        if other == name:
                            continue
                        d = _seg_distance(point, head, tail)
                        if d < best_d:
                            best, best_d = other, d
                    if best:
                        target = groups.get(best)
                        if target is None:
                            target = mesh.vertex_groups.new(name=best)
                            groups[best] = target
                            index_of[best] = target.index
                        # ADD, not REPLACE: the vertex may already carry some
                        # of this bone, and overwriting would throw away a
                        # weight the bind got right.
                        current = 0.0
                        for entry in vert.groups:
                            if entry.group == target.index:
                                current = entry.weight
                                break
                        target.add([vi], min(1.0, current + 1.0), "REPLACE")
                        moved[best] = moved.get(best, 0) + 1
                touched_all.add((mesh.name, vi))
            fixed.append({"bone": name, "mesh": mesh.name,
                          "islands_before": len(parts), "shells": shells,
                          "stray": len(stray), "moved_to": moved})

        # RENORMALISE, or the mesh inflates. A vertex whose weights sum to 1.7
        # is pulled 1.7 times as far as the bones actually moved, and it is
        # the touched vertices that are at risk because one just gained a
        # whole unit of influence.
        for mesh_name, vi in [t for t in touched_all if t[0] == mesh.name]:
            vert = mesh.data.vertices[vi]
            total = sum(e.weight for e in vert.groups)
            if total <= 1e-9 or abs(total - 1.0) < 1e-6:
                continue
            for entry in list(vert.groups):
                for group in mesh.vertex_groups:
                    if group.index == entry.group:
                        group.add([vi], entry.weight / total, "REPLACE")
                        break

    # SMOOTHING IS OPTIONAL AND CAPPED. A reassigned vertex sits at a hard
    # boundary its neighbours do not share, which reads as a crease when the
    # joint bends; one or two passes over the touched vertices and their
    # neighbours softens that. More is a general blur that undoes the bind.
    passes = int(P.get("smooth") or 0)
    smoothed = 0
    if passes and touched_all:
        for mesh in meshes:
            here = {vi for (m, vi) in touched_all if m == mesh.name}
            if not here:
                continue
            neighbours = {}
            for edge in mesh.data.edges:
                a, b = edge.vertices
                neighbours.setdefault(a, []).append(b)
                neighbours.setdefault(b, []).append(a)
            by_index = {g.index: g for g in mesh.vertex_groups}
            for _ in range(passes):
                updates = {}
                for vi in here:
                    ring = neighbours.get(vi, [])
                    if not ring:
                        continue
                    blend = {}
                    for other in ring + [vi]:
                        for entry in mesh.data.vertices[other].groups:
                            blend[entry.group] = blend.get(entry.group, 0.0) + entry.weight
                    total = sum(blend.values())
                    if total <= 1e-9:
                        continue
                    updates[vi] = {g: w / total for g, w in blend.items()}
                for vi, weights in updates.items():
                    for gi, w in weights.items():
                        group = by_index.get(gi)
                        if group is not None:
                            group.add([vi], w, "REPLACE")
                    smoothed += 1

    total_verts = sum(len(m.data.vertices) for m in meshes)
    loose = sum(1 for m in meshes for v in m.data.vertices if not v.groups)
    report = {"ok": True, "fixed": fixed, "skipped": skipped,
              "bones_repaired": len(fixed),
              "vertices_moved": len(touched_all),
              "smoothed": smoothed, "smooth_passes": passes,
              "armature": arm.name, "meshes": len(meshes),
              "vertices": total_verts, "unweighted": loose,
              "unweighted_pct": round(100.0 * loose / max(total_verts, 1), 3)}
except Exception as exc:
    report = {"ok": False, "error": str(exc),
              "traceback": traceback.format_exc()[-1500:]}
_write(report)
print("weights repair done")
'''


@router.post("/api/model3d/weights/repair")
def model_repair_weights(payload: dict) -> dict:
    """Move the bled vertices onto the bone that should have had them.

    THE CHECK ONLY EVER ACCUSED. weight_islands names the bone and counts the
    vertices; painting them was the one thing this surface could not do, so
    every bleeding bind went to Blender or shipped. The stray vertices already
    have an answer in the geometry — the deform bone whose segment passes
    nearest — and that is the same rule an envelope bind uses, applied here
    only to the vertices the gate flagged.

    Writes ``<stem>.weights.glb``. Re-run ``/api/model3d/weights`` against the
    result to see the verdict move; this route deliberately does not grade its
    own work.
    """
    rel = str(payload.get("rel") or "")
    project_root, target = _model(rel)
    _importable(target)

    mode = str(payload.get("mode") or "nearest")
    if mode not in ("nearest", "drop"):
        raise api.bad_request("mode must be nearest or drop", mode=mode)
    smooth = _number(payload, "smooth", 0)
    if not 0 <= smooth <= MAX_SMOOTH:
        raise api.bad_request(f"smooth must be between 0 and {MAX_SMOOTH}",
                              smooth=smooth)
    threshold = _number(payload, "threshold", 0.02)
    if not 0.0 < threshold < 1.0:
        raise api.bad_request("threshold must be between 0 and 1",
                              threshold=threshold)
    min_bleed = _number(payload, "min_bleed", 3)
    if min_bleed < 1:
        raise api.bad_request("min_bleed must be at least 1",
                              min_bleed=min_bleed)
    bones = payload.get("bones") or []
    if not isinstance(bones, list) or any(not isinstance(b, str) for b in bones):
        raise api.bad_request("bones must be a list of bone names")

    mod = _blender()
    if not mod.available().get("available"):
        raise api.ApiError(503, "Blender is not installed or not on the path",
                           detail=mod.available())

    out = target.with_name(target.stem + WEIGHTS_REPAIR_SUFFIX)
    report = _run(_rig_source() + "\n" + _BLENDER_PRELUDE + _WEIGHTS_REPAIR_BODY,
                  {"path": str(target), "ext": target.suffix.lower(),
                   "bones": bones, "mode": mode, "smooth": smooth,
                   "threshold": threshold, "min_bleed": min_bleed},
                  WEIGHTS_REPAIR_TIMEOUT, export_glb=str(out))
    if not report.get("ok"):
        try:
            out.unlink()
        except OSError:
            pass
        raise api.ApiError(502, "the weight repair failed inside Blender",
                           detail={"error": report.get("error")})
    # NOTHING TO FIX IS NOT A FIX. A run that found no bleeding must not leave
    # a near-identical copy of the mesh behind for someone to adopt as the new
    # source of truth; say so and delete it.
    if not report.get("fixed"):
        try:
            out.unlink()
        except OSError:
            pass
        return api.ok({"rel": rel, "out": None, "fixed": [],
                       "skipped": report.get("skipped") or [],
                       "bones_repaired": 0, "vertices_moved": 0,
                       "note": "no bone's weights split inside a connected "
                               "piece of surface; nothing was written",
                       "seconds": report.get("seconds")})
    _inspect_cache.clear()
    out_rel = (out.relative_to(project_root).as_posix() if out.is_file()
               else None)
    return api.ok({
        "rel": rel, "out": out_rel,
        "bytes": out.stat().st_size if out.is_file() else 0,
        "fixed": report.get("fixed") or [],
        "skipped": report.get("skipped") or [],
        "bones_repaired": report.get("bones_repaired"),
        "vertices_moved": report.get("vertices_moved"),
        "smoothed": report.get("smoothed"),
        "smooth_passes": report.get("smooth_passes"),
        "mode": mode,
        "unweighted": report.get("unweighted"),
        "unweighted_pct": report.get("unweighted_pct"),
        "seconds": report.get("seconds"),
    })


@router.get("/api/model3d/clip_catalogue")
def model_clip_catalogue() -> dict:
    """Every clip this pipeline can author, and every library clip it can
    retarget, without spending a Blender.

    THE PANEL MUST NOT GUESS THIS LIST. humanpose.CLIP_KINDS and
    quadpose.QUAD_CLIP_KINDS are the authority on what ``blender.animate``
    accepts, and a hard-coded copy in JavaScript would go stale the first time
    a kind was added — the caller would then ask for a clip Blender refuses,
    twenty minutes into a run.

    Library packs are reported from ``animlib.status``, which never downloads.
    A pack that is not fetched comes back with ``fetched`` false and the
    command that fetches it, so the panel can say why the clips are missing
    instead of showing an empty list.
    """
    mod = _blender()
    from bgate_adapters import humanpose as _humanpose
    from bgate_adapters import quadpose as _quadpose

    def _kinds(names) -> list:
        return [{"kind": k, "gait": mod.clip_gait(k)} for k in names]

    packs: dict = {}
    try:
        from bgate_adapters import animlib as _animlib
        report = _animlib.status()
        for key, row in (report.get("packs") or {}).items():
            entry = {k: row.get(k) for k in
                     ("title", "license", "source", "fetched", "fetch")}
            entry["clips"] = []
            if row.get("fetched"):
                try:
                    entry["clips"] = _animlib.clips(key)
                except Exception as exc:
                    entry["error"] = f"{type(exc).__name__}: {exc}"
            packs[key] = entry
    except Exception:  # animlib is optional the way ffmpeg is
        packs = {}

    return api.ok({
        "humanoid": {"kinds": _kinds(_humanpose.CLIP_KINDS),
                     "defaults": [dict(c) for c in mod.DEFAULT_CLIPS]},
        "quadruped": {"kinds": _kinds(_quadpose.QUAD_CLIP_KINDS),
                      "defaults": [dict(c) for c in mod.QUAD_DEFAULT_CLIPS]},
        "packs": packs, "default_pack": mod.DEFAULT_PACK,
        "facings": ["check", "repair", "skeleton"],
        "max_clips": MAX_CLIPS, "default_fps": 30,
    })


# A posed clip comes straight from a browser, so its size is somebody else's
# choice until it is bounded here. 120 keys at 30 fps is a four-second clip
# with a key on every frame, which is past what anyone hand-poses.
MAX_POSE_KEYS = 120
MAX_POSED_BONES = 256
MAX_CLIP_SECONDS = 600.0


def _posed_keys(spec: dict, clip: str) -> list:
    """Validate per-bone pose keys: [{"t", "bones": {name: [w,x,y,z]}, "root"}].

    humanpose.posed_clip normalises the quaternions and fixes their
    hemisphere, so this checks SHAPE and SIZE — the things that are a 400 here
    and an unbounded loop or a corrupt track inside Blender.
    """
    raw = spec.get("keys")
    if not isinstance(raw, list) or not raw:
        raise api.bad_request(f"the {clip!r} clip needs a list of keys",
                              clip=clip)
    if len(raw) > MAX_POSE_KEYS:
        raise api.bad_request(f"at most {MAX_POSE_KEYS} keys in one clip",
                              clip=clip, asked=len(raw))
    out = []
    for key in raw:
        if not isinstance(key, dict):
            raise api.bad_request("each key must be an object", clip=clip)
        try:
            when = float(key.get("t", 0.0))
        except (TypeError, ValueError):
            raise api.bad_request("a key's t is not a number", clip=clip) from None
        if when != when or not 0.0 <= when <= MAX_CLIP_SECONDS:
            raise api.bad_request(
                f"a key's t must be between 0 and {MAX_CLIP_SECONDS:g} seconds",
                clip=clip, t=key.get("t"))
        rots = key.get("bones") or {}
        if not isinstance(rots, dict):
            raise api.bad_request("a key's bones must be an object", clip=clip)
        if len(rots) > MAX_POSED_BONES:
            raise api.bad_request(f"at most {MAX_POSED_BONES} bones in a key",
                                  clip=clip, asked=len(rots))
        clean = {}
        for name, quat in rots.items():
            if not isinstance(name, str) or not name.strip():
                raise api.bad_request("a posed bone needs a name", clip=clip)
            if not isinstance(quat, (list, tuple)) or len(quat) != 4:
                raise api.bad_request(f"{name} must be a quaternion [w,x,y,z]",
                                      clip=clip, bone=name)
            try:
                values = [float(c) for c in quat]
            except (TypeError, ValueError):
                raise api.bad_request(f"{name}'s quaternion is not four numbers",
                                      clip=clip, bone=name) from None
            # NaN fails every comparison including its own, and a zero
            # quaternion has no rotation to normalise toward: both reach
            # Blender as a bone that vanishes rather than as an error.
            if any(c != c or abs(c) > 1e6 for c in values):
                raise api.bad_request(f"{name}'s quaternion is not finite",
                                      clip=clip, bone=name)
            if sum(c * c for c in values) < 1e-12:
                raise api.bad_request(f"{name}'s quaternion is zero",
                                      clip=clip, bone=name)
            clean[name] = values
        entry = {"t": when, "bones": clean}
        root = key.get("root")
        if root is not None:
            if not isinstance(root, (list, tuple)) or len(root) != 3:
                raise api.bad_request("a key's root must be [x, y, z]", clip=clip)
            try:
                triple = [float(c) for c in root]
            except (TypeError, ValueError):
                raise api.bad_request("a key's root is not three numbers",
                                      clip=clip) from None
            if any(c != c or abs(c) > 1e4 for c in triple):
                raise api.bad_request("a key's root is not finite", clip=clip)
            entry["root"] = triple
        out.append(entry)
    return out


def _anim_clips(payload: dict) -> Optional[list]:
    """Validate the requested clips here, where the answer is instant.

    ``blender.animate`` validates them too — inside a Blender that has already
    spent a minute importing the mesh. Every refusal this can make first, it
    should: a typo in a kind is worth a 400 in a millisecond, not a 502 after
    the import.
    """
    raw = payload.get("clips")
    if raw in (None, ""):
        return None
    if not isinstance(raw, list):
        raise api.bad_request("clips must be a list")
    if len(raw) > MAX_CLIPS:
        raise api.bad_request(f"at most {MAX_CLIPS} clips in one run",
                              asked=len(raw))
    from bgate_adapters import humanpose as _humanpose
    from bgate_adapters import quadpose as _quadpose
    known = set(_humanpose.CLIP_KINDS) | set(_quadpose.QUAD_CLIP_KINDS) | {"library"}
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise api.bad_request("each clip must be an object")
        spec = {k: v for k, v in item.items()
                if k in ("name", "kind", "clip", "pack", "frames", "overrides",
                         "keys", "loop", "ease")}
        # A library clip names the motion, not a procedural kind.
        if spec.get("clip") and not spec.get("kind"):
            spec["kind"] = "library"
        name = str(spec.get("name") or spec.get("clip") or spec.get("kind") or "")
        name = name.strip()
        if not name:
            raise api.bad_request("a clip needs a name or a kind")
        if name in seen:
            raise api.bad_request(f"two clips called {name!r}", clip=name)
        seen.add(name)
        spec["name"] = name
        kind = str(spec.get("kind") or name)
        if kind not in known:
            raise api.bad_request(
                f"no clip kind {kind!r}",
                clip=name, known=sorted(known))
        spec["kind"] = kind
        # A posed clip carries the pose itself rather than a parameter, so its
        # keys are the payload and are checked as one.
        if kind == "bones":
            spec["keys"] = _posed_keys(spec, name)
        out.append(spec)
    return out


@router.post("/api/model3d/animate")
def model_animate(payload: dict) -> dict:
    """Author gameplay clips on a rigged character and render the proof.

    THE RUNG THIS COLUMN WAS MISSING. rig binds a skeleton, weights says the
    bind is clean and flex says it survives being bent — and none of the three
    puts a walk cycle on the thing. blender_animate has done that over MCP
    since the animation layer landed; the surface with the model open could
    only ever PLAY clips somebody else authored elsewhere.

    Straight through to bgate_adapters.blender.animate, the same call the MCP
    tool makes. Its verdicts come back untouched: `support` is the foot-contact
    gate per clip, `collisions` is the self-intersection gate, and a run that
    reports ok with a failed gate is a run whose clips play and read wrong —
    summarising that away would be deciding for the reader which failures
    matter.

    `refused` is the facing gate and NOT an error: the skin's toes and the
    skeleton's foot bones disagree about which way is forward, which is the
    defect that walks a whole character backwards with every other gate green.
    It comes back 200 with the reason, because the fix (`facing: "repair"`) is
    a decision for whoever is looking at the mesh.
    """
    rel = str(payload.get("rel") or "")
    project_root, target = _model(rel)
    _importable(target)

    # THE REQUEST IS CHECKED BEFORE THE MACHINE IS. A misspelled clip kind is
    # wrong on a box with Blender and wrong on one without, and answering 503
    # to it would send the caller looking for an installation problem they do
    # not have. The rig and flex routes check availability first because they
    # have nothing to validate.
    clips = _anim_clips(payload)
    fps = _number(payload, "fps", 30)
    if not 5 <= fps <= 120:
        raise api.bad_request("fps must be between 5 and 120", fps=fps)
    proof_frames = int(payload.get("proof_frames", 6) or 0)
    if not 0 <= proof_frames <= MAX_PROOF_FRAMES:
        raise api.bad_request(
            f"proof_frames must be between 0 and {MAX_PROOF_FRAMES}",
            proof_frames=proof_frames)
    facing = str(payload.get("facing") or "check")
    if facing not in ("check", "repair", "skeleton"):
        raise api.bad_request("facing must be check, repair or skeleton",
                              facing=facing)

    mod = _blender()
    if not mod.available().get("available"):
        raise api.ApiError(503, "Blender is not installed or not on the path",
                           detail=mod.available())

    out = target.with_name(target.stem + ANIM_SUFFIX)
    out_dir = project_root / ".bgate_out" / ANIM_DIRNAME / target.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    with _blender_lock:
        report = mod.animate(str(target), str(out), clips=clips, fps=fps,
                             out_dir=str(out_dir), stem="proof",
                             proof_frames=proof_frames, facing=facing,
                             textured=bool(payload.get("textured", True)),
                             loop_suffix=bool(payload.get("loop_suffix", False)),
                             orient=bool(payload.get("orient", True)),
                             timeout=ANIM_TIMEOUT)
    if not report.get("ok") and not report.get("refused"):
        raise api.ApiError(502, "authoring the clips failed inside Blender",
                           detail={k: report.get(k) for k in
                                   ("error", "facing", "strays")})
    _inspect_cache.clear()
    if report.get("refused"):
        # animate() unlinks the .glb it refused to stand behind. Nothing was
        # written, so there is nothing to open and nothing to warn about.
        return api.ok({"rel": rel, "ok": False, "refused": True,
                       "error": report.get("error") or "",
                       "facing": report.get("facing"),
                       "seconds": report.get("seconds")})

    out_rel = (out.relative_to(project_root).as_posix() if out.is_file()
               else None)
    return api.ok({
        "rel": rel, "ok": True, "refused": False, "out": out_rel,
        "bytes": out.stat().st_size if out.is_file() else 0,
        "clips": report.get("clips") or [],
        "rig": report.get("rig"), "facing": report.get("facing"),
        "strays": report.get("strays") or [],
        "support": report.get("support") or {},
        "collisions": report.get("collisions") or {},
        "sheets": _anim_sheets(project_root, report),
        "seconds": report.get("seconds"),
    })


def _anim_sheets(project_root: Path, report: dict) -> list:
    """The per-clip proof sheets as URLs the panel can show.

    A sheet outside the project has no /api/preview address, which is a fact
    to report rather than a reason to drop the row: the clip still exists and
    its path is still worth printing.
    """
    sheets = []
    for sheet in report.get("sheets") or []:
        path = sheet.get("path") if isinstance(sheet, dict) else None
        if not path:
            continue
        url = None
        try:
            url = "/api/preview?rel=" + Path(path).relative_to(
                project_root).as_posix()
        except ValueError:
            url = None
        sheets.append({"clip": sheet.get("clip"), "path": str(path),
                       "url": url})
    return sheets


@router.get("/api/model3d/retarget")
def model_retarget(rel: str) -> dict:
    """Will Godot's own retargeter accept this skeleton.

    The last gate before an animation library is worth authoring against, and
    the one this dashboard could not previously ask without leaving it. Only
    answerable for a model inside res:// — Godot has to import the thing —
    which is itself the useful answer when it is not.
    """
    project_root, target = _model(rel)
    res_path = _res_path(project_root, target)
    if not res_path:
        return api.ok({"rel": rel, "available": False,
                       "reason": "this model is not inside the Godot project "
                                 "- import it first"})
    godot_dir = project_root
    for cand in (project_root, project_root / "game"):
        if (cand / "project.godot").is_file():
            godot_dir = cand
            break
    from bgate_adapters import godot as _godot
    if not _godot.available().get("available"):
        return api.ok({"rel": rel, "available": False,
                       "reason": "Godot is not installed or not on the path"})
    with _blender_lock:
        report = _godot.retarget_check(str(godot_dir), res_path)
    return api.ok({"rel": rel, "available": True, "res_path": res_path,
                   "report": report})


@router.post("/api/model3d/open_in_blender")
def model_open_in_blender(payload: dict) -> dict:
    """Launch the installed Blender with this model already imported.

    THE ESCAPE HATCH THE DEFORM TEST NEEDS. flex can report that a shoulder
    tears and weight_islands can name the bone doing it, and until now there
    was nothing a human could DO about either without leaving the product to
    go and find the file. Blender is already a hard dependency — bgate doctor
    gates on it and every blender_* tool shells out to it — so opening it is a
    local process launch, free and instant, and not a new dependency at all.

    A .blend opens as itself. Everything else has to be IMPORTED, because
    `blender foo.glb` is not a thing Blender does: it opens .blend documents
    and treats any other argument as a file it cannot read. So a glTF or an
    .obj gets an empty file plus one import call.

    Discovery is bgate_adapters.blender.find_blender, which is the same
    resolver doctor's blender row probes through (bgate_core/runtime/doctor.py:
    "Discovery is NOT reimplemented here").

    THE FILE IS THE SOURCE OF TRUTH FROM HERE ON. Whatever is saved in Blender
    lands on disk; the viewer is holding a copy it parsed minutes ago and will
    not notice. The panel says so before it launches anything.
    """
    rel = str(payload.get("rel") or "")
    project_root, target = _model(rel)
    mod = _blender()
    try:
        exe = mod.find_blender()
    except Exception as exc:
        raise api.ApiError(503, "blender not found", detail={
            "reason": str(exc),
            "fix": "install Blender, or set BGATE_BLENDER to its executable - "
                   "the same row `bgate doctor` reports"})

    suffix = target.suffix.lower()
    args = [str(exe)]
    if suffix == ".blend":
        args.append(str(target))
    else:
        # THE PATH IS A PYTHON LITERAL BUILT BY repr(), NEVER PASTED INTO ONE.
        #
        # This wrote `filepath=r'{path}'` - a RAW string literal with the path
        # interpolated into it - and the text below is then handed to Blender as
        # `--python-expr`, which is source code. A raw literal cannot escape
        # anything, so a single quote in the path ENDS the literal and the rest
        # of the filename is executed as Python. A file named
        #
        #     x'); __import__('os').system('...'); #.glb
        #
        # would run that command inside Blender with the user's privileges.
        #
        # THE FILENAME IS NOT TRUSTED INPUT, which is the part worth being
        # explicit about, because `_model` looks like it has already checked
        # this path. It has: `safe_under` refuses anything outside the project
        # and the suffix must be a known model format. Neither says a word about
        # the CHARACTERS in the name, and a single quote is legal in a filename
        # on Windows, macOS and Linux alike. Models arrive here downloaded from
        # providers and generated by agents, so their names are as external as
        # any other request field.
        #
        # repr() is the fix and it is the whole fix: it emits a correctly quoted
        # and escaped Python literal for any string, which is exactly the job.
        path = str(target).replace("\\", "/")
        lit = repr(path)
        if suffix in (".glb", ".gltf"):
            call = f"bpy.ops.import_scene.gltf(filepath={lit})"
        elif suffix == ".obj":
            call = (f"\n try:\n  bpy.ops.wm.obj_import(filepath={lit})\n"
                    f" except AttributeError:\n"
                    f"  bpy.ops.import_scene.obj(filepath={lit})")
        elif suffix == ".fbx":
            call = f"bpy.ops.import_scene.fbx(filepath={lit})"
        else:
            raise api.ApiError(415, "Blender cannot import this format",
                               detail={"ext": suffix})
        # A timer, not a straight call: import operators want a fully built
        # window context and running one while Blender is still starting up
        # gets a "context is incorrect" and an empty scene.
        args += ["--python-expr", (
            "import bpy\n"
            "bpy.ops.wm.read_homefile(use_empty=True)\n"
            "def _go():\n"
            f" {call}\n"
            " return None\n"
            "bpy.app.timers.register(_go, first_interval=0.3)\n")]

    flags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: the dashboard must not
        # end up owning a GUI Blender's lifetime, and closing the server must
        # not take the artist's editor down with it.
        flags = 0x00000008 | 0x00000200
    try:
        import subprocess
        proc = subprocess.Popen(args, close_fds=True, creationflags=flags,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise api.ApiError(502, "could not launch Blender",
                           detail={"error": str(exc), "exe": str(exe)})
    return api.ok({"rel": rel, "exe": str(exe), "pid": proc.pid,
                   "imported": suffix != ".blend",
                   "note": "edits save to the file on disk - reload the model "
                           "here to see them"})


# /api/model3d/engine_view used to live here — a second opinion from Godot's
# importer on a model already inside res://. Nothing ever called it. The MCP
# `godot_inspect_resource` tool is the surviving path to the same numbers.
