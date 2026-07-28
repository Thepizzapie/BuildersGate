"""Worn-gear placeholder sheets — measure the hand, then stamp a stand-in on it.

The equip/layer system (templates/2d gear_rig.gd) rides the body's clock: a gear
layer only has to be an ALIGNED SHEET — the character's own canvas and grid with
only the gear drawn — and it overlays 1:1 for free. That is cheap per animation
and brutally expensive per MISSING animation: the moment the body plays an
action the weapon has no sheet for, the layer blanks and the equipped weapon
VANISHES mid-combat. A real project shows the gap plainly: five weapons drawn
for four attack actions, against a body with fourteen.

Filling that gap with an image model is both wasteful and wrong. The gear has to
land on the HAND, in every frame, to the pixel — a diffusion model cannot hit
that, and paying it to miss is worse than not paying. So this module is
procedural end to end:

  1. MEASURE. The existing aligned sheets already encode the hand: each is a
     mostly-empty canvas with a weapon drawn at the grip. Five different weapons
     drawn for the same action agree on exactly one region — where the hand is.
     Intersect their masks per frame and the centroid of what survives is a
     ground-truth grip anchor. No guessing involved.
  2. INFER, and say so. Uncovered actions have no gear sheet to read, so the
     anchor comes from the body: a hand-colour palette LEARNED at the measured
     anchors, matched back into the body frame, largest non-head cluster on the
     hand's side, extremity of that cluster. Every anchor carries its `source`,
     and `validate_inference` re-runs the inference on the frames that DO have
     ground truth and reports the error in pixels — a guess presented as a
     measurement would be the one unforgivable output here.
  3. STAMP. A hazard-striped bar at the anchor, oriented away from the body.
     It must read as a placeholder at a glance; art that looks finished never
     gets redrawn.

Pure Pillow + stdlib on purpose (numpy is an optional extra here, and these
canvases are 384x160 — a Python loop is not the bottleneck). Every function is
I/O-free except the explicit save/scan helpers at the bottom.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from PIL import Image, ImageDraw, PngImagePlugin

# A pixel counts as drawn above this alpha. Sprite sheets carry feathered edges;
# 8 keeps the halo out of centroids without eating genuine soft pixels.
ALPHA_THRESHOLD = 8

# The PNG tEXt key that marks OUR output. Coverage has to tell "drawn by a human"
# from "stamped by this module", and a filename cannot carry that — the game's
# format string fixes the name, and the real project already ships hand-drawn art
# literally called placeholder_throw_one_hand.png. The marker travels in the file.
MARKER_KEY = "bgate"
MARKER_VALUE = "gear-placeholder"

# Grid search bounds. A sprite cell below 8px or a sheet past 16 cells on an axis
# is not a thing these pipelines produce, and allowing them lets a sparse sheet
# (one small band on a wide canvas) resolve to an absurdly fine grid.
MIN_CELL = 8
MAX_CELLS = 16

# Anchor provenance, weakest last. Callers report these verbatim — the whole
# point is that a reader can tell which anchors were read off real art.
MEASURED = "measured"                  # >=2 gear sheets agreed; grip intersection
MEASURED_SINGLE = "measured_single"    # one sheet only; centroid of the whole gear
INFERRED_HAND = "inferred_hand"        # learned hand palette, clustered on the body
INFERRED_SILHOUETTE = "inferred_silhouette"  # no palette match; body extremity
MEASURED_SOURCES = (MEASURED, MEASURED_SINGLE)

# One body animation can need MORE than one gear layer: a dual-wield swing drives
# a main-hand layer and an off-hand layer off the same body frames, and the game
# loads them under distinct layer-action names. Anything not listed maps to a
# single layer of its own name (main_hand_swing -> main_hand_swing).
BODY_TO_LAYER_ACTIONS: dict[str, tuple[str, ...]] = {
    "dual_wield_swing": ("dual_wield_main", "dual_wield_off"),
}

# Throw actions are served by the THROWABLE slot, not the held weapon: the game
# loads one shared placeholder_<action>.png rather than one per weapon, so these
# actions leave the weapon x action grid and get their own coverage rows.
THROWABLE_BODY_ACTIONS: tuple[str, ...] = ("throw_one_hand", "throw_two_hand")

# Which hand a layer hangs on. Drives the side bias: a main-hand layer must track
# the main hand even in a frame where both hands are visible.
LAYER_HAND: dict[str, str] = {
    "main_hand_swing": "main_hand",
    "dual_wield_main": "main_hand",
    "off_hand_swing": "off_hand",
    "dual_wield_off": "off_hand",
}


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Grid:
    """A sheet's cell lattice. cols x rows cells of cell_w x cell_h."""
    cell_w: int
    cell_h: int
    cols: int
    rows: int

    @property
    def size(self) -> tuple[int, int]:
        return self.cell_w * self.cols, self.cell_h * self.rows

    @property
    def cells(self) -> int:
        return self.cols * self.rows

    def box(self, row: int, col: int) -> tuple[int, int, int, int]:
        x, y = col * self.cell_w, row * self.cell_h
        return x, y, x + self.cell_w, y + self.cell_h

    def iter_cells(self) -> Iterable[tuple[int, int]]:
        for row in range(self.rows):
            for col in range(self.cols):
                yield row, col


