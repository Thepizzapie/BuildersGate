"""Sprite-sheet quality, measured and ENFORCED — the arithmetic half of consistency.

WHERE THIS SITS. ``chroma`` makes alpha and proves the cut is clean. ``sprites``
assembles frames into a sheet plus a SpriteFrames resource. This module is what
goes between them: the deterministic operations that make a set of independently
generated frames read as ONE character MOVING, rather than as N drawings of a
character shown in sequence.

The split it belongs to is the same one ``vfx`` states outright — the model draws,
arithmetic holds identity over time. Every measurement here reads ~0 on a good
sheet and spikes on a specific, named failure. Nothing here calls a model, and
nothing here is a matter of taste.

FOUR THINGS THAT WERE BEING REQUESTED INSTEAD OF ENFORCED
─────────────────────────────────────────────────────────

1. REGISTRATION. Frames were centred on their ALPHA BOUNDING BOX. A bounding box
   is not a body: a jab that puts the lead fist a foot to the right widens the box
   to the right, so box-centring slides the TORSO left to compensate, and the
   character visibly steps sideways every time he punches. The fix is the
   alpha-weighted centroid — ``cx = Σ(x·α)/Σα``. The torso is most of the ink, so
   it dominates the centroid and stays put no matter where a limb goes. Published
   measurements on exactly this substitution put box-centred drift at ~27px of
   standard deviation against ~0.2px centroid-pinned; on our own frames the
   difference is a fighter who stands still while he punches.

   Note that the vertical axis does NOT get this treatment. Feet belong on a
   ground line, and a centroid pin would let a crouch float. X from the centroid,
   Y from the floor — see :func:`place_offset`.

2. PALETTE. The existing gate MEASURES palette drift (histogram intersection
   against the batch median) and re-rolls frames that fail. That is detection, and
   detection costs an image call per failure. Quantising every frame to the
   reference's own palette makes the drift arithmetically impossible instead: no
   frame can contain a colour the character does not have, because there is
   nowhere for such a colour to be stored. See :func:`lock_palette`, and read its
   docstring before turning it on — it is right for flat and limited-palette art
   and wrong for smoothly rendered painterly art.

3. DEBRIS AND DISMEMBERMENT. Every existing audit inspects the CUT (border,
   fringe, alpha under zero, enclosed holes). None of them asks the question a
   human asks first: is this ONE figure? A frame where the key ate through a wrist
   ships a hand floating beside an armless character, and a speckly key ships
   confetti. Both are connected-component counts (:func:`parts`), and both are
   invisible to every threshold that already exists.

4. MOTION. Height jitter was the only cross-frame measurement. It cannot see the
   three failures that actually get noticed in play: two frames that are the same
   drawing (the animation reads as a still, and one generation was wasted), two
   adjacent frames with nothing in common (a pose popped), and a cycle whose last
   frame does not flow back into its first (the loop hitches once per repetition,
   forever). All three are silhouette overlap — :func:`motion_report`.

EVERY LOOP IN HERE IS A BAND OPERATION WHERE PILLOW HAS ONE. This runs over
whole sheets on the same worker thread that holds the MCP call, and the mass
measurement it replaces (`sum(1 for px in img.getdata() if px[3] > 60)`) walked
1.5 million Python tuples per frame.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

# Alpha at or above which a pixel counts as "the character is here". Well above
# zero because a keyed cut-out carries a rim of near-zero alpha that is not art,
# and well below 255 because anti-aliased interior edges are.
ALPHA_ON = 60

# Sheet geometry. 4096 is the conservative floor across the hardware Godot ships
# to — desktop GL and Vulkan allow 16384, mobile and web commonly cap at 4096,
# and a texture over the limit does not warn, it fails to upload and the sprite
# renders as nothing. A 30-frame sheet at 160px wide is 4800px, so this is not a
# hypothetical: it is the second character sheet anyone builds.
MAX_SHEET_PX = 4096


def _open(path: Any):
    from PIL import Image

    return Image.open(path).convert("RGBA")


def _on_mask(img):
    """Binary L-mode mask of "the character is here". LUT, so it runs in C."""
    return img.getchannel("A").point(lambda a: 255 if a >= ALPHA_ON else 0)


# ---------------------------------------------------------------------------
# 1. Registration — the alpha-weighted centroid
# ---------------------------------------------------------------------------

# A column holding less than this fraction of the tallest column's ink is a LIMB,
# not the body. See :func:`anchor_x`. 0.25 is loose enough that a coat, a cape or
# a wide stance still counts as body and tight enough that an arm, a sword, a
# tail or a thrown leg does not.
CORE_COLUMN_FRAC = 0.25


def anchor_x(img, *, robust: bool = True) -> float:
    """The character's horizontal anchor, in pixels from the left edge.

    ``robust=True`` (the default, and the one every caller should use) returns
    the weighted median of the CORE columns. ``robust=False`` returns the
    textbook alpha-weighted centroid, ``Σ(x·α)/Σα``, and is kept only so the
    difference can be measured rather than asserted.

    MEASURED, on a two-frame synthetic built to isolate exactly this: an
    identical torso in both frames, and one arm of constant mass folded across
    the body in the first frame and thrown out to the side in the second. The
    torso does not move, so every pixel the torso appears to move is registration
    error:

        bounding-box centre       torso moves 44.5px   the fighter steps sideways
        alpha-weighted centroid   torso moves  6.0px   better, still visible
        weighted median           torso moves  3.5px   close
        core-column median        torso moves  1.0px   integer paste rounding

    (tests/test_spritekit.py::TestRegistration keeps those four numbers honest —
    it rebuilds that synthetic and fails if the ordering ever inverts.)

    Three ideas stacked, each fixing what the one before it leaves:

    1. The BOX CENTRE is not a body. An outstretched limb widens the bounding box
       on one side, so centring the box slides the body the other way by half the
       limb's reach. This is the failure everything else here is measured against.

    2. The CENTROID is a mean, and a mean is dragged by a tail. An outstretched
       limb IS a tail in the horizontal mass distribution — a small fraction of
       the ink placed a long way from the body — which is the exact input shape a
       mean handles worst. It removes most of the error and leaves a visible
       remainder.

    3. The MEDIAN ignores the tail's DISTANCE but still counts its MASS, so a
       heavy arm drags it a little. Dropping columns that hold less than
       :data:`CORE_COLUMN_FRAC` of the tallest column's ink removes the arm from
       the vote entirely, and what is left is the torso — which is what an
       animator means by the centre line, and is why they draw one. The fallback
       when that discards nearly everything (a genuinely thin subject: a sword, a
       snake, a character lying down) is the plain median, because a subject with
       no torso has no torso to find.

    Computed from a column-sum reduction rather than a pixel loop: a BOX resize to
    a single row IS the per-column mean, in C, and it is proportional to the
    column sum so neither statistic is disturbed by the constant factor. The
    median interpolates inside its column so the anchor moves smoothly rather
    than snapping a whole pixel at a time — a snapping anchor is jitter of its
    own, just a smaller kind.

    Returns the geometric centre for an empty image: a frame with no ink has no
    anchor, and refusing here would fail a frame the caller has already kept.
    """
    from PIL import Image

    mask = _on_mask(img)
    width = mask.width
    if width == 0:
        return 0.0
    columns = list(mask.resize((width, 1), Image.BOX).getdata())
    total = float(sum(columns))
    if total <= 0:
        return width / 2.0
    if not robust:
        return sum(x * v for x, v in enumerate(columns)) / total

    floor = max(columns) * CORE_COLUMN_FRAC
    core = [v if v >= floor else 0 for v in columns]
    # Nothing tall enough to be a body — take the whole silhouette's median
    # rather than the median of an empty set.
    if sum(core) < total * 0.2:
        core = columns
    return _weighted_median(core, width)


def _weighted_median(columns: list[int], width: int) -> float:
    """Position with half the weight on either side, interpolated in-column."""
    total = float(sum(columns))
    if total <= 0:
        return width / 2.0
    half = total / 2.0
    seen = 0.0
    for x, value in enumerate(columns):
        if value and seen + value >= half:
            return x + (half - seen) / value
        seen += value
    return width / 2.0


def mass(img) -> int:
    """Count of ON pixels — the character's visual "amount of ink".

    Pose-invariant in a way height and width are not: the same character carries
    about the same ink standing, leaning or crouching, which is what makes it the
    right anchor for normalising draw-size drift between frames.

    Alpha HISTOGRAM, not a pixel walk. The bins are the whole computation.
    """
    hist = img.getchannel("A").histogram()
    return sum(hist[ALPHA_ON:])


def place_offset(frame_w: int, frame_h: int, sprite_w: int, sprite_h: int,
                 cx: float, *, ground: int = 0, lift: int = 0) -> tuple[int, int]:
    """Where to paste a trimmed sprite in its cell. (x, y), top-left.

    X pins the sprite's mass ANCHOR to the cell's centre line, then clamps so
    the drawing cannot hang off either edge — a pose whose mass sits far from its
    box (a long lunge) would otherwise be pushed out of frame by the very
    correction that is supposed to steady it. The clamp is the reason a report of
    the applied offset is worth keeping: a frame that clamped is a frame whose
    centroid could NOT be honoured, and that is a fact about the drawing.

    Y is the floor, not the centroid. ``ground`` is the margin reserved below the
    feet and ``lift`` is airborne rise; neither is negotiable by mass.
    """
    x = int(round(frame_w / 2.0 - cx))
    if sprite_w <= frame_w:
        x = max(0, min(frame_w - sprite_w, x))
    else:                      # wider than its cell: centre it and accept the crop
        x = (frame_w - sprite_w) // 2
    return x, frame_h - ground - sprite_h - lift


# ---------------------------------------------------------------------------
# 2. Palette — locking, not checking
# ---------------------------------------------------------------------------

def master_palette(path: Any, colors: int = 64) -> list[tuple[int, int, int]]:
    """The character's own colours, most-used first. [] if unreadable.

    Alpha-gated, so a keyed reference reports the FIGURE's palette and not the
    void around it — sampling the void is how a "master palette" ends up being
    mostly the key colour, which would then be re-introduced into every frame it
    was just cut out of.
    """
    from PIL import Image

    try:
        img = _open(path)
        img.thumbnail((256, 256))
        pixels = [(r, g, b) for r, g, b, a in img.getdata() if a >= ALPHA_ON]
        if not pixels:
            return []
        strip = Image.new("RGB", (len(pixels), 1))
        strip.putdata(pixels)
        quant = strip.quantize(colors=max(2, min(256, int(colors))),
                               method=Image.Quantize.MEDIANCUT)
        counts: dict[int, int] = {}
        for index in quant.getdata():
            counts[index] = counts.get(index, 0) + 1
        table = quant.getpalette() or []
        out: list[tuple[int, int, int]] = []
        for index, _ in sorted(counts.items(), key=lambda kv: -kv[1]):
            rgb = tuple(table[index * 3:index * 3 + 3])
            if len(rgb) == 3:
                out.append(rgb)
        return out
    except Exception:
        return []


def looks_limited_palette(path: Any, *, colors: int = 32,
                          coverage: float = 0.9) -> bool:
    """Does this art live on a small palette already? Decides whether locking is safe.

    Flat, cel and pixel art put ~all of their pixels on a few dozen colours;
    painterly rendering spreads across hundreds of near-identical shades. Locking
    the first to its own palette changes nothing visible and kills drift outright.
    Locking the second POSTERISES it, which is a visible downgrade nobody asked
    for. So the answer decides a default, and the caller can always overrule it.

    Measured as: do the top `colors` quantised entries account for `coverage` of
    the ink, at a quantisation fine enough (4 bits/channel) that flat art collapses
    onto single entries and gradients do not.
    """
    try:
        img = _open(path)
        img.thumbnail((160, 160))
        counts: dict[int, int] = {}
        total = 0
        for r, g, b, a in img.getdata():
            if a < ALPHA_ON:
                continue
            bucket = (r >> 4) << 8 | (g >> 4) << 4 | (b >> 4)
            counts[bucket] = counts.get(bucket, 0) + 1
            total += 1
        if not total:
            return False
        top = sorted(counts.values(), reverse=True)[:max(1, int(colors))]
        return sum(top) / total >= coverage
    except Exception:
        return False


def lock_palette(path: Any, palette: Sequence[Sequence[int]], *,
                 out_path: Optional[Any] = None) -> dict:
    """Snap every opaque pixel to the nearest colour in `palette`. In place by default.

    THIS IS THE POINT: the existing palette gate scores a frame's histogram
    against the batch and re-rolls the outliers, which costs one image call per
    failure and only ever reduces the probability of drift. A frame quantised to
    the reference's palette CANNOT drift, because a colour that is not in the
    character is not representable in the file. Detection becomes prevention, and
    the re-rolls it would have bought are not spent.

    WHEN IT IS WRONG, stated plainly so nobody switches it on globally: this is a
    posteriser. On flat, cel-shaded, limited-palette or pixel art it is invisible
    and free. On smoothly rendered painterly art with real gradients it will band
    the shading, and no palette size fixes that in general — it only moves the
    banding. :func:`looks_limited_palette` is the cheap test for which kind of art
    you have; the sprite tool uses it to pick a default and says which way it went.

    Alpha is carried through untouched and the RGB under alpha 0 is re-zeroed —
    quantisation has no opinion about invisible pixels, and leaving colour under
    transparency is exactly the "dirty alpha" the chroma audit fails on.

    Returns {ok, path, colors, changed, note} and never raises: a frame that could
    not be quantised is still a frame.
    """
    from PIL import Image

    src = Path(path)
    dst = Path(out_path or path)
    entries = [tuple(int(c) for c in rgb[:3]) for rgb in palette if len(rgb) >= 3]
    if not entries:
        return {"ok": False, "path": str(src), "colors": 0, "changed": 0.0,
                "note": "no palette to lock to — the reference had no sampled "
                        "colours, so the frame was left as it is"}
    try:
        img = _open(src)
        alpha = img.getchannel("A")
        # A P-mode image whose palette IS the target. Pillow's quantize() against
        # a supplied palette does the nearest-colour search in C; the equivalent
        # Python loop is width*height*len(palette) comparisons per frame.
        ramp = Image.new("P", (1, 1))
        used = entries[:256]
        flat: list[int] = []
        for rgb in used:
            flat.extend(rgb)
        # PAD BY REPEATING THE LAST REAL COLOUR, NEVER WITH BLACK. A P-mode
        # palette always holds 256 entries and Pillow searches ALL of them, so
        # zero-filling the tail puts pure black in the palette — and black wins
        # the nearest-colour vote for anything mid-tone. Measured on a red
        # reference and a drifted green frame: green sits 206 from black and 226
        # from the red it should have snapped to, so "lock this frame to the
        # character's colours" turned it black. The whole feature shipped
        # inverted, and every gate downstream would have called the result a
        # clean, on-palette sprite.
        flat.extend(list(used[-1]) * (256 - len(used)))
        ramp.putpalette(flat)
        before = img.convert("RGB")
        locked = before.quantize(palette=ramp,
                                 dither=Image.Dither.NONE).convert("RGB")
        changed = _fraction_differing(before, locked, alpha)
        out = locked.convert("RGBA")
        out.putalpha(alpha)
        # RGB:=0 where alpha==0. The quantiser has just written a palette colour
        # into every transparent pixel it touched.
        out.paste((0, 0, 0, 0), (0, 0),
                  alpha.point(lambda a: 255 if a <= 8 else 0))
        out.save(dst)
    except Exception as exc:                                    # noqa: BLE001
        return {"ok": False, "path": str(src), "colors": len(entries),
                "changed": 0.0,
                "note": f"could not lock the palette ({type(exc).__name__}: {exc})"
                        " — the frame was left as it is"}
    return {"ok": True, "path": str(dst), "colors": len(entries),
            "changed": round(changed, 4),
            "note": f"every opaque pixel snapped to one of {len(entries)} "
                    "reference colours — palette drift is now unrepresentable"}


def _fraction_differing(before, after, alpha) -> float:
    """How much of the INK the quantiser actually moved. 0 means it was a no-op.

    Reported because it is the honest answer to "did locking do anything to my
    art": a flat sprite reads near 0 and a painterly one reads high, which is the
    same signal :func:`looks_limited_palette` gives in advance, measured after the
    fact on this specific frame.
    """
    from PIL import ImageChops

    diff = ImageChops.difference(before, after).convert("L")
    moved = diff.point(lambda v: 255 if v > 8 else 0)
    ink = alpha.point(lambda a: 255 if a >= ALPHA_ON else 0)
    both = ImageChops.multiply(moved, ink).histogram()[255]
    total = ink.histogram()[255]
    return (both / total) if total else 0.0


# ---------------------------------------------------------------------------
# 3. Is it ONE figure? — connected components
# ---------------------------------------------------------------------------

def parts(path: Any, *, part_frac: float = 0.02,
          thumb: int = 128) -> dict:
    """Split the silhouette into connected blobs. {parts, speckles, largest_frac}.

    The question every existing audit skips. A key that bit through a wrist leaves
    a hand floating next to an armless character; a key that caught noise leaves
    confetti. Both are perfectly clean cuts by every threshold in ``chroma.audit``
    — the border keyed, the alpha is crisp, nothing is hollow — and both are
    obviously broken to anyone looking at the frame.

    `parts` counts blobs holding at least `part_frac` of the ink; `speckles`
    counts the rest. They mean different things and want different fixes: parts
    above 1 is usually the key biting into the art (re-run with a different key
    colour, exactly as a hollow interior would say), while a speckle count in the
    dozens is a backdrop that was never flat.

    Measured on a thumbnail — this is a topology question, and topology at 128px
    is the topology of the frame. Interpreting it is the caller's business: a
    character holding a detached weapon is legitimately two parts, which is why
    this reports rather than judges.
    """
    try:
        img = _open(path)
        img.thumbnail((thumb, thumb))
        mask = _on_mask(img)
        w, h = mask.size
        px = mask.load()
        seen = bytearray(w * h)
        sizes: list[int] = []
        for y0 in range(h):
            for x0 in range(w):
                i0 = y0 * w + x0
                if seen[i0] or not px[x0, y0]:
                    continue
                seen[i0] = 1
                stack = [(x0, y0)]
                size = 0
                while stack:
                    x, y = stack.pop()
                    size += 1
                    for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                        if 0 <= nx < w and 0 <= ny < h:
                            j = ny * w + nx
                            if not seen[j] and px[nx, ny]:
                                seen[j] = 1
                                stack.append((nx, ny))
                sizes.append(size)
        total = sum(sizes)
        if not total:
            return {"parts": 0, "speckles": 0, "largest_frac": 0.0}
        floor = total * part_frac
        big = [s for s in sizes if s >= floor]
        return {"parts": len(big), "speckles": len(sizes) - len(big),
                "largest_frac": round(max(sizes) / total, 3)}
    except Exception:
        return {"parts": 1, "speckles": 0, "largest_frac": 1.0}


# ---------------------------------------------------------------------------
# 4. Motion — what silhouette overlap says about a sequence
# ---------------------------------------------------------------------------

def silhouette_iou(a: Any, b: Any) -> float:
    """Intersection-over-union of two frames' ON masks. 1.0 = same shape.

    The one number that carries all three sequence faults. Frames are already
    registered into identical cells by the time this runs, so overlap is a
    statement about the POSE and nothing else.
    """
    from PIL import ImageChops

    ma, mb = _on_mask(_open(a)), _on_mask(_open(b))
    if ma.size != mb.size:
        mb = mb.resize(ma.size)
    inter = ImageChops.multiply(ma, mb).histogram()[255]
    union = ImageChops.lighter(ma, mb).histogram()[255]
    return (inter / union) if union else 0.0


# A pair this alike is the same drawing twice.
#
# Derived, not picked. For a silhouette of area A whose shape changes by area D,
# IoU is about 1 - 2D/A, so this threshold says "less than half a percent of the
# figure changed". The number that matters on the other side is a BREATHING IDLE,
# which is the most subtle animation anyone legitimately ships: a 3px chest rise
# on a 240px figure moves 1-2% of the silhouette and lands near 0.97. Setting the
# threshold there — the first value tried — flagged a healthy idle and a healthy
# four-frame walk as duplicates, which is precisely the audit-that-fires-on-
# everything the art brief's eighth rule says gets switched off. A true duplicate
# is 1.0 and a model that ignored the pose instruction lands above 0.995, so
# there is a wide gap to sit in.
DUPLICATE_IOU = 0.99
# A pair this unalike did not come from one motion: the character was redrawn
# rather than moved, and in play it reads as a snap.
#
# Loose for the same reason. A run's flight frame against its contact frame is a
# genuinely large silhouette change that still overlaps on the torso, and a fast
# attack's wind-up against its full extension is larger still. Both are correct
# animation. Only a frame sharing almost nothing with its neighbour is evidence
# of anything.
POP_IOU = 0.25
# How much worse the wrap-around pair may be than the animation's own average
# before the cycle is called open. Relative, because a fast run legitimately has
# low overlap everywhere and an idle legitimately has high overlap everywhere —
# an absolute floor would flag every run and pass every idle.
LOOP_SLACK = 0.6


def motion_report(frames: Sequence[tuple[str, str]], *,
                  looping: bool = True, height: Optional[dict] = None) -> dict:
    """Sequence quality for ONE animation. Advisory, with named findings.

    frames: [(frame_label, path)] in play order. `looping` says whether the
    wrap-around pair is real — a death does not flow back into its first frame and
    must not be marked for failing to.

    Findings, each of which is a thing a player sees:
      * ``duplicate``  two adjacent frames are the same drawing. The animation
                       reads as a still on those frames, and one generation was
                       bought for nothing.
      * ``pop``        two adjacent frames share almost no silhouette. Something
                       was redrawn rather than moved.
      * ``open_loop``  the last frame does not flow into the first. This one is
                       cheap to miss and impossible to unsee: it hitches once per
                       repetition, forever.
      * ``static``     nothing moves anywhere in the set.
    """
    labels = [name for name, _ in frames]
    paths = [path for _, path in frames]
    if len(paths) < 2:
        return {"ok": True, "frames": len(paths), "adjacent_iou": [],
                "loop_iou": None, "findings": [], "flagged": False}

    adjacent = [round(silhouette_iou(paths[i], paths[i + 1]), 3)
                for i in range(len(paths) - 1)]
    loop_iou = (round(silhouette_iou(paths[-1], paths[0]), 3)
                if looping and len(paths) > 2 else None)

    findings: list[dict] = []
    for i, value in enumerate(adjacent):
        pair = f"{labels[i]}->{labels[i + 1]}"
        if value >= DUPLICATE_IOU:
            findings.append({"kind": "duplicate", "pair": pair, "iou": value,
                             "note": "these two frames are the same drawing — the "
                                     "animation holds still here and one "
                                     "generation was wasted. Drop one frame, or "
                                     "make the pose description differ by more "
                                     "than an adjective."})
        elif value <= POP_IOU:
            findings.append({"kind": "pop", "pair": pair, "iou": value,
                             "note": "these two frames share almost no "
                                     "silhouette — the character was redrawn, not "
                                     "moved. Re-roll the second one against the "
                                     "first."})
    mean_adjacent = sum(adjacent) / len(adjacent)
    if loop_iou is not None and mean_adjacent > 0 and \
            loop_iou < mean_adjacent * LOOP_SLACK:
        findings.append({"kind": "open_loop", "pair": f"{labels[-1]}->{labels[0]}",
                         "iou": loop_iou,
                         "note": f"the cycle does not close: the wrap-around pair "
                                 f"overlaps {loop_iou} against {round(mean_adjacent, 3)} "
                                 "for the rest of the set, so the loop hitches once "
                                 "per repetition. Re-roll the LAST frame "
                                 "conditioned on the first, or set the animation "
                                 "to ping-pong, which cannot have this fault."})
    # Only worth saying when it adds something the per-pair findings do not. On a
    # two-frame set "duplicate" has already said the whole of it, and repeating
    # it as a second finding is the noise that gets a report ignored.
    if len(adjacent) > 1 and min(adjacent) >= DUPLICATE_IOU:
        findings.append({"kind": "static", "pair": "", "iou": round(mean_adjacent, 3),
                         "note": "no frame differs from its neighbour — this is "
                                 "not an animation."})

    report = {"ok": True, "frames": len(paths), "adjacent_iou": adjacent,
              "mean_iou": round(mean_adjacent, 3), "loop_iou": loop_iou,
              "findings": findings, "flagged": bool(findings)}
    if height:
        report["height"] = height
    return report


def sheet_report(ordered: Sequence[str], frame_files: dict[str, str], *,
                 no_loop: Iterable[str] = (), airborne: Iterable[str] = ()) -> dict:
    """:func:`motion_report` and :func:`parts` over a whole assembled sheet.

    Groups by animation (the ``anim/idx`` naming the sprite path already uses),
    skips the wrap-around check for one-shots, and rolls every finding up into one
    ``flagged`` list a caller can put in front of a human without reading the
    detail.
    """
    no_loop = set(no_loop)
    by_anim: dict[str, list[tuple[int, str]]] = {}
    for pose in ordered:
        anim, _, idx = pose.partition("/")
        by_anim.setdefault(anim, []).append((int(idx) if idx.isdigit() else 0, pose))

    anims: dict[str, dict] = {}
    flagged: list[str] = []
    for anim, items in by_anim.items():
        items.sort()
        sequence = [(pose, frame_files[pose]) for _, pose in items
                    if pose in frame_files]
        if not sequence:
            continue
        report = motion_report(sequence, looping=anim not in no_loop)
        broken = [p for p, path in sequence if _split_figure(path)]
        if broken:
            report["findings"].append(
                {"kind": "detached", "pair": ", ".join(broken), "iou": None,
                 "note": "the silhouette is in more than one piece — either the "
                         "key bit through a limb (re-run with a different key "
                         "colour) or the drawing genuinely came apart. A frame "
                         "like this is not fixed by a better prompt."})
            report["flagged"] = True
        anims[anim] = report
        if report["flagged"]:
            flagged.append(anim)
    return {"ok": True, "animations": anims, "flagged": flagged}


def _split_figure(path: str) -> bool:
    """True when a frame's ink is in more than one meaningful piece."""
    return parts(path)["parts"] > 1


