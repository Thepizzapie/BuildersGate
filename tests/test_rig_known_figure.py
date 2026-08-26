"""THE ONLY GROUND TRUTH IN THE RIG-FITTING WORK.

Every other check in this repo measures an unknown mesh and asks whether the
answer looks plausible. That is the same trap as a green gate: a number that is
wrong in a believable way passes it. `bg_human(height, heads)` builds a figure
whose every landmark is known BY CONSTRUCTION — the same `bg_proportions` call
places the shoulder, the waist and the crotch and then builds shells around
them — so the measurement can be handed a body and marked against the answer.

WHAT IT COSTS TO SKIP THIS, and the example is this file's own reason to exist.
The shoulder measurement was first validated by one hand-run at one head count
in one pose. It reported the joint 1.3 cm from the template's own shoulder_z
and read as a solved problem. Both halves of that were wrong. The matrix says
the error in that cell is +3.43% of body height — 6.0 cm — and that a 3-head
chibi is refused outright rather than measured at all.

The 1.3 cm itself was a UNIT ERROR, and it is the kind only a harness catches:
the hand-run let `rig()` take its default height of 1.8 m, so a 1.75 m figure
came back scaled to 1.8 m, and its absolute shoulder z was then compared
against the 1.75 m figure's absolute shoulder z. Two errors in opposite
directions cancelled to something small and believable. Normalised, that same
run is -1.57% of height. Compare fractions of height, never metres, and never
trust one cell.

HOW IT RUNS. One Blender process builds and measures the whole matrix, because
a per-case process turned a four-second measurement into three minutes of
startup. The functions being measured are spliced into that script from
bgate_adapters/bodymeasure.py, and this harness splices the same way — so what
it measures is the bytes production runs, not a copy of them.
"""
import json

import pytest

from bgate_adapters import blender

needs_blender = pytest.mark.skipif(
    not blender.available().get("available"),
    reason="Blender not installed")

MARK = "BGATE_KNOWN:"

# Head counts to cover, and 3.0 is in here on purpose: a chibi's arms never
# leave its body, and a measurement that cannot say so is worse than one that
# refuses. 4.0 stands in for the stylised generated characters this pipeline
# actually produces — the one that started this work measures 4.44.
HEAD_COUNTS = (7.5, 6.0, 5.0, 4.0, 3.0)
HEIGHT = 1.75
DETAIL = 3          # the density at which the error stops moving; see below

# TWO BUILDS, AND THE SECOND ONE IS NOT A NICETY. bg_human at its default build
# has thighs wider than their separation — hip_x is 0.048 of chin height and
# thigh_r is 0.062 — so the figure has no gap between its legs anywhere and no
# crotch can be measured on it at all. A slimmer build opens one. Both cases
# are covered because both must be: the fat one proves the refusal, the slim
# one proves the measurement.
BUILDS = (1.0, 0.6)

_DRIVER = '''
CFG = json.loads(r"""__CFG__""")
rows = []
for heads in CFG["heads"]:
    for build in CFG["builds"]:
        for pose in ("t", "a"):
            for o in list(bpy.context.scene.objects):
                bpy.data.objects.remove(o, do_unlink=True)
            P = bg_proportions(height=CFG["height"], heads=heads, build=build)
            bg_human(height=CFG["height"], heads=heads, build=build, rig=False,
                     name="Known", pose=pose, detail=CFG["detail"], finish=True)
            mesh = max([o for o in bpy.context.scene.objects
                        if o.type == "MESH"],
                       key=lambda o: len(o.data.vertices))
            rows.append({"heads": heads, "build": build, "pose": pose,
                         "verts": len(mesh.data.vertices),
                         "known": {k: P[k] for k in
                                   ("shoulder_z", "shoulder_x", "crotch_z",
                                    "waist_z", "chest_z", "upperchest_z",
                                    "crown", "chin", "body", "upper_arm_r",
                                    "hip_x", "thigh_r", "neck_r")},
                         "measured": landmarks(mesh)})
# TO A FILE, NOT TO STDOUT. run_script hands back the last 4000 characters of
# stdout only, and ten cells of landmarks is several times that — printed, the
# whole matrix comes back as an empty report indistinguishable from a crash.
# _TDEV_SCRIPT solved the same problem by shrinking its report; this one needs
# every row, so it writes them down.
with open(CFG["dump"], "w") as handle:
    json.dump({"ok": True, "rows": rows}, handle)
print("__MARK__" + json.dumps({"ok": True, "cells": len(rows)}))
'''


