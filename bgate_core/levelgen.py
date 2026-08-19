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


# ---------------------------------------------------------------------------
# Elevation
# ---------------------------------------------------------------------------
# A RAISED CELL IS SCENERY UNTIL SOMETHING CAN WALK ONTO IT. The isometric
# block gave levels height and gave the player nothing: `connected` asks
# whether the floor is one region four-directionally, which is exactly the
# question that stops meaning anything the moment two adjacent cells sit at
# different heights. A terrace nobody can reach and a wall are the same object.
#
# So height comes with its own reachability, and with the thing that makes the
# height crossable: a RAMP. A ramp is a floor cell at height h that also
# connects to the neighbour in ONE direction at h-1 — the tile art slopes that
# way, and the walk rule and the drawing agree because both read this field.
#
# Corridors stay on the base plane and rooms are what rise. That is a design
# choice and worth stating: it means every raised room is entered through its
# own doorways, so a ramp goes where a corridor already meets it, and no
# corridor ever needs a slope running along its length.

#: (dx, dy) per ramp direction name. These are CELL directions, which the
#: isometric projection renders as the four diagonals — the same mapping
#: `tilemask` carves the diamond's edges with.
RAMP_DIRS = {"n": (0, -1), "e": (1, 0), "s": (0, 1), "w": (-1, 0)}


def _height_of(heights: dict, cell) -> int:
    return int(heights.get(tuple(cell), 0))


def step_ok(a, b, heights: dict, ramps: dict) -> bool:
    """May a walker move between these two adjacent cells?

    Same height is always fine. A height change is fine ONLY across a ramp
    facing that way — which is what stops a generator from calling a level
    connected because two rooms happen to touch at different altitudes.
    """
    a, b = tuple(a), tuple(b)
    ha, hb = _height_of(heights, a), _height_of(heights, b)
    if ha == hb:
        return True
    high, low = (a, b) if ha > hb else (b, a)
    if abs(ha - hb) != 1:
        return False
    facing = ramps.get(high)
    if not facing:
        return False
    dx, dy = RAMP_DIRS[facing]
    return (high[0] + dx, high[1] + dy) == low


def reachable(floor, heights: dict, ramps: dict, start=None) -> set:
    """Every cell a walker can get to, obeying height and ramps."""
    floor = {tuple(c) for c in floor}
    if not floor:
        return set()
    start = tuple(start) if start is not None else next(iter(sorted(floor)))
    if start not in floor:
        return set()
    seen, stack = {start}, [start]
    while stack:
        x, y = stack.pop()
        for p in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if p in floor and p not in seen and step_ok((x, y), p, heights,
                                                        ramps):
                seen.add(p)
                stack.append(p)
    return seen


def terrace(level: dict, *, seed: int = 0, levels: int = 2,
            raised: float = 0.35) -> dict:
    """Give a flat plan its heights and the ramps that make them walkable.

    Rooms rise, corridors stay on the base plane, and every cell where a
    raised room meets the corridor that serves it becomes a ramp facing down
    into it. Built that way the level is reachable BY CONSTRUCTION rather than
    checked afterwards and re-rolled — the same discipline the side-scroller's
    segments follow.

    Returns the level with ``heights``, ``ramps`` and a height-aware
    ``connected``; the flat ``floor`` set is untouched, so every consumer that
    does not care about elevation keeps working.
    """
    if levels < 1:
        raise LevelError(f"{levels} levels is not a level")
    rng = random.Random(seed ^ 0x5EED)
    floor = {tuple(c) for c in level["floor"]}
    rooms = level.get("rooms") or []

    room_cells: list[set] = []
    for room in rooms:
        x, y, w, h = room["x"], room["y"], room["w"], room["h"]
        room_cells.append({(cx, cy) for cx in range(x, x + w)
                           for cy in range(y, y + h)} & floor)

    heights: dict = {}
    if levels > 1:
        for cells in room_cells:
            if not cells or rng.random() > raised:
                continue
            lift = rng.randint(1, levels - 1)
            for c in cells:
                heights[c] = lift

    # A RAMP WHERE THE GROUND CHANGES, facing DOWN the step. Every raised cell
    # with a lower neighbour is a candidate; taking them all would terrace the
    # whole rim into a slope, so one per lower neighbour DIRECTION per room is
    # enough to enter by and keeps the room's outline reading as a ledge.
    ramps: dict = {}
    for cells in room_cells:
        chosen: set = set()
        for cell in sorted(cells):
            h = _height_of(heights, cell)
            if not h:
                continue
            for name, (dx, dy) in RAMP_DIRS.items():
                below = (cell[0] + dx, cell[1] + dy)
                if below in floor and _height_of(heights, below) == h - 1:
                    if name in chosen:
                        continue
                    ramps[cell] = name
                    chosen.add(name)
                    break

    out = dict(level)
    out["heights"] = {f"{x},{y}": h for (x, y), h in sorted(heights.items())}
    out["ramps"] = {f"{x},{y}": d for (x, y), d in sorted(ramps.items())}
    out["levels"] = levels
    spawn = tuple(level["spawn"]) if level.get("spawn") else None
    got = reachable(floor, heights, ramps, start=spawn)
    out["connected"] = len(got) == len(floor)
    out["unreachable"] = sorted(c for c in floor if c not in got)
    return out


