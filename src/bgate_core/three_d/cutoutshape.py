"""The shape of every cutout piece, drawn by Builders Gate BEFORE any art.

WHY. Every generator so far drew a piece without knowing where it joins
(exit-67-r2, 2026-09-22): parts came back at any size, pivots were guessed
from alpha boxes, and the rig came apart at the shoulder, the waist and the
knee as soon as a limb turned. So the harness owns the geometry:

  * each generated slot has a SILHOUETTE in template pixels, in its bone's
    own frame (+y up, the bone origin at 0,0): capsules and discs;
  * every joint is a DISC of the same radius on both pieces that meet there,
    centred on the joint - the child's disc turns in place over the
    parent's, so a bent elbow or knee has no gap by construction;
  * the model PAINTS inside the silhouettes (the pinned reference is its
    style and identity input, never cut); the harness clips the paint to the
    silhouette, so the size and the pivot are the template's whatever the
    model does;
  * `joint_gaps` checks each finished texture: a joint disc the paint does
    not cover is a hole the rig will show, and it is reported.

Coordinates: doc space (+y up), forward = +x, template px on the 200 px
biped_v1 figure. The numbers are read off the bone table in cutout.py.
"""
from __future__ import annotations

import math
from typing import Optional

#: Texture pixels per template pixel for a finished piece. 2x keeps detail;
#: the sprite is scaled back by 1/TEX in the rig.
TEX = 2.0

#: slot -> primitives. ("cap", ax, ay, bx, by, r) is a capsule, ("disc", x,
#: y, r) a disc, ("ell", x, y, rx, ry) an ellipse.
SHAPES: dict[str, list[tuple]] = {
    # WIDTHS (2026-09-22): the first table drew a thin generic figure - stick
    # arms, a narrow torso, no shoulders - and a stocky character painted
    # into it read as a different body. Widened to a sturdy build.
    # Bone at the neck (160 up). Neck, then the skull; crown at ~200.
    "head": [("cap", 0, -5, 1, 8, 7.5), ("ell", 3, 22, 19, 19)],
    # Bone at the waist (130 up). Down into the hips, up to the shoulder
    # line, with only a neck-wide bump above it. MEASURED: a tall rounded
    # top read as HEAD space - three paints left the top third grey.
    "torso": [("cap", 0, -28, 1, 10, 19.5), ("disc", 4, 20, 10.0),
              ("disc", -4, 18, 10.0), ("cap", 1, 20, 2, 27, 7.0)],
    # Bone at the pelvis (96 up). Tall on purpose: it sits under the torso
    # (z 3 < 4) and fills the waist when the chest leans.
    "hip": [("cap", 0, -6, 0, 26, 17.0), ("disc", 4, -2, 10.5),
            ("disc", -4, -2, 10.5)],
    "arm": [("cap", 0, 0, 0, -26, 7.5)],
    "forearm": [("cap", 0, 0, 0, -24, 7.0)],
    "hand": [("disc", 0, 0, 7.0), ("ell", 0.5, -7, 7.5, 9)],
    "thigh": [("cap", 0, 0, 0, -44, 10.5)],
    "shin": [("cap", 0, 0, 0, -42, 9.0)],
    # Bone at the ankle (8 up). The ankle disc, and the boot forward to the
    # toe with its sole on the ground (-8).
    "foot": [("disc", 0, 0, 8.5), ("cap", -3, -2, 17, -2, 6.5)],
}

#: Joint discs per shape: (x, y, r) the paint must cover. The piece's own
#: hang joint and the joint its child hangs from.
JOINTS: dict[str, list[tuple]] = {
    "head": [(0, 0, 7.0)],
    # Every joint on the torso is covered by the piece that hangs there
    # (the head's neck disc, both arms' shoulder discs, all drawn over it),
    # so the torso owes none of its own.
    "torso": [],
    "hip": [(4, -2, 9.5)],
    "arm": [(0, 0, 7.0), (0, -26, 7.0)],
    "forearm": [(0, 0, 6.5), (0, -24, 6.5)],
    "hand": [(0, 0, 6.5)],
    "thigh": [(0, 0, 10.0), (0, -44, 10.0)],
    "shin": [(0, 0, 8.5), (0, -42, 8.5)],
    "foot": [(0, 0, 8.0)],
}

