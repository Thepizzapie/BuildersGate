"""Worn-gear sheets - find the grid, then stamp generated gear on the hand.

The equip/layer system (templates/2d gear_rig.gd) rides the body's clock: a gear
layer only has to be an ALIGNED SHEET - the character's own canvas and grid with
only the gear drawn - and it overlays 1:1 for free. That is cheap per animation
and brutally expensive per MISSING animation: the moment the body plays an
action the weapon has no sheet for, the layer blanks and the equipped weapon
VANISHES mid-combat. A real project shows the gap plainly: five weapons drawn
for four attack actions, against a body with fourteen.

Filling that gap with an image model is both wasteful and wrong. The gear has to
land on the HAND, in every frame, to the pixel - a diffusion model cannot hit
that, and paying it to miss is worse than not paying. So this module is
procedural end to end:

  1. GRID. `detect_grid` reads the cell size off the sheet's occupied bands.
  2. ANCHOR. The grip comes from rigmap (authored or sliced by a person) as
     `Anchor` objects; every anchor carries its `source`, so a reader can tell
     which ones were read off real art.
  3. STAMP. `stamp_generated` composites a generated gear image at the
     anchor, palette-conformed to the body first, clipped at the cell edge.

Pure Pillow + stdlib on purpose (numpy is an optional extra here, and these
canvases are 384x160 - a Python loop is not the bottleneck). Every function is
I/O-free except `conform_to_palette`, which reads and writes the icon.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from PIL import Image

# A pixel counts as drawn above this alpha. Sprite sheets carry feathered edges;
# 8 keeps the halo out of centroids without eating genuine soft pixels.
ALPHA_THRESHOLD = 8

# Grid search bounds. A sprite cell below 8px or a sheet past 16 cells on an axis
# is not a thing these pipelines produce, and allowing them lets a sparse sheet
# (one small band on a wide canvas) resolve to an absurdly fine grid.
MIN_CELL = 8
MAX_CELLS = 16

# Anchor provenance, weakest last. Callers report these verbatim - the whole
# point is that a reader can tell which anchors were read off real art.
AUTHORED = "authored"                  # a person pointed at the pixel (spriteedit)
SLICE = "slice"                        # a person dragged a slice in Aseprite
MEASURED = "measured"                  # >=2 gear sheets agreed; grip intersection
MEASURED_SINGLE = "measured_single"    # one sheet only; centroid of the whole gear
INFERRED_HAND = "inferred_hand"        # learned hand palette, clustered on the body
INFERRED_SILHOUETTE = "inferred_silhouette"  # no palette match; body extremity
# Human-authored tiers count as measured - rigmap has documented `authored` as
# outranking every inferred source since it existed, but this tuple never
# learned that, so Anchor.measured was False for the one kind of anchor a
# person had explicitly placed.
MEASURED_SOURCES = (AUTHORED, SLICE, MEASURED, MEASURED_SINGLE)


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
    band-COUNT dies on - a block sheet with one occupied column out of four has
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

    Pass `cell` whenever you know it - a gear sheet MUST inherit its body's grid,
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
    """One frame's drawn pixels. Coordinates are CELL-LOCAL throughout - an
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


def stamp_generated(sheet: Image.Image, grid: Grid, anchor: Anchor,
                    generated: Image.Image, *,
                    anchor_palette: Sequence[tuple[int, int, int]] = (),
                    anchor_stroke: float = 0.0) -> dict:
    """Composite a GENERATED weapon/gear image at the anchor - ITEM 9.

    MEASURED: a weapon generated separately from the body (its own model
    call, its own 1px ink line, a warmer palette than the pinned one) gets
    stamped straight onto a torso that WAS conformed to the pinned palette,
    and the seam is visible - the weapon's colours and line weight are both
    a different asset's. This path has real pixels, so it runs them
    through ``framegate.conform_stamp`` first - nearest-colour snap to the
    anchor palette, plus a reported (never silently "fixed") ink-weight flag
    a palette conform cannot correct - THEN composites, clipped at the cell
    edge.

    Returns {line_weight_flag, stamp_stroke, anchor_stroke}. `anchor_palette`/
    `anchor_stroke` empty skips the conform (the composite still happens) -
    a caller with no pinned palette gets the old behaviour, not a refusal.
    """
    from . import framegate as _fg
    conformed = (_fg.conform_stamp(generated, anchor_palette, anchor_stroke)
                if anchor_palette else
                {"image": generated, "line_weight_flag": False,
                 "stamp_stroke": 0.0, "anchor_stroke": anchor_stroke})
    img = conformed["image"]
    layer = Image.new("RGBA", (grid.cell_w, grid.cell_h), (0, 0, 0, 0))
    layer.alpha_composite(
        img, dest=(int(round(anchor.x - img.width / 2)),
                  int(round(anchor.y - img.height / 2))))
    ox, oy, _, _ = grid.box(anchor.row, anchor.col)
    sheet.alpha_composite(layer, dest=(ox, oy))
    return {"line_weight_flag": conformed["line_weight_flag"],
           "stamp_stroke": conformed["stamp_stroke"],
           "anchor_stroke": conformed["anchor_stroke"]}


