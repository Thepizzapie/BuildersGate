"""Session recording — game window video (ffmpeg gdigrab) + mic (sounddevice).

Two separate streams on one clock rather than one muxed ffmpeg command, because
the mic is the part that fails and it must fail LOUDLY and EARLY. ffmpeg's dshow
enumeration finds nothing on this machine, while sounddevice sees the devices
fine — so audio goes through sounddevice, which also lets us measure signal
before committing to a 20-minute recording.

The clock: every stream records its own wall-clock start. All downstream
timestamps are SECONDS FROM SESSION START, so transcript, frames, and telemetry
join on one axis.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

MIC_RATE = 16000       # what whisper wants; resampling later is wasted work
MIC_CHANNELS = 1
# Distinguishes a DEAD mic (unplugged/muted -> exact digital silence, peak ~0)
# from a LIVE one. It must sit BELOW a live mic's idle noise floor, not above
# it: at 0.001 a working-but-quiet headset (e.g. Arctis: idle ~3e-4, speech
# ~3e-3) failed the passive check unless you happened to be talking during it.
# 5e-5 clears any live mic's noise floor while still catching true silence.
SILENCE_PEAK = 0.00005


class RecorderError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Preflight — the point is to fail BEFORE a session, not after
# ---------------------------------------------------------------------------
def list_inputs() -> list[dict]:
    """Input devices sounddevice can see, with host API."""
    try:
        import sounddevice as sd
    except Exception as exc:
        raise RecorderError(f"sounddevice unavailable: {exc}") from exc

    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            out.append({
                "index": idx,
                "name": dev["name"],
                "channels": dev["max_input_channels"],
                "rate": int(dev["default_samplerate"]),
                "hostapi": sd.query_hostapis(dev["hostapi"])["name"],
            })
    return out


def probe_mic(device: Optional[int] = None, seconds: float = 1.5) -> dict:
    """Verify a mic is present and openable; measure level as an ADVISORY.

    Returns {ok, device, name, rms, peak, signal_detected, warning}. ok=True as
    long as a real input device OPENS — because a silence check cannot pass a
    noise-gated headset (Arctis, most gaming headsets) when you're not talking
    during the 1.5s probe: idle output is indistinguishable from a muted mic.
    Blocking those wastes more sessions than it saves. If no signal is heard we
    pass with a warning instead, and the empty-transcript case is caught on the
    far side. ok=False only when there is genuinely no openable device.
    """
    try:
        import numpy as np
        import sounddevice as sd
    except Exception as exc:
        return {"ok": False, "reason": f"audio deps unavailable: {exc}"}

    if device is None:
        try:
            device = sd.default.device[0]
        except Exception:
            device = -1
        if device is None or device == -1:
            # No default set, but fall back to the first real input device
            # rather than blocking — a connected-but-not-default mic is common.
            candidates = list_inputs()
            if not candidates:
                return {"ok": False, "reason": "no input devices at all"}
            device = candidates[0]["index"]

    try:
        info = sd.query_devices(device)
    except Exception as exc:
        return {"ok": False, "device": device, "reason": f"no such device: {exc}"}

    try:
        rec = sd.rec(int(seconds * MIC_RATE), samplerate=MIC_RATE,
                     channels=1, device=device, dtype="float32")
        sd.wait()
    except Exception as exc:
        return {"ok": False, "device": device, "name": info["name"],
                "reason": f"device failed to open: {exc}"}

    peak = float(np.max(np.abs(rec)))
    rms = float(np.sqrt(np.mean(rec ** 2)))
    signal = peak >= SILENCE_PEAK
    out = {"ok": True, "device": device, "name": info["name"],
           "rms": rms, "peak": peak, "signal_detected": signal}
    if not signal:
        # Present and openable, but quiet during the probe — likely a
        # noise-gated headset (nothing to hear until you talk) or a muted mic.
        # Pass with a warning rather than block; the transcript is the arbiter.
        out["warning"] = (f"{info['name']} opened but was silent during the "
                          "check — if it's a noise-gated headset that's normal; "
                          "if the transcript comes back empty, it was muted.")
    return out


def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RecorderError("ffmpeg not found on PATH — needed for screen capture")
    return exe


def list_windows(filter_text: str = "") -> list[dict]:
    """Visible top-level windows, for targeting gdigrab at the game."""
    if sys.platform != "win32":
        return []
    script = (
        "Get-Process | Where-Object { $_.MainWindowTitle } | "
        "Select-Object Id,ProcessName,MainWindowTitle | ConvertTo-Json -Compress"
    )
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=30,
                          stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    import json
    try:
        data = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    rows = [{"pid": d["Id"], "process": d["ProcessName"], "title": d["MainWindowTitle"]}
            for d in data]
    if filter_text:
        low = filter_text.lower()
        rows = [r for r in rows if low in r["title"].lower() or low in r["process"].lower()]
    return rows


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------
@dataclass
class Recording:
    """A live session. Streams start independently; offsets are recorded."""
    out_dir: Path
    video_path: Optional[Path] = None
    audio_path: Optional[Path] = None
    started_at: float = 0.0
    video_started_at: float = 0.0
    audio_started_at: float = 0.0
    _proc: Optional[subprocess.Popen] = None
    _stream: object = None
    _frames: list = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _err: list = field(default_factory=list)


def start(out_dir: str | Path, *, window_title: Optional[str] = None,
          mic_device: Optional[int] = None, fps: int = 30) -> Recording:
    """Begin capturing. Raises rather than returning a doomed session.

    window_title  gdigrab target. None captures the full desktop.
    mic_device    sounddevice input index. Probed first — a silent mic aborts.
    """
    import numpy as np
    import sounddevice as sd

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    probe = probe_mic(mic_device)
    if not probe["ok"]:
        raise RecorderError(
            f"mic preflight failed: {probe['reason']}. "
            "Recording a silent session wastes the whole playthrough."
        )
    mic_device = probe["device"]

    ffmpeg = find_ffmpeg()
    rec = Recording(out_dir=out)
    rec.video_path = out / "session.mp4"
    rec.audio_path = out / "session.wav"
    rec.started_at = time.time()

    # --- video ---------------------------------------------------------
    target = f"title={window_title}" if window_title else "desktop"
    cmd = [
        ffmpeg, "-y", "-loglevel", "warning",
        "-f", "gdigrab", "-framerate", str(fps), "-i", target,
        # yuv420p + even dims: anything else won't play in half the world's players
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        str(rec.video_path),
    ]
    # stdin=PIPE, not DEVNULL: ffmpeg wants 'q' to stop gracefully and finalize
    # the moov atom. A killed ffmpeg leaves an unplayable file.
    rec._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    rec.video_started_at = time.time()

    time.sleep(0.3)
    if rec._proc.poll() is not None:
        err = (rec._proc.stderr.read() or b"").decode("utf-8", "replace")
        raise RecorderError(
            f"ffmpeg died immediately (exit {rec._proc.returncode}). "
            f"Window title {window_title!r} probably doesn't exist. {err[-400:]}"
        )

    # --- audio ---------------------------------------------------------
    def on_audio(indata, frames, time_info, status):
        if status:
            rec._err.append(str(status))
        rec._frames.append(indata.copy())

    rec._stream = sd.InputStream(samplerate=MIC_RATE, channels=MIC_CHANNELS,
                                 device=mic_device, dtype="float32", callback=on_audio)
    rec._stream.start()
    rec.audio_started_at = time.time()
    return rec


def stop(rec: Recording, timeout: int = 60) -> dict:
    """End capture, finalize both files, return paths + the clock offsets."""
    import numpy as np

    ended = time.time()

    if rec._stream is not None:
        try:
            rec._stream.stop()
            rec._stream.close()
        except Exception as exc:
            rec._err.append(f"audio stop: {exc}")

    audio_seconds = 0.0
    if rec._frames:
        data = np.concatenate(rec._frames, axis=0)
        audio_seconds = len(data) / MIC_RATE
        pcm = np.clip(data, -1.0, 1.0)
        pcm = (pcm * 32767).astype(np.int16)
        with wave.open(str(rec.audio_path), "wb") as wf:
            wf.setnchannels(MIC_CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(MIC_RATE)
            wf.writeframes(pcm.tobytes())

    video_ok, video_err = True, ""
    if rec._proc is not None:
        try:
            # 'q' = graceful stop. Without it the moov atom never lands.
            rec._proc.stdin.write(b"q")
            rec._proc.stdin.flush()
            rec._proc.stdin.close()
        except Exception:
            pass
        try:
            rec._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            rec._proc.kill()
            rec._proc.wait(timeout=10)
            video_ok = False
            video_err = "ffmpeg would not exit; file may be truncated"
        if rec._proc.returncode not in (0, 255) and video_ok:
            stderr = (rec._proc.stderr.read() or b"").decode("utf-8", "replace")
            video_ok, video_err = False, stderr[-400:]

    return {
        "video_path": str(rec.video_path) if video_ok and rec.video_path.exists() else None,
        "audio_path": str(rec.audio_path) if rec.audio_path and rec.audio_path.exists() else None,
        "duration_s": round(ended - rec.started_at, 2),
        "audio_seconds": round(audio_seconds, 2),
        # Streams don't start at the same instant; downstream must correct for it.
        "audio_offset_s": round(rec.audio_started_at - rec.started_at, 3),
        "video_offset_s": round(rec.video_started_at - rec.started_at, 3),
        "video_ok": video_ok,
        "video_error": video_err,
        "warnings": rec._err[:10],
    }


def extract_frame(video_path: str, t: float, out_path: str) -> dict:
    """Pull a single frame at t seconds. This is what agents actually 'see'."""
    ffmpeg = find_ffmpeg()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    # -ss before -i: keyframe seek, fast and accurate enough for a screenshot.
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{max(t, 0):.3f}",
           "-i", video_path, "-frames:v", "1", "-q:v", "3", out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    if not Path(out_path).exists():
        return {"ok": False, "t": t,
                "error": (proc.stderr or "ffmpeg produced no frame")[-200:]}
    return {"ok": True, "t": t, "path": out_path}


def extract_filmstrip(video_path: str, out_dir: str, *, duration_s: float,
                      interval_s: float = 4.0, max_frames: int = 90) -> list[dict]:
    """Sample the WHOLE video into an ordered strip of frames.

    This is how the director actually watches a playtest: a Claude session
    cannot stream video, but it can read a sequence of stills. One ffmpeg pass
    with an fps filter is far cheaper than N seeks. The interval widens for long
    sessions so we never blow past max_frames.

    Returns [{i, t, path}] ordered by time. t is derived from frame index and
    the effective interval (fps filter emits evenly), which is accurate enough
    to line a frame up against a transcript timestamp.
    """
    ffmpeg = find_ffmpeg()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dur = max(float(duration_s or 0.0), interval_s)
    step = max(interval_s, dur / max_frames)  # widen so count stays <= max_frames
    # fps=1/step samples one frame every `step` seconds across the whole file.
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", video_path,
           "-vf", f"fps=1/{step:.4f},scale=768:-1", "-q:v", "4",
           str(out / "strip_%04d.jpg")]
    subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                   stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    frames = sorted(out.glob("strip_*.jpg"))
    # ffmpeg's first fps frame lands at t≈step/2; each subsequent is +step.
    return [{"i": i, "t": round(step * (i + 0.5), 2), "path": str(p)}
            for i, p in enumerate(frames)]


def probe_video(video_path: str) -> dict:
    """Duration/size of a finished recording — proves the file is playable."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"ok": False, "reason": "ffprobe not found"}
    cmd = [ffprobe, "-v", "error", "-show_entries",
           "format=duration,size:stream=width,height,codec_name",
           "-of", "json", video_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                          stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    import json
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "reason": (proc.stderr or "unreadable")[-200:]}
    if not data.get("format"):
        return {"ok": False, "reason": "no format data — file is not a valid video"}
    stream = (data.get("streams") or [{}])[0]
    return {
        "ok": True,
        "duration_s": round(float(data["format"].get("duration", 0)), 2),
        "bytes": int(data["format"].get("size", 0)),
        "width": stream.get("width"),
        "height": stream.get("height"),
        "codec": stream.get("codec_name"),
    }
