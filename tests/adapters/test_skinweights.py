"""Skin-weight plausibility, tested on hand-built GLBs — no Blender needed.

Every fixture here is a small skeleton written byte-for-byte, so the question
under test ("is this vertex driven by a bone anywhere near it?") is answerable
without an asset, an engine, or a rig authored by hand.

The pairing that matters is the last two tests: the SAME geometry, painted
correctly and painted wrong, so a green result is only meaningful because the
red one exists.
"""
from __future__ import annotations

import json
import struct

from bgate_adapters import skinweights as sw


#: A SPINE WITH A LIMB OFF IT, which is the smallest shape that can express
#: this defect at all. A straight chain cannot: paint a vertex to the wrong
#: bone in a single line of joints and that bone is still the nearest one, so
#: the ratio stays 1.0 and nothing is measured. The first version of these
#: fixtures was a straight chain and both the "good" and "bad" binds scored
#: 1.0 — the tests caught the test.
#:
#: root (0,0,0) -> spine (0,1,0) -> limb_a (0.5,1,0) -> limb_b (1.5,1,0)
#:
#: so `spine` spans x 0.0-0.5 and `limb_a` spans x 0.5-1.5, both at y=1.
_JOINT_POS = [(0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.5, 1.0, 0.0), (1.5, 1.0, 0.0)]
_JOINT_NAMES = ("root", "spine", "limb_a", "limb_b")

#: Vertices out along the limb, held 0.1 off its axis so that the distance to
#: the correct bone is small but non-zero — a vertex sitting exactly ON a bone
#: gives a zero denominator and no ratio worth reading.
_LIMB_VERTS = [(0.6 + 0.1 * i, 1.1, 0.0) for i in range(9)]


def _build_skinned_glb(tmp_path, name, assignments, *, weights=None,
                       joint_names=_JOINT_NAMES, verts=None, secondary=None):
    """A hand-built skinned GLB over the spine-and-limb skeleton above.

    `assignments` gives, per vertex, the joint index that dominates it.
    `secondary` gives the joint taking the remaining weight — it matters more
    than it looks, because a bone with NO weight anywhere is not a deform bone
    and the check correctly refuses to offer it as "the bone this should have
    been painted to". An earlier version of these fixtures starved the limb
    bone entirely, which left the mispainted case with nothing to be compared
    against and scored it a clean 1.0.
    `weights` optionally gives the full four-weight tuple.
    """
    verts = list(verts if verts is not None else _LIMB_VERTS)
    assert len(verts) == len(assignments)
    pos_bytes = b"".join(struct.pack("<3f", *v) for v in verts)
    if weights is None:
        weights = [(1.0, 0.0, 0.0, 0.0)] * len(assignments)
    w_bytes = b"".join(struct.pack("<4f", *w) for w in weights)
    sec = list(secondary if secondary is not None else [0] * len(assignments))
    j_bytes = b"".join(struct.pack("<4H", a, b, 0, 0)
                       for a, b in zip(assignments, sec))

    # inverse-bind matrices: translation is -joint_position (identity rotation)
    joint_pos = _JOINT_POS
    ibm_bytes = b""
    for p in joint_pos[:len(joint_names)]:
        m = [1.0, 0.0, 0.0, 0.0,
             0.0, 1.0, 0.0, 0.0,
             0.0, 0.0, 1.0, 0.0,
             -p[0], -p[1], -p[2], 1.0]
        ibm_bytes += struct.pack("<16f", *m)

    chunks = [pos_bytes, w_bytes, j_bytes, ibm_bytes]
    offsets, cur = [], 0
    for c in chunks:
        offsets.append(cur)
        cur += len(c)
    bin_data = b"".join(chunks)

    n = len(assignments)
    nodes = []
    for i, jn in enumerate(joint_names):
        node = {"name": jn}
        if i + 1 < len(joint_names):
            node["children"] = [i + 1]
        nodes.append(node)
    gltf = {
        "asset": {"version": "2.0"},
        "nodes": nodes,
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": n, "type": "VEC3"},
            {"bufferView": 1, "componentType": 5126, "count": n, "type": "VEC4"},
            {"bufferView": 2, "componentType": 5123, "count": n, "type": "VEC4"},
            {"bufferView": 3, "componentType": 5126,
             "count": len(joint_names), "type": "MAT4"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": offsets[i], "byteLength": len(c)}
            for i, c in enumerate(chunks)
        ],
        "buffers": [{"byteLength": len(bin_data)}],
        "meshes": [{"primitives": [{"attributes": {
            "POSITION": 0, "WEIGHTS_0": 1, "JOINTS_0": 2}}]}],
        "skins": [{"joints": list(range(len(joint_names))),
                   "inverseBindMatrices": 3}],
    }
    json_bytes = json.dumps(gltf).encode("utf-8")
    json_bytes += b" " * ((-len(json_bytes)) % 4)
    bin_padded = bin_data + b"\x00" * ((-len(bin_data)) % 4)
    json_chunk = struct.pack("<I4s", len(json_bytes), b"JSON") + json_bytes
    bin_chunk = struct.pack("<I4s", len(bin_padded), b"BIN\x00") + bin_padded
    total = 12 + len(json_chunk) + len(bin_chunk)
    header = struct.pack("<4sII", b"glTF", 2, total)
    path = tmp_path / name
    path.write_bytes(header + json_chunk + bin_chunk)
    return path


# ---------------------------------------------------------------------------
# Refusing rather than passing
# ---------------------------------------------------------------------------

