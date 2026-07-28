"""One aggregate dependency probe: can Builders Gate actually do its job here?

Answering "is ffmpeg on this machine" used to mean calling five different status
tools, and the playtest preflight answered it by OPENING THE MICROPHONE for 1.5s
and spawning a whisper probe subprocess — every 15 seconds, forever, for a
question no microphone can answer. So the cheap capability probes live here,
split away from anything that touches hardware: nothing in this module opens an
audio device, renders a frame, launches an engine, or downloads a model.

Discovery is NOT reimplemented here. blender.py / godot.py / transcribe.py
already know where their binaries hide on this machine and which BGATE_* env var
overrides them; doctor calls those and normalises the answers into one shape:

    {available, path, version, min_required, reason}   per dependency

Every probe is wall-clock bounded and returns a row instead of raising. A health
check that hangs is worse than one that says "unknown" — the caller is asking
BECAUSE something might be broken, and it must still get an answer.

Results are cached for a few seconds (CACHE_SECONDS) because the honest usage
pattern is a poll loop: a dashboard tick, a preflight, and an agent all asking
inside the same second should cost one probe, not three.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Optional

# Windows: never flash a console window out of a background health check.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# The order is the report order — cheap/local first, subprocess-spawning last.
CHECKS = ("python", "openai_key", "ffmpeg", "ffprobe", "blender", "godot",
          "godot_web_templates", "whisper")

# What the code in this repo actually assumes, not aspirational floors.
# blender: the default warmup engine is BLENDER_EEVEE_NEXT, which is 4.2+.
# godot: the templates and the telemetry autoload are Godot 4 only.
# whisper: faster-whisper's segment API (word confidences) settled at 0.10.
MIN_REQUIRED = {
    "python": "3.10",
    "openai_key": "",
    "ffmpeg": "",
    "ffprobe": "",
    "blender": "4.2",
    "godot": "4.0",
    "godot_web_templates": "",
    "whisper": "0.10",
}

CACHE_SECONDS = 5.0

# Per-probe wall clock. The underlying subprocess calls carry their own (shorter)
# timeouts; this is the backstop for a binary that ignores SIGTERM or a network
# filesystem that stalls a stat(). A probe over budget reports "timed out" and
# the aggregate still returns — the worker thread is abandoned, not awaited.
PROBE_BUDGET = 20.0

# A short timeout for `--version`: any binary that cannot print its own version
# in 15s is not going to survive a 4-minute import either.
_VERSION_TIMEOUT = 15

_NUMBERS = re.compile(r"(\d+(?:\.\d+)*)")

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def _row(*, available: bool, path: str = "", version: str = "",
         min_required: str = "", reason: str = "") -> dict:
    """Every dependency answers in exactly this shape, present or absent."""
    return {"available": bool(available), "path": path, "version": version,
            "min_required": min_required, "reason": reason}


def _as_tuple(text: str) -> tuple[int, ...]:
    """First dotted number in a version banner. ('Blender 4.5.1' -> (4,5,1))"""
    match = _NUMBERS.search(text or "")
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _too_old(name: str, version: str) -> str:
    """'' when the version is fine (or unreadable — never fail on a parse miss)."""
    floor = MIN_REQUIRED.get(name, "")
    if not floor:
        return ""
    found = _as_tuple(version)
    if not found:
        return ""
    want = _as_tuple(floor)
    if found[:len(want)] < want:
        return f"found {version.strip()}, which is below the minimum {floor}"
    return ""


def _finish(name: str, path: str, version: str) -> dict:
    """A found binary, downgraded to unavailable if it is too old to be usable."""
    stale = _too_old(name, version)
    return _row(available=not stale, path=path, version=version,
                min_required=MIN_REQUIRED.get(name, ""), reason=stale)


def _missing(name: str, reason: str) -> dict:
    return _row(available=False, min_required=MIN_REQUIRED.get(name, ""),
                reason=reason)


# ---------------------------------------------------------------------------
# The probes. One per dependency, each independent of every other — checking
# ffmpeg must never drag in the mic, and checking whisper must never load a model.
# ---------------------------------------------------------------------------
def _banner(exe: str) -> str:
    """First line of `exe -version`. Bounded, no shell, stdin closed.

    stdin=DEVNULL for the same reason the adapters do it: under a stdio MCP
    server, an inherited stdin is the client's protocol channel and the child
    will sit on it forever.
    """
    try:
        proc = subprocess.run([exe, "-version"], capture_output=True, text=True,
                              timeout=_VERSION_TIMEOUT, stdin=subprocess.DEVNULL,
                              creationflags=_NO_WINDOW)
    except Exception:
        return ""
    raw = (proc.stdout or proc.stderr or "").strip().splitlines()
    return raw[0].strip() if raw else ""


def _probe_python() -> dict:
    return _finish("python", sys.executable,
                   f"{platform.python_version()} ({platform.python_implementation()})")


def _probe_openai_key() -> dict:
    """Presence only. Never print, hash, or validate the key against the API —
    a health check that spends money is a health check nobody runs."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return _missing("openai_key",
                        "OPENAI_API_KEY not set — put it in the project's .env "
                        "(it is loaded from the project root) or the environment")
    return _row(available=True, path="OPENAI_API_KEY",
                version=f"set ({len(key)} chars)", min_required="")


def _probe_ffmpeg() -> dict:
    exe = shutil.which("ffmpeg")
    if not exe:
        return _missing("ffmpeg",
                        "ffmpeg not found on PATH — needed for screen capture, "
                        "frame extraction and playtest recording")
    banner = _banner(exe)  # "ffmpeg version 7.1-full_build-www.gyan.dev ..."
    match = re.search(r"version\s+(\S+)", banner)
    return _finish("ffmpeg", exe, match.group(1) if match else banner or "unknown")


