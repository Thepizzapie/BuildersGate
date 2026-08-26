"""The crease finder, on point clouds built to order.

WHY THIS FILE IS THE POINT OF LIFTING THE MODULE OUT OF THE RIG SCRIPT. Four
variants of this measurement were built against real meshes at 20-60 seconds an
iteration, and each one needed a whole Blender run to say whether a band rule
had helped. Here a body is forty lines of arithmetic and the answer arrives in
a millisecond, so the cases that are awkward to generate — arms welded to the
ribs, a crease that drops out for two bands, a pair of ears eight bands above
the shoulder — can be asked directly instead of hoped for.

These are not a substitute for tests/test_rig_known_figure.py. That one marks
the measurement against a body whose landmarks are known by construction and is
the only ground truth in this work. This one pins the RULES: what counts as a
crease, which direction the arm run climbs, and what happens when there is no
answer to give.
"""
import math

from bgate_adapters import bodymeasure


def column(z0, z1, x0, x1, rows=40, per_row=8, y=0.0):
    """A slab of surface points spanning [x0, x1] over [z0, z1].

    SAMPLE IT FINER THAN A CREASE. `side_creases` splits a band wherever the
    spacing in |x| exceeds ARM_SPLIT, so a slab whose own points are further
    apart than that reads as a row of creases and nothing works. The first
    version of this helper spaced the torso at 0.057 against a 0.035 threshold
    and every band came back shattered — which is exactly the failure a
    decimated mesh produces, and exactly why the real measurement has a floor
    on how many points a segment needs.
    """
    out = []
    for i in range(rows):
        z = z0 + (z1 - z0) * (i + 0.5) / rows
        for j in range(per_row):
            x = x0 + (x1 - x0) * j / max(1, per_row - 1)
            out.append((x, y, z))
    return out


def figure(gap=0.10, arm_top=1.30, arm_bottom=0.60, torso_half=0.20):
    """A body: one torso column, two legs, and an arm each side standing clear.

    The arm's inner edge sits `gap` outboard of the torso, so every band it
    spans is creased. Heights are the owner's, roughly, so the numbers read
    against the same scale as the real measurements.
    """
    points = column(0.0, 1.75, -torso_half, torso_half, rows=70, per_row=24)
    for sign in (-1.0, 1.0):
        points += column(0.0, 0.52, sign * 0.05, sign * 0.16, rows=25)
        inner = sign * (torso_half + gap)
        points += column(arm_bottom, arm_top, inner, sign * 0.45, rows=30,
                         per_row=8)
    return points


def measured(points):
    return bodymeasure.body_landmarks(points)


# ---------------------------------------------------------------------------
# What a crease is
# ---------------------------------------------------------------------------

def test_a_clear_arm_gives_a_shoulder_at_the_top_of_its_crease():
    got = measured(figure())
    for side in ("Left", "Right"):
        assert "shoulder" in got[side], got[side]
        # The joint sits IN the crease: outboard of the torso, inboard of the
        # arm, at the height the crease runs out.
        assert 0.20 < abs(got[side]["shoulder"][0]) < 0.30, got[side]
        assert abs(got[side]["shoulder"][2] - 1.30) < 0.06, got[side]


def test_both_sides_of_a_mirrored_body_measure_the_same():
    got = measured(figure())
    left, right = got["Left"]["shoulder"], got["Right"]["shoulder"]
    assert abs(abs(left[0]) - abs(right[0])) < 1e-9
    assert abs(left[2] - right[2]) < 1e-9


def test_an_arm_welded_to_the_ribs_is_refused_rather_than_guessed():
    """NO GAP, NO CREASE, NO SHOULDER. This is the chibi case and the coat
    case: when the arm's surface never leaves the body's, there is nothing to
    measure, and the honest output is a reason rather than a number."""
    got = measured(figure(gap=0.0))
    for side in ("Left", "Right"):
        assert "shoulder" not in got[side], got[side]
        assert "arm" in got[side]["why"]


