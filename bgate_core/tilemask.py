"""Reading a generated tile sheet: which tile answers which neighbour shape.

THE PIECE THAT MAKES GENERATED TILES USABLE. A model hands back a terrain
sheet — grass meeting stone, every edge and corner — in whatever arrangement
it felt like. `autotile.Terrain` needs the opposite: a map from NEIGHBOUR MASK
to atlas coordinate. Somebody has to say "this one is the north-west corner",
and asking a human to name 47 tiles by hand is how a pipeline stops being used.

So this reads it off the pixels. Each tile is classified by its own body
colour, then by what its eight compass zones show: a stone tile whose western
edge is drawn as grass is the tile for "stone with grass to the west", which
is a mask. Verified on real Retro Diffusion output before it was written —
pure fill came back 255, a west-edge tile 55, a corner 19.

WHAT IT REFUSES TO DO. Art whose colours do not separate into two terrains
(one terrain, or three) is reported, never guessed: a table built from a bad
split puts the wrong sprite at every seam, and every downstream check would
pass it because the numbers are all self-consistent.

Duplicates are NORMAL and are not an error. A twenty-tile sheet routinely
draws two different pure-grass tiles; `Terrain.from_table` raises on a
collision, so the second and later tiles for one mask become ALTERNATIVES,
which is what Godot's `alt` field is for and what variation looks like in a
finished level.
"""
from __future__ import annotations

from typing import Optional, Sequence

from bgate_core.autotile import E as BIT_E
from bgate_core.autotile import N as BIT_N
from bgate_core.autotile import NE as BIT_NE
from bgate_core.autotile import NW as BIT_NW
from bgate_core.autotile import S as BIT_S
from bgate_core.autotile import SE as BIT_SE
from bgate_core.autotile import SW as BIT_SW
from bgate_core.autotile import W as BIT_W

#: A zone is read AT THE BOUNDARY, not inset from it — and that had to be
#: measured to be believed. An inset probe (10% in) read a tile whose western
#: pixel column is grass as pure interior stone, because the transition strip
#: is only two pixels wide; the mask table would then have put an interior
#: sprite against every western wall, and the seam check caught the detector
#: rather than the art. Sides read the outermost rows/columns, corners the
#: literal corner block, so detection and seam_report look at the same pixels
#: and a disagreement between them means something.
_EDGE_DEPTH = 2

_ZONE_BIT = {"N": BIT_N, "E": BIT_E, "S": BIT_S, "W": BIT_W,
             "NE": BIT_NE, "SE": BIT_SE, "SW": BIT_SW, "NW": BIT_NW}
_SIDES = ("N", "E", "S", "W")
_CORNERS = ("NE", "SE", "SW", "NW")