# ---------------------------------------------------------------------------
# 5. Sheet geometry — layout that a GPU will actually accept
# ---------------------------------------------------------------------------

def layout(total: int, cell_w: int, cell_h: int, *, columns: int = 0,
           pad: int = 0, max_px: int = MAX_SHEET_PX) -> dict:
    """Where each frame sits on the sheet. {columns, rows, pad, width, height}.

    A single horizontal strip for as long as one fits, because that is what every
    existing sheet and every existing region assertion is, and a layout change
    nobody asked for is a re-import of every character in the project. When the
    strip would exceed `max_px` it wraps into a grid AND takes a one-pixel gutter,
    which is the point at which the gutter starts to matter: a gridded sheet has
    vertical neighbours, and a sprite drawn at a non-integer scale samples across
    its region edge into whatever is next to it.

    The gutter is transparent rather than an extrusion of the edge pixel. Sprite
    cells are alpha-trimmed with a margin, so what bleeds in from a transparent
    gutter is nothing — which is exactly the intended result, and is not true of
    extrusion, which would bleed the sprite's own edge colour outward into a halo.
    """
    total = max(1, int(total))
    pad = max(0, int(pad))
    if columns and columns > 0:
        cols = min(total, int(columns))
    else:
        cols = total
        if pad == 0 and cell_w * total > max_px:
            pad = 1
        stride = cell_w + pad
        if stride * total + pad > max_px:
            cols = max(1, (max_px - pad) // stride)
    rows = (total + cols - 1) // cols
    return {"columns": cols, "rows": rows, "pad": pad,
            "width": pad + cols * (cell_w + pad) if pad else cols * cell_w,
            "height": pad + rows * (cell_h + pad) if pad else rows * cell_h}


def cell_origin(index: int, cell_w: int, cell_h: int, plan: dict) -> tuple[int, int]:
    """Top-left of frame `index` under a :func:`layout` plan."""
    pad = plan["pad"]
    col = index % plan["columns"]
    row = index // plan["columns"]
    if not pad:
        return col * cell_w, row * cell_h
    return pad + col * (cell_w + pad), pad + row * (cell_h + pad)
