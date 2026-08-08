"""The rig quality gates: does it DEFORM, and does the ENGINE call it humanoid?

``test_blender_gates.py`` established the rule this file follows — every test
either makes a check FIRE or measures a number that came out of a real tool.
Both gates here exist because their absence hid a specific failure:

  * ``flex`` — the first real run of the deformation gate PASSED a model whose
    mesh carried no armature modifier. Six poses, zero issues, nothing moving.
    ``test_flex_refuses_an_inert_model`` is that bug.

  * ``retarget_check`` — the first version of the Godot probe reported a
    correctly-parented 23-bone skeleton as unparented, because Skeleton3D
    recomputes global bone transforms on its own notification and the probe read
    them in the same call that set the pose. ``test_retarget_chain_propagates``
    is that bug.

Real Blender and real Godot throughout. A mock cannot tell you that a bone pose
is an absolute local transform rather than a delta on rest, which is the other
thing that was wrong here.
"""
from __future__ import annotations

import json

import pytest

from bgate_adapters import blender, godot

needs_blender = pytest.mark.skipif(
    not blender.available()["available"], reason="Blender not installed"
)
needs_godot = pytest.mark.skipif(
    not godot.available()["available"], reason="Godot not installed"
)

HUMAN = 'bg_human(height=1.8, rig=True, name="Probe", pose="a")'


@pytest.fixture(scope="module")
def unbound_glb(tmp_path_factory):
    """A humanoid and a skeleton in one file, with NOTHING binding them.

    This is what bg_human produces on its own, and it is the shape of the false
    pass: an armature sitting inside a mesh it does not drive.
    """
    out = tmp_path_factory.mktemp("rigq") / "unbound.glb"
    got = blender.run_script(HUMAN, export_glb=str(out), record=False,
                             out_dir=str(out.parent))
    assert got["ok"] is True, got.get("error")
    return out


@pytest.fixture(scope="module")
def bound_glb(unbound_glb):
    """The same figure taken through rig(): adopted, fitted, bound, proven."""
    out = unbound_glb.parent / "bound.glb"
    got = blender.rig(str(unbound_glb), str(out), timeout=900)
    assert got.get("ok") is True, got.get("error")
    assert got.get("rigged") is True, got.get("reason")
    return out, got


# ---------------------------------------------------------------------------
# The verdict is pure, so it is tested without a tool anywhere near it
# ---------------------------------------------------------------------------

def test_verdict_fires_on_each_axis():
    report = {"rest": {"faces": 10000, "armature_modifier": True,
                       "vertex_groups": 23},
              "poses": [{"label": "elbow_bend", "volume_ratio": 0.70,
                         "max_displacement": 0.4,
                         "worst_pinch": {"bone": "LeftLowerArm", "ratio": 0.31},
                         "new_self_pairs": 900}]}
    verdict = blender.flex_verdict(report)
    kinds = {i["kind"] for i in verdict["issues"]}
    assert verdict["passed"] is False
    assert kinds == {"volume", "pinch", "intersection"}


def test_verdict_passes_a_healthy_bind():
    report = {"rest": {"faces": 10000, "armature_modifier": True,
                       "vertex_groups": 23},
              "poses": [{"label": "elbow_bend", "volume_ratio": 0.98,
                         "max_displacement": 0.4,
                         "worst_pinch": {"bone": "LeftLowerArm", "ratio": 0.94},
                         "new_self_pairs": 3}]}
    assert blender.flex_verdict(report)["passed"] is True


def test_verdict_calls_an_unbound_mesh_inert_before_anything_else():
    """Every other threshold reports perfection on a mesh nothing drives."""
    report = {"rest": {"faces": 10000, "armature_modifier": False,
                       "vertex_groups": 0},
              "poses": [{"label": "elbow_bend", "volume_ratio": 1.0,
                         "max_displacement": 0.0, "new_self_pairs": 0}]}
    verdict = blender.flex_verdict(report)
    assert verdict["passed"] is False
    assert [i["kind"] for i in verdict["issues"]] == ["inert"]


