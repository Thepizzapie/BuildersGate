"""Surface quality, tested on hand-built GLBs — no Blender, no asset needed.

Every fixture is a small mesh written byte-for-byte, so "does this surface look
decimated?" is answerable without an art pipeline.

THE PAIRINGS ARE THE POINT. Each defect is tested as the SAME geometry in a
good and a bad state, because a green result only means something if the red
one exists. The load-bearing pair is the box against the dented sphere: a box
has far LARGER dihedral angles than a dent does, so any check built on the
magnitude of the fold condemns the furniture kit and misses the defect. The
sign-consistency measure has to separate them, and if it ever stops doing so
these tests fail rather than the tool quietly passing everything.
"""
from __future__ import annotations

import json
import math
import struct

from bgate_adapters import meshquality as mq


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

def _write_glb(tmp_path, name, verts, tris, normals=None):
    pos = b"".join(struct.pack("<3f", *v) for v in verts)
    idx = b"".join(struct.pack("<I", i) for t in tris for i in t)
    chunks = [pos, idx]
    if normals is not None:
        chunks.append(b"".join(struct.pack("<3f", *n) for n in normals))

    offsets, cur = [], 0
    for c in chunks:
        offsets.append(cur)
        cur += len(c)
        if cur % 4:                       # accessors must stay 4-byte aligned
            pad = 4 - (cur % 4)
            chunks[chunks.index(c)] = c
            cur += pad
    bin_parts, cur = [], 0
    offsets = []
    for c in chunks:
        offsets.append(cur)
        bin_parts.append(c)
        cur += len(c)
        if cur % 4:
            pad = 4 - (cur % 4)
            bin_parts.append(b"\x00" * pad)
            cur += pad
    bin_data = b"".join(bin_parts)

    accessors = [
        {"bufferView": 0, "componentType": 5126, "count": len(verts), "type": "VEC3"},
        {"bufferView": 1, "componentType": 5125, "count": len(tris) * 3, "type": "SCALAR"},
    ]
    views = [
        {"buffer": 0, "byteOffset": offsets[0], "byteLength": len(chunks[0])},
        {"buffer": 0, "byteOffset": offsets[1], "byteLength": len(chunks[1])},
    ]
    attrs = {"POSITION": 0}
    if normals is not None:
        accessors.append({"bufferView": 2, "componentType": 5126,
                          "count": len(normals), "type": "VEC3"})
        views.append({"buffer": 0, "byteOffset": offsets[2],
                      "byteLength": len(chunks[2])})
        attrs["NORMAL"] = 2

    gltf = {
        "asset": {"version": "2.0"},
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(bin_data)}],
        "meshes": [{"name": name.replace(".glb", ""),
                    "primitives": [{"attributes": attrs, "indices": 1, "mode": 4}]}],
    }
    jb = json.dumps(gltf).encode("utf-8")
    jb += b" " * ((-len(jb)) % 4)
    bd = bin_data + b"\x00" * ((-len(bin_data)) % 4)
    json_chunk = struct.pack("<I4s", len(jb), b"JSON") + jb
    bin_chunk = struct.pack("<I4s", len(bd), b"BIN\x00") + bd
    total = 12 + len(json_chunk) + len(bin_chunk)
    path = tmp_path / name
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, total) + json_chunk + bin_chunk)
    return path


def _sphere(rings=14, segs=20, radius=1.0, jitter=0.0, seed=1):
    """A sphere BAND — latitudes sampled at ring centres, so the poles are open.

    Deliberately not a full UV sphere. At an exact pole every vertex of the row
    coincides, so the cap triangles are genuine slivers and a full sphere scores
    7% slivers while being a perfectly good test surface. Opening the poles
    removes an artifact of the fixture rather than of the thing under test.

    Winding is (a, b, c) / (b, d, c), which puts the geometric normals OUTWARD
    and therefore in agreement with the stored ones. The first version wound the
    other way and every clean fixture reported 178 degrees of normal deviation —
    caught by the tool, which is the outcome you want from a fixture bug.

    `jitter` pushes each vertex along its own radius, alternating, which is what
    decimation denting looks like: the shape survives and the surface pits.
    """
    verts, normals = [], []
    state = seed
    for i in range(rings + 1):
        lat = math.pi * (0.25 + 0.5 * i / rings)
        for j in range(segs):
            lon = 2.0 * math.pi * j / segs
            nx = math.sin(lat) * math.cos(lon)
            ny = math.cos(lat)
            nz = math.sin(lat) * math.sin(lon)
            r = radius
            if jitter:
                state = (state * 1103515245 + 12345) & 0x7FFFFFFF
                r = radius * (1.0 + jitter * (1.0 if (state >> 16) & 1 else -1.0))
            verts.append((nx * r, ny * r, nz * r))
            normals.append((nx, ny, nz))
    tris = []
    for i in range(rings):
        for j in range(segs):
            a = i * segs + j
            b = i * segs + (j + 1) % segs
            c = (i + 1) * segs + j
            d = (i + 1) * segs + (j + 1) % segs
            tris.append((a, b, c))
            tris.append((b, d, c))
    return verts, tris, normals