def _zone_pixels(img, ox: int, oy: int, tw: int, th: int, zone: str,
                 depth: int) -> list:
    """Every pixel of one boundary zone of the tile at (ox, oy)."""
    corner = max(2, min(tw, th) // 5)
    spans = {
        "N": [(ox + x, oy + d) for x in range(tw) for d in range(depth)],
        "S": [(ox + x, oy + th - 1 - d) for x in range(tw) for d in range(depth)],
        "W": [(ox + d, oy + y) for y in range(th) for d in range(depth)],
        "E": [(ox + tw - 1 - d, oy + y) for y in range(th) for d in range(depth)],
        "NW": [(ox + x, oy + y) for x in range(corner) for y in range(corner)],
        "NE": [(ox + tw - 1 - x, oy + y) for x in range(corner) for y in range(corner)],
        "SW": [(ox + x, oy + th - 1 - y) for x in range(corner) for y in range(corner)],
        "SE": [(ox + tw - 1 - x, oy + th - 1 - y)
               for x in range(corner) for y in range(corner)],
    }
    out = []
    for px, py in spans[zone]:
        if 0 <= px < img.width and 0 <= py < img.height:
            r, g, b, a = img.getpixel((px, py))
            if a > 8:
                out.append((r, g, b))
    return out

#: How far apart the two terrain centroids must sit (RGB distance) before the
#: split is believed. Below this the sheet is one terrain with shading, and a
#: mask table built from it is noise wearing a schema.
MIN_SEPARATION = 40.0

#: A zone this close to the midpoint between the two terrains did not really
#: classify; it is reported as low confidence rather than silently trusted.
CONFIDENT_RATIO = 1.15


class MaskError(ValueError):
    """A sheet this module will not guess at."""


def _mean(patch) -> tuple[float, float, float]:
    n = len(patch)
    return (sum(p[0] for p in patch) / n,
            sum(p[1] for p in patch) / n,
            sum(p[2] for p in patch) / n)


def _dist(a: Sequence[float], b: Sequence[float]) -> float:
    return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5


def _sample(img, cx: int, cy: int, probe: int) -> Optional[tuple[float, float, float]]:
    """Mean opaque colour of a probe box, or None where it is transparent."""
    half = max(1, probe // 2)
    px = []
    for y in range(cy - half, cy + half + 1):
        for x in range(cx - half, cx + half + 1):
            if 0 <= x < img.width and 0 <= y < img.height:
                r, g, b, a = img.getpixel((x, y))
                if a > 8:
                    px.append((r, g, b))
    return _mean(px) if px else None


def _two_means(points: list[tuple[float, float, float]],
               rounds: int = 12) -> tuple[tuple, tuple]:
    """Two colour centroids. Seeded from the most distant pair, so the split
    starts on the real terrains rather than wherever the list happened to."""
    far_a, far_b, best = points[0], points[-1], -1.0
    for i, a in enumerate(points):
        for b in points[i + 1:]:
            d = _dist(a, b)
            if d > best:
                far_a, far_b, best = a, b, d
    ca, cb = far_a, far_b
    for _ in range(rounds):
        group_a = [p for p in points if _dist(p, ca) <= _dist(p, cb)]
        group_b = [p for p in points if _dist(p, ca) > _dist(p, cb)]
        if not group_a or not group_b:
            break
        new_a, new_b = _mean(group_a), _mean(group_b)
        if new_a == ca and new_b == cb:
            break
        ca, cb = new_a, new_b
    return ca, cb


def detect(image, *, tile_size: tuple[int, int], bits: int = 4,
           probe: int = 6) -> dict:
    """Read a two-terrain sheet into per-terrain mask tables.

    Returns ``{ok, grid, terrains: {a|b: {colour, table, alts, pure}},
    tiles: [...], reason?}``. ``table`` is ready for
    ``Terrain.from_table(source, table, bits=bits)``; ``alts`` holds the extra
    tiles that answered a mask somebody else already answered.

    BOTH terrains come back, because a grass/stone sheet genuinely contains two
    autotile sets sharing one atlas — grass that meets stone, and stone that
    meets grass — and a game wants whichever it is painting with.
    """
    img = image.convert("RGBA")
    tw, th = int(tile_size[0]), int(tile_size[1])
    if tw < 4 or th < 4:
        raise MaskError(f"tile size {tw}x{th} is too small to sample")
    cols, rows = img.width // tw, img.height // th
    if cols < 1 or rows < 1:
        raise MaskError(
            f"a {img.width}x{img.height} sheet holds no {tw}x{th} tiles")

    bodies: dict[tuple[int, int], tuple[float, float, float]] = {}
    for ty in range(rows):
        for tx in range(cols):
            hit = _sample(img, tx * tw + tw // 2, ty * th + th // 2, probe)
            if hit is not None:
                bodies[(tx, ty)] = hit
    if len(bodies) < 2:
        return {"ok": False, "grid": [cols, rows],
                "reason": "fewer than two tiles carry any opaque pixels"}

    ca, cb = _two_means(list(bodies.values()))
    separation = _dist(ca, cb)
    if separation < MIN_SEPARATION:
        return {
            "ok": False, "grid": [cols, rows],
            "separation": round(separation, 1),
            "reason": f"the sheet's colours do not separate into two terrains "
                      f"(centroids {separation:.0f} apart, need "
                      f"{MIN_SEPARATION:.0f}) — this is one terrain with "
                      "shading, or more than two, and a mask table built from "
                      "it would put the wrong sprite at every seam"}

    def classify(colour) -> tuple[str, float]:
        da, db = _dist(colour, ca), _dist(colour, cb)
        if da <= db:
            return "a", (db / da if da > 0 else 99.0)
        return "b", (da / db if db > 0 else 99.0)

    terrains = {
        "a": {"colour": [round(v) for v in ca], "table": {}, "alts": {},
              "pure": []},
        "b": {"colour": [round(v) for v in cb], "table": {}, "alts": {},
              "pure": []},
    }
    tiles: list[dict] = []
    for (tx, ty), body_colour in sorted(bodies.items(), key=lambda kv: kv[0][::-1]):
        body, body_conf = classify(body_colour)
        mask, worst = 0, 99.0
        zones: dict[str, bool] = {}
        wanted_zones = _SIDES if bits == 4 else _SIDES + _CORNERS
        for zone in wanted_zones:
            pixels = _zone_pixels(img, tx * tw, ty * th, tw, th, zone,
                                  _EDGE_DEPTH)
            if not pixels:                        # transparent zone = not us
                zones[zone] = False
                continue
            # MAJORITY along the whole boundary, not one probe box: an edge is
            # a strip of pixels and a single sample lands wherever it lands.
            votes = [classify(px)[0] for px in pixels]
            same = votes.count(body) * 2 > len(votes)
            share = votes.count(body) / len(votes)
            zones[zone] = same
            worst = min(worst, 1.0 + abs(share - 0.5) * 2)
            if same:
                mask |= _ZONE_BIT[zone]
        entry = {"at": [tx, ty], "body": body, "mask": mask,
                 "confidence": round(min(worst, body_conf), 2),
                 "zones": zones}
        tiles.append(entry)

        bucket = terrains[body]
        full = (1 << 8) - 1 if bits == 8 else 0x0F
        if mask == full:
            bucket["pure"].append([tx, ty])
        if mask in bucket["table"]:
            bucket["alts"].setdefault(mask, []).append([tx, ty])
        else:
            bucket["table"][mask] = (tx, ty)

    return {"ok": True, "grid": [cols, rows],
            "separation": round(separation, 1),
            "terrains": terrains, "tiles": tiles,
            "low_confidence": [t["at"] for t in tiles
                               if t["confidence"] < CONFIDENT_RATIO]}


# ---------------------------------------------------------------------------
# Seams
# ---------------------------------------------------------------------------
#: How much of a shared edge may disagree about WHICH TERRAIN it shows before
#: the join is a defect. A fraction, not a colour distance: measured on real
#: generated terrain, two different PURE STONE tiles differ by ~50 per channel
#: from texture noise alone, so a per-pixel colour test flags the cleanest
#: possible join. What matters is whether the terrain continues across the
#: seam, which survives the noise.
SEAM_DISAGREE_MAX = 0.25


def _edge(img, at: Sequence[int], tile_size: Sequence[int], side: str) -> list:
    tw, th = int(tile_size[0]), int(tile_size[1])
    ox, oy = int(at[0]) * tw, int(at[1]) * th
    if side == "E":
        return [img.getpixel((ox + tw - 1, oy + y)) for y in range(th)]
    if side == "W":
        return [img.getpixel((ox, oy + y)) for y in range(th)]
    if side == "S":
        return [img.getpixel((ox + x, oy + th - 1)) for x in range(tw)]
    return [img.getpixel((ox + x, oy)) for x in range(tw)]


def seam_report(image, table: dict, *, tile_size: tuple[int, int],
                colours: Sequence[Sequence[float]],
                limit: float = SEAM_DISAGREE_MAX) -> dict:
    """Do the tiles autotiling will place NEXT TO EACH OTHER line up?

    NOT a wrap test. Asking whether a tile tiles with ITSELF is the obvious
    check and the wrong one — written first, measured giving nonsense on a real
    terrain set, because a corner piece is never meant to repeat, it is meant to
    meet its neighbours.

    NOT a per-pixel colour test either, and that one had to be measured to be
    believed: two different pure-stone tiles from the same sheet differ by ~50
    per channel on organic texture, so a colour metric reports the cleanest
    possible seam as broken. What is actually being asked is whether the
    TERRAIN continues across the join, so both edges are classified against the
    sheet's two centroids and the disagreement is counted.

    ``colours`` is the pair of terrain centroids from :func:`detect`. Advisory,
    in the shape every other report here uses.
    """
    img = image.convert("RGBA")
    ca, cb = colours[0], colours[1]

    def kind(px):
        if px[3] <= 8:
            return None
        return "a" if _dist(px[:3], ca) <= _dist(px[:3], cb) else "b"

    pairs = {"E": ("W", BIT_E, BIT_W), "S": ("N", BIT_S, BIT_N)}
    findings: list[dict] = []
    worst: dict[str, float] = {}
    for side, (other, bit_self, bit_other) in pairs.items():
        for mask_a, at_a in sorted(table.items()):
            if not mask_a & bit_self:
                continue                      # nothing of ours on that side
            for mask_b, at_b in sorted(table.items()):
                if not mask_b & bit_other:
                    continue
                edge_a = [kind(p) for p in _edge(img, at_a, tile_size, side)]
                edge_b = [kind(p) for p in _edge(img, at_b, tile_size, other)]
                both = [(x, y) for x, y in zip(edge_a, edge_b)
                        if x is not None and y is not None]
                if not both:
                    continue
                disagree = sum(1 for x, y in both if x != y) / len(both)
                key = f"{mask_a}{side}|{mask_b}"
                worst[key] = round(disagree, 3)
                if disagree > limit:
                    findings.append({
                        "kind": "seam_mismatch", "pair": key,
                        "value": round(disagree, 3),
                        "note": f"mask {mask_a} and mask {mask_b} are placed "
                                f"side by side ({side}) but {disagree:.0%} of "
                                "their shared edge disagrees about which "
                                "terrain it shows — the ground does not "
                                "continue across the join"})
    return {"findings": findings, "checked": len(worst),
            "worst": dict(sorted(worst.items(), key=lambda kv: -kv[1])[:6])}


# ---------------------------------------------------------------------------
# Filling the gaps for free
# ---------------------------------------------------------------------------
#: How a mask transforms when the tile is flipped. A west-edge tile mirrored
#: IS an east-edge tile; the pixels already exist and cost nothing.
_FLIP_H = {BIT_E: BIT_W, BIT_W: BIT_E, BIT_NE: BIT_NW, BIT_NW: BIT_NE,
           BIT_SE: BIT_SW, BIT_SW: BIT_SE, BIT_N: BIT_N, BIT_S: BIT_S}
_FLIP_V = {BIT_N: BIT_S, BIT_S: BIT_N, BIT_NE: BIT_SE, BIT_SE: BIT_NE,
           BIT_NW: BIT_SW, BIT_SW: BIT_NW, BIT_E: BIT_E, BIT_W: BIT_W}


def flip_mask(mask: int, how: str) -> int:
    table = _FLIP_H if how == "h" else _FLIP_V
    out = 0
    for bit, moved in table.items():
        if mask & bit:
            out |= moved
    return out


def synthesise(image, table: dict, wanted: Sequence[int], *,
               tile_size: tuple[int, int], rotate: bool = True) -> dict:
    """Fill missing masks by flipping and rotating tiles that exist. Free.

    Returns ``{image, table, made: {mask: (from_mask, how)}, still_missing}``
    with the new tiles appended as extra rows of the same sheet.

    ITERATES TO A FIXPOINT, because a tile made this pass is a legitimate
    source for the next: the first version built each new tile only from the
    ORIGINALS, so a corner reachable by flipping a corner that was itself
    synthesised came back "missing" while its pixels sat in the sheet.

    ROTATION as well as mirroring, because a square tile turned 90 degrees is
    another mask entirely — a north-end cap rotated is an east-end cap — and a
    four-bit set is mostly rotations of four shapes.

    THE CAVEAT IS THE SPRITE MIRROR RULE AGAIN: a flip or a turn is only
    honest where the art has no handedness or gravity. Terrain rarely has
    either; a tile with readable text, an arrow, or a lit side is a different
    tile when turned, and this cannot tell. Everything synthesised is reported
    with what it came from, so a human can look.
    """
    from PIL import Image

    img = image.convert("RGBA")
    tw, th = int(tile_size[0]), int(tile_size[1])
    cols = max(1, img.width // tw)
    square = tw == th
    have = dict(table)
    made: dict[int, tuple[int, str]] = {}
    new_tiles: list = []

    ops = ["h", "v"] + (["r90", "r180", "r270"] if (rotate and square) else [])
    # Fixpoint: keep sweeping while a pass produced anything new.
    progress = True
    while progress:
        progress = False
        for mask in wanted:
            if mask in have:
                continue
            for how in ops:
                source = next((m for m in have if _turn_mask(m, how) == mask),
                              None)
                if source is None:
                    continue
                # Where the source lives: either the original sheet, or a tile
                # this loop already queued.
                queued = next((t for t in new_tiles if t[0] == source), None)
                if queued is not None:
                    cell = queued[1]
                else:
                    sx, sy = have[source]
                    cell = img.crop((sx * tw, sy * th, sx * tw + tw,
                                     sy * th + th))
                new_tiles.append((mask, _turn_image(cell, how), source, how))
                have[mask] = None            # claimed; coordinates set below
                made[mask] = (source, how)
                progress = True
                break

    if not new_tiles:
        return {"image": img, "table": dict(table), "made": {},
                "still_missing": [m for m in wanted if m not in table]}

    extra_rows = (len(new_tiles) + cols - 1) // cols
    out = Image.new("RGBA", (img.width, img.height + extra_rows * th),
                    (0, 0, 0, 0))
    out.alpha_composite(img, (0, 0))
    base_row = img.height // th
    for i, (mask, cell, _source, _how) in enumerate(new_tiles):
        tx, ty = i % cols, base_row + i // cols
        out.alpha_composite(cell, (tx * tw, ty * th))
        have[mask] = (tx, ty)
    return {"image": out, "table": have, "made": made,
            "still_missing": [m for m in wanted if have.get(m) is None]}


def _turn_mask(mask: int, how: str) -> int:
    if how in ("h", "v"):
        return flip_mask(mask, how)
    turns = {"r90": 1, "r180": 2, "r270": 3}[how]
    out = mask
    for _ in range(turns):
        out = _rot90_mask(out)
    return out


#: One 90-degree clockwise turn: N->E->S->W->N, and the corners follow.
_ROT90 = {BIT_N: BIT_E, BIT_E: BIT_S, BIT_S: BIT_W, BIT_W: BIT_N,
          BIT_NE: BIT_SE, BIT_SE: BIT_SW, BIT_SW: BIT_NW, BIT_NW: BIT_NE}


def _rot90_mask(mask: int) -> int:
    out = 0
    for bit, moved in _ROT90.items():
        if mask & bit:
            out |= moved
    return out


def _turn_image(cell, how: str):
    from PIL import Image

    if how == "h":
        return cell.transpose(Image.FLIP_LEFT_RIGHT)
    if how == "v":
        return cell.transpose(Image.FLIP_TOP_BOTTOM)
    return cell.rotate({"r90": -90, "r180": 180, "r270": 90}[how], expand=False)

# ---------------------------------------------------------------------------
# Compositing — the last free fill
# ---------------------------------------------------------------------------
def composite(image, table: dict, wanted: Sequence[int], *,
              tile_size: tuple[int, int]) -> dict:
    """Build missing masks QUADRANT BY QUADRANT from tiles that exist.

    Flipping and turning cannot reach every shape: a four-bit set whose art
    has no straight corridor piece (N|S, E|W) cannot rotate one into being,
    and a BSP dungeon is mostly corridors — the masks missing after
    :func:`synthesise` are exactly the ones the level needs most.

    But each quadrant of a tile is decided by only the TWO sides that touch
    it: the north-west corner of a tile looks the way it does because of what
    lies north and west. So a missing mask can be assembled from four donors,
    each agreeing with the target on its own two sides. On organic terrain the
    joins are invisible; on anything with strong linework they will not be,
    and every composited tile is reported as composited so a human can look.

    Runs AFTER synthesise, because a whole tile is always better than four
    quarters of other tiles.
    """
    from PIL import Image

    img = image.convert("RGBA")
    tw, th = int(tile_size[0]), int(tile_size[1])
    cols = max(1, img.width // tw)
    hw, hh = tw // 2, th // 2
    have = dict(table)
    built: dict[int, dict] = {}
    new_tiles: list = []

    # (quadrant box, the two side bits that decide it)
    quads = (
        ((0, 0, hw, hh), (BIT_N, BIT_W)),
        ((hw, 0, tw, hh), (BIT_N, BIT_E)),
        ((0, hh, hw, th), (BIT_S, BIT_W)),
        ((hw, hh, tw, th), (BIT_S, BIT_E)),
    )

    def cell_for(mask: int):
        queued = next((n for n in new_tiles if n[0] == mask), None)
        if queued is not None:
            return queued[1]
        at = have.get(mask)
        if at is None:
            return None
        return img.crop((at[0] * tw, at[1] * th, at[0] * tw + tw,
                         at[1] * th + th))

    for mask in wanted:
        if have.get(mask) is not None:
            continue
        patch = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        donors: list[int] = []
        ok = True
        for box, bits_pair in quads:
            donor = next(
                (m for m in sorted(have)
                 if have.get(m) is not None
                 and all((m & b) == (mask & b) for b in bits_pair)),
                None)
            if donor is None:
                ok = False
                break
            src = cell_for(donor)
            if src is None:
                ok = False
                break
            patch.paste(src.crop(box), (box[0], box[1]))
            donors.append(donor)
        if not ok:
            continue
        new_tiles.append((mask, patch, donors))
        have[mask] = None
        built[mask] = {"from": donors}

    if not new_tiles:
        return {"image": img, "table": dict(table), "built": {},
                "still_missing": [m for m in wanted
                                  if table.get(m) is None]}

    extra_rows = (len(new_tiles) + cols - 1) // cols
    out = Image.new("RGBA", (img.width, img.height + extra_rows * th),
                    (0, 0, 0, 0))
    out.alpha_composite(img, (0, 0))
    base_row = img.height // th
    for i, (mask, patch, _donors) in enumerate(new_tiles):
        tx, ty = i % cols, base_row + i // cols
        out.alpha_composite(patch, (tx * tw, ty * th))
        have[mask] = (tx, ty)
    return {"image": out, "table": have, "built": built,
            "still_missing": [m for m in wanted if have.get(m) is None]}

def repack(image, table: dict, wanted: Sequence[int], *,
           tile_size: tuple[int, int], columns: int = 4) -> dict:
    """Lay the tiles out in CANONICAL MASK ORDER: mask m at (m%cols, m//cols).

    A detected table maps mask -> wherever the model happened to draw it, and
    that is unusable to anything but us: `autotile.Terrain.grid16`/`blob47`,
    Godot's own terrain editor, and every other tool that reads a tileset all
    assume row-major mask order from an origin. Emitting the atlas in that
    order is what makes the output a STANDARD tileset rather than one only
    this pipeline can read — and it is why level_generate's built-in layouts
    work against it without being handed a table.

    Returns ``{image, table}`` with the table now trivially m -> (m%cols,
    m//cols).
    """
    from PIL import Image

    img = image.convert("RGBA")
    tw, th = int(tile_size[0]), int(tile_size[1])
    masks = [m for m in wanted if table.get(m) is not None]
    rows = (len(wanted) + columns - 1) // columns
    out = Image.new("RGBA", (columns * tw, rows * th), (0, 0, 0, 0))
    packed: dict[int, tuple[int, int]] = {}
    for i, mask in enumerate(wanted):
        at = table.get(mask)
        if at is None:
            continue
        src = img.crop((at[0] * tw, at[1] * th, at[0] * tw + tw,
                        at[1] * th + th))
        tx, ty = i % columns, i // columns
        out.alpha_composite(src, (tx * tw, ty * th))
        packed[mask] = (tx, ty)
    return {"image": out, "table": packed, "columns": columns,
            "count": len(masks)}
