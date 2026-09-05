"""The modelling kit itself, exercised against REAL Blender.

The kit is a string of bpy source that gets prepended to an agent's script, so
the only place it can be tested is inside Blender. Mocking bpy here would test a
fiction: every defect these tests cover — a bone reparented to nothing, a
zero-length bone deleted on mode exit, a name Blender quietly changed — is
Blender's behaviour, not ours, and a fake would happily agree with the bug.

`bg_bone_chain` had no test at all. The rig it produced from a natural top-down
bone list was three parentless roots.
"""
from __future__ import annotations

import json

import pytest

from bgate_adapters import _blender_kit as kit
from bgate_adapters import blender

_no_blender = pytest.mark.skipif(
    not blender.available()["available"], reason="Blender not installed"
)


def requires_blender(obj):
    """SLOW as well as skipped-when-missing. See test_blender.py's docstring.

    Composed rather than set as a module-level ``pytestmark`` because a handful
    of tests here assert on the generated script without ever running it, and
    marking those slow would take real coverage out of the default run to no
    purpose.
    """
    return pytest.mark.slow(_no_blender(obj))


def _run(tmp_path, script):
    got = blender.run_script(script, out_dir=str(tmp_path))
    assert got["ok"] is True, got.get("error")
    return got


def _result(tmp_path, script):
    """Scripts hand structured findings back on stdout — bpy state does not
    survive the subprocess, only what the script chose to print."""
    got = _run(tmp_path, script)
    line = [ln for ln in got["print"].splitlines() if ln.startswith("RESULT ")]
    assert line, got["print"]
    return json.loads(line[-1][len("RESULT "):])


def _fails(tmp_path, script):
    got = blender.run_script(script, out_dir=str(tmp_path))
    assert got["ok"] is False, "expected the kit to refuse this"
    return got["error"] + "\n" + got.get("traceback", "")


# ---------------------------------------------------------------------------
# The kit as source — no Blender needed
# ---------------------------------------------------------------------------

class TestKitSource:
    def test_the_kit_is_valid_python(self):
        compile(kit.KIT, "<KIT>", "exec")

    def test_the_worked_example_is_valid_python(self):
        # It is a REFERENCE SCRIPT, not prose. If it stops compiling it has
        # stopped being worth reading.
        compile(kit.EXAMPLE, "<EXAMPLE>", "exec")

    def test_the_example_ships_inside_the_kit(self):
        # An agent that needs the example is already inside Blender with only
        # this script in front of it, so the example has to travel with it.
        assert "BG_EXAMPLE" in kit.KIT
        assert "bg_bone_chain" in kit.EXAMPLE
        assert kit.EXAMPLE.strip() in kit.KIT

    def test_the_example_ends_the_way_every_layer_must(self):
        assert "bg_finish(" in kit.EXAMPLE
        body = kit.EXAMPLE[:kit.EXAMPLE.rindex("bg_finish(")]
        assert "bg_bone_chain(" in body and "bg_mirror(" in body
        assert "bg_taper(" in body


# ---------------------------------------------------------------------------
# bg_bone_chain — the rig
# ---------------------------------------------------------------------------

TOP_DOWN = """
import json
rig = bg_bone_chain("Skeleton", [
    ("Hand",  (0.4, 0, 1.0), (0.4, 0, 0.9), "Arm",   0.0),
    ("Arm",   (0.2, 0, 1.4), (0.4, 0, 1.0), "Spine", 0.0),
    ("Spine", (0.0, 0, 0.9), (0.0, 0, 1.4), None,    0.0),
])
print("RESULT " + json.dumps({
    "bones": [b.name for b in rig.data.bones],
    "parents": {b.name: (b.parent.name if b.parent else None)
                for b in rig.data.bones},
}))
"""


