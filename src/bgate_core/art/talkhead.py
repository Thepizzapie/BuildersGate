"""Animated talking portraits: one head, a few mouth states, stitched to a sheet.

A DIFFERENT ASSET CLASS FROM A SPRITE SET, and the difference is what makes it
worth its own module. A sprite set animates a BODY moving through space, so its
frames differ by pose and its hard problem is silhouette consistency. A talking
portrait animates a FACE at rest: everything is meant to be identical between
frames except the mouth, and its hard problem is that "identical" is exactly
what a generator will not give you twice.

Why bother: a dialogue card showing a still bust reads as a picture of a
character. The same bust with a mouth that moves while the line types out reads
as the character talking to you. It is the cheapest animation in a game and one
of the highest-return.

THE FOUR RULES THIS ENCODES, each learned the expensive way on a shipped game:

  1. ONE ANCHOR, N SIBLINGS. Every frame conditions on the SAME anchor, never on
     the frame before it. Chained conditioning drifts, and on a face drift is
     instantly legible: by the third frame the ears have moved and it reads as a
     different character.

  2. MOUTHS CANNOT BE DERIVED. Elsewhere the rule is generate the minimum and
     derive the rest, because a mirrored facing or a walk cycle is a transform.
     There is no affine map from a closed mouth to an open one, so mouths are
     generated and everything else is held still by the anchor.

  3. REGISTER ON THE RIGID FEATURE. Independent generations do not land on the
     same pixel grid, and a head that jumps two pixels between frames reads as a
     flinch rather than speech. Alignment is on silhouette WIDTH, because an
     open jaw extends the silhouette downward: align on height and the face
     shrinks every time the character speaks.

  4. MEASURE THE DRIFT, DO NOT ASK FOR CONSISTENCY. "Same colours, same pose" in
     the prompt works about three times in four, which is the dangerous amount.
     The fourth comes back colour-shifted with a differently shaped hat, which
     is invisible in a 128px cell and obvious as a flicker at 8fps. `drift()`
     turns that into a number a loop can retry on.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Sequence

# The coarsest set that still reads as speech. Three mouths carry a talk cycle
# at 8-12fps; more frames buy nothing at portrait size and cost a generation
# each. `blink` is not part of the cycle -- it fires on its own timer, which is
# what stops a talking head looking like a mannequin between lines.
MOUTHS: dict[str, str] = {
    "rest": "its mouth CLOSED, a small neutral line",
    "half": "its mouth OPEN A LITTLE, as if mid-consonant, teeth not visible",
    "wide": "its mouth OPEN WIDE on a vowel, the jaw dropped",
    "blink": "its mouth CLOSED and its EYES SHUT, mid-blink",
}

#: Frames played in a loop while a line is being delivered. `blink` is excluded
#: deliberately: a blink on every syllable is a twitch.
TALK_CYCLE = ["rest", "half", "wide", "half"]

#: Max mean-RGB distance a frame may sit from the anchor frame before it is
#: rejected. Tight on purpose: these are the same character, same pose, same
#: light, so more than a few units means a different generation, not a
#: different mouth.
DRIFT_LIMIT = 6.0

# How much a frame may be scaled UP to match the anchor's width before it is
# called broken rather than small. Registration is a nudge; anything past this
# is a near-empty generation whose silhouette is a speck, and resizing by its
# scale factor allocates until the process dies.
MAX_UPSCALE = 8.0

FRAMING = (
    "A PORTRAIT AVATAR seen STRAIGHT ON, facing the viewer dead centre and "
    "looking directly into the camera. Head and upper chest only, filling the "
    "frame, centred, at exactly the same scale and position in every image. "
)

_HOLD = (
    " The reference image is this exact character. Reproduce it identically: "
    "same pose, same scale, same position in frame, same palette. Change ONLY "
    "the mouth and eyes as described. Do not restyle, recolour or reframe."
)


def prompt_for(subject: str, frame: str, *, has_anchor: bool = False) -> str:
    """The full prompt for one frame. Exposed so a caller can inspect it."""
    if frame not in MOUTHS:
        raise ValueError(f"unknown frame {frame!r}; frames are {list(MOUTHS)}")
    return (f"{FRAMING}The subject is {subject} It is drawn with "
            f"{MOUTHS[frame]}." + (_HOLD if has_anchor else ""))


def mean_rgb(path: str | os.PathLike[str], *, step: int = 4) -> list[float]:
    """Average colour of the OPAQUE pixels only.

    Sampled every `step` px in both axes: this runs once per frame per retry and
    an exact mean of a megapixel buys no accuracy the threshold can use.
    """
    from PIL import Image

    im = Image.open(path).convert("RGBA")
    rgb, alpha = im.convert("RGB").load(), im.getchannel("A").load()
    tot = [0.0, 0.0, 0.0]
    seen = 0
    for y in range(0, im.height, step):
        for x in range(0, im.width, step):
            if alpha[x, y] > 200:
                px = rgb[x, y]
                tot[0] += px[0]
                tot[1] += px[1]
                tot[2] += px[2]
                seen += 1
    return [t / max(1, seen) for t in tot]


def drift(frames: dict[str, str], *, anchor: str = "rest",
          limit: float = DRIFT_LIMIT) -> dict:
    """How far each frame sits from the anchor frame, per channel, worst case.

    `frames` maps frame name -> path. Returns {name: {drift, ok}} plus `worst`.
    A caller regenerates any frame whose `ok` is false; that loop is the whole
    point, because the failure it catches is one frame in four and silent.
    """
    if anchor not in frames:
        raise ValueError(f"anchor {anchor!r} not among frames {list(frames)}")
    means = {n: mean_rgb(p) for n, p in frames.items()}
    base = means[anchor]
    out: dict[str, Any] = {}
    for name, m in means.items():
        d = max(abs(m[i] - base[i]) for i in range(3))
        out[name] = {"drift": round(d, 1), "ok": d <= limit}
    out["worst"] = max(v["drift"] for k, v in out.items() if k != "worst")
    return out


def sheet(frames: Sequence[tuple[str, str]], out_path: str | os.PathLike[str],
          *, cell: int = 128) -> dict:
    """Stitch frames into one horizontal sheet, registered on silhouette width.

    `frames` is an ordered sequence of (name, path). Every cell comes out the
    same size with the head in the same place, which is the only reason the
    result reads as one face talking rather than four faces cutting.
    """
    from PIL import Image

    if not frames:
        raise ValueError("no frames to stitch")

    loaded = [(n, Image.open(p).convert("RGBA")) for n, p in frames]
    boxes = {}
    for name, im in loaded:
        bb = im.getchannel("A").getbbox()
        if bb is None:
            raise ValueError(f"frame {name!r} is fully transparent")
        boxes[name] = bb

    ref_name = loaded[0][0]
    ref_w = boxes[ref_name][2] - boxes[ref_name][0]

    out = Image.new("RGBA", (cell * len(loaded), cell), (0, 0, 0, 0))
    report: dict[str, Any] = {}
    for i, (name, im) in enumerate(loaded):
        x0, y0, x1, _ = boxes[name]
        # Scale to a common WIDTH. Width is the rigid measurement on a talking
        # head; height is not, because an open jaw grows the silhouette down.
        k = ref_w / max(1, x1 - x0)
        # A near-empty generation — a two-pixel speck against a 400px anchor —
        # gives a scale factor in the hundreds, and resize() then tries to
        # allocate a 200 000² image and dies with MemoryError inside a generic
        # except. Cap it: nothing that needs an 8× upscale to match the anchor
        # is the same head, so it is a bad frame, not a small one.
        if k > MAX_UPSCALE:
            raise ValueError(
                f"frame {name!r} is {x1 - x0}px wide against a {ref_w}px anchor "
                f"({k:.0f}x) — that is a blank or broken generation, not a face")
        if abs(k - 1.0) > 0.001:
            im = im.resize((max(1, round(im.width * k)),
                            max(1, round(im.height * k))), Image.LANCZOS)
            bb = im.getchannel("A").getbbox()
            x0, y0, x1 = bb[0], bb[1], bb[2]

        # The crop square is sized off the SILHOUETTE, not the source image.
        # Sizing it off the image cut every frame at the eyes and threw away the
        # mouth, which is the only thing this sheet exists to animate.
        side = x1 - x0
        cx = (x0 + x1) // 2
        crop = im.crop((cx - side // 2, y0, cx + side // 2, y0 + side))
        out.alpha_composite(crop.resize((cell, cell), Image.LANCZOS), (i * cell, 0))
        report[name] = {"scale": round(k, 4), "side": side}

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path)
    return {"path": str(out_path), "cells": len(loaded), "cell": cell,
            "order": [n for n, _ in loaded], "registration": report}


def spriteframes(sheet_path: str, *, cell: int = 128, fps: float = 10.0,
                 order: Optional[Sequence[str]] = None) -> str:
    """A Godot SpriteFrames .tres for the sheet: a `talk` loop and a `blink`.

    `talk` holds only the cycle frames, so a blink cannot land mid-syllable.
    `blink` is a one-shot the caller fires on its own timer.
    """
    order = list(order or MOUTHS)
    idx = {name: i for i, name in enumerate(order)}
    res = os.path.basename(sheet_path)

    def region(name: str) -> str:
        return f"Rect2({idx[name] * cell}, 0, {cell}, {cell})"

    lines = ['[gd_resource type="SpriteFrames" format=3]', "",
             f'[ext_resource type="Texture2D" path="{res}" id="1"]', ""]
    for i, name in enumerate(order):
        lines += [f'[sub_resource type="AtlasTexture" id="{i + 1}"]',
                  'atlas = ExtResource("1")',
                  f"region = {region(name)}", ""]
    lines += ["[resource]", "animations = ["]

    cycle = [n for n in TALK_CYCLE if n in idx]
    lines.append("{")
    lines.append('"frames": [' + ", ".join(
        '{"duration": 1.0, "texture": SubResource("%d")}' % (idx[n] + 1)
        for n in cycle) + "],")
    lines += ['"loop": true,', '"name": &"talk",', f'"speed": {fps}', "},"]

    if "blink" in idx:
        lines.append("{")
        lines.append('"frames": [{"duration": 1.0, "texture": SubResource("%d")}]'
                     % (idx["blink"] + 1) + ",")
        lines += ['"loop": false,', '"name": &"blink",', f'"speed": {fps}', "},"]

    lines.append("]")
    return "\n".join(lines) + "\n"
