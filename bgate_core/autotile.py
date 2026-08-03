"""Which tile goes in the cell — the step that makes the seams line up.

A generator produces a SHAPE: this set of coordinates is wall, that set is
floor. Nothing in that answers which of the forty-seven wall sprites belongs at
a given coordinate, and getting it wrong is the failure everyone recognises on
sight — corners facing out of the room, a straight run of wall wearing end caps
in the middle of it.

The Godot editor solves this with terrain sets, and its solver runs in the
editor. Builders Gate writes ``tile_map_data`` directly and never opens the
editor, so the same job has to be done here or every generated level ships with
the seams wrong.

The method is a neighbour bitmask, not constraint propagation. Each cell asks
which of its neighbours are the same terrain, packs the answers into an integer,
and looks that integer up in a table. It is O(cells), it cannot fail, and it is
exactly deterministic — three properties Wave Function Collapse does not have,
for a problem that does not need what WFC buys.

Two mask widths, because sheets come in two sizes:

  * 4-bit / 16 tiles. Sides only (N, E, S, W). Correct for a wall that is one
    cell thick and for most floor trim.
  * 8-bit / 47 tiles ("blob"). Sides plus the four corners, where a corner only
    counts if BOTH of its adjacent sides are already set — that reduction is
    what takes 256 combinations down to 47, and skipping it is how people end
    up authoring 209 tiles that can never be selected.

WHERE THE TABLE COMES FROM IS THE CALLER'S PROBLEM, and the default is a
convention, not a standard. ``Terrain.grid16`` and ``Terrain.blob47`` lay the
masks out in ascending order, row-major, on a sheet ``columns`` wide. Sheets
authored elsewhere (Tilesetter, Kenney, a hand-drawn set) use their own order,
and ``Terrain.from_table`` takes it. A wrong table is visible in the first
screenshot; a wrong ORDER silently drawn as if it were right is the thing to
watch for.

I/O-free: sets of coordinates in, cell dicts out, ready for
``tilemap.encode_cells``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

# Side bits. The order is this module's own and only has to agree with the
# tables below — a sheet's order is expressed in the table, never here.
N, E, S, W = 1, 2, 4, 8
# Corner bits, for the 8-bit mask.
NE, SE, SW, NW = 16, 32, 64, 128

_SIDES = ((N, 0, -1), (E, 1, 0), (S, 0, 1), (W, -1, 0))
_CORNERS = ((NE, 1, -1, N | E), (SE, 1, 1, S | E),
            (SW, -1, 1, S | W), (NW, -1, -1, N | W))


class TerrainError(ValueError):
    """A terrain this module will not guess at."""


# ---------------------------------------------------------------------------
# Masks
# ---------------------------------------------------------------------------
def canonical8(mask: int) -> int:
    """Drop the corner bits that cannot mean anything.

    A north-east corner only describes a shape when north and east are BOTH
    filled; otherwise the corner is already covered by the side edge and the
    two masks want the same sprite. Without this reduction the table needs 256
    entries, 209 of which are unreachable, and the ones an artist happens to
    draw get picked at random depending on what is diagonally adjacent.
    """
    for bit, _dx, _dy, needs in _CORNERS:
        if mask & bit and (mask & needs) != needs:
            mask &= ~bit
    return mask


def blob47_masks() -> list[int]:
    """The 47 masks that survive canonical8, ascending. The name is the test."""
    return sorted({canonical8(m) for m in range(256)})


def _member(filled, x: int, y: int, region, outside: bool) -> bool:
    if (x, y) in filled:
        return True
    if region is None:
        return False
    rx, ry, rw, rh = region
    if rx <= x < rx + rw and ry <= y < ry + rh:
        return False
    return outside


def bitmask(filled, x: int, y: int, *, bits: int = 8,
            region: Optional[tuple[int, int, int, int]] = None,
            outside: bool = False) -> int:
    """How this cell's neighbourhood looks, packed into an integer.

    ``outside`` decides what lies beyond ``region``. A dungeon carved out of
    solid rock wants ``outside=True`` on its wall terrain: otherwise every cell
    on the boundary believes it has open space behind it and the whole map gets
    a decorative border facing the void nobody will ever see.
    """
    mask = 0
    for bit, dx, dy in _SIDES:
        if _member(filled, x + dx, y + dy, region, outside):
            mask |= bit
    if bits == 4:
        return mask
    if bits != 8:
        raise TerrainError(f"mask width is 4 or 8, not {bits}")
    for bit, dx, dy, _needs in _CORNERS:
        if _member(filled, x + dx, y + dy, region, outside):
            mask |= bit
    return canonical8(mask)


# ---------------------------------------------------------------------------
# Terrains
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Terrain:
    """A source id plus mask -> atlas coordinate. Everything else is derived."""

    source: int
    bits: int
    table: Mapping[int, tuple[int, int]]
    name: str = ""
    alt: int = 0
    #: Used when a mask has no entry. Without one, a hole in the table becomes a
    #: hole in the level, which reads as a generator bug rather than a missing
    #: sprite.
    fallback: Optional[tuple[int, int]] = None

    def atlas_for(self, mask: int) -> Optional[tuple[int, int]]:
        hit = self.table.get(mask)
        if hit is not None:
            return hit
        return self.fallback

    # -- constructors -------------------------------------------------------
    @classmethod
    def solid(cls, source: int, atlas: tuple[int, int] = (0, 0), *,
              name: str = "", alt: int = 0) -> "Terrain":
        """One tile, every cell. The floor of most levels; no seams to line up."""
        return cls(source=source, bits=4, table={}, name=name, alt=alt,
                   fallback=(int(atlas[0]), int(atlas[1])))

    @classmethod
    def grid16(cls, source: int, *, columns: int = 4,
               origin: tuple[int, int] = (0, 0), name: str = "",
               alt: int = 0) -> "Terrain":
        """16 side-mask tiles, laid out row-major from ``origin``, mask ascending."""
        return cls(source=source, bits=4, name=name, alt=alt,
                   table=_row_major(range(16), columns, origin),
                   fallback=(origin[0], origin[1]))

    @classmethod
    def blob47(cls, source: int, *, columns: int = 8,
               origin: tuple[int, int] = (0, 0), name: str = "",
               alt: int = 0) -> "Terrain":
        """47 blob tiles, laid out row-major from ``origin``, mask ascending."""
        return cls(source=source, bits=8, name=name, alt=alt,
                   table=_row_major(blob47_masks(), columns, origin),
                   fallback=(origin[0], origin[1]))

    @classmethod
    def from_table(cls, source: int, table: Mapping, *, bits: int = 8,
                   name: str = "", alt: int = 0,
                   fallback: Optional[tuple[int, int]] = None) -> "Terrain":
        """A sheet's own order. Keys are masks, values are (atlas x, atlas y).

        The keys are canonicalised on the way in for ``bits=8`` — a table
        written with the raw 256-value masks in mind would otherwise have most
        of its entries sit unreachable behind ``canonical8``.
        """
        if bits not in (4, 8):
            raise TerrainError(f"mask width is 4 or 8, not {bits}")
        clean: dict[int, tuple[int, int]] = {}
        for mask, atlas in table.items():
            key = canonical8(int(mask)) if bits == 8 else int(mask) & 0x0F
            pair = (int(atlas[0]), int(atlas[1]))
            if clean.setdefault(key, pair) != pair:
                raise TerrainError(
                    f"mask {mask} reduces to {key}, which is already mapped to "
                    f"{clean[key]} — two sprites cannot answer the same shape")
        return cls(source=source, bits=bits, table=clean, name=name, alt=alt,
                   fallback=fallback)


def _row_major(masks: Iterable[int], columns: int,
               origin: tuple[int, int]) -> dict[int, tuple[int, int]]:
    if columns < 1:
        raise TerrainError("columns must be at least 1")
    return {m: (origin[0] + i % columns, origin[1] + i // columns)
            for i, m in enumerate(masks)}


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------
def resolve(filled: Iterable[tuple[int, int]], terrain: Terrain, *,
            region: Optional[tuple[int, int, int, int]] = None,
            outside: bool = False) -> list[dict]:
    """Every filled coordinate as a placed cell, tile chosen by its neighbours.

    Output is the shape ``tilemap.encode_cells`` takes. Cells whose mask has no
    entry and no fallback are DROPPED and counted by ``unmapped()`` rather than
    guessed at — a sprite that is merely wrong is harder to notice than a gap.
    """
    cells = set(filled)
    out = []
    for (x, y) in sorted(cells, key=lambda c: (c[1], c[0])):
        mask = bitmask(cells, x, y, bits=terrain.bits, region=region,
                       outside=outside)
        atlas = terrain.atlas_for(mask)
        if atlas is None:
            continue
        out.append({"x": x, "y": y, "source": terrain.source,
                    "ax": atlas[0], "ay": atlas[1], "alt": terrain.alt})
    return out


def unmapped(filled: Iterable[tuple[int, int]], terrain: Terrain, *,
             region: Optional[tuple[int, int, int, int]] = None,
             outside: bool = False) -> dict[int, int]:
    """Masks this terrain has no tile for, and how often each came up.

    The report that tells an artist which sprites are still missing, in the
    order that matters: the mask that shows up 200 times is the one to draw.
    """
    cells = set(filled)
    counts: dict[int, int] = {}
    for (x, y) in cells:
        mask = bitmask(cells, x, y, bits=terrain.bits, region=region,
                       outside=outside)
        if terrain.atlas_for(mask) is None:
            counts[mask] = counts.get(mask, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
