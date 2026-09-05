"""3D blockout specs: validation, and the bridge from a 2D `level_plan`.

The geometry itself is built in the engine by
``templates/shared/tools/bgate_blockout_gen.gd`` (rooms, one wall per shared
edge, doors with lintels, props resting on floors, a baked navmesh and a
measured report). This module holds the two pieces that do not need Godot:

* :func:`validate` - the same refusals the generator makes, so a bad spec is
  named before an engine is launched and so the tests can run without one.
* :func:`spec_from_plan` - turn ``levelgen.plan`` output (rooms and corridor
  paths in cells) into rooms + corridor rectangles in metres, with the
  corridors CLIPPED so they end flush against the rooms they join. Rooms and
  corridors that touch get their door from ``auto_doors``.

Coordinates: a room is an interior rectangle on the XZ plane - ``x``/``z`` its
minimum corner, ``w`` along +X, ``d`` along +Z, metres. Walls are centred on
the boundary, so two rooms that share an edge share a wall.
"""

from __future__ import annotations

from typing import Optional

EPS = 0.005


class BlockoutError(ValueError):
    """The spec cannot be built as written; the message names why."""


def validate(spec: dict) -> list[str]:
    """Every refusal the generator would make, as sentences. Empty is good."""
    problems: list[str] = []
    rooms = spec.get("rooms")
    if not isinstance(rooms, list) or not rooms:
        return ["spec.rooms is empty - nothing to block out"]
    names: dict[str, int] = {}
    rects: list[tuple[str, float, float, float, float, float]] = []
    for i, r in enumerate(rooms):
        if not isinstance(r, dict):
            problems.append(f"room {i} is not an object")
            continue
        name = str(r.get("name", f"Room_{i}")).strip()
        if not name or name in names:
            problems.append(f"room {i}: name {name!r} is empty or duplicated")
        names[name] = i
        try:
            w, d = float(r.get("w", 0)), float(r.get("d", 0))
            x, z = float(r.get("x", 0)), float(r.get("z", 0))
            y = float(r.get("floor_y", 0))
        except (TypeError, ValueError):
            problems.append(f"room {name}: x, z, w, d must be numbers")
            continue
        if w <= 0 or d <= 0:
            problems.append(f"room {name}: w and d must be positive metres")
        rects.append((name, x, z, w, d, y))
        for j, p in enumerate(r.get("props") or []):
            try:
                pw, ph, pd = float(p.get("w", 1)), float(p.get("h", 1)), float(p.get("d", 1))
                px, pz = float(p.get("x", 0)), float(p.get("z", 0))
            except (TypeError, ValueError, AttributeError):
                problems.append(f"prop {name}/{j}: x, z, w, h, d must be numbers")
                continue
            if min(pw, ph, pd) <= 0:
                problems.append(f"prop {name}/{p.get('name', j)}: sizes must be positive")
            if px < -EPS or pz < -EPS or px + pw > w + EPS or pz + pd > d + EPS:
                problems.append(f"prop {name}/{p.get('name', j)} pokes outside its room")
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            a, b = rects[i], rects[j]
            ox = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
            oz = min(a[2] + a[4], b[2] + b[4]) - max(a[2], b[2])
            if ox > EPS and oz > EPS and abs(a[5] - b[5]) < EPS:
                problems.append(
                    f"rooms {a[0]} and {b[0]} overlap by {ox:.2f} x {oz:.2f} m - "
                    "split them or make one a corridor that ENDS at the other's wall")
    for i, door in enumerate(spec.get("doors") or []):
        if not isinstance(door, dict):
            problems.append(f"door {i} is not an object")
            continue
        a, b = str(door.get("from", "")), str(door.get("to", ""))
        if a not in names:
            problems.append(f"door {i}: unknown room {a!r}")
        if b and b not in names and str(door.get("side", "s")) not in ("n", "s", "e", "w"):
            problems.append(f"door {i}: `to` is not a room and `side` is not n|s|e|w")
    return problems


# --------------------------------------------------------------- from a plan

