"""What the player can reach — the side-scroller's answer to `connected`.

A top-down level is playable when its floor is one region, and a flood fill
settles that. Under gravity the question is different and no flood fill
answers it: you cannot walk upward. A platform three cells up and five across
is either reachable or it is scenery, and which one it is depends entirely on
the character's jump — a number that lives in the player scene, not in the
level.

So the LEVEL GENERATOR IS PARAMETERISED BY THE JUMP. `kernel` turns a
`JumpSpec` into the set of landing offsets a standing character can reach, and
the arc each one travels through. Everything downstream is that set:

  * generation places the next platform INSIDE the kernel, so an unclearable
    gap is unrepresentable rather than caught afterwards
  * the reachability gate is a breadth-first search over the kernel from spawn
  * coins go ON the arcs, so the collectibles mark the route by construction
    rather than by an artist guessing where the player will fly

Everything here is in CELLS and SECONDS. Nothing knows the tile size, because
the jump is a property of the game and not of the art.

THE ARC MUST BE CLEAR, not just the landing. A jump that reaches a ledge by
passing through a ceiling is not a jump, and a generator that only checked
endpoints would produce levels that look correct and cannot be played — which
is the same failure as a floor whose tiles are all present and whose collision
says otherwise.

This module is PURE. It predicts; it does not measure. The engine is the
referee — see `godot.jump_probe`, which runs a real body with these numbers
and compares. A kernel that disagrees with the physics that ships is worse
than no kernel, because every level built on it is subtly unplayable.
"""
from __future__ import annotations

from typing import Iterable, Optional

#: How finely an arc is sampled, in steps per second. High enough that a fast
#: jump cannot tunnel through a one-cell ledge between samples, which is the
#: bug this constant exists to prevent.
STEPS_PER_SECOND = 120

#: How long an arc is followed before it is abandoned, in seconds. A fall that
#: has not landed by now is off the bottom of any level worth generating.
MAX_FLIGHT = 4.0

#: The hold fractions sampled for a variable-height jump. Platformers let you
#: tap for a short hop and hold for a full one, and a generator that only knew
#: the full jump would refuse gaps the player can clear and place ledges the
#: player sails past.
HOLDS = (0.35, 0.6, 0.8, 1.0)

#: Horizontal speeds sampled, as a fraction of the run. Standing jumps and
#: half-speed jumps reach places a full-speed jump overshoots.
SPEEDS = (0.0, 0.5, 1.0)


class JumpError(ValueError):
    """A jump that cannot describe a playable character."""


class JumpSpec:
    """The character's motion, in cells and seconds.

    ``body`` is (width, height) in CELLS and it matters more than it looks: a
    two-cell character cannot pass a one-cell gap, and a level that ignores it
    is one that reads perfectly and cannot be walked through.
    """

    __slots__ = ("run", "jump_speed", "gravity", "body", "terminal")

    def __init__(self, run: float = 8.0, jump_speed: float = 14.0,
                 gravity: float = 40.0, body=(1, 2),
                 terminal: float = 24.0):
        if run <= 0 or jump_speed <= 0 or gravity <= 0:
            raise JumpError("run, jump_speed and gravity must all be positive "
                            "— a character that cannot move cannot reach "
                            "anything, and the generator would refuse every "
                            "layout without saying why")
        bw, bh = int(body[0]), int(body[1])
        if bw < 1 or bh < 1:
            raise JumpError(f"a {bw}x{bh} body is not a character")
        self.run = float(run)
        self.jump_speed = float(jump_speed)
        self.gravity = float(gravity)
        self.body = (bw, bh)
        self.terminal = float(terminal)

    # -- the two numbers a designer actually thinks in ----------------------
    @property
    def peak(self) -> float:
        """Highest point of a full jump, in cells."""
        return (self.jump_speed ** 2) / (2.0 * self.gravity)

    @property
    def span(self) -> float:
        """How far a full-speed full jump travels before returning to height."""
        return self.run * (2.0 * self.jump_speed / self.gravity)

    def as_dict(self) -> dict:
        return {"run": self.run, "jump_speed": self.jump_speed,
                "gravity": self.gravity, "body": list(self.body),
                "terminal": self.terminal,
                "peak_cells": round(self.peak, 2),
                "span_cells": round(self.span, 2)}


def _arc(spec: JumpSpec, hold: float, speed: float, *,
         drop: bool = False) -> list:
    """One trajectory, as the cells its FEET pass through.

    ``hold`` cuts the upward velocity early, which is how a platformer's
    variable jump height works — release and the rise stops. ``drop`` is a walk
    off a ledge with no jump at all, which reaches places no jump does.
    """
    dt = 1.0 / STEPS_PER_SECOND
    x, y = 0.0, 0.0
    vx = spec.run * speed
    vy = 0.0 if drop else -spec.jump_speed
    cut = hold * (spec.jump_speed / spec.gravity) if not drop else 0.0
    out: list = []
    seen = set()
    t = 0.0
    while t < MAX_FLIGHT:
        if not drop and vy < 0 and t >= cut:
            vy = 0.0                       # released: the rise stops here
        vy = min(vy + spec.gravity * dt, spec.terminal)
        x += vx * dt
        y += vy * dt
        t += dt
        cell = (int(round(x)), int(round(y)))
        if cell not in seen:
            seen.add(cell)
            out.append((cell, vy))
        if y > MAX_FLIGHT * spec.terminal:
            break
    return out


