"""Animation-curve metrics, tested on synthetic data — no Blender/Godot needed.

The metrics operate on plain time/value arrays, so the interesting behaviour
(does an eased ramp read differently from a linear one, does a curved path
read differently from a straight one) is fully testable without ever
spawning an engine. The GLB parser gets its own round-trip test against a
hand-built binary file, since it is this module's one piece of code that
touches a real byte format rather than pure arithmetic.
"""
from __future__ import annotations

import json
import math
import struct

import pytest

from bgate_adapters import animcurves as ac


# ---------------------------------------------------------------------------
# The GLB parser, against a hand-built file
# ---------------------------------------------------------------------------

def _build_glb(tmp_path):
    """A minimal GLB: one node, one animation, one VEC3 translation channel."""
    times = [0.0, 0.5, 1.0]
    values = [(0.0, 0.0, 0.0), (1.0, 2.0, 0.0), (2.0, 0.0, 0.0)]
    times_bytes = struct.pack("<3f", *times)
    values_bytes = b"".join(struct.pack("<3f", *v) for v in values)
    bin_data = times_bytes + values_bytes
    gltf = {
        "asset": {"version": "2.0"},
        "nodes": [{"name": "Hand.L"}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 3, "type": "SCALAR"},
            {"bufferView": 1, "componentType": 5126, "count": 3, "type": "VEC3"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(times_bytes)},
            {"buffer": 0, "byteOffset": len(times_bytes), "byteLength": len(values_bytes)},
        ],
        "buffers": [{"byteLength": len(bin_data)}],
        "animations": [{
            "name": "wave",
            "samplers": [{"input": 0, "output": 1, "interpolation": "LINEAR"}],
            "channels": [{"sampler": 0, "target": {"node": 0, "path": "translation"}}],
        }],
    }
    json_bytes = json.dumps(gltf).encode("utf-8")
    json_pad = (-len(json_bytes)) % 4
    json_bytes += b" " * json_pad
    bin_pad = (-len(bin_data)) % 4
    bin_padded = bin_data + b"\x00" * bin_pad

    header = struct.pack("<4sII", b"glTF", 2, 0)  # length patched below
    json_chunk = struct.pack("<I4s", len(json_bytes), b"JSON") + json_bytes
    bin_chunk = struct.pack("<I4s", len(bin_padded), b"BIN\x00") + bin_padded
    total_len = 12 + len(json_chunk) + len(bin_chunk)
    header = struct.pack("<4sII", b"glTF", 2, total_len)

    path = tmp_path / "wave.glb"
    path.write_bytes(header + json_chunk + bin_chunk)
    return path


def test_extract_animations_round_trips_a_hand_built_glb(tmp_path):
    path = _build_glb(tmp_path)
    got = ac.extract_animations(path)
    assert got["ok"] is True
    anim = got["animations"][0]
    assert anim["name"] == "wave"
    channel = anim["channels"][0]
    assert channel["node"] == "Hand.L"
    assert channel["path"] == "translation"
    assert channel["times"] == [0.0, 0.5, 1.0]
    assert channel["values"][1] == (1.0, 2.0, 0.0)


def test_extract_animations_reports_a_missing_file(tmp_path):
    got = ac.extract_animations(tmp_path / "nope.glb")
    assert got["ok"] is False


# ---------------------------------------------------------------------------
# Arc deviation
# ---------------------------------------------------------------------------

def test_arc_deviation_is_near_zero_on_a_straight_path():
    positions = [(t, 0.0, 0.0) for t in (0.0, 0.5, 1.0, 1.5, 2.0)]
    result = ac.arc_deviation(list(range(5)), positions)
    assert result["deviation"] < 0.01


def test_arc_deviation_is_positive_on_a_bowed_path():
    n = 9
    positions = [(t / (n - 1), math.sin(math.pi * t / (n - 1)), 0.0)
                for t in range(n)]
    result = ac.arc_deviation(list(range(n)), positions)
    assert result["deviation"] > 0.15


# ---------------------------------------------------------------------------
# Velocity profile: linear vs eased
# ---------------------------------------------------------------------------

def test_velocity_profile_flags_constant_speed_motion():
    times = [i * 0.1 for i in range(20)]
    values = [t * 2.0 for t in times]          # constant slope end to end
    profile = ac.velocity_profile(times, values)
    verdict = ac.velocity_profile_verdict(profile)
    assert profile["cruising_fraction"] > 0.6
    assert verdict["passed"] is False


