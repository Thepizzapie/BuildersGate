"""Integrated loudness, measured — or reported as unmeasured. Never guessed.

The audio seat's hook table wants a LUFS column beside every bound sound,
because a stinger 5 dB over the ambient bed is the bug you cannot see in a
waveform and cannot hear until it is in the build. Until this module existed the
column said "not measured" on every row, which was honest and useless.

MEASURED, NOT ESTIMATED. Integrated loudness is not peak, not RMS and not
anything derivable from a file header: EBU R 128 puts the signal through a
K-weighting filter pair, gates it in 400 ms blocks against a relative threshold,
and averages what survives. Reimplementing that on stdlib would be a plausible
number rather than a true one, so this shells the reference implementation —
ffmpeg's ``ebur128`` filter — and when there is no ffmpeg it says so and returns
``None``. A row that reads "unmeasured" is correct; a row carrying -14.2 because
that is a normal-looking value is the exact failure the seat exists to catch.

CACHED ON (path, mtime, size), because the workspace polls. Measuring eighteen
files takes a second or two of subprocess time and the answer cannot change
while the bytes do not.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from . import ffmpegbin as _ffmpegbin

#: The seat's declared target. Godot mixes linearly and ships no normalisation,
#: so the target is a discipline the seat keeps, not something the engine
#: enforces — which is why it belongs next to the measurement.
TARGET_LUFS = -14.0

#: How far off target before a row is worth flagging. One dB is inaudible on a
#: one-shot; three is the point where a sound stops sitting in the mix.
TOLERANCE_LU = 3.0

#: Cap on how long one file may take. A corrupt file that makes ffmpeg spin
#: must not hang the workspace poll.
TIMEOUT_S = 30

# ffmpeg writes the ebur128 summary to stderr as an indented block:
#     Integrated loudness:
#       I:         -14.2 LUFS
#       Threshold: -24.8 LUFS
_I_LINE = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", re.M)
_PEAK_LINE = re.compile(r"^\s*Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", re.M)

# (resolved path, mtime_ns, size) -> result dict
_CACHE: dict[tuple[str, int, int], dict] = {}


def available() -> bool:
    """Is there an ffmpeg to measure with? The whole column depends on it."""
    return _ffmpegbin.resolve() is not None


def _unmeasured(reason: str) -> dict:
    return {"lufs": None, "true_peak": None, "measured": False, "reason": reason}


def measure(path: str | os.PathLike[str]) -> dict:
    """Integrated loudness of one file.

    Returns ``{lufs, true_peak, measured, reason}``. ``measured`` is False and
    ``lufs`` is None whenever the number is not known — no ffmpeg, unreadable
    file, silence with no gated blocks. The caller renders the reason; it must
    never substitute a default.
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return _unmeasured("no such file")
    key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
    hit = _CACHE.get(key)
    if hit is not None:
        return dict(hit)

    exe = _ffmpegbin.resolve()
    if not exe:
        # Deliberately NOT cached: installing ffmpeg must make the column work
        # without restarting the dashboard.
        return _unmeasured("no ffmpeg on this machine — set BGATE_FFMPEG or put "
                           "one in ~/.bgate/bin")
    try:
        proc = subprocess.run(
            [exe, "-nostdin", "-hide_banner", "-i", str(p),
             "-af", "ebur128=peak=true", "-f", "null", "-"],
            capture_output=True, text=True, timeout=TIMEOUT_S,
            errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return _unmeasured(f"ffmpeg would not run: {exc}")

    err = proc.stderr or ""
    # The LAST match is the end-of-stream summary; earlier ones are the running
    # per-block report, which is a different (momentary) number entirely.
    hits = _I_LINE.findall(err)
    if not hits:
        return _unmeasured("ffmpeg produced no ebur128 summary "
                           f"(exit {proc.returncode})")
    try:
        lufs = float(hits[-1])
    except ValueError:
        return _unmeasured("ffmpeg's loudness summary did not parse")

    peaks = _PEAK_LINE.findall(err)
    peak: Optional[float] = None
    if peaks:
        try:
            peak = float(peaks[-1])
        except ValueError:
            peak = None

    # -70 LUFS is ebur128's floor: NOTHING cleared the gate. For a UI one-shot
    # that is the normal answer rather than a fault — R 128 integrates 400 ms
    # blocks and half this project's SFX are shorter than one block — so the
    # row says integrated loudness is undefined and shows the peak, which IS
    # measured. Writing a plausible LUFS here would be the invented number.
    if lufs <= -70.0:
        out = _unmeasured("nothing cleared the 400 ms EBU gate — too short or "
                          "silent for an integrated reading")
        out["true_peak"] = peak
        _CACHE[key] = out
        return dict(out)

    out = {"lufs": round(lufs, 1), "true_peak": peak, "measured": True,
           "reason": ""}
    _CACHE[key] = out
    return dict(out)


def verdict(lufs: Optional[float], *, target: float = TARGET_LUFS,
            tolerance: float = TOLERANCE_LU) -> str:
    """"too loud" / "too quiet" / "on target" / "" for an unmeasured file.

    A word, not a colour: the seat reads this next to the number so a row says
    what is wrong with it rather than making the reader do the subtraction.
    """
    if lufs is None:
        return ""
    if lufs > target + tolerance:
        return "too loud"
    if lufs < target - tolerance:
        return "too quiet"
    return "on target"
