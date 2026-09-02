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
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence
from bgate_core.runtime import ffmpegbin as _ffmpegbin

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

MIC_RATE = 16000       # what whisper wants; resampling later is wasted work
MIC_CHANNELS = 1
# Distinguishes a DEAD mic (unplugged/muted -> exact digital silence, peak ~0)
# from a LIVE one. It must sit BELOW a live mic's idle noise floor, not above
# it: at 0.001 a working-but-quiet headset (e.g. Arctis: idle ~3e-4, speech
# ~3e-3) failed the passive check unless you happened to be talking during it.
# 5e-5 clears any live mic's noise floor while still catching true silence.
SILENCE_PEAK = 0.00005

# How much of the live signal the meter remembers. Seconds, not samples: the
# point is "have you said anything recently", and the alternative — keeping the
# whole take in memory to measure it — is a 20-minute array we already have on
# disk. ~2s of 512-sample blocks at 16 kHz.
LEVEL_WINDOW_S = 2.0


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

    # Try-order: the requested (or default) device first, then EVERY other input
    # device. A wireless headset — usually the system default — sleeps/powers off
    # and can no longer be OPENED even though it's still listed ("Invalid device"),
    # so we fall through to an always-on wired mic instead of blocking recording.
    preferred = device
    order: list[int] = []
    if preferred is not None:
        order.append(preferred)
    else:
        try:
            d0 = sd.default.device[0]
        except Exception:
            d0 = -1
        if isinstance(d0, int) and d0 >= 0:
            order.append(d0)
    try:
        for d in list_inputs():
            if d["index"] not in order:
                order.append(d["index"])
    except Exception:
        pass
    if not order:
        return {"ok": False, "reason": "no input devices at all"}

    info = None
    rec = None
    last_err = None
    for dev in order:
        try:
            info = sd.query_devices(dev)
            rec = sd.rec(int(seconds * MIC_RATE), samplerate=MIC_RATE,
                         channels=1, device=dev, dtype="float32")
            sd.wait()
        except Exception as exc:
            last_err = exc
            info = None
            continue                      # this one's asleep/busy — try the next
        device = dev
        break
    if rec is None or info is None:
        return {"ok": False, "device": order[0],
                "reason": f"no input device would open (tried {len(order)}): {last_err}"}
    fell_back = preferred is not None and device != preferred

    peak = float(np.max(np.abs(rec)))
    rms = float(np.sqrt(np.mean(rec ** 2)))
    signal = peak >= SILENCE_PEAK
    out = {"ok": True, "device": device, "name": info["name"],
           "rms": rms, "peak": peak, "signal_detected": signal}
    if fell_back:
        out["warning"] = (f"requested mic wouldn't open (asleep/disconnected) — "
                          f"fell back to {info['name']}.")
    if not signal:
        # Present and openable, but quiet during the probe — likely a
        # noise-gated headset (nothing to hear until you talk) or a muted mic.
        # Pass with a warning rather than block; the transcript is the arbiter.
        out["warning"] = (f"{info['name']} opened but was silent during the "
                          "check — if it's a noise-gated headset that's normal; "
                          "if the transcript comes back empty, it was muted.")
    return out


def find_ffmpeg() -> str:
    exe = _ffmpegbin.resolve()
    if not exe:
        raise RecorderError("ffmpeg not found on PATH — needed for screen capture")
    return exe


def list_windows(filter_text: str = "") -> list[dict]:
    """Visible top-level windows, for targeting gdigrab at the game."""
    if sys.platform != "win32":
        return []
    # ENCODING, PINNED AT BOTH ENDS. PowerShell 5.1 writes its output in the
    # console OEM codepage, and subprocess(text=True) decodes with the ANSI
    # codepage. Any window title containing a non-ASCII character therefore
    # arrived mangled: "downsizing · builders gate - Google Chrome" (U+00B7)
    # came back as "downsizing ú builders gate - Google Chrome", gdigrab could
    # not match it, and the failure surfaced as "that window probably doesn't
    # exist" — pointing at the game instead of at the decode.
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Get-Process | Where-Object { $_.MainWindowTitle } | "
        "Select-Object Id,ProcessName,MainWindowTitle | ConvertTo-Json -Compress"
    )
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, timeout=30,
                          stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    import json
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "[]")
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


