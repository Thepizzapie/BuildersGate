"""Surface quality, read straight off an exported glTF/GLB.

THE QUESTION NOTHING ELSE IN THIS PRODUCT ASKS: does this surface look like
the surface it is meant to be, or does it look like a decimated one?

FOUND ON A SHIPPED CHARACTER. A player looked at the owner's cardigan and said
"notice the roughness in the polygons". The sleeve reads as a crinkled paper
bag: visible triangle facets, shallow dents scattered across a shape that
should be smooth. Every geometry gate in the product passed it, because every
one of them asks a question this defect does not answer to —

  * triangle COUNT was inside budget.
  * DIMENSIONS measured correct against the scale contract.
  * `has_geometry`, `loads_in_engine`, `has_collider` were all true.
  * skin weights were plausible, bones were in the right places.
  * the SILHOUETTE was fine, which is what a turnaround render shows.

Nothing measured the surface BETWEEN the silhouette and the triangle count,
and that is where the entire defect lives.

FOUR DISTINCT DEFECTS THAT ALL LOOK LIKE "IT LOOKS ROUGH", and separating them
is most of this module's value, because the fix for each is different and three
of the four fixes make the others worse:

  DENTING      the geometry itself is lumpy. Decimation moved vertices off the
               original surface, so a smooth shape now has shallow pits in it.
               Fixed by decimating differently (staged halving, not one big
               collapse), never by touching normals.
  STALE NORMALS the geometry is fine and the shading is not: the stored vertex
               normals no longer agree with the faces around them, which
               happens the moment anything moves vertices on a mesh carrying
               baked custom split normals. Fixed by re-baking normals for the
               moved region ONLY.
  SLIVERS      long thin triangles. They shade badly no matter what the normals
               say, and they are what a quadric collapse leaves when it runs
               out of good edges. Fixed upstream, by welding before collapsing.
  FLAT SHADING the mesh has no smoothing at all — every stored normal is its
               own face's normal. Looks identical to "faceted" in a screenshot
               and is a one-line fix rather than a re-generation.

WHY THE SIGN OF THE DIHEDRAL IS THE MEASUREMENT THAT MATTERS. A box has large
dihedral angles and looks perfect; a dented sphere has smaller ones and looks
broken. So the magnitude alone cannot separate them — a threshold tuned to
catch the dent condemns every hard edge in the furniture kit. What differs is
CONSISTENCY: on any clean surface, smooth or boxy, the edges meeting at a
vertex bend the same way, because the surface is locally convex or locally
concave. Denting alternates — a pit is concave edges ringed by convex ones.
Counting sign flips around each vertex therefore finds the dent and ignores
the box, with no per-asset tuning, which is the only reason this can be run
without being switched off in a week.

WHY THE NORMAL CHECK IS SOUND ON SPLIT NORMALS, since that looks like a hole.
A hard edge in glTF is expressed by DUPLICATING the vertex — each copy carries
the normal of its own side. So a correctly authored mesh, split normals and
all, still has every vertex's stored normal close to the average of the faces
that actually use that index. A large disagreement is not a style choice.

THIS IS A PLAUSIBILITY CHECK, NOT A CORRECTNESS PROOF, and it is wrong in
knowable places: genuinely crumpled subjects (a duvet, a rug, foliage) will
read as dented and should be, so the verdict names the mesh and a human
decides. Deliberately faceted low-poly art will read as flat-shaded, correctly,
and is meant to be dismissed.

No Blender, no Godot, no third-party glTF library — the same reasoning as
animcurves.py and skinweights.py, whose reader this borrows.
"""
from __future__ import annotations

import math
from pathlib import Path

from .animcurves import _accessor_values, _read_glb

__all__ = ["faceting", "faceting_verdict"]


# --- small vector helpers, kept local so this module has no dependencies -----

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length(a):
    return math.sqrt(_dot(a, a))


def _angle_between(a, b) -> float:
    """Degrees between two vectors. Zero-length reads as 0, not as an error —
    a degenerate triangle has no normal to disagree with and is counted by the
    degenerate metric instead of poisoning this one."""
    la, lb = _length(a), _length(b)
    if la <= 1e-12 or lb <= 1e-12:
        return 0.0
    c = _dot(a, b) / (la * lb)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _unmeasured(reason: str, **extra) -> dict:
    out = {"measured": False, "reason": reason}
    out.update(extra)
    return out


# --- the reader -------------------------------------------------------------