def _alpha_rows(img: Image.Image, threshold: int) -> tuple[int, int, list[list[bool]]]:
    """The sheet reduced to a boolean drawn/not-drawn bitmap, once."""
    alpha = img.convert("RGBA").getchannel("A")
    w, h = alpha.size
    data = alpha.tobytes()
    return w, h, [[data[y * w + x] > threshold for x in range(w)] for y in range(h)]


def _bands(occupied: Sequence[bool]) -> list[tuple[int, int]]:
    """Maximal runs of occupied indices, as half-open [start, end) spans."""
    runs: list[tuple[int, int]] = []
    start: Optional[int] = None
    for i, v in enumerate(occupied):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(occupied)))
    return runs


def _divisors(n: int) -> list[int]:
    return sorted(d for d in range(MIN_CELL, n + 1) if n % d == 0 and n // d <= MAX_CELLS)


def _pitch(total: int, bands: Sequence[tuple[int, int]]) -> int:
    """Smallest cell size that divides the axis and no content band straddles.

    The aligned-sheet convention is the whole basis of this: a frame's art lives
    INSIDE its cell, so a candidate cell size that cuts through drawn pixels is
    provably wrong. Scanning divisors smallest-first then takes the finest grid
    the content permits. This is what survives the sparse layouts a naive
    band-COUNT dies on — a block sheet with one occupied column out of four has
    a single band, no pitch to measure, and still resolves correctly because
    only a cell of the true width contains that band whole.
    """
    if not bands:
        return total
    for cell in _divisors(total):
        if all(s // cell == (e - 1) // cell for s, e in bands):
            return cell
    return total


def detect_grid(img: Image.Image, *, cell: Optional[tuple[int, int]] = None,
                alpha_threshold: int = ALPHA_THRESHOLD) -> Grid:
    """The cell lattice of a sprite sheet.

    Pass `cell` whenever you know it — a gear sheet MUST inherit its body's grid,
    and inheriting is strictly better than re-deriving from sparser content.
    Autodetection is for the body sheet itself, or for a lone sheet with no peer.
    """
    w, h = img.size
    if cell:
        cw, ch = int(cell[0]), int(cell[1])
        if cw <= 0 or ch <= 0 or w % cw or h % ch:
            raise ValueError(f"cell {cw}x{ch} does not tile a {w}x{h} sheet")
        return Grid(cw, ch, w // cw, h // ch)

    _, _, bits = _alpha_rows(img, alpha_threshold)
    col_occ = [any(bits[y][x] for y in range(h)) for x in range(w)]
    row_occ = [any(row) for row in bits]
    if not any(col_occ):          # a blank sheet has no grid to find
        return Grid(w, h, 1, 1)
    cw = _pitch(w, _bands(col_occ))
    ch = _pitch(h, _bands(row_occ))
    return Grid(cw, ch, w // cw, h // ch)


# ---------------------------------------------------------------------------
# Masks
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Mask:
    """One frame's drawn pixels. Coordinates are CELL-LOCAL throughout — an
    anchor means the same thing in every frame of every sheet that way, and the
    caller adds the cell origin only when it draws."""
    w: int
    h: int
    bits: tuple[bool, ...]

    def __bool__(self) -> bool:
        return any(self.bits)

    @property
    def count(self) -> int:
        return sum(self.bits)

    def at(self, x: int, y: int) -> bool:
        return self.bits[y * self.w + x]

    def points(self) -> list[tuple[int, int]]:
        return [(i % self.w, i // self.w) for i, v in enumerate(self.bits) if v]

    def centroid(self) -> Optional[tuple[float, float]]:
        pts = self.points()
        if not pts:
            return None
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    def bbox(self) -> Optional[tuple[int, int, int, int]]:
        pts = self.points()
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def cell_mask(img: Image.Image, grid: Grid, row: int, col: int, *,
              alpha_threshold: int = ALPHA_THRESHOLD) -> Mask:
    alpha = img.convert("RGBA").getchannel("A").crop(grid.box(row, col))
    return Mask(grid.cell_w, grid.cell_h,
                tuple(v > alpha_threshold for v in alpha.tobytes()))


def _intersect(masks: Sequence[Mask]) -> Mask:
    first = masks[0]
    bits = tuple(all(m.bits[i] for m in masks) for i in range(len(first.bits)))
    return Mask(first.w, first.h, bits)


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Anchor:
    """Where a held item belongs in one frame. (x, y) are cell-local pixels.

    `source` is not decoration. A measured anchor is where five weapons agreed;
    an inferred one is this module's best read of a body pose. Anything that
    prints or ships anchors prints the source with them.
    """
    row: int
    col: int
    x: float
    y: float
    source: str
    support: int = 0          # how many sheets agreed (measured) / cluster px (inferred)

    @property
    def measured(self) -> bool:
        return self.source in MEASURED_SOURCES


def measure_anchors(sheets: Sequence[Image.Image], grid: Grid, *,
                    alpha_threshold: int = ALPHA_THRESHOLD) -> list[Anchor]:
    """Ground-truth grip anchors from the aligned sheets that already exist.

    Different weapons for the same action differ everywhere EXCEPT the hand —
    a dagger and a maul share only the few pixels where both are gripped. So the
    intersection of their per-frame masks is the grip, and its centroid is the
    anchor. With one sheet there is nothing to intersect and the honest answer is
    the whole gear's centroid, flagged `measured_single`: still a measurement,
    but of the weapon, not of the hand.
    """
    if not sheets:
        return []
    anchors: list[Anchor] = []
    for row, col in grid.iter_cells():
        masks = [cell_mask(s, grid, row, col, alpha_threshold=alpha_threshold)
                 for s in sheets]
        drawn = [m for m in masks if m]
        if not drawn:
            continue                       # empty cell: this frame has no gear
        if len(drawn) >= 2:
            shared = _intersect(drawn)
            if shared:
                cx, cy = shared.centroid()  # type: ignore[misc]
                anchors.append(Anchor(row, col, cx, cy, MEASURED, len(drawn)))
                continue
            # No common pixel — the weapons genuinely disagree about the grip.
            # Averaging their centroids is the weaker answer, and says so.
            cs = [m.centroid() for m in drawn]
            anchors.append(Anchor(row, col,
                                  sum(c[0] for c in cs) / len(cs),  # type: ignore[index]
                                  sum(c[1] for c in cs) / len(cs),  # type: ignore[index]
                                  MEASURED_SINGLE, len(drawn)))
            continue
        cx, cy = drawn[0].centroid()        # type: ignore[misc]
        anchors.append(Anchor(row, col, cx, cy, MEASURED_SINGLE, 1))
    return anchors


def anchor_side_bias(anchors: Sequence[Anchor], grid: Grid) -> dict[int, int]:
    """Per row: is this layer's hand on the +x or -x side of the frame?

    Rows are VIEWS (nw/sw, or the four diagonals), so the main hand swaps sides
    between them and a "farthest point from the body" rule picks the wrong hand
    half the time. The measured anchors already answer it per row, so the bias is
    read off the art rather than assumed from a handedness convention.
    """
    bias: dict[int, int] = {}
    for row in range(grid.rows):
        xs = [a.x for a in anchors if a.row == row]
        if xs:
            bias[row] = 1 if (sum(xs) / len(xs)) >= grid.cell_w / 2 else -1
    return bias


# ---------------------------------------------------------------------------
# Inference for uncovered actions
# ---------------------------------------------------------------------------
def _palette_counts(body: Image.Image, grid: Grid, anchors: Sequence[Anchor], *,
                    radius: int, min_luma_sum: int, alpha_threshold: int,
                    into: dict[tuple[int, int, int], int]) -> None:
    rgb = body.convert("RGBA")
    px = rgb.load()
    for a in anchors:
        if not a.measured:
            continue
        ox, oy, _, _ = grid.box(a.row, a.col)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                x, y = ox + int(a.x) + dx, oy + int(a.y) + dy
                if not (0 <= x < rgb.width and 0 <= y < rgb.height):
                    continue
                r, g, b, al = px[x, y]
                if al <= alpha_threshold or r + g + b < min_luma_sum:
                    continue
                key = (r // 16 * 16, g // 16 * 16, b // 16 * 16)
                into[key] = into.get(key, 0) + 1


def pool_hand_palette(samples: Sequence[tuple[Image.Image, Sequence[Anchor]]],
                      grid: Grid, *, radius: int = 5, top_n: int = 5,
                      min_luma_sum: int = 250,
                      alpha_threshold: int = ALPHA_THRESHOLD
                      ) -> list[tuple[int, int, int]]:
    """One palette from every covered action, ranked ONCE over the pooled counts.

    Concatenating each action's top-N instead would quietly widen the palette
    with each action's own runners-up — hair, leather, whatever else brushed the
    grip in one pose — and a wider palette merges the hand blob into the arm and
    the torso. Rank globally, keep the few colours that dominate everywhere.

    min_luma_sum is not a magic number: a hand is a LIT surface, while the dark
    end of the ramp is outline and shadow that every part of the character
    shares, so a dark entry in the palette matches the whole silhouette and the
    clustering collapses. Measured on the real project, letting one dark leather
    tone in tripled the inference error (median 11px -> 30px on a 96x80 cell).
    """
    counts: dict[tuple[int, int, int], int] = {}
    for body, anchors in samples:
        _palette_counts(body, grid, anchors, radius=radius,
                        min_luma_sum=min_luma_sum,
                        alpha_threshold=alpha_threshold, into=counts)
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:top_n]
    # Recenter each quantization bucket so the palette sits mid-bucket.
    return [(r + 8, g + 8, b + 8) for (r, g, b), _ in ranked]


def learn_hand_palette(body: Image.Image, grid: Grid, anchors: Sequence[Anchor],
                       **kw) -> list[tuple[int, int, int]]:
    """The colours the body wears AT a measured grip — i.e. the hand.

    No hardcoded skin tone: this samples the body sheet in a disc around each
    ground-truth anchor and keeps the most common quantized colours. Whatever
    the hand is — bare, gloved, gauntleted — it is the one thing every measured
    anchor has under it, so it wins the count. Very dark pixels are dropped
    because the outline colour is common to every part of the character and
    would match everywhere.
    """
    return pool_hand_palette([(body, anchors)], grid, **kw)


def _components(flags: list[bool], w: int, h: int, min_size: int) -> list[list[tuple[int, int]]]:
    """8-connected blobs of a boolean bitmap, largest-noise filtered."""
    seen = [False] * len(flags)
    out: list[list[tuple[int, int]]] = []
    for start in range(len(flags)):
        if not flags[start] or seen[start]:
            continue
        seen[start] = True
        stack = [start]
        pts: list[tuple[int, int]] = []
        while stack:
            i = stack.pop()
            x, y = i % w, i // w
            pts.append((x, y))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h:
                        j = ny * w + nx
                        if flags[j] and not seen[j]:
                            seen[j] = True
                            stack.append(j)
        if len(pts) >= min_size:
            out.append(pts)
    return out


def _cell_pixels(img: Image.Image, grid: Grid, row: int, col: int):
    return img.convert("RGBA").crop(grid.box(row, col)).load()


def infer_anchor(body: Image.Image, grid: Grid, row: int, col: int, *,
                 palette: Sequence[tuple[int, int, int]], side: int,
                 tolerance: float = 48.0, min_component: int = 8,
                 alpha_threshold: int = ALPHA_THRESHOLD) -> Optional[Anchor]:
    """Best read of the hand in ONE body frame that has no gear sheet.

    Palette-match the hand colour, drop the blob nearest the top-centre of the
    silhouette (that is the face — the same colour as the hand and always in
    frame), keep the blob on the side this layer's hand lives on, and take its
    outermost pixel: the grip sits at the END of the arm, not at its centre.
    Falls back to the silhouette extremity when nothing matches, and says which
    it did. Cost of being wrong is a placeholder a few px off the hand — which
    is why validate_inference exists to quantify it before anyone trusts it.
    """
    px = _cell_pixels(body, grid, row, col)
    w, h = grid.cell_w, grid.cell_h
    solid: list[bool] = []
    matched: list[bool] = []
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            on = a > alpha_threshold
            solid.append(on)
            if not on or not palette:
                matched.append(False)
                continue
            best = min((r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
                       for pr, pg, pb in palette)
            matched.append(best < tolerance * tolerance)
    body_pts = [(i % w, i // w) for i, v in enumerate(solid) if v]
    if not body_pts:
        return None
    cx = sum(p[0] for p in body_pts) / len(body_pts)
    cy = sum(p[1] for p in body_pts) / len(body_pts)
    top = min(p[1] for p in body_pts)
    midx = (min(p[0] for p in body_pts) + max(p[0] for p in body_pts)) / 2

    blobs = _components(matched, w, h, min_component)
    source = INFERRED_HAND
    if not blobs:
        # Nothing hand-coloured: fall back to the silhouette itself.
        blobs = [body_pts]
        source = INFERRED_SILHOUETTE

    def centre(pts):
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    if len(blobs) > 1:
        head = min(blobs, key=lambda p: (centre(p)[1] - top) ** 2
                   + 0.25 * (centre(p)[0] - midx) ** 2)
        blobs = [p for p in blobs if p is not head] or [head]
    # Prefer a big blob on the right side; size breaks ties between two arms.
    arm = max(blobs, key=lambda p: side * (centre(p)[0] - cx) * 2 + len(p) ** 0.5)
    ex, ey = max(arm, key=lambda p: side * (p[0] - cx) + 0.5 * abs(p[1] - cy))
    return Anchor(row, col, float(ex), float(ey), source, len(arm))


def infer_anchors(body: Image.Image, grid: Grid, *,
                  palette: Sequence[tuple[int, int, int]],
                  side_bias: dict[int, int], **kw) -> list[Anchor]:
    """Inferred anchors for every non-empty cell of a body sheet."""
    out: list[Anchor] = []
    for row, col in grid.iter_cells():
        side = side_bias.get(row, side_bias.get(0, 1))
        a = infer_anchor(body, grid, row, col, palette=palette, side=side, **kw)
        if a is not None:
            out.append(a)
    return out


def validate_inference(body: Image.Image, grid: Grid, measured: Sequence[Anchor], *,
                       palette: Sequence[tuple[int, int, int]],
                       side_bias: dict[int, int], **kw) -> dict:
    """Run the inference on frames that DO have ground truth and score it.

    This is the number that keeps the inferred anchors honest: same body sheet,
    same code path, compared against the intersection anchors. Reported in
    pixels alongside the cell size, so a reader can judge "6px on an 96x80 cell"
    for themselves instead of taking "inferred" on faith.
    """
    errors: list[float] = []
    per_cell: list[dict] = []
    for a in measured:
        if not a.measured:
            continue
        side = side_bias.get(a.row, side_bias.get(0, 1))
        got = infer_anchor(body, grid, a.row, a.col, palette=palette, side=side, **kw)
        if got is None:
            per_cell.append({"row": a.row, "col": a.col, "error_px": None})
            continue
        err = math.hypot(got.x - a.x, got.y - a.y)
        errors.append(err)
        per_cell.append({"row": a.row, "col": a.col, "error_px": round(err, 1),
                         "source": got.source})
    ordered = sorted(errors)
    return {
        "n": len(errors),
        "cell": [grid.cell_w, grid.cell_h],
        "median_px": round(ordered[len(ordered) // 2], 1) if ordered else None,
        "mean_px": round(sum(errors) / len(errors), 1) if errors else None,
        "max_px": round(ordered[-1], 1) if ordered else None,
        "per_cell": per_cell,
    }


# ---------------------------------------------------------------------------
# Placeholder rendering
# ---------------------------------------------------------------------------
# Hazard stripes. Nothing in a real palette looks like this, which is the point:
# a placeholder that could pass for art never gets replaced.
STRIPE_A = (255, 0, 190, 235)
STRIPE_B = (26, 26, 32, 235)
GRIP_DOT = (0, 240, 255, 255)
OUTLINE = (10, 10, 14, 255)


def _bar(length: int, width: int) -> Image.Image:
    """The placeholder glyph, drawn upright with its GRIP at the bottom-centre."""
    bar = Image.new("RGBA", (width, length), (0, 0, 0, 0))
    d = ImageDraw.Draw(bar)
    step = max(3, width)
    for i, y in enumerate(range(0, length, step)):
        d.rectangle([0, y, width - 1, min(length - 1, y + step - 1)],
                    fill=STRIPE_A if i % 2 == 0 else STRIPE_B)
    d.rectangle([0, 0, width - 1, length - 1], outline=OUTLINE)
    return bar


def _rotate_vec(vx: float, vy: float, deg: float) -> tuple[float, float]:
    """A vector under Pillow's CCW image rotation, in y-down image space."""
    a = math.radians(deg)
    return (math.cos(a) * vx + math.sin(a) * vy,
            -math.sin(a) * vx + math.cos(a) * vy)


def stamp_placeholder(sheet: Image.Image, grid: Grid, anchor: Anchor, *,
                      direction: tuple[float, float], length: int, width: int) -> None:
    """Composite one placeholder glyph so its grip lands exactly on the anchor.

    Drawn into a CELL-SIZED layer, never straight onto the sheet: a stamp that
    ran past its cell would land in the neighbouring frame, and the rig reads
    frames by cell rectangle — one long weapon would then smear across the whole
    animation. Clipping at the cell edge is what real aligned art does too.
    """
    dx, dy = direction
    norm = math.hypot(dx, dy) or 1.0
    dx, dy = dx / norm, dy / norm
    # The glyph points "up" (0,-1); solve for the CCW rotation that sends it to d.
    deg = math.degrees(math.atan2(-dx, -dy))
    bar = _bar(max(4, length), max(3, width))
    rot = bar.rotate(deg, resample=Image.NEAREST, expand=True)
    gx, gy = _rotate_vec(0.0, bar.height / 2.0, deg)   # grip offset, rotated
    cx, cy = anchor.x - gx, anchor.y - gy              # cell-local bar centre
    layer = Image.new("RGBA", (grid.cell_w, grid.cell_h), (0, 0, 0, 0))
    layer.paste(rot, (int(round(cx - rot.width / 2)), int(round(cy - rot.height / 2))))
    d = ImageDraw.Draw(layer)
    d.ellipse([anchor.x - 2, anchor.y - 2, anchor.x + 2, anchor.y + 2],
              fill=GRIP_DOT, outline=OUTLINE)
    ox, oy, _, _ = grid.box(anchor.row, anchor.col)
    sheet.alpha_composite(layer, dest=(ox, oy))


def outward_direction(body: Image.Image, grid: Grid, anchor: Anchor, *,
                      side: int, alpha_threshold: int = ALPHA_THRESHOLD
                      ) -> tuple[float, float]:
    """Which way the weapon points: away from the body's mass, through the grip.

    A weapon held in a hand extends outward, so the body-centroid -> anchor ray
    is the cheapest orientation that is right most of the time. Degenerate case
    (anchor sits on the centroid) falls back to straight out on the hand's side.
    """
    mask = cell_mask(body, grid, anchor.row, anchor.col, alpha_threshold=alpha_threshold)
    c = mask.centroid()
    if c is None:
        return (float(side), 0.0)
    dx, dy = anchor.x - c[0], anchor.y - c[1]
    if math.hypot(dx, dy) < 1.0:
        return (float(side), 0.0)
    return (dx, dy)


def build_placeholder_sheet(body: Image.Image, grid: Grid, anchors: Sequence[Anchor], *,
                            side_bias: Optional[dict[int, int]] = None,
                            item_class: str = "main_hand",
                            length_frac: Optional[float] = None,
                            width_frac: Optional[float] = None) -> Image.Image:
    """A full aligned sheet: the body's exact canvas and grid, gear only.

    Same size, same lattice, transparent everywhere the gear is not — that is the
    entire aligned-sheet contract, and it is what lets the rig overlay the result
    1:1 with no offset. Frames with no anchor stay empty rather than getting a
    guessed stamp; the rig hides a layer for a frame it cannot draw, which is a
    better failure than a weapon floating in the wrong place.
    """
    sheet = Image.new("RGBA", (grid.cell_w * grid.cols, grid.cell_h * grid.rows),
                      (0, 0, 0, 0))
    from bgate_core.items import gear_shape   # taxonomy owns the proportions
    default_len, default_wid = gear_shape(item_class)
    bias = side_bias or {}
    length = max(4, int(round(grid.cell_h * (length_frac or default_len))))
    width = max(3, int(round(grid.cell_h * (width_frac or default_wid))))
    for a in anchors:
        side = bias.get(a.row, bias.get(0, 1))
        d = outward_direction(body, grid, a, side=side)
        stamp_placeholder(sheet, grid, a, direction=d, length=length, width=width)
    return sheet


# ---------------------------------------------------------------------------
# The rig profile — everything the covered actions teach, in one object
# ---------------------------------------------------------------------------
def body_action_for(layer_action: str) -> str:
    """Which body animation drives a gear layer (dual_wield_main <- the swing)."""
    for body, layers in BODY_TO_LAYER_ACTIONS.items():
        if layer_action in layers:
            return body
    return layer_action


@dataclass
class RigProfile:
    """What the four covered actions teach about this character's hands.

    Learned once, reused for every uncovered action and every new weapon class.
    Keeping it one object is what stops the palette and the side bias from being
    silently re-derived (differently) at each call site.
    """
    grid: Grid
    palette: list[tuple[int, int, int]]
    side_bias: dict[str, dict[int, int]]        # hand -> row -> +1 / -1
    measured: dict[str, list[Anchor]]           # layer_action -> anchors
    validation: dict                            # inference error, in pixels

    def bias_for(self, layer_action: str) -> dict[int, int]:
        hand = LAYER_HAND.get(layer_action, "main_hand")
        return self.side_bias.get(hand) or next(iter(self.side_bias.values()), {})


def learn_rig(body_sheets: dict[str, Image.Image],
              gear_sheets: dict[str, Sequence[Image.Image]], *,
              grid: Optional[Grid] = None,
              alpha_threshold: int = ALPHA_THRESHOLD) -> RigProfile:
    """Measure the covered actions, then calibrate the inference against them.

    body_sheets is body_action -> sheet; gear_sheets is layer_action -> the
    aligned sheets that exist for it (one per weapon). The grid comes from a
    BODY sheet, never a gear sheet: gear art is sparse and a sparse sheet gives
    detection less to work with, while the aligned-sheet convention guarantees
    the two lattices are identical anyway.
    """
    if not body_sheets:
        raise ValueError("no body sheets — the grid and the hand palette both "
                         "come from the character, not from the gear")
    if grid is None:
        grid = detect_grid(next(iter(body_sheets.values())),
                           alpha_threshold=alpha_threshold)

    measured: dict[str, list[Anchor]] = {}
    for layer_action, sheets in gear_sheets.items():
        if sheets:
            measured[layer_action] = measure_anchors(list(sheets), grid,
                                                     alpha_threshold=alpha_threshold)

    bias: dict[str, dict[int, int]] = {}
    for layer_action, anchors in measured.items():
        hand = LAYER_HAND.get(layer_action, "main_hand")
        bias.setdefault(hand, {}).update(anchor_side_bias(anchors, grid))

    samples = [(body_sheets[body_action_for(la)], anchors)
               for la, anchors in measured.items()
               if body_action_for(la) in body_sheets]
    palette = pool_hand_palette(samples, grid, alpha_threshold=alpha_threshold)

    profile = RigProfile(grid, palette, bias, measured, {})
    validation: dict = {}
    for layer_action, anchors in measured.items():
        body = body_sheets.get(body_action_for(layer_action))
        if body is None:
            continue
        validation[layer_action] = validate_inference(
            body, grid, anchors, palette=palette,
            side_bias=profile.bias_for(layer_action),
            alpha_threshold=alpha_threshold)
    errs = [v["median_px"] for v in validation.values() if v.get("median_px") is not None]
    profile.validation = {
        "per_action": validation,
        "median_px": round(sorted(errs)[len(errs) // 2], 1) if errs else None,
        "cell": [grid.cell_w, grid.cell_h],
    }
    return profile


def anchors_for(profile: RigProfile, body: Image.Image, layer_action: str, *,
                alpha_threshold: int = ALPHA_THRESHOLD) -> list[Anchor]:
    """The anchors to stamp for one layer action — measured if we have them."""
    have = profile.measured.get(layer_action)
    if have:
        return have
    return infer_anchors(body, profile.grid, palette=profile.palette,
                         side_bias=profile.bias_for(layer_action),
                         alpha_threshold=alpha_threshold)


def anchor_provenance(anchors: Sequence[Anchor]) -> dict[str, int]:
    """How many anchors came from where — printed next to any anchor dump."""
    out: dict[str, int] = {}
    for a in anchors:
        out[a.source] = out.get(a.source, 0) + 1
    return out


# ---------------------------------------------------------------------------
# Naming + disk
# ---------------------------------------------------------------------------
def layer_sheet_name(weapon: str, layer_action: str) -> str:
    """The filename the game's format string builds:
    "res://assets/items/main_hand/animations/%s_%s.png" % [weapon, layer_action].
    Generated sheets must match it byte for byte or nothing loads them."""
    return f"{weapon}_{layer_action}.png"


def throwable_sheet_name(body_action: str) -> str:
    """The throwable slot's shared sheet: "placeholder_%s.png" % action."""
    return f"placeholder_{body_action}.png"


def layer_actions_for(body_action: str) -> tuple[str, ...]:
    """The gear layer(s) a body animation drives."""
    return BODY_TO_LAYER_ACTIONS.get(body_action, (body_action,))


def save_placeholder(sheet: Image.Image, path: Path, *, note: str = "") -> Path:
    """Write a placeholder sheet WITH the marker that makes coverage truthful."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    info = PngImagePlugin.PngInfo()
    info.add_text(MARKER_KEY, MARKER_VALUE)
    if note:
        info.add_text("bgate_note", note)
    sheet.save(path, "PNG", pnginfo=info)
    return path


def is_placeholder(path: Path) -> bool:
    """True only for sheets THIS module stamped. Filenames lie — the real project
    ships hand-drawn art called placeholder_throw_one_hand.png."""
    try:
        with Image.open(path) as im:
            return im.text.get(MARKER_KEY) == MARKER_VALUE  # type: ignore[attr-defined]
    except Exception:
        return False


def body_actions(char_dir: Path, prefix: str) -> list[str]:
    """The actions a character actually has sheets for, from <prefix>_<action>.png."""
    pat = re.compile(rf"^{re.escape(prefix)}_(.+)\.png$")
    out = []
    for p in sorted(Path(char_dir).glob(f"{prefix}_*.png")):
        m = pat.match(p.name)
        if m:
            out.append(m.group(1))
    return out


# ---------------------------------------------------------------------------
# Coverage — the thing that tells a human what still needs drawing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CoverageSpec:
    """What SHOULD exist. Coverage is the diff between this and the disk."""
    animations_dir: Path
    weapons: tuple[str, ...]
    body_actions: tuple[str, ...]
    throwable_dir: Optional[Path] = None


def coverage_report(spec: CoverageSpec) -> dict:
    """Which (weapon x action) sheets are real art, which are stamped
    placeholders, and which are simply absent.

    "Absent" is the load-bearing category: a missing sheet is not a cosmetic gap,
    it is the equipped weapon disappearing for that whole action. The throwable
    slot is included because the same disappearance happens there, off a
    different naming convention.
    """
    rows: list[dict] = []
    layer_of_body: list[tuple[str, str]] = []
    for action in spec.body_actions:
        if action in THROWABLE_BODY_ACTIONS:
            continue
        for layer in layer_actions_for(action):
            layer_of_body.append((action, layer))

    for weapon in spec.weapons:
        for body_action, layer in layer_of_body:
            path = Path(spec.animations_dir) / layer_sheet_name(weapon, layer)
            rows.append({
                "slot": LAYER_HAND.get(layer, "main_hand"),
                "weapon": weapon,
                "body_action": body_action,
                "layer_action": layer,
                "path": str(path),
                "status": ("placeholder" if is_placeholder(path)
                           else "real" if path.exists() else "missing"),
            })

    if spec.throwable_dir is not None:
        for action in spec.body_actions:
            if action not in THROWABLE_BODY_ACTIONS:
                continue
            path = Path(spec.throwable_dir) / throwable_sheet_name(action)
            rows.append({
                "slot": "throwable",
                "weapon": "*",
                "body_action": action,
                "layer_action": action,
                "path": str(path),
                "status": ("placeholder" if is_placeholder(path)
                           else "real" if path.exists() else "missing"),
            })

    summary = {"real": 0, "placeholder": 0, "missing": 0}
    for r in rows:
        summary[r["status"]] += 1
    return {
        "weapons": list(spec.weapons),
        "body_actions": list(spec.body_actions),
        "rows": rows,
        "summary": summary,
        "needs_art": [r for r in rows if r["status"] != "real"],
    }


def format_coverage(report: dict) -> str:
    """The coverage table a human reads: weapons down, layer actions across."""
    marks = {"real": "##", "placeholder": "::", "missing": "--"}
    layers: list[str] = []
    for r in report["rows"]:
        if r["slot"] != "throwable" and r["layer_action"] not in layers:
            layers.append(r["layer_action"])
    weapons = report["weapons"]
    cell = {(r["weapon"], r["layer_action"]): r["status"] for r in report["rows"]}
    wide = max([len(w) for w in weapons] + [8])
    head = " " * wide + " | " + " | ".join(a[:14].center(14) for a in layers)
    lines = [head, "-" * len(head)]
    for w in weapons:
        lines.append(w.ljust(wide) + " | " + " | ".join(
            marks.get(cell.get((w, a), "missing"), "--").center(14) for a in layers))
    extra = [r for r in report["rows"] if r["slot"] == "throwable"]
    if extra:
        lines.append("")
        for r in extra:
            lines.append(f"throwable {r['body_action']}: {r['status']}")
    s = report["summary"]
    lines.append("")
    lines.append(f"## real {s['real']}   :: placeholder {s['placeholder']}   "
                 f"-- missing {s['missing']}")
    return "\n".join(lines)
