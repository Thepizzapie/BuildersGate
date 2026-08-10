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

It also prints the EFFECTIVE SETTINGS and where each value came from
(:func:`settings_report`). That belongs next to the dependency rows for the same
reason they exist: the second-most expensive class of "why is this board not
doing what I told it" is not a missing binary, it is an env var in a shell
profile silently winning over what the panel shows. Nothing else in the tool
answers "what is this project actually configured to do" in one line, and it is
kept OUT of :func:`check` so the exit code keeps meaning "a dependency is
missing" — a setting that is merely non-default is not a failure.
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
#
# `art_key` REPLACED `openai_key`, and the rename is the fix rather than a
# tidy-up. The old row probed OPENAI_API_KEY and nothing else, so a project
# whose only credential was KREA_API_KEY — a key `.env.example` and the setup
# docs both tell people to set, and which every art tool will happily use — got
# `MISS openai_key` and a NON-ZERO EXIT while its setup was completely fine.
# That was documented as a known wart in CLAUDE.md and in three doc pages, which
# is what a wrong health check costs: everybody downstream writes a paragraph
# explaining when to ignore it. The row now asks bgate_core.providers, so it is
# green when ANY registered provider has a key and a third provider needs no
# edit here.
CHECKS = ("python", "art_key", "local_runtimes", "agent_cli", "ffmpeg",
          "ffprobe", "blender", "godot", "godot_web_templates", "whisper",
          "imageto3d", "local_image")