def _primitives(gltf: dict, bin_data: bytes):
    """Every triangle primitive in the file, as (mesh_name, positions,
    normals_or_None, indices)."""
    out = []
    for mesh in gltf.get("meshes", []):
        name = mesh.get("name", "")
        for prim in mesh.get("primitives", []):
            # mode 4 is TRIANGLES; the default when absent is also 4. Anything
            # else (strips, fans, lines, points) is not what this measures and
            # is skipped rather than misread.
            if prim.get("mode", 4) != 4:
                continue
            attrs = prim.get("attributes", {})
            if "POSITION" not in attrs:
                continue
            pos = _accessor_values(gltf, bin_data, attrs["POSITION"])
            nrm = None
            if "NORMAL" in attrs:
                nrm = _accessor_values(gltf, bin_data, attrs["NORMAL"])
            if "indices" in prim:
                idx = [int(v) for v in _accessor_values(gltf, bin_data, prim["indices"])]
            else:
                idx = list(range(len(pos)))
            out.append((name, pos, nrm, idx))
    return out


# --- the measurement --------------------------------------------------------

def faceting(path: str | Path, *, crease_deg: float = 30.0,
             sliver_deg: float = 10.0) -> dict:
    """Measure surface quality on every triangle primitive in a GLB.

    `crease_deg` is what counts as a hard edge for reporting purposes only —
    the denting metric does NOT use it, deliberately, because a threshold on
    dihedral magnitude cannot separate a box from a dent (see the module note).
    `sliver_deg` is the minimum interior angle below which a triangle is called
    a sliver.
    """
    try:
        gltf, bin_data = _read_glb(path)
    except (OSError, ValueError) as exc:
        return _unmeasured(f"could not read the file as a GLB: {exc}")

    prims = _primitives(gltf, bin_data)
    if not prims:
        return _unmeasured("no triangle primitive with POSITION in this file")

    meshes = []
    for name, pos, nrm, idx in prims:
        meshes.append(_measure_one(name, pos, nrm, idx, crease_deg, sliver_deg))

    measured = [m for m in meshes if m["triangles"] > 0]
    if not measured:
        return _unmeasured("every primitive was empty or degenerate")

    # The file-level figures are the WORST mesh, not an average. An average
    # hides one ruined sleeve inside a body that is fine, which is exactly the
    # case this was built for.
    worst_dent = max(measured, key=lambda m: m["dent_fraction"])
    worst_stale = max(measured, key=lambda m: m["stale_fraction"])
    worst_sliver = max(measured, key=lambda m: m["sliver_fraction"])
    worst_inv = max(measured, key=lambda m: m["inverted_fraction"])
    return {
        "measured": True,
        "path": str(path),
        "meshes": meshes,
        "mesh_count": len(meshes),
        "dent_fraction": worst_dent["dent_fraction"],
        "dent_worst_mesh": worst_dent["name"],
        "stale_fraction": worst_stale["stale_fraction"],
        "stale_worst_mesh": worst_stale["name"],
        "sliver_fraction": worst_sliver["sliver_fraction"],
        "sliver_worst_mesh": worst_sliver["name"],
        "inverted_fraction": worst_inv["inverted_fraction"],
        "inverted_worst_mesh": worst_inv["name"],
        "flat_shaded": all(m["flat_shaded"] for m in measured),
        "has_normals": all(m["has_normals"] for m in measured),
    }


def _weld_map(pos, decimals: int = 5) -> list[int]:
    """Index -> representative index for vertices at the same POSITION.

    THIS STEP IS NOT OPTIONAL AND IT IS WHY THE FIRST VERSION MEASURED NOTHING.
    An exported glTF duplicates a vertex wherever a UV seam or a hard edge
    needs a second normal, so the two triangles either side of a cube's corner
    do not share an index — they share a location. Without welding, every hard
    edge in the file is a BOUNDARY with no second face, the dihedral is never
    computed, and a box reports `interior_edges: 6` (its face diagonals) and a
    maximum fold of zero degrees. The bible states the same trap from the other
    direction: in an unwelded glTF every UV seam edge has one linked face,
    which is how a naive boundary fill sews a mesh shut.

    Welding is for TOPOLOGY only. The stored normals are still read per
    original index, because that is exactly what a split normal is.
    """
    seen: dict = {}
    out = []
    for i, p in enumerate(pos):
        key = (round(p[0], decimals), round(p[1], decimals), round(p[2], decimals))
        out.append(seen.setdefault(key, i))
    return out


