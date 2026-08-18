"""Dressing a generated level: where props go, and where they must not.

A dungeon of empty rooms reads as a floor plan. Props are what make it a
place — and they are also the fastest way to make a generated level
unplayable, because a barrel in the wrong cell walls off half the map and
nothing about the picture says so.

THE FIRST VERSION SPRINKLED UNIFORMLY AT RANDOM, and the measurements said
exactly what that looks like: all nineteen clutter props stranded in fully
open floor, torches standing next to each other, and every room dressed to
the same 14% whatever its size or purpose. Random placement over eligible
cells is not dressing, it is noise with a density dial.

So placement is by ROLE, and it is mostly a set of refusals:

  * TORCHES mount on the WALL, not on the floor beside it, and are spaced
    along each wall run so they read as lighting rather than as clutter.
  * CLUTTER hugs the architecture — candidates ranked by how enclosed they
    are, corners first, open floor refused outright.
  * The CENTRE of a room is left alone unless the room is a dead end and big
    enough to deserve a feature, because the middle is where the game happens.
  * The straight runs between doorways stay clear, so a room you walk through
    does not become an obstacle course.
  * Nothing on the spawn, the exit, or a doorway.
  * Nothing that disconnects the level, checked by actually flood filling the
    walkable set rather than by reasoning about it.

That last one is the gate that matters, and `levelgen.connected` already
answers it: a level whose floor is no longer one region has been broken by
its own decoration.

Pure data. Nothing here draws, generates or writes — `plan` returns where
things belong and the caller puts pixels there.
"""
from __future__ import annotations

import random
from typing import Iterable, Optional, Sequence

#: The coarse role of a prop. `mount` below is the precise version; this stays
#: because callers narrow by role — "give me lighting, no clutter".
KINDS = ("wall", "floor", "centre", "door", "portal")

#: Props that block movement, by kind, when a type does not say. A type's own
#: `solid` wins; this is only the fallback.
DEFAULT_SOLID = {"wall": False, "floor": True, "centre": True,
                 "door": False, "portal": False}

#: WHERE a prop attaches, which is the constraint placement actually obeys.
#:
#:   wall     on a wall cell along a straight run, facing into the room
#:   corner   on a wall CORNER — the cells a wall mount is refused, and exactly
#:            where a cobweb belongs. The same geometry, wanted instead of
#:            avoided.
#:   floor    clutter against the architecture
#:   pillar   architecture, not clutter: a colonnade down a hall, placed in
#:            pairs so the room reads as built rather than littered
#:   overlay  a decal you walk OVER — no collision, allowed on the routes,
#:            because a bloodstain is not an obstacle
#:   centre   the one feature piece of a room
#:   door     in a doorway, across the opening
#:   portal   the way in and the way out, on the spawn and exit cells
MOUNTS = ("wall", "corner", "floor", "pillar", "overlay", "centre",
          "door", "portal")

#: Draw order, low to high. A decal belongs UNDER everything and a prop over
#: it, and a TileMapLayer holds one tile per coordinate — so this is not
#: cosmetic, it is what lets a crack in the floor and a barrel share a cell.
LAYERS = ("decals", "props")

#: Which way a wall faces INTO its room, and whether that face can be seen.
#:
#: This is the geometry a top-down level cannot fudge. A wall cell north of the
#: room turns its inner face toward the camera — that face is visible and can
#: carry a torch. The wall cell SOUTH of the room is the one you are looking at
#: the back of: its inner face points away, and anything mounted there is
#: behind the masonry. In the ``faces`` convention here — the direction from the
#: wall cell into the room — that is ``faces == "n"``, and nothing mounts on it.
#:
#:   faces "s"  the north wall, inner face toward the camera — front view
#:   faces "e"  the west wall, seen side-on — a side or angled sprite
#:   faces "w"  the east wall, side-on and mirrored
#:   faces "n"  the south wall, seen from BEHIND — refused
MOUNTABLE_SIDES = ("s", "e", "w")