@requires_blender
class TestBoneChainOrder:
    """THE BUG. Parents used to resolve against the bones already made, so a
    bone listed before its parent was silently reparented to nothing. This exact
    list — the natural way a person writes a limb down — produced three
    parentless roots and nothing said so."""

    def test_top_down_authoring_still_wires_the_chain(self, tmp_path):
        got = _result(tmp_path, TOP_DOWN)
        assert got["parents"] == {"Hand": "Arm", "Arm": "Spine", "Spine": None}

    def test_top_down_authoring_leaves_exactly_one_root(self, tmp_path):
        got = _result(tmp_path, TOP_DOWN)
        roots = [name for name, parent in got["parents"].items() if parent is None]
        assert roots == ["Spine"], "a rig with more than one root is not a rig"

    def test_parent_and_roll_are_both_optional(self, tmp_path):
        got = _result(tmp_path, """
import json
rig = bg_bone_chain("Skeleton", [
    ("Root", (0, 0, 0), (0, 0, 1)),
    ("Tip",  (0, 0, 1), (0, 0, 2), "Root"),
])
print("RESULT " + json.dumps({"parents": {b.name: (b.parent.name if b.parent
                                                   else None)
                                          for b in rig.data.bones}}))
""")
        assert got["parents"] == {"Root": None, "Tip": "Root"}


@requires_blender
class TestBoneChainRefuses:
    """Everything else in the kit swallows its problems. A rig cannot: a wrong
    armature looks built and comes apart in the engine, hours later."""

    def test_a_parent_that_does_not_exist_is_named(self, tmp_path):
        error = _fails(tmp_path, """
bg_bone_chain("Skeleton", [("Hand", (0, 0, 1), (0, 0, 2), "Arm")])
""")
        assert "'Hand'" in error and "'Arm'" in error
        assert "no bone in the list defines" in error

    def test_a_zero_length_bone_is_refused_not_deleted(self, tmp_path):
        # Blender drops head == tail bones on leaving edit mode and reports
        # nothing — the bone is simply absent from the armature you get back.
        error = _fails(tmp_path, """
bg_bone_chain("Skeleton", [("Spine", (0, 0, 1), (0, 0, 2), None),
                           ("Zero", (0, 0, 2), (0, 0, 2), "Spine")])
""")
        assert "'Zero'" in error
        assert "head == tail" in error

    def test_a_duplicate_name_is_refused(self, tmp_path):
        # edit_bones.new() renames on collision, so the second Head becomes
        # Head.001 and bind='bone:Head' then matches the wrong bone.
        error = _fails(tmp_path, """
bg_bone_chain("Skeleton", [("Head", (0, 0, 1), (0, 0, 2), None),
                           ("Head", (0, 0, 2), (0, 0, 3), None)])
""")
        assert "'Head'" in error
        assert "defined twice" in error

    def test_a_refusal_does_not_strand_blender_in_edit_mode(self, tmp_path):
        # The raise happens inside edit mode. Anything the run does afterwards —
        # including the adapter reading the scene back — needs object mode.
        got = blender.run_script("""
try:
    bg_bone_chain("Skeleton", [("Zero", (0, 0, 1), (0, 0, 1), None)])
except ValueError as exc:
    print("refused")
print("MODE", bpy.context.object.mode if bpy.context.object else "none")
""", out_dir=str(tmp_path))
        assert got["ok"] is True, got.get("error")
        assert "refused" in got["print"]
        assert "MODE OBJECT" in got["print"]


@requires_blender
class TestBoneChainRoll:
    """With every roll at 0 there is no consistent twist axis: both elbows bend
    on whatever fell out of the head/tail direction, and a humanoid retarget
    produces the twisted-forearm look."""

    def test_roll_is_applied_in_degrees(self, tmp_path):
        got = _result(tmp_path, """
import json, math
rig = bg_bone_chain("Skeleton", [
    ("Spine",   (0, 0, 0.9), (0, 0, 1.4), None,    0.0),
    ("Arm.L",   (0.2, 0, 1.4), (0.5, 0, 1.4), "Spine",  90.0),
    ("Arm.R",   (-0.2, 0, 1.4), (-0.5, 0, 1.4), "Spine", -90.0),
])
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode="EDIT")
rolls = {b.name: round(math.degrees(b.roll), 4) for b in rig.data.edit_bones}
bpy.ops.object.mode_set(mode="OBJECT")
print("RESULT " + json.dumps(rolls))
""")
        assert got["Arm.L"] == pytest.approx(90.0, abs=1e-3)
        assert got["Arm.R"] == pytest.approx(-90.0, abs=1e-3)
        assert got["Spine"] == pytest.approx(0.0, abs=1e-3)


# ---------------------------------------------------------------------------
# bg_stats — the numbers a layer cannot be judged without
# ---------------------------------------------------------------------------