# What the code in this repo actually assumes, not aspirational floors.
# blender: the default warmup engine is BLENDER_EEVEE_NEXT, which is 4.2+.
# godot: the templates and the telemetry autoload are Godot 4 only.
# whisper: faster-whisper's segment API (word confidences) settled at 0.10.
MIN_REQUIRED = {
    "python": "3.10",
    "art_key": "",
    # Next to art_key on purpose: the two answer one question between them —
    # "can this project generate anything" — from the two directions it can be
    # answered from. art_key asks whether something is rented; this asks whether
    # something is running here. A project needs one of them, not both, and a
    # red row here is not a fault on a machine that never wanted local
    # generation.
    "local_runtimes": "",
    # No floor, but the least optional of the optional rows: the failure it
    # catches is a coding-agent CLI that is installed and registered and STILL
    # cannot reach the tools, because the registration names an interpreter
    # without Builders Gate in it. CLAUDE.md calls that the single most common
    # Windows failure, `claude mcp list` cannot see it, and the CLI's own error
    # ("failed to connect") points nowhere near the cause. Nothing else in this
    # report would go red for it.
    "agent_cli": "",
    "ffmpeg": "",
    "ffprobe": "",
    "blender": "4.2",
    "godot": "4.0",
    "godot_web_templates": "",
    "whisper": "0.10",
    # No floor. A red row here means image-to-3D is unavailable and every
    # other path still works — same status as ffmpeg or whisper. It reports a
    # GPU, not a binary, and it asks nvidia-smi rather than torch: this
    # machine's default interpreter carries torch 2.8.0+cpu, so a torch probe
    # would call a perfectly good RTX 3060 "no GPU" and never recover.
    "imageto3d": "",
    # Same status as imageto3d and for the same reason: a red row here means
    # LOCAL 2D generation is unavailable and every hosted path still works. It
    # is the row that makes "no API key" a configuration rather than a wall.
    "local_image": "",
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


def _probe_art_key() -> dict:
    """Is ANY art-generation provider configured?

    Presence only. Never print, hash, or validate a key against the API — a
    health check that spends money is a health check nobody runs, and one that
    prints the thing it is checking is worse than not having it.

    The provider list comes from the registry, so this row is green for a
    Krea-only project (it used to be red, and the exit code with it) and a third
    provider will not need a line here.

    IT IS ``art_providers()``, NOT ``PROVIDERS``, and the difference arrived
    with the first credential that generates nothing. Deepgram does speech in
    and out; counting it here would print a green art row for a project that
    cannot produce one image, which is the same lie in the other direction as
    the openai-only probe this function was written to replace.
    """
    try:
        from bgate_core import providers
    except Exception as exc:  # noqa: BLE001 - registry unimportable is its own red row
        return _missing("art_key", f"provider registry unavailable: {exc}")
    art = providers.art_providers()
    have = [one.env for one in art if (os.environ.get(one.env) or "").strip()]
    if not have:
        names = [one.env for one in art]
        listed = (" or ".join(names) if len(names) < 3
                  else ", ".join(names[:-1]) + " or " + names[-1])
        return _missing(
            "art_key",
            f"no art-generation key set — put one of {listed} in the project's "
            ".env (it is loaded from the project root) or the environment; "
            "local generation still works without any of them")
    # The NAMES of the configured variables, never their values or lengths.
    return _row(available=True, path=", ".join(have),
                version=f"{len(have)} of {len(art)} providers",
                min_required="")


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


def _probe_imageto3d() -> dict:
    """GPU capable of local image-to-3D, or the reason there is none.

    Imported inside the function on purpose. The adapter is careful never to
    pull torch, but doctor is on the startup path of every CLI invocation and
    an adapter import belongs behind the probe that needs it, not above it.
    """
    try:
        from bgate_adapters import imageto3d
    except Exception as exc:                       # adapter absent or broken
        return _missing("imageto3d", f"adapter unavailable: {exc}")
    row = imageto3d.doctor_row()
    return _row(available=bool(row.get("available")),
                path=row.get("path", ""), version=row.get("version", ""),
                min_required=row.get("min_required", ""),
                reason=row.get("reason", ""))


def _probe_local_image() -> dict:
    """A local ComfyUI that can paint 2D, or the reason there is none.

    Imported inside the function, same rule as _probe_imageto3d: doctor runs on
    the startup path of every CLI invocation and an adapter import belongs
    behind the probe that needs it.
    """
    try:
        from bgate_adapters import localgen
    except Exception as exc:
        return _missing("local_image", f"adapter unavailable: {exc}")
    row = localgen.doctor_row()
    ok = bool(row.get("available"))
    # `path` is what IS there, never what is missing — an unavailable row that
    # puts its reason in the path column reads as a found binary to every caller
    # that only checks whether the column is empty.
    return _row(available=ok, path=row.get("detail", "") if ok else "",
                min_required=MIN_REQUIRED["local_image"],
                reason="" if ok else row.get("detail", ""))


def _probe_local_runtimes() -> dict:
    """Which generators on THIS machine could run right now.

    Sits beside art_key because between them they answer "can this project make
    anything at all", from the two directions it can be answered from. A red row
    here means every local generator is either unset or not running; the hosted
    paths are untouched, which is why the reason says so.

    Root is not passed — like every probe here it reads the environment
    ``check()`` has already loaded the project's .env into.
    """
    try:
        from bgate_core import localruntimes
    except Exception as exc:                                     # noqa: BLE001
        return _missing("local_runtimes", f"registry unavailable: {exc}")
    row = localruntimes.doctor_row()
    ok = bool(row.get("available"))
    # `path` is what IS there, never what is missing — same rule as
    # _probe_local_image, for the same reason.
    return _row(available=ok, path=row.get("detail", "") if ok else "",
                min_required=MIN_REQUIRED["local_runtimes"],
                reason="" if ok else row.get("detail", ""))


def _probe_agent_cli() -> dict:
    """Is a coding-agent CLI installed AND actually wired to this interpreter.

    "Installed" was never the whole question and was the only half anything
    checked. A registration pointing at the wrong Python looks identical to a
    working one in ``claude mcp list``, fails at the first tool call, and
    reports "failed to connect".

    ``bgate_ui.agentcli`` lives on the UI side because it reads
    ``bgate_ui.runners``, which is where "which CLIs exist" is answered. The
    import is lazy and guarded — the same shape ``brainstorm`` and
    ``workflows`` already use to reach the UI layer from core, and neither that
    module nor ``runners`` pulls in FastAPI, so this costs a doctor run nothing.
    """
    try:
        from bgate_ui import agentcli
    except Exception as exc:                                     # noqa: BLE001
        return _missing("agent_cli", f"wiring registry unavailable: {exc}")
    row = agentcli.doctor_row()
    ok = bool(row.get("available"))
    return _row(available=ok, path=row.get("detail", "") if ok else "",
                min_required=MIN_REQUIRED["agent_cli"],
                reason="" if ok else row.get("detail", ""))


_PROBES: dict[str, Callable[[], dict]] = {
    "python": _probe_python,
    "art_key": _probe_art_key,
    "local_runtimes": _probe_local_runtimes,
    "agent_cli": _probe_agent_cli,
    "ffmpeg": _probe_ffmpeg,
    "ffprobe": _probe_ffprobe,
    "blender": _probe_blender,
    "godot": _probe_godot,
    "godot_web_templates": _probe_godot_web_templates,
    "whisper": _probe_whisper,
    "imageto3d": _probe_imageto3d,
    "local_image": _probe_local_image,
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


# ---------------------------------------------------------------------------
# Effective settings — the other half of "why is it behaving like that"
# ---------------------------------------------------------------------------
def settings_report(root: Optional[str] = None) -> list[dict]:
    """Every registered setting as ``{key, group, value, default, source, env,
    help}``, in registry order.

    Never raises and never opens a probe: a project whose DB is unreadable
    reports the defaults, because the point of the row is to show what the code
    will use, and with no store that IS the default.
    """
    try:
        from bgate_core import settings as _settings
        rows_out: list[dict] = []
        # Passing "" is deliberate rather than skipped: every store read in the
        # registry is individually guarded, so with no project the answer is
        # defaults plus whatever the environment forces — which is exactly what
        # somebody running `bgate doctor` outside a project needs to see.
        live = _settings.effective(root or "")
        for one in _settings.SETTINGS:
            got = live.get(one.key) or {}
            value = got.get("value", one.default)
            rows_out.append({
                "key": one.key,
                "group": one.group,
                "value": list(value) if isinstance(value, (list, tuple)) else value,
                "default": (list(one.default)
                            if isinstance(one.default, (list, tuple)) else one.default),
                "source": got.get("source", "default"),
                "env": got.get("env", ""),
                "help": one.help,
            })
        return rows_out
    except Exception:
        return []


def _render(value) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(part) for part in value) or "(none)"
    text = str(value)
    return text if text != "" else "(empty)"


def settings_lines(root: Optional[str] = None) -> list[str]:
    """The printable settings block: one line per setting, grouped.

    An overridden or non-default value is marked, because a wall of thirty rows
    in which everything looks the same is a wall nobody reads — the two facts
    worth finding here are "the environment took this away from you" and "this
    is not what ships by default".
    """
    rows_out = settings_report(root)
    if not rows_out:
        return ["settings   (registry unavailable)"]
    width = max(len(row["key"]) for row in rows_out)
    lines: list[str] = []
    group = ""
    for row in rows_out:
        if row["group"] != group:
            group = row["group"]
            lines.append(f"  {group}")
        if row["source"] == "env":
            note = f"  <- {row['env'] or 'env'}"
        elif row["source"] == "stored":
            changed = _render(row["value"]) != _render(row["default"])
            note = "  <- stored" + (f" (default {_render(row['default'])})"
                                    if changed else "")
        else:
            note = ""
        lines.append(f"    {row['key'].ljust(width)}  "
                     f"{_render(row['value'])}{note}")
    forced = [row["key"] for row in rows_out if row["source"] == "env"]
    if forced:
        lines.append("")
        lines.append(f"  {len(forced)} setting(s) forced by the environment: "
                     + ", ".join(forced))
    return lines


def print_settings(root: Optional[str] = None) -> None:
    """Print the settings block to stdout. Used by ``bgate doctor``."""
    print("effective settings  (env > stored > default)")
    for line in settings_lines(root):
        print(line)