def test_velocity_profile_passes_an_eased_ramp():
    times = [i * 0.05 for i in range(41)]
    # smoothstep: zero velocity at both ends, peak in the middle.
    values = [3 * (t / 2.0) ** 2 - 2 * (t / 2.0) ** 3 for t in times]
    profile = ac.velocity_profile(times, values)
    verdict = ac.velocity_profile_verdict(profile)
    assert verdict["passed"] is True


# ---------------------------------------------------------------------------
# SPARC jitter
# ---------------------------------------------------------------------------

def test_sparc_reads_a_smooth_sine_as_smooth():
    times = [i * 0.02 for i in range(60)]
    values = [(math.sin(2 * math.pi * 0.5 * t), 0.0, 0.0) for t in times]
    result = ac.sparc(times, values)
    verdict = ac.sparc_verdict(result)
    assert verdict["passed"] is True, result


def test_sparc_reads_alternating_noise_as_rough():
    times = [i * 0.02 for i in range(60)]
    values = [((1.0 if i % 2 == 0 else -1.0), 0.0, 0.0) for i in range(60)]
    result = ac.sparc(times, values)
    verdict = ac.sparc_verdict(result)
    assert result["sparc"] < -8.0
    assert verdict["passed"] is False


# ---------------------------------------------------------------------------
# Foot skate
# ---------------------------------------------------------------------------

def test_foot_skate_passes_a_clean_plant():
    positions = [(0.0, 0.0, 0.0)] * 10 + [(0.0, 0.3, 0.0), (0.0, 0.6, 0.0)]
    times = list(range(len(positions)))
    result = ac.foot_skate(times, positions)
    verdict = ac.foot_skate_verdict(result)
    assert result["contact_frames"] >= 10
    assert verdict["passed"] is True


def test_foot_skate_catches_a_sliding_plant():
    # low the whole time (contact), but sliding sideways throughout.
    positions = [(0.05 * i, 0.0, 0.0) for i in range(10)]
    times = list(range(len(positions)))
    result = ac.foot_skate(times, positions)
    verdict = ac.foot_skate_verdict(result)
    assert result["skating_frames"] > 0
    assert verdict["passed"] is False


# ---------------------------------------------------------------------------
# Anticipation / follow-through via LoG correlation — EXPERIMENTAL
# ---------------------------------------------------------------------------

def _hold_ramp_hold(times, ease, ramp_start=1.0, ramp_end=2.0):
    values = []
    for t in times:
        if t < ramp_start:
            values.append(0.0)
        elif t < ramp_end:
            x = (t - ramp_start) / (ramp_end - ramp_start)
            values.append(3 * x * x - 2 * x * x * x if ease else x)
        else:
            values.append(1.0)
    return values


def test_anticipation_verdict_flags_a_raw_linear_corner():
    times = [i * 0.05 for i in range(60)]  # 3s clip, 1s ramp
    values = _hold_ramp_hold(times, ease=False)
    verdict = ac.anticipation_verdict(times, values)
    assert verdict["passed"] is False
    assert verdict["events"] == 2
    assert all(i["kind"] == "unshaped_transition" for i in verdict["issues"])


def test_anticipation_verdict_passes_an_eased_transition():
    times = [i * 0.05 for i in range(60)]
    values = _hold_ramp_hold(times, ease=True)
    verdict = ac.anticipation_verdict(times, values)
    assert verdict["passed"] is True


def test_anticipation_verdict_refuses_a_flat_signal():
    """A constant track has no transition, so it cannot have a well-shaped one.

    This asserted `passed is True` when it was written, which is the whole
    fail-open in miniature: the loop that appends issues never runs on a signal
    with no peaks, and `not issues` reads that as a clean bill of health. A
    dead channel and a beautifully eased one returned the identical verdict.
    """
    times = [i * 0.05 for i in range(60)]
    values = [1.0] * 60
    verdict = ac.anticipation_verdict(times, values)
    assert verdict["passed"] is False
    assert verdict["events"] == 0
    assert verdict["issues"][0]["kind"] == "unmeasured"


def test_log_response_is_zero_mean_on_a_constant_signal():
    """A LoG kernel integrates to (approximately) zero, so convolving it
    against a constant should leave a response near zero everywhere — the
    kernel construction's own zero-mean correction is what this checks."""
    times = [i * 0.05 for i in range(40)]
    values = [5.0] * 40
    result = ac.log_response(times, values)
    assert max(abs(r) for r in result["response"]) < 1e-6


