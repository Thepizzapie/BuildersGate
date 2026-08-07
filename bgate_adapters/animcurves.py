"""Animation-curve quality metrics, read straight off an exported glTF/GLB.

WHY THIS OPERATES ON THE EXPORTED FILE AND NOT ON BLENDER F-CURVES. Every clip
this pipeline produces already crosses through a GLB export (see
blender.py's `export_animations` flag), and glTF's animation format is a
public, versioned spec rather than a moving target — a script written against
it does not break when Blender's Python API does. It also means these
functions carry no Blender dependency at all: no bpy import, no headless
spawn, just the accessor bytes.

WHY NO THIRD-PARTY glTF LIBRARY. The project keeps its core dependency list
short on purpose (see pyproject.toml's own comments on this) and everything
these metrics need from a GLB — the JSON chunk, and float accessor arrays out
of the BIN chunk — is a few dozen lines of `struct.unpack`. A dependency
earns its place by saving more than that.

THE METRICS ARE PROXIES, NOT A VERDICT ON "GOOD ANIMATION". Appeal and
exaggeration are not attempted here — the research behind this module found
no computational stand-in for either. What IS computable, and computable with
real precedent (SIGGRAPH's slow-in/slow-out filter, the mocap-cleanup
literature's footskate and SPARC-smoothness metrics), is measured. Treat a
clean pass as "no obvious defect", not "looks good" — the second question
still needs a human or, per the same research, a pairwise judge with far less
precedent behind it than these numbers have.
"""
from __future__ import annotations

import cmath
import json
import math
import struct
from pathlib import Path
from typing import Any

_COMPONENT_FORMATS = {
    5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2),
    5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4),
}
_TYPE_COUNTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4,
                "MAT2": 4, "MAT3": 9, "MAT4": 16}


def _read_glb(path: str | Path) -> tuple[dict, bytes]:
    data = Path(path).read_bytes()
    if len(data) < 12:
        raise ValueError(f"too small to be a GLB: {path}")
    magic, _version, length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise ValueError(f"not a GLB file (bad magic): {path}")
    offset = 12
    json_chunk: dict | None = None
    bin_chunk = b""
    while offset + 8 <= min(length, len(data)):
        chunk_len, chunk_type = struct.unpack_from("<I4s", data, offset)
        offset += 8
        chunk_data = data[offset:offset + chunk_len]
        offset += chunk_len
        if chunk_type == b"JSON":
            json_chunk = json.loads(chunk_data.decode("utf-8"))
        elif chunk_type == b"BIN\x00":
            bin_chunk = chunk_data
    if json_chunk is None:
        raise ValueError(f"no JSON chunk in {path}")
    return json_chunk, bin_chunk


def _accessor_values(gltf: dict, bin_data: bytes, accessor_index: int):
    acc = gltf["accessors"][accessor_index]
    count = acc["count"]
    n_comp = _TYPE_COUNTS[acc["type"]]
    bv_index = acc.get("bufferView")
    if bv_index is None:
        # A sparse or zero-filled accessor. Nothing this module reads from
        # animation samplers is legally sparse per the glTF spec, so this is
        # a defensive default, not an expected path.
        zero = 0.0 if n_comp == 1 else tuple([0.0] * n_comp)
        return [zero] * count
    comp_fmt, comp_size = _COMPONENT_FORMATS[acc["componentType"]]
    bv = gltf["bufferViews"][bv_index]
    base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or (comp_size * n_comp)
    fmt = "<" + comp_fmt * n_comp
    out = []
    for i in range(count):
        off = base + i * stride
        vals = struct.unpack_from(fmt, bin_data, off)
        out.append(vals[0] if n_comp == 1 else vals)
    return out


