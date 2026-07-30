"""Phases — the pockets of work inside one running agent.

A dispatched agent is not one action, it is a run: it says what it is about to
do, does five or nine tool calls, produces something, says what it is doing
next. The dashboard could only ever show that as one card with the last line of
output on it, so the shape of the run — which part is happening, what came out
of the part before, where it went wrong — was invisible unless you opened the
raw log and read four hundred lines of stream-json.

This turns the step feed into that shape, with no cooperation from the agent.

WHERE A PHASE STARTS. On a narration step. Claude emits assistant text before a
batch of tool calls ("Right — first the SpriteFrames, then the scene"), so the
text IS the agent declaring a unit of work, and the tools that follow are that
unit. It is a heuristic and it is honest about being one: a run with no
narration comes back as a single phase called "working", which is exactly as
much structure as that run really has.

WHAT A PHASE CARRIES. Its tools, its results, its steers — and the artifacts
that appeared during its window, matched on time (see dispatch._add_step for why
steps are stamped). That last part is the point of the whole module: "the audio
seat made these three files while it was doing the footsteps pass" is a
sentence the UI could not previously form.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

# A narration this short is an aside ("Done.", "Perfect!"), not a new unit of
# work — starting a phase on it produces a graph of one-line noise.
MIN_TITLE = 12
# Steps kept per phase in the payload. The full feed stays one click away at
# /api/agent-activity; this is what the graph and the rail draw.
KEEP = 24

_SENTENCE = re.compile(r"^\s*(.{12,110}?)(?:[.!?—:\n]|$)")


def _title(text: str) -> str:
    """The first clause of a narration, as a phase name."""
    flat = " ".join(str(text or "").split())
    found = _SENTENCE.match(flat)
    title = (found.group(1) if found else flat[:110]).strip(" -–—*#`")
    return title or "working"


def _epoch(stamp: str) -> float:
    """A SQLite ``datetime('now')`` string (UTC, second resolution) as epoch."""
    if not stamp:
        return 0.0
    try:
        return datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def split(steps: list[dict], *, running: bool = False) -> list[dict]:
    """The step feed as ordered phases, oldest first."""
    phases: list[dict] = []

    def start(title, ts):
        phases.append({"n": len(phases) + 1, "title": title, "started": ts,
                       "ended": ts, "steps": [], "tools": [], "results": 0,
                       "steers": 0, "state": "done", "error": ""})
        return phases[-1]

    for step in steps or []:
        kind = step.get("kind")
        ts = float(step.get("ts") or 0)
        text = str(step.get("text") or "")
        if kind == "say" and len(text.strip()) >= MIN_TITLE:
            start(_title(text), ts)
            phases[-1]["steps"].append(step)
            continue
        phase = phases[-1] if phases else start("working", ts)
        phase["steps"].append(step)
        if ts:
            phase["ended"] = ts
            if not phase["started"]:
                phase["started"] = ts
        if kind == "tool":
            name = str(step.get("name") or "tool")
            if name not in phase["tools"]:
                phase["tools"].append(name)
        elif kind == "result":
            phase["results"] += 1
            # A tool result that reads as an error is the single most useful
            # thing to surface at phase level — it is where a run goes wrong.
            body = text.lower()
            if not phase["error"] and ("error" in body[:200] or "traceback" in body[:200]
                                       or body.startswith("failed")):
                phase["error"] = text[:200]
        elif kind == "steer":
            phase["steers"] += 1

    for phase in phases:
        phase["steps"] = phase["steps"][-KEEP:]
        if phase["error"]:
            phase["state"] = "trouble"
    if phases and running:
        phases[-1]["state"] = "running"
    return phases


# Image paths in a step, in any of the shapes an agent's log carries them: a
# Read tool's hint (absolute, backslashes), a path inside a Bash one-liner, a
# bare project-relative path in a result line.
#
# NOT A REGEX. The obvious pattern for this — an unanchored lazy run of
# "path-ish characters" ending in an extension — is quadratic, because the
# character class contains space, dot and both slashes, so every start position
# rescans the rest of the blob. Measured at ~5 ms on a single 1000-character
# narration step, which over a 500-step ring and one poll every three seconds is
# more CPU than the entire rest of the dashboard, spent overwhelmingly on steps
# that contain no path at all. Splitting on whitespace and quotes and testing
# the tail of each token is linear and does the same job.
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_SPLIT = re.compile(r"[\s\"'`,;()\[\]{}<>|]+")


def _tokens(blob: str, exts: tuple) -> list[str]:
    """Whitespace/punctuation-separated tokens that end in one of ``exts``."""
    out: list[str] = []
    for token in _SPLIT.split(blob or ""):
        if not token or len(token) > 400:
            continue
        cleaned = token.rstrip(".:=")
        if cleaned.lower().endswith(exts) and cleaned not in out:
            out.append(cleaned)
    return out


def _image_tokens(blob: str) -> list[str]:
    return _tokens(blob, _IMG_EXT)


# THE FILES THAT ARE THE WORK, not pictures of it. An agent's run is mostly
# reading and writing source, scenes and data, and the log said so in prose — a
# 90-character absolute path in the middle of a sentence, which you could read
# but not open. These are the extensions worth turning into something clickable.
_READ_EXT = (".gd", ".gdshader", ".tscn", ".tres", ".godot", ".import",
             ".py", ".js", ".css", ".html", ".json", ".md", ".txt", ".cfg",
             ".ini", ".toml", ".yml", ".yaml", ".csv", ".sh", ".ps1", ".bat",
             ".gitignore", ".env.example")

# Files an agent touches that are not the work: its own log, temp dumps.
_IGNORE = (".bgate/agents/", ".bgate\\agents\\")
MAX_SEEN = 8
# Per step, and per phase. A step that greps a tree can name forty files; the
# rail is a rail, and the full list is one "full log" click away.
MAX_STEP_FILES = 5
MAX_READ = 12


def _relative(root: Path, raw: str) -> str:
    """A path from a log line as a project-relative path, or "" if it is not
    one (or does not exist — a path in a prompt is not a file)."""
    text = str(raw or "").strip().strip("'\"`,()[]")
    if not text:
        return ""
    try:
        candidate = Path(text)
        target = candidate if candidate.is_absolute() else (root / candidate)
        target = target.resolve()
        rel = target.relative_to(root.resolve())
    except (OSError, ValueError):
        return ""
    if not target.is_file():
        return ""
    out = str(rel).replace("\\", "/")
    return "" if any(skip.replace("\\", "/") in out for skip in _IGNORE) else out


def look(root: str | os.PathLike[str], phases: list[dict]) -> list[dict]:
    """What the agent actually had in front of it, per phase.

    An art agent spends its run reading references, opening sheets in PIL and
    re-reading its own last render — and none of that was anywhere on screen,
    so watching it work meant watching a spinner and trusting it. Registered
    artifacts only cover what it FILED; this covers what it LOOKED AT, which is
    most of the interesting part and all of the part you argue with.

    Paths are pulled out of the step text, resolved against the project, and
    kept only if they exist and live inside it. A path in a prompt that was
    never written is not a file and does not show up.
    """
    base = Path(root)
    for phase in phases or []:
        seen: list[str] = []
        read: list[str] = []
        after_image = False
        for step in phase.get("steps") or []:
            blob = " ".join(str(step.get(k) or "") for k in ("hint", "text", "name"))
            here: list[str] = []
            for match in _image_tokens(blob):
                if len(seen) >= MAX_SEEN and here:
                    break
                rel = _relative(base, match)
                if rel and rel not in here:
                    here.append(rel)
                if rel and rel not in seen:
                    seen.append(rel)
            # The same trick for the files that are not pictures. Stamped on the
            # step so the feed can offer the file where the agent named it,
            # rather than leaving a path in prose for you to copy out by hand.
            files: list[str] = []
            for match in _tokens(blob, _READ_EXT):
                if len(files) >= MAX_STEP_FILES:
                    break
                rel = _relative(base, match)
                if rel and rel not in files:
                    files.append(rel)
                if rel and rel not in read and len(read) < MAX_READ:
                    read.append(rel)
            if files:
                step["files"] = files
            # Stamped on the STEP, not just collected for the phase: the feed
            # shows the picture where the agent looked at it, in line, instead
            # of a file path you have to go and find.
            if here:
                step["images"] = here[:4]
            # The narration right after a look is the agent's READING of what it
            # saw — "the cut is off, the teal backing bled through". That is the
            # sentence you want next to the image, and it is only identifiable
            # by position.
            if after_image and step.get("kind") == "say":
                step["analysis"] = True
            after_image = bool(here) or (after_image and step.get("kind") == "result")
        made = {str(a.get("path") or "").replace("\\", "/")
                for a in phase.get("artifacts") or []}
        # What it MADE is already its own list; this is the rest of its view.
        phase["seen"] = [p for p in seen if p not in made]
        phase["read"] = [p for p in read if p not in made]
    return phases


def attach(phases: list[dict], artifacts: list[dict]) -> list[dict]:
    """Hand each artifact to the phase that was running when it appeared.

    Matched on time, with the LAST phase as the catch-all: an artifact whose
    row landed a moment after the final step still belongs to the work that
    made it, and dropping it would mean a finished run showing nothing it
    produced.
    """
    if not phases:
        return phases
    for phase in phases:
        phase["artifacts"] = []
    for art in artifacts or []:
        made = _epoch(art.get("created_at"))
        target = None
        for phase in phases:
            # A second of slack each way: the step clock is the parse time and
            # the artifact clock is SQLite's, and they round differently.
            if made and float(phase["started"] or 0) - 1 <= made <= float(phase["ended"] or 0) + 1:
                target = phase
        (target or phases[-1])["artifacts"].append({
            "id": art.get("id"),
            "logical_name": art.get("logical_name"),
            "path": art.get("path"),
            "kind": art.get("kind"),
            "status": art.get("status"),
            "revision": art.get("revision"),
            "producer": art.get("producer"),
            "metadata": art.get("metadata") or {},
            "created_at": art.get("created_at"),
        })
    return phases
