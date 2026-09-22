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
        # THE WRIST IS A BONE, NOT A SLOT. A slot hangs its part at its bone's
        # ORIGIN, so a hand on the forearm bone would sit at the elbow. The
        # hand bone sits at the forearm's far end; the weapon slot hangs from
        # the NEAR hand, which is what puts the grip inside the hand in every
        # frame by construction - the failure EXIT 67 could not gate (#8:
        # stamped guns floating in front of one painted arm).
        {"name": "hand_far",   "parent": "forearm_far", "pos": [0, -24], "rot": 0.0},
        {"name": "arm_near",   "parent": "chest",      "pos": [4, 22],   "rot": 8.0},
        {"name": "forearm_near", "parent": "arm_near", "pos": [0, -26],  "rot": -6.0},
        {"name": "hand_near",  "parent": "forearm_near", "pos": [0, -24], "rot": 0.0},
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
        {"name": "hand_far",     "bone": "hand_far",     "z": 1},
        {"name": "arm_near",     "bone": "arm_near",     "z": 8},
        {"name": "forearm_near", "bone": "forearm_near", "z": 8},
        {"name": "hand_near",    "bone": "hand_near",    "z": 10},
        {"name": "thigh_far",    "bone": "thigh_far",    "z": 2},
        {"name": "shin_far",     "bone": "shin_far",     "z": 2},
        {"name": "foot_far",     "bone": "foot_far",     "z": 2},
        {"name": "thigh_near",   "bone": "thigh_near",   "z": 7},
        {"name": "shin_near",    "bone": "shin_near",    "z": 7},
        {"name": "foot_near",    "bone": "foot_near",    "z": 7},
        {"name": "hat",          "bone": "head",         "z": 9},
        {"name": "weapon",       "bone": "hand_near",    "z": 9},
    ],
    # What each part IS, for the kit generator, and how tall it should be as a
    # fraction of the figure - read off the bone lengths above, so a part that
    # comes back at twice its height is flagged before it is assembled.
    # Equipment slots are not generated with the body; they are equipped.
    "parts": {
        "head":         {"what": "head and neck, facing the viewer's right in "
                                 "strict side profile", "height": 0.22},
        "torso":        {"what": "torso from the base of the neck to the hips, "
                                 "arms NOT included, shoulders cut clean at the "
                                 "seam", "height": 0.30},
        "hip":          {"what": "hips and belt, from waist to the top of the "
                                 "thighs", "height": 0.13},
        "arm_near":     {"what": "upper arm from shoulder to elbow, no hand",
                         "height": 0.15},
        "forearm_near": {"what": "forearm from elbow to wrist, no hand",
                         "height": 0.14},
        "hand_near":    {"what": "one hand, fingers curled as if gripping",
                         "height": 0.07},
        "thigh_near":   {"what": "thigh from hip to knee", "height": 0.23},
        "shin_near":    {"what": "shin from knee to ankle, no foot",
                         "height": 0.22},
        "foot_near":    {"what": "one boot or foot, sole flat on the ground",
                         "height": 0.08},
        "hat":          {"what": "headwear", "height": 0.10, "equipment": True},
        "weapon":       {"what": "held weapon", "height": 0.30, "equipment": True},
    },
    # Where a part hangs from, as a fraction of its own alpha bounding box.
    # These are DEFAULTS a human overrides by dragging; `pivot_source` records
    # which of the two a given pivot is, so regenerating a part can tell you it
    # invalidated an authored pivot instead of silently pointing it somewhere
    # else on the new drawing.
    "pivots": {
        "head": [0.5, 0.86], "torso": [0.5, 0.94], "hip": [0.5, 0.7],
        "arm_far": [0.5, 0.9], "forearm_far": [0.5, 0.9], "hand_far": [0.5, 0.85],
        "arm_near": [0.5, 0.9], "forearm_near": [0.5, 0.9], "hand_near": [0.5, 0.85],
        "thigh_far": [0.5, 0.92], "shin_far": [0.5, 0.92],
        "foot_far": [0.35, 0.8],
        "thigh_near": [0.5, 0.92], "shin_near": [0.5, 0.92],
        "foot_near": [0.35, 0.8],
        "hat": [0.5, 0.2], "weapon": [0.4, 0.85],
    },
    # The far side is the near side's drawing, tinted back. Stated in the
    # template so a kit knows it is generating ten parts and not sixteen.
    "reuse": {"arm_far": "arm_near", "forearm_far": "forearm_near",
              "hand_far": "hand_near",
              "thigh_far": "thigh_near", "shin_far": "shin_near",
              "foot_far": "foot_near"},
    "far_tint": [0.72, 0.72, 0.78, 1.0],
}

