"""TileMapLayer — the part of a scene that IS the level.

A viewport that skips tilemaps is a viewport that cannot show a tile-based
game, which is most 2D games and certainly this one: the office floor is three
TileMapLayers and nothing else. Reporting "nothing in this scene draws" for it
was not a limitation, it was a wrong answer.

Two formats have to be read to fix that, and neither is documented anywhere
except Godot's source:

  * ``tile_map_data`` is a base64 PackedByteArray, not text — a 2-byte format
    version followed by 12 bytes per placed cell: x, y, source_id, atlas x,
    atlas y, alternative, all little-endian uint16.
  * The TileSet is a .tres whose ``sources/N`` entries point at
    TileSetAtlasSource sub-resources, each carrying a texture and the size of
    one region within it.

Then the cell coordinates have to become pixels, and that depends on the
tileset's SHAPE. A square tilemap is a multiply. An isometric one is a diamond
projection, and getting it wrong does not error — it renders a plausible,
wrong-looking grid, which is the failure mode this whole module is written to
avoid.

I/O-free: the caller supplies the .tres text.
"""
from __future__ import annotations

import base64
import binascii
import re
import struct
from typing import Optional

# Godot TileSet.TileShape
SQUARE, ISOMETRIC, HALF_OFFSET_SQUARE, HEXAGON = 0, 1, 2, 3
# Godot TileSet.TileLayout — only the diamond layouts change the projection in
# a way that matters here; the stacked layouts are rarer and fall back to
# diamond-down rather than being silently rendered as square.
DIAMOND_RIGHT, DIAMOND_DOWN = 4, 5

CELL_STRUCT = struct.Struct("<HHHHHH")   # x, y, source, atlas x, atlas y, alt
CELL_BYTES = CELL_STRUCT.size            # 12
MAX_CELLS = 200_000                      # a guard, not a real limit
COORD_MIN, COORD_MAX = -0x8000, 0x7FFF   # what the signed read can give back
FIELD_MAX = 0xFFFF                       # source and alt are genuinely unsigned


class TileError(ValueError):
    """Tile data this module will not guess at."""


