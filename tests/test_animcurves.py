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