def test_a_gap_under_the_split_threshold_is_not_a_crease():
    """ARM_SPLIT is 2% of body height and the floor exists because a decimated
    surface has holes in it. A gap narrower than that is sampling, not
    anatomy, and reading it as a crease is how the arm run picks up noise."""
    narrow = 0.6 * bodymeasure.ARM_SPLIT * 1.75
    got = measured(figure(gap=narrow))
    assert "shoulder" not in got["Left"], got["Left"]


# ---------------------------------------------------------------------------
# Which way the run climbs, and how far it may jump
# ---------------------------------------------------------------------------

def test_the_arm_run_climbs_from_the_tip_and_does_not_walk_into_the_legs():
    """LEGS CREASE TOO. Measured on a real character, a run allowed to walk
    DOWN from the fingertips joined arm to leg, dragged itself from band 21 to
    band 8, and put one shoulder 9 cm from the other. The fingertips are the
    far end of an arm in every stance this rigs, so the run only ever goes up.
    """
    points = figure()
    got = measured(points)
    for side in ("Left", "Right"):
        # The armpit is the bottom of the ARM's own run, near the fingertips,
        # not down among the legs that crease at z < 0.52.
        assert got[side]["armpit_z"] > 0.55, got[side]


def test_a_crease_may_vanish_for_two_bands_but_not_for_eight():
    """The bridge is what survives a welded mesh; its limit is what keeps a
    pair of ears out of the arm. Both halves are asserted here because one
    without the other is either brittle or blind."""
    height, bands = 1.75, bodymeasure.ARM_BANDS
    step = height / bands
    base = figure()

    def arm_with_hole(missing_bands):
        keep = []
        low = 1.30 - step * (missing_bands + 0.5)
        for point in base:
            inside = low <= point[2] < 1.30
            if inside and abs(point[0]) > 0.20:
                continue          # punch the arm out of those bands only
            keep.append(point)
        return keep

    bridged = measured(arm_with_hole(2))
    assert "shoulder" in bridged["Left"], bridged["Left"]

    broken = measured(arm_with_hole(8))
    # The run stops below the hole rather than jumping it, so the shoulder is
    # found lower down the arm — it must not read the far side as the same arm.
    assert "shoulder" not in broken["Left"] or \
        broken["Left"]["shoulder"][2] < 1.30 - step * 6, broken["Left"]


# ---------------------------------------------------------------------------
# The parts that are not the arm
# ---------------------------------------------------------------------------

def test_a_solid_column_has_no_creases_at_all():
    creases, reach = bodymeasure.side_creases(
        column(0.0, 1.75, 0.0, 0.20, rows=70), 1.0, 0.0, 1.75)
    assert creases == {}
    assert reach, "a solid column still has a reach in every band it fills"


def test_the_gross_landmarks_come_straight_off_the_cloud():
    got = measured(figure())
    assert math.isclose(got["floor"], 0.0, abs_tol=0.02)
    assert math.isclose(got["top"], 1.75, abs_tol=0.02)
    assert math.isclose(got["height"], got["top"] - got["floor"])
    assert math.isclose(got["half_width"], 0.45, abs_tol=0.02)


def test_the_feet_and_hip_width_are_measured_per_side():
    got = measured(figure())
    for side, sign in (("Left", -1.0), ("Right", 1.0)):
        assert got[side]["foot"][2] < 0.12, got[side]["foot"]
        assert sign * got[side]["foot"][0] > 0.0, got[side]["foot"]
        assert got[side]["hip_x"] > 0.0


def test_the_module_needs_nothing_but_the_standard_library():
    """IT RUNS IN TWO WORLDS. bgate_adapters.blender splices this file's source
    into a Blender script, and this test process imports it directly; an import
    that only one of them can satisfy breaks the half that cannot."""
    import ast
    import pathlib

    source = pathlib.Path(bodymeasure.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    import sys
    assert imported <= sys.stdlib_module_names, sorted(imported)