def test_verdict_catches_weights_that_move_nothing():
    """Modifier present, groups present, and not one vertex travels."""
    report = {"rest": {"faces": 10000, "armature_modifier": True,
                       "vertex_groups": 23},
              "poses": [{"label": "elbow_bend", "volume_ratio": 1.0,
                         "max_displacement": 0.0, "new_self_pairs": 0}]}
    verdict = blender.flex_verdict(report)
    assert [i["kind"] for i in verdict["issues"]] == ["inert"]


# ---------------------------------------------------------------------------
# Template deviation: does a rig share the shipped skeleton's proportions
#
# Bone LENGTHS, not joint positions. Positions are stance-dependent and the two
# skeletons are never in the same stance — rig() defaults to an A-pose and the
# template is a T-pose — so the positional version reported both hands 0.154
# body-heights out against a 0.08 threshold on a correctly-rigged character,
# perfectly mirrored left to right, with every non-arm bone at exactly 0.0.
# Each bone maps to [length_as_body_height_fraction, parent_name].
# ---------------------------------------------------------------------------

def _tdev(ref, cand):
    return {"ok": True, "reference_bones": ref, "candidate_bones": cand}


def test_template_deviation_passes_an_identical_skeleton():
    bones = {"Hips": [0.06, "Root"], "Head": [0.13, "Neck"]}
    verdict = blender.template_deviation_verdict(_tdev(bones, dict(bones)))
    assert verdict["passed"] is True
    assert verdict["checked"] == 2


def test_template_deviation_survives_a_stance_difference():
    """THE BUG THIS CHECK WAS BORN WITH. An A-posed candidate against the
    T-posed template failed every bone in the arm chain, on a rig that was
    correct. Rotating a joint cannot change the length of the bone below it,
    so identical proportions read as identical whatever the pose."""
    bones = {"LeftUpperArm": [0.155, "LeftShoulder"],
             "LeftLowerArm": [0.146, "LeftUpperArm"],
             "LeftHand": [0.04, "LeftLowerArm"]}
    verdict = blender.template_deviation_verdict(_tdev(bones, dict(bones)))
    assert verdict["passed"] is True


def test_template_deviation_names_a_misproportioned_bone():
    ref = {"Hips": [0.06, "Root"], "LeftHand": [0.04, "LeftLowerArm"]}
    # A hand bone stretched to a quarter of body height — no fit produces this.
    cand = {"Hips": [0.06, "Root"], "LeftHand": [0.25, "LeftLowerArm"]}
    verdict = blender.template_deviation_verdict(_tdev(ref, cand))
    assert verdict["passed"] is False
    assert verdict["issues"][0]["bone"] == "LeftHand"
    assert verdict["issues"][0]["kind"] == "proportion"


def test_template_deviation_catches_a_rewired_chain():
    """Same 23 names, different hierarchy — nothing retargets onto that."""
    ref = {"LeftHand": [0.04, "LeftLowerArm"]}
    cand = {"LeftHand": [0.04, "Hips"]}
    verdict = blender.template_deviation_verdict(_tdev(ref, cand))
    assert verdict["passed"] is False
    assert verdict["issues"][0]["kind"] == "hierarchy"


def test_template_deviation_only_compares_shared_bones():
    ref = {"Hips": [0.06, "Root"], "Tail": [0.2, "Hips"]}
    cand = {"Hips": [0.06, "Root"]}
    verdict = blender.template_deviation_verdict(_tdev(ref, cand))
    assert verdict["checked"] == 1
    assert verdict["passed"] is True