def resolve_window(window_title: Optional[str] = None, *,
                   hints: Sequence[str] = ()) -> dict:
    """Decide what gdigrab actually points at, and say so out loud.

    gdigrab with no title records the ENTIRE DESKTOP — your inbox, your editor,
    whatever else was open — and every frame in the resulting bug report shows
    it. Worse, a title that no longer matches (the game was closed, or renamed)
    used to be indistinguishable from asking for the desktop on purpose.

    So: an explicit title that matches nothing RAISES, with the list of windows
    that do exist. No title falls back to the desktop, but the caller is handed
    the reason rather than left to assume.

    gdigrab matches the title exactly, so we return the real title off the
    window rather than the substring the user typed.
    """
    if sys.platform != "win32":
        # gdigrab is Windows-only anyway; there is nothing to enumerate against.
        return {"title": window_title, "whole_desktop": window_title is None,
                "matches": [],
                "note": "window enumeration is Windows-only — title passed through "
                        "unchecked"}
    try:
        visible = list_windows()
    except Exception as exc:                  # powershell missing/blocked
        return {"title": window_title, "whole_desktop": window_title is None,
                "matches": [],
                "note": f"could not enumerate windows ({exc}) — target unverified"}

    def _match(needle: str) -> list[dict]:
        low = needle.lower()
        return [w for w in visible
                if low in w["title"].lower() or low in w["process"].lower()]

    if window_title:
        matches = _match(window_title)
        if not matches:
            titles = ", ".join(repr(w["title"]) for w in visible[:12]) or "(none)"
            raise RecorderError(
                f"no visible window matches {window_title!r} — start the game "
                f"first, or pick from the open windows: {titles}. "
                "Refusing to silently record the whole desktop instead."
            )
        exact = [w for w in matches if w["title"] == window_title]
        chosen = (exact or matches)[0]
        return {"title": chosen["title"], "whole_desktop": False,
                "matches": matches,
                "note": f"capturing {chosen['title']!r} ({chosen['process']})"}

    for hint in hints:
        if not hint:
            continue
        matches = _match(hint)
        if matches:
            chosen = matches[0]
            return {"title": chosen["title"], "whole_desktop": False,
                    "matches": matches,
                    "note": f"auto-targeted {chosen['title']!r} from the project "
                            f"name {hint!r}"}

    return {"title": None, "whole_desktop": True, "matches": visible,
            "note": ("capturing the WHOLE DESKTOP — no window was named and none "
                     "matched the project name. Everything else on screen will "
                     "be in the recording.")}


# ---------------------------------------------------------------------------
# Where the window is — because gdigrab must NOT be pointed at it directly
# ---------------------------------------------------------------------------
# `gdigrab -i title=X` asks GDI for that window's device context. A Godot game
# does not draw into one: it renders through Vulkan/D3D and presents a swapchain
# the compositor owns, so GDI hands back the window's cleared background and
# nothing else. gdigrab then draws the mouse cursor on top itself — which is
# exactly the symptom, a black recording with a live cursor moving over it.
# Measured on this machine against the Godot editor: every pixel of a title=
# grab came back RGB(36,36,36), while a desktop grab of the same screen came
# back with a full 0-255 range.
#
# The composited desktop DOES have the game in it, because DWM has already
# flattened every GPU surface into it. So the capture is a desktop grab CROPPED
# to the window's rectangle, which keeps the reason the title targeting existed
# in the first place — an accidental desktop recording is somebody's inbox in a
# bug report — while capturing frames that are actually there.
#
# The alternative is ddagrab (Desktop Duplication API), which also works here
# and is cheaper on the GPU, but it needs a d3d11 hwdownload in the filter chain
# and fails outright over RDP and on some drivers. A cropped gdigrab has neither
# failure mode and needs nothing that is not already required.

_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN = 76, 77
_SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 78, 79
_DWMWA_EXTENDED_FRAME_BOUNDS = 9


