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
from . import ffmpegbin as _ffmpegbin

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
# explaining when to ignore it. The row now asks bgate_core.runtime.providers, so it is
# green when ANY registered provider has a key and a third provider needs no
# edit here.
CHECKS = ("python", "art_key", "local_runtimes", "agent_cli", "ffmpeg",
          "ffprobe", "blender", "godot", "godot_web_templates", "whisper",
          "imageto3d", "local_image", "aseprite", "anim_library")

# Rows that SUMMARISE A REGISTRY rather than probe one binary on PATH.
#
# Every other row answers "is this executable here, and what version": one
# lookup, one path, and a test can make it present or absent by controlling
# PATH. These two ask a registry how many of N things are wired, so there is no
# single binary to stub and no single path to report. A test that treats them
# like the others either fails on a machine that happens to have a coding-agent
# CLI installed, or asserts a path that was never going to exist.
#
# Named here rather than in the tests so the list cannot drift from CHECKS: the
# absent-binaries test already learned that lesson once, when adding the
# imageto3d row broke a hand-written count that passed on the two machines it
# was written on.
# anim_library is the third: it asks a cache directory how many packs are
# unpacked, so there is no binary a test can put on PATH and no path to stub.
SUMMARY_CHECKS = ("local_runtimes", "agent_cli", "anim_library")