def test_template_deviation_refuses_a_skeleton_it_shares_no_names_with():
    """An empty intersection is not agreement. A candidate on the mixamorig
    scheme used to report checked 0 and pass."""
    verdict = blender.template_deviation_verdict(
        _tdev({"Hips": [0.06, "Root"]}, {"mixamorig:Hips": [0.06, "Root"]}))
    assert verdict["passed"] is False
    assert verdict["checked"] == 0
    assert verdict["issues"][0]["kind"] == "unmeasured"


def test_template_deviation_refuses_a_failed_run():
    verdict = blender.template_deviation_verdict(
        {"ok": False, "error": "no report from Blender"})
    assert verdict["passed"] is False
    assert verdict["issues"][0]["kind"] == "unmeasured"


# ---------------------------------------------------------------------------
# Silhouette across the pose sweep — EXPERIMENTAL
# ---------------------------------------------------------------------------

def test_hull_area_of_a_unit_square():
    assert blender._hull_area([(0, 0), (1, 0), (1, 1), (0, 1)]) == 1.0


def test_hull_area_ignores_an_interior_point():
    pts = [(0, 0), (1, 0), (1, 1), (0, 1), (0.5, 0.5)]
    assert blender._hull_area(pts) == 1.0


def test_hull_area_of_a_triangle():
    assert blender._hull_area([(0, 0), (2, 0), (0, 2)]) == 2.0


def test_hull_area_is_zero_on_collinear_points():
    assert blender._hull_area([(0, 0), (1, 0), (2, 0)]) == 0.0


def test_hull_area_is_zero_on_fewer_than_three_points():
    assert blender._hull_area([]) == 0.0
    assert blender._hull_area([(1, 1)]) == 0.0
    assert blender._hull_area([(0, 0), (1, 1)]) == 0.0


def test_silhouette_verdict_passes_an_ordinary_pose_change():
    report = {"ok": True,
              "poses": [{"label": "arm_raise", "area_ratio": 1.4},
                        {"label": "elbow_bend", "area_ratio": 0.85}]}
    verdict = blender.silhouette_verdict(report)
    assert verdict["passed"] is True
    assert verdict["checked"] == 2


def test_silhouette_verdict_catches_a_collapse():
    report = {"ok": True, "poses": [{"label": "knee_bend", "area_ratio": 0.05}]}
    verdict = blender.silhouette_verdict(report)
    assert verdict["passed"] is False
    assert verdict["issues"][0]["kind"] == "collapsed"


def test_silhouette_verdict_catches_an_explosion():
    report = {"ok": True, "poses": [{"label": "spine_twist", "area_ratio": 12.0}]}
    verdict = blender.silhouette_verdict(report)
    assert verdict["passed"] is False
    assert verdict["issues"][0]["kind"] == "exploded"


def test_silhouette_verdict_refuses_a_sweep_that_measured_nothing():
    """Every pose skipped is not every pose clean. This returned checked 0
    and passed — a sweep of a rig missing the bones it rotates."""
    report = {"ok": True,
              "poses": [{"label": "head_turn", "skipped": "no bone"},
                        {"label": "knee_bend", "area_ratio": None}]}
    verdict = blender.silhouette_verdict(report)
    assert verdict["passed"] is False
    assert verdict["checked"] == 0
    assert verdict["issues"][0]["kind"] == "unmeasured"


def test_silhouette_verdict_refuses_an_inert_model():
    """flex_verdict's own bug, reintroduced here. A mesh no bone drives
    projects the identical outline in every pose, so every ratio is exactly
    1.0 — and bounds that only fire far from 1.0 called that a perfect sweep."""
    report = {"ok": True,
              "poses": [{"label": p, "area_ratio": 1.0}
                        for p in ("arm_raise", "elbow_bend", "knee_bend")]}
    verdict = blender.silhouette_verdict(report)
    assert verdict["passed"] is False
    assert verdict["issues"][0]["kind"] == "inert"