def _subtract_span(lo: float, hi: float, cuts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pieces = [(lo, hi)]
    for c0, c1 in cuts:
        nxt: list[tuple[float, float]] = []
        for p0, p1 in pieces:
            if c1 <= p0 + EPS or c0 >= p1 - EPS:
                nxt.append((p0, p1))
                continue
            if c0 > p0 + EPS:
                nxt.append((p0, c0))
            if c1 < p1 - EPS:
                nxt.append((c1, p1))
        pieces = nxt
    return pieces


def _clip(rect: dict, blockers: list[dict], axis: str) -> list[dict]:
    """Remove from `rect` every stretch along `axis` that another rectangle
    covers, so a corridor ends at the wall of whatever it runs into."""
    if axis == "z":
        lo, hi = rect["z"], rect["z"] + rect["d"]
        cuts = [(b["z"], b["z"] + b["d"]) for b in blockers
                if min(rect["x"] + rect["w"], b["x"] + b["w"]) - max(rect["x"], b["x"]) > EPS]
        return [{**rect, "z": p0, "d": p1 - p0} for p0, p1 in _subtract_span(lo, hi, cuts)
                if p1 - p0 > EPS]
    lo, hi = rect["x"], rect["x"] + rect["w"]
    cuts = [(b["x"], b["x"] + b["w"]) for b in blockers
            if min(rect["z"] + rect["d"], b["z"] + b["d"]) - max(rect["z"], b["z"]) > EPS]
    return [{**rect, "x": p0, "w": p1 - p0} for p0, p1 in _subtract_span(lo, hi, cuts)
            if p1 - p0 > EPS]


def spec_from_plan(plan: dict, *, cell_m: float = 1.0, corridor_width: int = 2,
                   wall_height: float = 3.0, player: Optional[dict] = None,
                   out_scene: str = "res://scenes/blockout/blockout.tscn",
                   room_names: Optional[list[str]] = None) -> dict:
    """A blockout spec from ``levelgen.plan`` output.

    Rooms keep their cell rectangles scaled by ``cell_m``. Each corridor path
    segment becomes a rectangle ``corridor_width`` cells wide, clipped against
    every room and every earlier corridor piece so nothing overlaps and every
    junction is a shared edge. The spawn is the plan's spawn, the plan's exit
    becomes a goal volume, and ``auto_doors`` opens every shared edge.
    """
    if cell_m <= 0:
        raise BlockoutError("cell_m must be positive")
    if corridor_width < 1:
        raise BlockoutError("corridor_width must be at least 1 cell")
    rooms_in = plan.get("rooms") or []
    if not rooms_in:
        raise BlockoutError("the plan has no rooms")
    rooms: list[dict] = []
    for i, r in enumerate(rooms_in):
        name = (room_names[i] if room_names and i < len(room_names) else f"Room_{i + 1:02d}")
        rooms.append({"name": name, "kind": "room",
                      "x": r["x"] * cell_m, "z": r["y"] * cell_m,
                      "w": r["w"] * cell_m, "d": r["h"] * cell_m})
    half_lo = (corridor_width - 1) // 2
    corridors: list[dict] = []
    n = 0
    for path in (c.get("path") or [] for c in plan.get("corridors") or []):
        for (x1, y1), (x2, y2) in zip(path, path[1:]):
            if x1 == x2 and y1 == y2:
                continue
            if x1 == x2:      # vertical run -> rectangle long along z
                rect = {"x": (x1 - half_lo) * cell_m, "w": corridor_width * cell_m,
                        "z": min(y1, y2) * cell_m, "d": (abs(y2 - y1) + 1) * cell_m}
                axis = "z"
            elif y1 == y2:
                rect = {"z": (y1 - half_lo) * cell_m, "d": corridor_width * cell_m,
                        "x": min(x1, x2) * cell_m, "w": (abs(x2 - x1) + 1) * cell_m}
                axis = "x"
            else:
                raise BlockoutError(f"corridor segment {(x1, y1)}->{(x2, y2)} is not axis-aligned")
            pieces = _clip(rect, rooms + corridors, axis)
            # A piece narrower than it is wide is junction debris.
            for piece in pieces:
                if min(piece["w"], piece["d"]) < corridor_width * cell_m - EPS:
                    if max(piece["w"], piece["d"]) < corridor_width * cell_m - EPS:
                        continue
                n += 1
                corridors.append({"name": f"Corridor_{n:02d}", "kind": "corridor", **piece})
    spec: dict = {
        "out_scene": out_scene,
        "wall_height": wall_height,
        "player": dict(player or {"height": 1.8, "radius": 0.4}),
        "auto_doors": True,
        "rooms": rooms + corridors,
    }
    if plan.get("spawn"):
        sx, sy = plan["spawn"]
        room = _room_at(rooms, (sx + 0.5) * cell_m, (sy + 0.5) * cell_m) or rooms[0]
        spec["spawn"] = {"room": room["name"], "x": (sx + 0.5) * cell_m - room["x"],
                         "z": (sy + 0.5) * cell_m - room["z"]}
    if plan.get("exit"):
        ex, ey = plan["exit"]
        room = _room_at(rooms, (ex + 0.5) * cell_m, (ey + 0.5) * cell_m) or rooms[-1]
        spec["goals"] = [{"name": "Exit", "room": room["name"],
                          "x": (ex + 0.5) * cell_m - room["x"],
                          "z": (ey + 0.5) * cell_m - room["z"], "radius": 0.6}]
    problems = validate(spec)
    if problems:
        raise BlockoutError("; ".join(problems))
    return spec


def _room_at(rooms: list[dict], x: float, z: float) -> Optional[dict]:
    for r in rooms:
        if r["x"] - EPS <= x <= r["x"] + r["w"] + EPS and r["z"] - EPS <= z <= r["z"] + r["d"] + EPS:
            return r
    return None
