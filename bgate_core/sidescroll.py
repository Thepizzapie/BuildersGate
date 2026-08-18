"""A side-scrolling level: segments left to right, built inside the jump.

A top-down level is a SPACE and its generator partitions one. A platformer
level is a SEQUENCE — you enter at the left, you leave at the right, and what
happens between is a run of set pieces: a flat stretch, a pit, a staircase, a
hop across floating platforms, a pipe to clear. Partitioning a rectangle
produces nothing like that, which is why `levelgen` does not survive the move
and this is a different generator rather than a parameter on that one.

THE JUMP IS THE CONSTRAINT, AND IT COMES FIRST. Every segment sizes itself
from `jump.kernel`: a pit is at most as wide as the character can clear, a
platform is at most as high as the character can rise, a pipe is never taller
than a jump. An unclearable gap is therefore UNREPRESENTABLE rather than
generated and rejected afterwards — the same discipline as refusing a prop
that would sever a top-down level, moved one step earlier.

That is not the whole gate, because segments compose and composition can
strand you where nothing individually did:

  * REACHABLE — the goal is inside the flood fill of jump arcs from spawn.
    This is the analogue of `levelgen.connected` and it is the one that
    matters.
  * CLEARANCE — the body fits everywhere it must pass. A one-cell slot under a
    ceiling reads perfectly and cannot be walked through.
  * NO SOFTLOCK — nowhere you can land and never leave. A pit you can drop
    into and not climb out of is a level that has to be restarted, and it
    looks completely fine in a screenshot.
  * NO BLIND LEAP — the landing is on screen when the jump is committed.
    Unfair rather than broken, but it is measurable, so it is measured.

Difficulty scales the pits and the gaps TOWARD the kernel's limit and never
past it, so a harder level is a tighter one rather than an impossible one.
"""
from __future__ import annotations

import random
from typing import Optional

from bgate_core import jump as _jump

#: Cells of sky above the ground line. Enough for a full jump plus the
#: floating platforms a jump reaches, or the level has no vertical play at all.
SKY = 8

#: How much of the level width is spent on the opening flat run, where the
#: player is still working out which way is forward. Mario 1-1 opens with a
#: long empty stretch for exactly this reason.
INTRO = 0.08

#: Cells the camera shows ahead of the player, for the blind-leap check. A
#: jump whose landing is further than this is committed on faith.
SCREEN_AHEAD = 12


class LevelError(ValueError):
    """A layout that cannot be built, or one that could not be played."""


class Limits:
    """What this character can do, read off the kernel instead of guessed.

    Every segment sizes itself from these, so changing the jump changes the
    levels rather than changing what the levels claim about themselves.
    """

    def __init__(self, spec: _jump.JumpSpec, kern: Optional[dict] = None):
        self.spec = spec
        self.kernel = kern if kern is not None else _jump.kernel(spec)
        flat = [o[0] for o in self.kernel if o[1] == 0 and o[0] > 0]
        self.gap = (max(flat) - 1) if flat else 0
        self.rise = max((-o[1] for o in self.kernel if o[1] < 0), default=0)
        up = [o[0] for o in self.kernel if o[1] == -self.rise and o[0] > 0]
        self.rise_gap = max(up) if up else 0
        if self.gap < 2 or self.rise < 1:
            raise LevelError(
                f"this character clears a {self.gap}-cell pit and rises "
                f"{self.rise} cells — there is no platformer in that. Raise "
                "jump_speed or run, or lower gravity.")

    def as_dict(self) -> dict:
        return {"max_pit": self.gap, "max_rise": self.rise,
                "reach_at_peak": self.rise_gap,
                **self.spec.as_dict()}


# ---------------------------------------------------------------------------
# Segments — each returns solid cells and the ground height it leaves you at
# ---------------------------------------------------------------------------
def _floor(x0, w, y, height) -> set:
    """Ground from `y` down to the bottom, so a pit is a hole and not a ledge
    floating over nothing."""
    return {(x, yy) for x in range(x0, x0 + w) for yy in range(y, height)}


def _seg_flat(rng, x0, y, limits, height, hard) -> tuple:
    w = rng.randint(4, 9)
    return _floor(x0, w, y, height), y, w, {"kind": "flat"}


def _seg_pit(rng, x0, y, limits, height, hard) -> tuple:
    """A hole. Width scales with difficulty but never past what clears."""
    widest = max(2, min(limits.gap, 2 + round((limits.gap - 2) * hard)))
    w = rng.randint(2, widest)
    lip = rng.randint(2, 4)                  # run-up on the far side to land on
    cells = _floor(x0 + w, lip, y, height)
    return cells, y, w + lip, {"kind": "pit", "width": w}