@requires_blender
class TestStatsDimensions:
    """A layer is built in its own empty scene. "Is the cap head-sized" is not
    something anyone can see there — it is a number, and until now the stats had
    every number except that one."""

    def test_stats_report_world_size_and_centre(self, tmp_path):
        got = _result(tmp_path, """
import json
slab = bg_box("Slab", size=(2, 1, 0.5), at=(0, 0, 3))
stats = bg_stats(slab)
print("RESULT " + json.dumps({k: (list(v) if isinstance(v, tuple) else v)
                              for k, v in stats.items()}))
""")
        assert got["dims"] == pytest.approx([2.0, 1.0, 0.5], abs=1e-4)
        assert got["centre"] == pytest.approx([0.0, 0.0, 3.0], abs=1e-4)
        assert got["min"] == pytest.approx([-1.0, -0.5, 2.75], abs=1e-4)
        assert got["max"] == pytest.approx([1.0, 0.5, 3.25], abs=1e-4)
        # The old keys are still there — this is an addition, not a swap.
        assert got["verts"] == 8 and got["faces"] == 6
        assert got["loose"] == 0 and got["nonmanifold"] == 0

    def test_size_survives_an_unapplied_scale(self, tmp_path):
        # matrix_world is stale until the depsgraph catches up. Without the
        # view_layer update a scale set two lines earlier reads as 1.0, and
        # every measurement taken during a build is silently wrong.
        got = _result(tmp_path, """
import json
ball = bg_ball("Ball", radius=0.5)
ball.scale = (1, 2, 4)
print("RESULT " + json.dumps(list(bg_stats(ball)["dims"])))
""")
        assert got == pytest.approx([1.0, 2.0, 4.0], abs=0.05)

    def test_an_inverted_face_is_counted(self, tmp_path):
        # An inverted face passes every other check, refuses bone-heat weighting
        # and renders as a hole.
        got = _result(tmp_path, """
import json, bmesh
box = bg_box("Box")
bm = bmesh.new(); bm.from_mesh(box.data); bm.faces.ensure_lookup_table()
bm.faces[0].normal_flip()
bm.to_mesh(box.data); bm.free()
dirty = bg_stats(box)["flipped"]
bg_clean(box)
print("RESULT " + json.dumps({"dirty": dirty, "clean": bg_stats(box)["flipped"]}))
""")
        assert got["dirty"] == 1
        assert got["clean"] == 0, "bg_clean(recalc=True) is the fix"

    def test_a_healthy_mesh_reports_no_flips(self, tmp_path):
        got = _result(tmp_path, """
import json
print("RESULT " + json.dumps([bg_stats(bg_ball("Ball"))["flipped"],
                              bg_stats(bg_plane("Plane"))["flipped"]]))
""")
        assert got == [0, 0]


# ---------------------------------------------------------------------------
# bg_overlap — the check nothing else in the pipeline can make
# ---------------------------------------------------------------------------