def _measure_one(name, pos, nrm, idx, crease_deg, sliver_deg) -> dict:
    tri_normals: list = []
    tri_areas: list[float] = []
    degenerate = 0
    slivers = 0
    min_angles: list[float] = []

    tris = [(idx[i], idx[i + 1], idx[i + 2]) for i in range(0, len(idx) - 2, 3)]
    weld = _weld_map(pos)
    for a, b, c in tris:
        pa, pb, pc = pos[a], pos[b], pos[c]
        n = _cross(_sub(pb, pa), _sub(pc, pa))
        area = _length(n) * 0.5
        tri_normals.append(n)
        tri_areas.append(area)
        if area <= 1e-14:
            degenerate += 1
            min_angles.append(0.0)
            continue
        ang = min(_angle_between(_sub(pb, pa), _sub(pc, pa)),
                  _angle_between(_sub(pa, pb), _sub(pc, pb)),
                  _angle_between(_sub(pa, pc), _sub(pb, pc)))
        min_angles.append(ang)

    n_tris = len(tris)
    if n_tris == 0:
        return {"name": name, "triangles": 0, "dent_fraction": 0.0,
                "stale_fraction": 0.0, "sliver_fraction": 0.0,
                "flat_shaded": False, "has_normals": nrm is not None}

    # --- edges, and the SIGNED dihedral across each interior one ------------
    # Signed by whether the two faces fold toward each other (concave) or away
    # (convex), decided by which side the opposite vertex falls on. The sign is
    # the whole measurement; the magnitude is only reported.
    edges: dict = {}
    for t, (a, b, c) in enumerate(tris):
        wa, wb, wc = weld[a], weld[b], weld[c]
        for u, v in ((wa, wb), (wb, wc), (wc, wa)):
            edges.setdefault((min(u, v), max(u, v)), []).append(t)

    dihedrals: list[float] = []
    over_crease = 0
    # per-vertex list of signs, so a flip can be counted where it happens
    vertex_signs: dict = {}
    for (u, v), faces in edges.items():
        if len(faces) != 2:
            continue          # boundary or non-manifold: no dihedral exists
        t0, t1 = faces
        if tri_areas[t0] <= 1e-14 or tri_areas[t1] <= 1e-14:
            continue
        ang = _angle_between(tri_normals[t0], tri_normals[t1])
        dihedrals.append(ang)
        if ang > crease_deg:
            over_crease += 1
        # convex or concave: does face 1's normal point away from face 0's
        # plane, measured along the edge-to-opposite-vertex direction.
        opp = [x for x in tris[t1] if weld[x] not in (u, v)]
        if not opp:
            continue
        sign = 1 if _dot(tri_normals[t0], _sub(pos[opp[0]], pos[u])) < 0 else -1
        if ang < 1e-6:
            sign = 0          # flat: belongs to neither camp
        vertex_signs.setdefault(u, []).append(sign)
        vertex_signs.setdefault(v, []).append(sign)

    # SLIVERS, COUNTED ONLY WHERE THEY CAN ACTUALLY HURT.
    #
    # A thin triangle on a FLAT surface is not a defect, it is a rectangle. The
    # furniture kit is boxes, and a couch frame is 1.58m x 0.16m, so splitting
    # that face into two triangles gives a minimum angle of about 5.8 degrees
    # by construction. The first version of this counted those and reported the
    # couch at 73% slivers — a gate that condemns every long box face is a gate
    # that gets switched off in a week, and then it catches nothing at all.
    #
    # What actually shades badly is a thin triangle spanning CURVATURE, where
    # the interpolated normal has to swing across a sliver's long axis. So a
    # triangle counts only if it is both thin AND sitting in a neighbourhood
    # that is not planar. Measured on the real assets this took the furniture
    # kit from 73% to 0% while leaving the decimated cat at 14%.
    # The test is whether the triangle's OWN stored normals vary across it. That
    # is what "shades badly" means mechanically: the renderer interpolates the
    # vertex normals over the face, and doing that across a sliver's short axis
    # is where the banding comes from. If all three normals agree, the triangle
    # shades as a flat patch no matter how thin it is.
    #
    # An earlier attempt asked instead whether the triangle touched a fold, and
    # it was still wrong for the same reason as the first: EVERY triangle on a
    # box face touches a 90-degree corner, so the couch still scored 73%. The
    # fold is next to the triangle; the shading problem is across it.
    #
    # With no NORMAL attribute there is nothing to interpolate and no claim to
    # make, so slivers are not counted at all — `has_normals` already reports
    # that as its own, larger, issue.
    for t, (a, b, c) in enumerate(tris):
        if tri_areas[t] <= 1e-14:
            continue                      # already counted as degenerate
        if min_angles[t] >= sliver_deg or nrm is None:
            continue
        if max(a, b, c) >= len(nrm):
            continue
        spread = max(_angle_between(nrm[a], nrm[b]),
                     _angle_between(nrm[b], nrm[c]),
                     _angle_between(nrm[a], nrm[c]))
        if spread > 5.0:
            slivers += 1

    # DENTING: a vertex whose surrounding edges disagree about which way the
    # surface bends. A box corner is unanimous (all convex). A pit is not.
    dented_vertices = 0
    considered = 0
    for _v, signs in vertex_signs.items():
        live = [s for s in signs if s != 0]
        if len(live) < 3:
            continue          # too few edges to have an opinion
        considered += 1
        pos_n = sum(1 for s in live if s > 0)
        neg_n = len(live) - pos_n
        # minority share: 0.0 unanimous, 0.5 maximally confused
        if min(pos_n, neg_n) / len(live) >= 0.34:
            dented_vertices += 1
    dent_fraction = (dented_vertices / considered) if considered else 0.0

    # STALE NORMALS: stored normal against the area-weighted mean of the faces
    # that actually use this index.
    stale_fraction = 0.0
    inverted_fraction = 0.0
    normal_dev_p50 = 0.0
    normal_dev_p90 = 0.0
    flat_shaded = False
    if nrm is not None:
        accum: dict = {}
        for t, (a, b, c) in enumerate(tris):
            if tri_areas[t] <= 1e-14:
                continue
            w = tri_areas[t]
            fn = tri_normals[t]
            for vtx in (a, b, c):
                cur = accum.get(vtx, (0.0, 0.0, 0.0))
                accum[vtx] = (cur[0] + fn[0] * w,
                              cur[1] + fn[1] * w,
                              cur[2] + fn[2] * w)
        devs = []
        flat_hits = 0
        for vtx, mean_n in accum.items():
            if vtx >= len(nrm):
                continue
            devs.append(_angle_between(nrm[vtx], mean_n))
        if devs:
            normal_dev_p50 = _percentile(devs, 0.5)
            normal_dev_p90 = _percentile(devs, 0.9)
            # INVERTED WINDING IS A DIFFERENT DEFECT FROM STALE NORMALS AND THE
            # FIXES ARE OPPOSITE. A face wound the wrong way puts its geometric
            # normal ~180 degrees from the stored one; the fix is to flip the
            # winding and KEEP the normals. Stale normals sit in the middle
            # ground; the fix there is to re-bake and keep the winding. Scoring
            # a flipped mesh as "stale" would send someone to recalculate
            # normals on a generated mesh, which this project has twice proved
            # shreds it into black facets. Measured on the shipped models: over
            # half the faces of a raw Krea generation arrive flipped.
            inverted = sum(1 for d in devs if d > 150.0)
            stale_fraction = sum(1 for d in devs if 25.0 < d <= 150.0) / len(devs)
            inverted_fraction = inverted / len(devs)
        # FLAT SHADING is a different claim from stale: every stored normal
        # equals its own face's normal, so the mesh has no smoothing at all.
        # Only meaningful on a mesh that HAS interior curvature to smooth.
        for t, (a, b, c) in enumerate(tris):
            if tri_areas[t] <= 1e-14:
                continue
            fn = tri_normals[t]
            if all(v < len(nrm) and _angle_between(nrm[v], fn) < 1.0
                   for v in (a, b, c)):
                flat_hits += 1
        flat_shaded = (flat_hits / n_tris > 0.95) and bool(dihedrals) \
            and _percentile(dihedrals, 0.5) > 1.0

    return {
        "name": name,
        "triangles": n_tris,
        "vertices": len(pos),
        "has_normals": nrm is not None,
        "interior_edges": len(dihedrals),
        "dihedral_p50": round(_percentile(dihedrals, 0.5), 4),
        "dihedral_p90": round(_percentile(dihedrals, 0.9), 4),
        "dihedral_max": round(max(dihedrals), 4) if dihedrals else 0.0,
        "over_crease_fraction": round(over_crease / len(dihedrals), 4) if dihedrals else 0.0,
        "dent_fraction": round(dent_fraction, 4),
        "dented_vertices": dented_vertices,
        "vertices_considered": considered,
        "stale_fraction": round(stale_fraction, 4),
        "inverted_fraction": round(inverted_fraction, 4),
        "normal_deviation_p50": round(normal_dev_p50, 4),
        "normal_deviation_p90": round(normal_dev_p90, 4),
        "flat_shaded": flat_shaded,
        "sliver_fraction": round(slivers / n_tris, 4),
        "min_angle_p50": round(_percentile(min_angles, 0.5), 4),
        "min_angle_p10": round(_percentile(min_angles, 0.1), 4),
        "degenerate_triangles": degenerate,
    }