#: A prop TYPE: the sprite's own constraints, declared rather than assumed.
#:
#: ``sides`` is the whole point. An ANGLED or side-view torch only reads on a
#: vertical wall, so it declares ("e", "w") and is refused on a horizontal one —
#: putting a three-quarter sprite on the north wall is the "floaty prop" defect,
#: and no amount of placement logic fixes it because the ART cannot go there.
#: A front-facing sconce is the opposite: ("s",) only.
#:
#:   mount     which cell it occupies and by what rule (see MOUNTS)
#:   kind      the coarse role, for callers narrowing by role
#:   layer     which TileMapLayer it draws on (see LAYERS)
#:   sides     the inward facings this sprite can be drawn at. Any mount drawn
#:             with the WALL-FACE camera needs it, not just `wall`: a door or
#:             an arch drawn as a flat elevation only reads on the wall whose
#:             face turns toward the camera. On the other three you are looking
#:             at that wall from above or behind.
#:   drawn     the side the art is actually drawn for
#:   mirror    whether flipping horizontally is legitimate for it
#:   solid     whether it blocks movement — gated on a flood fill either way
#:   purposes  which room purposes it belongs in, or None for any. A chest in
#:             a corridor is not a reward, it is scenery.
#:   once      at most one per room. Seven chests across four vaults is not a
#:             treasure room, it is a warehouse.
#:   footprint how much of its cell the ART may fill, 0-1. This is the
#:             difference between a prop that sits ON a floor and one that
#:             replaces it. Measured on a set that was judged by eye: the one
#:             prop called good covered 63% of its cell and touched its border
#:             on 3% of the edge; the ones called "too big" covered 84-91% and
#:             touched 56-62%. A prop drawn edge to edge reads as a tile, not
#:             as an object standing on one.
#:   size      (w, h) IN CELLS, and the reason props stop looking squat. A
#:             pillar is one wide and two high; drawing it into a 1x1 tile is
#:             what makes everything read as flat. The tile is placed at the
#:             prop's GROUND cell and drawn upward from there.
#:   anim      {frames, fps} for a prop that LOOPS — a torch flickers, a portal
#:             turns. Godot plays this off the tileset with no node involved.
#:   states    {name: frames} for a prop that does not loop but CHANGES: a chest
#:             is shut (1 frame), opening (6), then open (1). The frame counts
#:             differ per state and that is the point — "opening" is a run and
#:             the two ends are stills. An ambient loop and a state machine are
#:             not the same mechanism and cannot share one, so a type declares
#:             one or the other and `art_spec` reports which.
PROP_TYPES: dict = {
    # -- lighting, and the constraint that motivates the whole table ---------
    "torch":   {"mount": "wall", "kind": "wall", "sides": ("e", "w"),
                "drawn": "e", "mirror": True, "solid": False,
                "size": (1, 1), "footprint": 0.55,
                "anim": {"frames": 4, "fps": 8}},
    "sconce":  {"mount": "wall", "kind": "wall", "sides": ("s",),
                "drawn": "s", "mirror": False, "solid": False,
                "size": (1, 1), "anim": {"frames": 4, "fps": 8}},
    "banner":  {"mount": "wall", "kind": "wall", "sides": ("s",),
                "drawn": "s", "mirror": False, "solid": False,
                "purposes": ("vault", "entry"), "size": (1, 2),
                "anim": {"frames": 4, "fps": 4}},
    "shelf":   {"mount": "wall", "kind": "wall", "sides": ("s",),
                "drawn": "s", "mirror": False, "solid": False,
                "purposes": ("store",)},
    # -- the corners a wall mount refuses are where these belong ------------
    "cobweb":  {"mount": "corner", "kind": "wall", "mirror": True,
                "solid": False, "size": (1, 1)},
    # -- clutter -------------------------------------------------------------
    "barrel":  {"mount": "floor", "kind": "floor", "solid": True,
                "mirror": True, "footprint": 0.5},
    "crate":   {"mount": "floor", "kind": "floor", "solid": True,
                "mirror": True, "footprint": 0.5},
    "rubble":  {"mount": "floor", "kind": "floor", "solid": False,
                "mirror": True, "footprint": 0.6},
    "bones":   {"mount": "floor", "kind": "floor", "solid": False,
                "mirror": True, "footprint": 0.65},
    "chest":   {"mount": "floor", "kind": "floor", "solid": True,
                "mirror": False, "purposes": ("vault",), "once": True,
                "size": (1, 1), "footprint": 0.66,
                "states": {"shut": 1, "opening": 6, "open": 1}},
    # -- architecture, which is not clutter ---------------------------------
    "pillar":  {"mount": "pillar", "kind": "floor", "solid": True,
                "mirror": False, "purposes": ("hall", "vault"),
                "size": (1, 2)},
    # -- decals: walked over, so they may sit on the routes ------------------
    "crack":   {"mount": "overlay", "kind": "floor", "layer": "decals",
                "solid": False, "mirror": True},
    "stain":   {"mount": "overlay", "kind": "floor", "layer": "decals",
                "solid": False, "mirror": True},
    "drain":   {"mount": "overlay", "kind": "floor", "layer": "decals",
                "solid": False, "mirror": False},
    # -- the feature piece ---------------------------------------------------
    "altar":   {"mount": "centre", "kind": "centre", "solid": True,
                "mirror": False, "size": (2, 2)},
    "well":    {"mount": "centre", "kind": "centre", "solid": True,
                "mirror": False, "size": (2, 2),
                "anim": {"frames": 4, "fps": 3}},
    "statue":  {"mount": "centre", "kind": "centre", "solid": True,
                "mirror": False, "size": (1, 2)},
    # -- structure: the openings, and the way in and out ---------------------
    "door":    {"mount": "door", "kind": "door", "solid": False,
                "mirror": False, "size": (1, 1), "sides": ("s",),
                "footprint": 1.0,
                "states": {"shut": 1, "opening": 6, "open": 1}},
    "arch":    {"mount": "door", "kind": "door", "solid": False,
                "mirror": False, "size": (1, 1), "sides": ("s",),
                "footprint": 1.0},
    "stairs_up":   {"mount": "portal", "kind": "portal", "solid": False,
                    "mirror": False, "at": "spawn", "size": (2, 2)},
    "stairs_down": {"mount": "portal", "kind": "portal", "solid": False,
                    "mirror": False, "at": "exit", "size": (2, 2),
                    "anim": {"frames": 6, "fps": 6}},
}

#: What a level is dressed with when the caller names nothing.
#:
#: THIS LIST IS SHORT ON PURPOSE. Every type here was rendered into a real
#: level and read at 1x on a dark floor; the ones left out did not. A prop
#: whose subject cannot survive being drawn from directly overhead — a column
#: becomes a circle, a statue becomes a plaque — is not a default no matter how
#: well the placement rules handle it, and shipping it means every generated
#: level carries something that looks wrong.
#:
#: The others stay DECLARED, because their placement rules are correct and the
#: art is the part that is missing. Ask for them by name once they have art
#: that reads.
DEFAULT_TYPES = ("torch", "barrel", "crate", "rubble", "bones",
                 "chest", "door", "stairs_up", "stairs_down")


def _is_corner(cell, wall_ring: set) -> bool:
    """Does this wall cell turn a corner rather than lie along a straight run?

    A sprite bolted to a corner reads as bolted to nothing: the face it needs
    is interrupted. A cell is on a run when its two wall neighbours are opposite
    each other; when they are perpendicular, it is the corner itself.
    """
    x, y = cell
    ns = ((x, y - 1) in wall_ring, (x, y + 1) in wall_ring)
    ew = ((x - 1, y) in wall_ring, (x + 1, y) in wall_ring)
    return any(ns) and any(ew)


def layer_of(name: str) -> str:
    """Which TileMapLayer a type draws on. Decals go under, props over."""
    return prop_type(name).get("layer", "props")


def allows(name: str, purpose: str) -> bool:
    """Does this type belong in a room with this purpose?

    A chest in a corridor is not a reward, it is scenery, and a shelf in a
    thoroughfare is not a storeroom. Declared per type so the placer does not
    have to know what any particular prop means.
    """
    want = prop_type(name).get("purposes")
    return not want or purpose in want