def window_rect(title: str) -> Optional[dict]:
    """Where the window is, in ABSOLUTE virtual-screen coordinates, or None.

    Absolute, NOT relative to the virtual desktop's origin: when ``-video_size``
    is given, gdigrab assigns ``clip_rect.left = offset_x`` outright rather than
    adding it to that origin. Subtracting the origin first put the crop a full
    monitor-width to the right on a machine whose second screen sits to the left
    of the primary, and ffmpeg refused the input rather than recording the wrong
    thing — "Capture area extends outside window area".

    The rectangle comes from DWM's extended frame bounds rather than
    ``GetWindowRect``, which since Windows 10 includes an invisible ~8px resize
    border on every side. Measured on the Godot editor here: GetWindowRect says
    (-8,-8)-(1928,1040) for a window whose visible bounds are (0,0)-(1920,1032),
    and those eight pixels of overhang are themselves outside the desktop and
    enough to make ffmpeg refuse.

    Clamped to the virtual desktop as a backstop, because a window can be
    dragged half off-screen and a crop that leaves the canvas is a hard failure
    at open time, not a cosmetic one.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = ctypes.c_void_p()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _each(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value == title:
            found.value = hwnd
            return False           # stop enumerating
        return True

    user32.EnumWindows(_each, 0)
    if not found.value:
        return None
    hwnd = wintypes.HWND(found.value)

    rect = wintypes.RECT()
    try:
        ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect), ctypes.sizeof(rect)) == 0
    except Exception:               # no dwmapi: compositing off, or wine
        ok = False
    if not ok and not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None

    left, top = rect.left, rect.top
    right, bottom = rect.right, rect.bottom
    vx, vy = (user32.GetSystemMetrics(_SM_XVIRTUALSCREEN),
              user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
    vr = vx + user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
    vb = vy + user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
    left, top = max(left, vx), max(top, vy)
    right, bottom = min(right, vr), min(bottom, vb)

    # Even dimensions here, not just in the scale filter: an odd-width crop is
    # rejected when the input is opened, which is before any filter runs.
    width = (right - left) // 2 * 2
    height = (bottom - top) // 2 * 2
    if width <= 0 or height <= 0:   # minimised, or dragged fully off-screen
        return None
    return {"x": left, "y": top, "width": width, "height": height}


# The primary monitor, which is what "the screen" means to a person.
_SM_CXSCREEN, _SM_CYSCREEN = 0, 1


def primary_rect() -> Optional[dict]:
    """The PRIMARY monitor's rectangle, or None off Windows.

    THE FALLBACK MUST NOT BE THE VIRTUAL DESKTOP. `gdigrab -i desktop` with no
    crop captures every monitor stitched together - on a three-screen machine
    that is a recording of an editor, a browser and somebody's wallpaper, with
    the game a third of the way across. It was reported exactly that way: "I do
    not like how playtest records all screens."

    One screen is bounded, is what a person means by "record my screen", and is
    still honest about being wider than the game. The primary monitor's origin
    is (0,0) by definition on Windows, which is what makes this a two-metric
    call rather than a monitor enumeration.
    """
    if sys.platform != "win32":
        return None
    import ctypes

    try:
        user32 = ctypes.windll.user32
        width = int(user32.GetSystemMetrics(_SM_CXSCREEN))
        height = int(user32.GetSystemMetrics(_SM_CYSCREEN))
    except Exception:
        return None
    # Even dimensions, for the same reason window_rect enforces them: an odd
    # crop is rejected when the input is opened, before any filter runs.
    width, height = width // 2 * 2, height // 2 * 2
    if width <= 0 or height <= 0:
        return None
    return {"x": 0, "y": 0, "width": width, "height": height}


def _video_input(window_title: Optional[str], fps: int) -> tuple[list[str], str]:
    """The ffmpeg input args for the video stream, plus what they will capture.

    Always a desktop grab. When the window can be located it is cropped to that
    rectangle; when it cannot, the crop is dropped rather than the recording —
    a whole-desktop capture is embarrassing, a black one is useless.
    """
    args = ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1"]

    def _one_screen(why: str) -> tuple[list[str], str]:
        """Fall back to ONE monitor, never to every monitor at once."""
        screen = primary_rect()
        if not screen:
            return [*args, "-i", "desktop"], f"the whole desktop — {why}"
        return ([*args, "-offset_x", "0", "-offset_y", "0",
                 "-video_size", f"{screen['width']}x{screen['height']}",
                 "-i", "desktop"],
                f"the primary monitor ({screen['width']}x{screen['height']}) — {why}")

    if not window_title:
        return _one_screen("no window was named and none matched the project name")
    rect = window_rect(window_title)
    if not rect:
        return _one_screen(
            f"{window_title!r} could not be located, so the crop fell back to "
            "one screen rather than the recording being dropped")
    args += ["-offset_x", str(rect["x"]), "-offset_y", str(rect["y"]),
             "-video_size", f"{rect['width']}x{rect['height']}"]
    return ([*args, "-i", "desktop"],
            f"{window_title!r} at {rect['width']}x{rect['height']}, cropped out "
            "of the composited desktop")


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
    window_title: Optional[str] = None
    window_note: str = ""
    # What the video stream is ACTUALLY pointed at, which is not always what
    # window_note resolved — a window that cannot be located falls back to an
    # uncropped desktop, and that has to be visible rather than inferred.
    capture_note: str = ""
    mic_name: str = ""
    _proc: Optional[subprocess.Popen] = None
    _stream: object = None
    _frames: list = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _err: list = field(default_factory=list)
    # Live meter. Written from the audio callback, read from the HTTP thread —
    # hence the lock. A deque of per-block peaks, not the audio itself.
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _peaks: deque = field(default_factory=lambda: deque(maxlen=256))
    _rmss: deque = field(default_factory=lambda: deque(maxlen=256))
    _samples: int = 0
    _last_signal_at: float = 0.0


def start(out_dir: str | Path, *, window_title: Optional[str] = None,
          window_hints: Sequence[str] = (), mic_device: Optional[int] = None,
          fps: int = 30) -> Recording:
    """Begin capturing. Raises rather than returning a doomed session.

    window_title  gdigrab target. Must match a visible window or this raises.
    window_hints  tried in order when no title is given; the desktop is the
                  last resort, and rec.window_note says which happened.
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
    # Raises when an explicit title matches nothing — before a frame is written.
    window = resolve_window(window_title, hints=window_hints)
    window_title = window["title"]

    rec = Recording(out_dir=out)
    rec.video_path = out / "session.mp4"
    rec.audio_path = out / "session.wav"
    rec.started_at = time.time()
    rec.window_title = window_title
    rec.window_note = window["note"]
    rec.mic_name = probe.get("name", "")

    # --- video ---------------------------------------------------------
    # A CROPPED DESKTOP GRAB, never `title=`. See window_rect above for why
    # pointing gdigrab at a Godot window records a black rectangle.
    video_in, capture_note = _video_input(window_title, fps)
    rec.capture_note = capture_note
    cmd = [
        ffmpeg, "-y", "-loglevel", "warning",
        *video_in,
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
            f"ffmpeg died immediately (exit {rec._proc.returncode}) capturing "
            f"{capture_note}. {err[-400:]}"
        )

    # --- audio ---------------------------------------------------------
    def on_audio(indata, frames, time_info, status):
        if status:
            rec._err.append(str(status))
        rec._frames.append(indata.copy())
        # Meter the block here — it is already in cache, and the alternative is
        # re-scanning a growing array on every status poll.
        peak = float(np.max(np.abs(indata))) if frames else 0.0
        rms = float(np.sqrt(np.mean(indata ** 2))) if frames else 0.0
        now = time.time()
        with rec._lock:
            rec._peaks.append(peak)
            rec._rmss.append(rms)
            rec._samples += int(frames)
            if peak >= SILENCE_PEAK:
                rec._last_signal_at = now

    rec._stream = sd.InputStream(samplerate=MIC_RATE, channels=MIC_CHANNELS,
                                 device=mic_device, dtype="float32", callback=on_audio)
    rec._stream.start()
    rec.audio_started_at = time.time()
    rec._last_signal_at = rec.audio_started_at
    return rec


