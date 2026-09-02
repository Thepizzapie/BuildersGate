"""Where every joint actually is, frame by frame, in world space.

THE HOLE THIS FILLS, AND IT IS THE REASON HALF THE CURVE GATES HAVE NEVER RUN.
Every measurement in `animcurves.py` reads a channel's raw local values — the
numbers glTF stores against one node. That is the right input for asking
whether a curve is smooth, and it is the wrong input, structurally, for asking
anything about where a body part IS.

A foot is the case that makes it obvious. `foot_skate` wants the frames where a
planted foot slides, so it needs the foot's ground-plane position over time. On
a skinned humanoid the foot bone has **no translation channel at all** — it is
parented to a shin, which is parented to a thigh, which is parented to a hip,
and the foot moves entirely because those ancestors rotate. Its own local
translation is one constant key. Verified on both shipped characters of the
project this was written for: LeftFoot and RightFoot carry a `rotation` channel
and nothing else, in all six clips, in both files. So `foot_skate` — a gate
with a threshold, a verdict, a docstring citing Kovar — has never once run on a
real character. It could not. It was reading a constant.

Forward kinematics is the missing step: compose each joint's local transform
onto its parent's, down from the scene root, once per frame. What comes out is
a trajectory per joint in the same world the mesh lives in, which is what
`foot_skate`, `arc_deviation`, and every question of the form "how did this
part of the body move" actually needed all along.

WHAT THIS RETURNS ARE JOINT ORIGINS, NOT BONES. glTF stores joints as nodes and
stores no bone tails whatsoever — a leaf joint's extent is invented by whatever
importer reads the file, which is a trap documented against `template_deviation`
in blender.py. Nothing here invents one. A "bone" in this module is the segment
between a joint and its parent joint, both of which are stored facts.

No Blender, no Godot, no third-party glTF library — the same reasoning as
`skinweights.py`, whose reader this borrows.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

from .animcurves import _accessor_values, _read_glb

# glTF is Y-up by specification. Named rather than spelled 1 at each call site,
# because the one place this project got it wrong was a Z-up export read with
# the default, and a wrong axis looks exactly like a foot that never plants.
UP_AXIS = 1


# ---------------------------------------------------------------------------
# The small amount of linear algebra this needs, written out
# ---------------------------------------------------------------------------

def _quat_slerp(a, b, t: float):
    """Spherical interpolation between two unit quaternions.

    THE SPEC SAYS SLERP AND THE OBVIOUS IMPLEMENTATION IS LERP. glTF's LINEAR
    interpolation on a `rotation` channel is defined as spherical, and a
    component-wise lerp of two quaternions is neither unit-length nor
    constant-rate: it cuts the chord instead of following the arc, so a joint
    interpolated that way arrives early, slows at the midpoint, and drifts off
    the sphere on the way. On a 90-degree key pair the midpoint error is about
    8 degrees — small enough to look plausible in a still and exactly the sort
    of thing that turns into a wrong measurement rather than a visible bug.

    Falls back to lerp only for nearly-parallel inputs, where the arc and the
    chord agree to within float precision and the sine denominator does not.
    """
    dot = sum(x * y for x, y in zip(a, b))
    # THE DOUBLE COVER AGAIN. q and -q are the same rotation, and interpolating
    # toward the far one takes the long way round the sphere — a joint that
    # spins 350 degrees to reach a pose 10 degrees away.
    if dot < 0.0:
        b = tuple(-c for c in b)
        dot = -dot
    dot = min(1.0, max(-1.0, dot))
    if dot > 0.9995:
        out = tuple(x + (y - x) * t for x, y in zip(a, b))
        norm = math.sqrt(sum(c * c for c in out)) or 1.0
        return tuple(c / norm for c in out)
    theta = math.acos(dot)
    sin_theta = math.sin(theta)
    wa = math.sin((1.0 - t) * theta) / sin_theta
    wb = math.sin(t * theta) / sin_theta
    return tuple(x * wa + y * wb for x, y in zip(a, b))


def _trs_matrix(translation, rotation, scale) -> list[list[float]]:
    """A node's local matrix from its TRS, in glTF's own T * R * S order."""
    x, y, z, w = rotation
    rot = (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )
    return [[rot[r][c] * scale[c] for c in range(3)] + [translation[r]]
            for r in range(3)] + [[0.0, 0.0, 0.0, 1.0]]


def _mat_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)]
            for r in range(4)]


_IDENTITY = [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]


# ---------------------------------------------------------------------------
# Sampling one animation channel at an arbitrary time
# ---------------------------------------------------------------------------

def sample_channel(times: list[float], values: list, interpolation: str,
                   t: float, *, rotation: bool = False):
    """The channel's value at time `t`, by glTF's own interpolation rules.

    Outside the channel's own time range the endpoint is held, which is what
    the spec requires and is also the only answer that does not invent motion.

    CUBICSPLINE stores three values per key — in-tangent, value, out-tangent —
    so its `values` list is three times as long as its `times` list. Handled
    because the spec allows it and other exporters emit it; neither character
    this was written against uses it, so treat that path as implemented rather
    than as proven.
    """
    n = len(times)
    if n == 0:
        return None
    cubic = interpolation == "CUBICSPLINE"
    def value_at(i):
        return values[i * 3 + 1] if cubic else values[i]
    if n == 1 or t <= times[0]:
        return value_at(0)
    if t >= times[-1]:
        return value_at(n - 1)
    hi = 0
    while hi < n and times[hi] < t:
        hi += 1
    lo = max(0, hi - 1)
    if hi >= n:
        return value_at(n - 1)
    span = times[hi] - times[lo]
    u = 0.0 if span <= 1e-12 else (t - times[lo]) / span
    if interpolation == "STEP":
        return value_at(lo)
    a, b = value_at(lo), value_at(hi)
    if cubic:
        # Hermite, with the spec's tangent scaling by the key delta.
        out_tangent = values[lo * 3 + 2]
        in_tangent = values[hi * 3]
        u2, u3 = u * u, u * u * u
        h00 = 2 * u3 - 3 * u2 + 1
        h10 = u3 - 2 * u2 + u
        h01 = -2 * u3 + 3 * u2
        h11 = u3 - u2
        out = tuple(h00 * a[k] + h10 * span * out_tangent[k]
                    + h01 * b[k] + h11 * span * in_tangent[k]
                    for k in range(len(a)))
        if rotation:
            norm = math.sqrt(sum(c * c for c in out)) or 1.0
            out = tuple(c / norm for c in out)
        return out
    if rotation:
        return _quat_slerp(a, b, u)
    return tuple(x + (y - x) * u for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# The evaluator
# ---------------------------------------------------------------------------

def _unmeasured(reason: str, **extra) -> dict:
    return {"ok": False, "measured": False, "reason": reason, **extra}


def _node_names(gltf: dict) -> list[str]:
    return [n.get("name") or f"node{i}"
            for i, n in enumerate(gltf.get("nodes") or [])]


def _model_height(gltf: dict, up_axis: int = UP_AXIS) -> Optional[float]:
    """The mesh's own extent along the up axis, from the accessor bounds.

    glTF requires a POSITION accessor to carry `min`/`max`, so this is a read
    rather than a scan of every vertex. Used only to put a floor under the
    contact band — see `_contact_frames` for why a purely relative band is not
    safe on its own.
    """
    low, high = None, None
    for mesh in gltf.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            index = (prim.get("attributes") or {}).get("POSITION")
            if index is None:
                continue
            acc = (gltf.get("accessors") or [])[index]
            lo, hi = acc.get("min"), acc.get("max")
            if not lo or not hi or len(lo) <= up_axis:
                continue
            low = lo[up_axis] if low is None else min(low, lo[up_axis])
            high = hi[up_axis] if high is None else max(high, hi[up_axis])
    if low is None or high is None or high - low <= 1e-9:
        return None
    return high - low


def _parents(gltf: dict) -> dict[int, int]:
    out: dict[int, int] = {}
    for i, node in enumerate(gltf.get("nodes") or []):
        for child in node.get("children") or []:
            out[child] = i
    return out


def _clip_times(anim: dict, gltf: dict, bin_data: bytes,
                fps: Optional[float]) -> list[float]:
    """The grid to evaluate this clip on.

    THE UNION OF EVERY CHANNEL'S OWN KEY TIMES, not the densest channel's grid
    and not a resampled one. Channels in a single clip routinely disagree about
    where their keys are, and any grid that is not the union silently drops a
    key from whichever channel is finer — which shows up later as a joint that
    "did not move" on the frame it actually snapped. Passing `fps` overrides
    this with a uniform grid, for a caller that needs one; it can only lose
    information, never add any, so it is not the default.
    """
    if fps:
        starts, ends = [], []
        for ch in anim.get("channels") or []:
            sampler = anim["samplers"][ch["sampler"]]
            times = _accessor_values(gltf, bin_data, sampler["input"])
            if times:
                starts.append(times[0])
                ends.append(times[-1])
        if not starts:
            return []
        start, end = min(starts), max(ends)
        count = max(2, int(round((end - start) * fps)) + 1)
        return [start + (end - start) * i / (count - 1) for i in range(count)]
    seen: list[float] = []
    for ch in anim.get("channels") or []:
        sampler = anim["samplers"][ch["sampler"]]
        seen.extend(_accessor_values(gltf, bin_data, sampler["input"]))
    if not seen:
        return []
    seen.sort()
    grid = [seen[0]]
    for t in seen[1:]:
        if t - grid[-1] > 1e-6:
            grid.append(t)
    return grid


def joint_paths(path: str | Path, *, clip: Optional[str] = None,
                fps: Optional[float] = None,
                joints: Optional[list[str]] = None) -> dict:
    """Every joint's world-space position, per frame, for each clip in a GLB.

    Returns {ok, up_axis, clips: [{name, times, positions: {joint: [(x, y, z),
    ...]}, root_motion: bool}]}. `positions` is in the file's own world space,
    which is the space the mesh is in, Y-up per the glTF specification.

    `clip` selects one animation by name; `joints` restricts the output to the
    named nodes (their ancestors are still evaluated — they have to be). `fps`
    forces a uniform grid instead of the union of key times; see `_clip_times`
    for why that is not the default.

    A node carrying a `matrix` rather than TRS is used as-is: the spec forbids
    animating such a node, so it can only be a fixed transform in the chain.

    `root_motion` says whether the clip translates the skeleton's own root
    node. It matters for reading everything else: on an in-place clip the feet
    cycle under a stationary hip and the ground-plane travel of a planted foot
    IS the stride, while on a root-motion clip the whole body advances and a
    planted foot should be near-still in world space. The same foot trajectory
    means opposite things in the two cases, so the flag is reported rather than
    assumed.
    """
    src = Path(path)
    if not src.is_file():
        return _unmeasured(f"no file at {src}")
    try:
        gltf, bin_data = _read_glb(src)
    except Exception as exc:  # a malformed file is an unknown, not a pass
        return _unmeasured(str(exc))

    nodes = gltf.get("nodes") or []
    if not nodes:
        return _unmeasured("the file declares no nodes — there is no hierarchy "
                           "to walk")
    animations = gltf.get("animations") or []
    if not animations:
        return _unmeasured("the file carries no animations — a rigged but "
                           "unanimated export has no trajectories to compute")

    names = _node_names(gltf)
    parents = _parents(gltf)
    index_of = {name: i for i, name in enumerate(names)}

    skins = gltf.get("skins") or []
    skin_joints = list(skins[0].get("joints") or []) if skins else []
    # NO SKIN IS NOT AN ERROR HERE. A prop, a camera rig or a hand-built
    # hierarchy animates nodes with no skinning anywhere in the file, and its
    # trajectories are as real as a character's. Fall back to every node that
    # something in the file actually animates.
    if not skin_joints:
        skin_joints = sorted({ch["target"]["node"]
                              for anim in animations
                              for ch in anim.get("channels") or []
                              if (ch.get("target") or {}).get("node") is not None})
    if not skin_joints:
        return _unmeasured("no skin and no animated node — nothing here has a "
                           "trajectory")

    wanted = set(skin_joints)
    if joints:
        named = {index_of[n] for n in joints if n in index_of}
        if not named:
            return _unmeasured("none of the requested joints are in this file: "
                               + ", ".join(joints))
        wanted = named
    # Every ancestor has to be evaluated whether or not it was asked for.
    needed = set()
    for j in wanted:
        walk = j
        while walk is not None and walk not in needed:
            needed.add(walk)
            walk = parents.get(walk)
    # Parents strictly before children, so one pass composes the whole tree.
    def depth(i: int) -> int:
        d, walk = 0, parents.get(i)
        while walk is not None:
            d += 1
            walk = parents.get(walk)
        return d
    ordered = sorted(needed, key=depth)

    rest = {}
    for i in ordered:
        node = nodes[i]
        if "matrix" in node:
            m = node["matrix"]  # glTF matrices are column-major
            rest[i] = [[m[c * 4 + r] for c in range(4)] for r in range(4)]
        else:
            rest[i] = _trs_matrix(node.get("translation") or [0.0, 0.0, 0.0],
                                  node.get("rotation") or [0.0, 0.0, 0.0, 1.0],
                                  node.get("scale") or [1.0, 1.0, 1.0])

    clips = []
    for anim in animations:
        name = anim.get("name", "")
        if clip is not None and name != clip:
            continue
        tracks: dict[int, dict[str, dict]] = {}
        for ch in anim.get("channels") or []:
            target = ch.get("target") or {}
            node_index = target.get("node")
            if node_index is None or node_index not in needed:
                continue
            sampler = anim["samplers"][ch["sampler"]]
            tracks.setdefault(node_index, {})[target.get("path")] = {
                "times": _accessor_values(gltf, bin_data, sampler["input"]),
                "values": _accessor_values(gltf, bin_data, sampler["output"]),
                "interpolation": sampler.get("interpolation", "LINEAR"),
            }
        times = _clip_times(anim, gltf, bin_data, fps)
        if len(times) < 2:
            clips.append({"name": name, "times": times, "positions": {},
                          "measured": False,
                          "reason": "this clip carries fewer than two distinct "
                                    "key times — there is no trajectory in it"})
            continue

        positions: dict[str, list] = {names[i]: [] for i in wanted}
        root = min(needed, key=depth)
        root_travel = 0.0
        previous_root = None
        for t in times:
            world: dict[int, list[list[float]]] = {}
            for i in ordered:
                track = tracks.get(i)
                if track and "matrix" not in nodes[i]:
                    node = nodes[i]
                    translation = node.get("translation") or [0.0, 0.0, 0.0]
                    rotation = node.get("rotation") or [0.0, 0.0, 0.0, 1.0]
                    scale = node.get("scale") or [1.0, 1.0, 1.0]
                    if "translation" in track:
                        tr = track["translation"]
                        translation = sample_channel(tr["times"], tr["values"],
                                                     tr["interpolation"], t)
                    if "rotation" in track:
                        ro = track["rotation"]
                        rotation = sample_channel(ro["times"], ro["values"],
                                                  ro["interpolation"], t,
                                                  rotation=True)
                    if "scale" in track:
                        sc = track["scale"]
                        scale = sample_channel(sc["times"], sc["values"],
                                               sc["interpolation"], t)
                    local = _trs_matrix(translation, rotation, scale)
                else:
                    local = rest[i]
                parent = parents.get(i)
                world[i] = _mat_mul(world[parent], local) if parent in world \
                    else _mat_mul(_IDENTITY, local)
            for i in wanted:
                m = world[i]
                positions[names[i]].append((m[0][3], m[1][3], m[2][3]))
            rm = world[root]
            here = (rm[0][3], rm[1][3], rm[2][3])
            if previous_root is not None:
                root_travel += math.dist(here, previous_root)
            previous_root = here
        clips.append({"name": name, "times": times, "positions": positions,
                      "measured": True,
                      "root_motion": root_travel > 1e-4,
                      "root_travel": round(root_travel, 5),
                      "root_joint": names[root]})

    if not clips:
        return _unmeasured(f"no clip named {clip!r} in {src}"
                           if clip else "no clips were evaluated")
    return {"ok": True, "measured": True, "up_axis": UP_AXIS,
            "model_height": _model_height(gltf), "clips": clips}


# ---------------------------------------------------------------------------
# What world-space positions make answerable that local channels never did
# ---------------------------------------------------------------------------

def ground_clearance(positions: list, *, up_axis: int = UP_AXIS,
                     floor: Optional[float] = None,
                     tolerance: float = 0.01) -> dict:
    """How far this joint goes below the floor, and on how many frames.

    A JOINT THAT SINKS THROUGH THE GROUND IS THE ONE ANIMATION FAULT A PLAYER
    NEVER MISSES, and it is invisible to every local-channel measurement in
    this project: a foot's own rotation curve is perfectly smooth while the
    thigh above it drives it a hand's width into the floor.

    `floor` defaults to the lowest point this joint reaches, which asks the
    weaker question — does it dip below its own resting contact — and needs no
    knowledge of the scene. Pass an explicit floor (usually 0.0) to ask the
    real one.
    """
    if len(positions) < 2:
        return {"measured": False,
                "reason": f"{len(positions)} samples — nothing to trace"}
    heights = [p[up_axis] for p in positions]
    ground = min(heights) if floor is None else floor
    below = [ground - h for h in heights if h < ground - tolerance]
    return {"measured": True, "floor": round(ground, 5),
            "frames_below": len(below),
            "worst_penetration": round(max(below), 5) if below else 0.0,
            "lowest": round(min(heights), 5), "highest": round(max(heights), 5)}


def ground_clearance_verdict(result: dict, *, max_frames_below: int = 0,
                             max_penetration: float = 0.01) -> dict:
    if not result.get("measured", False):
        return {"passed": False, "issues": [{"kind": "unmeasured",
                "note": result.get("reason", "not measured")}]}
    issues = []
    if (result["frames_below"] > max_frames_below
            and result["worst_penetration"] > max_penetration):
        issues.append({"kind": "through_floor",
                       "value": result["worst_penetration"],
                       "note": f"this joint passes {result['worst_penetration'] * 100:.1f} cm "
                               f"below the floor on {result['frames_below']} "
                               "frames"})
    return {"passed": not issues, "issues": issues}


# ---------------------------------------------------------------------------
# Contact: what a planted foot is actually doing
# ---------------------------------------------------------------------------
# WHY THE OLD CHECK COULD NOT ASK THIS. `animcurves.foot_skate` takes a list of
# positions and looks for a foot that is low and still moving. Its logic is
# sound; it was simply never handed a foot. It is wired to a `translation`
# channel, and a foot bone on a skinned humanoid has none — so on both shipped
# characters, in every clip, it read one constant key and honestly reported
# that it could not measure anything. With world positions it can.
#
# AND THEN IT IS STILL THE WRONG QUESTION, HALF THE TIME. "A planted foot must
# not slide" is true only of a clip that moves the character. An IN-PLACE
# locomotion clip — which is what both of these files carry, root travel 0.0 on
# every clip — works the opposite way round: the body stays put and the ground
# is imagined to move, so the planted foot MUST slide, at exactly the speed the
# character is supposed to be travelling. Sliding is the animation, not the
# fault.
#
# What is wrong in that case is sliding at a VARYING speed, because that reads
# as the ground accelerating and braking under the character. So the same
# measurement — ground-plane speed while in contact — is judged against zero on
# a root-motion clip and against its own mean on an in-place one, and
# `joint_paths` reports `root_motion` so the caller does not have to guess.
#
# THE CONTACT BAND IS A FRACTION OF THE FOOT'S OWN LIFT, never a distance in
# metres. Measured on the two characters here: a human foot lifts 7.0 cm in its
# walk and a cat's paw lifts 1.4 cm in the same gait, so a 3 cm band that is
# reasonable for one swallows the entire cycle of the other and reports every
# frame as planted.

def _contact_frames(positions: list, up_axis: int, band_fraction: float,
                    band_absolute: Optional[float],
                    model_height: Optional[float] = None,
                    floor_fraction: float = 0.01) -> tuple[list[bool], float]:
    """Which frames have this foot within the contact band of its own lowest
    point, and how wide that band came out.

    A BAND PURELY RELATIVE TO THE FOOT'S OWN LIFT COLLAPSES ON A CLIP WHERE THE
    FOOT DOES NOT LIFT, and then reports the exact opposite of the truth.
    Measured: a standing idle whose feet move 0.1 mm across 91 frames gave a
    band of 0.025 mm, so 79% of its frames read as FLIGHT — a character
    standing still, judged airborne for most of a four-second clip.

    The relative band still has to exist, for the opposite reason: a human foot
    lifts 7.0 cm in its walk and a cat's paw 1.4 cm in the same gait, so no
    single distance in metres serves both characters in one project.

    So: the larger of the two. A fraction of the foot's own lift, floored at
    `floor_fraction` of MODEL HEIGHT — 1%, which is 1.75 cm on a 1.75 m figure
    and 2.7 mm on a 27 cm cat. A foot nearer the ground than that is planted,
    whatever else the clip does. With no model height the floor cannot be
    computed and only the relative band applies.
    """
    heights = [p[up_axis] for p in positions]
    low, high = min(heights), max(heights)
    if band_absolute is not None:
        band = band_absolute
    else:
        band = (high - low) * band_fraction
        if model_height:
            band = max(band, model_height * floor_fraction)
        band = max(band, 1e-6)
    return [h <= low + band for h in heights], band


def contact_slide(positions: list, times: list[float], *,
                  root_motion: bool, up_axis: int = UP_AXIS,
                  band_fraction: float = 0.25,
                  band_absolute: Optional[float] = None,
                  model_height: Optional[float] = None) -> dict:
    """Ground-plane speed of this foot while it is in contact.

    Returns the speeds, their mean and their coefficient of variation, plus
    `worst_slide` — the largest single-frame ground-plane step during contact,
    which is the number the root-motion case is judged on. `root_motion` is
    carried into the result so the verdict cannot be applied against the wrong
    convention by accident.
    """
    if len(positions) < 3 or len(times) != len(positions):
        return {"measured": False,
                "reason": f"{len(positions)} positions against {len(times)} "
                          "times — nothing can be traced through that"}
    contact, band = _contact_frames(positions, up_axis, band_fraction,
                                    band_absolute, model_height)
    horizontal = [k for k in range(3) if k != up_axis]
    # THE TOUCHDOWN AND LIFT-OFF FRAMES ARE THROWN AWAY, and without that this
    # measurement mostly reports how wide the band was. A foot arriving at the
    # ground and a foot leaving it are legitimately accelerating, so any band
    # generous enough to include those frames inflates the spread: measured on
    # one human walk, the same foot scored 0.024 at a tight band and 0.556 at
    # a loose one, purely from how much of the arc got counted as "planted".
    # Dropping the first and last step of each contact run holds it to
    # 0.024-0.079 across every band between 10% and 40% of the foot's lift —
    # still not band-independent at the extremes, and honest about that.
    runs, start = [], None
    for i, down in enumerate(list(contact) + [False]):
        if down and start is None:
            start = i
        elif not down and start is not None:
            runs.append((start, i - 1))
            start = None
    speeds, steps = [], []
    for first, last in runs:
        for i in range(first + 2, last + 1):
            step = math.dist(tuple(positions[i][k] for k in horizontal),
                             tuple(positions[i - 1][k] for k in horizontal))
            dt = times[i] - times[i - 1]
            steps.append(step)
            if dt > 1e-9:
                speeds.append(step / dt)
    if len(speeds) < 2:
        return {"measured": False,
                "reason": ("this foot is never planted for three consecutive "
                           "frames, so there is no steady part of a plant to "
                           "judge — only touchdowns and lift-offs, which are "
                           "supposed to accelerate. A contact band too tight "
                           "for the clip's frame rate, and a foot that "
                           "genuinely never lands, both look like this"),
                "contact_frames": sum(contact), "plants": len(runs),
                "band": round(band, 5)}
    # A FOOT THAT IS NOT GOING ANYWHERE HAS NO PACING, and a coefficient of
    # variation says the opposite about it as loudly as it can. On a standing
    # idle the planted foot drifts at 0.0007 m/s of float noise, whose relative
    # spread is 0.60 — so the gate reported an uneven plant on a character
    # standing still, in a note that printed "recedes at 0.00 to 0.00 m/s".
    # The same disease as an absolute dead-channel floor in animcurves: a ratio
    # needs its denominator to be real before it means anything.
    # TWO FLOORS, BECAUSE A FOOT CAN FAIL TO BE PACED IN TWO WAYS. The travel
    # floor catches a foot that never goes anywhere at all. The SPEED floor
    # catches the one that shuffles: measured on a standing clip, a foot that
    # crept 2.5 cm over three seconds scored a variation of 1.12 and was
    # reported as an uneven plant, on a character who was standing still
    # adjusting their weight. A coefficient of variation needs its mean to be
    # a real speed, not just a non-zero one — 2% of model height per second is
    # 3.5 cm/s on a 1.75 m figure, well under any locomotion and well over any
    # shuffle.
    travel = sum(steps)
    mean_speed = sum(speeds) / len(speeds)
    if model_height and mean_speed < model_height * 0.02:
        return {"measured": False,
                "reason": (f"this foot creeps at {mean_speed * 100:.1f} cm/s "
                           "while planted — that is a weight shift, not a "
                           "stride, and there is no locomotion pacing in it "
                           "to judge"),
                "contact_frames": sum(contact), "plants": len(runs),
                "mean_speed": round(mean_speed, 5), "band": round(band, 5)}
    if model_height and travel < model_height * 0.005:
        return {"measured": False,
                "reason": (f"this foot travels {travel * 1000:.2f} mm along the "
                           "ground across the whole plant — it is standing on "
                           "the spot, not receding, and there is no pacing in "
                           "that to judge either way"),
                "contact_frames": sum(contact), "plants": len(runs),
                "ground_travel": round(travel, 6), "band": round(band, 5)}
    mean = sum(speeds) / len(speeds)
    sd = (sum((s - mean) ** 2 for s in speeds) / len(speeds)) ** 0.5
    return {"measured": True, "root_motion": root_motion,
            "contact_frames": sum(contact), "plants": len(runs),
            "steady_steps": len(speeds), "band": round(band, 5),
            "mean_speed": round(mean, 4), "sd_speed": round(sd, 4),
            "variation": round(sd / mean, 4) if mean > 1e-9 else None,
            "min_speed": round(min(speeds), 4),
            "max_speed": round(max(speeds), 4),
            "worst_slide": round(max(steps), 5)}


def contact_slide_verdict(result: dict, *, max_slide: float = 0.02,
                          max_variation: float = 0.20) -> dict:
    """Judge a contact trace against the convention its clip actually uses.

    `max_slide` (2 cm per frame) applies to a ROOT-MOTION clip, where a planted
    foot should be still in world space.

    `max_variation` (0.20) applies to an IN-PLACE clip, where the planted foot
    is supposed to recede steadily: it is the coefficient of variation of the
    steady-plant speed, so 0.20 allows a fifth of the mean in spread.

    THAT NUMBER IS TIED TO THE DEFAULT CONTACT BAND and cannot be quoted
    without one. Variation rises monotonically with band width, because a
    wider band counts more of the arc where the foot is arriving or leaving —
    measured on one human walk, the same foot scored 0.024, 0.080, 0.283 and
    0.556 at bands of 0.5, 2, 3 and 5 cm. Trimming the touchdown and lift-off
    steps (see `contact_slide`) holds it to 0.024-0.079 from 10% to 40% of
    the foot's own lift, which is what makes a fixed threshold defensible at
    all. Move the band and this bound moves with it.
    """
    if not result.get("measured", False):
        return {"passed": False, "issues": [{"kind": "unmeasured",
                "note": result.get("reason", "not measured")}]}
    issues = []
    if result["root_motion"]:
        if result["worst_slide"] > max_slide:
            issues.append({"kind": "foot_skate", "value": result["worst_slide"],
                           "note": f"the planted foot moves "
                                   f"{result['worst_slide'] * 100:.1f} cm in one "
                                   "frame while in contact, on a clip that "
                                   "carries its own root motion — the foot is "
                                   "sliding on the ground"})
    else:
        variation = result.get("variation")
        if variation is not None and variation > max_variation:
            issues.append({"kind": "uneven_plant", "value": variation,
                           "note": f"the planted foot recedes at "
                                   f"{result['min_speed']:.2f} to "
                                   f"{result['max_speed']:.2f} m/s within one "
                                   "plant. On an in-place clip the foot is the "
                                   "ground: varying its speed reads as the "
                                   "floor accelerating under the character"})
    return {"passed": not issues, "issues": issues,
            "convention": "root_motion" if result["root_motion"] else "in_place"}


def support_phases(feet: dict, times: list[float], *, up_axis: int = UP_AXIS,
                   band_fraction: float = 0.25,
                   band_absolute: Optional[float] = None,
                   model_height: Optional[float] = None) -> dict:
    """How many feet are on the ground on each frame.

    `feet` maps a foot's name to its world positions. Returns the per-frame
    count, the histogram, and `flight_frames` — frames with nothing down.
    """
    if not feet:
        return {"measured": False, "reason": "no feet were named"}
    contacts = {}
    for name, positions in feet.items():
        if len(positions) != len(times) or len(positions) < 2:
            return {"measured": False,
                    "reason": f"{name} has {len(positions)} positions against "
                              f"{len(times)} times"}
        contacts[name], _ = _contact_frames(positions, up_axis, band_fraction,
                                            band_absolute, model_height)
    counts = [sum(contacts[n][i] for n in feet) for i in range(len(times))]
    flight = sum(1 for c in counts if c == 0)
    histogram = {}
    for c in counts:
        histogram[c] = histogram.get(c, 0) + 1
    return {"measured": True, "feet": sorted(feet), "frames": len(times),
            "flight_frames": flight,
            "flight_fraction": round(flight / len(times), 4),
            "histogram": {str(k): v for k, v in sorted(histogram.items())},
            "counts": counts}


def support_verdict(result: dict, gait: Optional[str] = None, *,
                    max_flight_fraction: Optional[float] = None) -> dict:
    """Judge a support trace — but ONLY against a declared gait.

    A GAIT MUST BE DECLARED AND THERE IS NO DEFAULT. Flight — every foot off
    the ground at once — is the definition of a run and is impossible in a
    walk, so the identical measurement is a pass or a failure depending
    entirely on what the clip was meant to be. Guessing from the clip's name
    would make this gate wrong on any project that names its clips anything
    else, and quietly. So an undeclared gait returns a REFUSAL carrying the
    measurement, not a pass: the number is there to read, and nothing is
    certified.

    `gait`: "walk" (no flight permitted), "run" (flight expected, and its
    complete absence is itself worth reporting), "stand" (everything down
    throughout), or "any" to accept whatever was measured.
    `max_flight_fraction` overrides the gait's own bound.
    """
    if not result.get("measured", False):
        return {"passed": False, "issues": [{"kind": "unmeasured",
                "note": result.get("reason", "not measured")}]}
    bounds = {"walk": 0.0, "run": 1.0, "stand": 0.0, "any": 1.0}
    if gait is None and max_flight_fraction is None:
        return {"passed": False, "gait": None,
                "issues": [{"kind": "unmeasured",
                            "note": "no gait was declared, so the flight "
                                    f"fraction of {result['flight_fraction']:.0%} "
                                    "was measured but not judged — the same "
                                    "number is correct for a run and "
                                    "impossible for a walk"}]}
    if gait is not None and gait not in bounds:
        return {"passed": False, "gait": gait,
                "issues": [{"kind": "unmeasured",
                            "note": f"unknown gait {gait!r}; expected one of "
                                    + ", ".join(sorted(bounds))}]}
    limit = max_flight_fraction if max_flight_fraction is not None \
        else bounds[gait]
    issues = []
    if result["flight_fraction"] > limit:
        issues.append({"kind": "flight", "value": result["flight_fraction"],
                       "note": f"every foot is off the ground on "
                               f"{result['flight_frames']} of "
                               f"{result['frames']} frames "
                               f"({result['flight_fraction']:.0%}). A {gait} "
                               "has no flight phase — that is the thing that "
                               "makes it a walk rather than a run"})
    if gait == "run" and result["flight_frames"] == 0:
        issues.append({"kind": "no_flight",
                       "note": "a run with no frame where every foot leaves "
                               "the ground is a fast walk"})
    if gait == "stand" and min(result["counts"]) < len(result["feet"]):
        issues.append({"kind": "foot_lifts",
                       "note": "a foot leaves the ground in a clip declared "
                               "as a stand"})
    return {"passed": not issues, "gait": gait, "issues": issues}
