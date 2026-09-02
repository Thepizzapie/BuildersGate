"""The gate judges a character against ITS OWN proportions — and must still bite.

THE DECISION THIS TESTS. template_deviation used to compare every character
against one shipped 7.5-head skeleton, so a stylised figure failed for being
stylised. A gate that fires on style gets switched off inside a week, which is
worse than never having shipped it, so the reference is now built at the
candidate's own height, head count and limb ratio.

AND THAT IS EXACTLY THE CHANGE THAT COULD MAKE THE GATE WORTHLESS. A reference
adapted to the candidate is one step from a reference fitted to the candidate,
and a parameter chosen to minimise deviation is a gate that passes everything
while looking like success. Two properties keep it honest, and both are tested
here rather than argued:

  DERIVED, NEVER SOLVED. Every axis comes from a landmark measured off the
  MESH — crown, neck base, crotch — and never from whatever value would make a
  row pass.

  STRUCTURALLY, NOT CAREFULLY. The axes come from the mesh; the comparison is
  of BONES. Breaking a skeleton therefore cannot move the reference it is
  judged against, and the test below proves that by breaking one and checking
  the derived numbers do not so much as flinch.
"""
import json

import pytest

from bgate_adapters import blender

needs_blender = pytest.mark.skipif(
    not blender.available().get("available"),
    reason="Blender not installed")

HEIGHT = 1.75
BREAK_FRACTION = 0.20       # of body height, the damage the gate must still see

_BUILD = '''
CFG = json.loads(r"""__CFG__""")
for o in list(bpy.context.scene.objects):
    bpy.data.objects.remove(o, do_unlink=True)
bg_human(height=CFG["height"], heads=CFG["heads"], limbs=CFG["limbs"],
         build=CFG["build"], rig=False, name="Subject", pose="a", detail=3,
         finish=True)
'''

_BREAK = '''
CFG = json.loads(r"""__CFG__""")
for o in list(bpy.context.scene.objects):
    bpy.data.objects.remove(o, do_unlink=True)
bpy.ops.import_scene.gltf(filepath=CFG["model"])
arm = next(o for o in bpy.context.scene.objects if o.type == "ARMATURE")
bpy.context.view_layer.objects.active = arm
bpy.ops.object.mode_set(mode="EDIT")
bone = arm.data.edit_bones[CFG["bone"]]
drop = CFG["height"] * CFG["fraction"]
# MOVE THE JOINT, WHICH SHORTENS THE BONE ABOVE IT AND LENGTHENS THE ONE BELOW.
# Dragging a whole bone would move its children with it and change no length at
# all, which is a rig that is wrong in a way this gate is not for.
bone.head = bone.head + Vector((0.0, 0.0, -drop))
bpy.ops.object.mode_set(mode="OBJECT")
'''


PRELUDE = "import json\nfrom mathutils import Vector\n"


def _run(script, cfg, out):
    got = blender.run_script(
        PRELUDE + script.replace("__CFG__", json.dumps(cfg)),
        export_glb=str(out), timeout=900, record=False)
    assert got.get("ok"), (got.get("error"),
                           (got.get("traceback") or "")[-600:])
    return got


@pytest.fixture(scope="module")
def rigged(tmp_path_factory):
    """A stylised figure — 5 heads, short legs — taken through the real rig().

    BUILT SLIM ON PURPOSE, AND THE REASON IS A LIMITATION WORTH KNOWING. At
    bg_human's default build the thighs are wider than their separation
    (hip_x 0.048 of chin height against thigh_r 0.062), so the figure has no
    gap between its legs, no crotch can be measured, `limbs` cannot be derived
    — and template_deviation now REFUSES the whole comparison rather than
    quietly falling back to judging it as an adult. Correct behaviour, and it
    means a fat or skirted character gets no proportional check at all unless
    the caller passes an explicit `reference=` file.
    """
    work = tmp_path_factory.mktemp("canon")
    raw = work / "subject.glb"
    _run(_BUILD, {"height": HEIGHT, "heads": 5.0, "limbs": 0.8,
                  "build": 0.6}, raw)
    assert raw.is_file(), "the subject figure did not export"
    out = work / "subject_rigged.glb"
    report = blender.rig(str(raw), str(out), kind="humanoid", height=HEIGHT)
    assert report.get("ok"), report.get("error")
    return work, out


@pytest.fixture(scope="module")
def broken(rigged):
    """The same rig with one joint dragged 20% of body height out of place."""
    work, good = rigged
    bad = work / "subject_broken.glb"
    _run(_BREAK, {"model": str(good).replace("\\", "/"),
                  "bone": "LeftLowerArm", "height": HEIGHT,
                  "fraction": BREAK_FRACTION}, bad)
    assert bad.is_file(), "the broken rig did not export"
    return bad


# ---------------------------------------------------------------------------
# Derived, never solved
# ---------------------------------------------------------------------------

