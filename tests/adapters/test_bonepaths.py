"""Forward kinematics off a GLB — the step that lets a foot be measured at all.

The linear algebra is tested directly, because that is where a wrong answer is
silent: a lerped quaternion still looks like a rotation and a transposed matrix
still looks like a position. The hierarchy walk gets one round-trip against a
hand-built binary file, the same way animcurves tests its parser.
"""
from __future__ import annotations

import json
import math
import struct

import pytest

from bgate_adapters import bonepaths as bp


def _quat_z(degrees: float):
    a = math.radians(degrees) / 2
    return (0.0, 0.0, math.sin(a), math.cos(a))


# ---------------------------------------------------------------------------
# Interpolation
# ---------------------------------------------------------------------------

def test_slerp_midpoint_is_the_halfway_rotation_and_stays_on_the_sphere():
    """A component-wise lerp gets neither of those right, and both failures
    are invisible in a still frame."""
    got = bp._quat_slerp(_quat_z(0), _quat_z(90), 0.5)
    assert got == pytest.approx(_quat_z(45), abs=1e-9)
    assert math.sqrt(sum(c * c for c in got)) == pytest.approx(1.0, abs=1e-12)


def test_slerp_takes_the_short_way_round_a_sign_flip():
    """q and -q are one rotation. Interpolating toward the far representation
    spins a joint 350 degrees to reach a pose 10 degrees away."""
    near = _quat_z(10)
    far = tuple(-c for c in near)
    for target in (near, far):
        got = bp._quat_slerp(_quat_z(0), target, 0.5)
        assert abs(got[2]) == pytest.approx(abs(_quat_z(5)[2]), abs=1e-9)


def test_step_interpolation_holds_the_previous_key():
    times = [0.0, 1.0]
    values = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)]
    assert bp.sample_channel(times, values, "STEP", 0.99)[0] == 0.0
    assert bp.sample_channel(times, values, "STEP", 1.0)[0] == 10.0


def test_linear_translation_interpolates_and_endpoints_hold():
    times = [0.0, 1.0]
    values = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)]
    assert bp.sample_channel(times, values, "LINEAR", 0.25)[0] == pytest.approx(2.5)
    assert bp.sample_channel(times, values, "LINEAR", -5.0)[0] == 0.0
    assert bp.sample_channel(times, values, "LINEAR", 99.0)[0] == 10.0


def test_a_rotation_channel_is_slerped_not_lerped():
    times = [0.0, 1.0]
    values = [_quat_z(0), _quat_z(90)]
    slerped = bp.sample_channel(times, values, "LINEAR", 0.5, rotation=True)
    lerped = tuple((a + b) / 2 for a, b in zip(*values))
    assert slerped == pytest.approx(_quat_z(45), abs=1e-9)
    assert slerped != pytest.approx(lerped, abs=1e-6)


# ---------------------------------------------------------------------------
# The transform stack
# ---------------------------------------------------------------------------