@needs_blender
def test_silhouette_measures_a_real_bind(bound_glb):
    out, _ = bound_glb
    got = blender.silhouette(str(out), timeout=900)
    assert got["ok"] is True, got.get("error")
    assert got["rest_area"] > 0
    assert got["rest_points"] >= 3
    verdict = blender.silhouette_verdict(got)
    # bg_human's own walk-cycle poses on its own body should not collapse or
    # explode the projected silhouette from flex's fixed camera.
    assert verdict["checked"] >= 4
    assert verdict["passed"] is True, verdict["issues"]


# ---------------------------------------------------------------------------
# Coverage: are the essential humanoid bones present, under the exact name
# ---------------------------------------------------------------------------

def test_coverage_passes_a_full_skeleton():
    verdict = blender.humanoid_coverage_verdict(list(blender.HUMANOID_BONES))
    assert verdict["passed"] is True
    assert verdict["missing"] == []


def test_coverage_names_a_missing_essential_bone():
    names = [b for b in blender.HUMANOID_BONES if b != "Hips"]
    verdict = blender.humanoid_coverage_verdict(names)
    assert verdict["passed"] is False
    assert verdict["missing"] == ["Hips"]


def test_coverage_is_exact_name_not_fuzzy():
    """A BoneMap-free retarget matches by string, so a near-miss still misses."""
    names = [b if b != "LeftHand" else "LeftHand_1" for b in blender.HUMANOID_BONES]
    verdict = blender.humanoid_coverage_verdict(names)
    assert "LeftHand" in verdict["missing"]


# ---------------------------------------------------------------------------
# Weight-island bleed: does a bone's paint cover one patch or two
# ---------------------------------------------------------------------------

def _bone(islands, shells, bleed, count=240):
    return {"vertex_count": count, "islands": islands, "shells": shells,
            "bleed_vertices": bleed, "sizes": [count - bleed, bleed],
            "largest_fraction": round((count - bleed) / count, 4)}


def test_weight_islands_verdict_passes_one_contiguous_patch():
    report = {"ok": True, "bones": {"Hand.L": _bone(1, 1, 0)}}
    assert blender.weight_islands_verdict(report)["passed"] is True


def test_weight_islands_verdict_catches_a_bleeding_bone():
    report = {"ok": True, "bones": {"Hand.L": _bone(2, 1, 8),
                                    "Spine": _bone(1, 1, 0, count=900)}}
    verdict = blender.weight_islands_verdict(report)
    assert verdict["passed"] is False
    assert [i["bone"] for i in verdict["issues"]] == ["Hand.L"]
    assert verdict["issues"][0]["bleed_vertices"] == 8


def test_weight_islands_verdict_allows_a_bone_spanning_separate_mesh_pieces():
    """THE BUG THIS GATE WAS BORN WITH, second layer. This pipeline joins
    characters out of primitives, so a hip bone legitimately covers three
    disconnected shells. Counting its islands against a hardcoded 1 made
    every such bone a false positive on a bind that was correct."""
    report = {"ok": True, "bones": {"Hips": _bone(3, 3, 90)}}
    assert blender.weight_islands_verdict(report)["passed"] is True


def test_weight_islands_verdict_still_catches_a_split_inside_one_piece():
    """Spanning pieces is fine; splitting within one is what nothing but a
    stray stroke explains."""
    report = {"ok": True, "bones": {"Hips": _bone(4, 3, 90)}}
    verdict = blender.weight_islands_verdict(report)
    assert verdict["passed"] is False
    assert verdict["issues"][0]["shells"] == 3


def test_weight_islands_verdict_ignores_a_single_stray_vertex():
    """One vertex a brush missed is a cleanup nit, not a failure this gate names."""
    report = {"ok": True, "bones": {"Hand.L": _bone(2, 1, 1)}}
    assert blender.weight_islands_verdict(report)["passed"] is True


