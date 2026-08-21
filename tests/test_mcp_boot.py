"""The server must BOOT the way registrations run it, not just import.

`claude mcp add ... -- <python> -m bgate_mcp.server` is the documented
registration, so `python -m bgate_mcp.server` is the one command every user's
MCP client actually executes. Under `-m` the module runs as `__main__`, and
when the domain modules import `bgate_mcp.server` back, Python starts a
second execution of server.py under its package name — which reached the
star imports while tools_blender was half-initialized and killed the boot
with a circular ImportError. Every test imported the module normally, so CI
stayed green while every registration on every machine got CONNECTION_CLOSED
and agents concluded the toolset was "not attached".

This test runs the real command: subprocess, closed stdin. A stdio MCP
server that boots cleanly reads EOF and exits 0; the broken one died with a
traceback before serving a byte.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys


def test_the_registered_command_boots_and_exits_cleanly_on_eof():
    repo = pathlib.Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "bgate_mcp.server"],
        cwd=repo, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, timeout=120)
    assert proc.returncode == 0, (
        "the MCP registration's own command crashed on boot:\n"
        + proc.stderr[-2000:])