#: What each silhouette IS, for the paint prompt.
WHAT = {
    "head": "the head in side profile facing right, with the neck and "
            "headwear; the small round end at the bottom is the neck",
    "torso": "the torso as an ARMLESS MANNEQUIN TORSO wearing the "
             "character's top layers, from the collar to the waist, with "
             "anything worn on the back or chest - no sleeve, no arm, no "
             "shoulder seam; the side of the body is the shirt or vest "
             "panel, and the small bump on top is the base of the neck",
    "hip": "the hips and belt, down to the tops of the legs",
    "arm": "the upper arm from shoulder to elbow; both round ends are the "
           "shoulder and the elbow",
    "forearm": "the forearm from elbow to wrist; both round ends are joints",
    "hand": "the hand, a closed fist",
    "thigh": "the thigh from hip to knee; both round ends are joints",
    "shin": "the shin from knee to ankle; both round ends are joints",
    "foot": "the boot, toe pointing right, sole flat along the bottom; the "
            "round top is the ankle",
}


def key(slot: str) -> str:
    """Shape row for a slot: arm_near and arm_far share `arm`."""
    for side in ("_near", "_far"):
        if slot.endswith(side):
            return slot[: -len(side)]
    return slot


def has_shape(slot: str) -> bool:
    return key(slot) in SHAPES


def bbox(slot: str, pad: float = 1.0) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) in template px, doc space, padded for the outline."""
    xs, ys = [], []
    for p in SHAPES[key(slot)]:
        if p[0] == "cap":
            _, ax, ay, bx, by, r = p
            xs += [ax - r, ax + r, bx - r, bx + r]
            ys += [ay - r, ay + r, by - r, by + r]
        elif p[0] == "disc":
            _, x, y, r = p
            xs += [x - r, x + r]
            ys += [y - r, y + r]
        else:
            _, x, y, rx, ry = p
            xs += [x - rx, x + rx]
            ys += [y - ry, y + ry]
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def pivot(slot: str) -> list[float]:
    """The bone origin as a fraction of the silhouette's bbox, y from the
    bottom - the emitter's pivot convention."""
    x0, y0, x1, y1 = bbox(slot)
    return [round(-x0 / (x1 - x0), 5), round(-y0 / (y1 - y0), 5)]


def mask(slot: str, scale: float, grow: float = 0.0):
    """The silhouette as a PIL 'L' image at `scale` px per template px,
    covering exactly bbox(slot). `grow` widens every primitive (template px)."""
    import numpy as np
    from PIL import Image

    x0, y0, x1, y1 = bbox(slot)
    w = max(1, int(round((x1 - x0) * scale)))
    h = max(1, int(round((y1 - y0) * scale)))
    yy, xx = np.mgrid[0:h, 0:w]
    # Pixel centres back into doc space.
    dx = x0 + (xx + 0.5) / scale
    dy = y1 - (yy + 0.5) / scale
    inside = np.zeros((h, w), dtype=bool)
    for p in SHAPES[key(slot)]:
        if p[0] == "cap":
            _, ax, ay, bx, by, r = p
            vx, vy = bx - ax, by - ay
            l2 = vx * vx + vy * vy or 1e-9
            t = np.clip(((dx - ax) * vx + (dy - ay) * vy) / l2, 0, 1)
            inside |= np.hypot(dx - (ax + t * vx), dy - (ay + t * vy)) <= r + grow
        elif p[0] == "disc":
            _, x, y, r = p
            inside |= np.hypot(dx - x, dy - y) <= r + grow
        else:
            _, x, y, rx, ry = p
            inside |= ((dx - x) / (rx + grow)) ** 2 + ((dy - y) / (ry + grow)) ** 2 <= 1
    return Image.fromarray((inside * 255).astype("uint8"), "L")


def joint_gaps(slot: str, texture_path: str, *, min_cover: float = 0.8) -> list[dict]:
    """Joint discs on a finished piece that the paint does not cover.

    A disc below `min_cover` opaque is a hole at a joint: the rig shows the
    background through the shoulder, the elbow, the knee.
    """
    import numpy as np
    from PIL import Image

    if not has_shape(slot):
        return []
    with Image.open(texture_path) as im:
        alpha = np.asarray(im.convert("RGBA"))[:, :, 3] > 128
    h, w = alpha.shape
    x0, y0, x1, y1 = bbox(slot)
    sx, sy = w / (x1 - x0), h / (y1 - y0)
    out = []
    yy, xx = np.mgrid[0:h, 0:w]
    for jx, jy, r in JOINTS.get(key(slot), []):
        cx, cy = (jx - x0) * sx, (y1 - jy) * sy
        disc = np.hypot(xx + 0.5 - cx, yy + 0.5 - cy) <= r * 0.8 * min(sx, sy)
        n = int(disc.sum())
        if not n:
            continue
        cover = float(alpha[disc].sum()) / n
        if cover < min_cover:
            out.append({"slot": slot, "joint": [jx, jy], "cover": round(cover, 3),
                        "note": f"{slot}: the joint disc at {jx},{jy} is only "
                                f"{cover:.0%} painted - the rig shows a hole "
                                "there when it bends"})
    return out
