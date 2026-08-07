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

def test_weight_islands_verdict_passes_one_contiguous_patch():
    report = {"bones": {"Hand.L": {"vertex_count": 240, "islands": 1,
                                   "sizes": [240], "largest_fraction": 1.0}}}
    assert blender.weight_islands_verdict(report)["passed"] is True


def test_weight_islands_verdict_catches_a_bleeding_bone():
    report = {"bones": {"Hand.L": {"vertex_count": 240, "islands": 2,
                                   "sizes": [232, 8], "largest_fraction": 0.967},
                        "Spine": {"vertex_count": 900, "islands": 1,
                                  "sizes": [900], "largest_fraction": 1.0}}}
    verdict = blender.weight_islands_verdict(report, min_largest_fraction=0.99)
    assert verdict["passed"] is False
    assert [i["bone"] for i in verdict["issues"]] == ["Hand.L"]
    assert verdict["issues"][0]["bleed_vertices"] == 8


def test_weight_islands_verdict_ignores_a_single_stray_vertex():
    """One vertex a brush missed is a cleanup nit, not a failure this gate names."""
    report = {"bones": {"Hand.L": {"vertex_count": 240, "islands": 2,
                                   "sizes": [239, 1], "largest_fraction": 0.996}}}
    verdict = blender.weight_islands_verdict(report)
    assert verdict["passed"] is True


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
def test_rig_reports_bone_coverage(bound_glb):
    _, report = bound_glb
    assert report.get("bone_names")
    coverage = report.get("coverage") or {}
    assert coverage.get("checked") == 15
    assert coverage.get("passed") is True, coverage.get("missing")


@needs_blender
def test_weight_islands_measures_a_real_bind(bound_glb):
    out, _ = bound_glb
    got = blender.weight_islands(str(out), timeout=900)
    assert got["ok"] is True, got.get("error")
    assert got["deform_bones"] >= 10
    assert len(got["bones"]) >= 10
    # A clean bind from a fresh bind() call should read as fully contiguous.
    verdict = blender.weight_islands_verdict(got)
    assert verdict["passed"] is True, verdict["issues"]


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