@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    """Every (head count, pose) cell, measured in one Blender start."""
    dump = tmp_path_factory.mktemp("known") / "matrix.json"
    cfg = {"heads": list(HEAD_COUNTS), "builds": list(BUILDS),
           "height": HEIGHT, "detail": DETAIL,
           "dump": str(dump).replace("\\", "/")}
    script = (blender._rig_script().split("PAY = json.loads(")[0]
              + _DRIVER.replace("__CFG__", json.dumps(cfg))
                       .replace("__MARK__", MARK))
    got = blender.run_script(script, timeout=900, record=False)
    report = blender._marked(got, MARK)
    assert report, (got.get("error"), (got.get("traceback") or "")[-800:])
    assert dump.is_file(), "Blender reported a matrix it did not write"
    rows = json.loads(dump.read_text())["rows"]
    assert len(rows) == len(HEAD_COUNTS) * len(BUILDS) * 2, len(rows)
    return {(r["heads"], r["build"], r["pose"]): r for r in rows}


def _shoulder(cell, side="Left"):
    return (cell["measured"].get(side) or {})


def _err_pct_height(cell, key, side="Left"):
    """Measured minus known, as a percentage of total height."""
    got = _shoulder(cell, side)
    if "shoulder" in got:
        x, _y, z = got["shoulder"]
        value = abs(x) if key == "shoulder_x" else z
        return 100.0 * (value - cell["known"][key]) / HEIGHT
    return None


# ---------------------------------------------------------------------------
# What the measurement is actually worth
# ---------------------------------------------------------------------------

@needs_blender
def test_the_shoulder_is_found_within_a_measured_envelope(matrix):
    """THE ACCURACY OF THIS MEASUREMENT, WRITTEN DOWN RATHER THAN HOPED FOR,
    and widened once already by covering more of the space.

    Error against each figure's own known shoulder_z, as a percentage of the
    1.75 m total height, at detail=3:

        heads  build 1.00 T / A     build 0.60 T / A
         7.5    +2.80  +3.43         +0.68   +1.93
         6.0    +2.70  +3.30         +0.98   +1.98
         5.0    +2.56  -1.34         +1.01  -13.36   <-- outlier
         4.0    +1.06  -1.26         -0.00   +0.55
         3.0    +1.17  refused       +0.11   refused

    ON THE DEFAULT BUILD ALONE THIS READS +-3.5%, AND THAT IS THE NUMBER THIS
    TEST USED TO CARRY. Adding a slimmer build — which had to be added anyway,
    because bg_human at build 1.0 has no crotch gap to measure — turned up a
    cell at -13.36%: a slim 5-head figure with its arms down, where the crease
    run ends early and the joint lands 23 cm low. One cell in twenty, and it
    would never have been found by running more head counts at one build.

    The envelope is therefore -13.4% to +3.4%, not +-3.5%, and anything derived
    from a shoulder inherits that. The trunk hangs off this landmark.
    """
    worst, where = 0.0, None
    for key, cell in matrix.items():
        got = _err_pct_height(cell, "shoulder_z")
        if got is None:
            continue
        if abs(got) > abs(worst):
            worst, where = got, key
    assert where is not None, "every cell refused — nothing was measured"
    assert abs(worst) <= 14.0, (where, worst, "shoulder_z error grew past the "
                                              "envelope this test records")