# ---------------------------------------------------------------------------
# tile_map_data
# ---------------------------------------------------------------------------
def decode_cells(packed: str) -> list[dict]:
    """The placed cells of one TileMapLayer.

    Coordinates are stored as uint16 but are SIGNED — a tilemap extending left
    or up from the origin wraps to 65535 and would otherwise render sixty-five
    thousand tiles away from everything else.
    """
    if not packed:
        return []
    try:
        blob = base64.b64decode(packed, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TileError(f"tile data is not valid base64: {exc}") from exc
    if len(blob) < 2:
        return []
    body = len(blob) - 2
    if body % CELL_BYTES:
        raise TileError(
            f"tile data is {body} bytes after the header, which is not a whole "
            f"number of {CELL_BYTES}-byte cells")
    count = body // CELL_BYTES
    if count > MAX_CELLS:
        raise TileError(f"{count} cells is past the {MAX_CELLS} cap")

    signed = lambda v: v - 0x10000 if v > 0x7FFF else v
    out = []
    for i in range(count):
        x, y, source, ax, ay, alt = CELL_STRUCT.unpack_from(blob, 2 + i * CELL_BYTES)
        out.append({"x": signed(x), "y": signed(y), "source": source,
                    "ax": signed(ax), "ay": signed(ay), "alt": alt})
    return out


def _cell_fields(cell) -> tuple[int, int, int, int, int, int]:
    """One cell as (x, y, source, ax, ay, alt), from a dict or a sequence.

    Both shapes exist already: decode_cells hands back dicts, layer_draw hands
    back 5-element lists. A generator that fed either one straight back in and
    got a TypeError would be right to be annoyed.
    """
    if isinstance(cell, dict):
        try:
            return (int(cell["x"]), int(cell["y"]), int(cell["source"]),
                    int(cell.get("ax", 0)), int(cell.get("ay", 0)),
                    int(cell.get("alt", 0)))
        except KeyError as exc:
            raise TileError(f"cell is missing {exc.args[0]!r}: {cell!r}") from exc
    seq = list(cell)
    if len(seq) not in (5, 6):
        raise TileError(
            f"a cell is 5 (x, y, source, ax, ay) or 6 values, got {len(seq)}")
    return tuple(int(v) for v in seq) + ((0,) if len(seq) == 5 else ())


def encode_cells(cells, *, version: int = 0) -> str:
    """Placed cells back into a ``tile_map_data`` string. Inverse of decode_cells.

    Written in (y, x) order rather than the caller's. Godot stores these in a
    hash map and does not care, but a generator that is re-run on the same seed
    should produce a byte-identical scene — an unordered write turns "nothing
    changed" into a diff of the entire level, every time.

    Two things are refused rather than wrapped, because both are silent in the
    engine and loud nowhere:

      * A coordinate outside int16. It survives the round trip as a tile 65,000
        cells away, which looks like the generator exploded rather than like a
        bounds bug.
      * Two cells on the same coordinate. A TileMapLayer is a map keyed by
        coordinate, so the loser simply never exists and the layer is quietly
        short a tile.
    """
    fields = [_cell_fields(c) for c in cells]
    if len(fields) > MAX_CELLS:
        raise TileError(f"{len(fields)} cells is past the {MAX_CELLS} cap")

    seen: set[tuple[int, int]] = set()
    for x, y, source, ax, ay, alt in fields:
        for name, v in (("x", x), ("y", y), ("ax", ax), ("ay", ay)):
            if not COORD_MIN <= v <= COORD_MAX:
                raise TileError(
                    f"{name}={v} is outside int16 ({COORD_MIN}..{COORD_MAX}); "
                    "it would wrap to the far side of the map")
        for name, v in (("source", source), ("alt", alt)):
            if not 0 <= v <= FIELD_MAX:
                raise TileError(f"{name}={v} is outside 0..{FIELD_MAX}")
        if (x, y) in seen:
            raise TileError(f"two cells on ({x}, {y}) — a layer holds one tile "
                            "per coordinate, so one of them would vanish")
        seen.add((x, y))

    blob = struct.pack("<H", version)
    for x, y, source, ax, ay, alt in sorted(fields, key=lambda f: (f[1], f[0])):
        blob += CELL_STRUCT.pack(x & 0xFFFF, y & 0xFFFF, source,
                                 ax & 0xFFFF, ay & 0xFFFF, alt)
    return base64.b64encode(blob).decode("ascii")


# ---------------------------------------------------------------------------
# The TileSet
# ---------------------------------------------------------------------------
_EXT_RE = re.compile(
    r'\[ext_resource type="[^"]*"\s+(?:uid="[^"]*"\s+)?path="([^"]+)"\s+id="([^"]+)"\]')
_SUB_HEAD_RE = re.compile(r'\[sub_resource type="TileSetAtlasSource" id="([^"]+)"\]')
_VEC2I_RE = re.compile(r"Vector2i\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)")
_SOURCE_RE = re.compile(r'^sources/(\d+)\s*=\s*SubResource\("([^"]+)"\)', re.MULTILINE)
# The tiles an atlas actually DEFINES, written "3:1/0 = 0". A tile that is not
# in this list cannot be placed — Godot draws nothing and reports nothing — so
# anything generating cell coordinates needs to know which ones exist. The
# alternative-tile and per-tile-property lines have more path segments before
# the '=', which is what keeps them out of this match.
_TILE_RE = re.compile(r"^(\d+):(\d+)/(\d+)\s*=", re.MULTILINE)


def parse_tileset(text: str) -> dict:
    """Shape, tile size, and every atlas source keyed by the id cells refer to."""
    if not text:
        raise TileError("empty tileset")
    ext = {rid: path for path, rid in _EXT_RE.findall(text)}

    atlases: dict[str, dict] = {}
    for block in re.split(r"\n(?=\[)", text):
        head = _SUB_HEAD_RE.match(block.split("\n", 1)[0])
        if not head:
            continue
        tex = re.search(r'texture\s*=\s*ExtResource\("([^"]+)"\)', block)
        region = re.search(r"texture_region_size\s*=\s*" + _VEC2I_RE.pattern, block)
        origin = re.search(r"texture_origin\s*=\s*" + _VEC2I_RE.pattern, block)
        if not tex:
            continue
        atlases[head.group(1)] = {
            "texture": ext.get(tex.group(1)),
            "region": ([int(region.group(1)), int(region.group(2))]
                       if region else None),
            "origin": ([int(origin.group(1)), int(origin.group(2))]
                       if origin else [0, 0]),
            "tiles": sorted({(int(x), int(y))
                             for x, y, _alt in _TILE_RE.findall(block)}),
        }

    resource = text.split("[resource]", 1)[-1]
    size = _VEC2I_RE.search(
        re.search(r"tile_size\s*=\s*Vector2i\([^)]*\)", resource).group(0)) \
        if re.search(r"tile_size\s*=\s*Vector2i", resource) else None
    tile_size = [int(size.group(1)), int(size.group(2))] if size else [16, 16]

    def _int(name: str, default: int) -> int:
        m = re.search(rf"^{name}\s*=\s*(-?\d+)", resource, re.MULTILINE)
        return int(m.group(1)) if m else default

    sources = {}
    for sid, sub in _SOURCE_RE.findall(resource):
        atlas = atlases.get(sub)
        if not atlas or not atlas["texture"]:
            continue
        sources[int(sid)] = {
            "texture": atlas["texture"],
            "region": atlas["region"] or tile_size,
            "origin": atlas["origin"],
            "tiles": atlas["tiles"],
        }
    return {
        "tile_size": tile_size,
        "shape": _int("tile_shape", SQUARE),
        "layout": _int("tile_layout", DIAMOND_RIGHT),
        "sources": sources,
    }


def _source_lines(src: dict, ext_ref: str) -> list[str]:
    """The .tres lines of ONE TileSetAtlasSource sub_resource.

    Factored out of :func:`write_tileset` so :func:`append_source` emits a
    new source in exactly the format the writer does — two emitters of an
    undocumented format is how they drift.
    """
    out = [f'[sub_resource type="TileSetAtlasSource" '
           f'id="TileSetAtlasSource_{src["id"]}"]',
           f'texture = ExtResource("{ext_ref}")',
           f"texture_region_size = Vector2i({src['region'][0]}, "
           f"{src['region'][1]})"]
    if src.get("origin") and tuple(src["origin"]) != (0, 0):
        out.append(f"texture_origin = Vector2i({src['origin'][0]}, "
                   f"{src['origin'][1]})")
    for x, y in src["tiles"]:
        # ANIMATION IS A TILE PROPERTY, NOT AN ALTERNATIVE'S, and it is
        # written BEFORE the `x:y/0 = 0` line that declares the tile.
        # Both facts came from having Godot author one and reading it back:
        # emitting `x:y/0/animation_columns` — the shape texture_origin
        # genuinely uses — loaded without complaint and simply never played,
        # which is the same silent-acceptance failure as polygons_count.
        span = (src.get("sizes") or {}).get((x, y))
        if span and span != (1, 1):
            if span[0] < 1 or span[1] < 1:
                raise TileError(
                    f"tile {x}:{y} spans {span[0]}x{span[1]} atlas cells, "
                    "which is not a tile")
            out.append(f"{x}:{y}/size_in_atlas = "
                       f"Vector2i({span[0]}, {span[1]})")
        anim = (src.get("animation") or {}).get((x, y))
        if anim:
            frames = int(anim.get("frames") or 0)
            if frames < 2:
                raise TileError(
                    f"tile {x}:{y} declares {frames} animation frames — an "
                    "animation needs at least two, and one frame is a "
                    "static tile that should not declare animation at all")
            cols = int(anim.get("columns") or frames)
            out.append(f"{x}:{y}/animation_columns = {cols}")
            if anim.get("speed"):
                out.append(f"{x}:{y}/animation_speed = "
                           f"{float(anim['speed'])}")
            durations = list(anim.get("durations") or [])
            for f in range(frames):
                d = durations[f] if f < len(durations) else 1.0
                out.append(f"{x}:{y}/animation_frame_{f}/duration = "
                           f"{float(d)}")
        out.append(f"{x}:{y}/0 = 0")
        nudge = (src.get("origins") or {}).get((x, y))
        if nudge and nudge != (0, 0):
            out.append(f"{x}:{y}/0/texture_origin = "
                       f"Vector2i({nudge[0]}, {nudge[1]})")
        # A tile with no polygon is WALKABLE-BY-OMISSION, which is the
        # right default. A tile can need SEVERAL — a corridor is solid on
        # two sides, an isolated tile on four — and each band is its own
        # convex polygon because a ring is not one.
        #
        # NO polygons_count LINE. Godot infers the count from the highest
        # polygon_N present and writing the count explicitly made it drop
        # every polygon silently: the resource loaded, the physics layer
        # existed, and the engine reported zero shapes. Confirmed by
        # having Godot author a tileset and reading back what it wrote —
        # which is the only way to learn a format nobody documents.
        for i, poly in enumerate((src.get("collision") or {}).get((x, y))
                                 or []):
            pts = ", ".join(f"{px}, {py}" for px, py in poly)
            out.append(f"{x}:{y}/0/physics_layer_0/polygon_{i}/points "
                       f"= PackedVector2Array({pts})")
    return out


def write_tileset(sources: list[dict], *, tile_size: tuple[int, int],
                  shape: int = SQUARE, layout: int = DIAMOND_RIGHT,
                  physics: bool = False) -> str:
    """A Godot 4 TileSet resource over one or more atlas textures.

    THE OTHER HALF OF :func:`parse_tileset`, and the piece whose absence kept
    the whole level chain dark: levelgen, autotile, encode_cells and
    wire_tilemap were all real and all blocked on a resource nothing here could
    produce, so a generated level needed a human to open the Godot editor first.

    ``sources``: ``[{id, texture, tiles, region?, origin?, origins?, collision?}]``
    — ``texture`` is
    a ``res://`` path, ``tiles`` the ``(atlas_x, atlas_y)`` coordinates the
    source DEFINES. A coordinate not listed here cannot be placed: Godot draws
    nothing and reports nothing, which is the failure `_TILE_RE` exists to let
    callers pre-empt.

    ``origin`` shifts every tile in the source; ``origins`` shifts ONE tile,
    ``{(x, y): (dx, dy)}``. Per-tile is what a mounted prop needs: a torch drawn
    centred in its wall cell reads as lying in the middle of the stone, and the
    sprite has to sit against the cell's inner edge to read as bolted to a wall.

    Byte-stable — coordinates sorted, ids ordered — for the same reason
    :func:`encode_cells` is: a regenerated tileset that differs only in line
    order is a diff nobody can review.

    The round trip is the cheap half of the check: ``parse_tileset`` reads this
    back and must agree. It cannot catch a misconception the reader and writer
    share, so the engine itself is the real referee — see godot.inspect_tileset.
    """
    if not sources:
        raise TileError("a tileset needs at least one atlas source")
    tw, th = int(tile_size[0]), int(tile_size[1])
    if tw < 1 or th < 1:
        raise TileError(f"tile size {tw}x{th} is not drawable")

    clean: list[dict] = []
    for entry in sources:
        texture = str(entry.get("texture") or "").strip()
        if not texture:
            raise TileError("an atlas source needs a texture path")
        tiles = sorted({(int(x), int(y)) for x, y in (entry.get("tiles") or [])})
        if not tiles:
            # An empty source parses back as a source with no tiles, which
            # level_generate then refuses one layer later with a coordinate
            # list. Refusing here names the actual mistake.
            raise TileError(f"atlas {texture} defines no tiles")
        if any(x < 0 or y < 0 for x, y in tiles):
            raise TileError(f"atlas {texture} has a negative tile coordinate")
        region = entry.get("region") or (tw, th)
        clean.append({
            "id": int(entry.get("id", len(clean))),
            "texture": texture,
            "tiles": tiles,
            "region": (int(region[0]), int(region[1])),
            "origin": tuple(int(v) for v in (entry.get("origin") or (0, 0))),
            # {(x, y): (w, h)} — a tile spanning SEVERAL atlas cells, which is
            # how a prop gets proportions: a pillar is one cell wide and two
            # high, and drawing it into a 1x1 tile is what makes every prop look
            # squat and flat.
            "sizes": {(int(cx), int(cy)): (int(w), int(h))
                      for (cx, cy), (w, h)
                      in (entry.get("sizes") or {}).items()},
            # {(x, y): {frames, columns?, speed?, durations?}} — a LOOPING
            # tile animation, which Godot plays without a node. See the emit
            # loop for the format, which is learned rather than documented.
            "animation": {(int(cx), int(cy)): dict(spec)
                          for (cx, cy), spec
                          in (entry.get("animation") or {}).items()},
            "origins": {(int(cx), int(cy)):
                        (int(ox), int(oy))
                        for (cx, cy), (ox, oy)
                        in (entry.get("origins") or {}).items()},
            # Carried through the normalisation, which it was not: the emit
            # loop reads from THIS dict, so a collision map left behind here
            # is a collision map that silently never reaches the file.
            "collision": {(int(cx), int(cy)): polys
                          for (cx, cy), polys
                          in (entry.get("collision") or {}).items()},
        })
    for src in clean:
        # A SPANNING TILE OWNS THE CELLS IT COVERS. Godot logs "tiles are
        # already present in the space the tile would cover" and then simply
        # does not create the tile — the resource loads, the tile is missing,
        # and the map draws nothing there. Refusing here names it.
        owned: dict = {}
        for x, y in src["tiles"]:
            w, h = (src.get("sizes") or {}).get((x, y), (1, 1))
            for dx in range(max(1, w)):
                for dy in range(max(1, h)):
                    at = (x + dx, y + dy)
                    if at in owned:
                        raise TileError(
                            f"atlas {src['texture']}: tiles {owned[at]} and "
                            f"{(x, y)} both cover cell {at} — a spanning tile "
                            "owns every cell of its region, and Godot drops "
                            "the loser without loading anything differently")
                    owned[at] = (x, y)
        # An animation's frames live in the cells to the RIGHT of the tile and
        # are equally owned: defining one of them as its own tile is the same
        # collision by another route.
        for (x, y), spec in (src.get("animation") or {}).items():
            w, _h = (src.get("sizes") or {}).get((x, y), (1, 1))
            for f in range(1, int(spec.get("frames") or 1)):
                at = (x + f * max(1, w), y)
                if at in owned and owned[at] != (x, y):
                    raise TileError(
                        f"atlas {src['texture']}: tile {(x, y)} animates over "
                        f"cell {at}, which tile {owned[at]} already defines")

    if len({s["id"] for s in clean}) != len(clean):
        raise TileError("two atlas sources share one source id")
    clean.sort(key=lambda s: s["id"])

    # load_steps counts the ext_resources plus the sub_resources plus one.
    steps = len(clean) * 2 + 1
    out = [f'[gd_resource type="TileSet" load_steps={steps} format=3]', ""]
    for i, src in enumerate(clean, start=1):
        out.append(f'[ext_resource type="Texture2D" path="{src["texture"]}" '
                   f'id="{i}"]')
    out.append("")
    for i, src in enumerate(clean, start=1):
        out.extend(_source_lines(src, str(i)))
        out.append("")
    out.append("[resource]")
    out.append(f"tile_size = Vector2i({tw}, {th})")
    if physics:
        # One physics layer on collision layer 1 — the default a 2D game
        # starts from. Ordering and spelling copied from a tileset Godot
        # saved itself; the per-tile polygons reference this layer by index.
        out.append("physics_layer_0/collision_layer = 1")
    if shape != SQUARE:
        out.append(f"tile_shape = {int(shape)}")
        out.append(f"tile_layout = {int(layout)}")
    for src in clean:
        out.append(f'sources/{src["id"]} = SubResource('
                   f'"TileSetAtlasSource_{src["id"]}")')
    out.append("")
    return chr(10).join(out)


def append_source(text: str, source: dict) -> dict:
    """Add one atlas source to an EXISTING TileSet .tres, textually.

    The seam this closes: prop_generate writes an atlas and a manifest,
    level generators place cells that reference that atlas by source id —
    and nothing ever taught the tileset the atlas exists, so the cells
    pointed at a source the resource never defined. Rewriting the whole
    file through write_tileset would drop whatever parse_tileset does not
    round-trip (another writer's collision, animation, terrain data), so
    the existing text is kept byte-for-byte and one source is appended in
    the writer's own format.

    ``source`` is a write_tileset source dict; ``id`` defaults to one past
    the highest existing. Returns ``{text, id, reused}`` — a source whose
    texture is already in the set is returned as-is, so this is idempotent.
    """
    parsed = parse_tileset(text)
    for sid, src in parsed["sources"].items():
        if src["texture"] == source.get("texture"):
            return {"text": text, "id": sid, "reused": True}

    sid = int(source.get("id") or 0)
    if not sid or sid in parsed["sources"]:
        sid = max(parsed["sources"], default=-1) + 1
    src = dict(source, id=sid)
    src.setdefault("region", parsed["tile_size"])
    src.setdefault("origin", (0, 0))

    ext_ids = set(re.findall(r'\[ext_resource[^\]]*?id="([^"]+)"', text))
    ext_ref = f"{len(ext_ids) + 1}_src{sid}"
    while ext_ref in ext_ids:
        ext_ref += "_"

    lines = text.split("\n")
    res_at = next((i for i, l in enumerate(lines)
                   if l.strip() == "[resource]"), None)
    if res_at is None:
        raise TileError("no [resource] section — this is not a .tres")
    last_ext = max((i for i, l in enumerate(lines)
                    if l.startswith("[ext_resource")), default=0)

    ext_line = (f'[ext_resource type="Texture2D" '
                f'path="{src["texture"]}" id="{ext_ref}"]')
    sub_lines = _source_lines({
        **src,
        "tiles": sorted({(int(x), int(y)) for x, y in src["tiles"]}),
    }, ext_ref)

    out = (lines[:last_ext + 1] + [ext_line]
           + lines[last_ext + 1:res_at] + sub_lines + [""]
           + lines[res_at:])
    out.append(f'sources/{sid} = SubResource("TileSetAtlasSource_{sid}")')

    def _bump(m):
        return f"load_steps={int(m.group(1)) + 2}"

    joined = re.sub(r"load_steps=(\d+)", _bump, "\n".join(out), count=1)
    # the writer's own reader must agree before anyone else is asked to
    check = parse_tileset(joined)
    if sid not in check["sources"]:
        raise TileError("appended source did not parse back — refusing to "
                        "hand the engine a resource this module cannot read")
    return {"text": joined, "id": sid, "reused": False}


# ---------------------------------------------------------------------------
# Cell -> pixels
# ---------------------------------------------------------------------------
def cell_center(x: int, y: int, *, shape: int, layout: int,
                tile_size: list[int]) -> tuple[float, float]:
    """Where the CENTRE of a cell sits, in the layer's own space.

    Godot's ``map_to_local``. Square is the multiply everyone expects;
    isometric is a diamond projection, and rendering an isometric map with the
    square formula produces a neat grid that is confidently wrong.

    THE HALF-CELL TERM IS NOT OPTIONAL, and leaving it off the isometric
    branches is why the viewport's floor never lined up with the things
    standing on it. `map_to_local` returns a cell's CENTRE — which is what the
    square branch has always done with its ``+ w/2, + h/2`` — and the diamond
    branches were returning the diamond's top corner instead. Every tile
    therefore drew exactly (w/2, h/2) up and left of where the engine puts it.

    Nothing on the tile side could reveal that: ``bounds`` is derived from this
    same function, so the layer stayed perfectly self-consistent and only
    disagreed with the NODES, whose positions come out of the .tscn already in
    engine coordinates. Measured on downsizing's floor_tut (233 props carrying
    both a ``cell`` and the ``position`` the game bakes from it): with the term,
    232 of 233 sit within one authored wall-hug offset of their own cell and the
    residual straddles zero (dy -6.4..13.0); without it, only 200 do and every
    single residual is positive (dy 9.6..29.0) — a constant shift, not noise.
    That project's own scripts/bake_prop_nodes.py documents the same formula and
    records it as verified against Godot 4.7's map_to_local.
    """
    w, h = tile_size[0], tile_size[1]
    if shape == ISOMETRIC:
        if layout == DIAMOND_DOWN:
            return ((x - y) * w / 2.0 + w / 2.0, (x + y) * h / 2.0 + h / 2.0)
        # DIAMOND_RIGHT and anything unrecognised: the other diagonal.
        return ((x + y) * w / 2.0 + w / 2.0, (y - x) * h / 2.0 + h / 2.0)
    return (x * w + w / 2.0, y * h + h / 2.0)


def layer_draw(packed: str, tileset: dict, *, y_sort: bool = False) -> dict:
    """One TileMapLayer as a compact draw payload.

    The cells are sent as a flat list and the CLIENT computes positions from
    the shared tileset block — a level here is ~560 cells across three layers,
    and expanding every one into its own rectangle server-side would triple the
    payload for arithmetic the canvas has to do anyway.

    ``y_sort`` reorders them, and on a wall layer it is not cosmetic. Godot
    stores ``tile_map_data`` in CELL (y, x) order, which on an isometric map is
    not screen order at all: screen y runs with ``x + y``, so every row wraps
    back above the one before it — 48 times on this project's floor. A 100px
    wall tile overlaps the two cells behind it, so drawn in file order the
    wall behind is painted over the wall in front. A y-sorted TileMapLayer in
    the engine gives every cell its own canvas item ordered by ``map_to_local``,
    which is what this reproduces. A layer that is NOT y-sorted keeps the
    file's order, because that is what the engine draws then.
    """
    cells = decode_cells(packed)
    if y_sort:
        shape, layout = tileset["shape"], tileset["layout"]
        size = tileset["tile_size"]

        def key(c):
            cx, cy = cell_center(c["x"], c["y"], shape=shape, layout=layout,
                                 tile_size=size)
            return (cy, cx)

        cells.sort(key=key)
    used = sorted({c["source"] for c in cells})
    # `tiles` stays out: the canvas draws the cells it is given and never needs
    # the list of every tile the atlas defines. That list is for the generator.
    sources = {sid: {k: v for k, v in tileset["sources"][sid].items()
                     if k != "tiles"}
               for sid in used if sid in tileset["sources"]}
    return {
        "tile_size": tileset["tile_size"],
        "shape": tileset["shape"],
        "layout": tileset["layout"],
        "sources": sources,
        "cells": [[c["x"], c["y"], c["source"], c["ax"], c["ay"]]
                  for c in cells if c["source"] in sources],
        "skipped": sum(1 for c in cells if c["source"] not in sources),
    }


def tile_rect(cx: float, cy: float, region: list[int],
              origin: list[int]) -> tuple[float, float, float, float]:
    """Where one tile's ART lands, given its cell CENTRE. Godot's ``draw_tile``.

    ``dest.position = centre - size/2 - texture_origin``. THE MINUS IS NOT A
    TYPO and it is the difference between a wall standing on its cell and a
    wall sunk into it. This project states the same rule from the other end
    (docs/SCALE.md, floor_layout.gd): ``texture_origin.y = h/2 - 16`` puts a
    texture's BOTTOM EDGE on the diamond's bottom vertex, i.e. at
    ``centre + 16``. Only the minus gives that:

        top    = cy - h/2 - (h/2 - 16) = cy - h + 16
        bottom = top + h               = cy + 16

    Adding it instead lands the bottom at ``cy - 16 + h``, which is ``h - 32``
    px too low — nothing at all for a 32px floor tile (origin 0), 38px for a
    70px cubicle and 68px for a 100px wall panel. That is why the floor grid
    always looked right while the furniture never sat on the walls.
    """
    return (cx - region[0] / 2.0 - origin[0],
            cy - region[1] / 2.0 - origin[1],
            region[0], region[1])


def bounds(cells: list, *, shape: int, layout: int, tile_size: list[int],
           sources: Optional[dict] = None
           ) -> Optional[tuple[float, float, float, float]]:
    """The area a layer covers, for framing the view on it.

    With ``sources``, the area a layer DRAWS — a 100px wall tile on a 64x32
    cell reaches 68px above the cell it stands on, and a bound that stops at
    the cell missed all of it. Without them, the cell footprint alone, which is
    what a caller that only has coordinates can honestly say.
    """
    if not cells:
        return None
    w, h = tile_size[0] / 2.0, tile_size[1] / 2.0
    x0 = y0 = float("inf")
    x1 = y1 = float("-inf")
    for c in cells:
        cx, cy = cell_center(c[0], c[1], shape=shape, layout=layout,
                             tile_size=tile_size)
        src = (sources or {}).get(c[2]) if len(c) > 2 else None
        if src:
            rx, ry, rw, rh = tile_rect(cx, cy, src["region"], src["origin"])
        else:
            rx, ry, rw, rh = cx - w, cy - h, tile_size[0], tile_size[1]
        x0, y0 = min(x0, rx), min(y0, ry)
        x1, y1 = max(x1, rx + rw), max(y1, ry + rh)
    return (x0, y0, x1, y1)
