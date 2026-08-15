"""Spawning and killing child processes, with the Windows details in one place.

TWO THINGS KEPT GOING WRONG, both of them invisible on the developer machine
that wrote the code and both of them ugly on a user's.

CONSOLE WINDOWS. A PyInstaller windowed build has no console, so every
subprocess Windows starts for it gets a BRAND NEW one -- a black box that
flashes onto the desktop, steals focus, and vanishes. One is a blink. The
playtest path spawns git four times per iteration record and twice more for the
build identity, and the report was "30 command prompts fly open when you hit
record". The fix is one flag, CREATE_NO_WINDOW, and the reason it kept being
missed is that it has to be remembered at all ten call sites and its absence
does nothing at all when you run from a terminal.

So the flag does not live at the call sites any more. :func:`run` and
:func:`popen` are thin wrappers that always pass it, and the rule is simply
that nothing in this codebase calls subprocess directly.

ORPHANS. `Popen.terminate()` ends the process you started and nothing it
started in turn. Godot launches its own children, and ffmpeg on Windows is
often reached through a launcher; terminating the parent leaves those running,
holding the capture file open and, in the game's case, sitting on screen after
the user pressed stop. :func:`kill_tree` kills the whole tree, which is what
"stop" has always meant to the person clicking it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, Optional

WINDOWS = sys.platform == "win32"

# CREATE_NO_WINDOW. Console programs started by a GUI process get their own
# console unless told otherwise; this is the "otherwise".
NO_WINDOW = 0x08000000 if WINDOWS else 0


def _flags(kwargs: dict) -> dict:
    """Merge NO_WINDOW into whatever creationflags a caller asked for."""
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | NO_WINDOW
    if not WINDOWS:
        # creationflags is Windows-only; on POSIX start a new process group so
        # kill_tree() has something to aim at.
        kwargs.pop("creationflags", None)
        kwargs.setdefault("start_new_session", True)
    return kwargs


def run(cmd, **kwargs: Any) -> subprocess.CompletedProcess:
    """subprocess.run that never flashes a console window.

    stdin defaults to DEVNULL: a child that inherits a closed or absent stdin
    and then reads from it blocks forever, and in a windowed build there is no
    console for anyone to notice it in.
    """
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(cmd, **_flags(kwargs))


def popen(cmd, **kwargs: Any) -> subprocess.Popen:
    """subprocess.Popen that never flashes a console window."""
    return subprocess.Popen(cmd, **_flags(kwargs))


def kill_tree(proc: Optional[subprocess.Popen], timeout: float = 3.0) -> bool:
    """End a process AND everything it started. True if anything was killed.

    Returns False for a process that was already gone, so a caller can report
    what it actually stopped rather than what it attempted.

    NEVER RAISES. This is called from stop paths and from the panic button, and
    a stop that fails because one of several children had already exited is not
    a stop the user can act on -- it is a stop that leaves the rest running.
    """
    if proc is None or proc.poll() is not None:
        return False

    if WINDOWS:
        # taskkill /T walks the child tree, which terminate() does not. /F
        # because a game mid-frame does not process WM_CLOSE promptly and the
        # user has already said they want it gone.
        try:
            run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
    return True