def level(rec: Recording) -> dict:
    """What the mic is hearing right now, for the live status panel.

    peak/rms are over the last ~LEVEL_WINDOW_S of audio, not the whole take, so
    the meter tracks the voice instead of slowly averaging it away.
    silent_for_s is the one number that matters: past ~20s of digital silence
    the session is recording nothing and should be stopped, not discovered dead
    at transcription time.
    """
    blocks = max(1, int(LEVEL_WINDOW_S * MIC_RATE / 512))
    with rec._lock:
        peaks = list(rec._peaks)[-blocks:]
        rmss = list(rec._rmss)[-blocks:]
        samples = rec._samples
        last_signal = rec._last_signal_at
    captured_s = round(samples / MIC_RATE, 2)
    peak = round(max(peaks), 6) if peaks else 0.0
    rms = round(sum(rmss) / len(rmss), 6) if rmss else 0.0
    started = rec.audio_started_at or rec.started_at or time.time()
    silent_for = round(max(0.0, time.time() - (last_signal or started)), 2)
    out = {
        "ok": True,
        "peak": peak,
        "rms": rms,
        "captured_s": captured_s,
        "elapsed_s": round(max(0.0, time.time() - (rec.started_at or time.time())), 2),
        "silent_for_s": silent_for,
        "signal": peak >= SILENCE_PEAK,
        "threshold": SILENCE_PEAK,
        "window_s": LEVEL_WINDOW_S,
        "device": rec.mic_name,
        "window_title": rec.window_title,
        "whole_desktop": rec.window_title is None,
        "warnings": rec._err[-3:],
    }
    if not peaks:
        out["warning"] = ("no audio blocks have arrived at all — the input "
                          "stream opened but is delivering nothing")
    elif not out["signal"]:
        out["warning"] = (f"digital silence for {silent_for:.0f}s — check the mic "
                          "is unmuted and selected before you lose the session")
    return out


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
