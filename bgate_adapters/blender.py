"""Headless Blender adapter — the agent's eyes on its own geometry.

An agent writes a bpy script, this runs it in ``blender --background``, and hands
back tri counts, UV warnings, materials, and optionally a render. That return
trip is the whole point: bpy is an unforgiving generation target, and an agent
that cannot see what it made will confidently produce nothing.

Blender is discovered from BGATE_BLENDER, then PATH, then the usual install dirs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from glob import glob
from pathlib import Path
from typing import Optional

# Windows: keep every subprocess from flashing a console window.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Every Blender launch MUST go through this.
#
# stdin=DEVNULL is load-bearing, not hygiene. When the MCP server runs over
# stdio, its stdin IS the client's protocol channel. A child that inherits it
# blocks reading a stream meant for the server — Blender then sits at ~0% CPU
# forever, which reads as "slow render" and gets misdiagnosed as a GPU stall.
# It can also steal bytes off the wire and corrupt the session.
#
# Symptom to remember: works standalone (stdin is a terminal), hangs under the
# server (stdin is a pipe nobody will ever write to).
def _spawn(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )

RUNNER = Path(__file__).with_name("_blender_runner.py")

ENGINES = ("BLENDER_WORKBENCH", "BLENDER_EEVEE_NEXT", "CYCLES")
DEFAULT_ENGINE = "BLENDER_WORKBENCH"  # fast, GPU-optional — a preview, not a beauty pass

# Measured on this machine (Blender 4.5, Windows): the FIRST EEVEE render after a
# cold boot blew past a 240s timeout; every run after it took 1-12s, and the same
# script that timed out later ran in 1.4s. Clearing Blender's own gl-shader-cache
# did NOT bring the stall back, so the warmup is below Blender — GPU driver shader
# cache or the OS first-loading its GPU DLLs. Cause unconfirmed; the cost is real.
#
# So: warmup() pays it once on purpose, and the first GPU-engine call gets a
# generous timeout. Never let an agent's first render be the one that eats this.
COLD_START_TIMEOUT = 420
GPU_ENGINES = ("BLENDER_EEVEE_NEXT", "CYCLES")

_warmed: set[str] = set()

# EEVEE_NEXT — the engine warmup() defaults to and the one every GPU path in
# this adapter names — landed in Blender 4.2 and does not exist before it, so a
# 4.1 install is not "old", it is unusable here. bgate_core.doctor declares the
# same floor in MIN_REQUIRED; the two must stay equal or the health check and
# the adapter will disagree about the same binary.
MIN_VERSION = (4, 2)

_SEARCH_GLOBS = (
    r"C:\Program Files\Blender Foundation\Blender *\blender.exe",
    r"C:\Program Files (x86)\Blender Foundation\Blender *\blender.exe",
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "/usr/bin/blender",
    "/usr/local/bin/blender",
    "/snap/bin/blender",
)


class BlenderNotFound(RuntimeError):
    pass


# Blender's Windows layout puts the version in a directory name
# ("C:\Program Files\Blender Foundation\Blender 4.5\blender.exe"), and the Linux
# packages sometimes do too ("/opt/blender-4.5.1/blender").
_DIR_VERSION = re.compile(r"blender[ _\-]*(\d+)\.(\d+)(?:\.(\d+))?", re.I)


def _path_version(path: str) -> tuple[int, ...]:
    """Version read out of the install PATH, or () when it carries none.

    From the name, never from `blender --version`: discovery is called by health
    polls and pytest skipifs, and spawning a process per candidate just to rank
    the list would turn a stat() into seconds. Unversioned layouts (a PATH entry,
    a custom dir) come back () and sort lowest — a known-good 4.5 beats a guess.
    """
    for part in reversed(Path(path).parts):
        match = _DIR_VERSION.search(part)
        if match:
            return tuple(int(g) for g in match.groups() if g is not None)
    return ()


def _pretty(version: tuple[int, ...]) -> str:
    return ".".join(str(n) for n in version)


def find_blender() -> str:
    """Locate the Blender executable. Newest usable version wins.

    "Newest" is by PARSED version, not by string: sorted() on the raw paths puts
    "Blender 4.10" BEFORE "Blender 4.9" (and "3.6" after "4.5" the moment a
    layout stops padding), so the lexicographic sort this used to do would hand
    an agent the older install and then blame Blender for the missing feature.

    Anything below MIN_VERSION is not a candidate — but it is not silence
    either: a too-old install is reported AS too old, with its version, because
    "Blender not found" sends the user hunting for an install they already have.
    """
    override = os.environ.get("BGATE_BLENDER")
    if override:
        if not Path(override).exists():
            raise BlenderNotFound(f"BGATE_BLENDER points at a missing file: {override}")
        return override

    on_path = shutil.which("blender")
    if on_path:
        return on_path

    found: list[str] = []
    for pattern in _SEARCH_GLOBS:
        found.extend(glob(pattern) if "*" in pattern else
                     ([pattern] if Path(pattern).exists() else []))

    if found:
        ranked = sorted(found, key=_path_version)
        usable = [p for p in ranked
                  if not _path_version(p) or _path_version(p) >= MIN_VERSION]
        if usable:
            return usable[-1]
        newest = ranked[-1]
        raise BlenderNotFound(
            f"Blender {_pretty(_path_version(newest))} is installed at {newest}, "
            f"but this adapter needs {_pretty(MIN_VERSION)} or newer — "
            "BLENDER_EEVEE_NEXT (the render engine every GPU path here uses) "
            f"does not exist before {_pretty(MIN_VERSION)}. Upgrade Blender, or "
            "point BGATE_BLENDER at a newer build."
        )

    raise BlenderNotFound(
        "Blender not found. Install it, put it on PATH, or set BGATE_BLENDER "
        "to the executable path."
    )


def available() -> dict:
    """Probe without running anything heavy — for health checks and tool errors."""
    try:
        path = find_blender()
    except BlenderNotFound as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, "path": path}


def version() -> dict:
    exe = find_blender()
    proc = _spawn([exe, "--version"], timeout=60)
    first = (proc.stdout or "").strip().splitlines()
    return {"path": exe, "version": first[0] if first else "unknown"}


def run_script(script: str, *, blend_file: Optional[str] = None,
               render: bool = False, out_dir: Optional[str] = None,
               engine: str = DEFAULT_ENGINE, timeout: int = 180,
               factory_startup: bool = True,
               export_glb: Optional[str] = None) -> dict:
    """Execute a bpy script headless and report what came back.

    script         bpy source. `bpy` is pre-imported; importing it again is fine.
    blend_file     open this .blend first; None starts from an empty scene
                   (no default cube — scripts should build what they mean).
    render         also render the active camera to a PNG.
    export_glb     also export the scene to this .glb (modifiers applied).
    out_dir        where renders land. Defaults to <cwd>/.bgate_out.
    engine         BLENDER_WORKBENCH (default, fast) | BLENDER_EEVEE_NEXT | CYCLES.

    Returns {ok, error, traceback, print, scene:{objects,totals,materials,...},
             issues:[game-readiness problems], glb:{...}, render:{...},
             exit_code, seconds}. A failing SCRIPT is a normal result with
     ok=False — not an exception. A failing BLENDER (missing binary, timeout,
     unparseable result) raises or reports ok=False with a reason.
    """
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")

    # First GPU-engine render in this process may hit the cold-start stall (see
    # COLD_START_TIMEOUT). Give it room rather than reporting a bogus failure.
    if render and engine in GPU_ENGINES and engine not in _warmed:
        timeout = max(timeout, COLD_START_TIMEOUT)

    exe = find_blender()
    out = Path(out_dir or (Path.cwd() / ".bgate_out"))
    out.mkdir(parents=True, exist_ok=True)

    # Everything from here down lives inside the try: the rmtree used to sit on
    # the last line, so the ONLY path that cleaned up was total success. A
    # timeout, a crashed Blender, an unreadable result.json, a missing
    # blend_file — every failure mode left its scratch dir in %TEMP%, and
    # failures are exactly what an agent produces in bulk while iterating.
    tmp = Path(tempfile.mkdtemp(prefix="bgate_blender_"))
    try:
        script_path = tmp / "agent_script.py"
        result_path = tmp / "result.json"
        script_path.write_text(script, encoding="utf-8")

        render_path = str(out / "render.png") if render else "-"
        glb_path = "-"
        if export_glb:
            glb_path = str(Path(export_glb).resolve())
            Path(glb_path).parent.mkdir(parents=True, exist_ok=True)

        cmd = [exe, "--background"]
        if blend_file:
            if not Path(blend_file).exists():
                raise FileNotFoundError(f"blend_file not found: {blend_file}")
            cmd.append(str(blend_file))
        if factory_startup:
            # Ignore the user's startup file and addons: agent runs must be
            # reproducible, and a stray addon changing defaults is a nightmare to
            # diagnose from a tool result.
            cmd.append("--factory-startup")
        cmd += ["--python", str(RUNNER), "--",
                str(script_path), str(result_path), render_path, engine, glb_path]

        import time
        started = time.monotonic()
        try:
            proc = _spawn(cmd, timeout=timeout)
        except subprocess.TimeoutExpired:
            hint = "infinite loop, a modal operator waiting on a UI, or a heavy render"
            if engine in GPU_ENGINES:
                hint = (f"{engine}'s first render on a cold machine can take minutes "
                        "(GPU shader warmup). Call warmup() once, use BLENDER_WORKBENCH "
                        "for iteration, or raise timeout.")
            return {"ok": False, "error": f"Blender timed out after {timeout}s",
                    "hint": hint, "seconds": timeout}
        finally:
            elapsed = round(time.monotonic() - started, 2)

        if render and engine in GPU_ENGINES:
            _warmed.add(engine)

        if not result_path.exists():
            # Blender died before the runner could write anything — a crash, a bad
            # .blend, or a startup failure. Surface its own words.
            return {
                "ok": False,
                "error": "Blender exited without producing a result",
                "exit_code": proc.returncode,
                "stderr": (proc.stderr or "")[-2000:],
                "stdout": (proc.stdout or "")[-1000:],
                "seconds": elapsed,
            }

        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"unreadable result from Blender: {exc}",
                    "exit_code": proc.returncode, "seconds": elapsed}

        result["exit_code"] = proc.returncode
        result["seconds"] = elapsed
        return result
    finally:
        # ignore_errors: a Blender killed on timeout can still hold a handle for
        # a beat on Windows, and a failed cleanup must not mask the result.
        shutil.rmtree(tmp, ignore_errors=True)


def warmup(engine: str = "BLENDER_EEVEE_NEXT", out_dir: Optional[str] = None) -> dict:
    """Pay the GPU cold-start cost once, on purpose, at a time of your choosing.

    Renders a trivial 32px scene. Do this at pipeline start (or after a reboot)
    so no agent's real render is the one that eats a multi-minute stall.
    """
    if engine not in GPU_ENGINES:
        return {"ok": True, "warmed": False, "reason": f"{engine} needs no warmup"}

    script = """