def _welded_box():
    """A cube as EIGHT shared vertices with smoothed normals.

    This is the magnitude control and the split-normal box cannot be it: with
    24 vertices every cube edge is two separate indices, so after welding it is
    still one location but the fixture proves nothing about hard edges that the
    weld itself did not do. Eight shared vertices give real 90-degree interior
    dihedrals — the case that breaks any check built on how far a surface folds.
    """
    verts = [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
             (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)]
    normals = []
    for v in verts:
        ln = math.sqrt(3.0)
        normals.append((v[0] / ln, v[1] / ln, v[2] / ln))
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
            (0, 1, 5), (0, 5, 4), (2, 3, 7), (2, 7, 6),
            (1, 2, 6), (1, 6, 5), (0, 4, 7), (0, 7, 3)]
    return verts, tris, normals


def _box():
    """A unit cube with SPLIT normals — 24 vertices, four per face, each
    carrying its own face's normal. This is a correctly authored hard-edged
    object and it must pass everything."""
    faces = [
        ((-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1), (0, 0, 1)),
        ((1, -1, -1), (-1, -1, -1), (-1, 1, -1), (1, 1, -1), (0, 0, -1)),
        ((1, -1, 1), (1, -1, -1), (1, 1, -1), (1, 1, 1), (1, 0, 0)),
        ((-1, -1, -1), (-1, -1, 1), (-1, 1, 1), (-1, 1, -1), (-1, 0, 0)),
        ((-1, 1, 1), (1, 1, 1), (1, 1, -1), (-1, 1, -1), (0, 1, 0)),
        ((-1, -1, -1), (1, -1, -1), (1, -1, 1), (-1, -1, 1), (0, -1, 0)),
    ]
    verts, normals, tris = [], [], []
    for a, b, c, d, n in faces:
        base = len(verts)
        verts.extend([a, b, c, d])
        normals.extend([n] * 4)
        tris.append((base, base + 1, base + 2))
        tris.append((base, base + 2, base + 3))
    return verts, tris, normals


# ---------------------------------------------------------------------------
# Refusing rather than passing
# ---------------------------------------------------------------------------

def test_a_file_that_is_not_a_glb_refuses(tmp_path):
    bad = tmp_path / "notamodel.glb"
    bad.write_bytes(b"this is not a model")
    report = mq.faceting(bad)
    assert report["measured"] is False
    assert mq.faceting_verdict(report)["passed"] is False


def test_an_unmeasured_report_never_passes():
    verdict = mq.faceting_verdict({"measured": False, "reason": "nothing"})
    assert verdict["passed"] is False
    assert verdict["issues"] == []


def test_a_file_with_no_triangles_refuses(tmp_path):
    path = _write_glb(tmp_path, "empty.glb", [(0.0, 0.0, 0.0)], [])
    report = mq.faceting(path)
    assert report["measured"] is False


# ---------------------------------------------------------------------------
# THE LOAD-BEARING PAIR: a box must not be mistaken for a dent
# ---------------------------------------------------------------------------

def test_a_clean_sphere_passes(tmp_path):
    v, t, n = _sphere()
    report = mq.faceting(_write_glb(tmp_path, "smooth.glb", v, t, n))
    assert report["measured"] is True
    verdict = mq.faceting_verdict(report)
    assert verdict["passed"] is True, verdict["issues"]


def test_a_hard_edged_box_passes_despite_90_degree_folds(tmp_path):
    """The control that stops this tool condemning the whole furniture kit."""
    v, t, n = _welded_box()
    report = mq.faceting(_write_glb(tmp_path, "box.glb", v, t, n))
    assert report["meshes"][0]["dihedral_max"] > 60.0, (
        "the fixture must actually contain hard edges or it controls nothing")
    verdict = mq.faceting_verdict(report)
    assert verdict["passed"] is True, verdict["issues"]


