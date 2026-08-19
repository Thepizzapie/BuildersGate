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
              tile_size: tuple[int, int],
              colours: Optional[Sequence[Sequence[float]]] = None) -> dict:
    """Build missing masks by CARVING VOID into a full floor tile.

    Flipping and turning cannot reach every shape: a four-bit set whose art
    has no straight corridor piece (N|S, E|W) cannot rotate one into being,
    and a BSP dungeon is mostly corridors — the masks missing after
    :func:`synthesise` are exactly the ones the level needs most.

    THE OBVIOUS CONSTRUCTION IS WRONG AND WAS SHIPPED ONCE. Taking each
    quadrant from a different donor cuts the tile across its middle, which is
    precisely where a corridor's floor has to run: the result was two
    disconnected halves with a seam between them, and every corridor in the
    level was drawn out of them. It reads as ridges and beading and a human
    spotted it immediately.

    So the floor is never cut. Start from the FULL tile — continuous floor,
    real art — and for each side the target wants void, carve in that donor's
    OWN void pixels, restricted to the half of the tile that side owns. The
    middle stays whole, the edges are real generated art, and nothing is
    stitched through the part that has to be continuous.

    ``colours`` is the terrain-centroid pair from :func:`detect`; without it
    the darker terrain is assumed to be the void.
    """
    from PIL import Image

    img = image.convert("RGBA")
    tw, th = int(tile_size[0]), int(tile_size[1])
    cols = max(1, img.width // tw)
    have = dict(table)
    built: dict[int, dict] = {}
    new_tiles: list = []

    full = BIT_N | BIT_E | BIT_S | BIT_W
    base_at = have.get(full)
    if base_at is None:
        return {"image": img, "table": dict(table), "built": {},
                "still_missing": [m for m in wanted if table.get(m) is None],
                "reason": "no full-floor tile to carve from"}

    def cell(at):
        return img.crop((at[0] * tw, at[1] * th, at[0] * tw + tw,
                         at[1] * th + th))

    # Which pixels of a tile are VOID (the terrain the target is NOT made of).
    if colours is not None:
        ca, cb = colours[0], colours[1]
        base_px = list(cell(base_at).convert("RGB").getdata())
        base_mean = _mean(base_px)
        floor_c = ca if _dist(base_mean, ca) <= _dist(base_mean, cb) else cb
        void_c = cb if floor_c is ca else ca

        def is_void(px):
            return _dist(px[:3], void_c) < _dist(px[:3], floor_c)
    else:
        def is_void(px):
            return (px[0] + px[1] + px[2]) / 3.0 < 60

    #: Which half of the tile a side owns — a donor's void is only trusted
    #: where that side can actually see it.
    halves = {
        BIT_N: (0, 0, tw, th // 2), BIT_S: (0, th // 2, tw, th),
        BIT_W: (0, 0, tw // 2, th), BIT_E: (tw // 2, 0, tw, th),
    }

    for mask in wanted:
        if have.get(mask) is not None:
            continue
        patch = cell(base_at).copy()
        px = patch.load()
        donors: list[int] = []
        ok = True
        for bit, (x0, y0, x1, y1) in halves.items():
            if mask & bit:
                continue                       # this side is floor: leave it
            # a donor that ALSO has void on this side, closest otherwise
            donor = min(
                (m for m in have
                 if have.get(m) is not None and not (m & bit)),
                key=lambda m: bin((m ^ mask) & full).count("1"),
                default=None)
            if donor is None:
                ok = False
                break
            donors.append(donor)
            dcell = cell(have[donor])
            dpx = dcell.load()
            for y in range(y0, y1):
                for x in range(x0, x1):
                    if is_void(dpx[x, y]):
                        px[x, y] = dpx[x, y]
        if not ok:
            continue
        new_tiles.append((mask, patch, donors))
        have[mask] = None
        built[mask] = {"carved_from": donors}

    if not new_tiles:
        return {"image": img, "table": dict(table), "built": {},
                "still_missing": [m for m in wanted if table.get(m) is None]}

    extra_rows = (len(new_tiles) + cols - 1) // cols
    out = Image.new("RGBA", (img.width, img.height + extra_rows * th),
                    (0, 0, 0, 0))
    out.alpha_composite(img, (0, 0))
    base_row = img.height // th
    for i, (mask, patch, _d) in enumerate(new_tiles):
        tx, ty = i % cols, base_row + i // cols
        out.paste(patch, (tx * tw, ty * th))
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
        out.paste(src, (tx * tw, ty * th))
        packed[mask] = (tx, ty)
    return {"image": out, "table": packed, "columns": columns,
            "count": len(masks)}

#: How deep the void reaches in from a tile edge, as a fraction of the tile.
#: Every edge in the set uses the SAME inset — that is what makes tiles line
#: up, and generated art does not provide it: measured on a real Retro
#: Diffusion terrain sheet, tiles with one void side ranged from 61% to 82%
#: floor and tiles with two ranged 28% to 73%, so the floor boundary jumped in
#: and out between neighbours. That reads as bulges along every room edge and
#: as a corridor thinner than the corridor beside it.
#:
#: It is a LIP, not a wall. 0.28 was the measured average of the model's own
#: edge depth, and using it drew the boundary TWICE — once as the floor tile's
#: carved edge and again as the wall layer occupying the neighbouring cell. A
#: two-cell corridor then lost over half its width and rendered as a seam in the
#: rock. The wall layer owns the wall; the floor only needs enough edge to stop
#: reading as a poster pasted over stone.
EDGE_INSET = 0.1

#: How far a COLLIDER reaches in, which is deliberately NOT `EDGE_INSET`.
#: They were one constant, and shrinking the visual lip to 0.1 silently shrank
#: every collider with it — the player would have walked most of a tile into the
#: stone before being stopped. What the art draws and where the game stops are
#: two different questions and they get two different numbers.
COLLIDER_INSET = 0.28


def normalise_edges(image, table: dict, wanted: Sequence[int], *,
                    tile_size: tuple[int, int],
                    colours: Sequence[Sequence[float]],
                    inset: float = EDGE_INSET, clear: bool = False) -> dict:
    """Rebuild every mask with a CONSISTENT edge inset, keeping the art.

    The model's texture is kept — floor from its own full tile, void from its
    own void regions — but the GEOMETRY is imposed: void reaches exactly
    ``inset`` of the way in from each side the mask says is void, on every
    tile. That is the property a hand-made autotile set has by construction
    and a generated one does not, and without it neighbours disagree about
    where the floor stops.

    ``clear`` carves the void band to TRANSPARENT instead of painting the void
    terrain into it, and WHICH ONE IS RIGHT depends entirely on what is behind
    the floor layer — a question no measurement of the sheet can answer, which
    is why it took a screenshot of the running game to settle:

      * Behind a wall FILL (``levelgen.layers(wall_fill=True)``, the default),
        leave it opaque. The band IS the wall face, the fill supplies the rock
        behind it, and the two agree. Carving here punches a hole to the clear
        colour and traces a light seam around every room.
      * Over a bare canvas or a wall RING, carve. An opaque band paints over
        whatever the layer beneath drew, which is what made every room edge read
        as a protrusion and every narrow corridor as a black crack.

    Carving also defringes the cut, because a clean rectangle is not a clean
    edge: the model draws the transition as a ramp and its anti-aliased tail
    survives the band.

    What this deliberately gives up: the ragged, per-tile organic outlines the
    model drew. They are lovely in isolation and they are exactly what does
    not tile. The texture inside the band is still the model's own, so the
    result is not a flat stencil.
    """
    from PIL import Image

    img = image.convert("RGBA")
    tw, th = int(tile_size[0]), int(tile_size[1])
    full = BIT_N | BIT_E | BIT_S | BIT_W
    base_at = table.get(full)
    if base_at is None:
        return {"ok": False, "reason": "no full-floor tile to build from"}

    ca, cb = colours[0], colours[1]

    def cell(at):
        return img.crop((at[0] * tw, at[1] * th, at[0] * tw + tw,
                         at[1] * th + th))

    base = cell(base_at).convert("RGBA")
    base_mean = _mean(list(base.convert("RGB").getdata()))
    floor_c = ca if _dist(base_mean, ca) <= _dist(base_mean, cb) else cb
    void_c = cb if floor_c is ca else ca

    # A patch of real VOID texture, taken from wherever the sheet has the most
    # of it, so the band is the model's own darkness rather than flat black.
    best, best_count = None, -1
    for mask, at in table.items():
        if at is None:
            continue
        c = cell(at)
        px = list(c.convert("RGB").getdata())
        n = sum(1 for p in px if _dist(p, void_c) < _dist(p, floor_c))
        if n > best_count:
            best, best_count = c, n
    void_tile = best

    bw, bh = max(1, round(tw * inset)), max(1, round(th * inset))
    # FOUR-BIT OR EIGHT, inferred from what was asked for. In a 4-bit set the
    # corner bits are simply absent, so carving "the corner bit is clear" would
    # notch all four corners out of every tile — including the fully open one.
    _CORNER_BITS = BIT_NE | BIT_SE | BIT_SW | BIT_NW
    eight_bit = any(m & _CORNER_BITS for m in wanted)
    out_tiles: dict[int, Image.Image] = {}
    for mask in wanted:
        tile = base.copy()
        vpx = void_tile.load()
        tpx = tile.load()
        bands = []
        if not mask & BIT_N:
            bands.append((0, 0, tw, bh))
        if not mask & BIT_S:
            bands.append((0, th - bh, tw, th))
        if not mask & BIT_W:
            bands.append((0, 0, bw, th))
        if not mask & BIT_E:
            bands.append((tw - bw, 0, tw, th))
        # THE DIAGONAL CASES, and the reason a 16-tile set is not enough.
        # A 4-bit mask cannot say "floor to the north and east, but void at the
        # north-east corner", so at every step in a room's outline there is no
        # tile to draw and the shadow band along the wall simply BREAKS. The
        # corner is a nibble out of the tile and pure geometry, so it does not
        # need new art — which is why a 47-tile set can be built from a 16-tile
        # generation instead of asking the model for corners it draws badly.
        if eight_bit:
            for bit, sides, box in (
                    (BIT_NE, BIT_N | BIT_E, (tw - bw, 0, tw, bh)),
                    (BIT_SE, BIT_S | BIT_E, (tw - bw, th - bh, tw, th)),
                    (BIT_SW, BIT_S | BIT_W, (0, th - bh, bw, th)),
                    (BIT_NW, BIT_N | BIT_W, (0, 0, bw, bh))):
                if (mask & sides) == sides and not mask & bit:
                    bands.append(box)
        for x0, y0, x1, y1 in bands:
            for y in range(y0, y1):
                for x in range(x0, x1):
                    if clear:
                        tpx[x, y] = (0, 0, 0, 0)
                        continue
                    src = vpx[x, y]
                    # only paint where the donor is genuinely void, so the
                    # band keeps its texture instead of becoming a rectangle
                    if _dist(src[:3], void_c) < _dist(src[:3], floor_c):
                        tpx[x, y] = src
                    else:
                        tpx[x, y] = (int(void_c[0]), int(void_c[1]),
                                     int(void_c[2]), 255)
        if clear:
            # DEFRINGE THE CUT. A clean rectangle is not a clean edge: the
            # model draws the floor/void transition as a ramp, so carving the
            # band leaves the anti-aliased tail of it opaque just inside the
            # cut. On a finished map that tail is a dark line, and the wall's
            # own mortar showing through the hole beside it is a light one —
            # together they read as a seam traced around every room. Anything
            # still classifying as void is part of the void.
            for y in range(th):
                for x in range(tw):
                    px = tpx[x, y]
                    if px[3] and _dist(px[:3], void_c) < _dist(px[:3], floor_c):
                        tpx[x, y] = (0, 0, 0, 0)
        out_tiles[mask] = tile

    cols = 4 if len(wanted) <= 16 else 8   # grid16 is 4 wide, blob47 is 8
    rows = (len(wanted) + cols - 1) // cols
    sheet = Image.new("RGBA", (cols * tw, rows * th), (0, 0, 0, 0))
    packed: dict[int, tuple[int, int]] = {}
    for i, mask in enumerate(wanted):
        tx, ty = i % cols, i // cols
        sheet.paste(out_tiles[mask], (tx * tw, ty * th))
        packed[mask] = (tx, ty)
    return {"ok": True, "image": sheet, "table": packed, "inset": inset}

# ---------------------------------------------------------------------------
# Collision
# ---------------------------------------------------------------------------
def collision_polygons(masks: Sequence[int], *, tile_size: tuple[int, int],
                       inset: float = COLLIDER_INSET,
                       solid_void: bool = True, full: bool = False) -> dict:
    """A collision polygon per mask, derived from a KNOWN inset.

    Not traced from pixels. The tiles were rebuilt with one known inset by
    :func:`normalise_edges`, so the walkable region of every tile is a
    rectangle known exactly — tracing the art would rediscover that rectangle
    with jitter and hand Godot a fifty-point polygon per tile for nothing.

    ``solid_void=True`` gives the collider the VOID (what stops the player),
    which is what a top-down dungeon wants: the floor is walkable and the dark
    is wall. The void of a tile is up to four edge bands, so this returns one
    polygon per band rather than trying to express a ring as a single convex
    shape — Godot takes several polygons per tile and each band is convex.

    ``full`` gives every mask one whole-tile collider instead of edge bands. It
    is what a WALL layer wants: when the wall paints every non-floor cell (see
    ``levelgen.layers(wall_fill=True)``) the wall tile is solid all the way
    across, and the floor layer beside it needs no collider at all.

    Coordinates are centred on the tile, which is the space Godot's tile
    collision uses: (0, 0) is the middle, not the corner.
    """
    tw, th = int(tile_size[0]), int(tile_size[1])
    bw, bh = max(1, round(tw * inset)), max(1, round(th * inset))
    hw, hh = tw / 2.0, th / 2.0

    def rect(x0, y0, x1, y1):
        return [(round(x0 - hw, 2), round(y0 - hh, 2)),
                (round(x1 - hw, 2), round(y0 - hh, 2)),
                (round(x1 - hw, 2), round(y1 - hh, 2)),
                (round(x0 - hw, 2), round(y1 - hh, 2))]

    out: dict[int, list] = {}
    for mask in masks:
        bands = []
        if full:
            out[mask] = [rect(0, 0, tw, th)]
            continue
        if solid_void:
            if not mask & BIT_N:
                bands.append(rect(0, 0, tw, bh))
            if not mask & BIT_S:
                bands.append(rect(0, th - bh, tw, th))
            if not mask & BIT_W:
                bands.append(rect(0, 0, bw, th))
            if not mask & BIT_E:
                bands.append(rect(tw - bw, 0, tw, th))
        else:
            x0 = bw if not mask & BIT_W else 0
            x1 = tw - bw if not mask & BIT_E else tw
            y0 = bh if not mask & BIT_N else 0
            y1 = th - bh if not mask & BIT_S else th
            if x1 > x0 and y1 > y0:
                bands.append(rect(x0, y0, x1, y1))
        out[mask] = bands
    return out


# ---------------------------------------------------------------------------
# Isometric — the diamond
# ---------------------------------------------------------------------------
# A SQUARE CARVE CANNOT SERVE A DIAMOND, which is why `tileset_generate`
# refused isometric projects rather than shipping a plausible-looking set. In
# an isometric tilemap the tile's art is a diamond inscribed in the tile rect,
# its corners transparent by construction, and the four EDGES of that diamond
# are the four cell neighbours — not the four sides of the rect. Carving square
# bands would put the shadow on the rect's edges, i.e. across the diamond's
# corners, and every room outline would read as a grid of notches.
#
# Which edge is which neighbour comes from the projection this module's own
# `tilemap.cell_center` implements, not from a guess. In DIAMOND_DOWN,
# screen = ((x - y) * w/2, (x + y) * h/2), so from any cell:
#
#     -y (N) renders UP-RIGHT      +x (E) renders DOWN-RIGHT
#     -x (W) renders UP-LEFT       +y (S) renders DOWN-LEFT
#
# and the diamond's four edges take those four names in that order.
#
# In normalised tile coordinates — u across, v down, both -1..1 from the tile's
# centre — the diamond is |u| + |v| <= 1. That single expression carries
# everything: |u| + |v| > 1 is outside the tile's art entirely, the value runs
# 0 at the centre to 1 at the edge so a band is a threshold on it, and the
# SIGNS of u and v name which edge a pixel belongs to. No trigonometry, no
# per-edge line equations, and it is exact at any tile size or aspect.

#: The diamond's edge band, which is DEEPER than the square set's EDGE_INSET
#: and has to be. A square tile's lip is read against the wall layer filling
#: the cell beside it; an isometric floor tile has nothing behind it but the
#: background, so its own rim is the only thing that says where the floor
#: stops. At 0.1 a generated level rendered as flat cut-out slabs with no
#: edge at all — compared side by side at 0.1 / 0.18 / 0.26, this is where the
#: edge reads without the band starting to eat the tile.
ISO_EDGE_INSET = 0.18

#: How far the band is darkened from the floor's own material, as a multiplier.
#: Not a separate generation: see the derivation note in `tileset_generate`.
ISO_BAND_LEVEL = 0.55

#: (bit, sign of u, sign of v) for each diamond edge. `0` in a sign slot means
#: "either", which only the vertices hit.
_DIAMOND_EDGES = (
    (BIT_N, +1, -1),      # up-right
    (BIT_E, +1, +1),      # down-right
    (BIT_S, -1, +1),      # down-left
    (BIT_W, -1, -1),      # up-left
)


def diamond_edge_for(u: float, v: float) -> int:
    """Which neighbour's edge a point in the tile belongs to."""
    su = 1 if u >= 0 else -1
    sv = 1 if v >= 0 else -1
    for bit, want_u, want_v in _DIAMOND_EDGES:
        if su == want_u and sv == want_v:
            return bit
    return BIT_N                                     # unreachable in practice


def diamond_polygon(tile_size: tuple[int, int], *,
                    inset: float = 0.0) -> list[tuple[float, float]]:
    """The diamond as a Godot collision polygon, centred on the tile.

    The walkable region of an isometric floor tile IS the diamond, so this is
    four points rather than the square path's edge bands. Centred, because
    that is the space Godot's tile collision uses.
    """
    tw, th = float(tile_size[0]), float(tile_size[1])
    k = max(0.0, 1.0 - float(inset))
    hw, hh = tw / 2.0 * k, th / 2.0 * k
    return [(0.0, -hh), (hw, 0.0), (0.0, hh), (-hw, 0.0)]


def diamond_tiles(floor_tile, void_tile, wanted: Sequence[int], *,
                  tile_size: tuple[int, int], inset: float = ISO_EDGE_INSET,
                  clear: bool = False) -> dict:
    """Build every isometric mask tile from a floor and a void texture.

    The counterpart of :func:`normalise_edges` for the diamond, and like it
    the geometry is IMPOSED rather than detected: coverage cannot be partial
    because every tile is constructed, and neighbours cannot disagree about
    where the floor stops because one number decides it for all of them.

    ``clear`` carves the band to transparent instead of painting the void
    texture into it — the same question the square path documents, with the
    same answer: opaque when a wall layer fills behind it, carved over a bare
    canvas. Outside the diamond is ALWAYS transparent, which is not a choice;
    it is what makes the tile a diamond.

    Returns ``{ok, image, table, inset}`` — the sheet packed four to a row in
    canonical mask order, so it is a tileset any consumer can read.
    """
    from PIL import Image

    tw, th = int(tile_size[0]), int(tile_size[1])
    if tw < 2 or th < 2:
        return {"ok": False, "reason": f"a {tw}x{th} tile has no diamond in it"}
    floor = floor_tile.convert("RGBA").resize((tw, th), Image.NEAREST)
    void = void_tile.convert("RGBA").resize((tw, th), Image.NEAREST)
    fpx, vpx = floor.load(), void.load()

    masks = list(wanted)
    tiles: dict[int, "Image.Image"] = {}
    for mask in masks:
        tile = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        tpx = tile.load()
        for py in range(th):
            # sample at the pixel's CENTRE: a pixel is a square of area, and
            # testing its top-left corner biases every edge half a pixel up
            # and left, which at a 32px tile is a visible stair.
            v = ((py + 0.5) - th / 2.0) / (th / 2.0)
            for px in range(tw):
                u = ((px + 0.5) - tw / 2.0) / (tw / 2.0)
                s = abs(u) + abs(v)
                if s > 1.0:
                    continue                    # outside the diamond
                if s > 1.0 - inset and not mask & diamond_edge_for(u, v):
                    if clear:
                        continue
                    tpx[px, py] = vpx[px, py]
                else:
                    tpx[px, py] = fpx[px, py]
        tiles[mask] = tile

    cols = 4
    rows = (len(masks) + cols - 1) // cols
    sheet = Image.new("RGBA", (cols * tw, rows * th), (0, 0, 0, 0))
    table: dict[int, tuple[int, int]] = {}
    for i, mask in enumerate(masks):
        tx, ty = i % cols, i // cols
        sheet.paste(tiles[mask], (tx * tw, ty * th))
        table[mask] = (tx, ty)
    return {"ok": True, "image": sheet, "table": table, "inset": inset}


#: How the two visible side faces of a block are lit, as multipliers on the
#: material. Light comes from the upper LEFT, which is the convention the
#: floor band already implies and the one nearly every isometric game uses:
#: the face turned south-west catches some of it, the face turned south-east
#: catches least. Equal faces read as a flat hexagon rather than a cube — the
#: whole illusion is that two planes at different angles are different values.
ISO_FACE_LEFT = 0.72
ISO_FACE_RIGHT = 0.48


def iso_block(top, material, *, tile_size: tuple[int, int], lift: int,
              faces: tuple[float, float] = (ISO_FACE_LEFT, ISO_FACE_RIGHT)
              ) -> dict:
    """A raised isometric cell: a diamond top with two side faces below it.

    THE ONE PRIMITIVE BEHIND BOTH WALLS AND ELEVATION, because in an
    isometric world they are the same object seen twice: a wall is a cell
    raised by `lift` that you may not enter, and a terrace is a cell raised by
    `lift` that you may. Building them from one function is not tidiness — it
    is why a wall and the ledge beside it cannot end up drawn with different
    geometry or lit from different directions.

    The art is ``(tw, th + lift)``: the top diamond occupies the first ``th``
    rows and the faces hang below it, each column falling from the diamond's
    own lower boundary so the silhouette is exact at any tile aspect. The
    faces are the material again rather than flat colour, sampled wrapped so
    the stone continues down the wall instead of stretching.

    Returns ``{image, origin, size}``. ``origin`` is the ``texture_origin``
    Godot needs — ``(0, lift // 2)`` — which puts the art's BOTTOM EDGE on the
    diamond's bottom vertex and therefore the block's TOP face exactly `lift`
    above the cell's floor plane. That rule is `tilemap.tile_rect`'s, stated
    from the other end; get its sign wrong and every wall sinks into the floor.
    """
    from PIL import Image

    tw, th = int(tile_size[0]), int(tile_size[1])
    lift = int(lift)
    if lift < 1:
        return {"ok": False, "reason": f"a {lift}px lift is not a block"}
    top = top.convert("RGBA").resize((tw, th), Image.NEAREST)
    mat = material.convert("RGBA").resize((tw, th), Image.NEAREST)
    left_k, right_k = faces

    out = Image.new("RGBA", (tw, th + lift), (0, 0, 0, 0))
    out.paste(top, (0, 0))
    opx, mpx = out.load(), mat.load()
    for px in range(tw):
        u = ((px + 0.5) - tw / 2.0) / (tw / 2.0)
        if abs(u) > 1.0:
            continue
        # the diamond's lower boundary in this column: |u| + |v| = 1
        y_low = (th / 2.0) * (2.0 - abs(u))
        k = left_k if u < 0 else right_k
        # SAME CENTRE-SAMPLING RULE AS THE DIAMOND, and it has to be: taking
        # int(y_low) as the first row truncated the fractional boundary, and
        # at the centre column — where the diamond's bottom vertex sits — that
        # left the block's lowest pixel row empty. A one-pixel notch at the
        # tip of every wall, which tiles into a dotted line along every ledge.
        for py in range(int(y_low), th + lift):
            if not (y_low <= py + 0.5 <= y_low + lift):
                continue
            r, g, b, a = mpx[px, py % th]
            if a:
                opx[px, py] = (int(r * k), int(g * k), int(b * k), 255)
    return {"ok": True, "image": out, "origin": (0, lift // 2),
            "size": (tw, th + lift)}


def crop_tile(sheet, at: Sequence[int], tile_size: tuple[int, int]):
    """One tile out of a packed sheet, by its (tx, ty) atlas coordinate."""
    tw, th = int(tile_size[0]), int(tile_size[1])
    tx, ty = int(at[0]), int(at[1])
    return sheet.crop((tx * tw, ty * th, tx * tw + tw, ty * th + th))


def iso_ramp(material, facing: str, *, tile_size: tuple[int, int], lift: int,
             faces: tuple[float, float] = (ISO_FACE_LEFT, ISO_FACE_RIGHT)
             ) -> dict:
    """A raised cell whose top face SLOPES down toward one neighbour.

    The piece that turns elevation from scenery into terrain: a block is a
    step nothing can climb, and this is the same cell with its top tilted so
    a walker arrives at the height of the neighbour it faces.

    The tilt is computed in CELL space, not screen space, which is the only
    way it can line up with the neighbour it serves. Screen u and v carry the
    cell axes as ``a = (u + v) / 2`` along +x (east) and ``b = (v - u) / 2``
    along +y (south) — read straight off the DIAMOND_DOWN projection this
    module already carves edges with — so a ramp facing east falls with `a`
    and one facing south falls with `b`, exactly and at any tile aspect.

    ``facing`` is the direction the ramp descends: the neighbour that way sits
    one level lower, which is the same field `levelgen.step_ok` reads when it
    decides whether a walker may cross. The art and the walk rule cannot
    disagree because they are the same number.
    """
    from PIL import Image

    tw, th = int(tile_size[0]), int(tile_size[1])
    lift = int(lift)
    facing = str(facing or "").strip().lower()
    if facing not in ("n", "e", "s", "w"):
        return {"ok": False, "reason": f"{facing!r} is not a ramp direction"}
    if lift < 1:
        return {"ok": False, "reason": f"a {lift}px lift is not a ramp"}
    mat = material.convert("RGBA").resize((tw, th), Image.NEAREST)
    mpx = mat.load()
    left_k, right_k = faces

    out = Image.new("RGBA", (tw, th + lift), (0, 0, 0, 0))
    opx = out.load()
    # SUPERSAMPLED DOWN THE COLUMN, because this maps source pixels FORWARD
    # to shifted destinations and neighbouring rows do not shift by the same
    # whole number. Stepping one source row at a time left a destination row
    # untouched wherever the shift jumped — a checkerboard of holes across
    # every sloped face, which is a dither pattern at a glance and a leaking
    # surface in the game.
    SUB = 4
    for px in range(tw):
        u = ((px + 0.5) - tw / 2.0) / (tw / 2.0)
        for step in range(th * SUB):
            fy = (step + 0.5) / SUB
            v = (fy - th / 2.0) / (th / 2.0)
            if abs(u) + abs(v) > 1.0:
                continue
            # cell-space position within the tile, both -0.5..0.5
            a, b = (u + v) / 2.0, (v - u) / 2.0
            drop = {"e": 0.5 + a, "w": 0.5 - a,
                    "s": 0.5 + b, "n": 0.5 - b}[facing]
            dest = int(fy + lift * (1.0 - drop))
            if not (0 <= dest < th + lift):
                continue
            r, g, b_, al = mpx[px, min(th - 1, int(fy))]
            if al:
                opx[px, dest] = (r, g, b_, 255)
    # the skirt: everything under the sloped top, so the ramp is solid rather
    # than a floating ribbon. Walk each column down from its lowest opaque
    # pixel to the block's base.
    for px in range(tw):
        u = ((px + 0.5) - tw / 2.0) / (tw / 2.0)
        if abs(u) > 1.0:
            continue
        low = max((py for py in range(th + lift) if opx[px, py][3]), default=-1)
        if low < 0:
            continue
        base = int((th / 2.0) * (2.0 - abs(u))) + lift
        k = left_k if u < 0 else right_k
        for py in range(low + 1, min(base, th + lift)):
            r, g, b_, al = mpx[px, py % th]
            if al:
                opx[px, py] = (int(r * k), int(g * k), int(b_ * k), 255)
    return {"ok": True, "image": out, "origin": (0, lift // 2),
            "size": (tw, th + lift), "facing": facing}


#: How deep a wall PANEL is, as a fraction of the cell. A wall in a building
#: is a plane, not a cube: it stands ON the boundary between two cells and the
#: floor runs up to both of its faces. Drawing one cell of wall as a full
#: block eats the whole cell, which is why a floor with one-cell partitions
#: renders as a maze of corridors instead of rooms with walls between them.
PANEL_DEPTH = 0.34


def iso_panel(material, axis: str, *, tile_size: tuple[int, int], lift: int,
              depth: float = PANEL_DEPTH,
              faces: tuple[float, float] = (ISO_FACE_LEFT, ISO_FACE_RIGHT)
              ) -> dict:
    """A thin wall standing along one cell axis, rather than filling the cell.

    ``axis`` is "x" (the wall runs toward the cell's +x/-x neighbours, which
    renders down-right/up-left) or "y" (+y/-y, down-left/up-right), or "post"
    for a junction, which keeps the full diamond so corners and T-joins do not
    leave a notch where two panels meet at different angles.

    The footprint is computed in CELL space for the same reason the ramp's
    slope is: `a = (u + v) / 2` runs along +x and `b = (v - u) / 2` along +y,
    so "thin in y" is `|b| <= depth/2` exactly, at any tile aspect. Everything
    else — the extrusion, the two face values, the origin — is the block's,
    because a panel is a block with a narrower footprint and nothing else
    about it should differ.
    """
    from PIL import Image

    tw, th = int(tile_size[0]), int(tile_size[1])
    lift = int(lift)
    axis = str(axis or "").strip().lower()
    if axis not in ("x", "y", "post"):
        return {"ok": False, "reason": f"{axis!r} is not x, y or post"}
    if lift < 1:
        return {"ok": False, "reason": f"a {lift}px lift is not a wall"}
    mat = material.convert("RGBA").resize((tw, th), Image.NEAREST)
    mpx = mat.load()
    left_k, right_k = faces
    half = max(0.02, float(depth)) / 2.0

    def inside(u, v):
        if abs(u) + abs(v) > 1.0:
            return False
        if axis == "post":
            return True
        a, b = (u + v) / 2.0, (v - u) / 2.0
        return abs(b) <= half if axis == "x" else abs(a) <= half

    out = Image.new("RGBA", (tw, th + lift), (0, 0, 0, 0))
    opx = out.load()
    # the top face, lifted
    for py in range(th):
        v = ((py + 0.5) - th / 2.0) / (th / 2.0)
        for px in range(tw):
            u = ((px + 0.5) - tw / 2.0) / (tw / 2.0)
            if inside(u, v):
                r, g, b_, al = mpx[px, py]
                if al:
                    opx[px, py] = (r, g, b_, 255)
    if lift:
        shifted = Image.new("RGBA", (tw, th + lift), (0, 0, 0, 0))
        shifted.paste(out.crop((0, 0, tw, th)), (0, 0))
        out = shifted
        opx = out.load()
    # the sides: from each column's lowest opaque pixel down to where the
    # footprint's own boundary would sit `lift` lower
    for px in range(tw):
        u = ((px + 0.5) - tw / 2.0) / (tw / 2.0)
        rows = [py for py in range(th) if opx[px, py][3]]
        if not rows:
            continue
        k = left_k if u < 0 else right_k
        for py in range(max(rows) + 1, min(max(rows) + 1 + lift, th + lift)):
            r, g, b_, al = mpx[px, py % th]
            if al:
                opx[px, py] = (int(r * k), int(g * k), int(b_ * k), 255)
    return {"ok": True, "image": out, "origin": (0, lift // 2),
            "size": (tw, th + lift), "axis": axis}