def test_weight_islands_verdict_refuses_a_bind_with_no_weights():
    """An empty table is not a clean one. A bind where no vertex cleared the
    threshold reported checked 0 and passed."""
    verdict = blender.weight_islands_verdict({"ok": True, "bones": {}})
    assert verdict["passed"] is False
    assert verdict["checked"] == 0
    assert verdict["issues"][0]["kind"] == "unmeasured"


def test_weight_islands_verdict_refuses_a_failed_run():
    verdict = blender.weight_islands_verdict({"ok": False, "error": "boom"})
    assert verdict["passed"] is False
    assert verdict["issues"][0]["kind"] == "unmeasured"


# ---------------------------------------------------------------------------
# The gate, against real geometry
# ---------------------------------------------------------------------------

@needs_blender
def test_flex_refuses_an_inert_model(unbound_glb, tmp_path):
    """THE BUG THIS GATE WAS BORN WITH. Six green poses on a dead rig."""
    got = blender.flex(str(unbound_glb), tmp_path, render=False, timeout=900)
    assert got["ok"] is True, got.get("error")
    assert got["rest"]["armature_modifier"] is False
    assert got["verdict"]["passed"] is False
    assert got["verdict"]["issues"][0]["kind"] == "inert"
    # And the vacuous evidence is still visible: nothing moved anywhere.
    assert max(p["max_displacement"] for p in got["poses"]) == 0.0


@needs_blender
def test_flex_measures_a_real_bind(bound_glb, tmp_path):
    out, _ = bound_glb
    got = blender.flex(str(out), tmp_path, render=False, timeout=900)
    assert got["ok"] is True, got.get("error")
    assert got["rest"]["armature_modifier"] is True
    assert got["rest"]["vertex_groups"] >= 20
    assert got["rest"]["owned_bones"] >= 10

    posed = [p for p in got["poses"] if not p.get("skipped")]
    assert len(posed) == len(blender.FLEX_POSES)
    # Every pose moved the body, and none of them moved it a kilometre.
    for pose in posed:
        assert 0.01 < pose["max_displacement"] < 5.0, pose
        assert 0.5 < pose["volume_ratio"] <= 1.05, pose
    # The pinch table is TRIMMED. The full 23-bone table per pose overflowed
    # run_script's 4000-character stdout window and the report parsed as absent.
    for pose in posed:
        assert len(pose.get("pinch") or {}) <= 6


@needs_blender
def test_flex_renders_one_frame_per_pose(bound_glb, tmp_path):
    out, _ = bound_glb
    got = blender.flex(str(out), tmp_path, render=True, size=(160, 160),
                       timeout=900)
    assert got["ok"] is True, got.get("error")
    shots = [p["render"] for p in got["poses"] if p.get("render")]
    assert len(shots) == len(blender.FLEX_POSES)
    from pathlib import Path
    for shot in shots:
        assert Path(shot).stat().st_size > 500, shot


@needs_blender
def test_template_deviation_of_a_real_bind(bound_glb):
    out, _ = bound_glb
    got = blender.template_deviation(str(out), timeout=900)
    assert got["ok"] is True, got.get("error")
    assert got["candidate_bones"]
    verdict = blender.template_deviation_verdict(got)
    # bg_human IS the template, adopted and rebound — its own bones should
    # keep the shipped skeleton's proportions. It is rigged in an A-pose and
    # the template is a T-pose, which is exactly the difference lengths are
    # immune to and joint positions were not.
    assert verdict["checked"] >= 10
    assert verdict["passed"] is True, verdict["issues"]


@needs_blender
def test_rig_reports_bone_coverage(bound_glb):
    _, report = bound_glb
    assert report.get("bone_names")
    coverage = report.get("coverage") or {}
    assert coverage.get("checked") == 15
    assert coverage.get("passed") is True, coverage.get("missing")


