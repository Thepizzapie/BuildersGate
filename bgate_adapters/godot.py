"""Headless Godot adapter — build, run, and export from an agent's hands.

Windows binary note, MEASURED not assumed: Godot ships two exes, and the common
claim that the plain ``Godot_v*.exe`` loses stdout when piped is FALSE — verified
on 4.7.1, both binaries deliver identical stdout/stderr through a pipe. The
``_console.exe`` only exists to attach a console WINDOW for interactive
double-clicking; it is a ~200KB launcher that then spawns the real binary.

So we prefer the MAIN exe: same output, one less process between us and the
engine (a wrapper makes kills and timeouts leak grandchildren).

Godot ships as a portable single exe with no installer and no PATH entry, so
discovery has to look in Downloads and the usual per-user program dirs.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from glob import glob
from pathlib import Path
from typing import Optional

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Same rule as blender.py: a child that inherits a stdio MCP server's stdin
# blocks on the client's protocol channel forever. See mcp-subprocess-stdin.
def _spawn(cmd: list[str], timeout: int, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          cwd=cwd, stdin=subprocess.DEVNULL,
                          creationflags=_NO_WINDOW)


_SEARCH_GLOBS = (
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Godot\Godot*.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Godot\Godot*.exe"),
    os.path.expandvars(r"%USERPROFILE%\Downloads\Godot*.exe"),
    r"C:\Program Files\Godot\Godot*.exe",
    "/Applications/Godot.app/Contents/MacOS/Godot",
    "/usr/bin/godot",
    "/usr/local/bin/godot",
)

_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

# A failed unzip leaves a 0-byte .exe that looks installed and dies with a
# baffling "not recognized as a program". Observed on this machine. The threshold
# only needs to reject stubs — the real editor is ~170MB and the console launcher
# ~200KB, so anything under 64KB is debris, not a binary.
_MIN_BYTES = 64_000


class GodotNotFound(RuntimeError):
    pass


def _is_usable(path: str) -> bool:
    try:
        return Path(path).is_file() and Path(path).stat().st_size >= _MIN_BYTES
    except OSError:
        return False


def find_godot(prefer_console: bool = False) -> str:
    """Locate a usable Godot binary. BGATE_GODOT overrides everything.

    Prefers the newest MAIN exe. prefer_console picks the _console.exe launcher
    instead — only useful when a human wants a visible console window; it does
    NOT affect piped stdout (measured, see module docstring).
    """
    override = os.environ.get("BGATE_GODOT")
    if override:
        if not _is_usable(override):
            raise GodotNotFound(
                f"BGATE_GODOT points at a missing or empty file: {override}")
        return override

    found: list[str] = []
    for pattern in _SEARCH_GLOBS:
        if "*" in pattern:
            found.extend(p for p in glob(pattern) if _is_usable(p))
        elif _is_usable(pattern):
            found.append(pattern)

    on_path = shutil.which("godot")
    if on_path and _is_usable(on_path):
        found.append(on_path)

    if not found:
        raise GodotNotFound(
            "Godot not found. It ships as a portable .exe with no installer — "
            "extract it to %LOCALAPPDATA%\\Programs\\Godot, or set BGATE_GODOT. "
            "(A 0-byte .exe from a failed unzip is ignored on purpose.)"
        )

    def rank(path: str) -> tuple:
        name = Path(path).name.lower()
        console = "_console" in name
        match = _VERSION.search(name)
        version = tuple(int(g or 0) for g in match.groups()) if match else (0, 0, 0)
        # Console first when asked, then newest version.
        return (console == prefer_console, version)

    return sorted(found, key=rank)[-1]


def available() -> dict:
    try:
        path = find_godot()
    except GodotNotFound as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, "path": path}


def version() -> dict:
    exe = find_godot()
    proc = _spawn([exe, "--version"], timeout=60)
    raw = (proc.stdout or proc.stderr or "").strip().splitlines()
    return {"path": exe, "version": raw[-1] if raw else "unknown"}


def run_script(script: str, project_dir: Optional[str] = None,
               timeout: int = 120) -> dict:
    """Run a GDScript file headless (a SceneTree script) and capture output.

    The script must extend SceneTree or MainLoop and call quit(), or it will run
    until the timeout. Returns {ok, stdout, stderr, exit_code, seconds}.
    """
    import tempfile
    import time

    exe = find_godot()
    tmp = Path(tempfile.mkdtemp(prefix="bgate_godot_"))
    # Godot only loads scripts from inside a project when --path is given; for a
    # bare script run it reads the file directly.
    script_path = tmp / "agent_script.gd"
    script_path.write_text(script, encoding="utf-8")

    cmd = [exe, "--headless"]
    if project_dir:
        cmd += ["--path", str(project_dir)]
    cmd += ["--script", str(script_path)]

    started = time.monotonic()
    try:
        proc = _spawn(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Godot timed out after {timeout}s",
                "hint": "a SceneTree script must call quit() or it runs forever",
                "seconds": timeout}
    finally:
        elapsed = round(time.monotonic() - started, 2)

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    return {
        "ok": proc.returncode == 0 and "SCRIPT ERROR" not in stdout + stderr,
        "stdout": stdout[-8000:],
        "stderr": stderr[-4000:],
        "exit_code": proc.returncode,
        "seconds": elapsed,
        "errors": _errors(stdout + stderr),
    }


def check_project(project_dir: str, timeout: int = 180) -> dict:
    """Import/validate a project without opening the editor. The 'does it build'."""
    import time

    project = Path(project_dir)
    if not (project / "project.godot").exists():
        return {"ok": False, "error": f"no project.godot in {project_dir}"}

    exe = find_godot()
    started = time.monotonic()
    try:
        # --import builds the .godot cache and reports resource errors, then exits.
        proc = _spawn([exe, "--headless", "--path", str(project), "--import"],
                      timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"import timed out after {timeout}s"}

    output = (proc.stdout or "") + (proc.stderr or "")
    errors = _errors(output)
    return {
        "ok": proc.returncode == 0 and not errors,
        "exit_code": proc.returncode,
        "errors": errors,
        "seconds": round(time.monotonic() - started, 2),
        "output": output[-3000:],
    }


def _errors(output: str) -> list[str]:
    """Godot reports failures on stdout and keeps going; grep them out."""
    hits = []
    for line in output.splitlines():
        low = line.lower()
        if any(marker in low for marker in
               ("script error", "parse error", "error:", "failed to load",
                "can't open", "cannot open", "invalid")):
            clean = line.strip()
            if clean and clean not in hits:
                hits.append(clean)
    return hits[:20]