@needs_blender
def test_the_shoulder_error_is_method_limited_not_sampling_limited(matrix):
    """AND THAT IS WHY 4% IS NOT FIXED BY A DENSER MESH. Measured on the
    7.5-head figure across bg_human's four detail levels — 578, 940, 1350 and
    1808 vertices — the error does not shrink toward zero, it CONVERGES:

        T-pose  +1.47  +2.64  +2.75  +2.80
        A-pose  -8.33  -1.57  +3.37  +3.43

    The coarse meshes are noisy around a value the fine ones agree on, so this
    is bias and not sampling. Where the bias comes from is visible in the
    numbers: on adult proportions it tracks the arm's OWN RADIUS, because the
    crease between arm and body runs out at the top of the arm while the joint
    centre sits a radius below it.

        heads   error   upper_arm_r (both as % of height)
         7.5    +2.80      2.99
         6.0    +2.70      2.88
         5.0    +2.56      2.76

    It stops tracking on stylised figures — 4.0 heads reads +1.06 against 2.59
    — so it is a mechanism, not a constant to subtract. Correcting it is its
    own change and has not been made.
    """
    cell = matrix[(7.5, 1.0, "t")]
    radius_pct = 100.0 * cell["known"]["upper_arm_r"] / HEIGHT
    got = _err_pct_height(cell, "shoulder_z")
    assert got is not None
    # The bias sits within half an arm-radius of the arm radius itself. If this
    # ever fails, the mechanism above is no longer the explanation.
    assert abs(got - radius_pct) <= radius_pct * 0.5, (got, radius_pct)


@needs_blender
def test_both_shoulders_are_measured_identically(matrix):
    """Left and right of a mirror-symmetric figure must not disagree at all.
    The broken version was symmetric too — it put both joints on the bicep —
    so this catches a sloppy measurement, never a wrong one."""
    for key, cell in matrix.items():
        left, right = _shoulder(cell, "Left"), _shoulder(cell, "Right")
        if "shoulder" not in left or "shoulder" not in right:
            assert "shoulder" not in left and "shoulder" not in right, key
            continue
        assert abs(abs(left["shoulder"][0]) - abs(right["shoulder"][0])) < 1e-4, key
        assert abs(left["shoulder"][2] - right["shoulder"][2]) < 1e-4, key


@needs_blender
def test_a_chibi_in_an_a_pose_is_refused_rather_than_guessed(matrix):
    """THE REFUSAL PATH, ON A REAL BODY RATHER THAN IN PRINCIPLE. A 3-head
    figure with its arms down has no gap anywhere between arm and torso, so
    there is no crease to find and no shoulder to report. `why` says that, and
    no `shoulder` key is emitted for anything downstream to trust."""
    cell = matrix[(3.0, 1.0, "a")]
    for side in ("Left", "Right"):
        got = _shoulder(cell, side)
        assert "shoulder" not in got, got
        assert got.get("why"), got


@needs_blender
def test_the_crotch_is_found_wherever_the_legs_are_actually_apart(matrix):
    """MEASURED AGAINST A KNOWN CROTCH, on the builds where one exists to find.
    Error as a percentage of total height, and it is the same in both stances
    because a crotch does not care what the arms are doing:

        heads   build 0.60
         7.5      -3.91
         6.0      -3.55
         5.0      -3.19
         4.0      -3.43
         3.0     -11.12   <-- the chibi

    ALWAYS NEGATIVE. The measurement finds where the midline FILLS, and the
    inner thighs meet below the joint they hang from, so it reads low by about
    a thigh's radius every time — 3.2% of height against a thigh_r of 3.2% at
    build 0.6. That is the same shape of bias the shoulder has, one landmark
    down, and correcting it is deliberately not done here: see the trunk report.

    The chibi is the outlier and its number is honest. A 3-head figure's legs
    are a fifth of its height, so a thigh radius is a much larger share of the
    distance being measured.
    """
    checked, worst = 0, 0.0
    for key, cell in matrix.items():
        crotch = (cell["measured"].get("trunk") or {}).get("crotch") or {}
        if not crotch.get("measured"):
            continue
        checked += 1
        err = 100.0 * (crotch["value"] - cell["known"]["crotch_z"]) / HEIGHT
        assert err < 0, (key, err, "a crotch read ABOVE the known one — the "
                                   "thigh-radius bias explains a low reading, "
                                   "nothing explains a high one")
        worst = max(worst, abs(err))
    assert checked >= len(HEAD_COUNTS), (checked, "the slim build should give "
                                                  "a crotch at every head count")
    assert worst <= 12.0, worst


