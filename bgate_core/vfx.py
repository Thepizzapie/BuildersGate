"""Derive an effect ANIMATION from ONE key frame, arithmetically.

WHY THIS EXISTS. Asking an image model for a grid of N animation frames does not
get you an animation. It gets you N INDEPENDENT DRAWINGS of roughly the same
idea, and the difference shows up as exactly the faults you cannot fix by
prompting harder:

  * a mug shatters over three frames and is intact again in the fourth;
  * a cloud's palette pops between frame 2 and frame 3;
  * a "fading" effect ends at full opacity, because the model has no notion of
    "the same thing, later" — only of "another picture of this";
  * a trail's frames point in different directions;
  * registration drifts, so a growing burst walks across the screen.

Those are all identity and continuity failures, and identity over time is the
one thing a text-to-image model structurally cannot hold. It is also the one
thing arithmetic gets for free. So this module inverts the split: the MODEL
draws ONE frame — the effect at its peak, the moment a human can look at and
approve — and TRANSFORMS derive the rest. Frame 3 is provably the same pixels
as frame 2, because it is made of them.

WHAT THAT BUYS, concretely: monotonic decay, stable registration, a palette
that cannot drift because no new colour is ever invented, and a cost of one
generation per effect instead of one-plus-rerolls.

WHAT IT DOES NOT DO. Motion an affine transform cannot express — a shape that
genuinely becomes a different shape (paper unfolding, a face changing) — is not
here and should not be faked here. Generate a second key frame and derive from
each. `animate` is deliberately narrow.

── THE FADE IS A DISSOLVE, NOT AN OPACITY RAMP ──────────────────────────────

Multiplying alpha to fade a sprite is the obvious implementation and it is the
wrong one for pixel art, twice over. It produces a field of half-transparent
pixels, which reads as a smudge against the hard-edged style everything else in
these projects is drawn in; and it trips the alpha audit's `soft_alpha` check,
which exists precisely to catch smudge.

So the fade is an ORDERED-DITHER DISSOLVE: a fixed Bayer threshold matrix
decides which pixels survive at a given coverage, alpha stays binary, and the
edge stays hard. Because the matrix is fixed and screen-aligned, a pixel that
has dropped out stays out as coverage falls — the dissolve reads as the effect
thinning, not as noise crawling over it. It is also what hand-drawn 16-bit VFX
actually did, for the same reason.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional

# The alpha at or below which a pixel is "not there". Kept well above 0 because
# a keyed cut-out carries a rim of near-zero alpha that is not art.
ALPHA_FLOOR = 8

# 4x4 ordered Bayer matrix, normalised to (0,1). Fixed, never randomised: the
# dissolve must be stable frame to frame or the effect boils.
_BAYER4 = [
    [0, 8, 2, 10],
    [12, 4, 14, 6],
    [3, 11, 1, 9],
    [15, 7, 13, 5],
]


def ease_out(t: float) -> float:
    """Fast then slow — how things that are losing energy actually move."""
    return 1.0 - (1.0 - t) ** 2


def ease_in(t: float) -> float:
    return t * t


# ── the motion vocabulary ────────────────────────────────────────────────────
#
# A motion is a description of how ONE key frame becomes N frames. Each entry
# is read by `animate`; nothing here talks to a model or to a file.
#
#   grow      scale at the first frame, as a fraction of the key frame's size.
#             The frames BEFORE `peak` interpolate grow -> 1.0.
#   expand    scale at the last frame. Frames AFTER `peak` interpolate
#             1.0 -> expand.
#   scatter   how far the effect's separate parts are pushed apart by the last
#             frame, as a fraction of the cell's width. 0 keeps it whole.
#   drift     pixels of travel by the last frame, (dx, dy) in cell fractions.
#             Negative y is upward, which is where smoke and steam go.
#   fade      coverage remaining at the last frame, 0..1. 1.0 never fades.
#   gravity   downward acceleration applied to scattered parts, cell fractions.
#   jitter    per-part wobble amplitude in pixels. The only motion for a loop.
#   squash    extra horizontal scale at the last frame — a puddle spreads wider
#             than it grows tall because it is being seen at a 2:1 iso angle.
MOTIONS: dict[str, dict[str, Any]] = {
    "burst": dict(
        grow=0.30, expand=1.45, scatter=0.22, drift=(0.0, 0.0), fade=0.10,
        gravity=0.0, jitter=0, squash=0.0, loop=False,
        doc="An impact. Snaps open from a point, blows its parts outward, "
            "dissolves. The default for anything that hits something."),
    "dissipate": dict(
        grow=0.55, expand=1.30, scatter=0.10, drift=(0.0, -0.10), fade=0.08,
        gravity=0.0, jitter=0, squash=0.0, loop=False,
        doc="A puff that rises and thins — muzzle smoke, a dust kick, steam. "
            "Gentler than a burst and it goes UP."),
    "shatter": dict(
        grow=0.95, expand=1.05, scatter=0.34, drift=(0.0, 0.0), fade=0.25,
        gravity=0.28, jitter=0, squash=0.0, loop=False,
        doc="Something rigid breaking. Parts fly apart and FALL, so the last "
            "frame reads as debris on the ground rather than a cloud. Give it "
            "a key frame that is already cracked into pieces."),
    "streak": dict(
        grow=1.0, expand=0.55, scatter=0.0, drift=(-0.18, 0.0), fade=0.12,
        gravity=0.0, jitter=0, squash=0.0, loop=False,
        doc="A trail shortening behind something that has moved on. Shrinks "
            "along its length and slides backward. Draw the key frame pointing "
            "the way the projectile travels."),
    "spread": dict(
        grow=0.40, expand=1.0, scatter=0.0, drift=(0.0, 0.0), fade=1.0,
        gravity=0.0, jitter=0, squash=0.30, loop=False,
        doc="A spill reaching its final size and STAYING. No fade — it holds "
            "on the last frame, because a puddle does not evaporate while you "
            "are looking at it. Widens faster than it deepens for a 2:1 floor."),
    "churn": dict(
        grow=1.0, expand=1.0, scatter=0.0, drift=(0.0, 0.0), fade=1.0,
        gravity=0.0, jitter=2, squash=0.0, loop=True,
        doc="A lingering cloud or pool that must stay put and stay the same "
            "size while it is alive. Only its parts wobble, so it LOOPS "
            "seamlessly for as long as the hazard lasts."),
}


def motion_help() -> str:
    """One line per motion, for a tool description or a seat brief."""
    return "\n".join(f"  {k:<10} {v['doc']}" for k, v in MOTIONS.items())


# ── image helpers ────────────────────────────────────────────────────────────

def _components(alpha, w: int, h: int, floor: int = ALPHA_FLOOR) -> list[list[int]]:
    """8-connected blobs of visible pixels, as flat pixel-index lists.

    An effect's separate parts are what `scatter` moves independently — the
    difference between a burst blowing apart and a burst merely getting bigger.
    Iterative flood fill: a recursive one blows the stack on a 96px cell.
    """
    seen = bytearray(w * h)
    out: list[list[int]] = []
    for start in range(w * h):
        if seen[start] or alpha[start] <= floor:
            continue
        stack = [start]
        seen[start] = 1
        blob = []
        while stack:
            i = stack.pop()
            blob.append(i)
            x, y = i % w, i // w
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h:
                        j = ny * w + nx
                        if not seen[j] and alpha[j] > floor:
                            seen[j] = 1
                            stack.append(j)
        out.append(blob)
    return out


def art_pixel(img, floor: int = ALPHA_FLOOR) -> int:
    """The size, in real pixels, of one of this sprite's APPARENT pixels.

    Chunky pixel art is drawn at a low resolution and shown large: a 64px frame
    may only have 16 art-pixels across it, each a 4x4 block of identical colour.
    Measured as the modal horizontal run of identical colour, ignoring runs of 1
    (which are anti-aliasing residue or single-pixel detail, not the grid).

    This exists because the dissolve HAS to know it — see `_dissolve`.
    """
    px = img.convert("RGBA").load()
    w, h = img.size
    runs: dict[int, int] = {}
    for y in range(h):
        run, prev = 0, None
        for x in range(w):
            c = px[x, y]
            if c[3] <= floor:
                if run > 1 and prev is not None:
                    runs[run] = runs.get(run, 0) + 1
                run, prev = 0, None
                continue
            if c == prev:
                run += 1
            else:
                if run > 1 and prev is not None:
                    runs[run] = runs.get(run, 0) + 1
                run, prev = 1, c
        if run > 1 and prev is not None:
            runs[run] = runs.get(run, 0) + 1
    if not runs:
        return 1
    # Weight by how much of the image each run length accounts for, not by how
    # many runs there are: a handful of long runs across a solid core should not
    # lose to a swarm of 2px runs along an edge.
    best = max(runs.items(), key=lambda kv: kv[0] * kv[1])[0]
    return max(1, min(best, max(1, min(w, h) // 4)))


def _dissolve(img, coverage: float, chunk: int = 1, jitter: float = 0.30,
              centres: Optional[list[tuple[float, float]]] = None):
    """Erode the effect from its EDGES inward until `coverage` of it remains.

    Alpha stays binary throughout — see the module docstring on why a fade is
    never an opacity ramp here.

    THE ORDER IS BY DISTANCE, NOT BY DITHER, and that is the correction that
    made this look like animation. A plain ordered dither removes pixels evenly
    across the whole sprite, so a half-faded burst is a solid burst with holes
    punched through its core — it reads as static crawling over the art rather
    than as the art burning down. Real hand-drawn VFX lose their OUTSIDE first
    and keep a hot core to the end.

    So each pixel is ranked by how far out it sits, and the outermost are
    dropped first. The Bayer matrix survives as a JITTER on that ranking, which
    is what keeps the receding edge ragged and pixel-shaped instead of a
    shrinking clean circle. `chunk` indexes the matrix in ART-pixel space so a
    whole apparent pixel jitters together — dithering per real pixel on art
    whose apparent pixel is a 4x4 block destroys the chunky look outright.

    The ranking is total and stable, so the pixels alive at coverage 0.4 are a
    subset of those alive at 0.6: the effect thins monotonically and never
    boils.

    `centres` IS WHAT KEEPS THIS FROM FIGHTING `scatter`. Distance is measured
    to the nearest centre given, so a burst that has blown into five fragments
    has each fragment thinning from ITS OWN edges. Measured against a single
    global centroid instead, the flying fragments are by definition the
    outermost pixels in the frame and the dissolve deletes them FIRST — the
    scatter and the fade cancel out, and a burst that should be spraying apart
    quietly erodes to a dot. Omit it and the whole sprite is treated as one
    mass, which is right for everything that does not come apart.
    """
    if coverage >= 0.999:
        return img
    px = img.load()
    w, h = img.size
    c = max(1, chunk)

    visible = []
    sx = sy = 0.0
    for y in range(h):
        for x in range(w):
            if px[x, y][3] > ALPHA_FLOOR:
                visible.append((x, y))
                sx += x
                sy += y
    if not visible:
        return img
    n = len(visible)
    anchors = centres or [(sx / n, sy / n)]

    def _near(x: int, y: int) -> float:
        return min(math.hypot(x - ax, y - ay) for ax, ay in anchors)

    far = max(_near(x, y) for x, y in visible) or 1.0

    ranked = []
    for x, y in visible:
        d = _near(x, y) / far
        b = (_BAYER4[(y // c) & 3][(x // c) & 3] + 0.5) / 16.0
        ranked.append((d + jitter * b, x, y))
    ranked.sort()
    for _, x, y in ranked[int(n * coverage):]:
        r, g, bb, a = px[x, y]
        px[x, y] = (r, g, bb, 0)
    return img


def _scaled(img, sx: float, sy: float):
    """NEAREST only, and never to zero. Any smooth filter turns chunky pixel art
    into porridge, which is the one thing every project using this cares about.
    """
    from PIL import Image

    w = max(1, int(round(img.width * sx)))
    h = max(1, int(round(img.height * sy)))
    return img.resize((w, h), Image.NEAREST)


def _trim(img):
    box = img.getbbox()
    return img.crop(box) if box else img


# ── the derivation ───────────────────────────────────────────────────────────

def derive_frames(key_img, *, motion: str = "burst", frames: int = 4,
                  peak: int = 1, cell: tuple[int, int] = (64, 64),
                  pad: float = 0.92, overrides: Optional[dict] = None,
                  seed: int = 0) -> list[Any]:
    """One key frame in, `frames` registered frames out.

    `peak` is which output frame the key frame IS — the moment it was drawn at.
    Frames before it grow into it; frames after it decay out of it. A burst
    whose key frame is its widest moment therefore wants peak=1 of 4: one frame
    of snap-in, two of break-up. peak=0 makes the key frame the start.

    EVERY FRAME IS COMPOSITED INTO THE SAME CELL AT THE SAME ANCHOR, which is
    the cell's centre. That is what makes the set stackable with the projectile
    it belongs to and is why nothing here trims per frame: trimming derives each
    frame's placement from its own alpha extent, and a growing effect then walks.
    """
    from PIL import Image

    spec = dict(MOTIONS.get(motion) or MOTIONS["burst"])
    spec.update(overrides or {})
    fw, fh = cell
    peak = max(0, min(peak, frames - 1))

    src = _trim(key_img.convert("RGBA"))
    # Fit the key frame to the cell once, at the peak size. Everything else is
    # relative to this, so `pad` is honoured for the whole set rather than per
    # frame.
    #
    # HEADROOM IS RESERVED FOR THE MOTION BEFORE IT HAPPENS. Fitting the key
    # frame to the cell and THEN expanding, scattering and drifting it pushes
    # the result straight out through the cell wall, where it is silently
    # clipped — and the clip is worst for exactly the motions that need to be
    # seen, since a burst's flying fragments are the outermost thing in the
    # frame. Measured: a 3-part burst at scatter 0.22 lost both outer fragments
    # entirely and read as a single blob that never came apart. So the effect is
    # fitted to the box its WHOLE ANIMATION occupies, not to the box its first
    # frame does.
    # SCATTER NEEDS SOMETHING TO SCATTER. Decided from the source, before any
    # scaling, because it also governs how much headroom to reserve: a key frame
    # drawn as one mass is not going to come apart, so reserving room for it to
    # would only shrink the effect for nothing.
    #
    # It is switched OFF rather than scaled down, and that distinction is the
    # whole bug. A lone part sits at the effect's own centre, so its offset from
    # that centre is sub-pixel rounding noise — and every attempt to normalise
    # that noise into a direction (`hypot(...) or 1.0`, then dividing by the
    # spread) faithfully turns it into a unit vector and pushes the part a full
    # scatter distance along it. A single-part burst drifted 8px up and left
    # across its four frames, in a direction that came from nowhere.
    src_alpha = list(src.getchannel("A").getdata())
    n_parts = len(_components(src_alpha, src.width, src.height))
    scatter = spec["scatter"] if n_parts >= 2 else 0.0

    room = (max(1.0, spec["expand"])
            * (1.0 + 2.0 * (scatter + spec["gravity"]
                            + max(abs(spec["drift"][0]), abs(spec["drift"][1])))))
    fit = pad / room
    base = min(fw * fit / max(1, src.width), fh * fit / max(1, src.height))
    peak_img = _scaled(src, base, base)
    # Measured on the PEAK image, after fitting — the dissolve runs on frames in
    # cell space, so the art-pixel size that matters is the one they end up with,
    # not the one the source file was drawn at.
    chunk = spec.get("chunk") or art_pixel(peak_img)

    # Parts are found ONCE, on the peak frame, and carried through every frame.
    # Re-finding them per frame would let a part appear or merge as the effect
    # changes size, which is the identity break this module exists to prevent.
    alpha = list(peak_img.getchannel("A").getdata())
    parts = _components(alpha, peak_img.width, peak_img.height)
    cx0 = peak_img.width / 2.0
    cy0 = peak_img.height / 2.0
    centroids = []
    for blob in parts:
        sx = sum(i % peak_img.width for i in blob) / len(blob)
        sy = sum(i // peak_img.width for i in blob) / len(blob)
        centroids.append((sx, sy))
    # How far the OUTERMOST part sits from the middle. Scatter is scaled against
    # this rather than applied as a unit vector, so a part's travel is
    # proportional to how far out it already was — outer debris flies furthest,
    # a part at the centre stays put, which is both what an explosion does and
    # what stops the degenerate case below.
    #
    # THE DEGENERATE CASE, because it shipped: normalising with
    # `hypot(vx, vy) or 1.0` turns a part sitting ON the centre into a UNIT
    # vector pointing wherever sub-pixel centroid noise happened to fall, and
    # then pushes it the full scatter distance that way. A single-part burst —
    # the most common kind — drifted 8px up and left over its four frames for
    # no reason anyone could see in the code.
    _spread = max(
        (math.hypot(bx - cx0, by - cy0) for bx, by in centroids), default=0.0)

    out = []
    for f in range(frames):
        # `t` is progress through the DECAY half (0 at the peak, 1 at the end);
        # `u` is progress through the GROW half (0 at the start, 1 at the peak).
        if f < peak:
            u = (f + 1) / (peak + 1)
            scale = spec["grow"] + (1.0 - spec["grow"]) * ease_out(u)
            t = 0.0
        else:
            t = 0.0 if frames - 1 == peak else (f - peak) / (frames - 1 - peak)
            scale = 1.0 + (spec["expand"] - 1.0) * ease_out(t)

        sx = scale * (1.0 + spec["squash"] * ease_out(t))
        stage = _scaled(peak_img, sx, scale)

        frame = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
        # Where each part ENDED UP this frame, so the dissolve can erode every
        # fragment from its own edges rather than from the frame's middle.
        placed: list[tuple[float, float]] = []
        if scatter <= 0 and spec["jitter"] <= 0:
            # Whole-sprite motion: one paste, centred.
            frame.paste(stage, ((fw - stage.width) // 2, (fh - stage.height) // 2),
                        stage)
        else:
            # Part-wise motion. Each blob is lifted off the PEAK image, scaled
            # with it, and placed at its own displaced position.
            ratio_x = stage.width / max(1, peak_img.width)
            ratio_y = stage.height / max(1, peak_img.height)
            ox = (fw - stage.width) // 2
            oy = (fh - stage.height) // 2
            for idx, blob in enumerate(parts):
                bx, by = centroids[idx]
                # Radial push away from the effect's own centre. A part sitting
                # exactly at the centre has no direction to go, so it stays —
                # which is right: that is the core of the burst.
                vx, vy = bx - cx0, by - cy0
                push = scatter * fw * ease_out(t) / (_spread or 1.0)
                dx = vx * push
                dy = vy * push + spec["gravity"] * fh * ease_in(t)
                if spec["jitter"]:
                    # Deterministic wobble: a fixed function of (part, frame),
                    # never a random draw. Two identical throws must look
                    # identical, and a loop must close.
                    ang = 2 * math.pi * ((f / max(1, frames)) + idx * 0.37 + seed * 0.11)
                    dx += math.cos(ang) * spec["jitter"]
                    dy += math.sin(ang) * spec["jitter"]
                sub = _blob_image(peak_img, blob, ratio_x, ratio_y)
                if sub is None:
                    continue
                img, px0, py0 = sub
                at_x = int(round(ox + px0 + dx))
                at_y = int(round(oy + py0 + dy))
                frame.alpha_composite(img, (at_x, at_y))
                placed.append((at_x + img.width / 2.0, at_y + img.height / 2.0))

        # Whole-effect travel, applied after placement so it moves parts and
        # wholes identically.
        ddx, ddy = spec["drift"]
        if ddx or ddy:
            shift = (int(round(ddx * fw * ease_out(t))),
                     int(round(ddy * fh * ease_out(t))))
            shifted = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
            shifted.alpha_composite(frame, shift)
            frame = shifted
            placed = [(cx + shift[0], cy + shift[1]) for cx, cy in placed]

        coverage = 1.0 - (1.0 - spec["fade"]) * ease_in(t)
        out.append(_dissolve(frame, coverage, chunk, centres=placed or None))
    return out


def _blob_image(src, blob: list[int], ratio_x: float, ratio_y: float):
    """Cut one connected part out of the peak image, scaled with it.

    Returns (image, x, y) positioned in the SCALED image's space, or None for a
    part too small to survive scaling — a single stray pixel is keying noise,
    not art, and carrying it costs a paste per frame.
    """
    from PIL import Image

    if len(blob) < 3:
        return None
    w = src.width
    xs = [i % w for i in blob]
    ys = [i // w for i in blob]
    x0, x1, y0, y1 = min(xs), max(xs) + 1, min(ys), max(ys) + 1
    patch = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    spx = src.load()
    ppx = patch.load()
    for i in blob:
        x, y = i % w, i // w
        ppx[x - x0, y - y0] = spx[x, y]
    scaled = _scaled(patch, ratio_x, ratio_y)
    return scaled, int(round(x0 * ratio_x)), int(round(y0 * ratio_y))


# ── the deliverable ──────────────────────────────────────────────────────────

def animate(key_path: str, out_dir: str, name: str, *, motion: str = "burst",
            frames: int = 4, peak: int = 1, cell: tuple[int, int] = (64, 64),
            fps: float = 14.0, res_dir: str = "assets/vfx", pad: float = 0.92,
            anim: str = "default", loop: Optional[bool] = None,
            overrides: Optional[dict] = None, seed: int = 0) -> dict:
    """ONE approved key frame -> frames + `<name>_sheet.png` + `<name>_frames.tres`.

    The sheet and the SpriteFrames come out of the SAME emitters
    `bgate_adapters.sprites` uses for character work, so a VFX set and a fighter
    set are the same kind of file and a project only has to learn one contract.

    Returns {ok, sheet, tres, frames, anchor, cell, motion, loop, coverage} —
    `anchor` being the pixel inside a frame that the effect is registered to,
    which is what a runtime manifest needs to place it.
    """
    from PIL import Image

    from bgate_adapters.sprites import _group_frames, _sprite_frames_tres, _stitch

    if motion not in MOTIONS:
        return {"ok": False, "error": f"unknown motion {motion!r} — one of "
                                      f"{sorted(MOTIONS)}"}
    if frames < 1:
        return {"ok": False, "error": "frames must be >= 1"}
    key = Path(key_path)
    if not key.exists():
        return {"ok": False, "error": f"no key frame at {key}"}

    src = Image.open(key).convert("RGBA")
    if not src.getbbox():
        return {"ok": False, "error": "key frame is fully transparent"}

    imgs = derive_frames(src, motion=motion, frames=frames, peak=peak,
                         cell=cell, pad=pad, overrides=overrides, seed=seed)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    names, paths = [], []
    for i, img in enumerate(imgs):
        pose = f"{anim}/{i}"
        dest = out / f"{name}_{anim}_{i}.png"
        img.save(dest)
        names.append(pose)
        paths.append(str(dest))

    sheet = out / f"{name}_sheet.png"
    _stitch(paths, sheet)
    is_loop = MOTIONS[motion]["loop"] if loop is None else bool(loop)
    tres = out / f"{name}_frames.tres"
    tres.write_text(
        _sprite_frames_tres(sheet.name, _group_frames(names), cell, fps, res_dir,
                            no_loop=() if is_loop else (anim,)),
        encoding="utf-8")

    cov = [round(_coverage(i), 4) for i in imgs]
    res = {"ok": True, "name": name, "sheet": str(sheet), "tres": str(tres),
           "frames": paths, "cell": list(cell),
           "anchor": [cell[0] // 2, cell[1] // 2],
           "motion": motion, "loop": is_loop, "parts": parts_in(src),
           "coverage": cov, "notes": []}

    # A motion that moves parts INDEPENDENTLY needs there to BE parts. A key
    # frame drawn as one solid mass has exactly one, so `scatter` and `gravity`
    # silently do nothing and the caller gets a set that merely grows — which
    # looks like the tool working. Say so instead: the fix is a key frame drawn
    # already broken (cracked, in pieces, mid-scatter), not a parameter.
    if MOTIONS[motion]["scatter"] > 0 and res["parts"] < 2:
        res["notes"].append(
            f"motion '{motion}' scatters parts but the key frame is ONE connected "
            "shape, so nothing flew apart — it only grew. Redraw the key frame "
            "already broken into separate pieces.")
    # The decay claim, checked rather than assumed.
    tail = cov[peak:]
    if not is_loop and len(tail) > 1 and any(
            b > a + 0.02 for a, b in zip(tail, tail[1:])):
        res["notes"].append(
            "coverage rises after the peak — expansion is outrunning the fade. "
            "Lower `expand` or `fade` if this should read as dying away.")
    return res


def parts_in(key_img) -> int:
    """How many separate pieces a key frame is drawn in."""
    img = _trim(key_img.convert("RGBA"))
    alpha = list(img.getchannel("A").getdata())
    return len(_components(alpha, img.width, img.height))


def _coverage(img) -> float:
    """Fraction of the frame that is solid. Reported per frame so a caller can
    ASSERT the decay is monotonic instead of trusting that it looked right."""
    a = img.getchannel("A").getdata()
    n = img.width * img.height
    return sum(1 for v in a if v > ALPHA_FLOOR) / max(1, n)