def _occupied(cell, body) -> list:
    """The cells a body standing with its FEET at `cell` fills.

    Feet-anchored, because that is what a surface supports and what a landing
    means. A head-anchored model gets every ceiling check off by the body
    height, which is invisible until a character wedges under a ledge.
    """
    x, y = cell
    return [(x + dx, y - dy) for dx in range(body[0]) for dy in range(body[1])]


def kernel(spec: JumpSpec, *, reach: int = 12, rise: int = 6,
           fall: int = 12) -> dict:
    """Landing offsets a standing character can reach, and the arc of each.

    ``{(dx, dy): {"clear": [cells the body must pass through], "kind": ...}}``
    where the offsets are relative to the character's FEET and ``dy`` is
    positive downward, matching tile coordinates.

    Both directions are covered by symmetry — a jump left is a jump right
    mirrored, and generating one and flipping it keeps the two from ever
    disagreeing.
    """
    out: dict = {}

    def record(kind, hold, speed, sign):
        for (cell, vy) in _arc(spec, hold, speed, drop=(kind == "fall")):
            dx, dy = cell[0] * sign, cell[1]
            if abs(dx) > reach or dy < -rise or dy > fall:
                continue
            # a landing is only possible while DESCENDING; on the way up you
            # are still under way, and calling that a landing lets the
            # generator place a ledge the player is rising past
            if vy <= 0:
                continue
            prior = out.get((dx, dy))
            if prior is not None and len(prior["clear"]) <= 1:
                continue
            path = []
            for (c, _v) in _arc(spec, hold, speed, drop=(kind == "fall")):
                px, py = c[0] * sign, c[1]
                if (px, py) == (dx, dy):
                    break
                path.append((px, py))
            entry = {"clear": path, "kind": kind, "hold": hold,
                     "speed": speed, "dir": sign}
            if prior is None or len(path) < len(prior["clear"]):
                out[(dx, dy)] = entry

    for sign in (1, -1):
        for hold in HOLDS:
            for speed in SPEEDS:
                record("jump", hold, speed, sign)
        for speed in SPEEDS[1:]:
            record("fall", 0.0, speed, sign)
    # standing still on the spot is not a move, and leaving it in makes every
    # surface trivially "reachable from itself"
    out.pop((0, 0), None)
    return out


def clear_for(spec: JumpSpec, offset, entry: dict) -> list:
    """Every cell that must be EMPTY for this jump, body included.

    The arc is where the feet go; the character is taller than its feet, so a
    ledge that clears the trajectory can still catch the head.
    """
    cells = set()
    for c in list(entry["clear"]) + [tuple(offset)]:
        cells.update(_occupied(c, spec.body))
    return sorted(cells)


def surfaces(solid: Iterable, *, body=(1, 2)) -> set:
    """Cells a character can STAND on: empty, with something solid beneath and
    enough headroom for the body."""
    solid = {tuple(c) for c in solid}
    out = set()
    for (x, y) in solid:
        foot = (x, y - 1)
        if foot in solid:
            continue
        if all((foot[0] + dx, foot[1] - dy) not in solid
               for dx in range(body[0]) for dy in range(body[1])):
            out.add(foot)
    return out


def reachable(solid: Iterable, start, spec: JumpSpec,
              kern: Optional[dict] = None) -> dict:
    """Which standable cells the player can actually get to from `start`.

    ``{reached, unreachable, ok}``. This is the side-scroller's flood fill and
    it is the gate everything else hangs off: a platform outside this set is
    not difficult, it is scenery.
    """
    solid = {tuple(c) for c in solid}
    stand = surfaces(solid, body=spec.body)
    kern = kern if kern is not None else kernel(spec)
    start = tuple(start)
    if start not in stand:
        return {"ok": False, "reached": set(), "unreachable": stand,
                "reason": f"the start {start} is not a standable cell — "
                          "nothing is solid under it, or the body does not fit"}

    seen = {start}
    stack = [start]
    while stack:
        at = stack.pop()
        # walking is free: the run along a continuous surface
        for step in (-1, 1):
            n = (at[0] + step, at[1])
            if n in stand and n not in seen:
                seen.add(n)
                stack.append(n)
        for off, entry in kern.items():
            land = (at[0] + off[0], at[1] + off[1])
            if land in seen or land not in stand:
                continue
            blocked = False
            for c in clear_for(spec, off, entry):
                if (at[0] + c[0], at[1] + c[1]) in solid:
                    blocked = True
                    break
            if blocked:
                continue
            seen.add(land)
            stack.append(land)

    return {"ok": True, "reached": seen, "unreachable": stand - seen,
            "standable": len(stand), "reached_count": len(seen)}
