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


# ---------------------------------------------------------------------------
# 6. ROWS — the multi-figure canvas, measured BEFORE anybody slices it
# ---------------------------------------------------------------------------
#
# Everything above this line measures frames that have already been registered
# into their own cells. That is the back half of the pipeline and it is the half
# that works. The failures that actually reach a human are upstream of it, in the
# thing an image model hands back when it is asked for four figures on one
# canvas: a "pose row".
#
# A row is not four frames. It is ONE drawing that happens to contain four
# figures, and the model draws them the way it draws anything — left to right,
# each one conditioned on the canvas so far. That autoregression is the whole
# problem, and it produces four faults that no measurement above can see because
# every one of them is a statement about where a figure sits ON THE SHARED
# CANVAS, which is information the slice throws away:
#
#   1. THE FEET ARE NOT ON A LINE. Figures 1 and 2 stand thirty pixels lower than
#      3 and 4. Sliced and bottom-pinned, every one of them lands on the cell
#      floor and the report reads clean — the drift was real, it was measurable
#      for exactly as long as the row existed, and pinning DESTROYED THE EVIDENCE
#      without fixing the drawing. The character's stride length, ground contact
#      and apparent weight all came from where the feet were, and they are gone.
#
#   2. THE FIGURE GROWS ACROSS THE ROW. Not jitter — a monotonic ramp, because
#      figure N was drawn from figure N-1 and every small error is inherited and
#      added to. :func:`_trend` is what separates the two, and the distinction is
#      the entire diagnosis: jitter says re-roll a frame, a ramp says stop
#      chaining and generate every pose against ONE reference.
#
#   3. THE FACING FLIPS. In a row that is supposed to be one direction of an
#      eight-way walk, one figure's head is yawed the other way. Silhouette
#      overlap cannot see it (a head is a head from either side) and neither can
#      any identity check (it is the same character, correctly drawn, pointing
#      wrong).
#
#   4. THE MODEL DREW THE PROMPT. Labels, arrows, callouts, a stray "FORWARD!"
#      beside the last figure. It is ink on the canvas, it survives the key, and
#      it slices into a frame as a floating word the player sees.
#
# All four read ~0 on a row that was drawn properly and spike with a name on one
# that was not. As everywhere else in this module: nothing here calls a model and
# nothing here is a matter of taste.


# Feet belong on a ground line. This is how far off it they may sit — as a
# fraction of the figure's OWN height, so the number means the same thing on a
# 200px row and a 2000px one. 3% of a 450px figure is 13px, which is about the
# point at which a walk stops reading as contact with the floor and starts
# reading as a hover. Deliberately tighter than every other threshold in this
# module because it is the one measurement with no legitimate reason to vary:
# a bob lifts the HEAD, not the feet.
FOOT_DRIFT_MAX = 0.03
# The head, by contrast, is SUPPOSED to move — the rise and fall twice per cycle
# is what a walk is, and a row whose head line is dead flat is the sliding-along-
# the-floor animation ``animspec`` exists to prevent. So this is loose, and it is
# only worth saying when the head has moved further than any bob accounts for.
HEAD_DRIFT_MAX = 0.18
# Draw-size spread, as a fraction of the median. The character is the same
# character in every figure and should carry the same amount of ink.
SIZE_DRIFT_MAX = 0.08
# |Spearman| between figure index and draw size at or above which the drift is
# not noise but a RAMP. 0.9 with four figures means "monotonic, or monotonic
# except for one adjacent swap".
TREND_RHO = 0.9
# Below this the head reads as symmetric and its skew sign is meaningless — a
# front-facing head genuinely has no side for the detail to be on, and flapping
# a facing finding at one is the audit-that-fires-on-everything the art brief's
# eighth rule says gets switched off.
FACING_SKEW_MIN = 0.05
# Fraction of the figure's height counted as "the head" for the facing measure.
HEAD_BAND_FRAC = 0.30
# Ink that is not attached to the figure and is at least this fraction of the
# canvas's ink is SOMETHING — a word, an arrow, a callout — rather than key
# speckle. Set by what it has to separate: a lettered "FORWARD!" beside a figure
# is a percent or two of the ink, and the confetti a bad key leaves is tenths of
# one percent per fleck.
STRAY_FRAC = 0.003