# ---------------------------------------------------------------------------
# Fail-open guards
#
# Every verdict in this module is a loop that appends an issue per fault and
# returns `passed = not issues`, so any input the loop cannot run over — a dead
# channel, a NaN, a clip too short to transform — used to come back green. The
# four metrics scored a 60-sample motionless track as clean, eased, jitter-free
# and skate-free at once. These pin the refusals.
# ---------------------------------------------------------------------------

def _still(n=60, dt=0.05):
    """A bone that does not move: n identical samples."""
    return [i * dt for i in range(n)], [(1.0, 2.0, 3.0)] * n


def test_zero_motion_is_unmeasured_not_smooth():
    times, values = _still()
    for measure, verdict in (
            (ac.velocity_profile(times, values),
             ac.velocity_profile_verdict),
            (ac.sparc(times, values), ac.sparc_verdict),
            (ac.foot_skate(times, values), ac.foot_skate_verdict)):
        assert measure["measured"] is False, measure
        assert verdict(measure)["passed"] is False


def test_nan_does_not_pass_every_threshold():
    """Every gate here is a `>` or `<`, and NaN compares False against both."""
    times = [i * 0.05 for i in range(20)]
    values = [(float("nan"), 0.0, 0.0)] * 20
    assert ac.velocity_profile(times, values)["measured"] is False
    assert ac.sparc(times, values)["measured"] is False
    assert ac.foot_skate(times, values)["measured"] is False
    assert ac.anticipation_verdict(times, [float("nan")] * 20)["passed"] is False


def test_a_clip_too_short_to_transform_is_not_the_smoothest_possible():
    """sparc 0.0 is its best score, and it was what a sub-0.1s clip of pure
    noise received — the arc loop never ran, so nothing was integrated."""
    times = [i * 0.01 for i in range(5)]
    values = [(v, 0.0, 0.0) for v in (0.0, 9.0, -7.0, 8.0, -9.0)]
    result = ac.sparc(times, values)
    assert result["measured"] is False
    assert ac.sparc_verdict(result)["passed"] is False


def test_a_foot_that_never_plants_is_not_a_foot_that_never_slides():
    """Zero contact frames yields zero skating frames — the same output a
    perfectly planted foot gives. A wrong ground_axis looks exactly like this."""
    times = [i * 0.05 for i in range(30)]
    rising = [(float(i), float(i) * 2.0, 0.0) for i in range(30)]
    result = ac.foot_skate(times, rising)
    assert result["measured"] is False
    assert ac.foot_skate_verdict(result)["passed"] is False


def test_a_real_clip_is_still_measured():
    """The guards must not swallow the ordinary case."""
    times = [i * 0.05 for i in range(60)]
    values = [(math.sin(t), 0.0, 0.0) for t in times]
    profile = ac.velocity_profile(times, values)
    assert profile.get("measured", True) is True
    assert profile["peak_speed"] > 0
    assert ac.sparc(times, values).get("measured", True) is True


# ---------------------------------------------------------------------------
# Rotation tracks: the double cover, and the antipode
# ---------------------------------------------------------------------------

def _swing(n, degrees=30.0, cycles=1.0, offset=None):
    """A clean sinusoidal swing about X, optionally composed onto a fixed
    offset rotation — the way a real thigh bone sits."""
    times = [i / (n - 1) for i in range(n)]
    out = []
    for t in times:
        a = math.radians(degrees) * math.sin(2 * math.pi * cycles * t)
        q = (math.sin(a / 2), 0.0, 0.0, math.cos(a / 2))
        if offset is not None:
            # Hamilton product offset * q
            w1, x1, y1, z1 = offset[3], offset[0], offset[1], offset[2]
            x2, y2, z2, w2 = q
            q = (w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                 w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                 w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
                 w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2)
        out.append(q)
    return times, out


def test_a_negated_quaternion_is_the_same_pose_and_not_a_jolt():
    """q and -q are one rotation. Differencing them as 4-vectors reported a
    jump of 2 units in a single frame on a bone that did not move — which is
    what ranked every UpperLeg as the roughest track in two shipped
    characters."""
    times, values = _swing(41)
    clean = ac.sparc(times, values)["sparc"]
    flipped = [tuple(-c for c in q) if i % 7 == 0 else q
               for i, q in enumerate(values)]
    assert ac.sparc(times, flipped)["sparc"] == pytest.approx(clean, abs=0.05)
    assert ac._speed(times, flipped) == pytest.approx(ac._speed(times, values),
                                                      abs=1e-6)


