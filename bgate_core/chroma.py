"""The keyable-background contract — how a sprite gets alpha when no model gives it.

MEASURED, NOT ASSUMED (2026-07-25, one 4-frame character sheet through both
providers):

  * gpt-image-1 called with ``background="transparent"`` returned a BROWN
    GRADIENT behind the character. The parameter is a request, not a guarantee,
    and when it does land it punches white interiors (the whites of the eyes) to
    holes — see ``sprites._close_interior_holes``, which exists only to repair
    that damage.
  * Krea has no transparency parameter on ANY of its models. Not a weak one —
    none. It cannot return alpha, ever.

So alpha is not something the pipeline can ASK for. It has to be MANUFACTURED:
force the model to paint a flat, uniform, saturated backdrop in a colour the art
never uses, then key that colour out deterministically and PROVE the result is
clean. That whole loop lives here, in one place, because the sprite path has to
behave identically whichever model produced the pixels — a sheet from Krea and a
sheet from gpt-image must be the same kind of file by the time they reach
``sprites.from_pose_images``.

Three parts, in order:

  1. :func:`pick` — the key colour, chosen AGAINST the art's own palette. A
     character in a green shirt must never get a green screen; that is the
     failure this reasoning exists to prevent.
  2. :func:`clause` — the prompt text that demands the flat backdrop. Explicit
     and repetitive on purpose: "flat", "uniform", "single solid colour", plus
     the exact RGB triple. A model that is merely *asked nicely* paints a
     vignette.
  3. :func:`finish` — key it out, then AUDIT it. An image whose background was
     never uniform enough to key cleanly is a FAILURE with a named flag, not a
     sprite with dirty alpha that poisons everything downstream. The audit is
     the gate; without it step 2 is a wish.

Not everything wants this. A background plate with its background removed is
nothing at all — see :data:`KEYED_KINDS` / :data:`PLATE_KINDS`.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

# Candidate key colours, saturated and far apart. Extremes on purpose: the
# further the key sits from anything a character is plausibly painted in, the
# wider the keying tolerance can be without eating the art.
CHROMA: list[tuple[str, tuple[int, int, int]]] = [
    ("magenta", (255, 0, 255)),
    ("green", (0, 255, 0)),
    ("cyan", (0, 255, 255)),
    ("blue", (0, 64, 255)),
    ("yellow", (255, 235, 0)),
]
DEFAULT_CHROMA = CHROMA[0]

# Below this the "key colour" is really one of the character's own colours and
# keying will eat the art. It is not a hard refusal (the art may genuinely span
# the wheel) but it is reported so a bad cut has a stated cause instead of being
# a mystery. Euclidean RGB distance; the full diagonal is ~441.
MIN_SAFE_DISTANCE = 120.0

# ---------------------------------------------------------------------------
# Which work gets a keyable background — the split, stated explicitly
# ---------------------------------------------------------------------------
# Kinds where THE TEXT IS THE ASSET — a wordmark, a team logo, an insignia.
# Mirrors artdirection.DECAL_KINDS (imported lazily below to keep this module
# free of a cycle; :func:`text_is_subject` prefers the live set when it can
# reach it, so the two cannot drift apart silently).
#
# These are sprite-shaped — a decal composites onto a cap, a wall or a shirt, so
# its background is waste exactly like a sprite's. They were held OFF the keyed
# path for one reason only: :func:`clause` hard-coded "NO text" into the
# background contract, so routing a logo through it reintroduced the very ban
# that made logos impossible, and the caller had to fall back to asking the API
# for transparency — which no model in either provider actually grants. The
# clause now has a text-is-the-subject variant, so they belong here.
TEXT_SUBJECT_KINDS = frozenset({
    "decal", "logo", "insignia", "emblem", "sticker",
})

# Sprite-shaped: the thing composites OVER game art, so its background is not
# part of the asset and every pixel of it is waste that has to come out.
KEYED_KINDS = frozenset({
    "anchor",     # the canonical character — every later frame derives from it
    "animation",  # pose frames / sheets
    "item",       # inventory and gear art
    "sprite", "sheet", "gear", "prop", "portrait", "icon",
    "vfx",        # an effect key frame — composites over the game, so alpha or
                  # nothing. See bgate_core.vfx for what happens to it next.
}) | TEXT_SUBJECT_KINDS
# Full-bleed: the background IS the asset. Keying these produces an empty file.
# `concept` sits here deliberately — exploration wants to see the model's own
# framing and lighting, and a concept pass is not a shippable sprite.
PLATE_KINDS = frozenset({
    "background", "tile", "ui", "concept", "plate", "backdrop", "splash",
})


def needs_key(task_kind: str) -> bool:
    """Does this kind of asset go through the keyable path?

    Unknown kinds answer False. Getting this wrong in the "yes" direction
    destroys the asset (a keyed backdrop is a blank file); getting it wrong in
    the "no" direction produces an opaque sprite that ``sprites`` refuses loudly
    and a human can re-run. Fail toward the recoverable mistake.
    """
    return str(task_kind or "").strip().lower() in KEYED_KINDS


def text_is_subject(task_kind: str) -> bool:
    """Is the writing on this asset the POINT of it?

    A decal is the one keyable kind whose background contract must not forbid
    lettering. Asks artdirection for the live list first — that module owns the
    decal vocabulary and its aliases — and falls back to the local mirror when
    it cannot be imported (chroma is imported BY artdirection in some paths).
    """
    kind = str(task_kind or "").strip().lower()
    try:
        from . import artdirection as _ad

        return _ad.is_decal_kind(kind)
    except Exception:
        return kind in TEXT_SUBJECT_KINDS


# ---------------------------------------------------------------------------
# 1. Picking a key colour that does not collide with the art
# ---------------------------------------------------------------------------

def palette_of(path: str | os.PathLike[str], colors: int = 10) -> list[tuple[int, int, int]]:
    """Dominant opaque colours of an image, quantized. [] if unreadable.

    Alpha-gated (a > 60) so a reference that is ALREADY keyed reports the
    character's palette rather than the void around it.
    """
    try:
        from PIL import Image

        im = Image.open(path).convert("RGBA")
        im.thumbnail((128, 128))
        px = [(r, g, b) for r, g, b, a in im.getdata() if a > 60]
        if not px:
            return []
        quant = Image.new("RGB", (len(px), 1))
        quant.putdata(px)
        quant = quant.quantize(colors)
        # Only the entries actually USED. quantize pads its palette to `colors`
        # with zeros when the image has fewer distinct shades, and those padded
        # slots used to come back as empty/black tuples — which made
        # distance_to() answer 0 for everything (zip against an empty tuple
        # sums nothing). That silently reported "this key collides with the art"
        # for every candidate, i.e. it defeated the collision check that stops a
        # green character getting a green screen.
        used = {i for i in quant.getdata()}
        pal = quant.getpalette() or []
        out: list[tuple[int, int, int]] = []
        for i in sorted(used):
            rgb = tuple(pal[i * 3:i * 3 + 3])
            if len(rgb) == 3:
                out.append(rgb)
        return out
    except Exception:
        return []


def distance_to(rgb: Sequence[int], palette: Iterable[Sequence[int]]) -> float:
    """Distance from `rgb` to the NEAREST colour in `palette` (inf if empty).

    Nearest, not average: a key colour is safe only if it collides with nothing.
    Averaging would let one green shirt hide behind a lot of grey armour.
    """
    best = float("inf")
    for other in palette:
        # Guard the shape. zip() silently truncates, so a malformed entry used
        # to score 0 — "collides with everything" — instead of being ignored.
        if other is None or len(other) < 3:
            continue
        d = sum((int(a) - int(b)) ** 2 for a, b in zip(rgb, other)) ** 0.5
        best = min(best, d)
    return best


def pick(ref_path: Optional[str | os.PathLike[str]] = None, *,
         anchors: Sequence[str | os.PathLike[str]] = (),
         palette: Sequence[Sequence[int]] = ()) -> tuple[str, tuple[int, int, int]]:
    """The key colour FARTHEST from the art's own palette. (name, rgb).

    Reasons from everything it is given: the working reference, any pinned
    anchor(s) — the anchor is the identity the frames must hold, so its greens
    count just as much as the reference's — and an explicit palette for callers
    that already sampled. With nothing to reason from it returns magenta, which
    is the least likely thing to appear in painted character art.
    """
    known: list[tuple[int, int, int]] = [tuple(int(c) for c in p) for p in palette]
    for src in ([ref_path] if ref_path else []) + list(anchors):
        if src:
            known.extend(palette_of(src))
    if not known:
        return DEFAULT_CHROMA
    best, best_d = DEFAULT_CHROMA, -1.0
    for name, rgb in CHROMA:
        d = distance_to(rgb, known)
        if d > best_d:
            best, best_d = (name, rgb), d
    return best


def pick_report(ref_path: Optional[str | os.PathLike[str]] = None, *,
                anchors: Sequence[str | os.PathLike[str]] = (),
                palette: Sequence[Sequence[int]] = ()) -> dict:
    """:func:`pick` with its evidence — the distance it won by, and whether that
    distance is actually safe. Callers put this in artifact metadata so a dirty
    cut can be traced to a colliding key instead of blamed on the model."""
    known: list[tuple[int, int, int]] = [tuple(int(c) for c in p) for p in palette]
    for src in ([ref_path] if ref_path else []) + list(anchors):
        if src:
            known.extend(palette_of(src))
    name, rgb = pick(ref_path, anchors=anchors, palette=palette)
    distance = distance_to(rgb, known) if known else None
    return {
        "name": name, "rgb": list(rgb),
        "distance": (None if distance is None else round(distance, 1)),
        "safe": (True if distance is None else distance >= MIN_SAFE_DISTANCE),
        "sampled_colors": len(known),
        "note": ("no palette to reason from — defaulted to magenta"
                 if distance is None else
                 f"nearest art colour is {distance:.0f} away"
                 + ("" if distance >= MIN_SAFE_DISTANCE else
                    " — CLOSE; the art shares this hue and the key may bite into it")),
    }


# ---------------------------------------------------------------------------
# 2. The prompt clause
# ---------------------------------------------------------------------------

# Callers wrote "fully transparent background" into their prompt templates back
# when we believed the API would deliver it (items.STYLE still does). Left in
# beside the flat-chroma clause it is a direct contradiction, and the model
# splits the difference: a half-transparent image with chroma smeared through
# it, which keys into swiss cheese. Strip the wish, keep the mechanism.
_ASKS_FOR_ALPHA = re.compile(
    r"[^.,;]*\b(fully\s+|completely\s+)?transparent\s+background\b[^.,;]*[.,;]?",
    re.I)


def strip_transparency_asks(prompt: str) -> str:
    """Remove any 'transparent background' phrasing from a prompt.

    Only used on the keyed path — the clause that replaces it is stricter and
    actually achievable.
    """
    return re.sub(r"\s{2,}", " ", _ASKS_FOR_ALPHA.sub(" ", str(prompt or ""))).strip()


def clause(chroma: tuple[str, tuple[int, int, int]] | None = None, *,
           subject: str = "character", text_is_subject: bool = False) -> str:
    """The background contract, as prompt text. Blunt and redundant on purpose.

    Every phrase here is scar tissue from a real generation: "no gradient"
    (gpt-image returned a brown gradient while being asked for transparency),
    "no vignette" and "no shadow on the background" (both keyed as fringe and
    tripped the halo audit), "fully inside the frame" (a limb touching the edge
    reads as background bleed to the audit and cannot be trimmed). Naming the
    exact RGB triple matters too — "magenta" alone gets a tasteful mauve.

    ``text_is_subject`` is the DECAL variant of the same contract, and it exists
    because "NO text" was written in here unconditionally. A logo or a wordmark
    is sprite-shaped — it composites onto a cap, a wall or a shirt, so it needs
    alpha exactly like a sprite does — but routing one through the keyed path
    handed the model an instruction to omit the only thing being asked for, and
    the kind had to stay off :data:`KEYED_KINDS` and rely on the caller passing
    ``transparent=True``, which no provider actually honours. The ban is scoped
    to the BACKGROUND here instead and the lettering is demanded rather than
    forbidden. Everything the audit depends on — the flatness, the exact RGB,
    the margin — is unchanged, because the cut still has to be clean.
    """
    name, rgb = chroma or DEFAULT_CHROMA
    # The one clause that flips. Everything else in this contract is about the
    # BACKDROP; "NO text" was the only line that reached onto the subject.
    forbidden = ("NO other objects" if text_is_subject
                 else "NO other objects, NO text")
    demanded = (
        f" The lettering, wordmark or mark IS the {subject} and must be "
        f"rendered in full — complete, crisp, high-contrast and exactly as "
        f"specified. The ban on text above applies to the BACKGROUND ONLY: the "
        f"flat {name} field carries no writing, no watermark, no label and no "
        f"caption of its own."
        if text_is_subject else "")
    return (
        f" BACKGROUND (mandatory): place the {subject} on a COMPLETELY FLAT, "
        f"UNIFORM, SINGLE SOLID {name} background, exact colour RGB "
        f"{rgb[0]},{rgb[1]},{rgb[2]}, filling the entire frame edge to edge "
        f"behind the subject. NO gradient, NO vignette, NO lighting falloff, NO "
        f"texture, NO pattern, NO ground plane, NO cast shadow on the "
        f"background, {forbidden}. That exact {name} must appear "
        f"NOWHERE on the {subject} itself.{demanded} The {subject} must be "
        f"entirely inside the frame with a clear margin on all four sides — "
        f"nothing touching or crossing the edge."
    )


# ---------------------------------------------------------------------------
# 3. Keying, and the audit that decides whether it worked
# ---------------------------------------------------------------------------

def key(img, chroma: Sequence[int], tol: int = 125, despill: int = 185):
    """Key a solid chroma backdrop to transparent, in place, with edge despill.
    Distance-based; safe because the chroma is picked far from the art.

    Whole-band Pillow math, not a per-pixel loop. This runs on every generated
    pose at 1024x1536 — 1.6M pixels, and the Python loop it replaces cost
    seconds of pure interpreter time per frame while holding a worker thread.
    Comparing SQUARED distance keeps it in integer bands (no sqrt, which
    ImageMath has no function for) and the ordering is identical, so the same
    pixels are keyed as before.
    """
    from PIL import Image as _I, ImageChops as _IC, ImageMath as _IM

    # unsafe_eval is ImageMath.eval renamed in Pillow 10.3 (the old name warns).
    ev = getattr(_IM, "unsafe_eval", None) or _IM.eval
    cr, cg, cb = chroma
    near, band = tol * tol, despill * despill
    r, g, b, a = img.split()
    d2 = ev("(r-cr)*(r-cr)+(g-cg)*(g-cg)+(b-cb)*(b-cb)",
            r=r, g=g, b=b, cr=cr, cg=cg, cb=cb)
    # *255: an ImageMath comparison yields 1, and a mask of 1 is a 1/255 blend —
    # it looks like the key silently did almost nothing.
    keyed = ev("convert((d2 < near) * 255, 'L')", d2=d2, near=near)
    fringe = ev("convert(min(d2 >= near, d2 < band) * 255, 'L')",
                d2=d2, near=near, band=band)
    # SECOND PASS ON PINKNESS, for the backdrop the sphere cannot reach.
    #
    # A distance key assumes the model painted the colour it was asked for. Krea
    # does not: it paints a desaturated version of it, far enough away to miss
    # the sphere and close enough that a human looking at the frame calls it
    # magenta. The result was an opaque pink slab that passed the audit on the
    # strength of its border, which HAD keyed.
    #
    # Union rather than replacement — the sphere is exact where it applies, and
    # this only ever ADDS the off-axis pixels the sphere left behind. Gated on
    # the chroma actually being a pink, so a cyan or chartreuse key is untouched.
    if pinkness((cr, cg, cb)) >= PINK_CHROMA_MIN:
        # int(), not the raw float: ImageMath binds a scalar by building a
        # one-colour image out of it, and Image.new refuses a float — it raises
        # "color must be int or single-element tuple", which names neither
        # ImageMath nor the argument and would land in finish()'s except as an
        # unexplained "chroma key failed".
        # Doubled to keep it division-free — see is_pinker_than, which is the
        # same predicate and must agree with this one pixel for pixel.
        floor = int(pinkness((cr, cg, cb)) * PINK_KEY_FRACTION)
        pinkish = ev("convert((((r+b) - 2*g) > twice) * 255, 'L')",
                     r=r, g=g, b=b, twice=2 * floor)
        keyed = ev("convert(max(keyed, pinkish), 'L')",
                   keyed=keyed, pinkish=pinkish)
        # Anything newly keyed must not also be treated as fringe, or the
        # despill below paints grey into pixels that are about to become
        # transparent — which is dirty alpha, the thing the audit fails on.
        fringe = ev("convert(min(fringe, 255 - keyed), 'L')",
                    fringe=fringe, keyed=keyed)
    # int() before convert() so the halving floors exactly like the // it
    # replaces — an F->L convert would be free to land a pixel one step off.
    grey = ev("convert(int((r+g+b)/3), 'L')", r=r, g=g, b=b)
    softened = _I.merge("RGB", tuple(
        ev("convert(int((c+m)/2), 'L')", c=c, m=grey) for c in (r, g, b)))
    img.paste(softened, (0, 0), fringe)
    # RGB:=0 under the key, not just alpha:=0. Leaving the chroma color sitting
    # under transparent pixels is exactly the "dirty alpha" the audit fails on,
    # and it fringes green/magenta the moment anything rescales.
    img.paste((0, 0, 0, 0), (0, 0), keyed)
    # Alpha LAST: the paste above is RGB-only in intent but Pillow promotes the
    # source to RGBA, so anything written to alpha before it would be lost.
    # Subtracting the 255-valued key mask clamps the keyed pixels to alpha 0 and
    # leaves every other pixel's alpha exactly where it was.
    img.putalpha(_IC.subtract(a, keyed))
    return img


# Audit thresholds. Every one of these reads ~0 on a cleanly keyed sprite and
# spikes on a specific, named failure — they are not tuned for taste.
BORDER_OPAQUE_MAX = 0.06   # background bleed: the key did not reach the edge
WHITE_FRINGE_MAX = 0.20    # white halo baked into the soft edge
SOFT_ALPHA_MAX = 0.35      # feathered/mushy cut instead of a crisp one
DIRTY_ALPHA_MAX = 0.15     # colour still sitting under alpha 0
HOLLOW_REVIEW = 0.05       # enclosed transparency — maybe a curled arm
HOLLOW_FAIL = 0.12         # ...or the key ate the art from the inside
# The key colour SURVIVING inside the frame. Every threshold above measures the
# cut; this one measures what the cut missed, which is the failure they all
# walked past — see the `residual_chroma` paragraph in audit().
RESIDUAL_CHROMA_MAX = 0.04


# PINKNESS, and why a colour distance is not enough.
#
# `key` compares squared RGB distance to the chroma, which is a SPHERE around
# one point. Krea ignores the pure-magenta backdrop contract and paints a DUSTY
# pink — measured at ~#c0559f, which is 143 away from (255,0,255) and therefore
# outside the default tol=125 sphere. Widening the sphere is not the fix: at 143
# it starts swallowing skin, brick and autumn leaf.
#
# The backdrop is not an arbitrary colour, though. It is the chroma DESATURATED
# toward grey, so it keeps the one property that made magenta a good key in the
# first place — red and blue high, green low — while losing the distance. That
# property is a scalar:
#
#     pinkness = (R + B) / 2 - G
#
# Measured on real frames from the run this came out of:
#
#     pure magenta   (255,  0,255) -> 255      the contract
#     krea's dusty   (192, 85,159) ->  90      what actually arrived
#     pink petal     (240,150,180) ->  60      must SURVIVE
#     buff plume     (220,200,160) -> -10      must survive
#     brown twig     (110, 80, 50) ->   0      must survive
#     green leaf     ( 60,120, 50) -> -65      must survive
#
# so a threshold anywhere in 65..85 separates the backdrop from every subject
# colour that caused trouble. 0.33 of the chroma's own pinkness puts it at 84 for
# magenta, which is the widest setting that still spares the petal.
PINK_KEY_FRACTION = 0.33
# Below this the chroma is not a pink at all (cyan, chartreuse, orange), the
# scalar means nothing, and the pass does not run.
PINK_CHROMA_MIN = 150


def pinkness(rgb: Sequence[int]) -> float:
    """``(R+B)/2 - G`` — how much a colour is magenta rather than anything else.

    Positive for magenta/pink, ~0 for neutrals and browns, negative for greens.
    Named because it is used in two places that must agree: the second keying
    pass and the audit that checks whether it worked.
    """
    r, g, b = (float(c) for c in rgb[:3])
    return (r + b) / 2.0 - g


def is_pinker_than(rgb: Sequence[int], floor: int) -> bool:
    """``pinkness(rgb) > floor``, in EXACT INTEGER ARITHMETIC.

    Doubled on both sides — ``(R+B) - 2G > 2*floor`` — so there is no division
    and therefore no rounding to disagree about. That matters because the same
    predicate is evaluated three ways: here per pixel, as a Pillow band
    expression inside :func:`key`, and per pixel again in the audit. ImageMath
    runs its arithmetic on int32 bands where ``/`` FLOORS, so a pixel whose true
    pinkness is 84.5 tested as 84 in the band version and 84.5 here — the two
    paths disagreed on exactly the colours sitting on the threshold, which is
    the worst possible place for them to differ and cost a real test failure.
    """
    r, g, b = (int(c) for c in rgb[:3])
    return (r + b) - 2 * g > 2 * int(floor)


def audit(path: str | os.PathLike[str],
          chroma: Optional[Sequence[int]] = None) -> dict:
    """Measure whether an image is actually a clean cut-out. Flags, not taste.

    Was born inside consistency_check as a review tripwire for gpt-image's
    transparent mode — white halos, feathered fringes, opaque background bleed,
    dirty RGB under zero alpha, hollow interiors — all of which a
    checklist-by-eye kept missing. It lives here now because the keyable path
    needs it as a GATE at generation time, not as a comment during review: a
    frame with dirty alpha that reaches ``sprites`` is already too late.

    ``residual_chroma`` IS THE ONE THAT WAS MISSING, and the gap it left is the
    reason to read this paragraph before adding another threshold. Every other
    measurement here inspects the CUT — the border, the soft edge, the pixels
    under zero alpha, the enclosed holes. Not one of them inspects what the cut
    left behind. So when Krea painted a dusty pink backdrop the sphere key could
    not reach, the frame border keyed (it was closest to the contract colour),
    the interior did not, and every threshold above read clean over an opaque
    pink slab. ``clean: true``, shipped.

    The shape of that bug is worth naming because it recurs: the check validated
    a PROXY for the artifact (the border) instead of the artifact. Pass
    ``chroma`` and this one asks the direct question — is the key colour still
    sitting in the frame, opaque, after keying? — using :func:`pinkness` rather
    than a distance, because the surviving backdrop is the chroma desaturated
    and a distance is exactly what it escaped.

    Without ``chroma`` the measurement is skipped and reported as ``None``, not
    as zero: "not checked" and "checked and clean" must not look the same.

    Returns {border_opaque, white_fringe, soft_alpha, dirty_alpha, hollow,
    residual_chroma, flags, review, clean}. `flags` are hard failures; `review`
    is advisory.
    """
    from PIL import Image

    im = Image.open(path).convert("RGBA")
    im.thumbnail((256, 256))
    W, H = im.size
    px = im.load()
    border = border_op = soft = opaque = softc = whal = 0
    dirty = transp = 0
    # Only meaningful for a pink key; see PINK_CHROMA_MIN.
    pink_floor = (pinkness(chroma) * PINK_KEY_FRACTION
                  if chroma is not None and pinkness(chroma) >= PINK_CHROMA_MIN
                  else None)
    residual = 0
    for y in range(H):
        for x in range(W):
            r, g, b, a = px[x, y]
            if x == 0 or y == 0 or x == W - 1 or y == H - 1:
                border += 1
                if a > 32:
                    border_op += 1
            if a >= 224:
                opaque += 1
                if pink_floor is not None and is_pinker_than((r, g, b), pink_floor):
                    residual += 1
            if 24 < a < 224:
                soft += 1
            if 24 < a < 240:
                softc += 1
                if r > 228 and g > 228 and b > 228:
                    whal += 1
            if a <= 8:
                transp += 1
                if r > 16 or g > 16 or b > 16:
                    dirty += 1
    border_opaque = border_op / max(1, border)
    # Over the WHOLE FRAME, not over the opaque pixels. A slab that barely keyed
    # is nearly all opaque, so dividing by the opaque count would put a
    # completely failed cut and a perfect one on the same denominator and read
    # the disaster as a modest percentage.
    residual_chroma = (residual / max(1, W * H)) if pink_floor is not None else None
    soft_ratio = soft / max(1, opaque + soft)
    white_fringe = whal / max(1, softc)
    dirty_alpha = dirty / max(1, transp)

    # HOLLOW = transparent ENCLOSED by opaque (a real hole), not the open gaps
    # between spread limbs. Flood-fill transparency inward from the frame
    # border; whatever transparency it cannot reach is enclosed.
    seen = bytearray(W * H)
    stack = []
    for x in range(W):
        for y in (0, H - 1):
            i = y * W + x
            if px[x, y][3] <= 16 and not seen[i]:
                seen[i] = 1; stack.append((x, y))
    for y in range(H):
        for x in (0, W - 1):
            i = y * W + x
            if px[x, y][3] <= 16 and not seen[i]:
                seen[i] = 1; stack.append((x, y))
    while stack:
        x, y = stack.pop()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < W and 0 <= ny < H:
                i = ny * W + nx
                if not seen[i] and px[nx, ny][3] <= 16:
                    seen[i] = 1; stack.append((nx, ny))
    enclosed = sum(1 for y in range(H) for x in range(W)
                   if px[x, y][3] <= 16 and not seen[y * W + x])
    hollow = enclosed / max(1, opaque)

    flags = []
    if border_opaque > BORDER_OPAQUE_MAX:
        flags.append(f"background bleed: {border_opaque:.0%} of the frame "
                     "border is opaque — the background was not a flat keyable "
                     "colour (gradient/vignette), or the subject runs off frame")
    if white_fringe > WHITE_FRINGE_MAX:
        flags.append(f"white halo: {white_fringe:.0%} of soft-edge pixels are "
                     "near-white — feathered white fringe around the sprite")
    if soft_ratio > SOFT_ALPHA_MAX:
        flags.append(f"feathered alpha: {soft_ratio:.0%} soft/partial-alpha — "
                     "edges aren't crisp (the backdrop shaded into the subject)")
    if dirty_alpha > DIRTY_ALPHA_MAX:
        flags.append(f"dirty alpha: {dirty_alpha:.0%} of transparent pixels "
                     "carry nonzero RGB — clean RGB:=0 where alpha==0")
    if residual_chroma is not None and residual_chroma > RESIDUAL_CHROMA_MAX:
        flags.append(
            f"unkeyed backdrop: {residual_chroma:.0%} of the frame is still "
            "opaque key colour. The model painted a DESATURATED version of the "
            "chroma (measured ~#c0559f against a pure-magenta contract), which "
            "is too far off for a distance key and near enough that the border "
            "keyed and the middle did not. This is a slab, not a sprite — "
            "regenerate; do not ship it.")
    # Enclosed transparency is ambiguous in general (a curled arm makes a real
    # hole) so consistency_check only ever flagged it for a human. In the KEYED
    # path it stops being ambiguous past a point: a large enclosed hole means
    # the key colour appeared INSIDE the art and was cut out of it, which is the
    # exact collision `pick` exists to prevent. Advisory below, fail above.
    review = []
    if hollow > HOLLOW_FAIL:
        flags.append(f"hollow interior: {hollow:.0%} of the figure is enclosed "
                     "transparency — the key colour appeared in the art and was "
                     "cut out of it; re-run with a different key")
    elif hollow > HOLLOW_REVIEW:
        review.append(f"possible hole: {hollow:.0%} of the figure is "
                      "transparent area ENCLOSED by the sprite — look for an "
                      "empty/holed region (vs. intended open gaps)")
    return {"border_opaque": round(border_opaque, 3),
            "white_fringe": round(white_fringe, 3),
            "soft_alpha": round(soft_ratio, 3),
            "dirty_alpha": round(dirty_alpha, 3),
            "hollow": round(hollow, 3),
            # None, not 0.0, when no chroma was passed — "not checked" and
            # "checked and clean" reading the same is how this got shipped.
            "residual_chroma": (round(residual_chroma, 3)
                                if residual_chroma is not None else None),
            "flags": flags, "review": review, "clean": not flags}


def finish(path: str | os.PathLike[str], chroma: Sequence[int], *,
           name: str = "", tol: int = 125, despill: int = 185) -> dict:
    """Key `path` in place, then audit it. The gate, not a decoration.

    Returns {ok, chroma, alpha, error}. ok=False means the background was never
    uniform enough to key cleanly and the file must NOT be treated as a sprite —
    the caller gets the specific flag ("background bleed", "white halo", ...) so
    the next attempt can change something real. The keyed file is left on disk
    either way: a human has to be able to LOOK at what failed.
    """
    from PIL import Image

    try:
        img = Image.open(path).convert("RGBA")
        key(img, tuple(int(c) for c in chroma), tol=tol, despill=despill)
        img.save(path)
    except Exception as exc:
        return {"ok": False, "chroma": name, "alpha": {},
                "error": f"chroma key failed on {path}: {type(exc).__name__}: {exc}"}
    try:
        # The chroma is passed so the audit can ask whether the key colour is
        # still in the frame. Without it the audit can only inspect the cut, and
        # the cut looks fine on an image that was never cut. See audit().
        flags = audit(path, chroma=tuple(int(c) for c in chroma))
    except Exception as exc:
        # An audit that cannot run must not silently pass the frame — the whole
        # point of this contract is that nothing ships unverified.
        return {"ok": False, "chroma": name, "alpha": {},
                "error": f"alpha audit could not run on {path}: "
                         f"{type(exc).__name__}: {exc}"}
    if flags["flags"]:
        return {"ok": False, "chroma": name, "alpha": flags,
                "error": "the background did not key cleanly: "
                         + "; ".join(flags["flags"])
                         + f" — the model ignored the flat-{name or 'chroma'} "
                           "background contract. Regenerate; do not ship this as "
                           "a sprite."}
    return {"ok": True, "chroma": name, "alpha": flags, "error": ""}


# ---------------------------------------------------------------------------
# The one door both providers walk through
# ---------------------------------------------------------------------------

def _trained_styles(root: Any) -> list:
    """This project's trained style as Krea wants it, or [].

    Guarded and lazy: a generation must not fail because a settings doc will not
    parse, and `styles` imports the adapter, which this module is imported BY in
    some paths. Returning [] means "generate the way this project always did",
    which is also what happens when nothing has been trained.
    """
    try:
        from . import styles as _styles

        return _styles.for_generation(root)
    except Exception:
        return []


def generate(prompt: str, out_path: str | os.PathLike[str], *,
             provider: str, model: str = "", task_kind: str = "",
             keyed: Optional[bool] = None, size: str = "1024x1024",
             quality: str = "medium", seed: Optional[int] = None,
             ref_paths: Sequence[str] = (), ref_strength: float = 0.5,
             anchors: Sequence[str] = (), transparent: bool = False,
             timeout: float = 300.0, root: Any = None,
             logical_name: str = "", work_item_id: Optional[int] = None,
             tileable: bool = False) -> dict:
    """Generate one image through the keyable-background contract.

    This is the ONLY thing sprite-shaped work should call. It picks the key
    colour, appends the clause, dispatches to whichever provider, keys the
    result and audits it — so a Krea sprite and a gpt-image sprite are the same
    kind of file when they come out, which is the entire point of having the
    contract in one place.

    ``keyed`` defaults from ``task_kind`` (see :func:`needs_key`). Pass it
    explicitly to override. When keying is OFF the prompt goes through
    untouched and ``transparent`` is honoured as the caller asked — a backdrop
    plate must not be handed a green screen.

    A DECAL KIND (decal/logo/insignia/emblem/sticker) is keyed like any other
    sprite-shaped asset and gets the text-is-the-subject variant of the
    contract — see :func:`clause`. It does not need, and must not be given,
    ``transparent=True``: that was the workaround for this kind being locked out
    of the keyed path, and asking the API for alpha has never worked.

    ``task_kind`` now reaches the ADAPTERS as well as the clause, because two
    decisions live down there and cannot be made from the prompt: a texture
    kind is forced square (a non-square map stretches across a unit UV and
    nothing downstream can undo it) and ``tileable`` runs the mirrored
    post-pass. Both are no-ops for every kind that is not a texture.

    Returns the adapters' shared result shape plus ``chroma`` (the pick and its
    evidence) and ``alpha`` (the audit). ``ok=False`` with an ``error`` when the
    provider failed OR when the audit rejected the cut.
    """
    from bgate_adapters import imagegen, kie, krea, localgen
    from bgate_core import artdirection

    provider = str(provider or "").strip().lower()
    if keyed is None:
        keyed = needs_key(task_kind)

    # The bible reaches the prompt HERE, at the one door every generation walks
    # through — otherwise "ART DIRECTION LOCKED" is a sentence in a database
    # that changes nothing, which is exactly how a corporate-satire project
    # generated a painterly fantasy paladin.
    art_clause = ""
    try:
        if root:
            art_clause = artdirection.clause(root, task_kind=task_kind,
                                             tileable=tileable)
        else:
            # No project, so no bible to read — but the FORM clause is not read
            # out of a bible. "A texture map carries no baked lighting" is true
            # of the asset kind, not of the project, so it must survive a
            # rootless call the way the style direction cannot.
            art_clause = artdirection.form_clause(task_kind, tileable=tileable)
    except Exception:
        art_clause = ""   # a missing bible must never block the work
    if art_clause:
        prompt = prompt + art_clause

    report = {}
    chroma_rgb = None
    if keyed:
        # Reason from the working reference AND the pinned anchors — the anchor
        # is the identity every frame must hold, so its palette constrains the
        # key just as hard as the reference's does.
        report = pick_report(ref_paths[0] if ref_paths else None,
                             anchors=list(anchors) + list(ref_paths[1:]))
        chroma_name, chroma_rgb = report["name"], tuple(report["rgb"])
        # A decal's whole content is writing, so it gets the variant of the
        # contract that scopes "NO text" to the backdrop. Without this the
        # keyable path and the decal art direction contradict each other in the
        # same prompt, and the model resolves that by dropping the logo.
        wordmark = text_is_subject(task_kind)
        prompt = strip_transparency_asks(prompt) + clause(
            (chroma_name, chroma_rgb),
            subject="graphic" if wordmark else "character",
            text_is_subject=wordmark)
        # NEVER also ask for transparency on the keyed path. Measured: gpt-image
        # honours neither reliably, and asking for both gets a transparent-ish
        # image with a gradient THROUGH it — two half-cuts instead of one clean
        # one. The flat backdrop is the mechanism; alpha is what we make.
        transparent = False
        # Any reference that is ITSELF a cut-out gets plated onto the key colour
        # first — see plate_reference for the $0.03 that taught us this.
        plated = []
        for ref in ref_paths:
            try:
                plated.append(plate_reference(ref, chroma_rgb,
                                              Path(out_path).parent / ".chroma_refs")
                              if opacity(ref) < 0.99 else str(ref))
            except Exception:
                plated.append(str(ref))   # a bad plate must not lose the anchor
        ref_paths = plated

    try:
        if provider == "krea":
            refs = [krea.style_ref(p, ref_strength) for p in ref_paths]
            # THE TRAINED STYLE, IF THIS PROJECT HAS ONE AND ASKED FOR IT.
            # Read at the same door the bible is appended at, for the same
            # reason: a look that applies to the art seat but not to a workflow
            # node is not this project's look. Empty unless art.style_source is
            # `lora` AND a style has finished training, so nothing changes for a
            # project that has not trained one.
            #
            # It rides ALONGSIDE the references rather than replacing them, and
            # that is the point of training: the LoRA carries the style, which
            # frees the reference slot to carry identity — the two jobs that
            # have been competing for one array.
            trained = _trained_styles(root)
            result = krea.generate(prompt, str(out_path),
                                   # Kind-aware, not one default for everything:
                                   # character work needs a model that EDITS a
                                   # reference, and the general default only
                                   # STYLES from one. See krea.model_for.
                                   model=model or krea.model_for(task_kind),
                                   size=size,
                                   seed=seed, style_refs=refs or None,
                                   styles=trained or None,
                                   quality=quality, task_kind=task_kind,
                                   tileable=tileable,
                                   timeout=timeout, root=root)
        elif provider in ("openai", "gpt-image", "imagegen"):
            if ref_paths:
                # gpt-image conditions on a reference by EDITING it — that is its
                # only way to hold an anchor, so a style ref becomes an edit.
                result = imagegen.edit(prompt, [str(p) for p in ref_paths],
                                       str(out_path), size=size, quality=quality,
                                       transparent=transparent, timeout=timeout,
                                       root=root, logical_name=logical_name,
                                       work_item_id=work_item_id,
                                       task_kind=task_kind, tileable=tileable)
            else:
                result = imagegen.generate(prompt, str(out_path), size=size,
                                           quality=quality,
                                           transparent=transparent,
                                           timeout=timeout, root=root,
                                           logical_name=logical_name,
                                           work_item_id=work_item_id,
                                           task_kind=task_kind,
                                           tileable=tileable)
        elif provider in ("local", "comfy", "localgen"):
            # LOCAL WALKS THROUGH THE SAME DOOR, and that is the whole design.
            # A sprite made on the user's own GPU has to be the same kind of
            # file as a Krea one — same keyable backdrop, same audited cut, same
            # art direction — or "local generation" becomes a second-class path
            # whose output nothing downstream can trust. The only difference
            # allowed here is which adapter paints the pixels.
            #
            # Referenced work goes through the EDIT graph for the same reason it
            # does on gpt-image: conditioning on a pinned reference is an
            # image-to-image operation, and a text-only graph has nowhere to put
            # the anchor.
            if ref_paths:
                result = localgen.edit(prompt, [str(p) for p in ref_paths],
                                       str(out_path), size=size, quality=quality,
                                       seed=seed, transparent=transparent,
                                       timeout=timeout, root=root,
                                       logical_name=logical_name,
                                       work_item_id=work_item_id,
                                       task_kind=task_kind, tileable=tileable)
            else:
                result = localgen.generate(prompt, str(out_path), size=size,
                                           quality=quality, seed=seed,
                                           transparent=transparent,
                                           timeout=timeout, root=root,
                                           logical_name=logical_name,
                                           work_item_id=work_item_id,
                                           task_kind=task_kind,
                                           tileable=tileable)
        elif provider == "kie":
            # kie WALKS THROUGH THIS DOOR TOO, but it is the first provider that
            # cannot take the anchors with it. Every image field kie documents
            # is a URI and its reference says nothing about base64 data URIs, so
            # a pinned ref on disk has nowhere to go — the adapter refuses one
            # rather than encoding it on a guess. Refusing HERE, before the
            # generation, is the difference between "kie cannot do anchored
            # work, use krea" and an unanchored image that was paid for and
            # looks nothing like the character.
            if ref_paths:
                return {"ok": False, "provider": provider, "model": model,
                        "error": "kie cannot condition on the pinned refs — its "
                                 "image fields take public URLs only, and every "
                                 "anchor here is a local file. Use provider "
                                 "'krea' for anchored work, or drop the refs."}
            result = kie.generate_image(prompt, str(out_path),
                                        model=model or kie.DEFAULT_IMAGE_MODEL,
                                        size=size, seed=seed,
                                        task_kind=task_kind, tileable=tileable,
                                        timeout=timeout, root=root,
                                        logical_name=logical_name,
                                        work_item_id=work_item_id)
        else:
            return {"ok": False, "provider": provider, "model": model,
                    "error": f"unknown provider {provider!r} — "
                             "'krea', 'openai', 'kie' or 'local'"}
    except Exception as exc:  # adapters raise on bad shapes; a caller must not
        return {"ok": False, "provider": provider, "model": model,
                "error": f"{type(exc).__name__}: {exc}"}

    result.setdefault("provider", provider)
    result["keyed"] = bool(keyed)
    result["art_direction_applied"] = bool(art_clause)
    if not keyed or not result.get("ok"):
        _art_check(result, root, out_path)
        return result

    result["chroma"] = report
    cut = finish(result.get("path") or str(out_path), chroma_rgb,
                 name=report["name"])
    result["alpha"] = cut.get("alpha")
    if not cut["ok"]:
        # The spend already happened and the file is on disk — say so, and hand
        # back the path so a human can look at what the model actually painted.
        result["ok"] = False
        result["error"] = cut["error"]
        result["rejected_path"] = result.get("path") or str(out_path)
    _art_check(result, root, out_path)
    return result


def _art_check(result: dict, root: Any, out_path: Any) -> None:
    """Measure the finished frame against the bible and attach the verdict.

    Runs on the keyed and unkeyed paths both — a backdrop can be off-model just
    as easily as a sprite. Only a LOCKED contradiction flips ``ok``; advisory
    drift is reported so a human can judge it, because a false rejection here
    throws away art that has already been paid for.
    """
    if not root:
        return
    path = result.get("path") or str(out_path)
    try:
        from pathlib import Path as _P
        if not _P(path).is_file():
            return
        from bgate_core import artdirection
        verdict = artdirection.check(root, path)
    except Exception:
        return  # never let the advisory gate take down the generation
    result["art_direction"] = verdict
    if result.get("ok") and not verdict["ok"]:
        names = ", ".join(f["flag"] for f in verdict["flags"] if f.get("locked"))
        result["ok"] = False
        result["error"] = (f"off art direction ({names}) — the bible locks this "
                           f"and the frame contradicts it")
        result["rejected_path"] = path


def plate_reference(ref_path: str | os.PathLike[str], rgb: Sequence[int],
                    work_dir: str | os.PathLike[str]) -> str:
    """A copy of `ref_path` composited ONTO the key colour. Path to the copy.

    MEASURED, and the reason this function exists: hand Krea an already-keyed
    (transparent) anchor as a style reference and it flattens the alpha to
    BLACK, then paints the new frame on a black background with a thin magenta
    rim light where it half-heard the prompt. 100% opaque border, audit
    rejected, $0.03 gone. gpt-image edits do the same thing more quietly.

    The reference is not just an identity cue — it is the strongest statement of
    what the image should look like, background included. So the background it
    shows has to be the background we want. Plate it.
    """
    from PIL import Image

    src = Image.open(ref_path).convert("RGBA")
    plate = Image.new("RGBA", src.size, tuple(int(c) for c in rgb) + (255,))
    plate.alpha_composite(src)
    out = Path(work_dir)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / (Path(ref_path).stem + "_on_chroma.png")
    plate.convert("RGB").save(dest)   # RGB: no alpha to flatten to black
    return str(dest)


def opacity(path: str | os.PathLike[str]) -> float:
    """Fraction of pixels that are (near-)opaque. 1.0 means no alpha at all.

    The cheap tripwire for "this was never keyed" — used by the sprite
    assembler, which silently produced framed screenshots-with-backgrounds when
    fed an opaque PNG.
    """
    try:
        from PIL import Image

        im = Image.open(path).convert("RGBA")
        im.thumbnail((128, 128))
        data = list(im.getdata())
        return sum(1 for _, _, _, a in data if a > 200) / max(1, len(data))
    except Exception:
        return 0.0


NO_ALPHA_HINT = (
    "the image has no transparency at all — it was never keyed. Sprite art must "
    "be generated through bgate_core.chroma.generate (the keyable-background "
    "contract): the model paints a flat chroma backdrop and it is keyed out and "
    "audited. Neither gpt-image's background=transparent nor any Krea model "
    "returns usable alpha on its own.")


def looks_unkeyed(path: str | os.PathLike[str], max_opaque: float = 0.97) -> bool:
    """True when a file that should be a cut-out is a solid rectangle."""
    return opacity(path) >= max_opaque