def _seg_stair(rng, x0, y, limits, height, hard) -> tuple:
    """A staircase, up or down. Each step is one cell, which is always
    walkable, so this is the segment that changes height safely."""
    n = rng.randint(2, max(2, limits.rise))
    up = rng.random() < 0.5
    cells: set = set()
    yy = y
    for i in range(n):
        yy = yy - 1 if up else yy + 1
        yy = max(SKY, min(height - 2, yy))
        cells |= _floor(x0 + i * 2, 2, yy, height)
    return cells, yy, n * 2, {"kind": "stair", "up": up, "steps": n}


def _seg_hop(rng, x0, y, limits, height, hard) -> tuple:
    """Floating platforms over a pit — the set piece the kernel exists for.

    Each platform is placed inside the reachable offsets from the last one, so
    the hop is clearable by construction.
    """
    count = rng.randint(2, 3)
    cells: set = set()
    px, py = x0, y
    for _ in range(count):
        rise = rng.randint(0, min(2, limits.rise))
        step = rng.randint(3, max(3, min(limits.gap, limits.rise_gap)))
        px, py = px + step, max(SKY, py - rise)
        cells |= {(px + i, py) for i in range(rng.randint(2, 4))}
    landing = px + 4
    cells |= _floor(landing, 4, y, height)
    return cells, y, landing + 4 - x0, {"kind": "hop", "platforms": count}


def _seg_blocks(rng, x0, y, limits, height, hard) -> tuple:
    """A row of floating blocks at head height — Mario's question blocks.

    Placed at `rise` above the ground so they can be hit from below, and never
    so low that they become a ceiling the player cannot pass under.
    """
    body_h = limits.spec.body[1]
    up = max(body_h + 1, min(limits.rise, 3))
    n = rng.randint(2, 4)
    cells = _floor(x0, n + 4, y, height)
    cells |= {(x0 + 2 + i, y - up) for i in range(n)}
    return cells, y, n + 4, {"kind": "blocks", "count": n, "height": up}


def _seg_pipe(rng, x0, y, limits, height, hard) -> tuple:
    """A pipe: a solid column you jump onto and over. Never taller than a
    jump, or it is a wall the level pretends is scenery."""
    h = rng.randint(2, max(2, min(limits.rise, 4)))
    cells = _floor(x0, 3, y, height)
    cells |= {(x0 + 3 + i, yy) for i in range(2)
              for yy in range(y - h, height)}
    cells |= _floor(x0 + 5, 4, y, height)
    return cells, y, 9, {"kind": "pipe", "height": h}


SEGMENTS = {"flat": _seg_flat, "pit": _seg_pit, "stair": _seg_stair,
            "hop": _seg_hop, "blocks": _seg_blocks, "pipe": _seg_pipe}

#: How often each segment comes up. Flat is common because a platformer needs
#: breathing room between set pieces — back-to-back challenges read as noise.
WEIGHTS = {"flat": 3, "pit": 3, "stair": 2, "hop": 2, "blocks": 2, "pipe": 2}


