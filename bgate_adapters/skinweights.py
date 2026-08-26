"""Skin-weight plausibility, read straight off an exported glTF/GLB.

THE QUESTION NOTHING ELSE IN THIS PRODUCT ASKS: is each vertex driven by a
bone that is anywhere NEAR it?

Found on a shipped character. A cat's animations were reported as "tearing",
with the tail wag in idle the only motion that read as smooth. Every rig gate
in the product passed it:

  * `blender_rig`'s `unweighted` count was 0 — every vertex had weight.
  * every vertex's weights summed to exactly 1.0.
  * `blender_weights` (bleed) passed — it reports a bone whose paint splits
    into more connected components than the mesh pieces it touches, and the
    offending bone's paint here was ONE connected patch. It simply ran too
    far down the legs, which is not a split.
  * `blender_flex` passed — its six test poses did not happen to open the
    seam far enough to trip the volume/pinch bounds.
  * `blender_template_deviation` passed — the SKELETON was correct. Bone
    lengths, names and parenting were all fine. Only the paint was wrong.

The actual defect: 42% of the vertices in the lower third of the model — the
legs and paws — had their dominant weight on `spine`, `hips`, `chest` or
`neck`. The worst were at y=0.005, ON THE FLOOR, driven by a bone 0.15 m up
inside the body. Geometry like that physically cannot follow the leg it
belongs to, so the leg surface stretches away from the body the moment the
leg swings. It is invisible in bind pose, which is what every stand-up
photograph in the pipeline captures.

WHY DISTANCE TO THE BONE SEGMENT, NOT TO THE JOINT. A vertex halfway down a
thigh is far from the hip joint AND far from the knee, so a joint-origin
measure calls correctly-painted limb geometry an outlier. Bones are segments
(joint to child joint); the distance that means anything is to the segment.
Leaf bones have no child, so they get a stub along the parent's direction.

WHY A RATIO AND NOT AN ABSOLUTE DISTANCE. Characters differ in size by
orders of magnitude across a project, and a tolerance in metres would have to
be retuned per asset — which means it would not be run. What is asked instead
is comparative and unit-free: how much farther is the bone that DRIVES this
vertex than the nearest bone that could have? A well-painted vertex sits at
or near 1.0. A vertex whose driver is three times farther away than the
nearest available bone is not a style choice.

THIS IS A PLAUSIBILITY CHECK, NOT A CORRECTNESS PROOF. Nearest-bone is a
heuristic and it is wrong in specific, knowable places: a shoulder legitimately
drives geometry closer to the ribs, a jaw sits near the skull, and any two
bones that pass close together (a tail against a thigh, an arm against a
torso) will trade vertices. That is why the default tolerance is a GROSS-ERROR
line rather than a fidelity one, and why the verdict names the bones so a
human can dismiss the two that are anatomy and act on the twenty that are not.

No Blender, no Godot, no third-party glTF library — the same reasoning as
animcurves.py, whose reader this borrows.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

from .animcurves import _accessor_values, _read_glb


def _bind_positions(gltf: dict, bin_data: bytes, skin: dict) -> list[tuple]:
    """Each joint's rest position in mesh space.

    The inverse-bind matrix maps mesh space into that joint's space, so the
    joint's own origin in mesh space is the translation of its INVERSE. For a
    rigid transform that is -R^T t, which avoids a general 4x4 inversion.
    glTF matrices are column-major.
    """
    ibm_index = skin.get("inverseBindMatrices")
    joints = skin.get("joints") or []
    if ibm_index is None:
        # No IBMs means identity binds; fall back to node translations.
        out = []
        for j in joints:
            node = gltf["nodes"][j]
            t = node.get("translation") or [0.0, 0.0, 0.0]
            out.append(tuple(float(v) for v in t))
        return out
    mats = _accessor_values(gltf, bin_data, ibm_index)
    out = []
    for m in mats:
        r = ((m[0], m[4], m[8]), (m[1], m[5], m[9]), (m[2], m[6], m[10]))
        t = (m[12], m[13], m[14])
        out.append(tuple(-sum(r[k][i] * t[k] for k in range(3))
                         for i in range(3)))
    return out


def _bone_segments(gltf: dict, skin: dict,
                   positions: list[tuple]) -> list[tuple]:
    """(head, tail) per joint, in the skin's joint order.

    A joint's tail is the mean of its children's heads — for a limb that is
    simply the next joint down, and for a branch point (a hip with two legs,
    a chest with two shoulders) the average keeps the stub pointing into the
    body it actually spans rather than picking one child arbitrarily. A leaf
    gets a short stub continuing its parent's direction, so a fingertip or a
    tail tip still has length to measure against.
    """
    joints = skin.get("joints") or []
    index_of = {node_index: i for i, node_index in enumerate(joints)}
    children: dict[int, list[int]] = {i: [] for i in range(len(joints))}
    parent_of: dict[int, int] = {}
    for i, node_index in enumerate(joints):
        for child in gltf["nodes"][node_index].get("children") or []:
            if child in index_of:
                children[i].append(index_of[child])
                parent_of[index_of[child]] = i

    # a reasonable stub length: a fraction of the whole skeleton's extent
    xs = [p[0] for p in positions] or [0.0]
    ys = [p[1] for p in positions] or [0.0]
    zs = [p[2] for p in positions] or [0.0]
    extent = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) or 1.0
    stub = extent * 0.02

    segments = []
    for i, head in enumerate(positions):
        kids = children.get(i) or []
        if kids:
            tail = tuple(sum(positions[k][a] for k in kids) / len(kids)
                         for a in range(3))
        else:
            p = parent_of.get(i)
            if p is None:
                tail = (head[0], head[1] + stub, head[2])
            else:
                d = [head[a] - positions[p][a] for a in range(3)]
                n = math.sqrt(sum(c * c for c in d)) or 1.0
                tail = tuple(head[a] + d[a] / n * stub for a in range(3))
        segments.append((head, tail))
    return segments


def _point_segment_distance(p: tuple, a: tuple, b: tuple) -> float:
    abx, aby, abz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    denom = abx * abx + aby * aby + abz * abz
    if denom <= 1e-12:
        dx, dy, dz = p[0] - a[0], p[1] - a[1], p[2] - a[2]
        return math.sqrt(dx * dx + dy * dy + dz * dz)
    t = ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby + (p[2] - a[2]) * abz) / denom
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    dx = p[0] - (a[0] + abx * t)
    dy = p[1] - (a[1] + aby * t)
    dz = p[2] - (a[2] + abz * t)
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def _unmeasured(reason: str, **extra) -> dict:
    out = {"ok": False, "measured": False, "reason": reason}
    out.update(extra)
    return out


def dominance(path: str | Path, *, min_weight: float = 0.5,
              min_distance_fraction: float = 0.12,
              sample: int = 0) -> dict:
    """How far each vertex's dominant bone is, against the nearest available.

    `min_weight` (0.5) restricts the check to vertices a single bone actually
    OWNS. Below half, no bone is dominant and the vertex is a genuine blend —
    calling its "dominant" bone wrong would be meaningless.

    `min_distance_fraction` (0.12 of model height) is a NEAR-JOINT FLOOR, and
    it exists because the ratio alone has a failure mode that this check hit on
    its own first real fix. Where two bone segments nearly meet — a paw against
    a shin, a shoulder against a chest — the distance to the NEAREST bone
    collapses toward zero, so the ratio explodes for a vertex that is
    perfectly well painted. Measured, on a re-skinned cat whose actual defect
    had just been repaired: a vertex 0.023 m from `backpaw_R` on a 0.272 m
    model scored 9.0, purely because `shin_R` happened to pass 0.0026 m away.
    2 cm from its own bone on a 27 cm animal is not a fault.

    The defect this tool exists to catch does not hide down there. The original
    mis-binding's worst vertices sat 0.076-0.116 m from their bone — 28% to 43%
    of model height — so a floor at 12% keeps every one of them and drops the
    joint-boundary noise. A vertex nearer its bone than that is not evidence of
    anything either way.

    `sample` caps the vertices examined (0 = all). Every mesh in this pipeline
    is small enough to run whole; the cap exists for imported assets that are
    not.
    """
    gltf, bin_data = _read_glb(path)
    skins = gltf.get("skins") or []
    if not skins:
        return _unmeasured("no skin in the file — nothing is rigged")
    skin = skins[0]
    joints = skin.get("joints") or []
    if not joints:
        return _unmeasured("the skin declares no joints")
    names = [gltf["nodes"][j].get("name") or f"joint_{j}" for j in joints]
    positions = _bind_positions(gltf, bin_data, skin)
    segments = _bone_segments(gltf, skin, positions)

    verts: list[tuple] = []
    weights: list[tuple] = []
    joint_idx: list[tuple] = []
    for mesh in gltf.get("meshes") or []:
        for prim in mesh.get("primitives") or []:
            attrs = prim.get("attributes") or {}
            if "JOINTS_0" not in attrs or "WEIGHTS_0" not in attrs:
                continue
            verts.extend(_accessor_values(gltf, bin_data, attrs["POSITION"]))
            weights.extend(_accessor_values(gltf, bin_data, attrs["WEIGHTS_0"]))
            joint_idx.extend(_accessor_values(gltf, bin_data, attrs["JOINTS_0"]))
    if not verts:
        return _unmeasured("no skinned primitive carries JOINTS_0/WEIGHTS_0")

    wacc = gltf["accessors"][
        (gltf["meshes"][0]["primitives"][0]["attributes"])["WEIGHTS_0"]]
    scale = {5121: 255.0, 5123: 65535.0}.get(wacc.get("componentType"), 1.0)

    step = 1
    if sample and len(verts) > sample:
        step = len(verts) // sample + 1

    ys = [v[1] for v in verts]
    height = (max(ys) - min(ys)) or 1.0
    floor = height * max(min_distance_fraction, 0.0)

    per_bone: dict[str, dict] = {
        n: {"owns": 0, "misbound": 0, "worst_ratio": 0.0} for n in names}
    worst: list[tuple] = []
    influences: dict[int, int] = {}
    checked = 0
    misbound = 0
    ratios: list[float] = []

    # ONLY DEFORM BONES ARE CANDIDATES FOR "NEAREST".
    #
    # A rig carries bones that move other bones and skin nothing — a root, an
    # IK target, a control. They are often placed at the origin or out beyond
    # the body, and on the first run of this check that is exactly what
    # happened: the worst offenders were reported as "driven by spine, but the
    # nearest bone is root", inflating the ratio to 6.91 by comparing against a
    # bone no vertex is allowed to be painted to. The comparison has to be
    # against bones the vertex could actually have been bound to, or the gate
    # generates its own false positives and gets switched off.
    deform: set[int] = set()
    for i in range(0, len(verts), step):
        w = weights[i]
        for k in range(len(w)):
            if w[k] / scale > 0.001:
                deform.add(joint_idx[i][k])
    candidates = [j for j in sorted(deform) if j < len(segments)]
    if not candidates:
        return _unmeasured("no joint carries any weight above 0.001")

    for i in range(0, len(verts), step):
        w = [x / scale for x in weights[i]]
        live = sum(1 for x in w if x > 0.001)
        influences[live] = influences.get(live, 0) + 1
        k = max(range(len(w)), key=lambda a: w[a])
        if w[k] < min_weight:
            continue
        dom = joint_idx[i][k]
        if dom >= len(segments):
            continue
        p = verts[i]
        d_dom = _point_segment_distance(p, *segments[dom])
        d_near = d_dom
        near_j = dom
        for j in candidates:
            d = _point_segment_distance(p, *segments[j])
            if d < d_near:
                d_near = d
                near_j = j
        checked += 1
        b = per_bone[names[dom]]
        b["owns"] += 1
        # too close to its own bone to be evidence either way — see
        # min_distance_fraction. Counted as owned, but it cannot raise a flag.
        if d_dom < floor:
            b["near_joint"] = b.get("near_joint", 0) + 1
            ratios.append(1.0)
            continue
        ratio = d_dom / d_near if d_near > 1e-9 else 1.0
        ratios.append(ratio)
        if ratio > b["worst_ratio"]:
            b["worst_ratio"] = round(ratio, 2)
        worst.append((ratio, i, names[dom], names[near_j],
                      round(d_dom, 4), round(p[1], 4)))

    if not checked:
        return _unmeasured(
            "no vertex is owned by a single bone above min_weight",
            min_weight=min_weight, vertices=len(verts))

    worst.sort(key=lambda r: -r[0])
    ratios.sort()
    driven = {n for n in names if per_bone[n]["owns"]}
    rigid = influences.get(1, 0)
    total_inf = sum(influences.values()) or 1

    return {
        "ok": True,
        "measured": True,
        "path": str(path),
        "vertices": len(verts),
        "checked": checked,
        "bones": len(names),
        "model_height": round(height, 4),
        "near_joint_floor": round(floor, 4),
        "median_ratio": round(ratios[len(ratios) // 2], 3),
        "p95_ratio": round(ratios[int(len(ratios) * 0.95)], 3),
        "max_ratio": round(ratios[-1], 3),
        "rigid_fraction": round(rigid / total_inf, 4),
        "influence_histogram": {str(k): v for k, v in sorted(influences.items())},
        "dead_bones": sorted(n for n in names if n not in driven),
        "per_bone": {n: v for n, v in per_bone.items() if v["owns"]},
        "worst": [
            {"ratio": round(r, 2), "vertex": i, "dominated_by": dom,
             "nearest_bone": near, "distance": d, "y": y}
            for r, i, dom, near, d, y in worst[:15]
        ],
    }


def dominance_verdict(report: dict, *, max_ratio: float = 3.0,
                      max_misbound_fraction: float = 0.05,
                      max_rigid_fraction: float = 0.50,
                      flag_dead_bones: bool = False) -> dict:
    """Pass/fail over a `dominance` report.

    THE DEFAULTS ARE SET FROM TWO MEASURED RIGS, one known-good and one known-
    bad, in the same project. The bad one is the cat described at the top of
    this module, whose animations a player described as tearing; the good one
    is that project's owner character, animating correctly at the same time.

                        median   p95    max    rigid
        owner  (good)     1.00   1.06   1.56      9%
        cat    (bad)      1.00   2.32   3.76     57%

    Note the median is 1.00 for BOTH. Most vertices in a broken bind are still
    painted correctly, so any average hides this defect completely — it lives
    in the tail, which is why the verdict reads the maximum and the rigid
    share rather than a mean.

    `max_ratio` (3.0) is deliberately a GROSS-ERROR line, and sits above the
    good rig's 1.56 with room to spare. Nearest-bone is a heuristic — a
    shoulder really does drive rib geometry, and bones that pass close
    together really do trade vertices — so a 1.5x tolerance would flag correct
    anatomy on every character and be switched off within a week, which is
    worse than never having shipped it.

    `max_rigid_fraction` (0.50) sits between the two measurements above. A
    fully rigid bind is a real and different failure — a mesh bound by
    nearest-bone with no smoothing — and it produces hard seams at every
    joint. Hard-surface characters that are legitimately rigid should raise
    this rather than have the check imposing a style on them.

    `flag_dead_bones` is OFF by default and that is deliberate. Every rig
    carries bones that drive no vertex on purpose — a root, an IK target, a
    control — and the good rig above fails on exactly that if this is on. The
    dead bones are always listed in the report, where they are information for
    a human rather than a gate failure. Turn it on for a pipeline whose rigs
    are all pure deform skeletons.

    Refuses rather than passes when nothing could be measured.
    """
    if not report.get("measured"):
        return {"passed": False, "checked": 0,
                "reason": report.get("reason", "not measured"), "issues": []}

    checked = report.get("checked", 0)
    issues: list[dict] = []
    misbound = 0
    for name, stats in (report.get("per_bone") or {}).items():
        if stats.get("worst_ratio", 0.0) > max_ratio:
            issues.append({
                "kind": "reaches_too_far",
                "bone": name,
                "worst_ratio": stats["worst_ratio"],
                "owns": stats["owns"],
                "detail": (f"{name} dominates a vertex "
                           f"{stats['worst_ratio']}x farther from it than the "
                           f"nearest bone to that vertex"),
            })
    for w in report.get("worst") or []:
        if w["ratio"] > max_ratio:
            misbound += 1

    frac = misbound / max(len(report.get("worst") or []), 1)
    rigid = report.get("rigid_fraction", 0.0)
    if rigid > max_rigid_fraction:
        issues.append({
            "kind": "no_falloff",
            "detail": (f"{rigid:.0%} of vertices have a single bone influence "
                       f"— the bind has no falloff across joints"),
        })
    if flag_dead_bones and report.get("dead_bones"):
        issues.append({
            "kind": "dead_bones",
            "bones": report["dead_bones"],
            "detail": ("these bones dominate no vertex: "
                       + ", ".join(report["dead_bones"])),
        })
    return {
        "passed": not issues,
        "checked": checked,
        "max_ratio": report.get("max_ratio"),
        "median_ratio": report.get("median_ratio"),
        "issues": issues,
    }