# ---------------------------------------------------------------------------
# A route, not a partition
# ---------------------------------------------------------------------------
# BSP ANSWERS THE WRONG QUESTION FOR A DESIGNED FLOOR. Splitting a rectangle
# until the pieces are room-sized gives rooms that are all the same KIND of
# thing: every one is a box off a corridor, none is first or last, and nothing
# says which way a player is going. Shown a real tutorial floor, the designer
# described it as five main rooms with a side room and drew the route through
# them — which is a sequence with branches, and no partition of a rectangle
# has a sequence in it.
#
# So this lays a CHAIN: rooms in order from entrance to exit, each connected to
# the next, with side rooms hung off the chain as optional stops. Spawn is the
# first room and exit the last, by construction rather than by picking the two
# farthest apart afterwards.


def plan_path(width: int, height: int, *, seed: int = 0, rooms: int = 5,
              side_rooms: int = 1, room_w: int = 11, room_h: int = 11,
              corridor_width: int = 3, margin: int = 2,
              jitter: float = 0.45) -> dict:
    """A level as a ROUTE through rooms, with optional side rooms off it.

    ``rooms`` is how many the critical path visits; ``side_rooms`` how many
    hang off it. The result has the same shape `plan` returns, plus
    ``main_path`` (the room indices in walking order) and ``side`` (the ones
    that are optional), so a caller can dress the route differently from the
    detour — which is the distinction a floor plan is actually made of.
    """
    if rooms < 2:
        raise LevelError(f"{rooms} rooms is not a route")
    rng = random.Random(seed)
    span = width - 2 * margin
    if span < rooms * 6:
        raise LevelError(
            f"a {width}-wide level cannot hold {rooms} rooms in a row — it "
            f"needs about {rooms * 6 + 2 * margin}")

    # ROOMS PACK THE PLATE. A chain of rooms strung across the middle leaves
    # the rest of the floor as dead grey, and a building does not have dead
    # space in it — an office floor is rooms wall to wall, and the route is
    # which ones you pass through, not where they sit. So the plate is cut
    # into a grid of rooms that FILL it, sharing their walls the way rooms
    # in a building do, and the route is a walk over that grid.
    cols = max(2, min(rooms, (width - 2 * margin) // 9))
    rows = max(1, -(-(rooms + max(0, side_rooms)) // cols))
    cw = (width - 2 * margin) // cols
    chh = (height - 2 * margin) // rows
    if cw < 6 or chh < 6:
        raise LevelError(
            f"a {width}x{height} plate cannot hold {rooms} rooms plus "
            f"{side_rooms} side — each needs about 6x6 with its walls")
    grid: list[Rect] = []
    for r in range(rows):
        for c in range(cols):
            x = margin + c * cw
            y = margin + r * chh
            # one cell of the gap belongs to the shared wall between rooms
            grid.append(Rect(x, y, cw - 1, chh - 1))

    # THE ROUTE IS A WALK OVER THE GRID: along the top row, down, back along
    # the next, so consecutive rooms are always neighbours and the corridor
    # between them is a doorway rather than a trek.
    order = []
    for r in range(rows):
        band = list(range(r * cols, min(len(grid), (r + 1) * cols)))
        order += band if r % 2 == 0 else band[::-1]
    placed = [grid[i] for i in order[:rooms + max(0, side_rooms)]]

    main = list(range(min(rooms, len(placed))))
    # SIDE ROOMS HANG OFF THE CHAIN, never between two links of it: a detour
    # you must pass through is not a detour, it is the route. With a packed
    # grid they are simply the rooms the walk does not need.
    side: list[int] = list(range(len(main), len(placed)))
    for _ in range(0):
        host = rng.randrange(1, rooms - 1) if rooms > 2 else 0
        base = placed[host]
        # A SIDE ROOM THAT DOES NOT FIT IS NOT A SIDE ROOM, and silently
        # dropping it left the floor a bare chain — the author asked for five
        # main rooms AND a side room. Try above, then below, shrinking to
        # whatever the band actually has room for rather than giving up on
        # the first miss.
        rw = max(5, min(room_w - 3, width - 2 * margin))
        spot = None
        for gap in (3, 2):
            over = base.y - gap - margin
            under = height - margin - (base.y + base.h + gap)
            if over >= 5:
                spot = (min(room_h - 3, over), base.y - gap - min(room_h - 3, over))
                break
            if under >= 5:
                spot = (min(room_h - 3, under), base.y + base.h + gap)
                break
        if spot is None:
            continue
        rh, y = spot
        x = max(margin, min(width - margin - rw, base.x))
        side.append(len(placed))
        placed.append(Rect(x, y, rw, rh))

    floor: set = set()
    for r in placed:
        floor |= r.cells()
    corridors = []
    spread = tuple(range(corridor_width))

    def join(a: Rect, b: Rect):
        # A DOORWAY, NOT A TREK. Two rooms that already share a wall are
        # joined by cutting an opening in it; running a corridor between
        # their centres instead carved a channel straight through both, which
        # is how a packed floor turned back into a chain with holes in it.
        gap_x = (a.x + a.w < b.x) or (b.x + b.w < a.x)
        gap_y = (a.y + a.h < b.y) or (b.y + b.h < a.y)
        left, right = (a, b) if a.x <= b.x else (b, a)
        top, bottom = (a, b) if a.y <= b.y else (b, a)
        # A DOOR IS A DOOR, NOT A MISSING WALL. At full corridor width and
        # always centred, every opening landed on the same line and the row
        # of them merged into one continuous channel — the packed floor read
        # as open plan with pillars. Two cells, placed anywhere along the
        # shared edge, is an opening you walk through.
        w = max(1, min(2, corridor_width))
        if not gap_y and abs((left.x + left.w) - right.x) <= 2:
            lo = max(left.y, right.y)
            hi = min(left.y + left.h, right.y + right.h)
            if hi - lo >= w + 2:
                mid = rng.randint(lo + 1, hi - w - 1)
                cells = {(x, y)
                         for x in range(left.x + left.w - 1, right.x + 1)
                         for y in range(mid, mid + w)}
                corridors.append({"from": list(a.center), "to": list(b.center),
                                  "kind": "door", "cells": len(cells)})
                return cells
        if not gap_x and abs((top.y + top.h) - bottom.y) <= 2:
            lo = max(top.x, bottom.x)
            hi = min(top.x + top.w, bottom.x + bottom.w)
            if hi - lo >= w + 2:
                mid = rng.randint(lo + 1, hi - w - 1)
                cells = {(x, y)
                         for y in range(top.y + top.h - 1, bottom.y + 1)
                         for x in range(mid, mid + w)}
                corridors.append({"from": list(a.center), "to": list(b.center),
                                  "kind": "door", "cells": len(cells)})
                return cells
        pa, pb = a.center, b.center
        cells, path = _elbow(pa, pb, rng, width=corridor_width)
        corridors.append({"from": list(pa), "to": list(pb), "kind": "corridor",
                          "path": path, "cells": len(cells)})
        return cells

    for i in range(len(main) - 1):
        floor |= join(placed[i], placed[i + 1])
    # SIDE ROOMS CHAIN. A detour that leads to another detour is the shape a
    # floor plan actually has — a store room off the break room, the server
    # closet behind IT — and hanging every side room directly off the route
    # gives a comb instead. Each one attaches to the nearest room already
    # reachable, main or side, so depth appears where the geometry allows it.
    reached = list(main)
    for idx in side:
        here = placed[idx].center
        host = min(reached,
                   key=lambda h: abs(placed[h].center[0] - here[0])
                   + abs(placed[h].center[1] - here[1]))
        floor |= join(placed[idx], placed[host])
        reached.append(idx)

    floor = {c for c in floor
             if 0 <= c[0] < width and 0 <= c[1] < height}
    walls = wall_ring(floor)
    solid = {(x, y) for y in range(height) for x in range(width)} - floor
    ordered = placed
    return {
        "seed": seed, "width": width, "height": height,
        "region": [0, 0, width, height],
        "rooms": [r.as_dict() for r in ordered],
        "corridors": corridors,
        "floor": sorted(floor, key=lambda c: (c[1], c[0])),
        "walls": sorted(walls, key=lambda c: (c[1], c[0])),
        "solid": sorted(solid, key=lambda c: (c[1], c[0])),
        "connected": connected(floor),
        "spawn": list(ordered[0].center),
        "exit": list(ordered[len(main) - 1].center),
        "main_path": main,
        "side": side,
    }