TEMPLATES = {"biped_v1": BIPED_V1}

# Clips that must NOT loop. A death that loops is a character standing back up.
NO_LOOP = ("jump", "fire", "attack_melee", "hurt", "death")

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
#
# SIGNS, MEASURED IN THE PROBE GYM (2026-09-22): a NEGATIVE rotation swings a
# hanging bone (arm, thigh) FORWARD, toward the facing; a POSITIVE shin or
# forearm delta bends the joint (heel back, hand up); a POSITIVE chest, head
# or hips rotation leans BACK; hips pos is pixels, +x forward, +y up.
#
# THE SET. Twelve clips, every one a side-scroller needs: idle (low ready),
# walk, run, jump, fall, crouch, slide, aim, fire, attack_melee, hurt, death.
# The first set had seven, no jump/fall/crouch/slide/fire at all, an idle with
# the arms hanging so the weapon floated at the hip in front of a hidden arm,
# a walk whose trailing knee bent backwards, and a death that rotated the
# standing figure ninety degrees. "These cutout rigs are not doing great."
# Every pose below was looked at in the engine before it shipped.
CLIPS: dict[str, dict] = {
    "idle": {
        # Low ready: both elbows bent so the weapon on the near hand sits in
    # front of the belly and the arm reads as HOLDING it. With the arms
    # hanging (the old idle) the gun sat at the hip and covered the arm.
        "length": 2.0, "loop": True, "fps": 12,
        "tracks": {
            "chest": {"rot": [[0.0, 0.0], [1.0, 1.6]]},
            "head": {"rot": [[0.0, 0.0], [1.0, -1.2]]},
            "arm_near": {"rot": [[0.0, -18.0], [1.0, -21.0]]},
            "forearm_near": {"rot": [[0.0, -72.0], [1.0, -70.0]]},
            "arm_far": {"rot": [[0.0, -30.0], [1.0, -27.0]]},
            "forearm_far": {"rot": [[0.0, -62.0], [1.0, -64.0]]},
            "hips": {"pos": [[0.0, [0.0, 0.0]], [1.0, [0.0, -1.5]]]},
        },
    },
    "walk": {
        # Trailing shin FLEXES (positive) - the old walk swung it forward and
    # the knee bent backwards on every stride.
        "length": 0.8, "loop": True, "fps": 12,
        "tracks": {
            "thigh_near": {
                "rot": [[0.0, 22.0], [0.2, 0.0], [0.4, -20.0], [0.6, 0.0]],
            },
            "shin_near": {
                "rot": [[0.0, 18.0], [0.2, 30.0], [0.4, 8.0], [0.6, 4.0]],
            },
            "thigh_far": {
                "rot": [[0.0, -20.0], [0.2, 0.0], [0.4, 22.0], [0.6, 0.0]],
            },
            "shin_far": {
                "rot": [[0.0, 8.0], [0.2, 4.0], [0.4, 18.0], [0.6, 30.0]],
            },
            "arm_near": {"rot": [[0.0, -24.0], [0.4, -12.0]]},
            "forearm_near": {"rot": [[0.0, -70.0], [0.4, -66.0]]},
            "arm_far": {"rot": [[0.0, -22.0], [0.4, -36.0]]},
            "forearm_far": {"rot": [[0.0, -60.0], [0.4, -66.0]]},
            "chest": {"rot": [[0.0, -3.0], [0.4, -5.0]]},
            "hips": {
                "pos": [[0.0, [0.0, 0.0]], [0.2, [0.0, 3.0]], [0.4, [0.0, 0.0]], [0.6, [0.0, 3.0]]],
            },
        },
    },
    "run": {
        # Contact / down / passing / up, near leg first, far leg half a cycle
    # behind; the heel kicks up behind on the passing pose.
        "length": 0.55, "loop": True, "fps": 12,
        "tracks": {
            "thigh_near": {
                "rot": [[0.0, -32.0], [0.14, -8.0], [0.28, 28.0], [0.41, 6.0]],
            },
            "shin_near": {
                "rot": [[0.0, 14.0], [0.14, 34.0], [0.28, 74.0], [0.41, 60.0]],
            },
            "thigh_far": {
                "rot": [[0.0, 28.0], [0.14, 6.0], [0.28, -32.0], [0.41, -8.0]],
            },
            "shin_far": {
                "rot": [[0.0, 74.0], [0.14, 60.0], [0.28, 14.0], [0.41, 34.0]],
            },
            "arm_near": {"rot": [[0.0, -46.0], [0.28, 10.0]]},
            "forearm_near": {"rot": [[0.0, -80.0], [0.28, -60.0]]},
            "arm_far": {"rot": [[0.0, 10.0], [0.28, -46.0]]},
            "forearm_far": {"rot": [[0.0, -60.0], [0.28, -80.0]]},
            "chest": {"rot": [[0.0, -10.0], [0.28, -7.0]]},
            "head": {"rot": [[0.0, 4.0], [0.28, 2.0]]},
            "hips": {
                "pos": [[0.0, [0.0, -2.0]], [0.14, [0.0, 5.0]], [0.28, [0.0, -2.0]], [0.41, [0.0, 5.0]]],
            },
        },
    },
    "jump": {
        # Launch crouch, then a tuck with the knees up. Not looped: the game
    # holds the last pose until `fall` takes over.
        "length": 0.5, "loop": False, "fps": 24,
        "tracks": {
            "hips": {
                "pos": [[0.0, [0.0, -12.0]], [0.12, [0.0, 4.0]], [0.5, [0.0, 2.0]]],
            },
            "thigh_near": {
                "rot": [[0.0, -30.0], [0.12, -20.0], [0.35, -55.0], [0.5, -50.0]],
            },
            "shin_near": {
                "rot": [[0.0, 50.0], [0.12, 10.0], [0.35, 85.0], [0.5, 80.0]],
            },
            "thigh_far": {
                "rot": [[0.0, -24.0], [0.12, 10.0], [0.35, -20.0], [0.5, -16.0]],
            },
            "shin_far": {
                "rot": [[0.0, 45.0], [0.12, 6.0], [0.35, 60.0], [0.5, 55.0]],
            },
            "chest": {"rot": [[0.0, -14.0], [0.12, -4.0], [0.5, -8.0]]},
            "head": {"rot": [[0.0, 6.0], [0.5, 2.0]]},
            "arm_near": {"rot": [[0.0, -10.0], [0.12, -40.0], [0.5, -34.0]]},
            "forearm_near": {"rot": [[0.0, -70.0], [0.5, -66.0]]},
            "arm_far": {"rot": [[0.0, -20.0], [0.12, -60.0], [0.5, -50.0]]},
            "forearm_far": {"rot": [[0.0, -60.0], [0.5, -50.0]]},
        },
    },
    "fall": {
        # Legs split, arms out, chest back a touch. Loops.
        "length": 0.6, "loop": True, "fps": 12,
        "tracks": {
            "thigh_near": {"rot": [[0.0, -22.0], [0.3, -26.0]]},
            "shin_near": {"rot": [[0.0, 24.0], [0.3, 30.0]]},
            "thigh_far": {"rot": [[0.0, 14.0], [0.3, 10.0]]},
            "shin_far": {"rot": [[0.0, 30.0], [0.3, 36.0]]},
            "chest": {"rot": [[0.0, 6.0], [0.3, 8.0]]},
            "head": {"rot": [[0.0, 10.0], [0.3, 12.0]]},
            "arm_near": {"rot": [[0.0, -50.0], [0.3, -56.0]]},
            "forearm_near": {"rot": [[0.0, -40.0], [0.3, -36.0]]},
            "arm_far": {"rot": [[0.0, -70.0], [0.3, -76.0]]},
            "forearm_far": {"rot": [[0.0, -30.0], [0.3, -26.0]]},
        },
    },
    "crouch": {
        # A squat: hips down, thighs forward-down, shins folded back under,
    # chest over the knees. Verified in the probe gym - the first draft
    # sat on air with its legs out.
        "length": 1.0, "loop": True, "fps": 12,
        "tracks": {
            "hips": {"pos": [[0.0, [6.0, -48.0]], [0.5, [6.0, -49.0]]]},
            "thigh_near": {"rot": [[0.0, -58.0], [0.5, -58.0]]},
            "shin_near": {"rot": [[0.0, 112.0], [0.5, 112.0]]},
            "thigh_far": {"rot": [[0.0, -50.0], [0.5, -50.0]]},
            "shin_far": {"rot": [[0.0, 104.0], [0.5, 104.0]]},
            "chest": {"rot": [[0.0, -26.0], [0.5, -25.0]]},
            "head": {"rot": [[0.0, 14.0], [0.5, 14.0]]},
            "arm_near": {"rot": [[0.0, -40.0], [0.5, -41.0]]},
            "forearm_near": {"rot": [[0.0, -50.0], [0.5, -50.0]]},
            "arm_far": {"rot": [[0.0, -50.0], [0.5, -50.0]]},
            "forearm_far": {"rot": [[0.0, -40.0], [0.5, -40.0]]},
        },
    },
    "slide": {
        # Hips at the ground, lead leg out straight, chest back, head up.
        "length": 0.5, "loop": True, "fps": 12,
        "tracks": {
            "hips": {"pos": [[0.0, [8.0, -62.0]], [0.25, [8.0, -63.0]]]},
            "thigh_near": {"rot": [[0.0, -92.0], [0.25, -92.0]]},
            "shin_near": {"rot": [[0.0, 12.0], [0.25, 12.0]]},
            "thigh_far": {"rot": [[0.0, -60.0], [0.25, -60.0]]},
            "shin_far": {"rot": [[0.0, 110.0], [0.25, 110.0]]},
            "chest": {"rot": [[0.0, 28.0], [0.25, 28.0]]},
            "head": {"rot": [[0.0, -22.0], [0.25, -22.0]]},
            "arm_near": {"rot": [[0.0, -56.0], [0.25, -56.0]]},
            "forearm_near": {"rot": [[0.0, -40.0], [0.25, -40.0]]},
            "arm_far": {"rot": [[0.0, 30.0], [0.25, 30.0]]},
            "forearm_far": {"rot": [[0.0, -20.0], [0.25, -20.0]]},
        },
    },
    "aim": {
        # A two-handed brace, held. Both forearms come forward so both hands
    # land on the weapon hanging from the near hand; the far arm reaches
    # further because its hand is on the fore-end.
        "length": 1.0, "loop": True, "fps": 12,
        "tracks": {
            "arm_near": {"rot": [[0.0, -62.0], [0.5, -60.0]]},
            "forearm_near": {"rot": [[0.0, -30.0], [0.5, -31.0]]},
            "arm_far": {"rot": [[0.0, -78.0], [0.5, -76.0]]},
            "forearm_far": {"rot": [[0.0, -12.0], [0.5, -13.0]]},
            "chest": {"rot": [[0.0, -6.0], [0.5, -5.0]]},
            "head": {"rot": [[0.0, 4.0], [0.5, 4.0]]},
        },
    },
    "fire": {
        # Recoil from the aim pose: muzzle up, chest back, a step back in the
    # hips. Starts and ends on the aim pose so it cuts back cleanly.
        "length": 0.2, "loop": False, "fps": 24,
        "events": [[0.0, 'shot']],
        "tracks": {
            "arm_near": {"rot": [[0.0, -62.0], [0.04, -72.0], [0.2, -62.0]]},
            "forearm_near": {
                "rot": [[0.0, -30.0], [0.04, -18.0], [0.2, -30.0]],
            },
            "arm_far": {"rot": [[0.0, -78.0], [0.04, -84.0], [0.2, -78.0]]},
            "forearm_far": {"rot": [[0.0, -12.0], [0.04, -6.0], [0.2, -12.0]]},
            "chest": {"rot": [[0.0, -6.0], [0.04, 2.0], [0.2, -6.0]]},
            "head": {"rot": [[0.0, 4.0], [0.04, 9.0], [0.2, 4.0]]},
            "hips": {
                "pos": [[0.0, [0.0, 0.0]], [0.04, [-3.0, 0.0]], [0.2, [0.0, 0.0]]],
            },
        },
    },
    "attack_melee": {
        # Wind-up back, swing through, a step into it.
        "length": 0.5, "loop": False, "fps": 24,
        "events": [[0.22, 'hit']],
        "tracks": {
            "arm_near": {
                "rot": [[0.0, -20.0], [0.14, 70.0], [0.24, -95.0], [0.5, -20.0]],
            },
            "forearm_near": {
                "rot": [[0.0, -70.0], [0.14, -60.0], [0.24, -10.0], [0.5, -70.0]],
            },
            "arm_far": {
                "rot": [[0.0, -30.0], [0.14, -10.0], [0.24, -50.0], [0.5, -30.0]],
            },
            "chest": {
                "rot": [[0.0, 0.0], [0.14, 12.0], [0.24, -14.0], [0.5, 0.0]],
            },
            "head": {"rot": [[0.0, 0.0], [0.24, 6.0], [0.5, 0.0]]},
            "hips": {
                "pos": [[0.0, [0.0, 0.0]], [0.14, [-4.0, 0.0]], [0.24, [8.0, -3.0]], [0.5, [0.0, 0.0]]],
            },
            "thigh_near": {"rot": [[0.0, 0.0], [0.24, -18.0], [0.5, 0.0]]},
            "shin_near": {"rot": [[0.0, 0.0], [0.24, 22.0], [0.5, 0.0]]},
        },
    },
    "hurt": {
        # Recoil: chest and head back, near arm up, a step back and down.
        "length": 0.4, "loop": False, "fps": 24,
        "tracks": {
            "chest": {"rot": [[0.0, 0.0], [0.08, 16.0], [0.4, 0.0]]},
            "head": {"rot": [[0.0, 0.0], [0.08, 22.0], [0.4, 0.0]]},
            "arm_near": {"rot": [[0.0, -18.0], [0.08, -44.0], [0.4, -18.0]]},
            "forearm_near": {
                "rot": [[0.0, -72.0], [0.08, -60.0], [0.4, -72.0]],
            },
            "arm_far": {"rot": [[0.0, -30.0], [0.08, -50.0], [0.4, -30.0]]},
            "forearm_far": {"rot": [[0.0, -62.0], [0.4, -62.0]]},
            "thigh_near": {"rot": [[0.0, 0.0], [0.08, -12.0], [0.4, 0.0]]},
            "shin_near": {"rot": [[0.0, 0.0], [0.08, 16.0], [0.4, 0.0]]},
            "hips": {
                "pos": [[0.0, [0.0, 0.0]], [0.08, [-6.0, -3.0]], [0.4, [0.0, 0.0]]],
            },
        },
    },
    "death": {
        # A real collapse, not a rotated standing figure: recoil, knees buckle,
    # sit back, lie flat. The final key lies on the ground with the head
    # lolled.
        "length": 1.2, "loop": False, "fps": 24,
        "events": [[1.0, 'died']],
        "tracks": {
            "chest": {
                "rot": [[0.0, 0.0], [0.15, 20.0], [0.5, 8.0], [0.9, 2.0], [1.2, -4.0]],
            },
            "head": {
                "rot": [[0.0, 0.0], [0.15, 26.0], [0.5, 14.0], [0.9, 8.0], [1.2, 20.0]],
            },
            "hips": {
                "rot": [[0.0, 0.0], [0.5, 12.0], [0.9, 70.0], [1.2, 88.0]],
                "pos": [[0.0, [0.0, 0.0]], [0.15, [-6.0, 0.0]], [0.5, [-12.0, -38.0]], [0.9, [-32.0, -82.0]], [1.2, [-42.0, -90.0]]],
            },
            "thigh_near": {
                "rot": [[0.0, 0.0], [0.5, -28.0], [0.9, 8.0], [1.2, 18.0]],
            },
            "shin_near": {
                "rot": [[0.0, 0.0], [0.5, 66.0], [0.9, 30.0], [1.2, 12.0]],
            },
            "thigh_far": {
                "rot": [[0.0, 0.0], [0.5, -20.0], [0.9, -10.0], [1.2, 0.0]],
            },
            "shin_far": {
                "rot": [[0.0, 0.0], [0.5, 58.0], [0.9, 40.0], [1.2, 26.0]],
            },
            "arm_near": {
                "rot": [[0.0, -18.0], [0.15, -60.0], [0.5, -40.0], [0.9, -20.0], [1.2, -30.0]],
            },
            "forearm_near": {
                "rot": [[0.0, -72.0], [0.15, -50.0], [0.5, -30.0], [1.2, -10.0]],
            },
            "arm_far": {
                "rot": [[0.0, -30.0], [0.15, -50.0], [0.5, -20.0], [0.9, -6.0], [1.2, 10.0]],
            },
            "forearm_far": {
                "rot": [[0.0, -62.0], [0.15, -40.0], [1.2, -10.0]],
            },
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
        equipment = [k for k, v in (spec.get("parts") or {}).items()
                     if v.get("equipment")]
        parts = [s["name"] for s in spec["slots"]
                 if s["name"] not in spec.get("reuse", {})
                 and s["name"] not in equipment]
        out.append({
            "name": name, "view": spec["view"], "height_px": spec["height_px"],
            "bones": [b["name"] for b in spec["bones"]],
            "slots": [s["name"] for s in spec["slots"]],
            "parts_to_generate": parts,
            "equipment": equipment,
            "parts": spec.get("parts") or {},
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
        # The identity reference the kit was generated against (a pin name or
        # a path) and its content hash at the time. Every part records the
        # anchor hash it was drawn from; status() compares.
        "reference": "",
        "reference_hash": "",
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
            # PROVENANCE. Which reference this part was generated against, and
            # the prompt that made it. EXIT 67 stitched frames from a
            # contaminated reference next to clean ones because nothing on the
            # frame said which reference it came from.
            "anchor_hash": str(raw.get("anchor_hash") or ""),
            "prompt": str(raw.get("prompt") or ""),
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
    out["reference"] = str(doc.get("reference") or "")
    out["reference_hash"] = str(doc.get("reference_hash") or "")
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

    # A KIT IS ONE CHARACTER. Every generated part carries the hash of the
    # reference it was conditioned on; a part whose hash differs from the
    # document's is a part from another run - or another character. This is
    # the check that did not exist when a flyer sheet shipped with a gator in
    # frames 2 and 5. Reused far-side parts inherit their near side's hash.
    ref_hash = doc.get("reference_hash") or ""
    for slot, entry in doc["skin"].items():
        if entry.get("reuse_of"):
            continue
        anchor = entry.get("anchor_hash") or ""
        if ref_hash and anchor and anchor != ref_hash:
            problems.append({
                "slot": slot, "kind": "stale_reference",
                "note": "this part was generated against a different reference "
                        "than the document names - a different run or a "
                        "different character; cutout_part_rerun it"})
    if root and doc.get("reference") and ref_hash:
        try:
            from ..art import refs as _refs
            now = part_hash(_refs.resolve(root, doc["reference"]))
        except Exception:
            now = ""
        if now and now != ref_hash:
            problems.append({
                "slot": "", "kind": "reference_moved",
                "note": f"the reference {doc['reference']!r} has changed since "
                        "this kit was generated (re-pinned or edited); the kit "
                        "still matches the OLD reference"})

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