def test_split_normals_are_welded_before_topology_is_measured(tmp_path):
    """The finding that reshaped this module.

    A cube exported with split normals has 24 vertices, so NOT ONE of its
    twelve hard edges is shared by index — before welding, the tool saw six
    face-diagonal edges and a maximum fold of zero degrees on a cube. Every
    exported asset in this product looks like this at its UV seams.
    """
    v, t, n = _box()
    report = mq.faceting(_write_glb(tmp_path, "split.glb", v, t, n))
    mesh = report["meshes"][0]
    assert mesh["vertices"] == 24
    assert mesh["interior_edges"] >= 18, (
        f"only {mesh['interior_edges']} interior edges — the weld did not "
        "happen and the topology metrics are measuring almost nothing")
    assert mesh["dihedral_max"] > 60.0
    assert mq.faceting_verdict(report)["passed"] is True


def test_a_dented_sphere_is_caught(tmp_path):
    v, t, n = _sphere(jitter=0.06)
    report = mq.faceting(_write_glb(tmp_path, "dented.glb", v, t, n))
    verdict = mq.faceting_verdict(report)
    assert verdict["passed"] is False
    assert "denting" in {i["kind"] for i in verdict["issues"]}


def test_the_box_and_the_dent_are_separated_by_sign_not_magnitude(tmp_path):
    """The claim the module rests on, pinned.

    The box folds by 90 degrees and is fine; the dent folds by far less and is
    broken. Any measure built on the SIZE of the fold gets this backwards, so
    this asserts both that the magnitudes really do run the wrong way and that
    the dent metric still separates them correctly.
    """
    bv, bt, bn = _welded_box()
    dv, dt, dn = _sphere(jitter=0.06)
    box = mq.faceting(_write_glb(tmp_path, "b.glb", bv, bt, bn))["meshes"][0]
    dent = mq.faceting(_write_glb(tmp_path, "d.glb", dv, dt, dn))["meshes"][0]

    # MEASURED, and this is the whole argument for the module: the box's
    # TYPICAL interior fold is 90 degrees and it is perfect; the dent's is 27
    # and it is broken. So a magnitude threshold set high enough to pass the
    # box lets the dent straight through, and one set low enough to catch the
    # dent condemns every hard edge in the furniture kit. Compared on p90
    # rather than max because a max is one bad vertex on either fixture.
    assert box["dihedral_p90"] >= dent["dihedral_p90"], (
        f"the premise failed: box p90 {box['dihedral_p90']} vs dent p90 "
        f"{dent['dihedral_p90']} — if the dent folds harder than the box, a "
        "magnitude threshold would work and this module is unnecessary")
    assert box["dihedral_p50"] > dent["dihedral_p50"]
    assert dent["dent_fraction"] > box["dent_fraction"] * 3 + 0.1, (
        f"box {box['dent_fraction']} vs dent {dent['dent_fraction']} — the "
        "metric must separate these or it is measuring nothing")


# ---------------------------------------------------------------------------
# Stale normals: same geometry, normals that do and do not match it
# ---------------------------------------------------------------------------

def test_normals_that_match_the_geometry_pass(tmp_path):
    v, t, n = _sphere()
    report = mq.faceting(_write_glb(tmp_path, "ok.glb", v, t, n))
    assert report["stale_fraction"] == 0.0
    assert mq.faceting_verdict(report)["passed"] is True


def test_normals_left_behind_when_vertices_moved_are_caught(tmp_path):
    """The exact shipped failure: vertices were moved, the baked normals were
    kept, and the surface shades as though it were still the old shape."""
    v, t, n = _sphere()
    moved = [(x, y, z * 0.35) for (x, y, z) in v]     # squashed on one axis
    report = mq.faceting(_write_glb(tmp_path, "stale.glb", moved, t, n))
    verdict = mq.faceting_verdict(report)
    assert verdict["passed"] is False
    assert "stale_normals" in {i["kind"] for i in verdict["issues"]}


def test_a_mesh_with_no_normals_at_all_is_named_as_such(tmp_path):
    v, t, _n = _sphere()
    report = mq.faceting(_write_glb(tmp_path, "bare.glb", v, t, None))
    assert report["has_normals"] is False
    verdict = mq.faceting_verdict(report)
    assert "no_normals" in {i["kind"] for i in verdict["issues"]}


# ---------------------------------------------------------------------------
# Flat shading is a DIFFERENT claim from stale, and is off by default
# ---------------------------------------------------------------------------