def extract_animations(path: str | Path) -> dict:
    """Every animation clip in a GLB, as plain time/value channel data.

    Returns {ok, animations: [{name, channels: [{node, path, interpolation,
    times, values}]}]}. `path` here is the glTF animation target path
    (translation/rotation/scale/weights), unrelated to the file path
    argument — an unfortunate but spec-mandated name collision.
    """
    src = Path(path)
    if not src.is_file():
        return {"ok": False, "error": f"no file at {src}"}
    try:
        gltf, bin_data = _read_glb(src)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    nodes = gltf.get("nodes") or []
    animations = []
    for anim in gltf.get("animations") or []:
        channels = []
        for ch in anim.get("channels") or []:
            sampler = anim["samplers"][ch["sampler"]]
            times = _accessor_values(gltf, bin_data, sampler["input"])
            values = _accessor_values(gltf, bin_data, sampler["output"])
            target = ch.get("target") or {}
            node_index = target.get("node")
            node_name = (nodes[node_index].get("name", f"node{node_index}")
                        if node_index is not None and node_index < len(nodes)
                        else "?")
            channels.append({
                "node": node_name, "path": target.get("path"),
                "interpolation": sampler.get("interpolation", "LINEAR"),
                "times": list(times), "values": list(values),
            })
        animations.append({"name": anim.get("name", ""), "channels": channels})
    return {"ok": True, "animations": animations}


# ---------------------------------------------------------------------------
# Shared derivative primitive
# ---------------------------------------------------------------------------

def _speed(times: list[float], values: list) -> list[float]:
    """Central-difference speed magnitude per sample.

    `values` is a list of scalars or a list of equal-length component tuples
    (VEC3 translation, VEC4 rotation quaternion). Endpoints use a
    one-sided difference since they have no symmetric neighbour.
    """
    n = len(times)
    if n < 2:
        return [0.0] * n
    vec = isinstance(values[0], (tuple, list))
    out = []
    for i in range(n):
        lo, hi = max(0, i - 1), min(n - 1, i + 1)
        dt = times[hi] - times[lo]
        if dt <= 1e-9:
            out.append(0.0)
            continue
        if vec:
            d = [(values[hi][k] - values[lo][k]) / dt for k in range(len(values[i]))]
            out.append(sum(x * x for x in d) ** 0.5)
        else:
            out.append(abs((values[hi] - values[lo]) / dt))
    return out


# ---------------------------------------------------------------------------
# Arcs: does a translation path bow, or cut a straight line
# ---------------------------------------------------------------------------

def arc_deviation(times: list[float], positions: list) -> dict:
    """How far a VEC3 path bows from the straight chord between its endpoints,
    as a fraction of the chord's own length.

    A DESCRIPTIVE measurement, not a pass/fail: animation principles call for
    an arc on a swinging limb and a straight line on a jab's extension, and
    knowing which this clip is doing is not something a curve alone can
    answer. Report the number; a caller with context on the motion's intent
    judges it.
    """
    if len(positions) < 3:
        return {"deviation": 0.0, "samples": len(positions)}
    p0, p1 = positions[0], positions[-1]
    chord = [p1[k] - p0[k] for k in range(3)]
    chord_len = sum(c * c for c in chord) ** 0.5
    if chord_len < 1e-9:
        mean = [sum(p[k] for p in positions) / len(positions) for k in range(3)]
        radii = [sum((p[k] - mean[k]) ** 2 for k in range(3)) ** 0.5 for p in positions]
        return {"deviation": round(max(radii), 4) if radii else 0.0,
                "samples": len(positions),
                "note": "start and end coincide; reporting max radius from mean"}
    axis = [c / chord_len for c in chord]
    total = 0.0
    for p in positions:
        rel = [p[k] - p0[k] for k in range(3)]
        proj = sum(rel[k] * axis[k] for k in range(3))
        perp = [rel[k] - proj * axis[k] for k in range(3)]
        total += sum(x * x for x in perp) ** 0.5
    return {"deviation": round((total / len(positions)) / chord_len, 4),
            "samples": len(positions)}