def _ink_mask(img):
    """"The subject is here", on a keyed row OR a raw un-keyed generation.

    A row is worth measuring at its MOST useful moment, which is straight out of
    the model and before anything has been spent keying, slicing or assembling
    it. At that moment it has no alpha, so the alpha threshold every other
    measurement in this module uses would report the whole canvas as subject and
    every finding would read 0.

    So: use alpha when there is alpha to use, and otherwise take the backdrop
    from the CORNERS and call everything far enough from it subject. That is the
    same assumption the generation contract already makes — a keyable row is
    drawn on one flat backdrop the character does not wear — and it is checkable
    rather than assumed: four corners that disagree with each other are not a
    flat backdrop, and the caller is told so instead of being handed a mask built
    on a guess.

    Returns (mask, note). `note` is None when the mask is trustworthy.
    """
    from PIL import Image, ImageChops

    alpha = img.getchannel("A")
    lo, hi = alpha.getextrema()
    if lo < 250:                    # there IS transparency — believe it
        return _on_mask(img), None

    rgb = img.convert("RGB")
    w, h = rgb.size
    corners = [rgb.getpixel(p) for p in
               ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    back = tuple(sum(c[i] for c in corners) // 4 for i in range(3))
    spread = max(max(abs(c[i] - back[i]) for i in range(3)) for c in corners)

    flat = Image.new("RGB", rgb.size, back)
    # Per-channel |difference|, then the max across channels: a subject that
    # matches the backdrop in two channels and not the third is still subject.
    diff = ImageChops.difference(rgb, flat).split()
    far = diff[0]
    for band in diff[1:]:
        far = ImageChops.lighter(far, band)
    mask = far.point(lambda v: 255 if v > 40 else 0)

    note = None
    if spread > 32:
        note = (f"this row has no alpha and its four corners disagree by "
                f"{spread} — the backdrop is not flat, so every measurement "
                "below was taken against a guessed background colour and should "
                "be read as indicative. Key the row first for numbers worth "
                "quoting.")
    return mask, note


def grid_cells(path: Any, columns: int, rows: int = 1) -> list[list]:
    """Slice a sheet into `rows` bands of `columns` cells. ``[row][column]``.

    Equal columns and equal bands, deliberately, and not a gap-finding split: an
    equal-cell slice is what every downstream assembler does, so measuring the
    cells the assembler will actually take is the only measurement that predicts
    what ships. A sheet whose figures do not fall on an even grid is itself a
    finding — it shows up as one cell holding two half-figures — and hiding it
    behind a cleverer splitter would report a clean sheet and then assemble a
    broken one.
    """
    img = _open(path)
    columns, rows = max(1, int(columns)), max(1, int(rows))
    dx, dy = img.width / float(columns), img.height / float(rows)
    return [[img.crop((int(round(c * dx)), int(round(r * dy)),
                       int(round((c + 1) * dx)), int(round((r + 1) * dy))))
             for c in range(columns)]
            for r in range(rows)]


def head_skew(img, mask=None, *, band: float = HEAD_BAND_FRAC) -> float:
    """Which side of the head the DARK detail sits on. -1 left, +1 right, 0 flat.

    The facing proxy, and the only cheap one that works. A yaw flip is invisible
    to every measurement this pipeline already has: the silhouette of a head is
    the silhouette of a head from either side, so overlap reads fine; the
    character is correctly drawn and on-model, so the identity gate passes; the
    palette is untouched. The one thing that DOES move is where the face is — a
    visor, a grille, a vent, an eye, a muzzle, a ponytail — and on essentially
    every character design that feature is DARKER than the head it sits in,
    because it is a recess or an opening and the rest of the head is a lit
    surface.

    So: take the top ``band`` of the figure, and ask where its dark pixels sit
    relative to its light ones, weighted by how dark they are and normalised
    across the head's own width. A three-quarter view facing right puts the
    grille right of centre and returns positive; the mirror of it returns
    negative; a flat front or back view returns near zero and is correctly
    refused a vote by :data:`FACING_SKEW_MIN`.

    WHAT IT IS NOT. It is not a yaw angle and must not be read as one — the
    magnitude depends on the design, so 0.4 and 0.6 on two figures says nothing.
    The SIGN is the whole signal, and comparing signs within one row (where the
    design is constant by construction) is the only use it has. That is enough,
    because the fault it exists to catch is a flip.
    """
    from PIL import Image

    if mask is None:
        mask, _ = _ink_mask(img)
    box = mask.getbbox()
    if not box:
        return 0.0
    left, top, right, bottom = box
    height = bottom - top
    depth = max(1, int(round(height * band)))
    head = img.crop((left, top, right, min(bottom, top + depth)))
    # SPAN-FILLED, and this is the whole measurement rather than a tidy-up. The
    # facing cue is a dark recess — a visor, a grille, an eye socket — and the
    # backdrop these rows are drawn on is very often near-black too, so the mask
    # that separates subject from backdrop cuts the recess out along with the
    # backdrop. Sampling the raw mask therefore samples everything about the head
    # EXCEPT the one feature that says which way it faces, and the measure reads
    # ~0 on a head whose facing is unmistakable to a human. Filling each row
    # between its first and last subject pixel puts the interior back, which is
    # what "inside the figure" means and is not a question the mask can answer.
    head_mask = _span_fill(mask.crop((left, top, right, min(bottom, top + depth))))
    inner = head_mask.getbbox()
    if not inner:
        return 0.0
    head = head.crop(inner)
    head_mask = head_mask.crop(inner)
    # A spatial statistic survives a thumbnail, and the band on a 2000px row is
    # otherwise a few hundred thousand pixels of interpreter time.
    if head.width > 128:
        size = (128, max(1, int(round(head.height * 128 / head.width))))
        head = head.resize(size, Image.BILINEAR)
        head_mask = head_mask.resize(size, Image.NEAREST)

    lum = head.convert("L").load()
    on = head_mask.load()
    w, h = head.size
    values = [lum[x, y] for y in range(h) for x in range(w) if on[x, y]]
    if len(values) < 16:
        return 0.0
    mean = sum(values) / len(values)

    num = den = 0.0
    half = (w - 1) / 2.0 or 1.0
    for y in range(h):
        for x in range(w):
            if not on[x, y]:
                continue
            weight = mean - lum[x, y]        # positive where the pixel is DARK
            num += ((x - half) / half) * weight
            den += abs(weight)
    return round(num / den, 4) if den else 0.0


def _span_fill(mask):
    """Every row filled between its first and last ON pixel. "Inside the figure".

    Not a morphological close — a close would also bridge the gap between two
    legs, and this deliberately does too. That is correct here: the question is
    which pixels are INTERIOR to the silhouette, and a dark shape enclosed by the
    figure's own outline is interior whether it is a visor or the shadow between
    the legs. Only used for reading colour out of a region, never for geometry.
    """
    from PIL import Image

    out = Image.new("L", mask.size, 0)
    px = mask.load()
    dst = out.load()
    w, h = mask.size
    for y in range(h):
        first = last = -1
        for x in range(w):
            if px[x, y]:
                if first < 0:
                    first = x
                last = x
        for x in range(first, last + 1):
            dst[x, y] = 255
    return out


def cell_stats(img) -> dict:
    """Everything measurable about one figure, in the ROW's coordinates.

    ``top``/``bottom`` are absolute y in the source image and that is the point:
    the cells are equal-width columns of one canvas, so they share a y-axis, and
    "are the feet on a line" is a question that can only be asked before the
    slice pins every figure to its own cell floor.
    """
    mask, note = _ink_mask(img)
    box = mask.getbbox()
    if not box:
        return {"empty": True, "top": None, "bottom": None, "height": 0,
                "width": 0, "ink": 0, "anchor": img.width / 2.0, "skew": 0.0,
                "note": note}
    left, top, right, bottom = box
    ink = mask.histogram()[255]
    return {"empty": False, "top": top, "bottom": bottom,
            "height": bottom - top, "width": right - left, "ink": ink,
            "anchor": round(_anchor_from_mask(mask), 2),
            "skew": head_skew(img, mask), "note": note}


def _anchor_from_mask(mask) -> float:
    """:func:`anchor_x`, given a mask that is already built."""
    from PIL import Image

    width = mask.width
    if width == 0:
        return 0.0
    columns = list(mask.resize((width, 1), Image.BOX).getdata())
    total = float(sum(columns))
    if total <= 0:
        return width / 2.0
    floor = max(columns) * CORE_COLUMN_FRAC
    core = [v if v >= floor else 0 for v in columns]
    if sum(core) < total * 0.2:
        core = columns
    return _weighted_median(core, width)


def _trend(values: Sequence[float]) -> float:
    """Spearman rank correlation between position and value. -1..1.

    THE MEASUREMENT THAT NAMES THE CAUSE. Spread alone cannot tell a row whose
    figures wobble around the right size from one whose figures grow steadily
    from the first to the last, and those are different bugs with different
    fixes: the first is one bad draw and wants a re-roll, the second is the model
    conditioning each figure on the one before it, which no re-roll survives
    because the next attempt does the same thing. Rank correlation rather than
    Pearson so a ramp that accelerates still reads as a ramp.
    """
    n = len(values)
    if n < 3:
        return 0.0
    order = sorted(range(n), key=lambda i: values[i])
    rank = [0.0] * n
    i = 0
    while i < n:                       # average ranks within a tie group
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0
        for k in range(i, j + 1):
            rank[order[k]] = shared
        i = j + 1
    mean_x = (n - 1) / 2.0
    mean_y = sum(rank) / n
    num = sum((x - mean_x) * (rank[x] - mean_y) for x in range(n))
    dx = sum((x - mean_x) ** 2 for x in range(n)) ** 0.5
    dy = sum((r - mean_y) ** 2 for r in rank) ** 0.5
    return round(num / (dx * dy), 3) if dx and dy else 0.0


def stray_ink(img, mask=None, *, frac: float = STRAY_FRAC) -> dict:
    """Ink that is NOT the figure. {blobs, largest_stray_frac}.

    :func:`parts` asks whether the figure came apart. This asks the opposite
    question — whether something ELSE is on the canvas — and it is a different
    question with a different fix. A model asked for "walk cycle, facing
    north-east, FORWARD" will cheerfully letter the word FORWARD next to the last
    figure, or draw the direction as an arrow, or caption the row. That ink is
    flat, opaque and well away from the character, so it keys cleanly, slices
    into a cell, and ships as a floating word in the middle of the animation. No
    threshold in this module fires on it: the figure is whole, the palette is
    fine, the silhouette overlaps its neighbours.

    Everything EXCEPT the largest blob, down to a `frac` noise floor. Two
    thresholds — "smaller than the figure" and "bigger than speckle" — would need
    a gap between them that does not exist: a lettered word can be several
    percent of the ink and a coarse key can leave flecks approaching one. Taking
    "not the biggest thing here" as the definition of not-the-character removes
    the guess, and the single remaining floor only has to clear noise.

    On a thumbnail, same as :func:`parts` and for the same reason.
    """
    from PIL import Image

    thumb = img.copy()
    thumb.thumbnail((160, 160), Image.NEAREST)
    if mask is None or mask.size != thumb.size:
        mask, _ = _ink_mask(thumb)
    sizes = _blob_sizes(mask)               # already largest-first
    total = sum(sizes)
    if not total or len(sizes) < 2:
        return {"blobs": 0, "largest_stray_frac": 0.0}
    floor = total * frac
    stray = [s for s in sizes[1:] if s >= floor]
    return {"blobs": len(stray),
            "largest_stray_frac": round(max(stray) / total, 4) if stray else 0.0}


def _blob_sizes(mask) -> list[int]:
    """Sizes of every 4-connected ON component in `mask`, largest first."""
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
    return sorted(sizes, reverse=True)


def _band_report(cells: list, names: list[str]) -> dict:
    """Every within-row finding for ONE band of cells. See :func:`row_report`."""
    stats = [cell_stats(c) for c in cells]
    for stat, cell in zip(stats, cells):
        if not stat["empty"]:
            stat.update(stray_ink(cell))

    findings: list[dict] = []
    notes = [s["note"] for s in stats if s.get("note")]

    empty = [n for n, s in zip(names, stats) if s["empty"]]
    if empty:
        findings.append({
            "kind": "empty_cell", "frames": empty, "value": None,
            "note": f"{len(empty)} of {len(cells)} columns hold no figure. Either "
                    "the model drew fewer poses than asked, or the figures are "
                    "not on an even grid and an equal-column slice cuts them in "
                    "half — look at the row before re-rolling, the two look "
                    "identical in this number and want opposite fixes."})

    live = [(n, s) for n, s in zip(names, stats) if not s["empty"]]
    if len(live) < 2:
        return {"ok": True, "frames": len(cells),
                "cells": [{"frame": n, **s} for n, s in zip(names, stats)],
                "findings": findings, "flagged": bool(findings), "notes": notes}

    heights = [s["height"] for _, s in live]
    scale = sorted(heights)[len(heights) // 2] or 1

    feet = [s["bottom"] for _, s in live]
    foot_spread = (max(feet) - min(feet)) / scale
    if foot_spread > FOOT_DRIFT_MAX:
        low, high = min(feet), max(feet)
        findings.append({
            "kind": "foot_drift", "frames": [n for n, s in live
                                             if s["bottom"] in (low, high)],
            "value": round(foot_spread, 3),
            "note": f"the feet span {high - low}px of ground, {round(foot_spread * 100)}% "
                    f"of the figure's own height, against {round(FOOT_DRIFT_MAX * 100)}% "
                    "allowed. The character hovers and lands once per cycle. Note "
                    "that SLICING THIS ROW HIDES IT rather than fixing it — every "
                    "cell gets bottom-pinned to its own floor and the report goes "
                    "quiet — so fix it here or accept that the stride is gone."})

    tops = [s["top"] for _, s in live]
    head_spread = (max(tops) - min(tops)) / scale
    if head_spread > HEAD_DRIFT_MAX:
        findings.append({
            "kind": "head_drift", "frames": names, "value": round(head_spread, 3),
            "note": f"the head line moves {max(tops) - min(tops)}px, "
                    f"{round(head_spread * 100)}% of the figure's height. Some of "
                    "that is the bob a walk is supposed to have; this much is the "
                    "whole figure sliding up and down the canvas."})

    roots = [s["ink"] ** 0.5 for _, s in live]
    median = sorted(roots)[len(roots) // 2] or 1.0
    size_spread = (max(roots) - min(roots)) / median
    rho = _trend(roots)
    if size_spread > SIZE_DRIFT_MAX:
        findings.append({
            "kind": "size_drift", "frames": names, "value": round(size_spread, 3),
            "note": f"the character is drawn {round(size_spread * 100)}% bigger in "
                    "one figure than another. Ink area, not bounding box, so a "
                    "crouch or a lean does not count — this is draw-size drift."})
        if abs(rho) >= TREND_RHO:
            direction = "grows" if rho > 0 else "shrinks"
            findings.append({
                "kind": "size_ramp", "frames": names, "value": rho,
                "note": f"and it {direction} MONOTONICALLY across the row "
                        f"(rank correlation {rho}). That is not one bad draw and "
                        "a re-roll will not fix it: the model drew each figure "
                        "from the canvas so far, so every error is inherited by "
                        "the next figure and added to. The fix is structural — "
                        "generate each pose as its OWN image conditioned on ONE "
                        "approved reference, which is what image_sprites does, "
                        "so no frame can inherit another frame's error."})

    voters = [(n, s["skew"]) for n, s in live if abs(s["skew"]) >= FACING_SKEW_MIN]
    if len(voters) >= 3:
        right = sum(1 for _, v in voters if v > 0)
        majority = 1 if right * 2 > len(voters) else -1
        odd = [n for n, v in voters if (1 if v > 0 else -1) != majority]
        if odd and len(odd) * 2 < len(voters):
            findings.append({
                "kind": "facing_flip", "frames": odd,
                "value": [v for n, v in voters if n in odd],
                "note": f"{', '.join(odd)} face the opposite way to the rest of "
                        "the row — the dark detail of the head (visor, grille, "
                        "eye) sits on the other side. In a single-direction row "
                        "this is a yaw flip, and it is invisible to every other "
                        "check here: the silhouette still overlaps, the character "
                        "is still on-model, the palette never moved. Re-roll the "
                        "named figures against a figure that faces correctly. If "
                        "this row is a TURNAROUND, the finding is wrong by "
                        "design — ignore it."})

    littered = [n for n, s in live
                if s.get("blobs", 0) and s.get("largest_stray_frac", 0) > 0.004]
    if littered:
        findings.append({
            "kind": "stray_ink", "frames": littered, "value": None,
            "note": "there is ink on this canvas that is not the character — a "
                    "caption, a direction arrow, a label. It is flat and opaque, "
                    "so it keys as cleanly as the figure does, slices into the "
                    "cell and ships as something floating in the animation. Strip "
                    "the words out of the prompt: a model asked for a direction "
                    "in prose will sometimes draw the prose."})

    return {"ok": True, "frames": len(cells),
            "cells": [{"frame": n, **s} for n, s in zip(names, stats)],
            "foot_drift": round(foot_spread, 3),
            "head_drift": round(head_spread, 3),
            "size_drift": round(size_spread, 3), "size_trend": rho,
            "findings": findings, "flagged": bool(findings), "notes": notes}


def _ink_palette_hist(cells: list) -> dict[int, float]:
    """Normalised 12-bit colour histogram over the INK of a whole band.

    Alpha-gated for the same reason :func:`master_palette` is: sampling the
    backdrop makes every band look identical, which is the one answer that
    cannot be informative. 4 bits per channel because the question is "is this
    the same character" and not "is this the same pixel" — shading gradients
    collapse onto a handful of buckets and a genuinely new colour does not.
    """
    from PIL import Image

    counts: dict[int, int] = {}
    total = 0
    for cell in cells:
        thumb = cell.copy()
        thumb.thumbnail((96, 96), Image.BILINEAR)
        mask, _ = _ink_mask(thumb)
        on = mask.load()
        rgb = thumb.convert("RGB").load()
        for y in range(thumb.height):
            for x in range(thumb.width):
                if not on[x, y]:
                    continue
                r, g, b = rgb[x, y]
                bucket = (r >> 4) << 8 | (g >> 4) << 4 | (b >> 4)
                counts[bucket] = counts.get(bucket, 0) + 1
                total += 1
    if not total:
        return {}
    return {k: v / total for k, v in counts.items()}


def _novelty(band: dict[int, float], others: Sequence[dict[int, float]],
             *, floor: float = 0.0008) -> float:
    """Fraction of a band's ink whose COLOUR appears nowhere else on the sheet.

    NOT histogram intersection, and the difference is why this measurement works.
    Intersection is dominated by bulk: a character who is 90% coat and 10%
    everything else scores ~0.95 against a row of himself with a colour added,
    because the coat still matches. The fault being hunted is small by nature —
    a tie, a trim, a pair of eyes that light up — so a measure the bulk can
    outvote cannot see it at any threshold that does not also fire on noise.

    Asking instead "how much of this row is painted in a colour the rest of the
    sheet never uses" puts the small thing in the numerator where it belongs. A
    row identical to its siblings scores 0 no matter how large the character is.
    """
    total = 0.0
    for bucket, share in band.items():
        if all(other.get(bucket, 0.0) < floor for other in others):
            total += share
    return round(total, 4)


# How much of a row may be painted in colours found nowhere else on the sheet.
# A percent is a detail nobody designed: a necktie, a light coming on, a trim
# that appears for two rows and goes away again. Below that is anti-aliasing and
# the handful of buckets a genuinely different VIEW introduces.
BAND_NOVELTY_MAX = 0.01


def row_report(path: Any, columns: int, rows: int = 1, *,
               labels: Optional[Sequence[str]] = None,
               row_labels: Optional[Sequence[str]] = None) -> dict:
    """Audit a pose row — or a whole multi-row character sheet. Named findings.

    The whole of section 6 in one call. Costs nothing, calls no model, and works
    on a raw generation as happily as on a keyed one — which matters, because the
    moment this is worth running is the moment the art comes back, while a
    re-roll is still one cheap call and before anybody has paid to key, slice,
    register and assemble something that was never going to work.

    ``rows`` is what makes it useful on the artifact people actually generate. A
    character sheet is not one row; it is six or eight animations stacked, and
    stacking is where a SECOND family of faults lives that no single-row check
    can reach — the walk row and the attack row drawn at different sizes, or one
    row carrying a colour the others do not (a tie that appears halfway down, a
    pair of eyes that light up for two rows and go dark again). Both are the same
    compounding drift as within a row, one level up.

    WITHIN each row:
      * ``foot_drift``   the figures are not standing on one line. The character
                         hovers and lands, once per cycle.
      * ``head_drift``   the figures translate vertically further than a bob.
      * ``size_drift``   the character is drawn at different sizes.
      * ``size_ramp``    ...and monotonically, which means the drift COMPOUNDS
                         and is not a re-roll away.
      * ``facing_flip``  a figure's head is yawed against the rest of the row.
      * ``stray_ink``    something on the canvas is not the character — a label,
                         an arrow, a caption.
      * ``empty_cell``   a column holds no figure.

    ACROSS rows:
      * ``sheet_size_drift`` / ``sheet_size_ramp`` — the rows do not agree on how
        big the character is, so animations pop when the game switches between
        them. A ramp down the sheet says each row was drawn from the one above.
      * ``band_palette`` — a row carries colours the rest of the sheet does not.

    WHAT THIS DELIBERATELY DOES NOT DO: judge whether it is the same CHARACTER.
    Identity is not arithmetic — a redesigned collar and a different pose are the
    same numbers — and ``consistency_check`` is the tool that asks a model. This
    measures the things a model is bad at and arithmetic is exact about.

    Advisory throughout. It reports; a human or the caller decides, because a
    sheet can be deliberately any of the above — a turnaround SHOULD flip its
    facing, a size chart SHOULD ramp.
    """
    grid = grid_cells(path, columns, rows)
    names = list(labels or [])
    names += [str(i) for i in range(len(names), columns)]
    band_names = list(row_labels or [])
    band_names += [f"row{i}" for i in range(len(band_names), len(grid))]

    bands = []
    for label, cells in zip(band_names, grid):
        report = _band_report(cells, list(names))
        report["row"] = label
        bands.append(report)

    findings: list[dict] = []
    flagged = [b["row"] for b in bands if b["flagged"]]

    if len(grid) > 1:
        sizes = []
        for band in bands:
            live = [c["ink"] for c in band["cells"] if not c["empty"]]
            sizes.append((sorted(live)[len(live) // 2] ** 0.5) if live else 0.0)
        real = [s for s in sizes if s > 0]
        if len(real) > 1:
            median = sorted(real)[len(real) // 2] or 1.0
            spread = (max(real) - min(real)) / median
            rho = _trend(sizes)
            if spread > SIZE_DRIFT_MAX:
                worst = band_names[sizes.index(max(sizes))]
                thinnest = band_names[sizes.index(min(real))]
                findings.append({
                    "kind": "sheet_size_drift", "frames": [worst, thinnest],
                    "value": round(spread, 3),
                    "note": f"the rows do not agree on how big the character is — "
                            f"{worst} is drawn {round(spread * 100)}% larger than "
                            f"{thinnest}. Every row assembles into its own "
                            "animation on one AnimatedSprite2D, so the character "
                            "changes size the moment the game switches between "
                            "them, which reads as a glitch rather than as art."})
                if abs(rho) >= TREND_RHO:
                    findings.append({
                        "kind": "sheet_size_ramp", "frames": band_names,
                        "value": rho,
                        "note": "and it moves monotonically DOWN the sheet "
                                f"(rank correlation {rho}) — the same compounding "
                                "the rows have internally, one level up: each row "
                                "was drawn from the canvas above it. No re-roll of "
                                "any single row fixes this. Generate each row as "
                                "its own image against one approved reference."})

        hists = [_ink_palette_hist(cells) for cells in grid]
        usable = [h for h in hists if h]
        if len(usable) > 2:
            odd = []
            for i, hist in enumerate(hists):
                if not hist:
                    continue
                score = _novelty(hist, [h for j, h in enumerate(hists)
                                        if h and j != i])
                if score > BAND_NOVELTY_MAX:
                    odd.append((band_names[i], score))
            if odd and len(odd) * 2 < len(usable):
                findings.append({
                    "kind": "band_palette",
                    "frames": [n for n, _ in odd],
                    "value": [s for _, s in odd],
                    "note": "these rows carry colours the rest of the sheet does "
                            "not — measured as the share of the row painted in "
                            "buckets no other row uses at all, so a row that is "
                            "merely a different VIEW of the same character does "
                            "not count. This is how a detail nobody designed gets "
                            "in and stays: a tie, a light that comes on, a trim "
                            "that appears for two rows and goes away again. It "
                            "survives every per-row check, because within its own "
                            "row it is perfectly consistent. Put the named rows "
                            "next to the reference before assembling any of them."})

    if findings:
        flagged.append("sheet")
    return {"ok": True, "columns": columns, "rows": len(grid),
            "bands": bands, "findings": findings, "flagged": flagged,
            "notes": sorted({n for b in bands for n in b["notes"]})}


def draw_guides(path: Any, columns: int, out_path: Any, rows: int = 1, *,
                report: Optional[dict] = None) -> dict:
    """Write a copy of the sheet with the alignment guides drawn on it.

    THE NUMBERS ARE NOT THE DELIVERABLE — the picture is. Every fault section 6
    measures was first spotted by a human holding a straight edge against a
    screenshot, and that is not a coincidence: "the feet are not on a line" is a
    sentence about a line, and an agent handed 0.071 has to take on faith what a
    human sees at a glance. This draws the line, so both of them are looking at
    the same evidence.

    Drawn, in order of how much they matter:
      * the GROUND line, at the median foot, and a per-figure tick showing how
        far that figure's feet sit from it;
      * the HEAD line, at the median top;
      * each figure's own foot and head, where they differ enough to see;
      * each figure's mass anchor, which is where the slice will centre it.

    Over a dark plate, because a row is transparent or nearly so by the time it
    is worth checking, and guides on a checkerboard are guides nobody can read.
    """
    from PIL import Image, ImageDraw

    report = report or row_report(path, columns, rows)
    src = _open(path)
    plate = Image.new("RGBA", src.size, (24, 24, 28, 255))
    plate.alpha_composite(src)
    canvas = plate.convert("RGB")
    draw = ImageDraw.Draw(canvas)

    bands = report["bands"]
    dx = src.width / float(max(1, int(columns)))
    dy = src.height / float(max(1, len(bands)))
    RED, CYAN, GREY = (255, 48, 48), (0, 220, 220), (110, 110, 120)
    drawn = 0

    for r, band in enumerate(bands):
        # Each band carries its OWN ground line. A sheet's rows are separate
        # animations that will be sliced into separate cells, so a single line
        # down the whole image would measure the rows against each other rather
        # than each row against itself, and every row below the first would read
        # as broken for the crime of being lower on the page.
        top_y = int(round(r * dy))
        if r:
            draw.line([(0, top_y), (src.width, top_y)], fill=GREY, width=2)
        live = [c for c in band["cells"] if not c["empty"]]
        if not live:
            continue
        drawn += len(live)
        feet = sorted(c["bottom"] for c in live)
        tops = sorted(c["top"] for c in live)
        ground = top_y + feet[len(feet) // 2]
        head = top_y + tops[len(tops) // 2]
        draw.line([(0, ground), (src.width, ground)], fill=RED, width=3)
        draw.line([(0, head), (src.width, head)], fill=RED, width=3)

        for i, cell in enumerate(band["cells"]):
            x0, x1 = int(round(i * dx)), int(round((i + 1) * dx))
            if i:
                draw.line([(x0, top_y), (x0, top_y + int(dy))],
                          fill=GREY, width=1)
            if cell["empty"]:
                draw.line([(x0, top_y), (x1, top_y + int(dy))], fill=RED, width=2)
                draw.line([(x0, top_y + int(dy)), (x1, top_y)], fill=RED, width=2)
                continue
            # The figure's own lines, only where they sit far enough off the
            # shared one to see. Drawing them at zero offset would stack a second
            # line on the first and make a clean row look busy.
            foot_y, head_yy = top_y + cell["bottom"], top_y + cell["top"]
            if abs(foot_y - ground) >= 2:
                draw.line([(x0 + 4, foot_y), (x1 - 4, foot_y)], fill=CYAN, width=1)
            if abs(head_yy - head) >= 2:
                draw.line([(x0 + 4, head_yy), (x1 - 4, head_yy)], fill=CYAN, width=1)
            anchor = int(round(x0 + cell["anchor"]))
            draw.line([(anchor, head_yy), (anchor, foot_y)], fill=CYAN, width=1)
            # Labelled at the TOP of the cell, on its own plate. At the ground
            # line it was legible right up until the ground line was near the
            # bottom edge, which is exactly the sheet worth labelling.
            off = foot_y - ground
            text = (f"{band['row']}/{cell['frame']} foot{off:+d} "
                    f"skew{cell['skew']:+.2f}")
            try:
                width = draw.textlength(text)
            except Exception:               # very old Pillow — estimate
                width = 6 * len(text)
            draw.rectangle([x0 + 2, top_y + 2, x0 + 10 + width, top_y + 14],
                           fill=(0, 0, 0))
            draw.text((x0 + 6, top_y + 4), text,
                      fill=RED if abs(off) > 2 else GREY)

    canvas.save(out_path)
    if not drawn:
        return {"ok": True, "path": str(out_path), "guides": 0,
                "note": "no figure found in any cell — nothing to draw a line "
                        "against. Is the grid the right way round?"}
    return {"ok": True, "path": str(out_path), "guides": drawn,
            "note": "red is each ROW's own ground and head line, taken from that "
                    "row's median figure; cyan is where THIS figure's feet, head "
                    "and mass anchor actually are. A row that is right has its "
                    "cyan feet hidden under the red line. Grey splits the cells "
                    "the slicer will take — a figure straddling a grey line is a "
                    "grid problem, not a drawing problem."}