@needs_blender
def test_the_axes_come_from_the_mesh_and_name_what_they_came_from(rigged):
    """THREE AXES FROM THREE INDEPENDENT LANDMARKS, and the report says which.

    height from crown-minus-floor, heads from neck-base-to-crown, limbs from
    the measured crotch against the canon's own leg arithmetic. `build` and
    `shoulders` are held at the canon's defaults and say why in their own rows:
    build changes no length this gate compares, and shoulders changes only the
    single row it would have been fitted to, which is solving.
    """
    _work, out = rigged
    report = blender.template_deviation(str(out))
    assert report.get("ok"), report.get("error")
    axes = report["axes"]
    assert axes["derived"] == 3, axes
    assert axes["from_measurements"] >= 3, axes
    for axis in ("height", "heads", "limbs"):
        assert axes[axis]["measured"] is True, (axis, axes[axis])
        assert axes[axis]["from"], axis
    for axis in ("build", "shoulders"):
        assert axes[axis]["measured"] is False
        assert "NOT derived" in axes[axis]["from"]


@needs_blender
def test_a_stylised_figure_is_judged_at_its_own_head_count(rigged):
    """THE POINT OF THE CHANGE. A 5-head figure is an art choice, and against
    the shipped 7.5-head skeleton it failed for being one."""
    _work, out = rigged
    report = blender.template_deviation(str(out))
    axes = report["axes"]
    assert 4.0 < axes["heads"]["value"] < 6.5, axes["heads"]
    assert axes["limbs"]["value"] < 1.0, axes["limbs"]
    assert report["reference_from"] == "canon"


@needs_blender
def test_a_pin_beats_the_measurement(rigged):
    """A project whose character IS adult canon says so and gets the strict
    comparison. The default is to measure; the pin is the override, never the
    other way round."""
    _work, out = rigged
    pinned = blender.template_deviation(str(out), heads=7.5)
    assert pinned["axes"]["heads"]["value"] == 7.5
    assert pinned["axes"]["heads"]["measured"] is False
    assert "pinned" in pinned["axes"]["heads"]["from"]
    loose = blender.template_deviation(str(out))
    assert loose["axes"]["heads"]["value"] != 7.5


# ---------------------------------------------------------------------------
# The half that matters: it must still bite
# ---------------------------------------------------------------------------

@needs_blender
def test_breaking_a_joint_does_not_move_the_derived_axes(rigged, broken):
    """THE SAFETY PROPERTY, PROVEN RATHER THAN ARGUED. The axes are derived
    from the mesh and the comparison is of bones, so damage to a skeleton
    cannot talk the gate into calling it a different body.

    THE TOLERANCE IS float32's, NOT A FUDGE. glTF stores positions as
    single-precision, so writing the broken rig back out and reading it in
    again moves every vertex in its last bit and `height` lands 4e-8 from where
    it started. A relative 1e-6 sits an order of magnitude above that and many
    orders below anything a landmark could mean.
    """
    _work, good = rigged
    before = blender.template_deviation(str(good))["axes"]
    after = blender.template_deviation(str(broken))["axes"]
    for axis in ("height", "heads", "limbs"):
        one, two = before[axis]["value"], after[axis]["value"]
        assert abs(one - two) <= abs(one) * 1e-6, (axis, before[axis],
                                                   after[axis])


@needs_blender
def test_a_rig_broken_by_twenty_percent_of_body_height_still_fails(rigged,
                                                                   broken):
    """AND IF THIS EVER PASSES, THE GATE IS WORTHLESS AND THIS IS THE ONLY
    THING THAT WILL SAY SO.

    One joint dragged 20% of body height down its own chain. The whole pipeline
    runs on the damaged rig, parameter derivation included — nothing here
    reaches around the adaptation to make the failure happen.
    """
    _work, good = rigged
    healthy = blender.template_deviation_verdict(
        blender.template_deviation(str(good)))
    damaged = blender.template_deviation_verdict(
        blender.template_deviation(str(broken)))
    assert damaged["passed"] is False, damaged
    hurt = {name for name, dev in damaged["deviations"].items()
            if dev > damaged["threshold"]}
    assert "LeftUpperArm" in hurt or "LeftLowerArm" in hurt, hurt
    # The damage must show up as MORE than the healthy rig's own quarrel with
    # the canon, or "it failed" would mean nothing.
    for name in ("LeftUpperArm", "LeftLowerArm"):
        assert (damaged["deviations"][name]
                > healthy["deviations"][name] + 0.05), (
            name, healthy["deviations"][name], damaged["deviations"][name])


@needs_blender
def test_the_undamaged_side_is_not_dragged_down_with_it(rigged, broken):
    """A gate that reddens every row when one bone moves cannot be used to
    find the bone. The right arm is untouched and must read untouched."""
    _work, good = rigged
    healthy = blender.template_deviation_verdict(
        blender.template_deviation(str(good)))
    damaged = blender.template_deviation_verdict(
        blender.template_deviation(str(broken)))
    for name in ("RightUpperArm", "RightLowerArm", "RightUpperLeg"):
        assert abs(damaged["deviations"][name]
                   - healthy["deviations"][name]) < 1e-6, name