# ---------------------------------------------------------------------------
# Velocity profile: easing and timing/spacing collapse to one signal once a
# clip is uniformly sampled, so one measurement answers both
# ---------------------------------------------------------------------------

def velocity_profile(times: list[float], values: list, *,
                     near_peak_ratio: float = 0.85) -> dict:
    """A speed signal, plus what fraction of the clip's DURATION it spends
    near its own peak speed.

    Classical spacing (distance travelled per frame) and easing (a curve's
    tangent flattening near a hold) are the same fact on a fixed-rate sampled
    export: a flat speed profile IS both even spacing and missing easing at
    once. `cruising_fraction` measures time, not sample count, on purpose —
    an adjacent-sample-difference test shrinks toward zero on any finely
    sampled curve regardless of its actual shape (a smooth bell curve at
    60fps looks locally "flat" between any two neighbours), so it cannot
    tell a genuinely eased curve from a linear one once the sample rate goes
    up. A pure linear ramp spends 100% of its duration at constant (hence
    "near-peak") speed; a properly eased motion spends only the time around
    its actual peak there — see the SIGGRAPH 2006 slow-in/slow-out filter:
    near-zero second derivative at a segment boundary is exactly what a
    dominant near-peak-speed duration also measures, from the other end.
    """
    speed = _speed(times, values)
    n = len(speed)
    if n < 2 or len(times) < 2:
        return {"speed": [round(s, 5) for s in speed], "peak_speed": 0.0,
                "cruising_fraction": 0.0}
    peak = max(speed) or 1e-9
    duration = times[-1] - times[0]
    if duration <= 1e-9:
        return {"speed": [round(s, 5) for s in speed], "peak_speed": round(peak, 5),
                "cruising_fraction": 0.0}
    threshold = near_peak_ratio * peak
    near_peak_time = 0.0
    for i in range(n - 1):
        if speed[i] >= threshold and speed[i + 1] >= threshold:
            near_peak_time += times[i + 1] - times[i]
    return {"speed": [round(s, 5) for s in speed], "peak_speed": round(peak, 5),
            "cruising_fraction": round(near_peak_time / duration, 4)}


def velocity_profile_verdict(profile: dict, *, max_cruising_fraction: float = 0.6) -> dict:
    frac = profile.get("cruising_fraction", 0.0)
    issues = []
    if frac > max_cruising_fraction:
        issues.append({"kind": "linear_motion", "value": frac,
                       "note": f"{frac * 100:.0f}% of this track holds a "
                               "near-constant speed between neighbouring "
                               "samples — reads as un-eased, linearly-"
                               "interpolated keyframes rather than a "
                               "weighted curve"})
    return {"passed": not issues, "issues": issues,
            "threshold": max_cruising_fraction}


# ---------------------------------------------------------------------------
# Jitter: SPARC, the mocap-cleanup literature's lowest-variance smoothness
# metric, computed off the speed profile via a direct (non-FFT) DFT
# ---------------------------------------------------------------------------

def _dft_magnitude(signal: list[float]) -> list[float]:
    """|DFT(signal)| for k = 0..N//2, via a direct O(n^2) sum.

    Animation clips run tens to a few hundred samples — well inside where a
    hand-written DFT beats adding a dependency for a real FFT.
    """
    n = len(signal)
    if n == 0:
        return []
    half = n // 2 + 1
    out = []
    for k in range(half):
        s = complex(0.0, 0.0)
        w = -2j * math.pi * k / n
        for t, x in enumerate(signal):
            s += x * cmath.exp(w * t)
        out.append(abs(s))
    return out