def plan(length: int = 200, height: int = 16, *, seed: int = 0,
         spec: Optional[_jump.JumpSpec] = None,
         difficulty: float = 0.5,
         kinds=None) -> dict:
    """A side-scrolling level, built inside what the character can do.

    ``{solid, spawn, goal, segments, limits, width, height}``. Nothing here is
    checked — see `check`, which is what decides whether it is playable.
    """
    if length < 40 or height < 12:
        raise LevelError(f"{length}x{height} is too small to hold a run — a "
                         "platformer needs room to build up speed and sky to "
                         "jump into")
    spec = spec or _jump.JumpSpec(run=9.0, jump_speed=18.0, gravity=40.0)
    limits = Limits(spec)
    hard = max(0.0, min(1.0, float(difficulty)))
    rng = random.Random(seed)
    pool = [k for k in (kinds or SEGMENTS) if k in SEGMENTS]
    if not pool:
        raise LevelError(f"no known segment kinds in {kinds!r}")

    ground = height - 4
    # THE HEIGHT THE RUN STARTS AT. `ground` is reassigned by every segment
    # that changes level, so reading it after the loop gives the height the
    # player ENDS at — which put the spawn five cells in the air, in a level
    # whose every other number was correct.
    start_ground = ground
    solid: set = set()
    segments: list = []

    intro = max(6, round(length * INTRO))
    solid |= _floor(0, intro, ground, height)
    segments.append({"kind": "intro", "x": 0, "w": intro})
    x = intro

    bag = [k for k in pool for _ in range(WEIGHTS.get(k, 1))]
    while x < length - 12:
        kind = rng.choice(bag)
        cells, ground, w, meta = SEGMENTS[kind](rng, x, ground, limits,
                                                height, hard)
        if x + w > length - 12:
            break
        solid |= cells
        segments.append({**meta, "x": x, "w": w})
        x += w
        # a beat of flat after a set piece, so challenges do not fuse
        if kind != "flat" and rng.random() < 0.6:
            rest = rng.randint(2, 5)
            solid |= _floor(x, rest, ground, height)
            segments.append({"kind": "rest", "x": x, "w": rest})
            x += rest

    outro = length - x
    solid |= _floor(x, outro, ground, height)
    segments.append({"kind": "outro", "x": x, "w": outro})

    return {"seed": seed, "width": length, "height": height,
            "solid": sorted(solid), "spawn": [2, start_ground - 1],
            "goal": [length - 3, ground - 1],
            "segments": segments, "limits": limits.as_dict(),
            "spec": spec.as_dict()}


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------
def check(level: dict, spec: Optional[_jump.JumpSpec] = None) -> dict:
    """Is this level PLAYABLE? ``{ok, findings, ...}``

    Four questions, all measured. `reachable` is the one that matters and the
    others are the ways a level passes it and still cannot be played.
    """
    spec = spec or _jump.JumpSpec(**{k: v for k, v in level["spec"].items()
                                     if k in ("run", "jump_speed", "gravity",
                                              "terminal")},
                                  body=tuple(level["spec"]["body"]))
    solid = {tuple(c) for c in level["solid"]}
    spawn, goal = tuple(level["spawn"]), tuple(level["goal"])
    kern = _jump.kernel(spec)
    findings: list = []

    got = _jump.reachable(solid, spawn, spec, kern)
    if not got["ok"]:
        return {"ok": False, "findings": [{"kind": "no_spawn",
                                           "note": got["reason"]}]}
    reached = got["reached"]

    if goal not in reached:
        findings.append({"kind": "goal_unreachable", "at": list(goal),
                         "note": "the end of the level cannot be jumped to "
                                 "from the start — this is a bug in the "
                                 "generator, not a difficulty setting"})

    # SOFTLOCK: somewhere you can land and not get back out. Checked by asking
    # whether the goal is reachable FROM there, not by eyeballing the shape.
    stuck = []
    for cell in sorted(reached):
        if cell == goal:
            continue
        onward = _jump.reachable(solid, cell, spec, kern)
        if onward["ok"] and goal not in onward["reached"]:
            stuck.append(cell)
            if len(stuck) >= 8:
                break
    if stuck:
        findings.append({"kind": "softlock", "cells": [list(c) for c in stuck],
                         "note": "you can land here and never reach the end; "
                                 "the level has to be restarted and it looks "
                                 "completely fine in a screenshot"})

    # STRANDED PLATFORMS. In a HAND-MADE level an unreachable ledge is
    # background; in a generated one it is a defect, because the generator only
    # places a platform it means you to land on. Counting it and not saying so
    # is how a level ships with a set piece nobody can play.
    orphans = sorted(got["unreachable"])
    if orphans:
        findings.append({"kind": "stranded", "count": len(orphans),
                         "cells": [list(c) for c in orphans[:8]],
                         "note": "standable cells the player cannot get to — "
                                 "the generator placed a platform outside its "
                                 "own jump"})

    # CLEARANCE: the body fits where it must pass.
    stand = _jump.surfaces(solid, body=spec.body)
    tight = [c for c in reached if c not in stand]
    if tight:
        findings.append({"kind": "clearance", "cells": [list(c) for c in tight[:8]],
                         "note": f"a {spec.body[0]}x{spec.body[1]} body does "
                                 "not fit here"})

    # BLIND LEAP: a jump longer than the camera shows ahead.
    blind = [o for o in kern if o[0] > SCREEN_AHEAD]
    reach_used = max((o[0] for o in kern), default=0)

    return {"ok": not findings, "findings": findings,
            "reachable": len(reached), "standable": len(stand),
            "unreachable": len(got["unreachable"]),
            "goal_reachable": goal in reached,
            "widest_jump": reach_used,
            "beyond_screen": len(blind)}


def ascii_map(level: dict, *, width: int = 0) -> str:
    """The level as text, which is the fastest way for anyone — person or
    agent — to see the SHAPE before spending a generation on art."""
    solid = {tuple(c) for c in level["solid"]}
    w = width or min(level["width"], 120)
    h = level["height"]
    spawn, goal = tuple(level["spawn"]), tuple(level["goal"])
    rows = []
    for y in range(h):
        row = []
        for x in range(w):
            if (x, y) == spawn:
                row.append("S")
            elif (x, y) == goal:
                row.append("G")
            elif (x, y) in solid:
                row.append("#")
            else:
                row.append(".")
        rows.append("".join(row))
    return chr(10).join(rows)