import bpy
bpy.ops.mesh.primitive_plane_add(size=1)
bpy.context.scene.render.resolution_x = 32
bpy.context.scene.render.resolution_y = 32
"""
    import time
    started = time.monotonic()
    got = run_script(script, render=True, engine=engine, out_dir=out_dir,
                     timeout=COLD_START_TIMEOUT)
    return {
        "ok": bool(got.get("ok")),
        "warmed": bool(got.get("render", {}).get("rendered")),
        "engine": engine,
        "seconds": round(time.monotonic() - started, 2),
        "error": got.get("error"),
    }


def scene_stats(blend_file: str, timeout: int = 120) -> dict:
    """Report a .blend without changing it — the read-only path."""
    return run_script("pass", blend_file=blend_file, timeout=timeout,
                      factory_startup=True)


def export_gltf(out_path: str, *, blend_file: Optional[str] = None,
                script: str = "pass", timeout: int = 240) -> dict:
    """Export a .blend (or a script-built scene) to .glb for the engine.

    Modifiers are applied — Blender's exporter defaults that OFF, which silently
    ships the base mesh and makes an asset look right in Blender and wrong in the
    engine. Returns the run result plus glb{exported, bytes} and the
    game-readiness issues worth fixing before this reaches a level.
    """
    return run_script(script, blend_file=blend_file, export_glb=out_path,
                      timeout=timeout)