@requires_blender
class TestOverlap:
    """Layers are authored in isolated scenes with no shared proportion frame,
    and nothing compares two of them until they are already combined. A cap sunk
    inside a head passes every check there is."""

    def test_two_boxes_that_meet_report_how_much(self, tmp_path):
        got = _result(tmp_path, """
import json
a = bg_box("A", size=(1, 1, 1), at=(0, 0, 0))
b = bg_box("B", size=(1, 1, 1), at=(0.5, 0, 0))
out = bg_overlap(a, b)
out["overlap"] = list(out["overlap"]); out["gap"] = list(out["gap"])
print("RESULT " + json.dumps(out))
""")
        assert got["intersects"] is True
        assert got["overlap"] == pytest.approx([0.5, 1.0, 1.0], abs=1e-4)
        assert got["volume"] == pytest.approx(0.5, abs=1e-4)
        assert got["fraction"] == pytest.approx(0.5, abs=1e-4)
        assert got["inside"] is None

    def test_two_boxes_apart_report_the_gap(self, tmp_path):
        got = _result(tmp_path, """
import json
a = bg_box("A", size=(1, 1, 1), at=(0, 0, 0))
b = bg_box("B", size=(1, 1, 1), at=(5, 0, 0))
out = bg_overlap(a, b)
out["overlap"] = list(out["overlap"]); out["gap"] = list(out["gap"])
print("RESULT " + json.dumps(out))
""")
        assert got["intersects"] is False
        assert got["gap"] == pytest.approx([4.0, 0.0, 0.0], abs=1e-4)
        assert "apart by 4" in got["verdict"]

    def test_a_layer_swallowed_by_another_is_named_as_such(self, tmp_path):
        # THE CAP SUNK INSIDE THE HEAD. It intersects, so "do they touch" says
        # yes; the fraction and the containment flag are what catch it.
        got = _result(tmp_path, """
import json
head = bg_ball("Head", radius=0.5, at=(0, 0, 1.6))
cap = bg_ball("Cap", radius=0.2, at=(0, 0, 1.6))
out = bg_overlap(cap, head)
out["overlap"] = list(out["overlap"]); out["gap"] = list(out["gap"])
print("RESULT " + json.dumps(out))
""")
        assert got["intersects"] is True
        assert got["inside"] == "a"
        assert got["fraction"] == pytest.approx(1.0, abs=1e-3)
        assert "entirely inside" in got["verdict"]

    def test_comparing_parts_after_a_join_says_so(self, tmp_path):
        # bg_join leaves ONE object; the parts are gone. A comparison written
        # after the join compares two ghosts, and reporting a confident zero
        # there is worse than reporting nothing.
        got = _result(tmp_path, """
import json
a = bg_box("A", size=(1, 1, 1), at=(0, 0, 0))
b = bg_box("B", size=(1, 1, 1), at=(0.5, 0, 0))
bg_join([a, b], "Both")
out = bg_overlap(a, b)
print("RESULT " + json.dumps({"intersects": out["intersects"],
                              "verdict": out["verdict"]}))
""")
        assert got["intersects"] is False
        assert "no longer in the scene" in got["verdict"]


# ---------------------------------------------------------------------------
# The worked example
# ---------------------------------------------------------------------------

@requires_blender
class TestWorkedExample:
    """The example is the only end-to-end reference an agent has. A reference
    script that no longer runs is worse than none — it is imitated anyway."""

    def test_the_example_runs_and_builds_what_it_claims(self, tmp_path):
        got = _result(tmp_path, """
import json
exec(compile(BG_EXAMPLE, "<BG_EXAMPLE>", "exec"), globals())
rig = bpy.data.objects["Skeleton"]
body = bpy.data.objects["Body"]
print("RESULT " + json.dumps({
    "parents": {b.name: (b.parent.name if b.parent else None)
                for b in rig.data.bones},
    "stats": {k: (list(v) if isinstance(v, tuple) else v)
              for k, v in bg_stats(body).items()},
    "uv_layers": len(body.data.uv_layers),
    "materials": [m.name for m in body.data.materials],
}))
""")
        parents = got["parents"]
        assert [name for name, p in parents.items() if p is None] == ["Hips"]
        assert parents["Head"] == "Neck" and parents["Hand.L"] == "LowerArm.L"
        assert parents["Foot.R"] == "LowerLeg.R"
        # Both sides exist, from one authored side plus a mirror.
        assert parents["UpperArm.L"] == "Shoulder.L"
        assert parents["UpperArm.R"] == "Shoulder.R"

    def test_the_example_produces_a_weightable_mesh_at_human_scale(self, tmp_path):
        got = _result(tmp_path, """
import json
exec(compile(BG_EXAMPLE, "<BG_EXAMPLE>", "exec"), globals())
body = bpy.data.objects["Body"]
print("RESULT " + json.dumps({
    "stats": {k: (list(v) if isinstance(v, tuple) else v)
              for k, v in bg_stats(body).items()},
    "uv_layers": len(body.data.uv_layers),
    "materials": [m.name for m in body.data.materials],
}))
""")
        stats = got["stats"]
        # 1.75 m tall, standing on the ground, and none of the geometry that
        # makes automatic weighting fail silently.
        assert stats["dims"][2] == pytest.approx(1.75, abs=0.12)
        assert stats["min"][2] == pytest.approx(0.0, abs=0.01)
        assert stats["loose"] == 0
        assert stats["flipped"] == 0
        # bg_finish ran last and owed the pipeline all four.
        assert got["uv_layers"] > 0
        assert got["materials"] == ["skin"]