def test_canonicalise_reports_the_flips_it_removed():
    times, values = _swing(11)
    flipped = [tuple(-c for c in q) if i % 2 else q
               for i, q in enumerate(values)]
    fixed, flips = ac.canonicalise_quaternions(flipped)
    # Five, not ten: each sample is compared against the ALREADY-CORRECTED
    # previous one, so fixing every other sample leaves the track consistent.
    assert flips == 5
    assert ac.canonicalise_quaternions(fixed)[1] == 0
    # a translation track is handed straight back
    assert ac.canonicalise_quaternions([(0.0, 1.0, 2.0)]) == ([(0.0, 1.0, 2.0)], 0)


def test_a_bone_resting_near_180_degrees_measures_like_any_other():
    """Every thigh on a skeleton whose leg bones point down from the hip sits
    at w near zero, where the 4-vector chord is worst. The same swing must
    read the same wherever on the sphere it happens to live."""
    times, upright = _swing(41)
    _, flipped = _swing(41, offset=(0.0, 0.0, 1.0, 0.0))  # 180 degrees about Z
    assert (ac.sparc(times, flipped)["sparc"]
            == pytest.approx(ac.sparc(times, upright)["sparc"], abs=0.05))
    assert (max(ac._speed(times, flipped))
            == pytest.approx(max(ac._speed(times, upright)), rel=1e-6))


def test_rotation_speed_is_reported_in_radians_per_second():
    """A quarter turn in one second is pi/2 rad/s, whatever the quaternion
    components happen to be."""
    times = [0.0, 0.5, 1.0]
    q = lambda a: (0.0, 0.0, math.sin(a / 2), math.cos(a / 2))
    values = [q(0.0), q(math.pi / 4), q(math.pi / 2)]
    assert max(ac._speed(times, values)) == pytest.approx(math.pi / 2, rel=1e-6)


def test_a_non_unit_four_vector_is_not_treated_as_a_rotation():
    """These functions take plain arrays; only a track that is actually on the
    unit sphere gets the angular path."""
    times = [0.0, 1.0, 2.0]
    values = [(0.0, 0.0, 0.0, 0.0), (1.0, 1.0, 1.0, 1.0), (2.0, 2.0, 2.0, 2.0)]
    assert ac._looks_like_rotation(values) is False
    assert max(ac._speed(times, values)) == pytest.approx(2.0, rel=1e-6)


# ---------------------------------------------------------------------------
# Motion concentration: the burst, which is velocity_profile's blind spot
# ---------------------------------------------------------------------------

def test_evenly_paced_motion_is_the_baseline():
    times = [i / 40 for i in range(41)]
    values = [(t, 0.0, 0.0) for t in times]
    result = ac.motion_concentration(times, values)
    assert result["burst_ratio"] == pytest.approx(1.0, abs=0.02)
    assert ac.motion_concentration_verdict(result)["passed"] is True


def test_a_clean_swing_sits_well_under_the_threshold():
    times, values = _swing(41)
    result = ac.motion_concentration(times, values)
    assert 1.3 < result["burst_ratio"] < 2.0
    assert ac.motion_concentration_verdict(result)["passed"] is True


def test_the_threshold_does_not_move_with_the_sample_rate():
    """The same swing sampled seven ways must score the same, or the bound
    would have to be retuned per clip length — which means it would not be."""
    scores = [ac.motion_concentration(*_swing(n))["burst_ratio"]
              for n in (11, 19, 25, 37, 61, 91, 121)]
    assert max(scores) - min(scores) < 0.2


def test_a_snap_with_a_drift_around_it_is_refused():
    """Two frames carry the whole pose change; the rest is a crawl. This is
    what both shipped characters' walk cycles do, and velocity_profile passes
    it — 4% cruising is as far from constant speed as a curve gets."""
    times = [i / 24 for i in range(25)]
    pos = [0.0]
    for i in range(1, 25):
        pos.append(pos[-1] + (1.0 if i in (5, 6) else 0.01))
    values = [(x, 0.0, 0.0) for x in pos]
    result = ac.motion_concentration(times, values)
    assert result["burst_ratio"] > 3.0
    verdict = ac.motion_concentration_verdict(result)
    assert verdict["passed"] is False
    assert any(i["kind"] == "burst" for i in verdict["issues"])
    # and the gate it was invisible to still says nothing is wrong
    assert ac.velocity_profile_verdict(
        ac.velocity_profile(times, values))["passed"] is True