def door_clusters(door_cells) -> list:
    """Doorway cells grouped into OPENINGS, with the axis each one spans.

    A two-cell corridor arrives as two adjacent cells and that is ONE doorway,
    not two — the same conflation that made every dead end read as a
    thoroughfare until `room_role` started clustering.
    """
    left = {tuple(c) for c in door_cells}
    out = []
    while left:
        stack, group = [left.pop()], []
        while stack:
            x, y = stack.pop()
            group.append((x, y))
            for n in ((x, y - 1), (x + 1, y), (x, y + 1), (x - 1, y)):
                if n in left:
                    left.discard(n)
                    stack.append(n)
        xs = {c[0] for c in group}
        out.append({"cells": sorted(group),
                    # a doorway spanning several columns is a horizontal
                    # opening, so the door across it stands vertically
                    "axis": "h" if len(xs) > 1 else "v"})
    return sorted(out, key=lambda g: g["cells"][0])


def prop_type(name: str) -> dict:
    """A declared type, or a refusal naming what is on offer."""
    try:
        return PROP_TYPES[name]
    except KeyError:
        raise PropError(
            f"unknown prop type {name!r}; declared types are "
            f"{sorted(PROP_TYPES)}") from None


def mountable(spec: dict, faces: str) -> bool:
    """Can this sprite mount on a wall whose inner face points ``faces``?"""
    if faces not in MOUNTABLE_SIDES:
        return False                      # the wall's back — never
    allowed = spec.get("sides") or MOUNTABLE_SIDES
    if faces in allowed:
        return True
    return bool(spec.get("mirror")) and _OPPOSITE.get(faces) in allowed

#: How enclosed a cell must be before clutter may sit there. Counted over the
#: EIGHT neighbours: open floor scores 0, a wall edge 3, an inside corner 5.
#: Below this the prop is stranded in the middle of the room, which is the
#: defect this threshold exists to end.
MIN_ENCLOSURE = 3

#: Unit steps, for asking which way a wall faces into its room.
_DIR = {"n": (0, -1), "e": (1, 0), "s": (0, 1), "w": (-1, 0)}

_OPPOSITE = {"e": "w", "w": "e", "n": "s", "s": "n"}


class PropError(ValueError):
    """A placement request that cannot be satisfied honestly."""


def doorways(floor: set, rooms: Sequence[dict]) -> set:
    """Floor cells where a corridor meets a room — never block these.

    A room cell is a doorway when it touches floor lying OUTSIDE every room:
    that is the corridor arriving. Blocking one is how a generated level
    loses a room while still looking fine.
    """
    in_room = set()
    for r in rooms:
        for x in range(r["x"], r["x"] + r["w"]):
            for y in range(r["y"], r["y"] + r["h"]):
                in_room.add((x, y))
    corridor = floor - in_room
    out = set()
    for (x, y) in in_room:
        for n in ((x, y - 1), (x + 1, y), (x, y + 1), (x - 1, y)):
            if n in corridor:
                out.add((x, y))
                out.add(n)
    return out


def room_role(room: dict, floor: set, doors: set) -> dict:
    """What this room IS, which decides how it gets dressed.

    A dead end is somewhere you arrive and stop — it earns a feature. A
    thoroughfare is somewhere you pass through, and clutter down the middle of
    it is an obstacle course.
    """
    own = [c for c in doors
           if room["x"] <= c[0] < room["x"] + room["w"]
           and room["y"] <= c[1] < room["y"] + room["h"]]
    # COUNT DOORWAYS, NOT CELLS. A two-cell-wide corridor arrives as two
    # adjacent door cells, and counting cells made every dead-end room look
    # like a thoroughfare the moment corridors got wider than one: vaults went
    # to zero and the whole level classified as halls.
    ways, left = 0, set(own)
    while left:
        stack, seen = [left.pop()], 1
        while stack:
            x, y = stack.pop()
            for n in ((x, y - 1), (x + 1, y), (x, y + 1), (x - 1, y)):
                if n in left:
                    left.discard(n)
                    stack.append(n)
                    seen += 1
        ways += 1
    area = room["w"] * room["h"]
    return {"doors": ways, "door_cells": own, "area": area,
            "dead_end": ways <= 1, "big": area >= 40}


#: What a room is FOR. Decoration ranked by enclosure gave every room the same
#: treatment, which is why a dressed level still read as arbitrary: nothing on
#: the map was placed because of what happens there.
#:   entry — holds the spawn: lit, and otherwise left alone
#:   vault — a dead end big enough to be worth walking to: the payload
#:   hall  — a room you cross: COVER, which is the only prop with a job
#:   store — small and off the path: junk against the walls
PURPOSES = ("entry", "vault", "hall", "store")


def room_purpose(room: dict, role: dict, level: dict) -> str:
    """Which of `PURPOSES` this room is, from the layout alone."""
    spawn = tuple(level.get("spawn") or ())
    if spawn and (room["x"] <= spawn[0] < room["x"] + room["w"]
                  and room["y"] <= spawn[1] < room["y"] + room["h"]):
        return "entry"
    if role["dead_end"] and role["big"]:
        return "vault"
    if role["doors"] >= 2 and role["area"] >= 48:
        return "hall"
    return "store"


def cover(door_cells, floor: set, cells) -> list:
    """Interior cells that break a sight line without blocking a route.

    A prop against a wall is scenery. A prop one cell OFF the line between two
    doors is cover: you can still walk the room, but you cannot see or shoot
    straight across it. This is the only placement rule here with a gameplay
    reason, and it is the one that was missing.
    """
    lines = walk_lines(door_cells, floor)
    if not lines:
        return []
    inside = set(cells)
    out = []
    for (x, y) in sorted(lines):
        for n in ((x, y - 1), (x + 1, y), (x, y + 1), (x - 1, y)):
            if n in inside and n not in lines and enclosure(n, floor) == 0:
                out.append(n)
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def enclosure(cell, floor: set) -> int:
    """Count of a cell's EIGHT neighbours that are not floor.

    The whole difference between clutter that looks placed and clutter that
    looks dropped: 0 is the middle of a room, 3 is against a wall, 5 is an
    inside corner.
    """
    x, y = cell
    ring = ((x, y - 1), (x + 1, y - 1), (x + 1, y), (x + 1, y + 1),
            (x, y + 1), (x - 1, y + 1), (x - 1, y), (x - 1, y - 1))
    return sum(1 for n in ring if n not in floor)