def test_a_rotated_parent_carries_its_child():
    """The whole point of FK: a child with a constant local translation moves
    because its parent turned. This is the case a local-channel reader sees as
    a bone that never moved."""
    parent = bp._trs_matrix((0.0, 0.0, 0.0), _quat_z(90), (1.0, 1.0, 1.0))
    child = bp._trs_matrix((1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
    world = bp._mat_mul(parent, child)
    assert (world[0][3], world[1][3]) == pytest.approx((0.0, 1.0), abs=1e-9)


def test_scale_on_a_parent_reaches_the_child():
    parent = bp._trs_matrix((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (2.0, 2.0, 2.0))
    child = bp._trs_matrix((3.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
    world = bp._mat_mul(parent, child)
    assert world[0][3] == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# The evaluator, against a hand-built GLB
# ---------------------------------------------------------------------------

def _build_skeleton_glb(tmp_path, *, animated=True):
    """Two joints and a one-triangle mesh. The root spins 90 degrees about Z
    over one second; the child sits one unit out along X with no channel of
    its own — so its motion exists only through its parent."""
    times = [0.0, 1.0]
    rots = [_quat_z(0), _quat_z(90)]
    times_bytes = struct.pack("<2f", *times)
    rot_bytes = b"".join(struct.pack("<4f", *q) for q in rots)
    verts = [(0.0, 0.0, 0.0), (0.0, 2.0, 0.0), (1.0, 0.0, 0.0)]
    vert_bytes = b"".join(struct.pack("<3f", *v) for v in verts)
    bin_data = times_bytes + rot_bytes + vert_bytes

    views, offset = [], 0
    for payload in (times_bytes, rot_bytes, vert_bytes):
        views.append({"buffer": 0, "byteOffset": offset,
                      "byteLength": len(payload)})
        offset += len(payload)

    gltf = {
        "asset": {"version": "2.0"},
        "scenes": [{"nodes": [0]}],
        "nodes": [
            {"name": "RootJoint", "children": [1],
             "translation": [0.0, 0.0, 0.0], "rotation": list(_quat_z(0))},
            {"name": "TipJoint", "translation": [1.0, 0.0, 0.0]},
        ],
        "meshes": [{"name": "body", "primitives": [{"attributes": {"POSITION": 2}}]}],
        "skins": [{"joints": [0, 1]}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 2, "type": "SCALAR"},
            {"bufferView": 1, "componentType": 5126, "count": 2, "type": "VEC4"},
            {"bufferView": 2, "componentType": 5126, "count": 3, "type": "VEC3",
             "min": [0.0, 0.0, 0.0], "max": [1.0, 2.0, 0.0]},
        ],
        "bufferViews": views,
        "buffers": [{"byteLength": len(bin_data)}],
    }
    if animated:
        gltf["animations"] = [{
            "name": "spin",
            "samplers": [{"input": 0, "output": 1, "interpolation": "LINEAR"}],
            "channels": [{"sampler": 0, "target": {"node": 0, "path": "rotation"}}],
        }]

    json_bytes = json.dumps(gltf).encode("utf-8")
    json_bytes += b" " * ((-len(json_bytes)) % 4)
    bin_padded = bin_data + b"\x00" * ((-len(bin_data)) % 4)
    json_chunk = struct.pack("<I4s", len(json_bytes), b"JSON") + json_bytes
    bin_chunk = struct.pack("<I4s", len(bin_padded), b"BIN\x00") + bin_padded
    total = 12 + len(json_chunk) + len(bin_chunk)
    path = tmp_path / ("spin.glb" if animated else "still.glb")
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, total)
                     + json_chunk + bin_chunk)
    return path


def test_joint_paths_traces_a_child_through_its_parents_rotation(tmp_path):
    """THE ENTIRE REASON THIS MODULE EXISTS. TipJoint carries one constant
    translation and no animation channel of its own, which is exactly what a
    foot bone looks like — and it travels a quarter circle."""
    result = bp.joint_paths(_build_skeleton_glb(tmp_path))
    assert result["ok"] is True
    clip = result["clips"][0]
    assert clip["name"] == "spin"
    tip = clip["positions"]["TipJoint"]
    assert tip[0] == pytest.approx((1.0, 0.0, 0.0), abs=1e-6)
    assert tip[-1] == pytest.approx((0.0, 1.0, 0.0), abs=1e-6)
    # the root itself never leaves the origin, so this is not root motion
    assert clip["root_motion"] is False


def test_model_height_comes_off_the_accessor_bounds(tmp_path):
    result = bp.joint_paths(_build_skeleton_glb(tmp_path))
    assert result["model_height"] == pytest.approx(2.0)


def test_a_file_with_no_animation_is_unmeasured_not_clean(tmp_path):
    result = bp.joint_paths(_build_skeleton_glb(tmp_path, animated=False))
    assert result["ok"] is False
    assert "no animations" in result["reason"]


def test_a_missing_file_refuses(tmp_path):
    assert bp.joint_paths(tmp_path / "nope.glb")["ok"] is False


# ---------------------------------------------------------------------------
# Contact
# ---------------------------------------------------------------------------

def _planted(n, lift, *, step=0.05):
    """A foot receding steadily at ground level, lifting by `lift` at the end."""
    out = []
    for i in range(n):
        y = 0.0 if i < n - 2 else lift
        out.append((-i * step, y, 0.0))
    return out


def test_a_foot_that_never_lifts_is_planted_not_airborne():
    """A standing idle whose feet move a tenth of a millimetre gave a contact
    band of 0.025 mm, and 79% of its frames read as FLIGHT. The band needs a
    floor in model height or it collapses onto the noise."""
    times = [i / 24 for i in range(91)]
    foot = [(0.0, 0.0001 * (i % 2), 0.0) for i in range(91)]
    without = bp.support_phases({"L": foot, "R": foot}, times)
    with_height = bp.support_phases({"L": foot, "R": foot}, times,
                                    model_height=1.75)
    assert without["flight_fraction"] > 0.3      # the collapse this guards
    assert with_height["flight_fraction"] == 0.0


def test_the_relative_band_still_serves_a_small_character():
    """A cat's paw lifts 1.4 cm where a human foot lifts 7.0 cm, so the floor
    must not swallow the small one: 1% of a 27 cm cat is 2.7 mm."""
    times = [i / 24 for i in range(24)]
    paw = [(0.0, 0.014 if 8 <= i < 16 else 0.0, 0.0) for i in range(24)]
    phases = bp.support_phases({"paw": paw}, times, model_height=0.2717)
    assert phases["flight_frames"] == 8


def test_support_refuses_to_judge_an_undeclared_gait():
    """The same flight fraction is correct for a run and impossible for a
    walk. Guessing from a clip name would be quietly wrong on any project
    that names its clips differently."""
    times = [i / 24 for i in range(24)]
    foot = [(0.0, 0.1 if i % 2 else 0.0, 0.0) for i in range(24)]
    phases = bp.support_phases({"L": foot}, times, model_height=1.75)
    undeclared = bp.support_verdict(phases)
    assert undeclared["passed"] is False
    assert undeclared["issues"][0]["kind"] == "unmeasured"
    assert bp.support_verdict(phases, "run")["passed"] is True
    assert bp.support_verdict(phases, "walk")["passed"] is False


def test_an_unknown_gait_name_refuses_rather_than_passing():
    times = [i / 24 for i in range(24)]
    foot = [(0.0, 0.0, 0.0) for _ in range(24)]
    phases = bp.support_phases({"L": foot}, times, model_height=1.75)
    assert bp.support_verdict(phases, "moonwalk")["passed"] is False


def test_a_run_with_no_flight_phase_is_a_fast_walk():
    times = [i / 24 for i in range(24)]
    foot = [(0.0, 0.0, 0.0) for _ in range(24)]
    phases = bp.support_phases({"L": foot}, times, model_height=1.75)
    verdict = bp.support_verdict(phases, "run")
    assert verdict["passed"] is False
    assert verdict["issues"][0]["kind"] == "no_flight"


def test_a_planted_foot_sliding_on_a_root_motion_clip_is_skate():
    times = [i / 24 for i in range(20)]
    foot = _planted(20, 0.5, step=0.05)
    result = bp.contact_slide(foot, times, root_motion=True, model_height=1.75)
    verdict = bp.contact_slide_verdict(result)
    assert verdict["convention"] == "root_motion"
    assert verdict["passed"] is False
    assert verdict["issues"][0]["kind"] == "foot_skate"


def test_the_same_slide_on_an_in_place_clip_is_the_animation():
    """An in-place walk holds the body still and moves the ground. Its planted
    foot MUST recede — judging it against zero would fail every correct
    locomotion clip in the project."""
    times = [i / 24 for i in range(20)]
    foot = _planted(20, 0.5, step=0.05)
    result = bp.contact_slide(foot, times, root_motion=False, model_height=1.75)
    verdict = bp.contact_slide_verdict(result)
    assert verdict["convention"] == "in_place"
    assert verdict["passed"] is True


def test_an_in_place_foot_that_changes_speed_mid_plant_is_refused():
    times = [i / 24 for i in range(20)]
    positions, x = [], 0.0
    for i in range(20):
        x -= 0.01 if i < 10 else 0.12
        positions.append((x, 0.0 if i < 18 else 0.5, 0.0))
    result = bp.contact_slide(positions, times, root_motion=False,
                              model_height=1.75)
    verdict = bp.contact_slide_verdict(result)
    assert verdict["passed"] is False
    assert verdict["issues"][0]["kind"] == "uneven_plant"


def test_a_foot_that_never_plants_is_unmeasured():
    times = [i / 24 for i in range(20)]
    rising = [(0.0, float(i), 0.0) for i in range(20)]
    result = bp.contact_slide(rising, times, root_motion=True,
                              band_absolute=1e-6)
    assert result["measured"] is False
    assert bp.contact_slide_verdict(result)["passed"] is False


def test_ground_clearance_sees_a_joint_pass_through_the_floor():
    positions = [(0.0, 0.5, 0.0), (0.0, 0.0, 0.0), (0.0, -0.08, 0.0),
                 (0.0, 0.2, 0.0)]
    result = bp.ground_clearance(positions, floor=0.0)
    assert result["frames_below"] == 1
    assert result["worst_penetration"] == pytest.approx(0.08)
    assert bp.ground_clearance_verdict(result)["passed"] is False
    assert bp.ground_clearance_verdict(
        bp.ground_clearance(positions))["passed"] is True


def test_touchdown_and_liftoff_are_not_counted_as_uneven_plant():
    """A foot arriving at the ground and a foot leaving it are supposed to
    accelerate. Counting those steps made this measurement mostly a report of
    how wide the contact band was: the same foot scored 0.024 at a tight band
    and 0.556 at a loose one."""
    times = [i / 24 for i in range(14)]
    # frames 0-1 descending fast, 2-11 a steady recede, 12-13 lifting away
    positions = []
    x = 0.0
    for i in range(14):
        if i < 2:
            y, x = 0.004, x - 0.30 / 24
        elif i < 12:
            y, x = 0.0, x - 0.05
        else:
            y, x = 0.05, x - 0.30 / 24
        positions.append((x, y, 0.0))
    result = bp.contact_slide(positions, times, root_motion=False,
                              band_absolute=0.005)
    assert result["measured"] is True
    assert result["plants"] == 1
    assert result["steady_steps"] < result["contact_frames"]
    assert result["variation"] == pytest.approx(0.0, abs=1e-6)
    assert bp.contact_slide_verdict(result)["passed"] is True


def test_a_plant_too_short_to_have_a_steady_part_is_unmeasured():
    """Two consecutive contact frames are a touchdown and a lift-off with
    nothing between them — there is no steady speed in that to judge."""
    times = [i / 24 for i in range(12)]
    positions = [(-i * 0.05, 0.0 if i in (5, 6) else 0.5, 0.0)
                 for i in range(12)]
    result = bp.contact_slide(positions, times, root_motion=False,
                              band_absolute=0.01)
    assert result["measured"] is False
    assert bp.contact_slide_verdict(result)["passed"] is False