# --- the verdict ------------------------------------------------------------

def faceting_verdict(report: dict, *, max_dent_fraction: float = 0.18,
                     max_stale_fraction: float = 0.10,
                     max_sliver_fraction: float = 0.08,
                     max_inverted_fraction: float = 0.05,
                     flag_flat_shaded: bool = False) -> dict:
    """Turn a measurement into a pass/fail with named, actionable issues.

    An UNMEASURED report never passes. That is the same rule skinweights.py
    follows and it exists because this project has twice been burned by a gate
    reporting nothing and being read as reporting nothing wrong.

    The defaults are GROSS-ERROR lines, not fidelity ones. A subject that is
    genuinely crumpled — a duvet, a rug, foliage — will exceed the dent line
    and should; the verdict names the mesh so a human can dismiss it. That is
    the same trade skinweights.py's max_ratio makes, for the same reason: a
    gate that fires on style gets switched off in a week, which is worse than
    never shipping it.

    `flag_flat_shaded` is OFF by default because deliberately faceted low-poly
    art is a legitimate style, and on this product it is a common one.
    """
    if not report.get("measured"):
        return {"passed": False,
                "reason": report.get("reason", "nothing was measured"),
                "issues": []}

    issues = []
    if report["dent_fraction"] > max_dent_fraction:
        issues.append({
            "kind": "denting",
            "mesh": report["dent_worst_mesh"],
            "value": report["dent_fraction"],
            "limit": max_dent_fraction,
            "note": "the surface bends both ways around the same vertices — "
                    "decimation moved vertices off the original surface. Fix "
                    "the collapse (staged halving, weld first), NOT the normals."})
    if report["stale_fraction"] > max_stale_fraction:
        issues.append({
            "kind": "stale_normals",
            "mesh": report["stale_worst_mesh"],
            "value": report["stale_fraction"],
            "limit": max_stale_fraction,
            "note": "stored normals disagree with the faces that use them — "
                    "something moved vertices on a mesh carrying baked normals. "
                    "Re-bake the moved region only; a global recalculate "
                    "destroys generated meshes."})
    if report.get("inverted_fraction", 0.0) > max_inverted_fraction:
        issues.append({
            "kind": "inverted_winding",
            "mesh": report["inverted_worst_mesh"],
            "value": report["inverted_fraction"],
            "limit": max_inverted_fraction,
            "note": "faces are wound the wrong way — the stored normals point "
                    "roughly opposite the geometry. FLIP THE WINDING and keep "
                    "the normals. Do NOT recalculate normals: on a generated "
                    "mesh the baked custom split normals are the only reason it "
                    "shades correctly, and recalculating shreds it."})
    if report["sliver_fraction"] > max_sliver_fraction:
        issues.append({
            "kind": "slivers",
            "mesh": report["sliver_worst_mesh"],
            "value": report["sliver_fraction"],
            "limit": max_sliver_fraction,
            "note": "long thin triangles shade badly whatever the normals say. "
                    "Weld and remove degenerates BEFORE collapsing, not after."})
    if not report.get("has_normals", True):
        issues.append({
            "kind": "no_normals",
            "mesh": "",
            "value": 0.0,
            "limit": 0.0,
            "note": "no NORMAL attribute at all — the renderer will face-shade "
                    "this, and no amount of material work will smooth it."})
    if flag_flat_shaded and report.get("flat_shaded"):
        issues.append({
            "kind": "flat_shaded",
            "mesh": "",
            "value": 0.0,
            "limit": 0.0,
            "note": "every stored normal is its own face's normal, on a mesh "
                    "with real curvature. If that is not the intended style it "
                    "is a one-line fix, not a re-generation."})

    return {"passed": not issues, "issues": issues,
            "dent_fraction": report["dent_fraction"],
            "stale_fraction": report["stale_fraction"],
            "sliver_fraction": report["sliver_fraction"],
            "inverted_fraction": report.get("inverted_fraction", 0.0)}