# ---------------------------------------------------------------------------
# Naming + disk
# ---------------------------------------------------------------------------
def conform_to_palette(icon_path: str | Path, palette: Sequence[Sequence[int]],
                       *, stroke_width: int = 0,
                       stroke_color: Optional[Sequence[int]] = None,
                       out_path: Optional[str | Path] = None) -> dict:
    """Snap a generated icon onto the pinned palette, then outline it.

    `palette` is RGB triples (bible-pinned or sampled from the character's own
    sheets - same shape :func:`bgate_core.art.spritekit.lock_palette` and
    ``image_sprites``' palette_lock already take). Quantisation is delegated
    there wholesale rather than forked.

    `stroke_width` > 0 dilates the icon's opaque silhouette by that many
    pixels and fills the ring BEHIND the (already-quantised) icon with
    `stroke_color` (default: the darkest palette entry) - a cheap, deterministic
    outline so a held weapon icon still reads against a busy floor tile, which a
    body sheet locked to its own palette never needed. Never raises: a stroke
    or a lock that fails leaves the source file untouched and says why.
    """
    from bgate_core.art import spritekit as _spritekit

    src = Path(icon_path)
    dst = Path(out_path or icon_path)
    if not src.is_file():
        return {"ok": False, "path": str(src), "error": f"no icon at {src}"}

    lock = _spritekit.lock_palette(src, palette, out_path=dst)
    result = {"ok": bool(lock.get("ok")), "path": str(dst),
              "colors": lock.get("colors", 0), "changed": lock.get("changed", 0.0),
              "palette_note": lock.get("note", ""), "stroke_width": int(stroke_width),
              "stroked": False}
    if not lock.get("ok") or stroke_width <= 0:
        return result

    try:
        img = Image.open(dst).convert("RGBA")
        alpha = img.getchannel("A")
        mask = alpha.point(lambda a: 255 if a > ALPHA_THRESHOLD else 0)
        from PIL import ImageFilter

        # MaxFilter grows the opaque region by roughly (size-1)/2 px per pass;
        # odd kernel size, chained so a stroke_width of any size is reachable
        # with a small, always-odd filter.
        dilated = mask
        remaining = max(1, int(stroke_width))
        while remaining > 0:
            step = min(remaining, 3)
            k = step * 2 + 1
            dilated = dilated.filter(ImageFilter.MaxFilter(k))
            remaining -= step
        ring = Image.eval(dilated, lambda a: a)
        color = tuple(int(c) for c in (stroke_color or _darkest(palette))[:3])
        outline = Image.new("RGBA", img.size, color + (0,))
        outline.putalpha(ring)
        composed = Image.alpha_composite(outline, img)
        composed.save(dst)
        result["stroked"] = True
        result["stroke_color"] = list(color)
    except OSError as exc:
        result["stroke_error"] = f"{type(exc).__name__}: {exc}"
    return result


def _darkest(palette: Sequence[Sequence[int]]) -> tuple[int, int, int]:
    """Fallback stroke colour when none is given: the darkest palette entry,
    which reads as an outline on flat/cel art far more often than black does
    (black is frequently already IN the palette as a highlight or a line)."""
    entries = [tuple(int(c) for c in rgb[:3]) for rgb in palette if len(rgb) >= 3]
    if not entries:
        return (0, 0, 0)
    return min(entries, key=sum)