# What the code in this repo actually assumes, not aspirational floors.
# blender: the default warmup engine is BLENDER_EEVEE_NEXT, which is 4.2+.
# godot: the templates and the telemetry autoload are Godot 4 only.
# whisper: faster-whisper's segment API (word confidences) settled at 0.10.
MIN_REQUIRED = {
    # 3.11 because pyproject.toml's requires-python is >=3.11: a floor of 3.10
    # here made `bgate doctor` report a green python row on an interpreter pip
    # then REFUSES to install on, which is a false pass in the one check a
    # first-time user reads before anything else works.
    "python": "3.11",
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
    # The Lua surface this project scripts (json global, tag.repeats,
    # --script-param) is a 1.3 feature set; a 1.2 install reads as too old,
    # not as broken scripts. Optional row: red costs .aseprite masters and
    # palette derivation, nothing else.
    "aseprite": "1.3",
    # No floor and no binary: a CC0 clip pack in ~/.bgate/animlib. Red means
    # blender_animate can still author its procedural clips and cannot yet
    # retarget hand-keyed ones; the row names the one command that fixes it.
    "anim_library": "",
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
        from . import providers
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

    # WHAT THE ROUTER WOULD ACTUALLY DO, not how many variables are set.
    #
    # THIS ROW SAID "4 of 4 providers" ON A MACHINE THE GENERATION GATEWAY
    # DESCRIBED AS openai-unkeyed, krea-unkeyed, one live option, no
    # alternatives. Measured across three benchmark games. Both statements were
    # derived from the same environment and only one of them was the answer to
    # the question a human asks a health check, because a key without its
    # adapter's package - or exported empty, or belonging to a provider whose
    # library is not installed - is a set variable and not a usable provider.
    #
    # `providers.usable` borrows each ADAPTER's own available() verdict, which
    # is the same offline truth `gateway._probe` reads for `keyed`, so this row
    # and provider_status cannot disagree about who can run. It deliberately
    # stops there: `drained` needs a balance probe over the network and a
    # doctor row must never spend a round trip, so the row names
    # provider_status as the live read rather than guessing at it.
    try:
        live = providers.usable()
    except Exception:  # noqa: BLE001 - an adapter blowing up is not a red row
        live = []
    usable = [one.env for one in art if one.id in live]
    if not usable:
        return _missing(
            "art_key",
            f"{len(have)} art key(s) are SET ({', '.join(have)}) and none is "
            "usable — the adapter behind each refuses (missing package, empty "
            "value, or unreachable). provider_status names which and why; "
            "nothing will generate until one of them answers")
    # The NAMES of the usable variables, never their values or lengths.
    detail = f"{len(usable)} of {len(art)} providers usable"
    if len(usable) < len(have):
        detail += f" ({len(have)} keyed)"
    return _row(available=True, path=", ".join(usable),
                version=detail, min_required="")


def _probe_ffmpeg() -> dict:
    exe = _ffmpegbin.resolve()
    if not exe:
        return _missing("ffmpeg",
                        "ffmpeg not found on PATH — needed for screen capture, "
                        "frame extraction and playtest recording")
    banner = _banner(exe)  # "ffmpeg version 7.1-full_build-www.gyan.dev ..."
    match = re.search(r"version\s+(\S+)", banner)
    version = match.group(1) if match else banner or "unknown"

    # WHICH ffmpeg, NOT JUST WHETHER. libtheora is an OPTIONAL build flag and
    # several distributions ship an ffmpeg without it. Such a build passes every
    # check above, records playtests perfectly, and then fails at the one thing
    # cutscenes need — writing the Ogg Theora that is the only format Godot
    # plays — after a whole sequence has been generated and paid for.
    #
    # IT DOES NOT TURN THE ROW RED, and it does not add a column. Screen
    # capture, frame extraction and recording are what this row has always
    # meant and all of them work without libtheora, so a red row would tell a
    # project that ships no cutscenes that its setup is broken. And every
    # dependency answers in exactly _row's shape — a `theora` key here would be
    # one row wider than the other eleven, which every consumer of this report
    # would have to special-case. So it rides in the version string, where it is
    # visible in the doctor table and costs nothing.
    #
    # A BROKEN libtheora IS THE OTHER CASE AND IT DOES GO RED, which is not an
    # inconsistency with the paragraph above — it is the same rule applied to a
    # different fact. An ABSENT libtheora removes a capability this row never
    # promised, and it announces itself: the encode fails loudly with "Unknown
    # encoder". A libtheora that is present and writes files nothing can decode
    # announces nothing at all. It exits 0, the file is the right size, and the
    # cutscene is flat green rectangles in the shipped game — which is exactly
    # what happened, for the entire life of this product, while this row was
    # green (GyanD/codexffmpeg issue #200; cinematic.ffmpeg_status carries the
    # measurement and the remedy). Silent corruption is the one thing a health
    # check exists for, and no other row here would ever say a word about it.
    try:
        from ..cine import cinematic as _cine

        encoder = _cine.ffmpeg_status()
        # Only when the probe actually RAN. A binary that could not be executed
        # tells us nothing about its build, and "no libtheora" is a claim, not a
        # default — asserting it about an encoder we never reached would send a
        # user to fix something that is not broken.
        if encoder.get("probed") and not encoder.get("theora"):
            version += " (no libtheora — cannot write the Ogg Theora Godot " \
                       "plays, so generated cutscenes cannot be delivered)"
        elif encoder.get("theora") and not encoder.get("ok") \
                and (encoder.get("roundtrip") or {}).get("ran"):
            # `ran` is required: an encoder we could not exercise is UNKNOWN,
            # and telling somebody to replace a build we never managed to test
            # is the same mistake as claiming it has no libtheora.
            errors = int((encoder.get("roundtrip") or {}).get("errors") or 0)
            return _row(
                available=False, path=exe,
                version=f"{version} (libtheora present but BROKEN — "
                        f"{errors} decode error(s) round-tripping one second of "
                        "video)",
                min_required=MIN_REQUIRED.get("ffmpeg", ""),
                reason=encoder.get("reason", ""))
    except Exception:                                            # noqa: BLE001
        pass    # a probe that cannot answer must not take the doctor down
    return _finish("ffmpeg", exe, version)


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


def _probe_aseprite() -> dict:
    """Aseprite — .aseprite masters, hand-edit round trips, palette derivation.

    Optional in the same sense as blender: a red row costs the features that
    need it and nothing else. It is also a PAID product, so unlike ffmpeg
    there is no fetch button — the fix is an install or BGATE_ASEPRITE.
    """
    from bgate_adapters import aseprite

    probe = aseprite.available()
    if not probe.get("available"):
        return _missing("aseprite", probe.get("reason", "aseprite not found"))
    try:
        found = aseprite.version()
    except Exception as exc:
        return _row(available=True, path=probe.get("path", ""),
                    min_required=MIN_REQUIRED["aseprite"],
                    reason=f"found the binary but it would not report a version "
                           f"({type(exc).__name__}: {exc})")
    return _finish("aseprite", found.get("path", ""), found.get("version", ""))


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


def _probe_anim_library() -> dict:
    """Which CC0 animation packs are fetched. Optional, like imageto3d."""
    try:
        from bgate_adapters import animlib
    except Exception as exc:
        return _missing("anim_library", f"adapter unavailable: {exc}")
    row = animlib.doctor_row()
    return _row(available=bool(row.get("available")), path=row.get("path", ""),
                version=row.get("version", ""),
                min_required=MIN_REQUIRED["anim_library"],
                reason=row.get("reason", ""))


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
        from . import localruntimes
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

    ``bgate_ui.agents.agentcli`` lives on the UI side because it reads
    ``bgate_ui.agents.runners``, which is where "which CLIs exist" is answered. The
    import is lazy and guarded — the same shape ``brainstorm`` and
    ``workflows`` already use to reach the UI layer from core, and neither that
    module nor ``runners`` pulls in FastAPI, so this costs a doctor run nothing.
    """
    try:
        from bgate_ui.agents import agentcli
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
    "anim_library": _probe_anim_library,
    "local_image": _probe_local_image,
    "aseprite": _probe_aseprite,
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
    # Both layers, and the machine-wide one even with no project: "does this
    # machine have an art provider" is the question the art_key row asks, and it
    # had a right answer long before any particular game existed.
    try:
        from ..store import envfile
        envfile.load_env(root)
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

    # A DISABLED MODULE'S DEPENDENCY IS NOT A FAULT. A project that switched
    # playtest capture and 3D off must not open doctor to red rows about
    # ffmpeg and Blender — a report that grades features you deliberately
    # declined teaches you to ignore the report. Rows are marked, not
    # removed, so "why isn't blender listed" has an answer on the row itself;
    # a row two modules share stays graded while either is on.
    if root:
        try:
            from ..store import modules as _modules

            off = _modules.disabled(root)
            for name, row in report.items():
                if off and not _modules.doctor_row_enabled(name, off):
                    # The probe's finding stands (available says what is
                    # true); the marker says nobody here needs it.
                    row["module_disabled"] = True
                    row["reason"] = ((row.get("reason") or "")
                                     + " (module disabled — not required for "
                                       "this project)").strip()
        except Exception:
            pass
    return report


def project_report(root: Optional[str] = None) -> list[dict]:
    """Project-level configuration faults — the ones no binary probe can see.

    SEPARATE FROM ``CHECKS`` ON PURPOSE. Every row there answers "is this
    executable on this machine", is cached per machine, and has a test contract
    that counts it. These rows answer "is THIS PROJECT wired correctly", need a
    root, and are cheap enough to recompute every time.

    Two faults, both of which used to be invisible until an agent hit them:
    lanes that describe a directory layout the project does not have (every
    dispatched agent then refused on contact with the source tree), and lane
    rules that are not being enforced at all because the hook was never
    installed — adopt does not install it, so that is the out-of-box state.

    Returns [{name, ok, detail, fix}]. Never raises: a diagnostic that dies
    takes the whole doctor run with it.
    """
    out: list[dict] = []
    if not root:
        return out
    try:
        from ..board import seats as _seats
        layout = _seats.detect_layout(root)
        owned = _seats.lane_owners(root, layout["prefix"] + "scenes/x.tscn")
        ok = bool(owned)
        out.append({
            "name": "seat_lanes",
            "ok": ok,
            "detail": (
                f"lanes cover this project's layout (game lives at "
                f"{layout['prefix'] or 'the project root'})" if ok else
                f"NO SEAT owns {layout['prefix'] or ''}scenes/** — the seat "
                "lanes describe a layout this project does not have, so every "
                "dispatched agent is refused on contact with the source tree"),
            "fix": ("" if ok else
                    "bgate adopt (re-runs layout detection and rewrites the "
                    "lanes), or set them by hand with seat_configure"),
        })
    except Exception as exc:
        out.append({"name": "seat_lanes", "ok": False,
                    "detail": f"could not read the seat table: {exc}",
                    "fix": ""})
    try:
        from bgate_cli import hook as _hook
        state = _hook.selftest(root)
        live = bool(state.get("installed") and state.get("enforcing"))
        out.append({
            "name": "lane_hook",
            "ok": live,
            "detail": ("lane and lock enforcement is live" if live else
                       "the PreToolUse hook is not installed here, so lanes "
                       "and locks are advisory — agents can write anywhere"),
            "fix": "" if live else f"bgate hook-install {root}",
        })
    except Exception as exc:
        out.append({"name": "lane_hook", "ok": False,
                    "detail": f"could not probe the hook: {exc}", "fix": ""})
    return out


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
        from ..store import settings as _settings
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