@needs_blender
def test_a_figure_whose_thighs_touch_is_refused_a_crotch(matrix):
    """bg_human AT ITS OWN DEFAULT BUILD HAS NO CROTCH GAP. hip_x is 0.048 of
    chin height and thigh_r is 0.062, so the thighs overlap across the midline
    and the legs are one solid from the ankles up. The measurement declines
    rather than reporting the height at which the calves merge, which is what
    it would otherwise find — measured, 13% of body height, a knee.

    This is not a quirk of the template. A skirt, a robe or a long coat does
    exactly the same thing to a generated character.
    """
    for heads in HEAD_COUNTS:
        cell = matrix[(heads, 1.0, "t")]
        crotch = (cell["measured"].get("trunk") or {}).get("crotch") or {}
        assert crotch.get("measured") is False, (heads, crotch)
        assert crotch.get("why"), heads


@needs_blender
def test_the_neck_is_found_within_a_hair_of_the_chin(matrix):
    """AND THE CHIN IS THE ONLY KNOWN LANDMARK IT CAN BE MARKED AGAINST, because
    bg_human's neck is a cylinder hidden under the arms in a T-pose — the arm
    spans the shoulder line and swamps the neck's own width. What the
    measurement finds is the base of the head shell, and it tracks the chin
    closely, in percent of total height:

        heads   build 1.00   build 0.60
         7.5      +1.61        +0.05
         6.0      +1.82        +0.26
         5.0      +2.03        +0.47
         4.0      +0.78        +0.78
         3.0      +1.30        -0.26

    NOT THE RADIUS SIGNATURE THE SHOULDER AND THE CROTCH HAVE, and that
    question was asked on purpose. neck_r falls from 3.94% of height to 3.03%
    across those head counts while the error wanders between -0.26 and +2.03
    without following it, and it halves when the build slims while neck_r
    halves too — the opposite of a one-radius offset. Whatever this is, it is
    not one body radius, so the correction that would pay twice for the
    shoulder and the crotch does not pay a third time here.
    """
    checked, worst = 0, 0.0
    for key, cell in matrix.items():
        neck = (cell["measured"].get("trunk") or {}).get("neck_base") or {}
        if not neck.get("measured"):
            continue
        checked += 1
        err = 100.0 * (neck["value"] - cell["known"]["chin"]) / HEIGHT
        worst = max(worst, abs(err))
    assert checked >= 8, checked
    assert worst <= 3.0, worst


@needs_blender
def test_a_trunk_with_no_bottom_anchor_is_not_fitted(matrix):
    """THE REFUSAL HAS TO REACH THE DECISION, not just the report. When the
    crotch cannot be found there is no span to hang a spine in, so `fitted` is
    False and fit_trunk leaves every trunk bone where the template put it — and
    says TRUNK ASSUMED, because a caller that cannot tell a measured spine from
    an inherited one is the disease this whole sequence has been treating.
    """
    for heads in HEAD_COUNTS:
        trunk = matrix[(heads, 1.0, "t")]["measured"].get("trunk") or {}
        assert trunk.get("fitted") is False, (heads, trunk)
    slim = matrix[(7.5, 0.6, "t")]["measured"].get("trunk") or {}
    assert slim.get("fitted") is True, slim


@needs_blender
def test_the_landmarks_the_trunk_still_does_not_measure(matrix):
    """WHAT IS STILL ASSUMED, kept as a fact in the report rather than an
    absence from it. The spine is now hung between a measured crotch and a
    measured shoulder, but the four bones BETWEEN them — Hips, Spine, Chest,
    UpperChest — are still placed by the template's proportions within that
    span. Nothing here measures a waist or a chest, because on an A-posed mesh
    the arms touch the ribs and no cross-section can separate them.

    This fails the day one of them is measured without its own row being added
    to the envelope above.
    """
    trunk = matrix[(7.5, 0.6, "t")]["measured"]["trunk"]
    assert set(trunk) >= {"crotch", "crown", "shoulder_line", "neck_base",
                          "fitted"}
    for name in ("waist", "chest", "upperchest"):
        assert name not in trunk, (name, "measured now — extend the envelope")