@needs_blender
def test_weight_islands_does_not_flag_every_bone_of_a_clean_bind(bound_glb):
    """THE FALSE-POSITIVE STORM. Before the adjacency graph welded coincident
    vertices, this reported 14 to 30 islands on EVERY deform bone of a bind
    the rest of the suite calls good — it was counting the vertex splits the
    glTF exporter makes at UV and normal seams, not weight bleed. A gate that
    is red on 100% of valid input is worse than no gate: it teaches the agent
    reading it to ignore the result.

    Asserted as a ceiling on flagged bones rather than zero, because zero is
    not true of this fixture and pinning it would have been the same mistake
    in the other direction — see the test below.
    """
    out, _ = bound_glb
    got = blender.weight_islands(str(out), timeout=900)
    assert got["ok"] is True, got.get("error")
    assert got["deform_bones"] >= 10
    assert len(got["bones"]) >= 10
    verdict = blender.weight_islands_verdict(got)
    flagged = {i["bone"] for i in verdict["issues"]}
    assert len(flagged) <= 3, sorted(flagged)
    # The limbs were the loudest false positives and must now be clean.
    for bone in ("LeftUpperArm", "RightUpperArm", "LeftUpperLeg",
                 "RightUpperLeg", "LeftShoulder", "RightShoulder"):
        assert bone not in flagged, verdict["issues"]


@needs_blender
def test_weight_islands_finds_the_chest_split_in_the_shipped_template(bound_glb):
    """A REAL PROPERTY OF bg_human's BIND, not a measurement artifact, and
    recorded here so a future change to bind() shows up as a diff rather than
    as a silent improvement or regression.

    Chest's weights fall into two separate regions of ONE connected piece of
    mesh — 22 vertices and 15 — and they stay separate as the membership
    threshold is raised from 0.02 all the way to 0.2, so this is not the
    near-zero fringe a low threshold invents. Whether it is worth fixing in
    the template is a rigging question this test does not answer; that it is
    genuinely there is what it pins.
    """
    out, _ = bound_glb
    got = blender.weight_islands(str(out), threshold=0.1, timeout=900)
    chest = got["bones"]["Chest"]
    assert chest["shells"] == 1
    assert chest["islands"] == 2, chest


# ---------------------------------------------------------------------------
# The audit, which runs BEFORE the bind and predicts how it will go
# ---------------------------------------------------------------------------

@needs_blender
def test_rig_reports_shells_and_symmetry(bound_glb):
    _, report = bound_glb
    audit = report["audit"]
    # A primitive humanoid IS several shells — head, torso, four limbs. The
    # number being honest is the point; a real generation came back as 940.
    assert audit["shells"]["count"] >= 2
    assert audit["shells"]["largest"] <= audit["shells"]["verts"]
    assert audit["symmetry"]["ok"] is True
    # Built by mirroring, so it should measure as very nearly its own mirror.
    assert audit["symmetry"]["mean"] < 0.01


@needs_blender
def test_rig_symmetrises_a_symmetric_body(bound_glb):
    _, report = bound_glb
    sym = report["symmetrised"]
    assert sym["ran"] is True, sym.get("reason")
    assert sym["paired"] > 0
    # Mirroring must never cost coverage — that was the regression guard.
    assert sym["unweighted_after"] <= max(8, report["adopt"].get("verts", 0) * 0.01)
    assert report["rigged"] is True


@needs_blender
def test_symmetrize_off_is_honoured(unbound_glb, tmp_path):
    out = tmp_path / "nosym.glb"
    got = blender.rig(str(unbound_glb), str(out), symmetrize="off", timeout=900)
    assert got.get("ok") is True, got.get("error")
    assert got["symmetrised"]["ran"] is False
    assert got["symmetrised"]["reason"] == "disabled"


# ---------------------------------------------------------------------------
# The engine's own opinion
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def probe_project(tmp_path_factory, bound_glb):
    if not godot.available()["available"]:
        pytest.skip("Godot not installed")
    out, _ = bound_glb
    root = tmp_path_factory.mktemp("gproj")
    (root / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nconfig/name="retarget-probe"\n',
        encoding="utf-8")
    got = godot.import_asset(str(root), str(out), dest_rel="assets")
    assert got["ok"] is True, got
    return root, got["res_path"]