def test_a_file_with_no_skin_refuses_instead_of_passing(tmp_path):
    path = tmp_path / "bare.glb"
    gltf = {"asset": {"version": "2.0"}, "nodes": [{"name": "mesh"}]}
    jb = json.dumps(gltf).encode("utf-8")
    jb += b" " * ((-len(jb)) % 4)
    chunk = struct.pack("<I4s", len(jb), b"JSON") + jb
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, 12 + len(chunk)) + chunk)

    report = sw.dominance(path)
    assert report["measured"] is False
    verdict = sw.dominance_verdict(report)
    assert verdict["passed"] is False, "an unmeasurable file must not pass"
    assert "skin" in verdict["reason"]


def test_an_unmeasured_report_never_passes():
    verdict = sw.dominance_verdict({"measured": False, "reason": "nothing"})
    assert verdict["passed"] is False


# ---------------------------------------------------------------------------
# The pairing: same geometry, painted right and painted wrong
# ---------------------------------------------------------------------------

#: Two influences, so neither fixture trips the separate no-falloff check and
#: the ONLY difference between the pair is which bone dominates — the one
#: variable under test.
_BLEND = [(0.8, 0.2, 0.0, 0.0)] * len(_LIMB_VERTS)


def _correct(tmp_path):
    """Every limb vertex dominated by the limb bone it sits on."""
    n = len(_LIMB_VERTS)
    return _build_skinned_glb(tmp_path, "good.glb", [2] * n,
                              secondary=[1] * n, weights=_BLEND)


def _mispainted(tmp_path):
    """The same limb vertices painted to `spine`, which ends at x=0.5.

    This is the shipped defect in miniature: geometry out on a limb driven by
    a bone back in the body, so it cannot follow the limb when the limb moves.
    """
    n = len(_LIMB_VERTS)
    return _build_skinned_glb(tmp_path, "bad.glb", [1] * n,
                              secondary=[2] * n, weights=_BLEND)


def _rigid(tmp_path):
    """Correctly painted, but with a single influence per vertex."""
    n = len(_LIMB_VERTS)
    return _build_skinned_glb(tmp_path, "rigid.glb", [2] * n,
                              secondary=[1] * n,
                              weights=[(1.0, 0.0, 0.0, 0.0)] * n)


def test_a_correctly_painted_bind_passes(tmp_path):
    report = sw.dominance(_correct(tmp_path))
    assert report["measured"] is True
    verdict = sw.dominance_verdict(report)
    assert verdict["passed"] is True, verdict["issues"]
    assert report["max_ratio"] < 3.0


def test_a_vertex_driven_by_a_distant_bone_is_caught(tmp_path):
    report = sw.dominance(_mispainted(tmp_path))
    assert report["measured"] is True
    verdict = sw.dominance_verdict(report)
    assert verdict["passed"] is False
    kinds = {i["kind"] for i in verdict["issues"]}
    assert "reaches_too_far" in kinds
    # and it names the bone actually at fault
    reaching = [i for i in verdict["issues"] if i["kind"] == "reaches_too_far"]
    assert any(i["bone"] == "spine" for i in reaching), reaching


def test_the_good_and_bad_binds_are_separated_by_the_ratio(tmp_path):
    good = sw.dominance(_correct(tmp_path))
    bad = sw.dominance(_mispainted(tmp_path))
    assert bad["max_ratio"] > good["max_ratio"] * 2, (
        f"good {good['max_ratio']} vs bad {bad['max_ratio']} — the metric "
        "must separate these or it is measuring nothing")


# ---------------------------------------------------------------------------
# The two secondary findings
# ---------------------------------------------------------------------------

def test_a_fully_rigid_bind_is_reported_as_having_no_falloff(tmp_path):
    report = sw.dominance(_rigid(tmp_path))
    assert report["rigid_fraction"] == 1.0
    verdict = sw.dominance_verdict(report, max_rigid_fraction=0.5)
    assert any(i["kind"] == "no_falloff" for i in verdict["issues"])


def test_a_blended_bind_is_not_reported_as_rigid(tmp_path):
    report = sw.dominance(_correct(tmp_path))
    assert report["rigid_fraction"] == 0.0
    verdict = sw.dominance_verdict(report, max_rigid_fraction=0.5)
    assert not any(i["kind"] == "no_falloff" for i in verdict["issues"])


def test_dead_bones_are_listed_but_do_not_fail_by_default(tmp_path):
    # `root` drives nothing in either fixture — that is normal for a control
    # bone, and the good rig this tool was calibrated against fails on exactly
    # this when the flag is on.
    report = sw.dominance(_correct(tmp_path))
    assert "root" in report["dead_bones"]
    assert sw.dominance_verdict(report)["passed"] is True
    strict = sw.dominance_verdict(report, flag_dead_bones=True)
    assert strict["passed"] is False
    assert any(i["kind"] == "dead_bones" for i in strict["issues"])


def test_non_deform_bones_are_not_candidates_for_nearest(tmp_path):
    """The check's own first-run false positive, pinned.

    `root` sits at y=0 and skins nothing. Before deform-only filtering it was
    reported as the "nearest bone" for vertices near the base, inflating the
    ratio to 6.91 on a real asset by comparing against a bone no vertex is
    allowed to be painted to.
    """
    report = sw.dominance(_correct(tmp_path))
    nearest = {w["nearest_bone"] for w in report["worst"]}
    assert "root" not in nearest, (
        "a bone that deforms nothing must never be offered as the bone a "
        f"vertex should have been painted to (got {nearest})")
