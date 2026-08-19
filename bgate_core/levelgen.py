"""Where the rooms go — BSP layout, and the level plan it produces.

Binary space partition, because the guarantee is the point. Recursively cut the
map into two, keep cutting until a piece is small enough to hold one room, put a
room in each piece, and then — on the way back up — join the two halves of every
cut. That last step is not decoration: joining at every internal node builds a
spanning tree over the rooms, so EVERY room is reachable from every other room
by construction, not by a check that might fail.

That is what a scattered-rooms-plus-random-corridors generator cannot promise,
and what a texture-synthesis generator (Wave Function Collapse and friends)
promises even less: they produce plausible local structure and no global one,
which is fine for a wall pattern and useless for a level a player has to finish.

This module is geometry only. It emits sets of coordinates — floor here, wall
there — and hands them to ``autotile`` to become actual tiles. Keeping those
apart is what lets the same layout be re-skinned, and lets the layout be tested
without a tileset existing at all.

Deterministic on ``seed``: same seed, same level, byte for byte, forever. A
generator that cannot be re-run is one nobody can file a bug against.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, Optional

from bgate_core import autotile

MAX_AREA = 512 * 512          # a guard, not a design limit


class LevelError(ValueError):
    """Parameters that cannot produce a level, said before anything is drawn."""


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    def cells(self) -> set[tuple[int, int]]:
        return {(x, y) for y in range(self.y, self.y + self.h)
                for x in range(self.x, self.x + self.w)}

    def as_dict(self) -> dict:
        cx, cy = self.center
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h,
                "center": [cx, cy]}


# ---------------------------------------------------------------------------
# The partition
# ---------------------------------------------------------------------------
def _split(rect: Rect, rng: random.Random, *, min_leaf: int, max_depth: int,
           depth: int = 0) -> dict:
    """One node of the BSP tree: a rect, and either two children or none."""
    can_v = rect.w >= 2 * min_leaf
    can_h = rect.h >= 2 * min_leaf
    if depth >= max_depth or not (can_v or can_h):
        return {"rect": rect, "children": []}

    # Split the LONGER side unless the piece is roughly square, in which case
    # pick at random. Always splitting the longer side produces a grid of
    # near-identical squares; always picking at random produces corridors
    # shaped like hallways in a nightmare. The ratio gate is the middle.
    if can_v and can_h:
        if rect.w >= rect.h * 1.25:
            vertical = True
        elif rect.h >= rect.w * 1.25:
            vertical = False
        else:
            vertical = rng.random() < 0.5
    else:
        vertical = can_v

    if vertical:
        cut = rng.randint(rect.x + min_leaf, rect.x + rect.w - min_leaf)
        a = Rect(rect.x, rect.y, cut - rect.x, rect.h)
        b = Rect(cut, rect.y, rect.x + rect.w - cut, rect.h)
    else:
        cut = rng.randint(rect.y + min_leaf, rect.y + rect.h - min_leaf)
        a = Rect(rect.x, rect.y, rect.w, cut - rect.y)
        b = Rect(rect.x, cut, rect.w, rect.y + rect.h - cut)

    return {"rect": rect, "children": [
        _split(a, rng, min_leaf=min_leaf, max_depth=max_depth, depth=depth + 1),
        _split(b, rng, min_leaf=min_leaf, max_depth=max_depth, depth=depth + 1)]}


def _leaves(node: dict) -> list[dict]:
    if not node["children"]:
        return [node]
    return [leaf for c in node["children"] for leaf in _leaves(c)]


def _place_room(leaf: Rect, rng: random.Random, *, min_room: int,
                margin: int, fill: float = 0.8) -> Rect:
    """A room inside a leaf, never touching the leaf's edge.

    The margin is why two rooms in adjacent leaves cannot fuse into one
    L-shaped cavity — with it at zero, the partition is still correct and the
    level stops reading as rooms.

    ``fill`` is the floor on how much of the leaf the room must take, and it
    exists because uniform sizing looked wrong for a reason that is invisible
    in the plan: a room half its leaf's width leaves the OTHER half as solid
    rock, so the level renders as thin rooms separated by slabs. The rock
    between two rooms should read as a wall, which means the rooms have to
    come close to their partition.
    """
    max_w = leaf.w - 2 * margin
    max_h = leaf.h - 2 * margin
    w = rng.randint(max(min_room, int(max_w * fill)), max(min_room, max_w))
    h = rng.randint(max(min_room, int(max_h * fill)), max(min_room, max_h))
    x = rng.randint(leaf.x + margin, leaf.x + leaf.w - margin - w)
    y = rng.randint(leaf.y + margin, leaf.y + leaf.h - margin - h)
    return Rect(x, y, w, h)


# ---------------------------------------------------------------------------
# Corridors
# ---------------------------------------------------------------------------
def _run(a: tuple[int, int], b: tuple[int, int],
         width: int) -> set[tuple[int, int]]:
    """A straight, axis-aligned run of cells from a to b, ``width`` thick."""
    (x1, y1), (x2, y2) = a, b
    spread = range(0, width)
    if y1 == y2:
        lo, hi = sorted((x1, x2))
        return {(x, y1 + d) for x in range(lo, hi + 1) for d in spread}
    lo, hi = sorted((y1, y2))
    return {(x1 + d, y) for y in range(lo, hi + 1) for d in spread}


def _elbow(a: tuple[int, int], b: tuple[int, int], rng: random.Random,
           width: int) -> tuple[set[tuple[int, int]], list[list[int]]]:
    """An L-shaped corridor. Contiguous by construction — the two runs share
    their corner cell, so a flood fill crosses from one to the other."""
    (x1, y1), (x2, y2) = a, b
    if rng.random() < 0.5:
        knee = (x2, y1)
    else:
        knee = (x1, y2)
    cells = _run(a, knee, width) | _run(knee, b, width)
    return cells, [[x1, y1], [knee[0], knee[1]], [x2, y2]]


def _connect(node: dict, rng: random.Random, *, width: int, corridors: list,
             cells: set) -> tuple[int, int]:
    """Join this subtree, and hand back a point inside it to join further up.

    The return value is what makes the spanning tree work: a parent joins the
    point its left subtree reports to the point its right subtree reports, and
    since each of those is already connected to everything under it, the join
    merges two connected sets into one. Induction does the rest.
    """
    if not node["children"]:
        return node["room"].center
    a = _connect(node["children"][0], rng, width=width,
                 corridors=corridors, cells=cells)
    b = _connect(node["children"][1], rng, width=width,
                 corridors=corridors, cells=cells)
    run, path = _elbow(a, b, rng, width)
    cells |= run
    corridors.append({"path": path, "cells": len(run)})
    return a if rng.random() < 0.5 else b


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------
def wall_ring(floor: Iterable[tuple[int, int]]) -> set[tuple[int, int]]:
    """Every cell touching the floor that is not floor, diagonals included.

    Diagonals included ON PURPOSE. With only the four sides, an inside corner
    where two corridors meet has a single missing cell at the diagonal, the
    player can see through it, and it is the classic one-pixel hole that reads
    as a rendering bug rather than a missing tile.
    """
    floor = set(floor)
    ring: set[tuple[int, int]] = set()
    for (x, y) in floor:
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                p = (x + dx, y + dy)
                if p not in floor:
                    ring.add(p)
    return ring


def connected(floor: Iterable[tuple[int, int]]) -> bool:
    """Is every floor cell reachable from every other, four-directionally?

    This is the property the whole BSP-plus-join structure exists to provide,
    so it is checked rather than asserted in a comment.
    """
    floor = set(floor)
    if not floor:
        return True
    start = next(iter(floor))
    seen = {start}
    stack = [start]
    while stack:
        x, y = stack.pop()
        for p in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if p in floor and p not in seen:
                seen.add(p)
                stack.append(p)
    return len(seen) == len(floor)


def plan(width: int, height: int, *, seed: int = 0, min_leaf: int = 10,
         min_room: int = 4, margin: int = 1, max_depth: int = 5,
         corridor_width: int = 2, room_fill: float = 0.8) -> dict:
    """A level layout: rooms, corridors, and the floor and wall cells they make.

    Everything is validated up front, because the alternative is a plan that
    comes back with one room in it and no explanation.

    ``corridor_width`` defaults to TWO. One is a legal design and it renders as
    a crack: the tile art carries the wall boundary inside its own edge, so a
    one-cell passage loses over half its width to the two carved sides and
    reads as a seam in the rock rather than as somewhere you walk.
    """
    if width < 1 or height < 1:
        raise LevelError("width and height must be positive")
    if width * height > MAX_AREA:
        raise LevelError(f"{width}x{height} is past the {MAX_AREA}-cell cap")
    if min_room < 1:
        raise LevelError("min_room must be at least 1")
    if margin < 0:
        raise LevelError("margin cannot be negative")
    if corridor_width < 1:
        raise LevelError("corridor_width must be at least 1")
    if not 0.0 <= room_fill <= 1.0:
        raise LevelError("room_fill is a share of the leaf, 0.0 to 1.0")
    if min_leaf < min_room + 2 * margin:
        raise LevelError(
            f"min_leaf={min_leaf} cannot hold a {min_room} room with a "
            f"{margin} margin — it needs to be at least {min_room + 2 * margin}")
    if min_leaf > width or min_leaf > height:
        raise LevelError(
            f"min_leaf={min_leaf} does not fit in a {width}x{height} map")

    rng = random.Random(seed)
    root = _split(Rect(0, 0, width, height), rng, min_leaf=min_leaf,
                  max_depth=max_depth)

    leaves = _leaves(root)
    for leaf in leaves:
        leaf["room"] = _place_room(leaf["rect"], rng, min_room=min_room,
                                   margin=margin, fill=room_fill)

    floor: set[tuple[int, int]] = set()
    for leaf in leaves:
        floor |= leaf["room"].cells()
    corridors: list[dict] = []
    _connect(root, rng, width=corridor_width, corridors=corridors, cells=floor)

    # A corridor is aimed at room centres, so it stays inside the map — but
    # corridor_width thickens it downward/rightward and can push a cell over the
    # edge. Clip rather than let a tile land outside the level's own bounds.
    floor = {(x, y) for (x, y) in floor if 0 <= x < width and 0 <= y < height}
    walls = wall_ring(floor)

    # SOLID ROCK: every cell that is not floor, not just the one-cell ring.
    # A ring is a wall with nothing behind it, and on a finished map that reads
    # as a shelf jutting into empty space — the level has to be carved out of
    # something.
    solid = {(x, y) for x in range(width) for y in range(height)
             if (x, y) not in floor}

    ordered = [leaf["room"] for leaf in leaves]
    return {
        "seed": seed,
        "width": width,
        "height": height,
        "region": [0, 0, width, height],
        "rooms": [r.as_dict() for r in ordered],
        "corridors": corridors,
        "floor": sorted(floor, key=lambda c: (c[1], c[0])),
        "walls": sorted(walls, key=lambda c: (c[1], c[0])),
        "solid": sorted(solid, key=lambda c: (c[1], c[0])),
        "connected": connected(floor),
        "spawn": list(ordered[0].center) if ordered else None,
        "exit": list(ordered[-1].center) if ordered else None,
    }


def ascii_map(level: dict) -> str:
    """The plan as text. The fastest way for anyone — person or agent — to see
    that a level is one big room, or two disconnected halves, without a
    screenshot or an engine."""
    floor = {tuple(c) for c in level["floor"]}
    walls = {tuple(c) for c in level["walls"]}
    rows = []
    for y in range(-1, level["height"] + 1):
        row = []
        for x in range(-1, level["width"] + 1):
            row.append("." if (x, y) in floor
                       else "#" if (x, y) in walls else " ")
        rows.append("".join(row).rstrip())
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Plan -> tiles
# ---------------------------------------------------------------------------
def layers(level: dict, *, floor: autotile.Terrain,
           wall: Optional[autotile.Terrain] = None,
           floor_name: str = "Floor", wall_name: str = "Walls",
           wall_fill: bool = True) -> list[dict]:
    """The plan resolved into TileMapLayer payloads, one per terrain.

    Two layers, not one, because they are two terrains and a TileMapLayer holds
    a single tile per coordinate: a wall drawn over a floor cell would evict the
    floor under it, and the moment anything is destructible that hole is
    visible.

    Walls resolve with ``outside=True`` — beyond the level bounds is solid rock,
    not open air, and telling the mask otherwise puts an outward-facing edge
    around the entire perimeter.

    ``wall_fill`` paints EVERY non-floor cell rather than the one-cell ring, and
    it is the default for the reason above: a ring leaves the rest of the map
    empty, so the wall has no rock behind it and the floor layer's own carved
    edge is the only boundary — drawn twice, once by each layer, which is what
    made corridors render at half their width.
    """
    region = tuple(level["region"])
    out = [{
        "name": floor_name,
        "terrain": floor.name or floor_name,
        "cells": autotile.resolve([tuple(c) for c in level["floor"]], floor,
                                  region=region),
        "unmapped": autotile.unmapped([tuple(c) for c in level["floor"]], floor,
                                      region=region),
    }]
    if wall is not None:
        wall_cells = [tuple(c) for c in
                      (level.get("solid") if wall_fill and level.get("solid")
                       else level["walls"])]
        out.append({
            "name": wall_name,
            "terrain": wall.name or wall_name,
            "cells": autotile.resolve(wall_cells, wall, region=region,
                                      outside=True),
            "unmapped": autotile.unmapped(wall_cells, wall, region=region,
                                          outside=True),
        })
    return out