@needs_godot
def test_retarget_chain_propagates(probe_project):
    """Rotate a shoulder, watch the hand. A flat skeleton passes every other
    check in this product and fails only this one."""
    root, res = probe_project
    got = godot.retarget_check(str(root), res)
    assert got.get("ok") is True, got
    assert got["essential_missing"] == []
    assert got["chain"], got
    for link in got["chain"]:
        assert link["propagates"] is True, link
        # Composed onto the bone's REST rotation, so the swing is physical: a
        # 45 degree hip moves a foot well under a metre on a 1.8 m figure.
        assert 0.05 < link["moved_m"] < 1.0, link


@needs_godot
def test_retarget_clip_drives_the_skeleton(probe_project):
    root, res = probe_project
    got = godot.retarget_check(str(root), res)
    assert got["clip"]["drives"] is True, got["clip"]
    # The track keys rest and rest*60deg, so the measured delta IS the 60.
    assert abs(got["clip"]["rotated_deg"] - 60.0) < 1.0, got["clip"]
    assert got["retargetable"] is True


@needs_godot
def test_retarget_writes_a_bone_map(probe_project, tmp_path):
    root, res = probe_project
    got = godot.retarget_check(str(root), res,
                               bone_map_res="res://probe_bonemap.tres")
    assert got["bone_map"]["written"] is True, got["bone_map"]
    assert got["bone_map"]["entries"] >= 15
    written = root / "probe_bonemap.tres"
    assert written.exists()
    body = written.read_text(encoding="utf-8")
    assert "BoneMap" in body and "Hips" in body


@needs_godot
def test_retarget_refuses_a_path_that_is_not_res(probe_project):
    root, _ = probe_project
    got = godot.retarget_check(str(root), str(root / "assets" / "bound.glb"))
    assert got["ok"] is False
    assert "res://" in got["error"]


@needs_godot
def test_retarget_says_so_when_the_model_is_not_rigged(probe_project):
    """A prop has no Skeleton3D, and the answer must name that rather than
    crashing or reporting an empty humanoid."""
    root, _ = probe_project
    got = godot.retarget_check(str(root), "res://assets/does_not_exist.glb")
    assert got["ok"] is False
    assert "no resource" in json.dumps(got)


# ---------------------------------------------------------------------------
# The two hull_area copies
# ---------------------------------------------------------------------------

def test_the_embedded_hull_area_matches_the_module_one():
    """_hull_area and the copy inside _SILHOUETTE_SCRIPT are maintained by
    hand, in two places, and only one of them is reachable from a test — the
    other runs inside Blender. Nothing stopped them drifting apart, which
    would have made the module's unit tests above prove nothing about the
    number the gate actually judges.
    """
    ns: dict = {}
    body = blender._SILHOUETTE_SCRIPT.split("def hull_area(points):", 1)[1]
    lines = body.splitlines()
    end = next(i for i, ln in enumerate(lines)
               if ln and not ln.startswith((" ", "\t")))
    exec("def hull_area(points):" + "\n".join(lines[:end]), ns)
    embedded = ns["hull_area"]

    cases = [
        [(0, 0), (1, 0), (1, 1), (0, 1)],
        [(0, 0), (2, 0), (0, 2)],
        [(0, 0), (1, 0), (1, 1), (0, 1), (0.5, 0.5)],
        [(0, 0), (1, 0), (2, 0)],
        [(1, 1)],
        [],
        [(0.0, 0.0), (3.5, 0.25), (2.0, 4.0), (-1.0, 2.5), (1.0, 1.0)],
    ]
    for pts in cases:
        assert embedded(pts) == blender._hull_area(pts), pts
