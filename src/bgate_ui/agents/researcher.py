"""The research agent: a Claude Code session that can read the web and nothing else.

Every other session Builders Gate spawns is denied WebSearch and WebFetch
(runners.AGENT_BUILTIN_TOOLS says why). Pre-production research
(bgate_core.design.research) is the deliberate exception, because "what did
the comparable games get right" cannot be answered without reading about
them. The exception is kept as narrow as the flags allow:

  * ``--tools WebSearch,WebFetch`` - the built-in tool set is exactly those
    two. No Read, Write, Edit, Bash, Glob or Grep exists in the process.
  * ``--strict-mcp-config`` with no ``--mcp-config`` - no MCP server at all,
    so not the builders-gate one this may have been called from.
  * ``--setting-sources ""`` - no settings file can add a tool back.
  * the working directory is an empty temp dir, not the project.
  * ``--system-prompt`` replaces the coding-agent framing, as the brainstorm
    partner's does.

One shot, ``--output-format json``: the prompt goes in on stdin (clear of the
Windows argv limit) and one result object comes back. What the agent says is
text; bgate_core.design.research parses and validates it before any row is
written, so this module never touches the database.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import Optional

from . import runners as _runners

TOOLS = ("WebSearch", "WebFetch")
FALLBACK_MODEL = "sonnet"
# A teardown reads a dozen pages; the measured spread is minutes, not seconds.
TIMEOUT_S = {"suggest": 300.0, "teardown": 900.0, "plan": 600.0}


def _model(root) -> str:
    """The brainstorm model: the same "think, don't build" budget choice."""
    try:
        from bgate_core.store import settings as _settings

        return str(_settings.get(root, "brainstorm.model") or FALLBACK_MODEL)
    except Exception:
        return FALLBACK_MODEL


def build_args(exe: str, *, system: str, model: Optional[str]) -> list[str]:
    return [exe, "-p", "--output-format", "json",
            "--tools", ",".join(TOOLS),
            "--allowedTools", ",".join(TOOLS),
            "--strict-mcp-config", "--setting-sources", "",
            "--disable-slash-commands",
            "--system-prompt", system] \
        + (["--model", model] if model else [])


def available() -> dict:
    exe = _runners.find_claude()
    if not exe:
        return {"available": False,
                "reason": "claude CLI not found on PATH - research spawns a "
                          "real Claude Code session with web search"}
    return {"available": True, "exe": exe}


def _parse(stdout: str) -> dict:
    """The one result object ``-p --output-format json`` prints."""
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict) and "result" in got:
            return got
    try:
        got = json.loads(stdout)
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def run(root, *, system: str, prompt: str, kind: str = "teardown",
        timeout: Optional[float] = None) -> dict:
    """One research turn. Returns ``{ok, text, model, seconds, usd}`` or
    ``{ok: False, error}`` - a failure is returned, not raised, like
    brainsession.ask."""
    ready = available()
    if not ready["available"]:
        return {"ok": False, "error": ready["reason"]}
    model = _model(root)
    args = build_args(ready["exe"], system=system, model=model)
    started = time.monotonic()
    scratch = tempfile.mkdtemp(prefix="bgate_research_")
    try:
        proc = subprocess.run(args, input=prompt, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              cwd=scratch, env=dict(os.environ),
                              timeout=timeout or TIMEOUT_S.get(kind, 600.0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "seconds": round(time.monotonic() - started, 2),
                "error": f"research {kind} ran past its "
                         f"{int(timeout or TIMEOUT_S.get(kind, 600))}s ceiling"}
    except OSError as exc:
        return {"ok": False, "error": f"could not start claude: {exc}"}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    seconds = round(time.monotonic() - started, 2)
    got = _parse(proc.stdout)
    if not got or got.get("is_error") or proc.returncode != 0:
        why = (got.get("result") or got.get("subtype")
               or (proc.stderr or "").strip()[-400:]
               or f"exit {proc.returncode}")
        return {"ok": False, "error": f"research {kind} failed: {why}"[:500],
                "seconds": seconds, "usd": got.get("total_cost_usd", 0.0)}
    return {"ok": True, "text": str(got.get("result") or ""), "model": model,
            "seconds": seconds, "usd": got.get("total_cost_usd", 0.0)}