def _probe_ffprobe() -> dict:
    exe = shutil.which("ffprobe")
    if not exe:
        return _missing("ffprobe",
                        "ffprobe not found on PATH — recordings can be made but "
                        "their duration cannot be read back")
    banner = _banner(exe)
    match = re.search(r"version\s+(\S+)", banner)
    return _finish("ffprobe", exe, match.group(1) if match else banner or "unknown")


def _probe_blender() -> dict:
    from bgate_adapters import blender

    probe = blender.available()
    if not probe.get("available"):
        return _missing("blender", probe.get("reason", "blender not found"))
    try:
        found = blender.version()
    except Exception as exc:
        return _row(available=True, path=probe.get("path", ""),
                    min_required=MIN_REQUIRED["blender"],
                    reason=f"found the binary but it would not report a version "
                           f"({type(exc).__name__}: {exc})")
    return _finish("blender", found.get("path", ""), found.get("version", ""))


def _probe_godot() -> dict:
    from bgate_adapters import godot

    probe = godot.available()
    if not probe.get("available"):
        return _missing("godot", probe.get("reason", "godot not found"))
    try:
        found = godot.version()
    except Exception as exc:
        return _row(available=True, path=probe.get("path", ""),
                    min_required=MIN_REQUIRED["godot"],
                    reason=f"found the binary but it would not report a version "
                           f"({type(exc).__name__}: {exc})")
    return _finish("godot", found.get("path", ""), found.get("version", ""))


def _probe_godot_web_templates() -> dict:
    """The Web export templates — what stands between a finished game and a URL.

    Separate from the `godot` row because they are a separate ~1GB download and
    a separate failure: the editor runs fine, `bgate publish` produces nothing,
    and the error Godot prints reads like a broken preset.
    """
    from bgate_adapters import godot

    probe = godot.export_templates("web")
    if not probe.get("available"):
        return _missing("godot_web_templates",
                        probe.get("reason", "web export templates not installed"))
    return _row(available=True, path=probe.get("path", ""),
                version=probe.get("version", ""))


def _probe_whisper() -> dict:
    """Is faster-whisper importable by the interpreter that will run it.

    This is the ONLY part of the old preflight worth polling: it asks a python
    a question. It does not open the mic, and it does not load a model.
    """
    from bgate_adapters import transcribe

    probe = transcribe.available()
    if not probe.get("available"):
        return _row(available=False, path=probe.get("python", ""),
                    min_required=MIN_REQUIRED["whisper"],
                    reason=probe.get("reason", "faster-whisper not importable"))
    return _finish("whisper", probe.get("python", ""), probe.get("version", ""))


_PROBES: dict[str, Callable[[], dict]] = {
    "python": _probe_python,
    "openai_key": _probe_openai_key,
    "ffmpeg": _probe_ffmpeg,
    "ffprobe": _probe_ffprobe,
    "blender": _probe_blender,
    "godot": _probe_godot,
    "godot_web_templates": _probe_godot_web_templates,
    "whisper": _probe_whisper,
}


def _run(name: str) -> dict:
    """A probe can never take the report down with it."""
    try:
        return _PROBES[name]()
    except Exception as exc:
        return _missing(name, f"probe failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# The one call everything else uses
# ---------------------------------------------------------------------------
def check(root: Optional[str] = None, *, refresh: bool = False) -> dict:
    """Probe every external dependency once and return one dict.

    root      a project root; its .env is loaded first so a key that lives with
              the project is seen. None = whatever is already in the environment.
    refresh   ignore the cache (for a human who just installed something).

    Returns {name: {available, path, version, min_required, reason}} for every
    name in CHECKS. Never raises. Never opens the microphone.
    """
    if root:
        try:
            from bgate_core import envfile
            envfile.load_project_env(root)
        except Exception:
            pass

    key = str(root or "")
    now = time.monotonic()
    if not refresh:
        with _lock:
            cached = _cache.get(key)
        if cached and now - cached[0] < CACHE_SECONDS:
            return {name: dict(row) for name, row in cached[1].items()}

    # Probes are independent, so run them together — the aggregate then costs
    # the slowest one rather than the sum. Threads are abandoned on budget
    # overrun (each underlying subprocess carries its own timeout and will die
    # on its own); waiting on a wedged probe is the failure being fixed here.
    report: dict[str, dict] = {}
    pool = ThreadPoolExecutor(max_workers=len(CHECKS),
                              thread_name_prefix="bgate-doctor")
    try:
        pending: dict[str, Future] = {name: pool.submit(_run, name) for name in CHECKS}
        deadline = time.monotonic() + PROBE_BUDGET
        for name, future in pending.items():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                report[name] = future.result(timeout=remaining)
            except Exception:
                report[name] = _missing(
                    name, f"probe did not answer within {PROBE_BUDGET:.0f}s — "
                          "treat as unavailable")
    finally:
        pool.shutdown(wait=False)

    with _lock:
        _cache[key] = (time.monotonic(), {n: dict(r) for n, r in report.items()})
    return report


def summary(report: dict) -> str:
    """One human line: what is missing, or that nothing is."""
    missing = [name for name in CHECKS if not report.get(name, {}).get("available")]
    if not missing:
        return f"all {len(CHECKS)} dependencies available"
    return f"{len(missing)} unavailable: " + ", ".join(missing)
