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
EDGE_INSET = 0.28


def normalise_edges(image, table: dict, wanted: Sequence[int], *,
                    tile_size: tuple[int, int],
                    colours: Sequence[Sequence[float]],
                    inset: float = EDGE_INSET) -> dict:
    """Rebuild every mask with a CONSISTENT edge inset, keeping the art.

    The model's texture is kept — floor from its own full tile, void from its
    own void regions — but the GEOMETRY is imposed: void reaches exactly
    ``inset`` of the way in from each side the mask says is void, on every
    tile. That is the property a hand-made autotile set has by construction
    and a generated one does not, and without it neighbours disagree about
    where the floor stops.

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
        for x0, y0, x1, y1 in bands:
            for y in range(y0, y1):
                for x in range(x0, x1):
                    src = vpx[x, y]
                    # only paint where the donor is genuinely void, so the
                    # band keeps its texture instead of becoming a rectangle
                    if _dist(src[:3], void_c) < _dist(src[:3], floor_c):
                        tpx[x, y] = src
                    else:
                        tpx[x, y] = (int(void_c[0]), int(void_c[1]),
                                     int(void_c[2]), 255)
        out_tiles[mask] = tile

    cols = 4 if len(wanted) <= 16 else 8
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
                       inset: float = EDGE_INSET,
                       solid_void: bool = True) -> dict:
    """A collision polygon per mask, derived from the SAME inset the art uses.

    Not traced from pixels. The tiles were rebuilt with one known inset by
    :func:`normalise_edges`, so the walkable region of every tile is a
    rectangle known exactly — tracing the art would rediscover that rectangle
    with jitter and hand Godot a fifty-point polygon per tile for nothing.

    ``solid_void=True`` gives the collider the VOID (what stops the player),
    which is what a top-down dungeon wants: the floor is walkable and the dark
    is wall. The void of a tile is up to four edge bands, so this returns one
    polygon per band rather than trying to express a ring as a single convex
    shape — Godot takes several polygons per tile and each band is convex.

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