def sparc(times: list[float], values: list, *, freq_cutoff: float = 10.0,
         amplitude_threshold: float = 0.05) -> dict:
    """Spectral arc length of this track's speed profile.

    Balasubramanian et al.'s smoothness metric — reported across comparative
    studies as the most duration-unbiased, lowest-variance jitter measure
    against alternatives like normalized jerk. More negative is rougher;
    near 0 is smoothest. The cutoff walks in from DC until the normalized
    spectrum first drops below `amplitude_threshold` or passes
    `freq_cutoff`, which is SPARC's own adaptive-cutoff definition rather
    than a fixed window.
    """
    speed = _speed(times, values)
    n = len(speed)
    if n < 4 or not times:
        return {"sparc": 0.0, "samples": n}
    duration = max(times) - min(times)
    if duration <= 1e-9:
        return {"sparc": 0.0, "samples": n}
    fs = (n - 1) / duration
    mag = _dft_magnitude(speed)
    peak = max(mag) or 1e-9
    norm = [m / peak for m in mag]
    freqs = [k * fs / n for k in range(len(mag))]
    cutoff_idx = 0
    for i, (f, m) in enumerate(zip(freqs, norm)):
        if f > freq_cutoff:
            break
        cutoff_idx = i
        if m < amplitude_threshold:
            break
    arc = 0.0
    for i in range(cutoff_idx):
        df = freqs[i + 1] - freqs[i]
        dm = norm[i + 1] - norm[i]
        arc += math.sqrt(df * df + dm * dm)
    return {"sparc": round(-arc, 4), "samples": n, "cutoff_index": cutoff_idx}


def sparc_verdict(result: dict, *, min_sparc: float = -8.0) -> dict:
    """min_sparc is a starting point borrowed from the gait-smoothness
    literature, not a value validated against this project's own stylized
    character clips — treat this verdict as advisory until it has been
    checked against known-good output, the same caveat the research behind
    this module raised for every judge in this space.
    """
    value = result.get("sparc", 0.0)
    issues = []
    if value < min_sparc:
        issues.append({"kind": "jitter", "value": value,
                       "note": f"SPARC {value} is rougher than {min_sparc} "
                               "— this track's speed changes direction or "
                               "magnitude erratically rather than smoothly"})
    return {"passed": not issues, "issues": issues, "threshold": min_sparc}


# ---------------------------------------------------------------------------
# Foot skate: a planted foot that still slides
# ---------------------------------------------------------------------------

def foot_skate(times: list[float], positions: list, *, ground_axis: int = 1,
               contact_band: float = 0.03, skate_tolerance: float = 0.02) -> dict:
    """Frames where a foot sits within `contact_band` of its lowest point in
    this clip but still moves horizontally more than `skate_tolerance`.

    Kovar et al.'s footskate signature, from the mocap-cleanup literature.
    `ground_axis` defaults to 1 (glTF is Y-up by convention); pass 2 for a
    Z-up export. This is a heuristic against the clip's OWN lowest sample,
    not a real ground-contact classifier — it will misread a foot that
    never actually plants (a jump, a kick) as having no contact frames at
    all, which is the conservative failure direction.
    """
    if len(positions) < 3:
        return {"contact_frames": 0, "skating_frames": 0, "worst_slide": 0.0}
    heights = [p[ground_axis] for p in positions]
    floor = min(heights)
    contact = [h - floor <= contact_band for h in heights]
    horiz = [k for k in range(3) if k != ground_axis]
    skating, worst = 0, 0.0
    for i in range(1, len(positions)):
        if contact[i] and contact[i - 1]:
            d = sum((positions[i][k] - positions[i - 1][k]) ** 2 for k in horiz) ** 0.5
            if d > skate_tolerance:
                skating += 1
                worst = max(worst, d)
    return {"contact_frames": sum(contact), "skating_frames": skating,
            "worst_slide": round(worst, 4)}


def foot_skate_verdict(result: dict, *, max_skating_frames: int = 0) -> dict:
    issues = []
    if result.get("skating_frames", 0) > max_skating_frames:
        issues.append({"kind": "foot_skate", "value": result["skating_frames"],
                       "note": f"{result['skating_frames']} frames where a "
                               "planted foot still slid up to "
                               f"{result.get('worst_slide')} units — the "
                               "classic mocap-cleanup footskate signature"})
    return {"passed": not issues, "issues": issues}