def test_a_motionless_track_is_unmeasured_not_evenly_paced():
    times = [i / 24 for i in range(25)]
    values = [(0.0, 0.0, 0.0)] * 25
    result = ac.motion_concentration(times, values)
    assert result["measured"] is False
    assert ac.motion_concentration_verdict(result)["passed"] is False


def test_too_few_frames_to_have_a_fastest_tenth():
    times = [i / 8 for i in range(9)]
    values = [(float(i), 0.0, 0.0) for i in range(9)]
    assert ac.motion_concentration(times, values)["measured"] is False


# ---------------------------------------------------------------------------
# float32 noise on a bone nobody animated
# ---------------------------------------------------------------------------

def test_a_static_channel_exported_as_float32_noise_is_not_motion():
    """A scale of 1.0 comes back from a GLB as 0.9999999403953552 — one ULP.
    That cleared the absolute dead-channel floor and 34 of a walk cycle's 69
    channels were then judged as un-eased linear motion, every one of them a
    bone that was never animated."""
    times = [i / 20 for i in range(21)]
    values = [(1.0, 1.0 - (i % 2) * 6e-8, 1.0) for i in range(21)]
    profile = ac.velocity_profile(times, values)
    assert profile["measured"] is False
    assert ac.motion_concentration(times, values)["measured"] is False


def test_a_rotation_track_that_never_turns_is_not_motion():
    times = [i / 20 for i in range(21)]
    values = [(0.0, 0.3585513234138489, -0.9335100054740906,
               7.1e-08 - (i % 2) * 4e-10) for i in range(21)]
    values = [tuple(c / sum(x * x for x in q) ** 0.5 for c in q) for q in values]
    assert ac.velocity_profile(times, values)["measured"] is False


def test_two_samples_have_no_speed_profile_to_read():
    """One interval is trivially at its own peak for its whole duration, so
    cruising_fraction was 1.0 for every two-key channel in every file."""
    profile = ac.velocity_profile([0.0, 1.5], [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)])
    assert profile["measured"] is False
    assert ac.velocity_profile_verdict(profile)["passed"] is False


def test_real_motion_still_measures_after_the_noise_guard():
    times = [i / 30 for i in range(31)]
    values = [(math.sin(t * 3), 0.0, 0.0) for t in times]
    assert ac.velocity_profile(times, values).get("measured", True) is True
    assert ac.motion_concentration(times, values).get("measured", True) is True


def test_no_gate_in_this_module_reads_float32_noise_as_motion():
    """The guard belongs to all four, not to the two that were fixed first.

    SPARC was the worst of them: it normalises the spectrum by its own peak,
    so a track of pure last-digit rounding is normalised UP into a
    full-amplitude noise spectrum and scores about -9.6 — past the -8.0 bound,
    and reported as the roughest track in the file. anticipation's own floor
    was absolute at 1e-12, which catches exact constants and misses float32
    rounding at 1e-8, so it reported two un-anticipated transitions on a bone
    nobody animated.
    """
    times = [i / 20 for i in range(21)]
    values = [(1.0, 1.0 - (i % 2) * 6e-8, 1.0) for i in range(21)]
    for measure in (ac.velocity_profile, ac.motion_concentration, ac.sparc):
        assert measure(times, values)["measured"] is False, measure.__name__
    assert ac.sparc_verdict(ac.sparc(times, values))["passed"] is False
    verdict = ac.anticipation_verdict(times, [v[1] for v in values])
    assert verdict["events"] == 0
    assert verdict["issues"][0]["kind"] == "unmeasured"


def test_the_guards_do_not_swallow_a_real_curve():
    """All four still measure something that actually moves."""
    times = [i / 30 for i in range(31)]
    values = [(math.sin(t * 3), 0.0, 0.0) for t in times]
    assert ac.velocity_profile(times, values).get("measured", True) is True
    assert ac.motion_concentration(times, values).get("measured", True) is True
    assert ac.sparc(times, values).get("measured", True) is True
    assert ac.anticipation_verdict(times, [v[0] for v in values])["events"] >= 1
