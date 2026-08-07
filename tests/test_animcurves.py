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
