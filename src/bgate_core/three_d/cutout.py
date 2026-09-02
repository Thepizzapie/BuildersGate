"""Cutout characters: parts on a skeleton, animated once per template.

WHAT THIS IS. A 2D character assembled from individually generated PARTS —
head, torso, upper arm, forearm, thigh, shin, boot — hung on a shared bone
skeleton, animated ONCE per template, and re-skinned per character by swapping
textures. The frame-by-frame sprite path stays exactly where it was; this is a
second, parallel way to make a character move, and it changes nothing that
already exists.

WHY BONES RATHER THAN FRAMES. A frame pipeline pays per character per animation:
six clips at eight frames is forty-eight paid generations that must all agree
with each other, and a new hat means regenerating every one of them. A cutout
kit is ten generations and the animation is free forever after, because the
motion lives in the rig instead of in the pixels. Equipment becomes a texture
swap on one slot rather than a re-draw of the whole figure.

WHAT IT IS NOT, IN V1, AND EVERY ONE OF THESE IS DELIBERATE AND LABELLED:

  * No mesh deformation. Bones are Node2Ds and parts are Sprite2Ds — a puppet,
    not a skinned mesh. Polygon2D deform is a later version and is listed as
    such, so it does not get filed as a bug.
  * No IK, no blending, no AnimationTree. One AnimationPlayer, one library.
  * No auto-rigging. The template carries defaults and a human drags the pivots
    that are wrong. An automatic pivot finder is not deferred, it is refused:
    the anatomically correct point is not recoverable from an alpha mask.
  * No segmentation. Parts are GENERATED individually against a pinned
    reference; nothing here cuts a finished drawing into pieces.

THE TWO CONTRACTS THAT EVERYTHING ELSE DEPENDS ON:

  ORIGIN. Feet contact at (0, 0) and +y is UP in doc space. Godot 2D has +y
  DOWN, and the emitter is the single place that flip happens. Every number in
  a rig document is therefore readable as "how far above the ground", which is
  what a human dragging a pivot is actually thinking about.

  DELTAS. Animation tracks are deltas FROM THE TEMPLATE REST POSE, never
  absolute values. A per-character adjustment (this one's arms sit lower) has to
  survive frame one of every clip, and an absolute track would erase it — making
  "adjustments are preserved" a lie the moment anything played.

Pure data and pure functions. Nothing here touches Godot, an image, or a model;
cutoutwire emits, and the generation path fills the skin.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Optional

SUFFIX = ".cutout.json"
VERSION = 1

# Ceilings, not budgets. A document past either of these is a bug in whatever
# wrote it, and a scene with four thousand Node2Ds is not a character.
MAX_BONES = 64
MAX_SLOTS = 64
MAX_CLIP_KEYS = 240

# NODEPATH-SAFE NAMES, AND THIS IS THE ONE THAT FAILS SILENTLY. Godot rewrites
# illegal node names on load — a bone called "arm/left" becomes "arm_left" — and
# every animation track that names the old path then resolves to nothing. The
# clip plays. Nothing moves. There is no error anywhere.
BAD_NAME = re.compile(r'[/:@%."\s]')
NAME_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")


class CutoutError(ValueError):
    """A rig document is not usable, and the message says which part of it."""


# ---------------------------------------------------------------------------
# The shipped template
# ---------------------------------------------------------------------------
# A SIDE-VIEW BIPED, and the choice of side view is what makes the far-limb
# problem tractable: the near arm and the far arm are the same drawing at a
# different z and a different tint, so a kit is ten generations instead of
# sixteen. `reuse_of` in the skin says which parts are doing that.
#
# Positions are in doc space: x right, y UP from the ground contact at (0, 0),
# in pixels at the template's nominal 200 px figure height. `rot` is the rest
# angle in degrees. `z` is the draw order, and it is ABSOLUTE — see the emitter's
# note on z_as_relative, which is the Godot default that silently sums z down a
# node chain and puts the near arm behind the head.
BIPED_V1 = {
    "name": "biped_v1",
    "view": "side",
    "height_px": 200,
    "bones": [
        {"name": "hips",       "parent": "",           "pos": [0, 96],   "rot": 0.0},
        {"name": "chest",      "parent": "hips",       "pos": [0, 34],   "rot": 0.0},
        {"name": "head",       "parent": "chest",      "pos": [2, 30],   "rot": 0.0},
        {"name": "arm_far",    "parent": "chest",      "pos": [-2, 22],  "rot": -8.0},
        {"name": "forearm_far", "parent": "arm_far",   "pos": [0, -26],  "rot": 6.0},
        {"name": "arm_near",   "parent": "chest",      "pos": [4, 22],   "rot": 8.0},
        {"name": "forearm_near", "parent": "arm_near", "pos": [0, -26],  "rot": -6.0},
        {"name": "thigh_far",  "parent": "hips",       "pos": [-4, -2],  "rot": 0.0},
        {"name": "shin_far",   "parent": "thigh_far",  "pos": [0, -44],  "rot": 0.0},
        {"name": "foot_far",   "parent": "shin_far",   "pos": [0, -42],  "rot": 0.0},
        {"name": "thigh_near", "parent": "hips",       "pos": [4, -2],   "rot": 0.0},
        {"name": "shin_near",  "parent": "thigh_near", "pos": [0, -44],  "rot": 0.0},
        {"name": "foot_near",  "parent": "shin_near",  "pos": [0, -42],  "rot": 0.0},
    ],
    "slots": [
        {"name": "head",         "bone": "head",         "z": 6},
        {"name": "torso",        "bone": "chest",        "z": 4},
        {"name": "hip",          "bone": "hips",         "z": 3},
        {"name": "arm_far",      "bone": "arm_far",      "z": 1},
        {"name": "forearm_far",  "bone": "forearm_far",  "z": 1},
        {"name": "arm_near",     "bone": "arm_near",     "z": 8},
        {"name": "forearm_near", "bone": "forearm_near", "z": 8},
        {"name": "thigh_far",    "bone": "thigh_far",    "z": 2},
        {"name": "shin_far",     "bone": "shin_far",     "z": 2},
        {"name": "foot_far",     "bone": "foot_far",     "z": 2},
        {"name": "thigh_near",   "bone": "thigh_near",   "z": 7},
        {"name": "shin_near",    "bone": "shin_near",    "z": 7},
        {"name": "foot_near",    "bone": "foot_near",    "z": 7},
        {"name": "hat",          "bone": "head",         "z": 9},
        {"name": "weapon",       "bone": "forearm_near", "z": 9},
    ],
    # Where a part hangs from, as a fraction of its own alpha bounding box.
    # These are DEFAULTS a human overrides by dragging; `pivot_source` records
    # which of the two a given pivot is, so regenerating a part can tell you it
    # invalidated an authored pivot instead of silently pointing it somewhere
    # else on the new drawing.
    "pivots": {
        "head": [0.5, 0.86], "torso": [0.5, 0.94], "hip": [0.5, 0.7],
        "arm_far": [0.5, 0.9], "forearm_far": [0.5, 0.9],
        "arm_near": [0.5, 0.9], "forearm_near": [0.5, 0.9],
        "thigh_far": [0.5, 0.92], "shin_far": [0.5, 0.92],
        "foot_far": [0.35, 0.8],
        "thigh_near": [0.5, 0.92], "shin_near": [0.5, 0.92],
        "foot_near": [0.35, 0.8],
        "hat": [0.5, 0.2], "weapon": [0.4, 0.85],
    },
    # The far side is the near side's drawing, tinted back. Stated in the
    # template so a kit knows it is generating ten parts and not sixteen.
    "reuse": {"arm_far": "arm_near", "forearm_far": "forearm_near",
              "thigh_far": "thigh_near", "shin_far": "shin_near",
              "foot_far": "foot_near"},
    "far_tint": [0.72, 0.72, 0.78, 1.0],
}

TEMPLATES = {"biped_v1": BIPED_V1}

# Clips that must NOT loop. A death that loops is a character standing back up.
NO_LOOP = ("attack_melee", "hurt", "death")

# ---------------------------------------------------------------------------
# The shipped animation library — DELTAS, in degrees and pixels
# ---------------------------------------------------------------------------
# Authored against biped_v1's rest pose. `rot` keys are degrees away from rest;
# `pos` keys are pixels away from rest in doc space. The emitter adds the rest
# pose and the character's own adjustments and bakes the sum, so a character
# whose arms hang lower keeps them lower on frame one of every clip.
#
# NO KEY AT EXACTLY t == length ON A LOOPING CLIP. Godot blends the last key
# into the first, and a duplicate at both ends holds the pose for two frames —
# the hitch every hand-authored loop has until someone explains this.
CLIPS: dict[str, dict] = {
    "idle": {
        "length": 2.0, "loop": True, "fps": 12,
        "tracks": {
            "chest": {"rot": [[0.0, 0.0], [1.0, 1.6]]},
            "head": {"rot": [[0.0, 0.0], [1.0, -1.2]]},
            "arm_near": {"rot": [[0.0, 0.0], [1.0, 3.0]]},
            "arm_far": {"rot": [[0.0, 0.0], [1.0, -2.4]]},
            "hips": {"pos": [[0.0, [0.0, 0.0]], [1.0, [0.0, -1.5]]]},
        },
    },
    "walk": {
        "length": 0.8, "loop": True, "fps": 12,
        "tracks": {
            "thigh_near": {"rot": [[0.0, 22.0], [0.2, 0.0], [0.4, -20.0], [0.6, 0.0]]},
            "shin_near": {"rot": [[0.0, -10.0], [0.2, -4.0], [0.4, 26.0], [0.6, 2.0]]},
            "thigh_far": {"rot": [[0.0, -20.0], [0.2, 0.0], [0.4, 22.0], [0.6, 0.0]]},
            "shin_far": {"rot": [[0.0, 26.0], [0.2, 2.0], [0.4, -10.0], [0.6, -4.0]]},
            "arm_near": {"rot": [[0.0, -18.0], [0.4, 18.0]]},
            "forearm_near": {"rot": [[0.0, -8.0], [0.4, 6.0]]},
            "arm_far": {"rot": [[0.0, 18.0], [0.4, -18.0]]},
            "forearm_far": {"rot": [[0.0, 6.0], [0.4, -8.0]]},
            "chest": {"rot": [[0.0, 2.0], [0.4, -2.0]]},
            # Two bobs per stride: the body rises on each passing position.
            "hips": {"pos": [[0.0, [0.0, 0.0]], [0.2, [0.0, 3.0]],
                             [0.4, [0.0, 0.0]], [0.6, [0.0, 3.0]]]},
        },
    },
    "run": {
        "length": 0.55, "loop": True, "fps": 12,
        "tracks": {
            "thigh_near": {"rot": [[0.0, 40.0], [0.14, 6.0], [0.28, -34.0], [0.41, 4.0]]},
            "shin_near": {"rot": [[0.0, -34.0], [0.14, -16.0], [0.28, 52.0], [0.41, 8.0]]},
            "thigh_far": {"rot": [[0.0, -34.0], [0.14, 4.0], [0.28, 40.0], [0.41, 6.0]]},
            "shin_far": {"rot": [[0.0, 52.0], [0.14, 8.0], [0.28, -34.0], [0.41, -16.0]]},
            "arm_near": {"rot": [[0.0, -46.0], [0.28, 34.0]]},
            "forearm_near": {"rot": [[0.0, -52.0], [0.28, -28.0]]},
            "arm_far": {"rot": [[0.0, 34.0], [0.28, -46.0]]},
            "forearm_far": {"rot": [[0.0, -28.0], [0.28, -52.0]]},
            "chest": {"rot": [[0.0, 8.0], [0.28, 5.0]]},
            "hips": {"pos": [[0.0, [0.0, -2.0]], [0.14, [0.0, 5.0]],
                             [0.28, [0.0, -2.0]], [0.41, [0.0, 5.0]]]},
        },
    },
    "attack_melee": {
        "length": 0.5, "loop": False, "fps": 24,
        "events": [[0.22, "hit"]],
        "tracks": {
            "arm_near": {"rot": [[0.0, 0.0], [0.14, -70.0], [0.24, 58.0],
                                 [0.5, 0.0]]},
            "forearm_near": {"rot": [[0.0, 0.0], [0.14, -40.0], [0.24, 20.0],
                                     [0.5, 0.0]]},
            "chest": {"rot": [[0.0, 0.0], [0.14, -10.0], [0.24, 12.0], [0.5, 0.0]]},
            "head": {"rot": [[0.0, 0.0], [0.24, 6.0], [0.5, 0.0]]},
        },
    },
    "hurt": {
        "length": 0.4, "loop": False, "fps": 24,
        "tracks": {
            "chest": {"rot": [[0.0, 0.0], [0.08, 16.0], [0.4, 0.0]]},
            "head": {"rot": [[0.0, 0.0], [0.08, 22.0], [0.4, 0.0]]},
            "arm_near": {"rot": [[0.0, 0.0], [0.08, -24.0], [0.4, 0.0]]},
            "arm_far": {"rot": [[0.0, 0.0], [0.08, -18.0], [0.4, 0.0]]},
            "hips": {"pos": [[0.0, [0.0, 0.0]], [0.08, [-6.0, 0.0]],
                             [0.4, [0.0, 0.0]]]},
        },
    },
    "death": {
        "length": 1.1, "loop": False, "fps": 24,
        "events": [[0.9, "died"]],
        "tracks": {
            "chest": {"rot": [[0.0, 0.0], [0.3, 28.0], [1.1, 74.0]]},
            "head": {"rot": [[0.0, 0.0], [0.3, 18.0], [1.1, 40.0]]},
            "hips": {"pos": [[0.0, [0.0, 0.0]], [0.3, [-8.0, -20.0]],
                             [1.1, [-26.0, -92.0]]],
                     "rot": [[0.0, 0.0], [1.1, 86.0]]},
            "thigh_near": {"rot": [[0.0, 0.0], [1.1, -46.0]]},
            "shin_near": {"rot": [[0.0, 0.0], [1.1, 38.0]]},
            "thigh_far": {"rot": [[0.0, 0.0], [1.1, -28.0]]},
            "arm_near": {"rot": [[0.0, 0.0], [1.1, 34.0]]},
            "arm_far": {"rot": [[0.0, 0.0], [1.1, 20.0]]},
        },
    },
}


# ---------------------------------------------------------------------------
# Reading the template
# ---------------------------------------------------------------------------

def template(name: str = "biped_v1") -> dict:
    got = TEMPLATES.get(str(name or "").strip())
    if not got:
        raise CutoutError(
            f"no cutout template {name!r} — have: {sorted(TEMPLATES)}")
    return got


def templates() -> list[dict]:
    """Every template, with enough detail to choose and to generate a kit."""
    out = []
    for name, spec in TEMPLATES.items():
        parts = [s["name"] for s in spec["slots"]
                 if s["name"] not in spec.get("reuse", {})]
        out.append({
            "name": name, "view": spec["view"], "height_px": spec["height_px"],
            "bones": [b["name"] for b in spec["bones"]],
            "slots": [s["name"] for s in spec["slots"]],
            "parts_to_generate": parts,
            "reused": spec.get("reuse", {}),
            "clips": sorted(CLIPS),
            "no_loop": list(NO_LOOP),
        })
    return out


def bone_node_path(doc_or_template: dict, bone: str) -> str:
    """The scene path of a bone, FROZEN PER TEMPLATE VERSION.

    Animation tracks name these, so renaming a bone or reparenting one is a new
    template version (biped_v2), never an edit to this one. A track whose path
    no longer resolves does not error — it plays and moves nothing.
    """
    spec = (doc_or_template if "bones" in doc_or_template and
            isinstance(doc_or_template.get("bones"), list)
            else template(doc_or_template.get("template", "biped_v1")))
    by_name = {b["name"]: b for b in spec["bones"]}
    if bone not in by_name:
        raise CutoutError(f"no bone {bone!r} in this template")
    chain = [bone]
    seen = {bone}
    parent = by_name[bone].get("parent") or ""
    while parent:
        if parent in seen:
            raise CutoutError(f"bone cycle through {parent!r}")
        seen.add(parent)
        chain.append(parent)
        parent = (by_name.get(parent) or {}).get("parent") or ""
    return "Visual/" + "/".join(reversed(chain))


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

def empty(name: str = "character", template_name: str = "biped_v1") -> dict:
    spec = template(template_name)
    return {
        "version": VERSION,
        "name": str(name),
        "template": spec["name"],
        "view": spec["view"],
        "bones": [dict(b) for b in spec["bones"]],
        "slots": [dict(s) for s in spec["slots"]],
        "skin": {},
        "adjustments": {},
        "notes": "",
    }


def part_hash(path: str | os.PathLike[str]) -> str:
    """A short digest of a part file, so an authored pivot can notice that the
    drawing under it was replaced."""
    p = Path(path)
    if not p.is_file():
        return ""
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def _num(value: Any, field: str, *, lo: float, hi: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise CutoutError(f"{field} must be a number, got {value!r}") from None
    if not (lo <= out <= hi) or math.isnan(out):
        raise CutoutError(f"{field} must be between {lo} and {hi}, got {out}")
    return out


def _name(value: Any, field: str) -> str:
    got = str(value or "").strip()
    if not got:
        raise CutoutError(f"{field} is required")
    if BAD_NAME.search(got) or not NAME_OK.match(got):
        raise CutoutError(
            f"{field} {got!r} is not a legal Godot node name — letters, digits "
            "and underscore only, starting with a letter or underscore. Godot "
            "silently rewrites illegal names on load and every animation track "
            "that pointed at the old one then resolves to nothing, which looks "
            "exactly like a rig that does not move")
    return got


def _pair(value: Any, field: str, *, lo: float = -100000.0,
          hi: float = 100000.0) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise CutoutError(f"{field} must be a two-number list, got {value!r}")
    return [_num(value[0], f"{field}[0]", lo=lo, hi=hi),
            _num(value[1], f"{field}[1]", lo=lo, hi=hi)]


def normalise(doc: dict) -> dict:
    """The ONE funnel. Every path that writes a document comes through here.

    Refuses, rather than repairs: a cycle, an unknown parent, a duplicate name,
    an illegal node name, a slot on a bone that does not exist, a skin entry for
    a slot that does not exist. Each of those produces a scene that loads and
    then behaves wrongly in a way that is very hard to trace back.
    """
    if not isinstance(doc, dict):
        raise CutoutError("a rig document must be a dict")
    out = dict(doc)
    out["version"] = int(doc.get("version") or VERSION)
    out["name"] = _name(doc.get("name") or "character", "name")
    out["template"] = str(doc.get("template") or "biped_v1")
    spec = template(out["template"])
    out["view"] = str(doc.get("view") or spec["view"])

    bones = doc.get("bones") or [dict(b) for b in spec["bones"]]
    if len(bones) > MAX_BONES:
        raise CutoutError(f"{len(bones)} bones, over the {MAX_BONES} ceiling")
    seen: set[str] = set()
    clean_bones = []
    for i, raw in enumerate(bones):
        if not isinstance(raw, dict):
            raise CutoutError(f"bone {i} must be a dict")
        name = _name(raw.get("name"), f"bones[{i}].name")
        if name in seen:
            raise CutoutError(f"duplicate bone name {name!r}")
        seen.add(name)
        parent = str(raw.get("parent") or "")
        if parent:
            _name(parent, f"bones[{i}].parent")
        clean_bones.append({
            "name": name, "parent": parent,
            "pos": _pair(raw.get("pos") or [0, 0], f"bones[{i}].pos"),
            "rot": _num(raw.get("rot") or 0.0, f"bones[{i}].rot",
                        lo=-360.0, hi=360.0),
        })
    names = {b["name"] for b in clean_bones}
    for bone in clean_bones:
        if bone["parent"] and bone["parent"] not in names:
            raise CutoutError(
                f"bone {bone['name']!r} names parent {bone['parent']!r}, which "
                f"is not a bone in this document")
    _refuse_cycles(clean_bones)
    if sum(1 for b in clean_bones if not b["parent"]) != 1:
        raise CutoutError(
            "a rig needs exactly one root bone (a bone with no parent) — "
            "several roots means several characters in one document")
    out["bones"] = clean_bones

    slots = doc.get("slots") or [dict(s) for s in spec["slots"]]
    if len(slots) > MAX_SLOTS:
        raise CutoutError(f"{len(slots)} slots, over the {MAX_SLOTS} ceiling")
    clean_slots, slot_names = [], set()
    for i, raw in enumerate(slots):
        if not isinstance(raw, dict):
            raise CutoutError(f"slot {i} must be a dict")
        name = _name(raw.get("name"), f"slots[{i}].name")
        if name in slot_names:
            raise CutoutError(f"duplicate slot name {name!r}")
        slot_names.add(name)
        bone = _name(raw.get("bone"), f"slots[{i}].bone")
        if bone not in names:
            raise CutoutError(
                f"slot {name!r} hangs off bone {bone!r}, which does not exist")
        clean_slots.append({"name": name, "bone": bone,
                            "z": int(_num(raw.get("z") or 0, f"slots[{i}].z",
                                          lo=-4096, hi=4096))})
    out["slots"] = clean_slots

    skin = doc.get("skin") or {}
    if not isinstance(skin, dict):
        raise CutoutError("skin must be a dict of slot -> part")
    clean_skin = {}
    for slot, raw in skin.items():
        if slot not in slot_names:
            raise CutoutError(
                f"skin names slot {slot!r}, which this template does not have "
                f"— slots are: {sorted(slot_names)}")
        if not isinstance(raw, dict):
            raise CutoutError(f"skin[{slot}] must be a dict")
        texture = str(raw.get("texture") or "").strip()
        if not texture:
            raise CutoutError(f"skin[{slot}] has no texture")
        source = str(raw.get("pivot_source") or "default")
        if source not in ("default", "authored"):
            raise CutoutError(
                f"skin[{slot}].pivot_source must be 'default' or 'authored'")
        entry = {
            "texture": texture,
            "pivot": _pair(raw.get("pivot") or
                           spec["pivots"].get(slot, [0.5, 0.5]),
                           f"skin[{slot}].pivot", lo=-4.0, hi=4.0),
            "pivot_source": source,
            "part_hash": str(raw.get("part_hash") or ""),
            "rot_offset": _num(raw.get("rot_offset") or 0.0,
                               f"skin[{slot}].rot_offset", lo=-360.0, hi=360.0),
            "scale": _num(raw.get("scale") or 1.0, f"skin[{slot}].scale",
                          lo=0.01, hi=100.0),
            "reuse_of": str(raw.get("reuse_of") or ""),
            "far_tint": (list(raw["far_tint"]) if raw.get("far_tint") else None),
        }
        if entry["reuse_of"] and entry["reuse_of"] not in slot_names:
            raise CutoutError(
                f"skin[{slot}].reuse_of names {entry['reuse_of']!r}, which is "
                "not a slot")
        clean_skin[slot] = entry
    out["skin"] = clean_skin

    adjustments = doc.get("adjustments") or {}
    if not isinstance(adjustments, dict):
        raise CutoutError("adjustments must be a dict of bone -> deltas")
    clean_adj = {}
    for bone, raw in adjustments.items():
        if bone not in names:
            raise CutoutError(
                f"adjustments names bone {bone!r}, which does not exist")
        entry = {}
        if raw.get("pos") is not None:
            entry["pos"] = _pair(raw["pos"], f"adjustments[{bone}].pos",
                                 lo=-2000.0, hi=2000.0)
        if raw.get("rot") is not None:
            entry["rot"] = _num(raw["rot"], f"adjustments[{bone}].rot",
                                lo=-360.0, hi=360.0)
        if entry:
            clean_adj[bone] = entry
    out["adjustments"] = clean_adj
    out["notes"] = str(doc.get("notes") or "")
    return out


def _refuse_cycles(bones: list[dict]) -> None:
    parent = {b["name"]: b["parent"] for b in bones}
    for start in parent:
        seen, cur = {start}, parent[start]
        while cur:
            if cur in seen:
                raise CutoutError(
                    f"bone hierarchy has a cycle through {cur!r} — Godot would "
                    "not be able to build the node tree at all")
            seen.add(cur)
            cur = parent.get(cur) or ""


def rest_pose(doc: dict) -> dict:
    """Where each bone actually sits for THIS character: template plus its own
    adjustments. The emitter bakes animation deltas on top of this."""
    out = {}
    for bone in doc["bones"]:
        adj = (doc.get("adjustments") or {}).get(bone["name"]) or {}
        pos = list(bone["pos"])
        if adj.get("pos"):
            pos = [pos[0] + adj["pos"][0], pos[1] + adj["pos"][1]]
        out[bone["name"]] = {"pos": pos,
                             "rot": bone["rot"] + float(adj.get("rot") or 0.0)}
    return out


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------

def doc_path(root: str | os.PathLike[str], name: str) -> Path:
    return Path(root) / f"{name}{SUFFIX}"


def save(path: str | os.PathLike[str], doc: dict) -> Path:
    clean = normalise(doc)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
    return p


def load(path: str | os.PathLike[str]) -> dict:
    p = Path(path)
    if not p.is_file():
        raise CutoutError(f"no rig document at {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CutoutError(f"{p} is not valid JSON: {exc}") from exc
    return normalise(raw)


# ---------------------------------------------------------------------------
# What is wrong with this rig
# ---------------------------------------------------------------------------

def status(doc: dict, *, root: str | os.PathLike[str] = "") -> dict:
    """Everything that would make this rig emit badly, in one answer.

    Reports rather than raises: a half-generated kit is the NORMAL state during
    authoring, and the point is to say which parts are still missing rather than
    to refuse to look at the document.
    """
    doc = normalise(doc)
    spec = template(doc["template"])
    base = Path(root) if root else None
    slots = [s["name"] for s in doc["slots"]]
    filled = [s for s in slots if s in doc["skin"]]
    missing = [s for s in slots if s not in doc["skin"]]

    problems: list[dict] = []
    for slot, entry in doc["skin"].items():
        target = (base / entry["texture"]) if base and not os.path.isabs(
            entry["texture"]) else Path(entry["texture"])
        if base is not None and not target.is_file():
            problems.append({"slot": slot, "kind": "missing_texture",
                             "note": f"skin points at {entry['texture']}, "
                                     "which is not on disk"})
            continue
        # AN AUTHORED PIVOT IS A CLAIM ABOUT A SPECIFIC DRAWING. Regenerate the
        # part and the pivot still points at the same fraction of a different
        # picture — the hand ends up in the middle of the forearm and nothing
        # says why.
        if entry["pivot_source"] == "authored" and entry["part_hash"] and base:
            now = part_hash(target)
            if now and now != entry["part_hash"]:
                problems.append({
                    "slot": slot, "kind": "stale_pivot",
                    "note": "the pivot on this slot was placed by hand against "
                            "a different version of this part — check it or "
                            "re-drag it"})

    # THE ANKLE IS NOT THE SOLE. The origin contract is that the character's
    # FEET CONTACT (0, 0), and the lowest BONE is the ankle joint, which sits a
    # boot's thickness above that — 8 px on the 200 px template. Demanding zero
    # would fail every anatomically sensible rig, so the check is a band, and a
    # bone below the line is as wrong as one floating well above it.
    lowest = _lowest_foot(doc)
    ankle_band = spec["height_px"] * 0.075
    if lowest is not None and not (-1.0 <= lowest <= ankle_band):
        problems.append({
            "slot": "", "kind": "origin", "value": round(lowest, 1),
            "note": (f"the lowest bone sits {lowest:.0f}px "
                     + ("above" if lowest > 0 else "below")
                     + " the ground line, outside the 0 to "
                     f"{ankle_band:.0f}px ankle band. The origin contract is "
                     "feet contact at (0, 0) with +y up, and a rig that breaks "
                     "it either hovers or sinks in every scene it is placed in")})

    unknown = [c for c in doc.get("clips", CLIPS) if c not in CLIPS]
    return {
        "ok": not problems,
        "name": doc["name"],
        "template": doc["template"],
        "bones": len(doc["bones"]),
        "slots": len(slots),
        "filled": filled,
        "missing": missing,
        "complete": not missing,
        "problems": problems,
        "clips": sorted(CLIPS),
        "unknown_clips": unknown,
        "adjusted_bones": sorted(doc.get("adjustments") or {}),
        "reuse_available": spec.get("reuse", {}),
    }


def _lowest_foot(doc: dict) -> Optional[float]:
    """The doc-space y of the lowest bone, resolved through the hierarchy."""
    rest = rest_pose(doc)
    parent = {b["name"]: b["parent"] for b in doc["bones"]}
    lowest = None
    for name in rest:
        y, cur = 0.0, name
        guard = 0
        while cur and guard < MAX_BONES:
            y += rest[cur]["pos"][1]
            cur = parent.get(cur) or ""
            guard += 1
        lowest = y if lowest is None else min(lowest, y)
    return lowest


def clip(name: str) -> dict:
    got = CLIPS.get(name)
    if not got:
        raise CutoutError(f"no clip {name!r} — have: {sorted(CLIPS)}")
    return got


def clip_names(doc: Optional[dict] = None) -> list[str]:
    return sorted(CLIPS)
