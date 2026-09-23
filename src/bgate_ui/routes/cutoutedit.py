"""The cutout rig editor's API - look at a rig in every pose and fix it by hand.

USER DIRECTIVE (2026-09-22): after an evening of generated rigs that were
"not quite" - a shoulder a few pixels high, arms that did not line up with
the torso - the fix that costs nothing and cannot be misjudged from a
screenshot is a person dragging the joint. This is that surface's server:

  * list the rigs in the project and load one: the document, its textures,
    and every clip BAKED by the same code the emitter uses (mirror, floor
    solve, adjustments), so what the editor plays is what Godot plays;
  * save: bone adjustments and per-piece pivot / rotation / scale go into the
    .cutout.json through cutout.normalise, and the rig is re-emitted;
  * proof: render the engine proof sheet on demand.

Nothing here talks to a model or spends money. Everything is project-root
relative and refuses to escape it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse
from PIL import Image

from bgate_ui import api
from bgate_ui.deps import root, safe_under

router = APIRouter()

SCAN_SKIP = {".godot", ".git", "node_modules", ".bgate_out", ".bgate", ".import"}
#: Pieces the editor lets a person change, and the fields on them.
PIECE_FIELDS = ("pivot", "rot_offset", "scale")


def _rel(base: Path, path: str | os.PathLike[str]) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = base / p
    try:
        return p.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return ""


def _doc_path(rel: str) -> Path:
    from bgate_core.three_d import cutout
    target = safe_under(root(), rel)
    if not target.name.endswith(cutout.SUFFIX) or not target.is_file():
        raise api.ApiError(404, "no rig document there", detail={"rel": rel})
    return target


def _sizes(doc: dict) -> dict:
    out = {}
    for slot, entry in (doc.get("skin") or {}).items():
        try:
            with Image.open(entry["texture"]) as im:
                out[slot] = im.size
        except OSError:
            pass
    return out


def _player_height(base: Path) -> int:
    try:
        from bgate_core.three_d import scalecontract
        return int(scalecontract.contract(base).get("player_height_px") or 0)
    except Exception:                                          # noqa: BLE001
        return 0


def payload(base: Path, doc_file: Path) -> dict:
    """Everything the editor draws, from the document on disk."""
    from bgate_core.three_d import cutout, cutoutwire

    doc = cutout.load(doc_file)
    sizes = _sizes(doc)
    rest = cutout.rest_pose(doc)
    bones = []
    for b in doc["bones"]:
        pos = cutoutwire.to_godot_pos(rest[b["name"]]["pos"])
        bones.append({"name": b["name"], "parent": b["parent"],
                      "rest": {"pos": list(pos),
                               "rot": cutoutwire.to_godot_rot(rest[b["name"]]["rot"])}})
    parts = {}
    for slot in doc["slots"]:
        entry = doc["skin"].get(slot["name"])
        if not entry or slot["name"] not in sizes:
            continue
        parts[slot["name"]] = {
            "bone": slot["bone"], "z": slot["z"],
            "url": "/api/cutout/file?rel=" + _rel(base, entry["texture"]),
            "size": list(sizes[slot["name"]]),
            "pivot": list(entry["pivot"]),
            "rot_offset": entry["rot_offset"], "scale": entry["scale"],
            "reuse_of": entry.get("reuse_of") or "",
            "far_tint": entry.get("far_tint"),
            "pivot_source": entry.get("pivot_source") or "default",
        }
    clips = {}
    for name in cutout.clip_names():
        try:
            baked = cutoutwire.bake_clip(doc, name, sizes)
        except Exception as exc:                               # noqa: BLE001
            clips[name] = {"error": str(exc)}
            continue
        tracks = {}
        for tr in baked["tracks"]:
            path, prop = tr["path"].split(":")
            bone = path.split("/")[-1]
            tracks.setdefault(bone, {})["rot" if prop == "rotation" else "pos"] = [
                [k[0], list(k[1]) if isinstance(k[1], (list, tuple)) else k[1]]
                for k in tr["keys"]]
        clips[name] = {"length": baked["length"], "loop": bool(baked["loop_mode"]),
                       "tracks": tracks}
    fit = cutoutwire.game_fit(doc, sizes, _player_height(base))
    overrides = {c: sorted(b) for c, b in (doc.get("clip_overrides") or {}).items()}
    for name, c in clips.items():
        if "error" not in c:
            c["events"] = [list(e) for e in (cutout.clip(name).get("events") or [])]
    return {"ok": True, "rel": _rel(base, doc_file), "name": doc["name"],
            "overrides": overrides,
            "template": doc["template"], "bones": bones, "parts": parts,
            "clips": clips, "ease": cutoutwire.EASE,
            "adjustments": doc.get("adjustments") or {},
            "game_fit": fit, "player_height_px": _player_height(base)}


@router.get("/api/cutout/rigs")
def list_rigs() -> dict:
    """Every .cutout.json in the project."""
    from bgate_core.three_d import cutout
    base = root()
    found = []
    for dirpath, dirnames, files in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SCAN_SKIP]
        for f in files:
            if f.endswith(cutout.SUFFIX):
                p = Path(dirpath) / f
                found.append({"rel": _rel(base, p), "name": f[: -len(cutout.SUFFIX)],
                              "mtime": p.stat().st_mtime})
    found.sort(key=lambda r: -r["mtime"])
    return {"ok": True, "rigs": found}


@router.get("/api/cutout/rig")
def load_rig(rel: str) -> dict:
    return payload(root(), _doc_path(rel))


@router.get("/api/cutout/file")
def rig_file(rel: str) -> FileResponse:
    target = safe_under(root(), rel, must_be_image=True)
    if not target.is_file():
        raise api.ApiError(404, "no such file", detail={"rel": rel})
    return FileResponse(target, media_type="image/png",
                        headers={"Cache-Control": "no-cache"})


def clip_edits(doc: dict, clips: Optional[dict], reset: Optional[dict]) -> dict:
    """Keys posed in the editor into the document's clip_overrides.

    The editor keys ABSOLUTE Godot rotations (radians, what it plays); the
    document stores the library's convention - degrees on this character's
    rest pose, authored facing CLIP_FORWARD - so the emitter bakes them with
    everything else and the floor solve still runs. `reset` = {clip: [bone]}
    (an empty list resets the whole clip) drops overrides back to the library.
    """
    import math
    from bgate_core.three_d import cutout
    out = dict(doc)
    over = {c: {b: dict(t) for b, t in bones.items()}
            for c, bones in (doc.get("clip_overrides") or {}).items()}
    for clip_name, bones in (reset or {}).items():
        if clip_name in over:
            if not bones:
                over.pop(clip_name)
            else:
                for b in bones:
                    over[clip_name].pop(b, None)
                if not over[clip_name]:
                    over.pop(clip_name)
    if clips:
        rest = cutout.rest_pose(cutout.normalise(doc))
        facing = int(cutout.template(doc["template"]).get("forward") or 1)
        m = 1.0 if facing == cutout.CLIP_FORWARD else -1.0
        for clip_name, bones in clips.items():
            length = float(cutout.clip(clip_name)["length"])
            for bone, ch in (bones or {}).items():
                keys = []
                for t, g in (ch or {}).get("rot") or []:
                    doc_deg = -math.degrees(float(g))
                    keys.append([min(max(0.0, float(t)), length),
                                 round((doc_deg - rest[bone]["rot"]) / m, 3)])
                if keys:
                    over.setdefault(clip_name, {})[bone] = {"rot": keys}
    out["clip_overrides"] = over
    return out


def apply_edits(doc: dict, adjustments: Optional[dict], pieces: Optional[dict]) -> dict:
    """Bone adjustments and piece fields onto the document. A piece whose
    pivot moved becomes AUTHORED (status flags it if the piece is later
    redrawn); a far piece that reuses an edited near piece follows it."""
    out = dict(doc)
    if adjustments is not None:
        out["adjustments"] = {b: {k: v for k, v in a.items() if k in ("pos", "rot")}
                              for b, a in adjustments.items() if isinstance(a, dict)}
    skin = {k: dict(v) for k, v in (doc.get("skin") or {}).items()}
    for slot, fields in (pieces or {}).items():
        if slot not in skin or not isinstance(fields, dict):
            continue
        for key in PIECE_FIELDS:
            if key in fields and fields[key] is not None:
                if key == "pivot" and list(fields[key]) != list(skin[slot].get("pivot") or []):
                    skin[slot]["pivot_source"] = "authored"
                skin[slot][key] = fields[key]
        # An edited piece is the author's now: the fit must not undo it.
        skin[slot]["fit"] = False
    for far, entry in skin.items():
        near = entry.get("reuse_of")
        if near and near in (pieces or {}) and far not in (pieces or {}):
            for key in PIECE_FIELDS:
                if key in skin[near]:
                    entry[key] = skin[near][key]
            entry["fit"] = False
    out["skin"] = skin
    # DRAW ORDER lives on the slot, not the skin: a z edit re-ranks the slot.
    zs = {slot: int(f["z"]) for slot, f in (pieces or {}).items()
          if isinstance(f, dict) and f.get("z") is not None}
    if zs:
        out["slots"] = [dict(sl, z=zs.get(sl["name"], sl["z"])) for sl in doc["slots"]]
    return out


@router.post("/api/cutout/save")
def save_rig(body: dict = Body(...)) -> dict:
    """Write the edits and re-emit the rig. Body: {rel, adjustments, pieces}."""
    from bgate_core.three_d import cutout, cutoutwire
    base = root()
    doc_file = _doc_path(str(body.get("rel") or ""))
    doc = cutout.load(doc_file)
    doc = cutout.normalise(apply_edits(doc, body.get("adjustments"), body.get("pieces")))
    # Clip keys convert against the rest pose AFTER the new adjustments.
    doc = cutout.normalise(clip_edits(doc, body.get("clips"), body.get("clip_reset")))
    cutout.save(doc_file, doc)
    scene = doc_file.with_name(doc["name"] + ".tscn")
    emitted = cutoutwire.emit(doc, project_dir=base, scene_path=scene,
                              sizes=_sizes(doc), player_height_px=_player_height(base),
                              force=True)
    return {**payload(base, doc_file), "emitted": bool(emitted.get("ok")),
            "scene": _rel(base, scene)}


@router.post("/api/cutout/regen")
def regen_piece(body: dict = Body(...)) -> dict:
    """Repaint ONE piece into its silhouette from the rig's written
    description (the reference image is never sent). One paid image; two
    for a torso, whose painted-on arm is then edited off. Body: {rel, slot,
    note}. The piece keeps the template's size and pivot, so it drops into
    the rig with every joint where it was."""
    from bgate_core.art import refs
    from bgate_core.runtime import providers
    from bgate_core.three_d import cutout, cutoutkit, cutoutshape, cutoutwire
    base = root()
    doc_file = _doc_path(str(body.get("rel") or ""))
    doc = cutout.load(doc_file)
    slot = str(body.get("slot") or "")
    near = (doc["skin"].get(slot) or {}).get("reuse_of") or slot
    if not cutoutshape.has_shape(near):
        raise api.ApiError(400, f"{slot} has no silhouette to paint (equipment is equipped, not generated)")
    description = doc.get("description") or ""
    if not description:
        raise api.ApiError(400, "this rig has no written description to paint from - "
                           "cutout_kit_generate(description=...) sets one")
    ref = refs.resolve(base, doc["reference"]) if doc.get("reference") else doc_file
    made = cutoutkit.generate_kit(
        base, doc["name"], str(ref), out_dir=doc_file.parent / "parts",
        provider=providers.provider_for("sprite", root=base), parts=[near],
        mode="sheet", description=description, note=str(body.get("note") or ""),
        max_paid_calls=2 if near == "torso" else 1)
    fresh = made["parts"].get(near)
    if not fresh:
        raise api.ApiError(502, "the piece did not come back: " + "; ".join(
            f.get("error", "") for f in made["failed"]) or made.get("stopped") or "no piece")
    entry = dict(doc["skin"].get(near) or {})
    entry.update({k: v for k, v in fresh.items() if k in (
        "texture", "part_hash", "anchor_hash", "prompt", "pivot", "scale", "fit", "shape")})
    entry["pivot_source"] = "default"
    doc["skin"][near] = entry
    doc["skin"] = cutoutkit.fill_reuse({k: v for k, v in doc["skin"].items()
                                        if v.get("reuse_of") != near}, doc["template"])
    doc = cutout.normalise(doc)
    cutout.save(doc_file, doc)
    scene = doc_file.with_name(doc["name"] + ".tscn")
    cutoutwire.emit(doc, project_dir=base, scene_path=scene, sizes=_sizes(doc),
                    player_height_px=_player_height(base), force=True)
    return {**payload(base, doc_file), "regen": {"slot": near, "calls": made["calls"],
            "flags": [f.get("note") for f in made["flags"]]}}


@router.post("/api/cutout/proof")
def render_proof(body: dict = Body(...)) -> dict:
    """Render the engine proof sheet for a rig (30-90 s: it runs Godot)."""
    from bgate_core.three_d import cutout, cutoutproof
    base = root()
    doc_file = _doc_path(str(body.get("rel") or ""))
    doc = cutout.load(doc_file)
    scene = doc_file.with_name(doc["name"] + ".tscn")
    shot = cutoutproof.proof(base, doc["name"], scene,
                             reference_px=_player_height(base), timeout=240)
    img = shot.get("image") or ""
    return {"ok": bool(img), "error": shot.get("error") or "",
            "url": ("/api/cutout/file?rel=" + _rel(base, img)) if img else ""}