def walk_lines(door_cells: Sequence[tuple], floor: set) -> set:
    """Cells on the straight runs between a room's doorways — the path a
    player actually takes through it, kept clear."""
    keep = set()
    for i, a in enumerate(door_cells):
        for b in door_cells[i + 1:]:
            x, y = a
            while x != b[0]:
                x += 1 if b[0] > x else -1
                if (x, y) in floor:
                    keep.add((x, y))
            while y != b[1]:
                y += 1 if b[1] > y else -1
                if (x, y) in floor:
                    keep.add((x, y))
    return keep


def plan(level: dict, *, seed: int = 0, density: float = 0.12,
         types: Optional[Sequence[str]] = None,
         kinds: Optional[Sequence[str]] = None,
         solid: Optional[dict] = None,
         reserved: Iterable[tuple] = (),
         torch_spacing: int = 4, view: str = "",
         walls: Optional[Iterable[tuple]] = None) -> dict:
    """Where props go. ``{props, skipped, checks}``.

    ``density`` is the share of a room's cells that may take clutter — a dial,
    not a promise: every candidate still has to clear the enclosure threshold,
    the walk lines, and the connectivity gate.
    """
    floor = {tuple(c) for c in level["floor"]}
    wall_ring = {tuple(c) for c in (walls if walls is not None
                                   else level.get("walls") or ())}
    rooms = level.get("rooms") or []
    # TYPES are what the caller actually has sprites for, and each one declares
    # its own constraints. `kinds` still narrows by role, so a caller can ask
    # for lighting and no clutter without naming types.
    names = tuple(types or DEFAULT_TYPES)
    for n in names:
        prop_type(n)                       # refuse an unknown type up front
    kinds = tuple(kinds or KINDS)
    names = tuple(n for n in names if PROP_TYPES[n]["kind"] in kinds)
    # A MOUNT THE VIEW DOES NOT HAVE IS NOT PLACED. A colonnade reads as depth
    # and a side-scroller has none; a ceiling mount is meaningless from above.
    # Refused loudly rather than dropped, because a level quietly missing a
    # whole class of prop looks like a density problem.
    from bgate_core import gameview as _view

    seen_view = _view.normalise(view)
    for n in names:
        if not _view.supports(seen_view, PROP_TYPES[n]["mount"]):
            raise PropError(
                f"{n} mounts on {PROP_TYPES[n]['mount']!r}, which means "
                f"nothing in a {seen_view} level — that view has "
                f"{list(_view.mounts(seen_view))}")
    by_mount = {m: tuple(n for n in names if PROP_TYPES[n]["mount"] == m)
                for m in MOUNTS}
    wall_types = by_mount["wall"]
    floor_types = by_mount["floor"]
    centre_types = by_mount["centre"]
    solid_map = {**DEFAULT_SOLID, **(solid or {})}
    rng = random.Random(seed)

    doors = doorways(floor, rooms)
    keep_clear = set(doors)
    for spot in (level.get("spawn"), level.get("exit")):
        if spot:
            keep_clear.add(tuple(spot))
    keep_clear |= {tuple(c) for c in reserved}

    placed: list[dict] = []
    taken: set = set()
    skipped = {"doorway": 0, "walk_line": 0, "too_open": 0,
               "disconnects": 0, "spacing": 0, "back_wall": 0,
               "corner": 0, "no_side": 0, "wrong_purpose": 0, "occupied": 0}
    purposes: dict = {}

    def solid_cells() -> set:
        return {(q["x"], q["y"]) for q in placed if q["solid"]}

    def _fits(pool, purpose):
        """The types in `pool` that belong in a room with this purpose."""
        ok = [n for n in pool if allows(n, purpose)]
        if pool and not ok:
            skipped["wrong_purpose"] += 1
        return ok

    # -- portals: the way in and the way out --------------------------------
    #
    # Spawn and exit were coordinates that everything else was told to avoid,
    # and nothing ever marked them. A generated level you cannot see the exit
    # of is a maze, not a level.
    for name in by_mount["portal"]:
        spot = level.get(prop_type(name).get("at") or "spawn")
        if not spot:
            continue
        at = tuple(spot)
        if at not in floor or at in taken:
            skipped["occupied"] += 1
            continue
        placed.append({"type": name, "kind": "portal", "mount": "portal",
                       "x": at[0], "y": at[1], "solid": False,
                       "faces": "", "mirror": False,
                       "label": f"{name}_{prop_type(name).get('at', 'spawn')}"})
        taken.add(at)

    # -- doors: on the WALL the opening cuts through --------------------
    #
    # A door drawn as a flat elevation and placed on the doorway's FLOOR cell
    # reads as a door lying in the middle of the room — which is exactly how it
    # rendered. A doorway in a top-down level is a gap in a wall, so the door
    # belongs on the masonry beside the gap, and only on the wall whose inner
    # face turns toward the camera. On the other three you are looking at that
    # wall from above or behind and a front elevation is meaningless there.
    if by_mount["door"] and wall_ring:
        for group in door_clusters(doors):
            opening = set(group["cells"])
            # the wall cells flanking this opening, and which way each faces in
            flank = []
            for (x, y) in sorted(opening):
                for d, (dx, dy) in _DIR.items():
                    w = (x + dx, y + dy)
                    if w in wall_ring and w not in taken:
                        # `d` points from the floor cell to the wall, so the
                        # wall faces back the other way
                        flank.append((w, _OPPOSITE[d]))
            if not flank:
                skipped["occupied"] += 1
                continue
            pool = [n for n in by_mount["door"]]
            hung = False
            for cell, inward in flank:
                fits = [n for n in pool if mountable(prop_type(n), inward)]
                if not fits:
                    continue
                name = fits[rng.randrange(len(fits))]
                placed.append({"type": name, "kind": "door", "mount": "door",
                               "axis": group["axis"], "faces": inward,
                               "mirror": False,
                               "x": cell[0], "y": cell[1],
                               # NEVER solid: a door that blocks its own
                               # doorway severs the level, and the flood gate
                               # would then refuse every one of them
                               "solid": False,
                               "opening": [list(c) for c in sorted(opening)],
                               "label": f"{name}_{inward}"})
                taken.add(cell)
                hung = True
                break
            if not hung:
                # every wall around this opening faces a way the art cannot be
                # drawn at — counted, because a level with no doors at all
                # looks like the door type was never requested
                skipped["no_side"] += 1

    for room in rooms:
        role = room_role(room, floor, doors)
        cells = [(x, y)
                 for x in range(room["x"], room["x"] + room["w"])
                 for y in range(room["y"], room["y"] + room["h"])
                 if (x, y) in floor]
        if not cells:
            continue
        lines = walk_lines(role["door_cells"], floor)
        purpose = room_purpose(room, role, level)
        once_here: set = set()      # types capped at one per room
        purposes[purpose] = purposes.get(purpose, 0) + 1

        # -- wall mounts: ON the wall, on a face you can see ---------------
        if wall_types and wall_ring:
            room_cells = set(cells)
            sides: dict = {}
            for c in sorted(wall_ring):
                inward = next((d for d in ("n", "e", "s", "w")
                               if (c[0] + _DIR[d][0],
                                   c[1] + _DIR[d][1]) in room_cells), "")
                if not inward:
                    continue
                if inward not in MOUNTABLE_SIDES:
                    # the wall SOUTH of the room, seen from behind: its inner
                    # face points away from the camera and anything bolted to
                    # it is hidden by its own masonry
                    skipped["back_wall"] += 1
                    continue
                if _is_corner(c, wall_ring):
                    skipped["corner"] += 1
                    continue
                sides.setdefault(inward, []).append(c)
            border = [c for run in sides.values() for c in run]

            # A BUDGET, not just a spacing rule, and spread ROUND THE ROOM.
            # Greedy spacing over a sorted list put 13 torches on one room and
            # 5 on a 20-cell closet; capping the count then put 17 of 22 on the
            # west wall, because sorted order reaches it first and the budget
            # ran out there. So take them a side at a time.
            budget_lit = max(1, min(4, round(len(border) / 12))) if border else 0
            gap = max(torch_spacing, len(border) // max(budget_lit * 2, 1) or 1)
            lit: list[tuple] = []
            order = ("n", "e", "s", "w")
            turn = rng.randrange(4)
            queues = [(d, list(sides[d]))
                      for d in (order[(turn + i) % 4] for i in range(4))
                      if d in sides]
            for _, run in queues:         # start a third of the way along each
                if len(run) > 2:          # wall, not in its corner
                    del run[:len(run) // 3]
            while queues and len(lit) < budget_lit:
                for entry in list(queues):
                    if len(lit) >= budget_lit:
                        break
                    inward, run = entry
                    # only the types whose ART can face this way
                    fits = [n for n in _fits(wall_types, purpose)
                            if mountable(prop_type(n), inward)]
                    if not fits:
                        skipped["no_side"] += 1
                        run.clear()
                        continue
                    while run:
                        c = run.pop(0)
                        if any(abs(c[0] - o[0]) + abs(c[1] - o[1]) < gap
                               for o in lit):
                            skipped["spacing"] += 1
                            continue
                        if c in taken:
                            continue
                        name = fits[rng.randrange(len(fits))]
                        spec = prop_type(name)
                        step = _DIR[inward]
                        placed.append({
                            "type": name, "kind": spec["kind"],
                            "mount": "wall",
                            # THE WALL CELL. A wall prop drawn in the room beside
                            # its wall is a floating prop, and that is exactly
                            # what it looks like: the sprite has to be ON the
                            # masonry it is bolted to.
                            "x": c[0], "y": c[1],
                            "faces": inward,
                            "mirror": inward != spec.get("drawn", inward),
                            "lights": [c[0] + step[0], c[1] + step[1]],
                            "solid": bool(solid_map.get(spec["kind"],
                                                        spec.get("solid", False))),
                            "label": f"{name}_{inward}"})
                        lit.append(c)
                        taken.add(c)
                        break
                queues = [(d, q) for d, q in queues if q]

        # -- clutter: a FEW clusters, not one item in every corner ---------
        #
        # Ranking by enclosure alone put a barrel in all four corners of every
        # room, which is a pattern rather than a plan: it reads as mechanical
        # the moment you see two rooms side by side. So pick a small number of
        # ANCHORS among the most enclosed cells and pile 1-3 things around
        # each, leaving the other corners bare.
        # -- corners: the cells a wall mount is REFUSED ---------------------
        #
        # Same geometry, opposite verdict. A cobweb wants the corner a torch
        # cannot use, so the corner test earns its keep twice.
        corner_pool = _fits(by_mount["corner"], purpose)
        if corner_pool and wall_ring:
            room_cells = set(cells)
            corners = [c for c in sorted(wall_ring)
                       if _is_corner(c, wall_ring) and c not in taken
                       and any((c[0] + _DIR[d][0], c[1] + _DIR[d][1])
                               in room_cells for d in MOUNTABLE_SIDES)]
            for cell in rng.sample(corners, k=min(len(corners),
                                                  max(1, len(corners) // 3))):
                name = corner_pool[rng.randrange(len(corner_pool))]
                placed.append({"type": name, "kind": prop_type(name)["kind"],
                               "mount": "corner", "x": cell[0], "y": cell[1],
                               "faces": "", "solid": False,
                               "mirror": bool(prop_type(name).get("mirror")
                                              and rng.getrandbits(1)),
                               "label": f"{name}_corner"})
                taken.add(cell)

        # -- pillars: architecture, placed in PAIRS -------------------------
        #
        # The difference between a built room and a littered one. A colonnade
        # is symmetric about the room's axis, so pillars go in twos down the
        # long side and the hall reads as load-bearing rather than cluttered.
        pillar_pool = _fits(by_mount["pillar"], purpose)
        if pillar_pool and role["area"] >= 60:
            x0, y0 = room["x"], room["y"]
            w, h = room["w"], room["h"]
            inset_x, inset_y = max(1, w // 4), max(1, h // 4)
            pairs = []
            if w >= h:                       # colonnade down the long axis
                for i in range(1, max(2, w // 6) + 1):
                    px = x0 + (w * i) // (max(2, w // 6) + 1)
                    pairs.append(((px, y0 + inset_y), (px, y0 + h - 1 - inset_y)))
            else:
                for i in range(1, max(2, h // 6) + 1):
                    py = y0 + (h * i) // (max(2, h // 6) + 1)
                    pairs.append(((x0 + inset_x, py), (x0 + w - 1 - inset_x, py)))
            name = pillar_pool[rng.randrange(len(pillar_pool))]
            is_solid = bool(prop_type(name).get("solid", True))
            for pair in pairs:
                # BOTH or NEITHER — half a colonnade is worse than none
                if any(c not in floor or c in taken or c in keep_clear
                       or c in lines for c in pair):
                    continue
                if is_solid and not _connected(
                        floor - solid_cells() - set(pair)):
                    skipped["disconnects"] += 1
                    continue
                for c in pair:
                    placed.append({"type": name, "kind": "floor",
                                   "mount": "pillar", "x": c[0], "y": c[1],
                                   "faces": "", "mirror": False,
                                   "solid": is_solid,
                                   "label": f"{name}_colonnade"})
                    taken.add(c)

        # -- cover: the one prop kind placed for a reason ------------------
        cover_pool = _fits(floor_types, purpose)
        if cover_pool and purpose == "hall":
            spots = [c for c in cover(role["door_cells"], floor, cells)
                     if c not in keep_clear and c not in taken]
            want = max(1, min(4, role["area"] // 24))
            for cell in rng.sample(spots, k=min(want, len(spots))):
                is_solid = bool(solid_map.get("floor", True))
                if is_solid and not _connected(floor - solid_cells() - {cell}):
                    skipped["disconnects"] += 1
                    continue
                name = cover_pool[rng.randrange(len(cover_pool))]
                placed.append({"type": name, "kind": "floor", "mount": "floor",
                               "x": cell[0], "y": cell[1],
                               "solid": is_solid, "role": "cover",
                               "enclosure": 0, "faces": "",
                               "mirror": bool(prop_type(name).get("mirror")
                                              and rng.getrandbits(1)),
                               "label": f"{name}_cover"})
                taken.add(cell)

        # -- clutter: skipped entirely where the room does not want it ------
        clutter_pool = _fits(floor_types, purpose)
        if clutter_pool and purpose != "entry":
            eligible = [c for c in cells
                        if c not in keep_clear and c not in taken
                        and c not in lines
                        and enclosure(c, floor) >= MIN_ENCLOSURE]
            eligible.sort(key=lambda c: (-enclosure(c, floor), c))
            # Clustering with a share-of-area budget emptied the big rooms:
            # one barrel in a 169-cell hall is not restraint, it is bare. Every
            # room that has anywhere to put things gets at least two piles.
            share = {"store": density * 1.6, "vault": density,
                     "hall": density * 0.4}.get(purpose, density)
            budget = max(2, round(len(cells) * share)) if eligible else 0
            anchors = min(len(eligible), max(2, round(budget / 2.5)))
            chosen = rng.sample(eligible[:max(anchors * 3, anchors)],
                                k=min(anchors, len(eligible))) if anchors else []
            for anchor in chosen:
                if budget <= 0:
                    break
                # An L, not a queue. Growing a pile along the wall-adjacent
                # ring makes a straight row of four flush against the wall,
                # which reads as a shelf; capping the run at two per axis and
                # turning the corner gives it a shape.
                def _grow(step, limit=2):
                    out, cur = [], anchor
                    for _ in range(limit):
                        cur = (cur[0] + step[0], cur[1] + step[1])
                        if (cur not in cells or cur in keep_clear
                                or cur in lines
                                or enclosure(cur, floor) < MIN_ENCLOSURE):
                            break
                        out.append(cur)
                    return out

                arms = [_grow(d) for d in ((1, 0), (0, 1), (-1, 0), (0, -1))]
                pile = [anchor]
                for i in range(2):                # one from each arm, then two
                    for arm in arms:
                        if i < len(arm):
                            pile.append(arm[i])
                for cell in pile[:rng.choice((2, 2, 3, 3, 4))]:
                    if budget <= 0 or cell in taken:
                        continue
                    is_solid = bool(solid_map.get("floor", True))
                    if is_solid and not _connected(
                            floor - solid_cells() - {cell}):
                        skipped["disconnects"] += 1
                        continue
                    # a TYPE per prop, so one sprite is not stamped across
                    # the whole level and the layer stays readable
                    pool = [n for n in clutter_pool
                            if not (prop_type(n).get("once") and n in once_here)]
                    if not pool:
                        break
                    name = pool[rng.randrange(len(pool))]
                    if prop_type(name).get("once"):
                        once_here.add(name)
                    placed.append({"type": name, "kind": "floor",
                                   "mount": "floor",
                                   "x": cell[0], "y": cell[1],
                                   "solid": is_solid, "faces": "",
                                   "mirror": bool(
                                       prop_type(name).get("mirror")
                                       and rng.getrandbits(1)),
                                   "enclosure": enclosure(cell, floor),
                                   "label": f"{name}_clutter"})
                    taken.add(cell)
                    budget -= 1

        # -- a feature, only where one is earned ---------------------------
        centre = tuple(room.get("center") or ())
        centre_pool = _fits(centre_types, purpose)
        if (centre_pool and purpose == "vault"
                and centre in floor and centre not in keep_clear
                and centre not in taken):
            is_solid = bool(solid_map.get("centre", True))
            if not is_solid or _connected(floor - solid_cells() - {centre}):
                name = centre_pool[rng.randrange(len(centre_pool))]
                placed.append({"type": name, "kind": "centre",
                               "mount": "centre",
                               "x": centre[0], "y": centre[1],
                               "solid": is_solid, "faces": "", "mirror": False,
                               "label": f"{name}_feature"})
                taken.add(centre)

    # -- decals: walked OVER, so the rules that protect routes do not apply -
    #
    # They live on their own layer, which is what lets a crack in the floor and
    # a barrel occupy one cell: a TileMapLayer holds a single tile per
    # coordinate, so sharing a cell means sharing nothing but the coordinate.
    if by_mount["overlay"]:
        marked: set = set()
        room_floor = [c for r in rooms
                      for c in ((x, y)
                                for x in range(r["x"], r["x"] + r["w"])
                                for y in range(r["y"], r["y"] + r["h"]))
                      if c in floor]
        want = max(0, round(len(room_floor) * density * 0.5))
        for cell in rng.sample(room_floor, k=min(want, len(room_floor))):
            if cell in marked:
                continue
            name = by_mount["overlay"][rng.randrange(len(by_mount["overlay"]))]
            placed.append({"type": name, "kind": prop_type(name)["kind"],
                           "mount": "overlay", "x": cell[0], "y": cell[1],
                           "faces": "", "solid": False,
                           "mirror": bool(prop_type(name).get("mirror")
                                          and rng.getrandbits(1)),
                           "label": f"{name}_decal"})
            marked.add(cell)

    blocked = solid_cells()
    by_kind: dict = {}
    by_type: dict = {}
    for pr in placed:
        by_kind[pr["kind"]] = by_kind.get(pr["kind"], 0) + 1
        by_type[pr["type"]] = by_type.get(pr["type"], 0) + 1
    return {"props": placed, "skipped": skipped,
            "purposes": purposes, "view": seen_view,
            "layers": sorted({layer_of(q["type"]) for q in placed}),
            "checks": {"still_connected": _connected(floor - blocked),
                       "solid": len(blocked), "total": len(placed),
                       "by_kind": by_kind, "by_type": by_type,
                       "types": list(names),
                       "kept_clear": len(keep_clear)}}


def _connected(cells: set) -> bool:
    """Is the walkable set one region? Reuses levelgen's own answer so the
    prop gate and the layout guarantee cannot disagree."""
    if not cells:
        return False
    from bgate_core import levelgen

    return bool(levelgen.connected(cells))


# ---------------------------------------------------------------------------
# Into the engine
# ---------------------------------------------------------------------------
#: Godot's transform bits, packed into a cell's ALTERNATIVE id. Undocumented in
#: the places you would look for them, and the reason a torch can be drawn once
#: and mounted on either wall instead of generated twice.
FLIP_H = 1 << 12
FLIP_V = 1 << 13

#: How deep into its cell a wall mount sits, as a fraction of the tile.
#:
#: A sprite drawn CENTRED in its wall cell reads as lying in the middle of the
#: stone rather than bolted to a face — half a tile too far in. The fix is
#: Godot's per-tile ``texture_origin``, and two things about it were measured in
#: the engine because neither is documented:
#:
#:   * a POSITIVE origin shifts the texture the OTHER way — Vector2i(8, 0) moved
#:     the sprite eight pixels LEFT;
#:   * the flip bit does NOT mirror it. A flipped cell and a plain cell with the
#:     same origin both moved left by eight.
#:
#: The second one is the design constraint: mirroring saves ART but not
#: PLACEMENT, so a wall type that needs an offset needs one atlas tile per
#: facing. `mount_origin` gives the offset, `cells` refuses the shortcut.
MOUNT_DEPTH = 0.28


def mount_origin(faces: str, tile_size, depth: float = MOUNT_DEPTH):
    """The ``texture_origin`` that seats a wall mount against its inner face.

    ``faces`` is the direction from the wall cell INTO the room, so the sprite
    has to move that way — and the sign is inverted, per the measurement above.
    """
    tw, th = int(tile_size[0]), int(tile_size[1])
    step = _DIR.get(faces)
    if step is None:
        return (0, 0)
    return (-int(round(tw * depth)) * step[0],
            -int(round(th * depth)) * step[1])

#: Which way the art is DRAWN. A wall prop facing the other way is the same
#: tile with `FLIP_H` set; one facing north or south is not a flip of an
#: east-facing sprite at all, so it is drawn unflipped and says so.
DRAWN_FACING = "e"



def cells(plan: dict, atlas: dict, *, source: int,
          layer: str = "") -> dict:
    """A prop plan as `tilemap.encode_cells` input. ``{cells, mirrored, types}``

    ``layer`` takes one of `LAYERS` and emits only the props that draw on it.
    That split is not cosmetic: a TileMapLayer holds ONE tile per coordinate, so
    a crack in the floor and the barrel standing on it can only coexist as two
    layers. Omit it and every prop comes back, which is a duplicate-coordinate
    error the moment decals are in the mix — deliberately, because silently
    dropping one of them is how a level loses half its dressing.

    ``atlas`` maps a prop TYPE to an atlas coordinate, or to a list of them when
    the sheet holds variants — ``{"torch": (0, 0), "barrel": [(1, 0), (2, 0)]}``.
    Keying on the type rather than the role is what lets a torch and a banner
    coexist as separate sprites with separate mounting rules.

    Mirroring comes from the PLAN, not from a guess here: `plan` already refused
    any wall whose face the sprite cannot be drawn at, so a prop marked
    ``mirror`` is one the type declared it could be flipped for.

    This is the step that was missing while props existed as a plan and a Python
    composite: nothing in the engine drew them, and a render that only exists in
    a PNG is a mock-up of a feature, not the feature.
    """
    out, mirrored = [], 0
    types: dict = {}
    for pr in plan["props"]:
        name = pr["type"]
        if layer and layer_of(name) != layer:
            continue
        if name not in atlas:
            raise PropError(f"no atlas entry for prop type {name!r} — the plan "
                            f"placed {sorted({q['type'] for q in plan['props']})}"
                            f" and the atlas names {sorted(atlas)}")
        spot = atlas[name]
        per_face = isinstance(spot, dict)
        if per_face:
            # one tile PER FACING — the only way to seat a wall mount, because
            # the flip bit does not mirror texture_origin (measured)
            if pr["faces"] not in spot:
                raise PropError(
                    f"{name} is mounted facing {pr['faces']!r} at "
                    f"({pr['x']}, {pr['y']}) and the atlas has no tile for that "
                    f"facing — it names {sorted(spot)}")
            spot = spot[pr["faces"]]
        if isinstance(spot, (list, tuple)) and spot and                 isinstance(spot[0], (list, tuple)):
            spot = spot[hash((pr["x"], pr["y"])) % len(spot)]
        alt = 0
        if pr.get("mirror") and not per_face:
            if not prop_type(name).get("mirror"):
                raise PropError(
                    f"{name} is marked mirrored at ({pr['x']}, {pr['y']}) but "
                    "its type does not allow flipping — a directional sprite "
                    "flipped is a sprite facing the wrong way")
            alt |= FLIP_H
            mirrored += 1
        out.append({"x": pr["x"], "y": pr["y"], "source": int(source),
                    "ax": int(spot[0]), "ay": int(spot[1]), "alt": alt})
        types[name] = types.get(name, 0) + 1
    return {"cells": out, "mirrored": mirrored, "types": types}


def mount_origins(plan: dict, atlas: dict, *, tile_size,
                  depth: float = MOUNT_DEPTH) -> dict:
    """``{(ax, ay): (dx, dy)}`` for `tilemap.write_tileset`'s ``origins``.

    Only wall mounts get one, and only where the atlas gives that facing its own
    tile — a shared tile cannot carry two different offsets, so asking for one
    would seat the prop correctly on one wall and wrongly on the other.
    """
    out: dict = {}
    for pr in plan["props"]:
        if pr["mount"] != "wall":
            continue
        spot = atlas.get(pr["type"])
        if not isinstance(spot, dict) or pr["faces"] not in spot:
            continue
        coord = tuple(int(v) for v in spot[pr["faces"]])
        nudge = mount_origin(pr["faces"], tile_size, depth)
        if coord in out and out[coord] != nudge:
            raise PropError(
                f"atlas tile {coord} is used for two facings with different "
                "offsets — give each facing its own tile")
        out[coord] = nudge
    return out


# ---------------------------------------------------------------------------
# What the art has to be
# ---------------------------------------------------------------------------
def art_spec(name: str, *, tile_px: int = 32, view: str = "") -> dict:
    """Exactly what a generator has to produce for this type.

    Every number here is a CONSTRAINT the placer already relies on, so art made
    to something else will not fit — a prop drawn at the wrong proportion is not
    a stylistic difference, it is a sprite that hangs off its cell or floats
    above the floor. This is the contract, in pixels.

    ``motion`` is the mechanism, and it is one of three because they are three
    different things in the engine:

      loop    an ambient cycle the TILESET plays with no node — a torch
              flickering, a portal turning. One row of frames.
      states  discrete conditions with their own lengths — shut, opening, open.
              Not a loop: something has to drive it, so each state is its own
              tile (or its own animated tile) and a script picks between them.
      static  one frame.
    """
    from bgate_core import gameview as _view

    spec = prop_type(name)
    w, h = spec.get("size", (1, 1))
    cell = (w * tile_px, h * tile_px)
    foot = float(spec.get("footprint", 0.75))
    out = {"type": name, "cells": [w, h], "cell_px": list(cell),
           "footprint": foot,
           # the box the ART fills, inside the cell it occupies
           "art_px": [max(1, round(cell[0] * foot)),
                      max(1, round(cell[1] * foot))],
           "mount": spec["mount"], "layer": layer_of(name),
           # THE CAMERA COMES FROM THE PROJECT'S VIEW, not from the prompt the
           # generator happens to write. A barrel showing its lid is right for
           # top-down and wrong for a platformer, and the only way that stays
           # consistent across agents is for nobody to decide it locally.
           "view": _view.normalise(view),
           "camera": _view.camera_clause(view, spec["mount"]),
           "in_view": _view.supports(view, spec["mount"]),
           "mirror": bool(spec.get("mirror")),
           # a wall mount that needs seating needs one drawing PER FACING,
           # because the flip bit does not carry texture_origin
           "facings": list(spec.get("sides") or ()) if spec["mount"] == "wall"
                      else [],
           "ground": [w * tile_px // 2, h * tile_px],   # bottom-centre
           }
    if spec.get("anim"):
        n = int(spec["anim"]["frames"])
        out.update(motion="loop", frames=n, fps=spec["anim"]["fps"],
                   sheet_px=[cell[0] * n, cell[1]],
                   note="one row, left to right, seamless loop")
    elif spec.get("states"):
        states = dict(spec["states"])
        out.update(motion="states", states=states,
                   frames=sum(states.values()),
                   sheet_px=[cell[0] * max(states.values()), cell[1] * len(states)],
                   note="one ROW PER STATE, longest state sets the width")
    else:
        out.update(motion="static", frames=1, sheet_px=list(cell),
                   note="one frame")
    return out


def art_manifest(names=None, *, tile_px: int = 32, view: str = "") -> dict:
    """`art_spec` for every type, plus the totals a generation has to budget.

    Types whose mount means nothing in this view are reported rather than
    dropped: a ceiling mount is ordinary in a platformer and meaningless seen
    from above, and a generation that silently skipped them would look like a
    budget that came in under estimate.
    """
    from bgate_core import gameview as _view

    names = tuple(names or sorted(PROP_TYPES))
    specs = [art_spec(n, tile_px=tile_px, view=view) for n in names]
    return {"tile_px": tile_px, "view": _view.normalise(view),
            "specs": [s for s in specs if s["in_view"]],
            "out_of_view": [s["type"] for s in specs if not s["in_view"]],
            "drawings": sum(max(1, len(s["facings"]))
                            for s in specs if s["in_view"]),
            "frames": sum(s["frames"] * max(1, len(s["facings"]))
                          for s in specs if s["in_view"])}