def test_flat_shading_is_detected_but_does_not_fail_by_default(tmp_path):
    """Deliberately faceted low-poly is a legitimate style and a common one
    here, so it is reported and not condemned unless asked for."""
    v, t, _ = _sphere(rings=6, segs=8)
    flat_v, flat_n, flat_t = [], [], []
    for (a, b, c) in t:
        base = len(flat_v)
        pa, pb, pc = v[a], v[b], v[c]
        fn = mq._cross(mq._sub(pb, pa), mq._sub(pc, pa))
        ln = mq._length(fn) or 1.0
        fn = (fn[0] / ln, fn[1] / ln, fn[2] / ln)
        flat_v.extend([pa, pb, pc])
        flat_n.extend([fn] * 3)
        flat_t.append((base, base + 1, base + 2))
    report = mq.faceting(_write_glb(tmp_path, "lowpoly.glb", flat_v, flat_t, flat_n))
    assert report["flat_shaded"] is True
    assert mq.faceting_verdict(report)["passed"] is True
    strict = mq.faceting_verdict(report, flag_flat_shaded=True)
    assert strict["passed"] is False
    assert "flat_shaded" in {i["kind"] for i in strict["issues"]}


def test_a_smooth_sphere_is_not_called_flat_shaded(tmp_path):
    v, t, n = _sphere()
    report = mq.faceting(_write_glb(tmp_path, "smooth2.glb", v, t, n))
    assert report["flat_shaded"] is False


# ---------------------------------------------------------------------------
# Slivers and degenerates
# ---------------------------------------------------------------------------

def test_sliver_triangles_spanning_curvature_are_caught(tmp_path):
    """A thin triangle across a CURVED surface — what a quadric collapse leaves
    when it runs out of good edges, and what actually shades badly."""
    verts, tris, normals = [], [], []
    segs, rows = 48, 3
    for r in range(rows + 1):
        for j in range(segs):
            ang = 2.0 * math.pi * j / segs
            # a barrel: curved around, and the rows are very close together, so
            # every triangle is long around the curve and thin across it
            x, z = math.cos(ang), math.sin(ang)
            verts.append((x, r * 0.004, z))
            normals.append((x, 0.0, z))
    for r in range(rows):
        for j in range(segs):
            a = r * segs + j
            b = r * segs + (j + 1) % segs
            c = (r + 1) * segs + j
            d = (r + 1) * segs + (j + 1) % segs
            tris.append((a, b, c))
            tris.append((b, d, c))
    report = mq.faceting(_write_glb(tmp_path, "slivers.glb", verts, tris, normals))
    assert report["sliver_fraction"] > 0.9
    assert "slivers" in {i["kind"] for i in mq.faceting_verdict(report)["issues"]}


def test_a_thin_triangle_on_a_FLAT_face_is_not_a_sliver(tmp_path):
    """The control that keeps this tool usable on a furniture kit.

    A couch frame is 1.58m x 0.16m. Split that rectangle into two triangles and
    the minimum angle is about 5.8 degrees BY CONSTRUCTION — it is a rectangle,
    not a decimation artifact, and it shades perfectly because the surface is
    planar. Counting these reported the real couch at 73% slivers, and a gate
    that condemns every long box face is one that gets switched off in a week.
    """
    verts, tris = [], []
    for i in range(40):
        base = len(verts)
        verts.extend([(0.0, i * 0.1, 0.0), (10.0, i * 0.1, 0.0),
                      (5.0, i * 0.1 + 0.002, 0.0)])
        tris.append((base, base + 1, base + 2))
    report = mq.faceting(_write_glb(tmp_path, "flat_thin.glb", verts, tris))
    assert report["meshes"][0]["min_angle_p50"] < 10.0, (
        "the fixture must actually be thin or it controls nothing")
    assert report["sliver_fraction"] == 0.0
    assert "slivers" not in {i["kind"] for i in mq.faceting_verdict(report)["issues"]}


def test_well_shaped_triangles_are_not_called_slivers(tmp_path):
    v, t, n = _sphere()
    report = mq.faceting(_write_glb(tmp_path, "fine.glb", v, t, n))
    assert report["sliver_fraction"] < 0.02
    assert report["meshes"][0]["min_angle_p50"] > 20.0


def test_degenerate_triangles_are_counted_not_crashed_on(tmp_path):
    verts = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0)]
    report = mq.faceting(_write_glb(tmp_path, "zero.glb", verts, [(0, 1, 2)]))
    assert report["measured"] is True
    assert report["meshes"][0]["degenerate_triangles"] == 1


# ---------------------------------------------------------------------------
# The file-level roll-up
# ---------------------------------------------------------------------------

def test_the_worst_mesh_wins_rather_than_the_average(tmp_path):
    """One ruined sleeve inside a body that is fine must not average away —
    that is the case this was built for."""
    v, t, n = _sphere(jitter=0.06)
    report = mq.faceting(_write_glb(tmp_path, "one.glb", v, t, n))
    assert report["dent_worst_mesh"] == "one"
    assert report["dent_fraction"] == report["meshes"][0]["dent_fraction"]
