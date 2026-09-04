"""Builders Gate MCP server (FastMCP, stdio).

Every tool takes an optional `project_dir` and resolves the project from it,
then BGATE_ROOT, then the cwd by walking up for a .bgate dir - so an agent
working inside a game repo never has to pass paths around, but a fleet sharing
one server can always be explicit.

There used to be a module-level `_ACTIVE_ROOT` that project_select mutated, and
which every tool read. That made "which game does this call affect" a function
of call ORDER: two agents on one server, one selects project A, the other
selects B, and the first one's next write lands in B. Deleted. The per-call
`project_dir` travels with the call, and the fallback for a call that omits it
is a contextvar bound for the duration of THAT call only - nothing a concurrent
call can reach in.

Tool errors return a dict with an "error" key rather than raising: a raised
exception inside a tool call reads to the model as a broken server, while an
error payload reads as a fact it can act on.

FAILURE SHAPE - ONE PREDICATE, EVERY TOOL. A result is a failure if and only if
it carries a truthy "error"; every failure also carries "ok": false, and the two
are always set together, so either key answers the question. Legacy shapes are
kept alongside rather than replaced, because callers already read them: a tool
that used to answer {"available": false, "reason": ...} still answers with those
keys AND with ok/error, and a tool that answered a bare {"ok": false, ...} gains
the "error" string built from whatever reason it did state. Success payloads are
left exactly as they were - an absent "error" is the success signal, and no tool
gains a cosmetic "ok": true it never had.

Not everything false is a failure: seat_can_write's {"allowed": false} and
queue_next's {"empty": true} are ANSWERS the tool succeeded in producing, and
neither is normalized into an error.

Tool bodies do NOT run on the event loop. Every tool is a plain sync def and the
`_tool` decorator hands it to a worker thread: one image_sprites call can spend
half an hour in paid API calls, and while it does, the dashboard, the queue and
every other seat's tool call must still be served. The per-call ContextVar is
bound INSIDE a copied context in that thread, so the isolation survives the hop.
"""
from __future__ import annotations

import sys as _sys

# `python -m bgate_mcp.server` - the command every MCP registration runs -
# executes this file as `__main__`, NOT as `bgate_mcp.server`. The domain
# modules at the bottom then `from bgate_mcp.server import ...`, which starts
# a SECOND execution of this file under its package name; that copy reaches
# the star imports while tools_blender is still half-initialized and the whole
# boot dies on a circular ImportError. Registering the running module under
# its package name first means the domain modules find THIS instance, exactly
# as they do under a plain `import bgate_mcp.server`. This must precede every
# bgate import below.
if __name__ == "__main__":  # pragma: no cover - the subprocess boot test
    _sys.modules.setdefault("bgate_mcp.server", _sys.modules[__name__])

import contextvars
import functools
import inspect
import itertools
import logging
import json as _json
import os
import re as _re  # noqa: F401 - re-exported to bgate_mcp.tools_level, which imports _re from this module
import tempfile as _tempfile
import threading
import time as _time
from pathlib import Path as _Path
from typing import Annotated, Callable, Optional

import anyio
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from bgate_adapters import godot as _godot
from bgate_adapters import recorder as _recorder
from bgate_adapters import sprites as _sprites
from bgate_core.board import activity as _activity
from bgate_core.board import aegis as _aegis
from bgate_core.store import assets as _assets
from bgate_core.art import gameview as _gameview
from bgate_core.runtime import providers as _providers
from bgate_core.level import scenewire as _scenewire
from bgate_core.board import toolindex as _toolindex
from bgate_core.art import art_tournament as _art_tournament
from bgate_core.store import artifacts as _artifacts
from bgate_core.art import refs as _refs
from bgate_core.board import seats as _seats
from bgate_core.design import bible as _bible
from bgate_core.design import bible_refs as _bible_refs
from bgate_core.qa import playtest as _playtest
from bgate_core.store import scaffold as _scaffold
from bgate_core.design import canon as _canon
from bgate_core.art import chroma as _chroma
from bgate_core.design import causal as _causal
from bgate_core.store import db as _db
from bgate_core.design import decisions as _decisions
from bgate_core.board import handoff as _handoff
from bgate_core.design import lore as _lore
from bgate_core.design import quests as _quests
from bgate_core.board import iterations as _iterations
from bgate_core.art import items as _items
from bgate_core.store import project as _project
from bgate_core.store import search as _search
from bgate_core.art import animspec as _animspec
from bgate_core.art import spritekit as _spritekit
from bgate_core.art import vfx as _vfx
from bgate_core.art import artdirection as _artdirection

# THE ONE CHANNEL THAT CANNOT BE DROPPED BY CHANGING DIRECTORY.
#
# The working process used to be communicated four ways, and every one of them
# was conditional: tool docstrings (only if the agent reads the schema), the
# CLAUDE.md managed block (only if the project was init/adopt-ed AND the agent
# is standing in it), seat_brief (only if the agent thinks to call it), and the
# dispatch prompt (only for agents the dashboard spawned). A human-started
# session in a fresh checkout hit none of them and saw a bare tool list - so it
# did the work itself, off the board and past the QA gate, which is exactly what
# the pipeline exists to prevent.
#
# `instructions` is the MCP protocol's own answer and it was left empty. The
# server is registered `--scope user`, so this string arrives in EVERY session on
# the machine, in every project, with no per-project install step. Switching
# projects can no longer lobotomize the orchestrator.
#
# It is read ONCE, at server start, and that is correct rather than a limitation:
# each MCP client spawns its own stdio server process, so BGATE_SEAT here is this
# session's identity for its whole life - the same fact `_seat()` below relies on.
#
# The root is resolved here too, and best-effort: a seatless session's brief
# quotes the DIRECTOR SEAT's own mission, so a project that rewrote it with
# seat_configure gets its wording rather than the shipped default. A server can
# legitimately start outside any project, so `None` is an ordinary answer and
# seats.py falls back to the code default - this must never stop a boot.
def _boot_root() -> Optional[str]:
    hint = os.environ.get("BGATE_ROOT", "").strip()
    if hint:
        return hint
    try:
        found = _db.resolve_root(_Path.cwd())
    except Exception:
        return None
    return str(found) if found else None


mcp = FastMCP(
    "builders-gate",
    instructions=_seats.director_instructions(
        os.environ.get("BGATE_SEAT", "").strip(), _boot_root()),
)


# ONE SENTENCE PER TOOL, THE PARAGRAPH ONCE. The full explanation rode on all
# 251 tools - 78k identical characters, ~19k tokens, in every session before
# its first turn. It now lives in `instructions` (see _install_tool_index) and
# each schema carries only the pointer.
_PROJECT_DIR_DOC = "Project root; see the server instructions."
_PROJECT_DIR_NOTE = (
    "EVERY TOOL TAKES `project_dir`: the absolute path to the Builders Gate "
    "project root (the directory holding .bgate). Omit it and the server falls "
    "back to BGATE_ROOT, then to walking up from the working directory. Pass "
    "it explicitly whenever more than one project could be in play - it is the "
    "only way a call is guaranteed to land in the game you mean."
)

# The per-call project override. A ContextVar, deliberately: it is set on the
# way into ONE tool call and reset on the way out, so it cannot be observed by
# any other call. The module-level `_ACTIVE_ROOT` this replaces could - that was
# the race, and this is the whole reason the contextvar exists.
_CALL_ROOT: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "bgate_call_root", default=None)


def _root_hint() -> Optional[str]:
    """The root this call was given, if any - before falling back to discovery."""
    return _CALL_ROOT.get() or (os.environ.get("BGATE_ROOT") or None)


def _art_out(root, filename: str):
    """`<root>/.bgate_out/art/<filename>`, CONTAINED.

    An MCP tool argument is not user input in the usual sense -- it is model
    output, and a model can be steered by anything it has read: a lore entry, a
    playtest transcript, a file name in someone else's repo. `filename` used to
    be joined straight onto the art directory, so "../../../.ssh/authorized_keys"
    wrote attacker-chosen bytes wherever the desktop user could write. Every
    filesystem route in bgate_ui already resolves-and-contains; this is the same
    discipline on the model's side of the fence.
    """
    base = (_Path(root) / ".bgate_out" / "art").resolve()
    out = (base / filename).resolve()
    try:
        out.relative_to(base)
    except ValueError:
        raise ValueError(
            f"filename must stay inside .bgate_out/art - refusing {filename!r}"
        ) from None
    return out


def _keys(root: Optional[str] = None) -> None:
    """Make credentials live: the project's .env, then ~/.bgate/.env.

    Split out of :func:`_root` because the two questions came apart. "Which
    project is this call about" can legitimately have no answer; "does this
    machine have an OpenAI key" always does, and it used to be reachable only
    through a project root - so a tool that needs a key and not a game could not
    be reached at all without inventing a project to hold the credential.

    Order is the precedence and is documented at envfile.load_env: shell beats
    both files, and the project beats the machine-wide store.
    """
    try:
        from bgate_core.store import envfile
        envfile.load_env(root)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CONTAINMENT - the same project boundary the PreToolUse hook enforces, on the
# side of the fence the hook cannot see.
#
# The hook judges FILE OPERATIONS, so it sees what Write, Edit, Read and Bash
# do and nothing else. Every tool in this module takes `project_dir` and then
# goes straight to sqlite and the filesystem IN THIS PROCESS: no Write, no
# Bash, no PreToolUse event, no hook. An agent dispatched for Ember could call
# queue_add(project_dir=<Hollow>), scene_set_property against another game's
# scene, or asset_lock on somebody else's file, and not one of those crossed a
# gate. Containment in the hook alone was theatre for anything holding these
# tools, which is every dispatched agent.
#
# THE ANSWER COMES FROM bgate_core.board.aegis, the same function the hook calls,
# against the same pinned BGATE_ROOT dispatch stamps at spawn. Two processes,
# one decision function, no shared state - see that module's docstring for why
# it is pure.
_CALL_TOOL: contextvars.ContextVar[str] = contextvars.ContextVar(
    "bgate_call_tool", default="")


class ContainmentRefused(Exception):
    """This seated session named a project that is not the one it is pinned to.

    A distinct type rather than a PermissionError so the wrapper can recognise
    it and answer with the structured payload below; tools that catch broadly
    and return `_fail` still surface the same sentence, which is the part the
    model acts on.
    """


# WHAT A SEATED AGENT MAY STILL ASK ABOUT ANOTHER PROJECT: things that only
# look. Refusing every cross-project call would be defensible - the hook
# contains reads too - but the cost lands very differently at the two ends. A
# refused read is an agent that cannot orient; a refused WRITE is the whole
# point, because a write into another game is the damage nobody can undo by
# reading more carefully.
#
# EVERYTHING NOT LISTED HERE IS TREATED AS WRITING. That direction is
# deliberate: a new tool arrives contained, and the failure mode of forgetting
# to add a genuinely read-only tool to this set is a refusal a human can see
# and fix, while the failure mode of a default-allow list is a silent write
# into somebody else's game.
#
# Membership was decided by reading the bodies, not the names. Tools whose name
# reads like a report and whose body is not one are absent on purpose:
# sprite_sheet_check writes a guides PNG next to the sheet, godot_retarget_check
# and the two *_verdict tools record their verdict, consistency_check generates
# images and spends money, queue_claim_next takes the item, and
# cinematic_stuck_shots / music_stuck_tracks poll the provider and write job
# rows back. playtest_check is here despite opening the microphone: a device
# probe changes nothing in any project.
_READ_ONLY_TOOLS = frozenset({
    # machine and project diagnostics
    "bgate_doctor", "project_status", "image_status", "local_status",
    "blender_status", "godot_status", "voice_status", "kie_status",
    "godot_templates", "godot_check_project", "godot_inspect_resource",
    "item_classes", "sfx_kinds", "cutout_templates", "cutout_status",
    "playtest_devices", "playtest_check", "playtest_telemetry_contract",
    # the board and the seats, read side
    "seat_list", "seat_brief", "seat_can_write", "seat_notes", "handoff_read",
    "queue_list", "queue_get", "queue_next", "board_digest", "plan_status",
    "pending_decisions", "decision_list", "not_building_list",
    "iteration_status", "asset_status",
    # design, canon and lore, read side
    "bible_read", "bible_ref_list", "lore_list", "lore_brief", "canon_check",
    "recall", "ref_list", "profile_get", "quest_list", "quest_read",
    "causal_chains", "causal_specs", "scene_outline", "dialogue_read",
    "dialogue_list", "sfx_list", "brainstorm_list", "brainstorm_feed",
    "playtest_list", "playtest_brief", "art_tournament_standings",
    # long-running production pipelines, read side
    "music_options", "music_candidates", "cinematic_options",
    "cinematic_styles", "cinematic_sequences", "cinematic_candidates",
    "cinematic_shot_status", "cinematic_estimate", "music_status",
    "storyboard_boards", "storyboard_open",
})


def _log_containment(result: dict, target: str, tool: str, mode: str) -> None:
    """Put every crossing on the record, refused or not.

    Same file and same line shape as the hook's own containment log, so `bgate
    hook --log` reads both without knowing there are two writers; `surface`
    says which one wrote the line. THE LOG GOES IN THE PINNED PROJECT, never
    the one that was asked for - writing the audit trail into the other game
    would be the boundary crossing this line exists to report.

    Best effort, by the rule that governs every side effect on this path: a
    logger that raises would take the session down to save a line of audit.
    """
    try:
        from datetime import datetime, timezone

        pinned = os.environ.get("BGATE_ROOT", "").strip()
        if not pinned:
            return
        path = _Path(pinned) / ".bgate" / "hook.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": "containment",
                "surface": "mcp",
                "verdict": result.get("verdict", ""),
                "mode": mode,
                "enforced": mode == "block" and tool not in _READ_ONLY_TOOLS,
                "tool": tool,
                "seat": os.environ.get("BGATE_SEAT", ""),
                "owner": os.environ.get("BGATE_LOCK_OWNER", ""),
                "scope": result.get("scope", ""),
                "target": str(target)[:1000],
                "reason": str(result.get("reason", ""))[:1000],
            }) + "\n")
    except Exception:
        pass


def _contained(root: str) -> str:
    """``root`` back if this session may act on it, else raise.

    SEATED SESSIONS ONLY, exactly as in the hook. A human-started director with
    no BGATE_SEAT legitimately works across projects - reading one game's design
    while planning another is ordinary top-level work - and an unpinned session
    has claimed no scope for anyone to enforce.

    A check that cannot run must not become a session that cannot work, so an
    unexpected failure inside aegis allows the call. That is the same fail-safe
    the hook documents, and it is safe to repeat here for the same reason: the
    gate exists to stop an accident, and an accident is not what survives a
    broken import.
    """
    mode = _aegis.mode()
    if mode == "off":
        return root
    seat = _seat()
    if not seat:
        return root
    pinned = os.environ.get("BGATE_ROOT", "").strip()
    if not pinned:
        return root

    try:
        # cwd is the resolved root itself: it is already absolute, so nothing
        # is being guessed here - the argument is passed only because aegis
        # refuses a relative target rather than resolving one blindly.
        result = _aegis.decide(pinned, root, cwd=root, seat=seat)
        allowed = _aegis.is_allowed(result)
    except Exception:
        return root
    if allowed:
        return root

    tool = _CALL_TOOL.get() or "this tool"
    _log_containment(result, root, tool, mode)
    if tool in _READ_ONLY_TOOLS or mode == "warn":
        return root

    other_game = result.get("verdict") == _aegis.DENY
    tail = (" Another game's files are on the other side of that call. Nothing "
            "makes them yours to change, including being asked to."
            if other_game else
            " Your work belongs in the tree you were dispatched for and nowhere "
            "else.")
    raise ContainmentRefused(
        f"[builders-gate] seat {seat!r} may not run {tool} against that "
        f"project: {result.get('reason', '')}.{tail} If your task genuinely "
        "needs something from outside, do not go and get it - say so in your "
        "result note and name the project, so a human decides.")


def _contained_path(target, what: str = "path"):
    """A RAW filesystem argument, allowed for this session — or a refusal.

    The seated-session mirror of :func:`_contained` for tools whose target is
    not ``project_dir``: a ``godot_project`` directory, an ``out_path``, a
    ``blend_file``, an ``out_dir``. The module comment at the top of this file
    names the exact attack these carried: ``scene_set_property`` against
    another game's scene, with ``project_dir`` omitted, resolved the PINNED
    root, passed every gate, and wrote into the other game — because only the
    project root was ever asked about, never the argument doing the writing.

    Same rules as _contained: seated sessions only, and a target outside
    EVERY project is the lane story (the hook's), not this gate's. The check
    resolves which project the target belongs to and puts that root through
    the same aegis decision a project_dir would face.
    """
    text = str(target or "").strip()
    if not text:
        return target
    if not _seat() or _aegis.mode() == "off":
        return target
    if not os.environ.get("BGATE_ROOT", "").strip():
        return target
    try:
        from bgate_core.store import db as _db

        resolved = _Path(text).expanduser().resolve()
        probe = resolved if resolved.is_dir() else resolved.parent
        owner = _db.resolve_root(probe)
    except Exception:
        return target
    if owner is None:
        return target
    _contained(str(owner))
    return target


def _res_pair(godot_project: str, path: str, suffix: str) -> tuple:
    """A res:// path and its file on disk, from either form.

    THE CONTAINMENT GATE RUNS HERE, once, for every scene tool: the
    ``godot_project`` argument is the write target's real address, and it used
    to go straight to the adapter while only ``project_dir`` was gated.

    Lives HERE with the rest of the shared plumbing, not in the level module
    it once sat next to: scene tools in this file and the carved-out level
    tools both route through it, and it must resolve ``_contained_path`` as
    THIS module's global so a test (or an operator) overriding the gate
    overrides it for every caller at once.
    """
    _contained_path(godot_project, "godot_project")
    gd = _Path(godot_project).expanduser().resolve()
    if not (gd / "project.godot").is_file():
        raise ValueError(f"no project.godot in {gd} - that is not a Godot project")
    rel = path[len("res://"):] if path.startswith("res://") else path
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel.endswith(suffix):
        raise ValueError(f"expected a {suffix} path, got {path!r}")
    disk = (gd / rel).resolve()
    if gd not in disk.parents:
        raise ValueError(f"{path!r} points outside the Godot project")
    return disk, f"res://{rel}"


def _root(scratch: bool = False) -> str:
    """The project root for THIS call: project_dir > BGATE_ROOT > walk up from cwd.
    Also loads the project's .env and the machine-wide one, in that order.

    ``project_dir="scratch"`` (or "global") resolves to ``~/.bgate/scratch``,
    which is how a caller says "this one is not about any of my games" without
    leaving the directory it is standing in.

    ``scratch=True`` additionally puts that project at the BOTTOM of the
    discovery chain rather than raising. Passed by the tools whose output has
    somewhere to go even when no game does - see :func:`_scratch_root`.
    """
    override = _root_hint()
    if override:
        # An alias is checked before the path: nobody has a project directory
        # literally called "scratch" relative to nothing, and resolving it as
        # one would silently create it in the cwd.
        aliased = _project.resolve_alias(override)
        root = str(aliased) if aliased else override
    else:
        try:
            root = str(_project.require_root(scratch=scratch))
        except LookupError as exc:
            # core's own hint still points at project_select, which no longer
            # switches anything. Restate it in terms of what actually works now.
            raise LookupError(
                f"{exc} Pass project_dir=<absolute path to the project root> on "
                "this call, or project_dir='scratch' to use ~/.bgate/scratch, "
                "or export BGATE_ROOT, or run project_init to create one "
                "here.") from None
    # BEFORE _keys, AND THAT ORDER IS THE POINT. _keys loads the named
    # project's .env into this process's environment, so a containment check
    # placed after it would hand a seated agent another game's API keys and
    # then tell it it was not allowed to be there. Refuse first, read the
    # credentials second.
    root = _contained(root)
    _keys(root)
    return root


def _scratch_root() -> str:
    """The root for a tool whose output does not need a game to belong to.

    Generation is the case: an image, a sheet, a track - all of them need a
    project for the artifact registry and `.bgate_out`, and
    none of them need an engine. Falling back here rather than refusing is what
    makes "just use one tool for something" possible without inventing a game to
    hold the result.

    Not the default for every tool. `godot_run` against an empty scratch project
    is a confusing failure a long way from its cause, and a tool that edits game
    files has nothing to edit - those keep refusing, which is the useful answer.
    """
    return _root(scratch=True)


# The machine-wide keys are live from the moment the server starts, so a tool
# that needs a credential and not a project - provider_status, doctor - answers
# correctly in a session that never names one.
_keys()


# Keys a tool might have used to say WHY, in the order they are believed. The
# first non-empty one becomes the unified "error" string.
_REASON_KEYS = ("error", "reason", "message", "detail", "stderr", "traceback")

# The same question, asked of a SUB-result. `verdict` is in this list and not in
# the one above because at the top level it is usually an ANSWER (canon_check
# replies "ok" or "conflict") while on a per-item entry that failed it is the
# statement of what went wrong.
_NESTED_REASON_KEYS = ("verdict",) + _REASON_KEYS

# What a per-item entry calls itself, so a joined reason names WHICH item.
_ENTRY_LABEL_KEYS = ("label", "name", "pose", "part", "layer")

# How many per-item reasons are worth carrying up. Four is a turnaround.
_NESTED_REASON_CAP = 4


def _reason_text(value) -> str:
    """One reason string out of whatever a tool put under a reason key.

    Lists are joined rather than dropped: a tool that states its reason as the
    frame-by-frame verdicts has stated it, and reading only `str` there was one
    of the two ways a populated reason still got overwritten.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "; ".join(text for text in (_reason_text(item) for item in value)
                         if text)
    return ""


def _nested_reasons(result: dict) -> str:
    """The reason a STRUCTURED failure states one level down.

    Some tools fail per item and say so per item: blender_turnaround comes back
    ok=False with the exposure verdict living in renders[i]["verdict"], and the
    top level carries no reason key at all. Collapsing that to "the call failed
    without stating a reason" threw away the one mechanical signal the tool
    exists to produce - and the standard response to a tool that looks broken is
    to retry it or move on, not to turn the lights down.

    Only entries that themselves claim failure contribute, so the three good
    frames of a four-frame turnaround stay quiet, and only the top level's own
    values are inspected - this reads a result, it does not walk a tree.
    """
    found: list[str] = []
    for value in result.values():
        for entry in (value if isinstance(value, list) else [value]):
            if not isinstance(entry, dict):
                continue
            if entry.get("ok") is not False and entry.get("available") is not False:
                continue
            reason = next((text for text in (_reason_text(entry.get(key))
                                             for key in _NESTED_REASON_KEYS)
                           if text), "")
            if not reason:
                continue
            label = next((str(entry[key]) for key in _ENTRY_LABEL_KEYS
                          if entry.get(key)), "")
            stated = f"{label}: {reason}" if label else reason
            if stated not in found:
                found.append(stated)
    return "; ".join(found[:_NESTED_REASON_CAP])


def _normalize(result):
    """Collapse the three legacy failure shapes onto one predicate.

    {error}, {ok: false, ...} and {available: false, reason} all meant failure
    and a model calling these tools had to know which tool spoke which dialect.
    Now any of them also carries ok=false AND a filled-in "error", so `"error"
    in result` is the whole test. Legacy keys stay put - the dashboard and the
    seat scripts already read `available` and `reason`, and a normalizer that
    renames things breaks callers to please a schema.

    Only the top level is touched, and only when the result claims failure: a
    doctor report whose `blender` row is unavailable is a SUCCESSFUL answer to
    "what is installed", and stamping an error on it would be a lie.

    A REASON THE TOOL DID STATE IS NEVER REPLACED. That includes one stated per
    item - see _nested_reasons. The generic string is the last resort, not the
    first: it says "this tool is broken", which is a different fact from any
    verdict the tool actually reached, and the model acts differently on it.
    """
    if not isinstance(result, dict):
        return result
    failed = (result.get("ok") is False or bool(result.get("error"))
              or result.get("available") is False)
    if not failed:
        return result
    reason = ""
    for key in _REASON_KEYS:
        text = _reason_text(result.get(key))
        if text:
            reason = text
            break
    return {**result, "ok": False,
            "error": (reason or _nested_reasons(result)
                      or "the call failed without stating a reason")}


# Handing a render BACK to the model, rather than a path to one.
#
# "LOOK AT THE ASSET" was imperative prose in a docstring, and the tool it was
# written on returned four file paths and two floats. Nothing in the transport
# ever carried a pixel, so an agent could only obey the instruction by believing
# it had. FastMCP's Image content block is the mechanism that makes looking
# possible at all; the exposure verdict stays, because a number the model cannot
# talk itself out of is worth more than an image it can.
#
# Downscaled first: four 640x960 PNGs are several megabytes of base64 in one
# response, and every defect these frames exist to catch - blown out, black,
# imported nothing, wrong colour entirely - survives a long edge of 512.
_IMAGE_RETURN_EDGE = 512
_IMAGE_RETURN_CAP = 6

#: `image_sprites(limits=...)` - how long one run may take and how hard it
#: may retry. Grouped because they are one question; named here so the tool
#: and its refusal message cannot disagree about the legal keys.
_SPRITE_LIMITS = {"max_retries": 1, "timeout": 300, "max_seconds": 1800}

#: `image_sprites(palette=...)` - `lock` is "auto" (default), "on" or "off";
#: `colors` is the quantisation target when locking.
_SPRITE_PALETTE = {"lock": "auto", "colors": 64}


def _image_blocks(paths) -> list:
    """The frames themselves as MCP image content. Never raises."""
    from io import BytesIO

    from mcp.server.fastmcp import Image as _McpImage

    blocks = []
    for path in list(paths or [])[:_IMAGE_RETURN_CAP]:
        try:
            from PIL import Image as _PILImage

            with _PILImage.open(path) as frame:
                shrunk = frame.convert("RGB")
                shrunk.thumbnail((_IMAGE_RETURN_EDGE, _IMAGE_RETURN_EDGE))
                buffer = BytesIO()
                shrunk.save(buffer, format="PNG")
            blocks.append(_McpImage(data=buffer.getvalue(), format="png"))
        except Exception:
            # Pillow missing, or a frame this build cannot decode. The file is
            # still a real render; hand it over whole rather than not at all.
            try:
                blocks.append(_McpImage(path=str(path)))
            except Exception:
                continue
    return blocks


#: How often a long-running tool tells the client it is still alive. Must be
#: comfortably under the smallest client idle ceiling; MCP clients commonly
#: default to 1800s and some are configured far lower.
_HEARTBEAT_SECONDS = 20.0


def _tool(fn: Optional[Callable] = None, *,
          images: Optional[Callable] = None) -> Callable:
    """Register a function as an MCP tool, with `project_dir` bolted on, run OFF
    the event loop, and its failures normalized to one shape.

    `images` is an optional callable taking the (already normalized) result and
    returning the image paths to hand back as MCP image content alongside the
    JSON payload. The payload is unchanged and still LAST, so a caller reading
    the text block reads exactly what it read before; the frames are simply
    also in the response.

    Every tool gets the same optional trailing parameter rather than 70-odd
    hand-edited signatures, and the wrapper binds it into `_CALL_ROOT` for the
    duration of the call so the existing `_root()` bodies need no change. The
    binding is reset in a finally - a tool that raises must not leave its root
    behind for the next call on this thread.

    The bodies are blocking by nature: subprocesses (Blender, Godot, ffmpeg),
    sqlite, and image-model calls that legitimately run for tens of minutes.
    Run as a plain sync def, FastMCP would await them ON the loop and one
    image_sprites batch would freeze the dashboard, the queue and every other
    seat's tool call behind it - the exact failure the transcribe adapter's
    docstring says this design exists to avoid. So the wrapper is async and the
    body goes to a worker thread.

    That hop is why the ContextVar is bound inside a FRESH copied context rather
    than around the await: anyio reuses worker threads, so a `set` left on a
    pooled thread's default context could be seen by whatever call lands on that
    thread next. Each call gets its own contextvars.Context, sets the root in
    there, and drops it - call N's project_dir cannot reach call N+1 no matter
    which thread either one runs on.
    """
    if fn is None:
        return functools.partial(_tool, images=images)

    signature = inspect.signature(fn, eval_str=True)
    if "project_dir" in signature.parameters:
        # Guard, not politeness: a tool carrying its own `project_dir` meaning
        # something else (an ENGINE project, say) would silently shadow the
        # project root and put the ambiguity right back. Rename that parameter.
        raise TypeError(
            f"{fn.__name__} already declares 'project_dir'; the name is reserved "
            "for the Builders Gate project root on every tool")

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        given = (kwargs.pop("project_dir", None) or "").strip() or None

        def _call():
            token = _CALL_ROOT.set(given)
            # The tool's own name, for the containment gate: it is what decides
            # whether this call only looks or actually writes, and _root() has
            # no other way to know which of 200 tools is asking.
            name_token = _CALL_TOOL.set(fn.__name__)
            # ANNOUNCE THE CALL BEFORE IT BLOCKS. The heartbeat above stops a
            # slow tool from LOOKING dead; it does nothing about the restart
            # that happens anyway, and a killed server takes its worker
            # threads with it while the provider still charges and still
            # writes the file. This row outlives the process, so the next
            # server start can name what the last one was holding instead of
            # the work being gone with no record it existed.
            flight_root = _root_hint()
            flight = ""
            if flight_root:
                try:
                    from bgate_core.board import inflight as _inflight

                    flight = _inflight.begin(
                        flight_root, fn.__name__, seat=_seat(),
                        item_id=_work_item_id())
                except Exception:                                 # noqa: BLE001
                    flight = ""
            try:
                payload = _normalize(fn(*args, **kwargs))
                if images is None or not isinstance(payload, dict):
                    return payload
                try:
                    blocks = _image_blocks(images(payload))
                except Exception:
                    blocks = []  # a picture is a bonus; never lose the result
                return [*blocks, payload] if blocks else payload
            except ContainmentRefused as exc:
                # Most tools catch their own exceptions and would have turned
                # this into _fail's "ContainmentRefused: ..." string, which is
                # the same sentence. This is for the ones that do not, so a
                # refusal is never the model's first sight of a raised
                # exception - and so both shapes carry ok/error like every
                # other failure this module produces.
                return {"ok": False, "error": str(exc),
                        "refused": "containment",
                        "pinned_root": os.environ.get("BGATE_ROOT", "").strip(),
                        "seat": _seat()}
            except Exception as exc:
                # THE NET UNDER THE 200 PER-TOOL try/excepts. Almost every
                # tool body carries its own `except Exception: return _fail`,
                # but "almost" is the operative word: a tool whose body lacks
                # one (or whose except clause itself raises) used to surface
                # to the model as a raw MCP protocol error with no ok/error
                # shape. Same _fail sentence either way, so a body that
                # already catches loses nothing and a body that forgot is no
                # longer a different kind of failure. The per-tool copies can
                # now be deleted at leisure; this makes their absence safe.
                return _fail(exc)
            finally:
                if flight and flight_root:
                    try:
                        from bgate_core.board import inflight as _inflight

                        _inflight.end(flight_root, flight)
                    except Exception:                             # noqa: BLE001
                        pass
                _CALL_TOOL.reset(name_token)
                _CALL_ROOT.reset(token)

        # abandon_on_cancel stays False (the default): a cancelled client must
        # not leave a half-written .blend or a half-downloaded image behind.
        #
        # HEARTBEAT WHILE THE BODY RUNS, or the client kills the call and the
        # work is thrown away after we paid for it. MCP clients abort a tool
        # that "sent no response or progress" for their idle ceiling (1800s by
        # default), and these bodies are blocking by design - Blender, Godot,
        # ffmpeg, and image/animation models that legitimately run for minutes.
        # Because abandon_on_cancel is False the thread then RUNS TO
        # COMPLETION regardless, so the cancelled call still spends the money,
        # still writes its files, and simply has nowhere to deliver its
        # result. MEASURED on night-shift: Retro Diffusion jobs charged and
        # succeeded, sheets appeared on disk at 19:46, 20:41 and 21:04, and
        # the agent that paid for each one saw nothing and reported a hang.
        # A periodic progress notification resets the client's idle timer, so
        # a slow tool stays slow instead of becoming a silent loss.
        async def _run_with_heartbeat():
            done = anyio.Event()
            result: dict = {}

            async def _body():
                try:
                    result["value"] = await anyio.to_thread.run_sync(
                        contextvars.copy_context().run, _call)
                finally:
                    done.set()

            async def _beat():
                # Cheap and best-effort: a transport that cannot take a
                # progress notification must never break the tool call it was
                # meant to protect.
                try:
                    ctx = mcp.get_context()
                except Exception:
                    return
                ticks = 0
                while not done.is_set():
                    with anyio.move_on_after(_HEARTBEAT_SECONDS):
                        await done.wait()
                    if done.is_set():
                        return
                    ticks += 1
                    try:
                        await ctx.report_progress(
                            progress=ticks,
                            message=f"{fn.__name__}: working "
                                    f"({ticks * _HEARTBEAT_SECONDS:.0f}s)")
                    except Exception:
                        return

            async with anyio.create_task_group() as tg:
                tg.start_soon(_beat)
                await _body()
            return result.get("value")

        return await _run_with_heartbeat()

    wrapper.__signature__ = signature.replace(
        parameters=[*signature.parameters.values(), inspect.Parameter(
            "project_dir", inspect.Parameter.KEYWORD_ONLY, default=None,
            annotation=Annotated[Optional[str],
                                 Field(default=None, description=_PROJECT_DIR_DOC)])])
    if not _module_registers(fn.__name__):
        # A DISABLED MODULE'S TOOL IS NEVER REGISTERED — the whole point of
        # the switch: ~200 tool schemas ride in every agent's context on
        # every turn, and a project that turned cinematics off stops paying
        # for cinematic_* on every one of them. The function itself is
        # returned intact so any internal caller keeps working; it is only
        # absent from the MCP registry this process serves. A tool the SEAT
        # scoped off (not the project) is parked so tool_unlock can register
        # it later in the session.
        if _seat_scoped_off(fn.__name__):
            _PARKED[fn.__name__] = wrapper
        return wrapper
    return mcp.tool()(wrapper)


# Tools this process could serve but the seat does not hold. tool_unlock moves
# them into the live registry; a project's module choice is not overridable
# this way and never lands here.
_PARKED: dict[str, Callable] = {}


def _seat_scoped_off(tool_name: str) -> bool:
    """True when only the seat (not the project's modules) hides this tool."""
    from bgate_core.store import modules as _modules

    if _MODULES_OFF and not _modules.tool_enabled(tool_name, _MODULES_OFF):
        return False
    return not _modules.seat_tool_enabled(tool_name, _seat())


# Which modules the PINNED project has switched off, resolved once: the tool
# registry is built at import, one process per session, and the session is
# pinned to one project (BGATE_ROOT at dispatch, cwd for a hand-started one).
# A session no project claims — or any failure reading the choice — registers
# everything: a missing feature must only ever be the result of a stored
# decision, never of a broken read.
_MODULES_OFF: Optional[set] = None


def _module_registers(tool_name: str) -> bool:
    global _MODULES_OFF
    if _MODULES_OFF is None:
        try:
            from bgate_core.store import modules as _modules

            root = os.environ.get("BGATE_ROOT", "").strip()
            if not root:
                root = str(_project.require_root())
            _MODULES_OFF = _modules.disabled(root)
        except Exception:
            _MODULES_OFF = set()
    from bgate_core.store import modules as _modules

    if _MODULES_OFF and not _modules.tool_enabled(tool_name, _MODULES_OFF):
        return False
    # THE SEAT'S CRAFT, on top of the project's modules. A dispatched seat
    # registers only the craft surfaces it practises plus the shared spine —
    # a gameplay agent stops carrying every blender_ and cinematic_ schema on
    # every turn. Scoped-off is per process and per seat, exactly like the
    # module gate; BGATE_SEAT_TOOLS=all is the escape hatch for a session
    # that genuinely needs everything (say so in the dispatch env).
    if (os.environ.get("BGATE_SEAT_TOOLS", "").strip().lower()) == "all":
        return True
    return _modules.seat_tool_enabled(tool_name, _seat())


def _fail(exc: Exception) -> dict:
    # ok=false alongside the message: one predicate for every failure in this
    # module, whatever the tool. See _normalize.
    out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    # A BILLING-SHAPED FAILURE CARRIES ITS OWN REDIRECT. The observed agent
    # response to "no credit" is to conclude the pipeline is closed and
    # hand-roll the asset - while a second provider sits keyed and funded.
    # Appending the gateway's board here, in the same tool result as the
    # refusal, reaches the agent at the exact moment it is deciding that.
    # Best-effort by the _fail rule: explaining a failure must never raise.
    try:
        from bgate_core.runtime import gateway as _gateway
        if _gateway.is_billing_error(exc):
            out["route"] = _gateway.billing_note(_root_hint())
    except Exception:
        pass
    return out


_RUN_SEQ = itertools.count()
_RUN_SEQ_LOCK = threading.Lock()


def _run_tag(label: str = "") -> str:
    """A token unique to ONE tool call, for output paths nobody else can clobber.

    Fixed output names (shot.png, consistency_check.png, .bgate_out/render.png)
    are fine with one seat and silently destructive with several: two seats
    screenshotting at once, and the second write lands under the first one's
    returned path, so the first seat reviews the second seat's game. The pid is
    in there because seats are separate PROCESSES - a counter alone repeats
    across them - and the counter is because two calls in one process can start
    inside the same clock second.
    """
    with _RUN_SEQ_LOCK:
        seq = next(_RUN_SEQ)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:32]
    stamp = f"{_time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{seq:04d}"
    return f"{safe}-{stamp}" if safe else stamp


def _actor() -> str:
    """Who this server process acts as - the identity artifacts.review checks."""
    return _activity.current_actor()


def _caller_is_agent() -> bool:
    """Is this server an AGENT's, rather than the human's own session?

    Two signals, either is enough. BGATE_ACTOR carries the `agent:` prefix the
    core's actor model uses; but dispatch.py stamps a spawned seat with
    BGATE_SEAT / BGATE_WORK_ITEM and does NOT stamp BGATE_ACTOR, so trusting the
    actor alone would let every dispatched agent read as the human at the
    keyboard - which is precisely the caller a permission gate must catch.
    """
    if _activity.is_agent(_actor()):
        return True
    return bool(_seat() or os.environ.get("BGATE_WORK_ITEM", "").strip())


def _seat() -> str:
    """The session's adopted seat, if any. Each Claude session spawns its own
    stdio server process, so a per-session env var is a per-session identity."""
    return os.environ.get("BGATE_SEAT", "").strip()


def _lock_identity(requested_seat: str) -> tuple[str, str]:
    """Bind asset ownership to the dispatched session when one is present."""
    adopted = _seat()
    if adopted and requested_seat != adopted:
        raise PermissionError(
            f"session adopted seat {adopted!r}; it cannot claim seat {requested_seat!r}")
    return requested_seat, os.environ.get("BGATE_LOCK_OWNER", "").strip()


def _log(kind: str, summary: str, ref: str = "") -> None:
    """Ledger entry against the active project. Never lets telemetry fail work."""
    try:
        from bgate_core.board import activity
        activity.log(_root(), kind, summary, seat=_seat(), ref=ref)
    except Exception:
        pass


def _archive_preview(src: str, label: str) -> Optional[str]:
    """Copy a render into .bgate/previews/ so the dashboard keeps a history.

    Renders land on a fixed path (render.png) and each run overwrites the last - without archiving, the dashboard could only ever show the newest one.
    """
    try:
        import shutil
        import time

        root = _Path(_root())
        previews = root / ".bgate" / "previews"
        previews.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40]
        # The SOURCE suffix, not a hardcoded .png: this used to copy a GIF's
        # bytes under a .png name, which browsers would sniff and animate but
        # the API would serve with the wrong type. An archive that renames a
        # file's format is lying about the one fact its name carries.
        suffix = _Path(src).suffix.lower() or ".png"
        dest = previews / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe or 'render'}{suffix}"
        shutil.copy2(src, dest)
        return str(dest)
    except Exception:
        return None


def _work_item_id() -> Optional[int]:
    """The work item this session is executing, if any."""
    raw = os.environ.get("BGATE_WORK_ITEM", "").strip()
    return int(raw) if raw.isdigit() else None


def _provider_gate(root: str, capability: str, what: str) -> Optional[dict]:
    """Refuse a paid run BEFORE it starts when nothing can serve it.

    The gateway's failure-time redirect (see _fail) reaches an agent after a
    402 has already been spent and read; this is the same answer one step
    earlier, so the common case - every account for this capability drained
    or unkeyed - costs a refusal instead of a paid error. A provider that IS
    routable returns None and the tool routes as it always did: this gate
    answers "can anything serve this job", never "which one should".

    Fail-open on its own faults, like every explanatory gate here: the
    machinery for explaining money must never be the thing that blocks work.
    """
    try:
        from bgate_core.runtime import gateway as _gateway
        routed = _gateway.pick(root, capability)
    except Exception:
        return None
    if routed.get("provider"):
        return None
    return {"ok": False, "stage": "provider_gate", "capability": capability,
            "providers": routed.get("why", ""),
            "error": (f"{what} has no routable provider - "
                      f"{routed.get('why', 'nothing keyed')}. This is an "
                      "account problem, not a request problem: do NOT "
                      "hand-roll a substitute asset. provider_status("
                      "fresh=true) re-probes after a top-up; if nothing is "
                      "funded, file the top-up as the blocker - a human "
                      "decides which account gets money.")}


def _register_artifact(logical_name: str, path: str, *, producer: str,
                       model: str = "", prompt: str = "",
                       refs: Optional[list[str]] = None,
                       metadata: Optional[dict] = None) -> Optional[dict]:
    """Best-effort provenance; failure never discards a successfully made file."""
    try:
        work_item = os.environ.get("BGATE_WORK_ITEM", "").strip()
        return _artifacts.register(
            _root(), logical_name, path, producer=producer, model=model,
            prompt=prompt, refs=refs, metadata=metadata,
            work_item_id=int(work_item) if work_item.isdigit() else None)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------
@_tool
def project_init(name: str, pitch: str = "", engine: str = "godot",
                 dimension: str = "2d", root: Optional[str] = None) -> dict:
    """Create a Builders Gate project (.bgate/game.db) at root (default: cwd).

    engine: godot | none. dimension: 2d | 3d | 2d+3d. Safe to re-run.
    root is where the project is CREATED; it wins over project_dir here, since
    on this one tool the directory is the thing being made, not looked up.
    """
    target = root or _root_hint() or os.getcwd()
    return _project.init(target, name, pitch=pitch, engine=engine, dimension=dimension)


@_tool
def project_select(project: str = "") -> dict:
    """Resolve a Builders Gate project by registered name or path. DEPRECATED as
    a mode switch - it no longer changes what later calls affect.

    It only ANSWERS: verifies the project exists, registers it so it stays
    discoverable, and hands back its absolute root. Feed that root to the
    `project_dir` parameter every tool carries (or export BGATE_ROOT). Empty
    arg: the root this session resolves to plus every known project.
    Returns {active, known} or {active, project, use_project_dir, deprecated}.
    Full notes: docs/tools.md#project_select
    """
    known = _project.known_projects()
    if not project:
        active = None
        try:
            active = _root()
        except Exception:
            pass
        return {"active": active, "known": known}
    root = known.get(project, project)  # name wins, else treat as a path
    if not (_Path(root) / _db.DB_DIRNAME / _db.DB_FILENAME).exists():
        raise LookupError(
            f"{project!r} is not a known project name or a project root. "
            f"Known: {known}")
    resolved = str(_Path(root).resolve())
    _project.register(resolved)
    return {"active": resolved, "project": _project.get(resolved),
            "use_project_dir": resolved,
            "deprecated": "project_select no longer switches the server's "
                          "active project - pass project_dir=<active> on "
                          "each tool call instead"}


@_tool
def bgate_doctor(refresh: bool = False) -> dict:
    """Can this machine actually do the work? One call, every dependency.

    Run this FIRST, and any time a tool fails with "not found", instead of
    calling blender_status, godot_status, image_status, playtest_check and
    playtest_devices one by one. Opens no mic, renders nothing, downloads
    nothing. Cached a few seconds; pass refresh=True right after installing
    something. Returns {blender, godot, ffmpeg, ffprobe, whisper, art_key,
    python}, each {available, path, version, min_required, reason}. `art_key`
    is green when ANY registered art provider has a key. Never raises.
    Full notes: docs/tools.md#bgate_doctor
    """
    from bgate_core.runtime import doctor as _doctor

    root = None
    try:
        root = _root()  # only to pick up the project's .env for the API key
    except Exception:
        pass
    return _doctor.check(root, refresh=bool(refresh))


@_tool
def project_status() -> dict:
    """The project's identity plus a count of what's in the bible and lore."""
    root = _root()
    conn = _db.connect(root)
    counts = {
        "bible_sections": conn.execute(
            "SELECT count(*) FROM bible_section").fetchone()[0],
        "entities": conn.execute("SELECT count(*) FROM lore_entity").fetchone()[0],
        "canon_entities": conn.execute(
            "SELECT count(*) FROM lore_entity WHERE status = 'canon'").fetchone()[0],
        "facts": conn.execute("SELECT count(*) FROM canon_fact").fetchone()[0],
        "links": conn.execute("SELECT count(*) FROM lore_link").fetchone()[0],
    }
    return {"project": _project.get(root), "root": root, "counts": counts,
            # SAY WHEN THIS IS THE SCRATCH PROJECT. Otherwise "where did my
            # sprite sheet go" has an answer nothing on any surface states,
            # and the honest one - a directory under ~/.bgate that was
            # created for you - is not a place anyone would think to look.
            "scratch": _project.is_scratch(root)}


@_tool
def project_set_dimension(dimension: str) -> dict:
    """Correct the project's 2d | 3d | 2d+3d record after the game changed shape.

    ``init`` writes this and ``adopt`` detects it; nothing else changes it, and
    re-running ``project_init`` rewrites name, pitch and engine too. Not
    cosmetic: the field steers scaffolding templates and seat-brief wording.
    Use ``2d+3d`` for the real mixed case (a 3D game with a 2D HUD) rather than
    picking whichever is closer.
    Full notes: docs/tools.md#project_set_dimension
    """
    was = _project.get(_root()).get("dimension") or ""
    after = _project.set_dimension(_root(), dimension)
    _log("project", f"dimension {was or '(unset)'} -> {dimension}")
    return {"project": after, "was": was, "now": after.get("dimension"),
            "changed": was != after.get("dimension")}


# ---------------------------------------------------------------------------
# Design bible
# ---------------------------------------------------------------------------
@_tool
def bible_add(kind: str, title: str, body: str = "", rank: int = 0) -> dict:
    """Add a bible section.

    kind: pillar | loop | constraint | reference.
    rank orders sections within a kind - it is the reading order of the
    document, lowest first.
    """
    return _bible.add(_root(), kind, title, body=body, rank=rank)


@_tool
def bible_update(section_id: int, title: Optional[str] = None,
                 body: Optional[str] = None, rank: Optional[int] = None) -> dict:
    """Update a bible section in place. Omitted fields keep their current value."""
    return _bible.update(_root(), section_id, title=title, body=body, rank=rank)


@_tool
def bible_read(kind: Optional[str] = None) -> dict:
    """Read the bible. No kind: every section, grouped by kind."""
    root = _root()
    if kind:
        return {"kind": kind, "sections": _bible.list_sections(root, kind)}
    return _bible.overview(root)


@_tool
def bible_ref_attach(section_id: int, ref: str, kind: str = "style",
                     note: str = "", rank: int = 0) -> dict:
    """Anchor a pinned reference IMAGE to a bible section - the art that says
    what the words mean.

    ref is a PIN NAME from ref_list (or a project-relative path); pin the image
    first with ref_pin. The NAME is stored, never the resolved path, so
    re-pinning better art under the same name upgrades every section pointing
    at it. kind: character | style | ui | concept.
    Full notes: docs/tools.md#bible_ref_attach
    """
    out = _bible_refs.add(_root(), section_id, ref, kind=kind, note=note,
                          rank=rank)
    _log("bible", f"anchored ref {ref!r} to bible section {section_id}",
         ref=f"bible:{section_id}")
    return out


@_tool
def bible_ref_list(section_id: Optional[int] = None, suggest: bool = False) -> dict:
    """The reference art anchored to the bible. READ BEFORE WRITING OR
    ILLUSTRATING CANON.

    No section_id: every section that has anchors. With one: that section's
    anchors plus `resolved` - the layered set to condition a generation on
    (section anchors first, then global pins). Every entry carries
    resolved_path and exists; a false `exists` is a pin whose file went
    missing. suggest=True adds `suggestions`: sections whose PROSE names a
    pin - a proposal only; attach with bible_ref_attach.
    Full notes: docs/tools.md#bible_ref_list
    """
    root = _root()
    if section_id is None:
        grouped = _bible_refs.list_all(root)
        titles = {int(s["id"]): s["title"] for s in _bible.list_sections(root)}
        out = {"by_section": [
            {"section_id": sid, "title": titles.get(sid, ""), "refs": anchored}
            for sid, anchored in sorted(grouped.items())]}
        if suggest:
            out["suggestions"] = _bible_refs.suggest_from_titles(root)
        return out
    return {"section_id": section_id,
            "anchored": _bible_refs.list_for_section(root, section_id),
            "resolved": _bible_refs.resolve_for_section(root, section_id)}


@_tool
def bible_ref_detach(section_id: int, ref: str) -> dict:
    """Remove one anchor from a bible section. The pin itself survives - only
    the claim that this section is about that image goes away."""
    out = _bible_refs.remove(_root(), section_id, ref)
    _log("bible", f"detached ref {ref!r} from bible section {section_id}",
         ref=f"bible:{section_id}")
    return out


# `scope_check(rank)` was here, and every seat's rules told agents to call it
# before building anything. It answered off a `cut_line` bible section that
# almost no project drew, and the queue gate behind the same idea never once
# refused an item. Removed with the rest of the tier machinery - a tool that
# always says yes still costs a slot in every agent's tool list.


# ---------------------------------------------------------------------------
# Lore
# ---------------------------------------------------------------------------
@_tool
def lore_add(kind: str, name: str, summary: str = "", body: str = "",
             status: str = "draft") -> dict:
    """Create a lore entity.

    kind: faction | character | place | event | item | concept | species.
    status: draft | canon | retired. Names are unique - update, don't duplicate.
    """
    return _lore.add_entity(_root(), kind, name, summary=summary, body=body,
                            status=status)


@_tool
def lore_update(ref: str, summary: Optional[str] = None, body: Optional[str] = None,
                status: Optional[str] = None) -> dict:
    """Update an entity by slug or name. Promote draft to canon with status='canon'."""
    return _lore.update_entity(_root(), ref, summary=summary, body=body, status=status)


@_tool
def lore_brief(ref: str) -> dict:
    """Everything about one entity - record, facts, and edges. Read before writing it."""
    return _lore.brief(_root(), ref)


@_tool
def lore_list(kind: Optional[str] = None, status: Optional[str] = None) -> dict:
    """List entities, optionally filtered by kind and/or status."""
    return {"entities": _lore.list_entities(_root(), kind=kind, status=status)}


@_tool
def lore_link(src: str, rel: str, dst: str, note: str = "") -> dict:
    """Connect two entities. rel is free-form: 'rules', 'allied_with', 'born_in'."""
    return _lore.link(_root(), src, rel, dst, note=note)


@_tool
def lore_fact(ref: str, statement: str, source: str = "", locked: bool = False) -> dict:
    """Assert ONE atomic fact about an entity - canon_check compares against these.

    Keep it to a single checkable claim ("The siege lasted seven years"), not a
    paragraph. locked=True marks it immovable: conflicts against it are hard.
    """
    return _lore.add_fact(_root(), ref, statement, source=source, locked=locked)


# ---------------------------------------------------------------------------
# Canon + recall
# ---------------------------------------------------------------------------
@_tool
def canon_check(text: str, entities: Optional[list[str]] = None) -> dict:
    """Check text against canon BEFORE it lands. Run on every narrative write.

    Returns verdict (ok | review | conflict), the entities it touches, the canon
    facts in play, and flags. Deterministic lexical checks: catches retired
    entities, invented proper nouns, polarity flips, and number disagreements.
    It does not judge tone or theme - 'ok' means nothing mechanical is wrong.
    """
    return _canon.check(_root(), text, entities=entities)


@_tool
def recall(query: str, limit: int = 10, kind: Optional[str] = None) -> dict:
    """Search the bible and lore. Call this BEFORE inventing anything."""
    conn = _db.connect(_root())
    return {"query": query, "results": _search.find(conn, query, limit=limit, kind=kind)}


# ---------------------------------------------------------------------------
# The provider gateway
# ---------------------------------------------------------------------------
@_tool
def provider_status(capability: str = "", fresh: bool = False) -> dict:
    """Which paid providers are LIVE - keyed, and funded where they will say.

    READ THIS BEFORE CONCLUDING A PIPELINE IS CLOSED: one account answering
    "no credit" is a routing event, not an outage. Per provider: `keyed`,
    `balance` (a NUMBER only where exposed - kie and Retro Diffusion; None
    means UNKNOWN and still routable), and `reason` when unkeyed. Pass
    `capability` ("image" | "animate" | "three_d" | "music" | "video") to get
    `pick`, the provider that job routes to now. `fresh=true` re-probes past
    the 2-minute cache. Keys never appear here and no tool writes one.
    Full notes: docs/tools.md#provider_status
    """
    from bgate_core.runtime import gateway as _gateway
    from bgate_core.runtime import providers as _providers
    try:
        root = _root()
    except LookupError:
        root = None
        _keys()
    out = {"ok": True,
           "providers": _gateway.status(root, fresh=bool(fresh)),
           "capabilities": {k: list(v)
                            for k, v in _gateway.CAPABILITIES.items()}}
    # WHICH PROVIDER WOULD ACTUALLY BE SELECTED, for every family, without
    # having to ask one capability at a time. `bgate doctor` and this tool were
    # observed answering the same question differently across three benchmark
    # games (doctor: "4 of 4 providers"; here: one live option, no
    # alternatives), so the answer a caller acts on is stated in full: the
    # order, the pick, the real alternatives, and WHY each excluded provider is
    # out. `character_work` is the one route that does not come off the table.
    out["routing"] = _providers.routing(root)
    if capability:
        out["pick"] = _gateway.pick(root, str(capability))
    return out


# ---------------------------------------------------------------------------
# Painted art (gpt-image)
# ---------------------------------------------------------------------------
@_tool
def image_status() -> dict:
    """Is the painted-art leg usable - hosted, local, or neither?

    Reports BOTH legs, because "no API key" stopped meaning "no art". The
    hosted answer checks the key without exposing it; the local answer says
    whether a ComfyUI on this machine is reachable and configured, what model
    was declared, and what that model's licence permits - which is the question
    that decides whether the output can ship in a game you sell.
    """
    # NO PROJECT IS NOT AN ERROR FOR THIS QUESTION. "Can this machine make
    # an image" is about credentials and installed packages, and both have
    # an answer before any game exists - this used to raise LookupError
    # outside a project, which made the one tool you would reach for to
    # diagnose a key the one tool you could not run.
    try:
        root = _root()          # loads project .env, then the global one
    except LookupError:
        root = None
        _keys()                 # the machine-wide layer, standalone
    from bgate_adapters import imagegen

    legs = {}
    legs["openai"] = dict(imagegen.available())
    # KREA IS A FIRST-CLASS PROVIDER AND THIS TOOL DID NOT KNOW IT EXISTED.
    # It probed OPENAI_API_KEY alone and answered for the whole painted-art
    # leg, so a project holding a working Krea key - which image_generate
    # will happily auto-select - was told the leg was unavailable. It cost a
    # support cycle in a real run. blender_status has always reported
    # per-backend; this is that shape.
    try:
        from bgate_adapters import krea
        legs["krea"] = dict(krea.available(root))
    except Exception as exc:
        legs["krea"] = {"available": False,
                        "reason": f"{type(exc).__name__}: {exc}"}
    try:
        from bgate_adapters import kie
        legs["kie"] = dict(kie.available(root))
    except Exception as exc:
        legs["kie"] = {"available": False,
                       "reason": f"{type(exc).__name__}: {exc}"}
    # RETRO DIFFUSION IS AN ART PROVIDER TOO, and this tool did not know —
    # the same staleness krea hit, one provider later. It does not PAINT
    # (that is the kie/openai/krea leg); it animates a sheet that already
    # exists, which is its own credential and its own answer to "can this
    # machine make the asset". A leg missing here reads as a leg that is
    # unavailable.
    try:
        from bgate_adapters import retrodiffusion as _rdp
        legs["retrodiffusion"] = dict(_rdp.available(root))
    except Exception as exc:
        legs["retrodiffusion"] = {"available": False,
                                  "reason": f"{type(exc).__name__}: {exc}"}
    try:
        from bgate_adapters import localgen
        legs["local"] = dict(localgen.status(probe=True))
    except Exception as exc:
        legs["local"] = {"available": False,
                         "reason": f"{type(exc).__name__}: {exc}"}

    # PAINTING AND ANIMATING ARE DIFFERENT ANSWERS. Every leg is REPORTED,
    # because "is my RD key working" is a real question and a missing leg
    # reads as a broken one — but only the legs that can MINT an image
    # count toward `available`/`auto_picks`, which is what a caller asks
    # before generating art. Counting RD would tell a project holding only
    # an animation key that it can paint, and the first thing to notice
    # would be a generation failing.
    PAINTERS = ("openai", "krea", "kie", "local")
    usable = [name for name, leg in legs.items()
              if leg.get("available") and name in PAINTERS]
    animators = [name for name, leg in legs.items()
                 if leg.get("available") and name not in PAINTERS]
    return {
        # `available` answers about the LEG, not about one adapter: any
        # usable provider means painted art is available. A caller that only
        # reads this key gets the honest answer now.
        "available": bool(usable),
        "providers": usable,
        "animation_providers": animators,
        "auto_picks": (usable[0] if usable else ""),
        "legs": legs,
        "project": root or "",
        # Setting a key is HUMAN-ONLY and there is deliberately no tool here
        # that does it - an agent that can write credentials can hand itself
        # a provider nobody paid for. So the fix names what the human runs,
        # not something to call.
        "reason": "" if usable else
                  "no image provider is configured. A human can fix it with "
                  "`bgate key set openai --global` (stores it in "
                  "~/.bgate/.env, which every project on this machine "
                  "inherits and which works with no project at all), or "
                  "without --global to keep it to one game, or in the "
                  "dashboard's provider panel - or configure a local "
                  "ComfyUI (see the local leg's `how`)",
    }


@_tool
def local_status() -> dict:
    """Every generator running on THIS machine: 2D, 3D, and what each one needs.

    The whole local registry - the ComfyUI image path, every local
    image-to-3D backend - each with a STAGE rather than a boolean, because
    "not set up", "nothing running" and "workflow file gone" need different
    fixes. READ ONLY, deliberately without a write counterpart: configuring a
    local runtime is a human's job in the dashboard. Starts nothing.
    Full notes: docs/tools.md#local_status
    """
    from bgate_core.runtime import localruntimes

    root = _root()  # triggers .env load
    rows = localruntimes.status(root, probe=True)
    ready = [r["id"] for r in rows if r["available"]]
    return {
        "available": bool(ready),
        "ready": ready,
        "runtimes": rows,
        "stages": localruntimes.STAGES,
        "reason": "" if ready else
                  "nothing local is running. Each row's `reason` says what "
                  "it needs; a human sets that up in the dashboard under "
                  "Settings → Local & agents. Hosted providers are "
                  "unaffected - see image_status.",
    }


# How many pinned anchors an automatic pull may put in front of the model.
# A project accumulates pins; conditioning one generation on nine of them is
# both expensive and incoherent, and gpt-image weights the first ones hardest.
_PINNED_REF_CAP = 4

# What `use_pinned` accepts beyond a ref kind. Spelled out so a typo answers
# with the list instead of silently pulling nothing, which reads exactly like a
# project with no pins.
_PINNED_ALL = ("all", "*", "any")


def _pinned_refs(root, spec: str) -> tuple[list[str], list[str]]:
    """The project's pinned anchors as (names, paths), for `use_pinned`.

    THE POINT: the art seat brief tells an agent to generate "conditioned on the
    pinned refs", and until now the only tool that took a reference at all was
    image_edit - so obeying the brief meant knowing a path the brief never
    stated. A pin already knows where it lives; this is the tool asking.
    """
    want = str(spec or "").strip().lower()
    if not want:
        return [], []
    kind = None if want in _PINNED_ALL else want
    pins = _refs.list_refs(root, kind=kind)
    if not pins and not kind:
        raise LookupError(
            "this project has no pinned references - use_pinned asked for "
            "anchors that do not exist. Pin the approved art with ref_pin, or "
            "drop use_pinned to generate unconditioned deliberately.")
    if not pins and kind:
        kinds = sorted({p.get("kind", "") for p in _refs.list_refs(root)})
        raise LookupError(
            f"no pinned reference of kind {kind!r} - pinned kinds here: "
            f"{', '.join(k for k in kinds if k) or 'none'}. Pass "
            f"use_pinned='all', a kind that exists, or pin one with ref_pin.")
    chosen = pins[:_PINNED_REF_CAP]
    return ([p["name"] for p in chosen], [p["path"] for p in chosen])


def _wall_tile_from(wall_img, floor_sheet, tile_px: int, out_dir, name: str) -> dict:
    """One clean wall tile out of a generated wall sheet, toned to the floor.

    Picks the most UNIFORM cell — a wall is a mass, and the least varied cell
    is the one without a transition running through it — then scales its
    luminance to sit below the floor. Generated wall art came back at 1.97x
    the floor's brightness, which renders as lit paths around dark pits.
    """
    import numpy as np

    best, best_var = None, float("inf")
    for ty in range(wall_img.height // tile_px):
        for tx in range(wall_img.width // tile_px):
            cell = wall_img.crop((tx * tile_px, ty * tile_px,
                                  (tx + 1) * tile_px, (ty + 1) * tile_px))
            var = float(np.asarray(cell.convert("RGB")).astype(float).std())
            if var < best_var:
                best, best_var = cell, var
    if best is None:
        return {"ok": False, "error": "wall sheet held no tiles"}

    arr = np.asarray(best.convert("RGB")).astype(float)
    floor_arr = np.asarray(floor_sheet.convert("RGB")).astype(float)
    floor_lum = float(floor_arr[floor_arr.mean(axis=2) > 45].mean())         if (floor_arr.mean(axis=2) > 45).any() else 90.0
    wall_lum = float(arr.mean()) or 1.0
    scale = (floor_lum * 0.62) / wall_lum
    toned = np.clip(arr * scale, 0, 255).astype("uint8")

    from PIL import Image

    path = out_dir / f"{name}_wall.png"
    Image.fromarray(toned).convert("RGBA").save(path)
    return {"ok": True, "path": str(path), "uniformity": round(best_var, 1),
            "tone_scale": round(scale, 2),
            "luminance": {"floor": round(floor_lum), "wall": round(float(toned.mean()))}}




_TEXTURE_STYLE = (
    "16-bit SNES-era pixel art floor tile texture, {what}. "
    "Top-down orthographic flat view, perfectly flat with no perspective and "
    "no vanishing point, even ambient light with no highlight or shadow "
    "gradient across the frame. Hand-placed pixels with visible dithering, "
    "limited palette of roughly twelve flat colours, crisp single-pixel "
    "detail and hard edges, no photographic blur, no anti-aliased gradients. "
    "The whole frame is the material, repeating edge to edge, seamless."
)


#: How much of the painting one tile holds, as a multiple of the tile. Small
#: means fine grain: the whole generation squeezed into roughly one tile, so a
#: floor is a texture rather than a pattern of motifs. Raise it for a material
#: whose features are meant to be legible individually — a brick wall, a plank
#: floor — where reading one brick matters more than hiding the repeat.
_TEXTURE_ZOOM = 1.5


@_tool
def tileset_generate(name: Annotated[str, Field(description='Tileset name; the atlas, resource and <name>.tiles.json sidecar are named from it.')], prompt: Annotated[str, Field(description='The FLOOR material, painted by kie as a texture the tiles are cut from.')], tile_px: Annotated[int, Field(description='Tile edge in pixels. Default 32.')] = 32,
                     bits: Annotated[int, Field(description='Autotile mask width: 8 for blob47 (default), 4 for the 16-mask side set.')] = 8, void_prompt: Annotated[str, Field(description='What shows where there is no floor. Default: featureless darkness.')] = "",
                     wall_prompt: Annotated[str, Field(description='Wall material prompt; empty derives walls from the floor/void pair.')] = "", wall_lift: Annotated[int, Field(description='Isometric block height in pixels above the floor plane; 0 = one tile height.')] = 0,
                     reuse: Annotated[bool, Field(description='Reuse an already-generated texture for the same prompt instead of re-buying it. Default True.')] = True, materials: Annotated[str, Field(description='Semicolon list of name=prompt extra floor materials, each painted as its own atlas source with 3 variants.')] = "",
                     godot_project: Annotated[str, Field(description='Directory holding project.godot. Needed only when install=True.')] = "", res_dir: Annotated[str, Field(description='res:// directory the tileset installs under. Default assets/tiles.')] = "assets/tiles",
                     install: Annotated[bool, Field(description='False (default) leaves output in .bgate_out/tiles/; True writes into the Godot project and loads it in-engine.')] = False,
                     collide: Annotated[bool, Field(description='Give wall tiles collision polygons. Default True.')] = True) -> dict:
    """GENERATE A GODOT TILESET - the bridge the level pipeline was missing.

    kie paints `prompt` (the FLOOR material) and `void_prompt` (what shows
    where there is no floor); every mask tile is built GEOMETRICALLY from
    those two. `bits`: 8 for blob47 (default; 4-bit breaks the wall shadow
    at every corner), 4 for the 16-mask side set. `install=False` lands
    everything in .bgate_out/tiles/; True writes into the Godot project AND
    loads it in-engine. Isometric projects get raised BLOCKS; `wall_lift` is
    their height in pixels (0 = one tile). Also written as an Aseprite master.
    Full notes: docs/tools.md#tileset_generate
    """
    try:
        _contained_path(godot_project, "godot_project")
        from PIL import Image as _Img

        from bgate_adapters import kie as _kie
        from bgate_core.art import autotile as _autotile

        view = _gameview.load(_root())
        iso = view == "isometric"
        if iso and bits != 4:
            # FOUR IS NOT A COMPROMISE HERE, it is the shape of the thing. A
            # diamond has four edges and each one IS a cell neighbour, so the
            # 16-mask set is complete for an isometric floor. The 47-tile blob
            # exists because a square tile's corners are a shape its four
            # sides cannot describe; the diamond's corners are its vertices,
            # where two edges already meet.
            bits = 4
        from bgate_core.level import tilemap as _tilemap
        from bgate_core.art import tilemask as _tilemask

        root = _Path(_root())
        if bits not in (4, 8):
            return {"ok": False, "error": "bits is 4 (16 masks) or 8 (blob47)"}
        tile_px = int(tile_px)
        # AFTER the free validations, deliberately: "your view is isometric"
        # and "bits is 4 or 8" are answers about the REQUEST, and the more
        # specific answer must win over "nothing is keyed".
        refused = _provider_gate(_root(), "image", "a tileset generation")
        if refused:
            return refused

        out_dir = root / ".bgate_out" / "tiles"
        out_dir.mkdir(parents=True, exist_ok=True)
        spent = 0.0
        # AN ISOMETRIC TILE IS 2:1. `tile_px` names the tile's WIDTH, which is
        # the number everything else in the pipeline already means by it; the
        # height follows the projection rather than being asked for, because a
        # diamond whose height is not half its width is not the diamond the
        # engine will draw with these cells.
        tw, th = (tile_px, tile_px // 2) if iso else (tile_px, tile_px)

        def _texture(what: str, tag: str, variants: int = 0):
            """One tile of material, painted by kie at canvas size.

            The tile is CUT from a 4x-tile downscale of the painting rather
            than the whole frame squeezed into one tile — squeezing turns a
            wall of bricks into noise; cutting keeps the material at a scale
            where a brick is still a brick. ``variants`` returns that many
            MORE tiles, which are the same tile modulated rather than other
            crops of the painting — see `_cut`.
            """
            raw = out_dir / f"{name}_{tag}_raw.png"
            # REUSE THE PAINTING IF IT IS ALREADY HERE. Every geometry change
            # in this tool — the diamond carve, the panel masks, the sampling
            # scale — was iterated by regenerating art that had not changed,
            # which costs money, burns time and hands the provider another
            # chance to refuse a texture it already painted once. The raw
            # generation is kept beside the atlas precisely so it can be
            # re-cut; `reuse=False` forces a fresh roll.
            #
            # ONLY IF THE PROMPT STILL MATCHES. The cache used to key on the
            # filename alone, so calling again with a NEW prompt under the
            # same name returned the old art at spend 0 with nothing saying
            # the new words never reached a model. The painting's prompt is
            # stamped beside it and a mismatch is a fresh roll, not a hit.
            stamp = raw.with_name(raw.stem + ".prompt.txt")
            if reuse and raw.is_file():
                try:
                    same = stamp.read_text(encoding="utf-8") == what
                except OSError:
                    # A pre-stamp cache cannot prove its prompt; honour it
                    # once (the old behaviour) and stamp it on the way out.
                    same = True
                if same:
                    base, extra = _cut(_Img.open(raw).convert("RGBA"),
                                       variants)
                    try:
                        stamp.write_text(what, encoding="utf-8")
                    except OSError:
                        pass
                    return base, extra, 0.0
            # SAY WHAT YOU WANT, NOT WHAT YOU DO NOT. This asked for a
            # texture with "no border, no vignette, no objects", and a pile
            # of negations reads as an attempt to suppress rather than to
            # describe.
            prompt_text = _TEXTURE_STYLE.format(what=what)
            # AND RETRY, because the refusals are a COIN TOSS rather than a
            # verdict on the words. Measured here: "dark navy blue office
            # partition panel, matte painted surface" was refused by the
            # provider's safety filter on one call and generated on the very
            # next, unchanged; an office carpet did the same. A pipeline that
            # dies on a false positive makes the human rewrite a prompt that
            # was never the problem, so the retry happens here and the error
            # only surfaces once the provider has said no repeatedly.
            got = {}
            for attempt in range(3):
                # SEEDREAM FIRST, and the fallback is the interesting part.
                # The nano-banana family shares Google's safety filter, which
                # refuses plain material descriptions at random — the same
                # office carpet three times running, in words that had worked
                # minutes before. A texture pipeline cannot rest on that, so
                # the non-Google model leads and Google's is the second try.
                model = "seedream-4-t2i" if attempt == 0 else "nano-banana-2"
                got = _kie.generate_image(
                    prompt_text, str(raw), model=model,
                    size="1024x1024", task_kind="tile", tileable=True,
                    root=str(root))
                if got.get("ok"):
                    break
            if not got.get("ok"):
                why = str(got.get("error"))[:200]
                raise ValueError(
                    f"the {tag} texture failed three times: {why}"
                    + (" — that is the provider's safety filter refusing a "
                       "plain material description, which it does "
                       "intermittently; the prompt is not the problem and "
                       "re-running usually is the fix."
                       if "filter" in why or "Prohibited" in why else ""))
            cost = float(got.get("usd") or 0.02)
            try:
                stamp.write_text(what, encoding="utf-8")
            except OSError:
                pass          # an unstamped cache degrades to the old reuse
            base, extra = _cut(_Img.open(raw).convert("RGBA"), variants)
            return base, extra, cost

        def _cut(img, variants: int):
            """One painting to one tile plus its variants.

            Both the fresh roll and the `reuse` path went through their own
            copy of this, which is how the two drifted apart while the
            sampling rules were being iterated.
            """
            # SAMPLE THE PAINTING FINE, NOT BIG. At 4x the tile the crop held
            # a few large features, so the floor read as a handful of motifs
            # repeating — and mirroring them to kill the diamond seams only
            # turned the motifs into butterflies. A floor material wants many
            # small features per tile: that is what carpet, concrete and
            # stone actually look like at a metre away, and it is what makes
            # the repeat stop being legible at all.
            span = max(2, int(tile_px * _TEXTURE_ZOOM))
            zoom = img.resize((span, span), _Img.LANCZOS)
            # CROP the tile's real proportions out of the painting rather than
            # resizing a square into them. An isometric tile is 2:1, and
            # squashing a square crop to fit would halve the material's
            # vertical scale — the brick would still be a brick, drawn by a
            # bricklayer who had been stood on.
            ox0, oy0 = max(0, (span - tw) // 2), max(0, (span - th) // 2)
            # MIRROR-QUAD A HALF TILE, so the material is continuous across
            # the diagonals where diamonds actually meet — see
            # tilemask.mirror_tile for why "seamless" from the model is not
            # the same question.
            patch = zoom.crop((ox0, oy0, ox0 + tw // 2, oy0 + th // 2))
            base = _tilemask.mirror_tile(patch).resize((tw, th),
                                                       _Img.NEAREST)
            # VARIANTS ARE THE SAME TILE MODULATED, NOT A SECOND CROP. Cutting
            # them from elsewhere in the painting gave visibly different
            # swatches whose edge pixels no longer agreed, so alternating them
            # across a room broke the material's own grid at every join and
            # read as a patchwork quilt. drift_variant keeps the tile — grid,
            # alpha and edges untouched — and moves only a wrapped
            # low-frequency cast plus the flecks. See its docstring.
            extra = [_tilemask.drift_variant(
                base, phase=ph, drift=dr, lift=lf)
                for ph, dr, lf in ((5, 0.75, 0.0), (13, -0.62, 0.03),
                                   (29, 0.40, 0.07))[:variants]]
            return base, extra

        floor_tile, floor_variants, usd = _texture(prompt, "floor",
                                                   variants=3)
        spent += usd
        if iso and not void_prompt:
            # AN ISOMETRIC TILE'S BAND IS ITS OWN EDGE, NOT A NEIGHBOUR. In a
            # top-down level the void is a second terrain you genuinely see
            # between rooms, so it is worth painting. On a diamond the band is
            # the lip where this tile falls away — the floor's own material in
            # shadow — so deriving it costs nothing, cannot drift in hue from
            # the surface it edges, and removes a whole generation.
            #
            # It also removes a failure: asking a provider for "deep darkness,
            # near black" was refused by its safety filter twice here, because
            # an image request describing nothing visible reads as an attempt
            # to get nothing back. There is now nothing to refuse.
            void_tile = _Img.eval(
                floor_tile.convert("RGBA"),
                lambda c: int(c * _tilemask.ISO_BAND_LEVEL))
            void_tile.putalpha(floor_tile.convert("RGBA").getchannel("A"))
        else:
            void_tile, _unused, usd = _texture(
                void_prompt or "very dark charcoal grey stone in deep shadow, "
                               "faint rough texture, matte", "void")
            spent += usd

        # The working sheet is two tiles: the floor material and the void
        # material. Everything else is geometry.
        sheet = _Img.new("RGBA", (2 * tw, th), (0, 0, 0, 0))
        sheet.paste(floor_tile, (0, 0))
        sheet.paste(void_tile, (tw, 0))
        pinned = _artdirection.palette_pinned(str(root))
        raw_png = out_dir / f"{name}_raw.png"
        sheet.save(raw_png)
        if pinned:
            _spritekit.lock_palette(str(raw_png), pinned)
            sheet = _Img.open(raw_png).convert("RGBA")
            floor_tile = sheet.crop((0, 0, tw, th))
            void_tile = sheet.crop((tw, 0, 2 * tw, th))

        def _tile_mean(tx):
            return _tilemask._mean(list(
                sheet.crop((tx * tw, 0, (tx + 1) * tw, th))
                .convert("RGB").getdata()))

        colours = [_tile_mean(0), _tile_mean(1)]
        full = (_tilemask.BIT_N | _tilemask.BIT_E |
                _tilemask.BIT_S | _tilemask.BIT_W)
        wanted = (list(range(16)) if bits == 4
                  else _autotile.blob47_masks())
        # ONE EDGE INSET FOR THE WHOLE SET, same machinery the RD path used
        # to repair its sheets — here it is not a repair, it is the whole
        # construction: full-floor donor at (0,0), void donor at (1,0), and
        # every wanted mask carved between them. Coverage cannot be partial.
        #
        # The isometric branch is the SAME idea against a different geometry:
        # four diamond edges instead of four rect sides, and everything
        # outside the diamond transparent because that is what makes the tile
        # a diamond at all.
        if iso:
            norm = _tilemask.diamond_tiles(floor_tile, void_tile, wanted,
                                           tile_size=(tw, th))
        else:
            norm = _tilemask.normalise_edges(
                sheet, {full: (0, 0), 0: (1, 0)}, wanted,
                tile_size=(tw, th), colours=colours)
        if not norm.get("ok"):
            return {"ok": False, "stage": "compose",
                    "error": norm.get("reason"), "raw": str(raw_png),
                    "spend": {"usd": round(spent, 4)}}
        sheet, table = norm["image"], norm["table"]
        result_inset = norm["inset"]

        # BREAK THE WALLPAPER. One interior tile repeated across a floor is
        # the single loudest generated-level tell, so the atlas carries the
        # variant crops as extra tiles after the mask set, and the level
        # generators scatter them over interior cells. The sidecar is how
        # they learn which tiles those are — a .tres cannot say it.
        interior_at = table[15 if bits == 4 else 255]
        cols = max(1, sheet.width // tw)
        n0 = len(wanted)
        rows_need = (n0 + len(floor_variants) + cols - 1) // cols
        if rows_need * th > sheet.height:
            grown = _Img.new("RGBA", (sheet.width, rows_need * th),
                             (0, 0, 0, 0))
            grown.paste(sheet, (0, 0))
            sheet = grown
        # An isometric variant is the interior DIAMOND cut from another part
        # of the painting, not a rectangle: pasted square it would overdraw
        # its neighbours' corners and the level would grow opaque seams
        # exactly where the diamonds are meant to interlock.
        if iso:
            floor_variants = [
                _tilemask.diamond_tiles(v, v, [full],
                                        tile_size=(tw, th))["image"]
                for v in floor_variants]
        variant_coords = []
        for j, vt in enumerate(floor_variants):
            tx, ty = (n0 + j) % cols, (n0 + j) // cols
            sheet.paste(vt, (tx * tw, ty * th))
            variant_coords.append((tx, ty))

        atlas_png = out_dir / f"{name}.png"
        sheet.save(atlas_png)
        if pinned:
            _spritekit.lock_palette(str(atlas_png), pinned)
            sheet = _Img.open(atlas_png).convert("RGBA")
        # THE MANIFEST - the knowledge this generator holds and used to throw
        # away. Every atlas coordinate, the mask table, which source is
        # which: all of it was in this function's locals and only in this
        # function's locals, so the gameplay/tech seat re-typed it into
        # level_generate as ten floor_*/wall_* parameters and a string DSL,
        # hand-carried between seats in a pasted brief. The sidecar IS the
        # handoff now: level_generate and sidescroll_generate read it off
        # disk beside the .tres and the parameters collapse to nothing.
        # `interior`/`variants` stay at top level for the older reader
        # (_scatter_variants) - same file, superset schema. Built here,
        # WRITTEN ONCE after the walls exist, so the manifest can say what
        # the .tres actually defines.
        sidecar = {"kind": "bgate-tileset", "version": 1,
                   "tile_px": tile_px, "bits": bits,
                   "floor": {"source": 0,
                             "table": {str(m): list(c)
                                       for m, c in sorted(table.items())},
                             "solid": list(interior_at),
                             "variants": [list(v) for v in variant_coords]},
                   "interior": list(interior_at),
                   "variants": [list(v) for v in variant_coords]}
        sidecar_path = out_dir / f"{name}.tiles.json"
        # THE SEAM CHECK IS SQUARE-ONLY, and saying so beats reporting a
        # number that means nothing. It samples the tile RECT's edges and asks
        # whether neighbouring tiles agree there; on a diamond those edges are
        # the transparent corners, so every comparison would be one empty band
        # against another and every set would score perfect.
        seams = ({"checked": 0, "findings": [],
                  "skipped": "isometric: the tile rect's edges are the "
                             "diamond's transparent corners, so edge "
                             "continuity is not what holds an iso set "
                             "together — the shared diamond outline is, and "
                             "that is constructed rather than measured"}
                 if iso else
                 _tilemask.seam_report(sheet, table, tile_size=(tw, th),
                                       colours=colours))

        # WALLS, when asked for. A wall is a solid MASS whose shape comes
        # from the level's wall ring, not a 16-mask terrain — so this needs
        # one clean tile, not a set, and it is attached as a second source in
        # the same resource (level_generate addresses floor and wall by
        # source id). Toned DOWN relative to the floor on purpose: generated
        # wall art came back brighter than the floor, which reads as lit
        # paths around dark pits rather than as rooms with walls.
        wall_source = None
        if iso:
            # AN ISOMETRIC LEVEL NEEDS ITS WALLS AS BLOCKS, always — there is
            # no flat wall tile that reads as a wall in this projection, only
            # a raised cell with its two camera-facing sides showing. So this
            # is built whether or not a wall material was asked for: without
            # `wall_prompt` the block is the floor's own stone, and the face
            # shading alone is what separates a wall from the ground it
            # stands on (it is enough — the two planes are at different
            # angles to the light, which is the entire isometric illusion).
            lift = int(wall_lift) or th
            try:
                if wall_prompt:
                    wmat, _unused, usd = _texture(wall_prompt, "wall")
                    spent += usd
                else:
                    # THE WALL MASS IS ROCK, NOT FLOOR. Built from the floor's
                    # own tile the block came back with a top face identical
                    # to the ground, so a filled wall region rendered as a
                    # plateau of flagstone and the rooms were legible only by
                    # the shadow lines between them. The darkened material —
                    # the same one the floor's edge band is cut from — makes
                    # the raised mass read as stone and the walkable floor
                    # read as floor, which is the distinction the square path
                    # already makes by toning its wall to 0.62 of the floor.
                    wmat = void_tile
                def _diamond_of(mat):
                    return _tilemask.crop_tile(
                        _tilemask.diamond_tiles(mat, mat, [full],
                                                tile_size=(tw, th))["image"],
                        (0, 0), (tw, th))

                # THREE KINDS OF RAISED CELL, and the difference is what its
                # top is made of. A WALL's top is rock, because nothing stands
                # on it. A TERRACE's top is the floor, because something does.
                # A RAMP is a terrace with its top tilted toward the neighbour
                # one level down. Drawing them from one primitive is why a
                # ledge and the ramp leading onto it cannot disagree.
                # A WALL AND A STEP ARE NOT THE SAME HEIGHT, and drawing
                # them so is what turned the first terraced render into
                # noise: a terrace top and a wall top at one altitude give
                # the eye no profile to read, just two colours interleaved.
                # A barrier is full height; something you walk up is half of
                # it — which is also what makes the ramp a ramp rather than
                # a cliff with a slope painted on.
                step = max(2, lift // 2)
                parts = [("wall", _tilemask.iso_block(
                    _diamond_of(wmat), wmat, tile_size=(tw, th), lift=lift))]
                parts.append(("terrace", _tilemask.iso_block(
                    _diamond_of(floor_tile), wmat, tile_size=(tw, th),
                    lift=step)))
                for face in ("n", "e", "s", "w"):
                    parts.append((f"ramp_{face}", _tilemask.iso_ramp(
                        floor_tile, face, tile_size=(tw, th), lift=step)))
                # A WALL IN A BUILDING IS A PLANE, NOT A CUBE. The block
                # above is right for terrain — a plateau, a ledge — and wrong
                # for architecture: it eats the whole cell, so a floor built
                # from one-cell partitions renders as a maze of corridors
                # instead of rooms with walls between them. These are the
                # same wall at the same height with a narrow footprint.
                for mk in range(16):
                    parts.append((f"panel{mk}", _tilemask.iso_panel(
                        wmat, mk, tile_size=(tw, th), lift=lift)))
                for _kind, part in parts:
                    if not part.get("ok"):
                        raise ValueError(part.get("reason"))
                # ONE REGION AND ONE ORIGIN for the whole source, because an
                # atlas source has exactly one texture_region_size. A shorter
                # tile is pasted at the BOTTOM of the tall cell, so its own
                # top face lands `step` above the floor while the resource
                # still describes one rectangle.
                bw, bh = tw, th + lift
                strip = _Img.new("RGBA", (bw * len(parts), bh), (0, 0, 0, 0))
                blocks = {}
                for i, (kind, part) in enumerate(parts):
                    strip.paste(part["image"],
                                (i * bw, bh - part["size"][1]))
                    blocks[kind] = [i, 0]
                wall_png = out_dir / f"{name}_wall.png"
                strip.save(wall_png)
                if pinned:
                    _spritekit.lock_palette(str(wall_png), pinned)
                wall_source = {"ok": True, "path": str(wall_png),
                               "lift": lift,
                               "origin": list(parts[0][1]["origin"]),
                               "size": [bw, bh], "step": step,
                               "tiles": [tuple(v) for v in blocks.values()],
                               "blocks": blocks}
            except Exception as exc:                            # noqa: BLE001
                wall_source = {"ok": False,
                               "error": f"{type(exc).__name__}: {exc}"}
        elif wall_prompt:
            try:
                wtile, _unused, usd = _texture(wall_prompt, "wall")
                spent += usd
                wall_source = _wall_tile_from(wtile, sheet, tile_px, out_dir,
                                              name)
            except Exception as exc:                            # noqa: BLE001
                wall_source = {"ok": False,
                               "error": f"{type(exc).__name__}: {exc}"}

        # EXTRA FLOOR MATERIALS, one atlas source each. A floor with one
        # surface everywhere is the single loudest thing about a generated
        # level: real buildings change underfoot at every threshold — carpet
        # to walkway to lino to the cold vinyl of a server room — and the
        # layout that is being dressed already knows where those lines are,
        # because it drew them with different tiles. This is what lets a
        # re-skin keep them.
        extra_sources, extra_meta = [], {}
        for spec in [s for s in str(materials).split(";") if s.strip()]:
            mat_name, _, mat_prompt = spec.partition("=")
            mat_name = mat_name.strip() or f"mat{len(extra_sources) + 2}"
            mat_prompt = mat_prompt.strip() or mat_name
            try:
                mtile, mvars, usd = _texture(mat_prompt, mat_name, variants=3)
                spent += usd
                mnorm = _tilemask.diamond_tiles(mtile, mtile, wanted,
                                                tile_size=(tw, th))
                if not mnorm.get("ok"):
                    raise ValueError(mnorm.get("reason"))
                msheet, mtable = mnorm["image"], mnorm["table"]
                mcols = max(1, msheet.width // tw)
                n0m = len(wanted)
                need = (n0m + len(mvars) + mcols - 1) // mcols
                if need * th > msheet.height:
                    grown = _Img.new("RGBA", (msheet.width, need * th),
                                     (0, 0, 0, 0))
                    grown.paste(msheet, (0, 0))
                    msheet = grown
                mvar_at = []
                for j, vt in enumerate(mvars):
                    vt = _tilemask.diamond_tiles(vt, vt, [full],
                                                 tile_size=(tw, th))["image"]
                    txm, tym = (n0m + j) % mcols, (n0m + j) // mcols
                    msheet.paste(vt, (txm * tw, tym * th))
                    mvar_at.append([txm, tym])
                mpath = out_dir / f"{name}_{mat_name}.png"
                msheet.save(mpath)
                if pinned:
                    _spritekit.lock_palette(str(mpath), pinned)
                sid = 2 + len(extra_sources)
                extra_sources.append({
                    "id": sid,
                    "texture": f"res://{res_dir.strip('/')}/"
                               f"{name}_{mat_name}.png",
                    "tiles": sorted({(tx, ty) for ty in
                                     range(msheet.height // th)
                                     for tx in range(msheet.width // tw)
                                     if msheet.crop((tx * tw, ty * th,
                                                     (tx + 1) * tw,
                                                     (ty + 1) * th))
                                     .getbbox() is not None}),
                    "path": str(mpath)})
                extra_meta[mat_name] = {
                    "source": sid,
                    "interior": list(mtable[15 if bits == 4 else 255]),
                    "variants": mvar_at}
            except Exception as exc:                        # noqa: BLE001
                # ONE MATERIAL FAILING IS NOT THE SET FAILING. The provider
                # refuses plain descriptions at random, and losing a whole
                # tileset because the third of five textures was unlucky is
                # a worse answer than shipping four and naming the fifth.
                extra_meta[mat_name] = {"ok": False,
                                        "error": f"{type(exc).__name__}: {exc}"}

        # UPDATED, not rebuilt: the manifest keys (kind/floor/table) were
        # stamped where the table was minted, and rebuilding the dict here
        # silently dropped them once already in a bad merge.
        if extra_meta:
            sidecar["materials"] = extra_meta
        if iso and wall_source and wall_source.get("ok"):
            # WHERE THE RAISED TILES ARE, for the generator that places them.
            # A .tres can say a tile exists; it cannot say which one is a ramp
            # facing east, and that is the field level_generate needs to turn
            # a height map into cells. Written AFTER the blocks are built,
            # which is the kind of ordering a NameError is good at finding.
            sidecar["blocks"] = wall_source["blocks"]
            sidecar["lift"] = wall_source["lift"]
            # The blocks' own source, the contract the level tools read —
            # sources here are always floor 0 / wall strip 1.
            sidecar["wall_source"] = 1

        # COLLISION, derived from the inset the tiles were rebuilt with —
        # not traced from pixels, because the walkable region is a rectangle
        # we chose and tracing would rediscover it with jitter and hand Godot
        # fifty points per tile. Verified by physics rather than by the file:
        # without it a body stood in the void on 223 of 280 sampled frames,
        # with it on 0.
        # An ISOMETRIC floor gets NO colliders at all — walkable-by-omission,
        # the same rule the square path applies to interior tiles. The first
        # cut here put the full diamond on every mask, reasoning "the outline
        # is what stops the player"; but a TileSet physics polygon is a SOLID
        # OBSTACLE (see collision_polygons: the collider is the VOID), so
        # that made the entire floor collision and a CharacterBody2D spawned
        # embedded in it, unable to move. What stops an iso walker at the
        # floor's edge is the wall/void tiles beside it, which carry their
        # own solidity.
        if not collide:
            collision = {}
        elif iso:
            collision = {}
        else:
            collision = _tilemask.collision_polygons(
                sorted(table), tile_size=(tw, th),
                inset=result_inset or _tilemask.EDGE_INSET)

        res_texture = f"res://{res_dir.strip('/')}/{name}.png"
        # EVERY TILE IN THE ATLAS, not just the chosen terrain's table. The
        # table is the autotiling vocabulary for ONE terrain; the resource is
        # what Godot can paint with at all, and a coordinate missing from it
        # cannot be placed by anything — level_generate refused a hand-picked
        # floor tile for exactly this reason, and the tile was sitting in the
        # atlas the whole time.
        all_tiles = sorted({
            (tx, ty)
            for ty in range(sheet.height // th)
            for tx in range(sheet.width // tw)
            if sheet.crop((tx * tw, ty * th, (tx + 1) * tw, (ty + 1) * th))
            .getbbox() is not None})
        sources = [{"id": 0, "texture": res_texture, "tiles": all_tiles,
                    "collision": {table[m]: polys
                                  for m, polys in collision.items()
                                  if m in table and polys}}]
        if wall_source and wall_source.get("ok"):
            sources.append({
                "id": 1,
                "texture": f"res://{res_dir.strip('/')}/{name}_wall.png",
                "tiles": wall_source.get("tiles") or [(0, 0)],
                # A BLOCK IS TALLER THAN ITS CELL, and the resource has to say
                # so twice: the region is the art's real size, and the origin
                # puts the art's bottom edge on the diamond's bottom vertex so
                # the top face lands exactly `lift` above the floor plane.
                # ORIGINS, PLURAL — texture_origin is TileData, not source
                # data. Written at the source level Godot accepts the file,
                # ignores the key and reports origin (0, 0), which drew every
                # wall at half the height it was built with: plausible enough
                # in a screenshot, wrong by lift/2 everywhere. The engine
                # probe is what said so.
                **({"region": tuple(wall_source["size"]),
                    "origins": {tuple(at): tuple(wall_source["origin"])
                                for at in wall_source["tiles"]}}
                   if iso else {}),
                # The wall is solid all the way through, so its collider is
                # the whole cell rather than an edge band.
                "collision": ({at: [_tilemask.diamond_polygon((tw, th))]
                               for at in map(tuple,
                                             wall_source.get("tiles")
                                             or [(0, 0)])}
                              if iso else
                              {(0, 0): [[(-tw / 2, -th / 2),
                                         (tw / 2, -th / 2),
                                         (tw / 2, th / 2),
                                         (-tw / 2, th / 2)]]})
                if collide else {}})
        for src in extra_sources:
            sources.append({k: v for k, v in src.items() if k != "path"})
        shape, layout = ((_tilemap.ISOMETRIC, _tilemap.DIAMOND_DOWN) if iso
                         else (_tilemap.SQUARE, _tilemap.DIAMOND_RIGHT))
        tres_text = _tilemap.write_tileset(
            sources, tile_size=(tw, th), shape=shape, layout=layout,
            physics=bool(collide))
        tres_path = out_dir / f"{name}.tres"
        tres_path.write_text(tres_text, encoding="utf-8")

        # Written AFTER the wall block so the manifest can say whether a wall
        # source exists at all - a level generator that guesses one draws a
        # wall out of source ids the .tres does not define.
        if wall_source and wall_source.get("ok"):
            sidecar["wall"] = {"source": 1, "layout": "solid",
                               "atlas": [0, 0]}
        sidecar_path.write_text(_json.dumps(sidecar, indent=1),
                                encoding="utf-8")

        result = {"ok": True, "name": name,
                  "atlas": str(atlas_png), "tileset": str(tres_path),
                  # The cross-seat handoff, on disk. Hand level_generate /
                  # sidescroll_generate ONLY the tileset path - they read
                  # this file beside it and no atlas coordinate needs typing.
                  "manifest": str(sidecar_path),
                  "tile_px": tile_px, "tile_size": [tw, th], "bits": bits,
                  "view": view, "shape": "isometric" if iso else "square",
                  "colours": {"floor": [round(c) for c in colours[0]],
                              "void": [round(c) for c in colours[1]]},
                  # Coverage is total BY CONSTRUCTION — every mask is carved
                  # from the two textures, so there is no roll to fail and
                  # nothing to refuse. The seam report and the engine load
                  # are the gates that remain.
                  "coverage": {"have": len(table), "want": len(wanted),
                               "constructed": True},
                  "table": {str(m): list(c) for m, c in sorted(table.items())},
                  # The tile that is SAFE AS A SOLID FILL — mask 15/255 is
                  # "every neighbour is me", i.e. an interior. Surfaced because
                  # a caller picking atlas coordinates by eye picks an edge
                  # tile and paints a level entirely out of seams.
                  "solid": list(interior_at),
                  "interior_variants": [list(v) for v in variant_coords],
                  **({"materials": extra_meta} if extra_meta else {}),
                  "seams": seams, "edge_inset": result_inset,
                  **({"wall": wall_source} if wall_source else {}),
                  "collision": {"tiles": len([m for m, v in collision.items()
                                              if v]),
                                "polygons": sum(len(v) for v in
                                                collision.values())},
                  "spend": {"usd": round(spent, 4), "provider": "kie"}}

        master = _tileset_master_for(str(atlas_png), out_dir / f"{name}.aseprite",
                                     (tw, th))
        if master is not None:
            result["aseprite"] = master

        if install and godot_project:
            proj = _Path(_assets.normalize_path(root, godot_project))
            proj = proj if proj.is_absolute() else root / proj
            dest_dir = proj / res_dir.strip("/")
            dest_dir.mkdir(parents=True, exist_ok=True)
            import shutil as _sh
            _sh.copyfile(atlas_png, dest_dir / f"{name}.png")
            _sh.copyfile(sidecar_path, dest_dir / f"{name}.tiles.json")
            for src in extra_sources:
                _sh.copyfile(src["path"],
                             dest_dir / _Path(src["path"]).name)
            if wall_source and wall_source.get("ok"):
                _sh.copyfile(wall_source["path"], dest_dir / f"{name}_wall.png")
            (dest_dir / f"{name}.tres").write_text(tres_text, encoding="utf-8")
            result["installed"] = {
                "atlas": str(dest_dir / f"{name}.png"),
                "tileset": str(dest_dir / f"{name}.tres")}
            # A NEWLY COPIED PNG DOES NOT EXIST AS FAR AS GODOT IS CONCERNED.
            # Godot 4 loads textures through their .import metadata, so a
            # tileset whose ExtResource names a never-imported file fails to
            # load with no useful error — measured: the engine gate caught
            # exactly this on the first end-to-end run, and the resource was
            # perfectly well-formed. check_project already runs --import, so
            # the pass exists; it just was not on this path.
            result["import"] = _godot.check_project(str(proj))
            # THE ONLY CHECK THAT COUNTS: our writer agreeing with our own
            # parser proves nothing, because both were written against the
            # same reading of a format neither owns.
            result["engine"] = _godot.inspect_tileset(
                str(proj), f"res://{res_dir.strip('/')}/{name}.tres")

        _log("art", f"tileset {name}: {len(table)}/{len(wanted)} masks, "
                    f"${spent:.2f}", ref=str(atlas_png))
        return result
    except Exception as exc:
        return _fail(exc)


def _tileset_master_for(atlas: str, out, tile_size) -> Optional[dict]:
    """Best-effort .aseprite tileset beside the atlas. None without Aseprite."""
    from bgate_adapters import aseprite as _ase
    if not _ase.available().get("available"):
        return None
    try:
        return _ase.tileset_master(atlas, str(out), tile_size=tile_size)
    except Exception as exc:                                    # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@_tool
def image_generate(prompt: Annotated[str, Field(description='What to paint. Framing and background contracts are appended automatically.')], filename: Annotated[str, Field(description='Output name relative to the project\'s .bgate_out/art/ (e.g. "tommy_portrait.png").')], size: Annotated[str, Field(description='WxH the provider is asked for. Default 1024x1024; task_kind=texture forces square.')] = "1024x1024",
                   quality: Annotated[str, Field(description='low | medium | high; sets the per-image price. Default medium.')] = "medium", transparent: Annotated[bool, Field(description="True runs the keyable-background contract (chroma backdrop, keyed, audited); never the API's own alpha.")] = False,
                   ref_images: Annotated[Optional[list[str]], Field(description='Pin NAMES from ref_list (preferred; `name@r2` reaches an older revision) or absolute paths to condition on.')] = None,
                   use_pinned: Annotated[str, Field(description='Pull the project\'s own anchors by kind (character | style | ui | concept) or "all"; capped at the first 4 pins.')] = "", anchors: Annotated[Optional[list[str]], Field(description='Extra images used ONLY to choose the chroma key colour; never sent to the model.')] = None,
                   task_kind: Annotated[str, Field(description='What is being made: texture, decal, anchor/animation/item/sprite (keyed), background/tile/ui/concept (never keyed). Omit to follow `transparent`.')] = "", tileable: Annotated[bool, Field(description='With task_kind=texture, run the mirrored seam post-pass for a repeating field.')] = False,
                   ref_strength: Annotated[float, Field(description='How hard a reference pulls, 0-1 (Krea-side). Default 0.5.')] = 0.5, provider: Annotated[str, Field(description='"" picks the configured provider (openai, else krea); a name forces it and surfaces that provider\'s own key error.')] = "",
                   model: Annotated[str, Field(description='Provider-specific model id; "" takes the provider\'s default.')] = "") -> dict:
    """Generate PAINTED art - portraits, select-screen cards, title splashes,
    textures, decals, stage paint-overs. Costs real money per image
    (~$0.02-0.19).

    provider "" picks what is CONFIGURED (openai, else krea); naming one
    forces it. Condition on refs: ref_images (pin NAMES), use_pinned (a kind
    or "all"), ref_strength. task_kind changes real decisions: texture forces
    square/flat-albedo, decal keys lettering, sprite kinds are keyed,
    background/tile/ui/concept never are. transparent=True runs the
    keyable-background contract, not API alpha. filename is relative to
    .bgate_out/art/. LOOK before importing.
    Full notes: docs/tools.md#image_generate
    """
    root = _Path(_scratch_root())
    refused = _provider_gate(str(root), "image",
                           f"generating {filename!r}")
    if refused:
        return refused
    out = _art_out(root, filename)
    from bgate_adapters import imagegen
    named = [str(r) for r in (ref_images or []) if str(r).strip()]
    resolved = [_refs.resolve(root, r) for r in named]
    pinned_names, pinned_paths = _pinned_refs(root, use_pinned)
    for name, path in zip(pinned_names, pinned_paths):
        if path not in resolved:          # an explicit ref wins its slot
            named.append(name)
            resolved.append(path)
    anchor_paths = [_refs.resolve(root, a)
                    for a in (anchors or []) if str(a).strip()]
    # keyed=None hands the decision to task_kind (chroma.needs_key). With no
    # task_kind that answers False, which is exactly what this tool did
    # before any of these parameters existed.
    # PROVIDER IS A CHOICE NOW, not a constant. This was hardcoded to
    # openai, so a user whose only key is KREA_API_KEY - a key the setup
    # docs tell them to configure - could not reach this tool at all, and
    # Krea images were only obtainable through image_sprites and
    # image_talkhead, the two tools that happened to expose `provider`.
    # chroma.generate has dispatched to either since it was written; the
    # tool simply never passed the choice along.
    result = _chroma.generate(prompt, str(out),
                              provider=_providers.provider_for(
                                  task_kind, asked=provider, root=root),
                              model=model,
                              task_kind=task_kind,
                              keyed=True if transparent else None,
                              size=size, quality=quality, transparent=False,
                              ref_paths=resolved, ref_strength=ref_strength,
                              anchors=anchor_paths, tileable=tileable,
                              root=root,
                              logical_name=_Path(filename).stem,
                              work_item_id=_work_item_id())
    result["refs_used"] = named
    # WHAT HAPPENED, NOT WHAT WAS ASKED FOR. `tileable` above is the request;
    # chroma puts the mirror pass's own {ok, method, note} at result["tileable"]
    # and it can be a failure. Recording the boolean meant a map that never
    # tiled was filed as a tileable map, and the seam turned up in a render
    # days later with the metadata still claiming otherwise. A flag computed
    # from the request is not evidence about the artifact.
    tiled = result.get("tileable")
    tiled_ok = bool(tiled.get("ok")) if isinstance(tiled, dict) else bool(tiled)
    if tileable and not tiled_ok:
        # Surfaced on the result, not buried in metadata: the caller is
        # standing right here and can regenerate or heal the seam now.
        result["warning"] = (
            "tileable was requested and DID NOT happen: "
            + (tiled.get("note") if isinstance(tiled, dict) else "no tile pass ran")
            + " - this map will seam where it repeats")
    if result.get("ok"):
        archived = _archive_preview(result["path"], f"art-{_Path(filename).stem}")
        if archived:
            result["preview"] = archived
        artifact = _register_artifact(
            _Path(filename).stem, result["path"], producer="image_generate",
            model=result.get("model", ""), prompt=prompt, refs=named,
            metadata={"size": size, "quality": quality,
                      "transparent": transparent,
                      "task_kind": task_kind,
                      "tileable_requested": tileable,
                      "tileable": tiled_ok,
                      "tileable_detail": tiled if isinstance(tiled, dict) else None,
                      "resolved_refs": resolved,
                      "pinned_refs": pinned_names,
                      "anchors": anchor_paths,
                      "keyed": result.get("keyed"),
                      "chroma": result.get("chroma"),
                      "alpha": result.get("alpha"),
                      "preview": archived or "",
                      **imagegen.cost_meta(result)})
        if artifact:
            result["artifact"] = artifact
        _log("art", f"generated painted art {filename} ({size}, {quality}"
                    + (f", {task_kind}" if task_kind else "") + ")",
             ref=archived or result["path"])
    return result


@_tool
def image_edit(prompt: str, ref_images: list[str], filename: str,
               size: str = "1024x1536", quality: str = "medium",
               transparent: bool = False) -> dict:
    """Generate an image CONDITIONED ON reference image(s) - the consistency
    primitive, exposed raw.

    Use it to regenerate a single sprite pose against a character's existing
    reference (~$0.04 at medium) or to derive on-model variants. ref_images:
    PINNED REFERENCE NAMES (see ref_list) or absolute paths. filename lands
    under .bgate_out/art/; the result is archived to the gallery - LOOK at it.
    transparent=True runs the keyable-background contract, not the API's
    background=transparent.
    Full notes: docs/tools.md#image_edit
    """
    root = _Path(_scratch_root())
    refused = _provider_gate(str(root), "image", f"editing into {filename!r}")
    if refused:
        return refused
    out = _art_out(root, filename)
    from bgate_adapters import imagegen
    resolved = [_refs.resolve(root, r) for r in ref_images]
    # WAS hardcoded "openai" with no parameter — the pin image_generate's
    # comment calls the defect, fixed in one tool out of five. A Krea-only
    # setup got "OPENAI_API_KEY not set" from every edit.
    result = _chroma.generate(prompt, str(out),
                              provider=_providers.provider_for(root=root),
                              keyed=bool(transparent), ref_paths=resolved,
                              size=size, quality=quality, transparent=False,
                              root=root, logical_name=_Path(filename).stem,
                              work_item_id=_work_item_id())
    if result.get("ok"):
        archived = _archive_preview(result["path"], f"edit-{_Path(filename).stem}")
        if archived:
            result["preview"] = archived
        artifact = _register_artifact(
            _Path(filename).stem, result["path"], producer="image_edit",
            model=result.get("model", ""), prompt=prompt, refs=ref_images,
            metadata={"resolved_refs": resolved, "size": size,
                      "quality": quality, "transparent": transparent,
                      "chroma": result.get("chroma"),
                      "alpha": result.get("alpha"),
                      "preview": archived or "",
                      **imagegen.cost_meta(result)})
        if artifact:
            result["artifact"] = artifact
        _log("art", f"reference-edit {filename}", ref=archived or result["path"])
    return result


# ---------------------------------------------------------------------------
# The item-art pipeline - item-as-object, class-templated, Codex-drivable.
# Variants are cheap and classes are expensive: one prompt template per class
# holds framing/light/scale/background invariant, a parameter grid mints the
# variants. See bgate_core/art/items.py for the taxonomy and the pure builders.
# ---------------------------------------------------------------------------
@_tool
def item_classes() -> dict:
    """The item-art taxonomy: the classes, their equip slot, and the variant
    axes. This IS the contract to drive item_generate / item_variants - read it
    before minting gear so names/slots line up with the equip/layer system."""
    return {
        "ok": True,
        "classes": {
            name: {"label": c["label"], "slot": c["slot"], "worn": c["worn"],
                   "subject": c["subject"]}
            for name, c in _items.ITEM_CLASSES.items()
        },
        "axes": {"material": "free text (e.g. iron, damascus steel, bone)",
                 "element": list(_items.ELEMENTS),
                 "tier": list(_items.TIERS)},
        "slots": list(_items.SLOTS),
    }


def _item_style_clause(root: _Path, character: str) -> str:
    """The cross-leg style rail: a character's stored visual profile -> the
    style clause appended to every item prompt, so worn gear reads as the same
    set as the body it hangs on. Same fallback chain image_sprites uses.
    Naming a character with no profile raises - silently minting unstyled gear
    would LOOK like a result."""
    if not character.strip():
        return ""
    for key in (character, f"{character}-character"):
        profile = _refs.profile_get(root, key)
        if profile:
            return profile.get("style", "")
    raise ValueError(
        f"no visual profile for {character!r} - set one with profile_set "
        "(or drop the character param to mint unstyled)")


def _index_item(root: _Path, man: dict) -> bool:
    """Upsert one manifest into .bgate_out/items/_index.json - the one-shot
    rollup the equip UI reads. Loose per-item manifests stay the source of
    truth; a missing/corrupt index is rebuilt from them, never trusted."""
    path = root / _items.INDEX_REL
    index: dict = {}
    try:
        loaded = _json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("items"), dict):
            index = loaded
    except Exception:
        pass
    if not index:  # first write or corrupt - rebuild from the loose manifests
        index = {"items": {}}
        for f in sorted(path.parent.glob("*.json")) if path.parent.is_dir() else []:
            if f.name == path.name:
                continue
            try:
                loose = _json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(loose, dict) and loose.get("name"):
                index["items"][loose["name"]] = loose
    _items.update_index(index, man)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(index, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False  # rollup is a cache; never a reason to lose a made item


def _mint_item(root: _Path, spec: dict, quality: str) -> dict:
    """Generate one item from a variant spec, then archive + track + manifest.

    A single spec (from items.plan_variants) carries its own prompt, so this is
    pure I/O: paint it on a keyable backdrop, key + audit it to real alpha,
    register provenance, track the binary so the QA gate and dashboard see it,
    and drop the JSON bridge record the equip system reads. Returns the per-item
    result; failures are reported, not raised, so one bad variant never sinks a
    batch.

    Gear is a LAYER - it hangs on a fighter, so its background is not part of
    the asset. That makes it sprite-shaped and it goes through the keyable
    contract: the items STYLE clause asks for "fully transparent background",
    which is a wish no model in either provider grants."""
    from bgate_adapters import imagegen
    rel = _items.rel_art_path(spec["item_class"], spec["name"])
    out = root / rel
    result = _chroma.generate(spec["prompt"], str(out),
                              provider=_providers.provider_for("item",
                                                               root=root),
                              task_kind="item", quality=quality, root=root,
                              logical_name=spec["name"],
                              work_item_id=_work_item_id())
    if not result.get("ok"):
        return {"ok": False, "name": spec["name"], "error": result.get("error"),
                "alpha": result.get("alpha"), "prompt": spec["prompt"]}

    # Project-palette conform, BEFORE the preview is archived — the preview a
    # human approves must be the pixels that ship. Advisory when it cannot run;
    # an item minted without a pinned palette is the pre-palette status quo.
    conform = None
    palette = _artdirection.palette_pinned(root)
    if palette:
        got = _spritekit.lock_palette(result["path"], palette)
        conform = ({"ok": True, "colors": len(palette), "source": "bible",
                    "changed": got.get("changed")}
                   if got.get("ok") else got)

    archived = _archive_preview(result["path"], f"item-{spec['name']}")
    _register_artifact(spec["name"], result["path"], producer="item_generate",
                       model=result.get("model", ""), prompt=spec["prompt"],
                       metadata={"item_class": spec["item_class"],
                                 "slot": spec["slot"], "params": spec["params"],
                                 "chroma": result.get("chroma"),
                                 "alpha": result.get("alpha"),
                                 "preview": archived or "",
                                 **imagegen.cost_meta(result)})
    try:
        _assets.track(root, out)
    except Exception:
        pass  # tracking is provenance, never a reason to lose a made file
    man = _items.manifest(spec, rel)
    man_path = root / _items.rel_manifest_path(spec["name"])
    man_path.parent.mkdir(parents=True, exist_ok=True)
    man_path.write_text(_json.dumps(man, indent=2), encoding="utf-8")
    indexed = _index_item(root, man)
    out_row = {"ok": True, "name": spec["name"], "item_class": spec["item_class"],
               "slot": spec["slot"], "sprite": rel,
               "manifest": _items.rel_manifest_path(spec["name"]),
               "indexed": indexed,
               "preview": archived or result["path"]}
    if conform:
        out_row["palette"] = conform
    return out_row


@_tool
def item_generate(item_class: Annotated[str, Field(description='One of item_classes(): main_hand, off_hand, head, body, feet, consumable, throwable, ranged.')], name: Annotated[str, Field(description='Logical asset name the icon and its manifest are filed under.')], descriptor: Annotated[str, Field(description='What the item is, e.g. "curved saber".')],
                  material: Annotated[str, Field(description='Variant axis: the material it is made of.')] = "", element: Annotated[str, Field(description='Variant axis: elemental affinity.')] = "", tier: Annotated[str, Field(description='Variant axis: quality tier.')] = "",
                  quality: Annotated[str, Field(description='low | medium | high; sets the per-image price. Default medium.')] = "medium", character: Annotated[str, Field(description='Pinned ref name with a visual profile (profile_set); its style is appended so the gear matches the fighter.')] = "",
                  force: Annotated[bool, Field(description='Regenerate even when a manifest for this item already exists. Default False.')] = False) -> dict:
    """Mint ONE gear/item icon - transparent, class-templated, tracked.

    item_class is one of item_classes(); descriptor names the item ("curved
    saber"); material/element/tier are the variant axes. `character` names a
    pinned ref with a visual profile so worn gear matches its fighter. An
    already-minted item (manifest on disk) is skipped, not re-bought;
    force=true regenerates. Costs ~$0.02-0.19 per image at `quality`. For a
    batch use item_variants. LOOK at the preview before importing.
    Full notes: docs/tools.md#item_generate
    """
    root = _Path(_root())
    refused = _provider_gate(str(root), "image", f"generating item {name!r}")
    if refused:
        return refused
    style_clause = _item_style_clause(root, character)
    [spec] = _items.plan_variants(
        item_class, name, descriptor,
        materials=[material] if material else None,
        elements=[element] if element else None,
        tiers=[tier] if tier else None,
        style_clause=style_clause)
    if not force:
        _, skipped = _items.split_existing(
            [spec], lambda rel: (root / rel).is_file())
        if skipped:
            return {"ok": True, "name": spec["name"], "skipped": True,
                    "manifest": _items.rel_manifest_path(spec["name"]),
                    "estimated_cost_usd": 0.0,
                    "note": "already minted - manifest exists; pass "
                            "force=true to re-buy"}
    res = _mint_item(root, spec, quality)
    if res.get("ok"):
        res["estimated_cost_usd"] = _items.estimate_cost(1, quality)
        res["style_rail"] = bool(style_clause)
        _log("art", f"minted {item_class} item {spec['name']}",
             ref=res["preview"])
    return res


@_tool
def item_variants(item_class: Annotated[str, Field(description='One of item_classes(): main_hand, off_hand, head, body, feet, consumable, throwable, ranged.')], base_name: Annotated[str, Field(description="Stem every variant's asset name is built from.")], descriptor: Annotated[str, Field(description='What the item is, e.g. "curved saber".')],
                  materials: Annotated[Optional[list[str]], Field(description='Material axis values; one variant per combination with the other axes.')] = None,
                  elements: Annotated[Optional[list[str]], Field(description='Element axis values.')] = None,
                  tiers: Annotated[Optional[list[str]], Field(description='Tier axis values.')] = None,
                  quality: Annotated[str, Field(description='low | medium | high; sets the per-image price. Default medium.')] = "medium", limit: Annotated[int, Field(description='Maximum NEW images this run may buy; the plan is refused past it. Default 12.')] = 12,
                  character: Annotated[str, Field(description='Pinned ref name with a visual profile; its style is woven into every prompt.')] = "", force: Annotated[bool, Field(description='Re-buy variants that already have a manifest on disk. Default False.')] = False) -> dict:
    """Mint a BATCH of variants of one class from a parameter grid - the
    cartesian product of the axes you pass, each a self-contained item.

    Pass materials/tiers/elements lists and get one on-set icon per
    combination; `character` weaves a pinned ref's profile into every prompt.
    Already-minted variants are skipped and reported; force=true re-buys.
    `limit` caps what a run may BUY (default 12): the plan and its $ estimate
    are reported and REFUSED if new images exceed the cap. LOOK at the set
    before importing.
    Full notes: docs/tools.md#item_variants
    """
    root = _Path(_root())
    style_clause = _item_style_clause(root, character)
    specs = _items.plan_variants(item_class, base_name, descriptor,
                                 materials=materials, elements=elements,
                                 tiers=tiers, style_clause=style_clause)
    to_mint, skipped = (specs, []) if force else _items.split_existing(
        specs, lambda rel: (root / rel).is_file())
    estimate = _items.estimate_cost(len(to_mint), quality)
    if len(to_mint) > max(1, limit):
        return {"ok": False, "planned": len(specs),
                "to_buy": len(to_mint), "already_minted": len(skipped),
                "limit": limit, "estimated_cost_usd": estimate,
                "names": [s["name"] for s in to_mint],
                "error": f"grid needs {len(to_mint)} new images "
                         f"(~${estimate:.2f} at {quality!r}, > limit "
                         f"{limit}); raise limit to confirm the spend or "
                         "narrow the axes"}
    # AFTER the free planning refusals - "your grid is over the limit you
    # set" is an answer about the request, and the more specific answer must
    # win over "nothing is keyed".
    refused = _provider_gate(str(root), "image",
                           f"generating {len(to_mint)} variants of {base_name!r}")
    if refused:
        return refused
    results = [_mint_item(root, s, quality) for s in to_mint]
    made = [r for r in results if r.get("ok")]
    _log("art", f"minted {len(made)}/{len(to_mint)} {item_class} variants "
         f"of {base_name}"
         + (f" ({len(skipped)} already on disk)" if skipped else ""))
    return {"ok": all(r.get("ok") for r in results),
            "class": item_class, "count": len(made),
            "skipped": [s["name"] for s in skipped],
            "estimated_cost_usd": estimate,
            "style_rail": bool(style_clause),
            "items": results}


def _guide_image(result: dict) -> list[str]:
    path = (result or {}).get("guides_png_abs")
    return [path] if path else []


@_tool
def sprite_sheet_slice(image: str, out_dir: str = "", pad: int = 0,
                       min_px: int = 16) -> dict:
    """Find every sprite on an IRREGULAR sheet and cut it out. Free and local.

    For the sheet with no grid: connected-alpha analysis turns each blob of
    ink into a box, ignores speckle under `min_px`, grows each box by `pad`,
    and returns them in reading order so slice N is frame N. With `out_dir`
    each box is also cropped to <stem>_NN.png; without it nothing is written -
    call it bare first, then again with out_dir once the boxes look right.
    Full notes: docs/tools.md#sprite_sheet_slice
    """
    root = _Path(_root())
    rel = _assets.normalize_path(root, image)
    src = root / rel
    if not src.exists():
        return {"ok": False, "error": f"no image at {rel}"}
    _contained_path(out_dir, "out_dir")
    boxes = _spritekit.islands(str(src), min_px=int(min_px), pad=int(pad))
    result: dict = {"ok": True, "count": len(boxes), "slices": boxes}
    if out_dir and boxes:
        from PIL import Image as _Img
        dest = root / out_dir
        dest.mkdir(parents=True, exist_ok=True)
        written = []
        with _Img.open(src) as im:
            rgba = im.convert("RGBA")
            for n, b in enumerate(boxes):
                cell = rgba.crop((b["x"], b["y"],
                                  b["x"] + b["w"], b["y"] + b["h"]))
                out_path = dest / f"{src.stem}_{n:02d}.png"
                cell.save(out_path)
                written.append(_assets.normalize_path(root, out_path))
        result["cells"] = written
        _note_tool_write(_root(), str(dest / f"{src.stem}_00.png"))
    return result


@_tool(images=_guide_image)
def sprite_sheet_check(image: str, columns: int, rows: int = 1,
                       labels: Optional[list[str]] = None,
                       row_labels: Optional[list[str]] = None,
                       guides: bool = True) -> dict:
    """LOOK AT A GENERATED POSE ROW OR CHARACTER SHEET BEFORE SPENDING ANYTHING
    ELSE ON IT. Free - calls no model, buys nothing, changes nothing.

    Call it the moment a multi-figure image comes back. Returns named
    findings (foot_drift, head_drift, size_drift, size_ramp, facing_flip,
    stray_ink, empty_cell per row; sheet_size_drift, sheet_size_ramp,
    band_palette across rows) plus an ANNOTATED COPY of the image. A ramp
    means the drift is monotonic: re-rolling will not fix it - generate each
    pose against ONE reference (image_sprites). Advisory, never a gate.
    Full notes: docs/tools.md#sprite_sheet_check
    """
    root = _Path(_root())
    rel = _assets.normalize_path(root, image)
    src = root / rel
    if not src.exists():
        return {"ok": False, "error": f"no image at {rel}"}
    report = _spritekit.row_report(src, int(columns), int(rows),
                             labels=labels, row_labels=row_labels)
    report["image"] = rel
    if guides:
        out = src.with_name(f"{src.stem}_guides.png")
        drawn = _spritekit.draw_guides(src, int(columns), out, int(rows),
                                 report=report)
        report["guides_png"] = _assets.normalize_path(root, out)
        report["guides_png_abs"] = str(out)
        report["guides_note"] = drawn["note"]
    if not report["flagged"]:
        report["note"] = (
            "nothing measurable is wrong with this sheet: the feet are on a "
            "line, the draw size holds, no head is yawed against its row and "
            "there is no ink on the canvas that is not the character. This "
            "says nothing about whether it is the right CHARACTER - that is "
            "consistency_check, which asks a model, because identity is not "
            "arithmetic.")
    return report


@_tool
def item_to_spriteframes(sprite: str, name: str, res_dir: str = "assets/gear",
                         frame_size: Optional[list[int]] = None) -> dict:
    """Wrap a single item PNG into a 1-frame Godot SpriteFrames .tres so it drops
    straight into an equip slot - the bridge from the item pipeline to the
    equip/layer system (templates/2d gear_rig.gd).

    A static held weapon/shield with one frame is the honest v1 for worn gear: it
    shows in-hand and rides the fighter's facing, before the per-frame worn-gear
    rig exists. sprite is a repo-relative or absolute PNG path. Emits the .tres
    next to the sheet the equip layer will load from res://<res_dir>/."""
    root = _Path(_root())
    rel = _assets.normalize_path(root, sprite)
    src = root / rel
    if not src.exists():
        return {"ok": False, "error": f"no image at {rel}"}
    from PIL import Image as _Img
    with _Img.open(src) as im:
        size = tuple(frame_size) if frame_size else im.size
    slug = _items.slugify(name)
    out_dir = src.parent
    sheet_name = f"{slug}_sheet.png"
    # The single frame IS the sheet - copy under the sheet name the tres
    # expects, so the pair imports together like every other SpriteFrames.
    from shutil import copyfile
    copyfile(src, out_dir / sheet_name)
    tres = _sprites._sprite_frames_tres(  # noqa: SLF001 - shared emitter
        sheet_name, [("default", 1)], (int(size[0]), int(size[1])),
        1.0, res_dir)
    tres_rel = out_dir / f"{slug}_frames.tres"
    tres_rel.write_text(tres, encoding="utf-8")
    return {"ok": True, "tres": _assets.normalize_path(root, tres_rel),
            "sheet": _assets.normalize_path(root, out_dir / sheet_name),
            "animation": "default", "res_dir": res_dir}


@_tool
def sprite_contract_get(character: str = "", action: str = "") -> dict:
    """The project's SPRITE CONTRACT - the declared shape of every sheet.

    View (side / top-down / isometric), direction set, which directions are
    DRAWN vs mirrored at runtime, cell size, layout, frames per action, and
    per-character overrides. Generation, checks and emitters all read this;
    it is how a four-corner top-down game and an E/W side-scroller use the
    same pipeline. With character/action, returns the fully resolved contract
    for that piece of work (override ladder applied)."""
    root = _root()
    from bgate_core.art import spritecontract as _sc
    if character or action:
        return {"ok": True, **_sc.contract_for(root, character, action)}
    return {"ok": True, **_sc.load(root),
            "presets": sorted(_sc.PRESETS)}


@_tool
def sprite_contract_set(preset: str = "", patch: Optional[dict] = None) -> dict:
    """Declare or change the sprite contract. preset one of single /
    sidescroller / four_corner / four_dir / eight_dir, then patch overrides
    individual fields (cell, actions, characters, ...). A preset REPLACES the
    shape wholesale - half of one preset merged into another is how
    contradictions are born. Without preset, patch edits the stored contract.

    This is the swappable-config lever: setting a different preset reshapes
    what every future generation produces, what the battery expects, and how
    sheets are laid out, with no other changes anywhere."""
    root = _root()
    from bgate_core.art import spritecontract as _sc
    if preset:
        saved = _sc.apply_preset(root, preset, patch)
    else:
        current = _sc.load(root)
        current.update(patch or {})
        saved = _sc.save(root, current)
    _log("art", f"sprite contract set: {saved['preset']} "
                f"({len(saved['directions'])} directions, "
                f"{saved['cell'][0]}x{saved['cell'][1]})")
    return {"ok": True, **saved}


#: Game-side action names -> RD's advanced-animation styles. Anything not
#: here becomes custom_action with the game name as the motion prompt, which
#: costs more ($0.25 vs $0.14) and is exactly what custom_action is for.
_RD_ACTIONS = {"walk": "walking", "run": "walking", "idle": "idle",
               "jump": "jump", "crouch": "crouch", "attack": "attack",
               "ability": "custom_action", "hurt": "custom_action",
               "ko": "custom_action", "death": "custom_action"}
_RD_PROMPTS = {"walk": "confident, steady steps",
               "run": "fast, urgent running strides",
               "hurt": "flinching backward from a hit",
               "ability": "casting an ability with a flourish",
               "ko": "collapsing to the ground, defeated",
               "death": "collapsing to the ground, defeated"}


@_tool
def animation_generate(character: str, action: str,
                       source_sheet: str = "", prompt: str = "",
                       frames: int = 0, max_retries: int = 1,
                       direction: str = "") -> dict:
    """CONTRACT-DRIVEN character animation via Retro Diffusion.

    Reads the sprite contract for character+action (directions, cell size,
    frame count, layout). For each DRAWN direction it takes a start frame from
    the character's sheet, sends it to RD's animation model (~$0.14/direction),
    runs the full battery, and stitches the sheet + SpriteFrames .tres with
    animations named {action}_{direction}; mirrored directions are reported
    for flip_h. source_sheet defaults to the idle sheet, then the action's
    own. Large detailed characters get redrawn at ~70% and this says so.
    Full notes: docs/tools.md#animation_generate
    """
    from PIL import Image as _Img

    from bgate_adapters import retrodiffusion as _rd
    from bgate_core.art import spritecontract as _sc

    root = _Path(_root())
    refused = _provider_gate(str(root), "animate", "an animation cycle")
    if refused:
        return refused
    contract = _sc.contract_for(str(root), character, action)
    act = str(action).strip().lower()
    drawn = contract["drawn"]
    # ONE DIRECTION PER CALL, WHEN THE CALLER ASKS FOR IT. Retro Diffusion's
    # turnaround is about ten minutes per drawn direction, so buying all
    # three inside this single blocking call means ~30 minutes of silence -
    # and the MCP client kills a tool that reports no progress at its idle
    # ceiling (1800s by default). One cycle therefore sits right on the
    # boundary: it looks stuck the whole time and dies at the edge, having
    # already been billed. MEASURED on night-shift across several runs.
    # Passing `direction` buys exactly that one, which finishes in about a
    # third of the time and comfortably inside the ceiling; the per-direction
    # resume check above then accumulates the cycle across successive calls,
    # so three cheap calls produce the same sheet as one that cannot finish.
    if direction:
        want = str(direction).strip().lower()
        if want not in drawn:
            return {"ok": False,
                    "error": f"direction {want!r} is not drawn for "
                             f"{character}/{action}; drawn = {drawn}",
                    "drawn": drawn}
        drawn = [want]
    cw, ch = contract["cell"]
    spec = contract.get("action") or {}
    n_frames = int(frames or spec.get("frames") or 8)
    # RD only generates 4/6/8/10/12/16; the CONTRACT owns the sheet's
    # frame count. Generate the nearest count AT OR ABOVE and keep the
    # first n_frames — a 2-frame hurt is the first two cells of a 4-frame
    # flinch, not a format the game has to bend for.
    eligible = [f for f in _rd.FRAME_COUNTS if f >= n_frames]
    rd_frames = min(eligible) if eligible else max(_rd.FRAME_COUNTS)
    keep = min(n_frames, rd_frames)
    fps = float(spec.get("fps") or 8.0)
    rd_action = _RD_ACTIONS.get(act, "custom_action")
    motion = prompt or _RD_PROMPTS.get(act, f"{act} animation, smooth motion")

    probe = _rd.available(str(root))
    if not probe.get("available"):
        return {"ok": False, "error": probe.get("reason")}

    starts = _anim_start_frames(root, character, act, contract, source_sheet)
    if not starts.get("ok"):
        return starts

    out_dir = root / ".bgate_out" / "sprites"
    out_dir.mkdir(parents=True, exist_ok=True)
    pinned = _artdirection.palette_pinned(str(root))
    per_dir: dict[str, dict] = {}
    frame_files: dict[str, str] = {}
    ordered: list[str] = []
    spent = 0.0
    loop = spec.get("loop")
    if loop is None:
        loop = act not in _sprites.NO_LOOP
    # WHICH generated frames survive a trim is not "the first keep". A
    # one-shot's payoff is its LAST frame — trimming a 4-frame collapse to
    # its first 3 shipped a ko that never reached the floor. One-shots
    # sample evenly INCLUDING the endpoint; loops stride the cycle so the
    # wrap-around stays a genuine adjacent pair.
    if keep >= rd_frames:
        picks = list(range(rd_frames))
    elif loop:
        picks = [(i * rd_frames) // keep for i in range(keep)]
    elif keep == 1:
        picks = [rd_frames - 1]
    else:
        picks = [round(i * (rd_frames - 1) / (keep - 1))
                 for i in range(keep)]
    for direction in drawn:
        # RESUME BEFORE YOU BUY. This tool blocks one MCP call across every
        # drawn direction, and the client aborts a silent tool at its idle
        # ceiling — so a long run gets killed mid-loop routinely. The cells
        # for directions that already finished are on disk; without this
        # check the next run re-extracts the start frames and RE-BUYS them.
        # MEASURED on night-shift: the identical motion prompt was charged
        # twice for the player attack, twice for the manager walk and twice
        # for the paper-jam attack across three aborted runs, and the
        # provider's own job list shows each pair succeeding. Money spent,
        # nothing new on disk. A direction whose frames are all present and
        # newer than the start frame it came from is DONE; skip it.
        done_paths = [out_dir / f"{character}_{act}_{direction}_{i}.png"
                      for i in range(keep)]
        if all(p.is_file() for p in done_paths):
            seed = _Path(starts["frames"][direction])
            fresh = (not seed.is_file()
                     or min(p.stat().st_mtime for p in done_paths)
                     >= seed.stat().st_mtime)
            if fresh:
                for i, p in enumerate(done_paths):
                    label = f"{act}_{direction}/{i}"
                    frame_files[label] = str(p)
                    ordered.append(label)
                per_dir[direction] = {"findings": [], "attempts": 0,
                                      "resumed": True}
                continue
        best = None
        attempts = max(1, 1 + int(max_retries))
        for attempt in range(attempts):
            got = _rd.animate(starts["frames"][direction], rd_action,
                              frames=rd_frames, size=(cw, ch), prompt=motion,
                              out_path=out_dir / (f"{character}_{act}_"
                                                  f"{direction}_rd_sheet.png"),
                              root=str(root))
            spent += float(got.get("usd") or 0.0)
            with _Img.open(got["path"]) as _sheet_src:
                sheet_img = _rd.key_background(_sheet_src.copy())
            cols = max(1, sheet_img.width // cw)
            trial_files: dict[str, str] = {}
            trial_order: list[str] = []
            for i, src_index in enumerate(picks):
                r, c = divmod(src_index, cols)
                cell = sheet_img.crop((c * cw, r * ch,
                                       (c + 1) * cw, (r + 1) * ch))
                path = out_dir / (f"{character}_{act}_{direction}_{i}"
                                  + (f"_try{attempt}" if attempt else "")
                                  + ".png")
                cell.save(path)
                if pinned:
                    _spritekit.lock_palette(str(path), pinned)
                label = f"{act}_{direction}/{i}"
                trial_files[label] = str(path)
                trial_order.append(label)
            report = _spritekit.facing_report(
                trial_order, trial_files, expected=direction,
                reference=starts["frames"][direction])
            report["findings"].extend(_spritekit.flicker_report(
                trial_order, trial_files)["findings"])
            trial = {"files": trial_files, "order": trial_order,
                     "findings": report["findings"],
                     "balance": got.get("balance"), "attempt": attempt}
            if best is None or len(trial["findings"]) < len(best["findings"]):
                best = trial
            if not trial["findings"]:
                break
        # promote the winning attempt to the canonical filenames
        for i, label in enumerate(best["order"]):
            src = _Path(best["files"][label])
            dest = out_dir / f"{character}_{act}_{direction}_{i}.png"
            if src != dest:
                src.replace(dest)
            frame_files[label] = str(dest)
            ordered.append(label)
        per_dir[direction] = {
            "findings": best["findings"],
            "attempts": best["attempt"] + 1,
            "balance": best["balance"],
        }

    # Contract-shaped sheet: one row per drawn direction, in drawn order.
    plan = _spritekit.layout(len(ordered), cw, ch, columns=keep)
    sheet_path = out_dir / f"{character}_{act}_sheet.png"
    _sprites._stitch([frame_files[p] for p in ordered], sheet_path,  # noqa: SLF001
                     plan=plan)
    anims = [(f"{act}_{d}", keep) for d in drawn]
    timing = {name: {"fps": fps, "loop": bool(loop)} for name, _ in anims}
    tres_path = out_dir / f"{character}_{act}_frames.tres"
    tres_path.write_text(
        _sprites._sprite_frames_tres(  # noqa: SLF001 - shared emitter
            sheet_path.name, anims, (cw, ch), fps, "assets/characters",
            timing=timing, plan=plan),
        encoding="utf-8")
    previews = _gif_previews(frame_files, str(sheet_path),
                             f"{character}_{act}", timing, fps)
    motion_report = _spritekit.sheet_report(
        ordered, frame_files,
        no_loop=() if loop else tuple(name for name, _ in anims))
    # THE CROSS-DIRECTION CHECK — the consistency the per-strip battery
    # cannot see: every direction of this action must contain the same
    # character at the same scale on the same palette.
    drift = _spritekit.set_drift({
        f"{act}_{d}": [frame_files[p] for p in ordered
                       if p.startswith(f"{act}_{d}/")]
        for d in drawn})
    # The .aseprite master, same as image_sprites builds — a contract
    # sheet is exactly the thing somebody hand-fixes one frame of.
    ase = _ase_master_for(str(sheet_path), (cw, ch),
                          {name: rd_frames for name, _ in anims},
                          timing, fps)
    result = {"ok": True, "character": character, "action": act,
              "sheet": str(sheet_path), "tres": str(tres_path),
              "animations": {name: rd_frames for name, _ in anims},
              "mirror": contract["mirror"],
              "unplayable": contract.get("unplayable", []),
              "cell": [cw, ch], "frames_per_direction": rd_frames,
              "directions": per_dir, "motion": motion_report,
              "set": drift,
              **({"aseprite": ase} if ase is not None else {}),
              "animation_previews": previews,
              "start_frames": starts["frames"],
              "spend": {"usd": round(spent, 4), "calls": len(drawn),
                        "provider": "retrodiffusion"}}
    archived = _archive_preview(str(sheet_path), f"anim-{character}-{act}")
    artifact = _register_artifact(
        f"{character}_{act}", str(sheet_path),
        producer="animation_generate",
        refs=list(starts["frames"].values()),
        metadata={"contract": {k: contract[k] for k in
                               ("preset", "view", "drawn", "cell", "layout")},
                  "preview": archived or "",
                  "animation_previews": previews,
                  "motion": motion_report,
                  "directions": {d: r["findings"] for d, r in per_dir.items()},
                  "usd": round(spent, 4)})
    if artifact:
        result["artifact"] = artifact
    _log("art", f"animated {character} {act}: {len(drawn)} direction(s), "
                f"${spent:.2f}", ref=str(sheet_path))
    return result


def _anim_start_frames(root: _Path, character: str, action: str,
                       contract: dict, source_sheet: str) -> dict:
    """One start frame per drawn direction, sliced from an existing sheet.

    The sheet's rows are directions (the contract's row order); the first
    column of each row seeds that direction. Search order: an explicit
    source_sheet, the character's idle sheet, the action's own sheet - idle
    first because a neutral stance is the best seed for any motion.
    """
    from PIL import Image as _Img

    slug = str(character).strip()
    candidates = []
    if source_sheet:
        candidates.append(root / _assets.normalize_path(root, source_sheet))
    for name in ("idle", action):
        candidates.extend(root.glob(f"**/{slug}/{slug}_{name}.png"))
        candidates.extend(root.glob(f"**/{slug}_{name}.png"))
    sheet_path = next((c for c in candidates if c.is_file()), None)
    if sheet_path is None:
        return {"ok": False, "error":
                f"no source sheet found for {slug!r} - pass source_sheet, or "
                "generate a reference first (image_sprites / image_generate)"}
    cw, ch = contract["cell"]
    rows = contract["rows"]
    out_dir = root / ".bgate_out" / "sprites" / "starts"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: dict[str, str] = {}
    with _Img.open(sheet_path) as img:
        sheet = img.convert("RGBA")
        if sheet.height < ch * len(rows) or sheet.width < cw:
            return {"ok": False, "error":
                    f"{sheet_path.name} is {sheet.width}x{sheet.height}; the "
                    f"contract expects rows of {cw}x{ch} cells x {len(rows)} "
                    "direction row(s) - fix the contract or the sheet"}
        for row_index, direction in enumerate(rows):
            if direction not in contract["drawn"]:
                continue
            cell = sheet.crop((0, row_index * ch, cw, (row_index + 1) * ch))
            dest = out_dir / f"{slug}_{action}_{direction}_start.png"
            cell.save(dest)
            frames[direction] = str(dest)
    missing = [d for d in contract["drawn"] if d not in frames]
    if missing:
        return {"ok": False, "error":
                f"source sheet rows cover {sorted(frames)} but the contract "
                f"draws {contract['drawn']} - missing {missing}. Its rows and "
                "the contract's disagree; set characters overrides to match."}
    return {"ok": True, "frames": frames, "source": str(sheet_path)}


def _ase_anim_specs(animations: dict, timing: Optional[dict],
                    fps: float) -> list[dict]:
    """The per-frame durations aseprite.master needs, from the sheet's own plan.

    ``animations`` is {anim: frame_count} in sheet order; ``timing`` is the
    animspec dict image_sprites already carries. Holds are relative, so a hold
    of 2.0 at 8fps is 250ms — the master plays exactly what the .tres plays.
    """
    from bgate_adapters.sprites import NO_LOOP
    specs = []
    for anim, count in animations.items():
        spec = (timing or {}).get(anim) or {}
        anim_fps = float(spec.get("fps") or fps) or 8.0
        holds = list(spec.get("holds") or [])
        holds += [1.0] * (int(count) - len(holds))
        loop = spec.get("loop")
        if loop is None:
            loop = anim not in NO_LOOP
        specs.append({"name": str(anim).replace(":", "_").replace("|", "_"),
                      "durations_ms": [max(1, round(float(h) * 1000.0 / anim_fps))
                                       for h in holds[:int(count)]],
                      "loop": bool(loop)})
    return specs


def _gif_previews(frame_map: dict, sheet: str, name: str,
                  timing: Optional[dict], fps: float) -> dict[str, str]:
    """One playable GIF per animation, beside the sheet. {} on any failure.

    ``frame_map`` is {pose: path} in sheet order, pose names "anim/idx" or
    bare — the same grouping rule _group_frames uses. The first animation's
    GIF is also archived so the dashboard gallery shows motion, not a grid.
    """
    try:
        from bgate_adapters.sprites import NO_LOOP
        from bgate_core.art import animgif as _animgif

        by_anim: dict[str, list[str]] = {}
        for pose, path in frame_map.items():
            by_anim.setdefault(str(pose).split("/", 1)[0], []).append(path)
        written = _animgif.write_gifs(by_anim, str(_Path(sheet).parent), name,
                                      timing=timing, fps=fps, no_loop=NO_LOOP,
                                      scale=2 if _gif_cells_small(frame_map) else 1)
        if written:
            first = next(iter(written.values()))
            archived = _archive_preview(first, f"anim-{name}")
            if archived:
                written["_archived"] = archived
        return written
    except Exception:                                           # noqa: BLE001
        return {}


def _gif_cells_small(frame_map: dict) -> bool:
    """Upscale the preview 2x when the frames are small enough to squint at."""
    try:
        from PIL import Image
        with Image.open(next(iter(frame_map.values()))) as im:
            return max(im.size) <= 128
    except Exception:                                           # noqa: BLE001
        return False


def _ase_master_for(sheet: str, cell: tuple[int, int],
                    animations: dict, timing: Optional[dict],
                    fps: float) -> Optional[dict]:
    """Best-effort .aseprite master beside the sheet. None when Aseprite is absent.

    Never raises and never fails the sheet: the master is a convenience for
    hand edits, and a machine without Aseprite still ships the same PNG+tres.
    """
    from bgate_adapters import aseprite as _ase
    if not _ase.available().get("available"):
        return None
    out = str(_Path(sheet).with_suffix(".aseprite"))
    try:
        got = _ase.master(sheet, out, cell=cell,
                          anims=_ase_anim_specs(animations, timing, fps))
    except Exception as exc:                                    # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    got["master"] = out
    return got


@_tool
def aseprite_status() -> dict:
    """Is Aseprite usable on this machine? Path and version, or what to do.

    Aseprite is OPTIONAL: sheets, conform and .tres still work without it.
    What needs it: .aseprite masters (aseprite_master, auto-built by
    image_sprites), re-export of hand-edited masters (aseprite_export), and
    palette derivation from refs (palette_pin without explicit colors)."""
    from bgate_adapters import aseprite as _ase
    try:
        info = _ase.available()
        if info.get("available"):
            info.update(_ase.version())
        return {"ok": True, **info}
    except Exception as exc:
        return _fail(exc)


@_tool
def palette_pin(colors: Optional[list[str]] = None,
                max_colors: int = 32) -> dict:
    """PIN THE PROJECT PALETTE in the art bible - the fix for uneven pixel art.

    Writes a LOCKED bible constraint; from then on every image_sprites sheet,
    item and vfx set is conformed to exactly these colours and drift becomes
    unrepresentable. colors: explicit hex list. Omitted: derived from the
    pinned style refs, quantised by Aseprite to at most max_colors (without
    Aseprite, the refs' dominant colours). Re-running replaces the palette.
    16-40 colours is the useful range.
    Full notes: docs/tools.md#palette_pin
    """
    root = _root()
    hexes: list[str] = []
    source = "explicit"
    if colors:
        for entry in colors:
            text = str(entry).strip().lstrip("#").lower()
            if len(text) != 6 or any(c not in "0123456789abcdef" for c in text):
                return {"ok": False, "error": f"not a hex colour: {entry!r}"}
            if text not in hexes:
                hexes.append(text)
    else:
        anchors = _artdirection.anchors_for(root, limit=4)
        if not anchors:
            return {"ok": False, "error":
                    "no colors given and no style refs pinned to derive "
                    "from - pass colors=[...] or ref_pin a style image first"}
        from bgate_adapters import aseprite as _ase
        if _ase.available().get("available"):
            source = "derived (aseprite quantise over style refs)"
            seen: list[str] = []
            for anchor in anchors:
                with _tempfile.TemporaryDirectory() as tmp:
                    got = _ase.conform(anchor, str(_Path(tmp) / "q.png"),
                                       max_colors=max(2, int(max_colors)))
                for hexcode in got.get("palette") or []:
                    if hexcode not in seen:
                        seen.append(hexcode)
            hexes = seen
        else:
            source = "derived (dominant ref colours - no aseprite)"
            for anchor in anchors:
                for r, g, b in _chroma.palette_of(anchor, colors=max_colors):
                    hexcode = f"{r:02x}{g:02x}{b:02x}"
                    if hexcode not in hexes:
                        hexes.append(hexcode)
        if len(hexes) > int(max_colors):
            # Multiple refs can each contribute a near-duplicate ramp; cap
            # by re-quantising the union down to the asked-for size.
            from PIL import Image as _Img
            strip = _Img.new("RGB", (len(hexes), 1))
            strip.putdata([tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
                           for h in hexes])
            quant = strip.quantize(colors=int(max_colors),
                                   method=_Img.Quantize.MEDIANCUT)
            table = quant.getpalette() or []
            used = sorted(set(quant.getdata()))
            hexes = [f"{table[i * 3]:02x}{table[i * 3 + 1]:02x}"
                     f"{table[i * 3 + 2]:02x}" for i in used]
    if not hexes:
        return {"ok": False, "error": "no colours to pin"}

    body = ("Every shipped 2D asset uses exactly these colours - sheets, "
            "items and VFX are conformed to them automatically:\n"
            + " ".join(f"#{h}" for h in hexes))
    existing = next(
        (s for s in _bible.list_sections(root, "constraint")
         if _artdirection.PALETTE_TITLE in str(s.get("title") or "")), None)
    if existing:
        _bible.update(root, int(existing["id"]), body=body)
        section_id = int(existing["id"])
    else:
        section_id = _bible.add(root, "constraint",
                                _artdirection.PALETTE_TITLE, body)["id"]
    _log("art", f"pinned {len(hexes)}-colour project palette ({source})")
    return {"ok": True, "section_id": section_id, "source": source,
            "colors": [f"#{h}" for h in hexes], "count": len(hexes),
            "note": "every future sheet/item/vfx conforms to this palette; "
                    "regenerate or re-conform existing art to migrate it"}


@_tool
def aseprite_master(sheet: str, cell: list[int],
                    anims: Optional[list[dict]] = None,
                    fps: float = 8.0) -> dict:
    """Sheet PNG -> tagged .aseprite MASTER, the file a human edits.

    Each cell a real frame, each animation a named tag with its timing,
    written beside the sheet. image_sprites builds one automatically; this is
    for sheets from elsewhere. cell: [width, height] of one frame. anims:
    [{name, frames, fps?, loop?}] in sheet order; omitted, the whole sheet is
    one looping "default". aseprite_export brings it back as sheet + EXACT
    SpriteFrames .tres.
    Full notes: docs/tools.md#aseprite_master
    """
    root = _Path(_root())
    rel = _assets.normalize_path(root, sheet)
    src = root / rel
    if not src.exists():
        return {"ok": False, "error": f"no sheet at {rel}"}
    from bgate_adapters import aseprite as _ase
    from PIL import Image as _Img
    cw, ch = int(cell[0]), int(cell[1])
    with _Img.open(src) as im:
        width, height = im.size
    if width % cw or height % ch:
        return {"ok": False, "error":
                f"cell {cw}x{ch} does not tile the {width}x{height} sheet"}
    total = (width // cw) * (height // ch)
    if anims:
        animations = {str(a["name"]): int(a.get("frames") or 1) for a in anims}
        timing = {str(a["name"]): {"fps": float(a["fps"])} for a in anims
                  if a.get("fps")}
        for a in anims:
            if a.get("loop") is not None:
                timing.setdefault(str(a["name"]), {})["loop"] = bool(a["loop"])
        claimed = sum(animations.values())
        if claimed > total:
            return {"ok": False, "error":
                    f"anims claim {claimed} frames but the sheet has {total}"}
    else:
        animations, timing = {"default": total}, {}
    got = _ase.master(str(src), str(src.with_suffix(".aseprite")),
                      cell=(cw, ch),
                      anims=_ase_anim_specs(animations, timing, float(fps)))
    got["master"] = _assets.normalize_path(root, src.with_suffix(".aseprite"))
    return got


@_tool
def aseprite_export(master: str, res_dir: str = "assets/sprites",
                    out_dir: str = "") -> dict:
    """Hand-edited .aseprite -> sheet PNG + EXACT SpriteFrames .tres.

    The export JSON states every frame's rect, duration and tag, so
    per-animation speeds and per-frame holds survive as authored. Output lands
    beside the master (or in out_dir) as <stem>_sheet.png + <stem>_frames.tres;
    import the pair at res://<res_dir>/. SLICES named after rig slots become
    exact per-frame anchors (<stem>_offsets.json); every tag gets a GIF
    preview; motion/palette findings are ADVISORY, never a refusal.
    Full notes: docs/tools.md#aseprite_export
    """
    _contained_path(out_dir, "out_dir")
    root = _Path(_root())
    rel = _assets.normalize_path(root, master)
    src = root / rel
    if not src.exists():
        return {"ok": False, "error": f"no master at {rel}"}
    from bgate_adapters import aseprite as _ase
    from bgate_core.art import asejson as _asejson
    stem = src.stem.removesuffix("_sheet")
    dest = (root / out_dir) if out_dir else src.parent
    dest.mkdir(parents=True, exist_ok=True)
    sheet_path = dest / f"{stem}_sheet.png"
    data_path = dest / f"{stem}_frames.json"
    data = _ase.export(str(src), str(sheet_path), str(data_path))
    tres_path = dest / f"{stem}_frames.tres"
    tres_path.write_text(
        _asejson.spriteframes_text(data, sheet_path.name, res_dir),
        encoding="utf-8")
    frames = data.get("frames") or []
    tags = [t.get("name") for t in
            (data.get("meta") or {}).get("frameTags") or []]
    try:
        _assets.track(root, sheet_path)
    except Exception:
        pass
    result = {"ok": True,
              "sheet": _assets.normalize_path(root, sheet_path),
              "tres": _assets.normalize_path(root, tres_path),
              "data": _assets.normalize_path(root, data_path),
              "frames": len(frames), "animations": tags or ["default"],
              "res_dir": res_dir}
    result.update(_ase_export_review(root, data, sheet_path, stem))
    result.update(_ase_export_anchors(data, sheet_path, stem))
    return result


@_tool
def aseprite_antialias(image: str, out: str = "",
                       use_pinned_palette: bool = True) -> dict:
    """Soften stair-step corners in a sprite or tile PNG. Free and local.

    A pixel with two or more orthogonal neighbours of one same other colour
    becomes the midpoint of the pair; straight edges and transparent pixels
    are untouched, so silhouettes do not grow. With use_pinned_palette
    (default) every blended pixel snaps back into the pinned palette. Writes
    <stem>_aa.png beside the source unless `out` names a path. Run AFTER
    generation and conform, before delivery.
    Full notes: docs/tools.md#aseprite_antialias
    """
    root = _Path(_root())
    rel = _assets.normalize_path(root, image)
    src = root / rel
    if not src.exists():
        return {"ok": False, "error": f"no image at {rel}"}
    _contained_path(out, "out")
    dest = (root / out) if out else src.with_name(src.stem + "_aa.png")
    from bgate_adapters import aseprite as _ase
    palette = _artdirection.palette_pinned(root) if use_pinned_palette else ()
    got = _ase.antialias(str(src), str(dest), palette=palette or ())
    if got.get("ok"):
        got["out"] = _assets.normalize_path(root, dest)
        got["palette_snapped"] = bool(palette)
        _note_tool_write(_root(), str(dest))
    return got


@_tool
def aseprite_normal_map(image: str, out: str = "", intensity: float = 1.0,
                        invert_y: bool = False) -> dict:
    """Normal map from a sprite or tile PNG, brightness read as height.

    Central differences over the value channel, alpha preserved, green axis
    OpenGL (+Y up) - exactly what a Godot CanvasTexture's normal slot wants
    for 2D dynamic lighting. Art shaded light-on-top reads as relief; flat
    art gives flat normals, so raise `intensity` for subtle shading. Writes
    beside the source as <stem>_n.png unless `out` names a path. Wire it in
    Godot by setting the sprite/tile texture to a CanvasTexture whose
    diffuse is the art and whose normal_texture is this output."""
    root = _Path(_root())
    rel = _assets.normalize_path(root, image)
    src = root / rel
    if not src.exists():
        return {"ok": False, "error": f"no image at {rel}"}
    _contained_path(out, "out")
    dest = (root / out) if out else src.with_name(src.stem + "_n.png")
    from bgate_adapters import aseprite as _ase
    got = _ase.normal_map(str(src), str(dest), intensity=float(intensity),
                          invert_y=bool(invert_y))
    if got.get("ok"):
        got["out"] = _assets.normalize_path(root, dest)
        _note_tool_write(_root(), str(dest))
    return got


def _ase_export_review(root: _Path, data: dict, sheet_path: _Path,
                       stem: str) -> dict:
    """Re-grade a re-exported master, advisory, plus playable GIF previews.

    Every other art path passes motion_report and the palette check; a hand
    edit was the one door with no mirror on it. Advisory on purpose - a
    human changed this file deliberately, so a finding is for their eyes,
    not a refusal. Never raises: the pair on disk is the deliverable and it
    is already written.
    """
    import tempfile as _tf

    out: dict = {}
    try:
        from PIL import Image as _Img

        from bgate_core.art import animgif as _animgif

        frames = data.get("frames") or []
        tags = list((data.get("meta") or {}).get("frameTags") or [])
        if not tags:
            tags = [{"name": "default", "from": 0, "to": len(frames) - 1}]
        with _tf.TemporaryDirectory(prefix="bgate-ase-review-") as tmp, \
                _Img.open(sheet_path) as sheet_img:
            sheet = sheet_img.convert("RGBA")
            ordered: list[str] = []
            frame_files: dict[str, str] = {}
            by_anim: dict[str, list[str]] = {}
            no_loop: set[str] = set()
            for tag in tags:
                name = str(tag.get("name") or "default")
                lo, hi = int(tag.get("from", 0)), int(tag.get("to", -1))
                if not (0 <= lo <= hi < len(frames)):
                    continue
                if str(tag.get("repeat") or "") == "1":
                    no_loop.add(name)
                for i, frame in enumerate(range(lo, hi + 1)):
                    rect = frames[frame].get("frame") or {}
                    cell = sheet.crop((rect["x"], rect["y"],
                                       rect["x"] + rect["w"],
                                       rect["y"] + rect["h"]))
                    path = str(_Path(tmp) / f"{name}_{i}.png")
                    cell.save(path)
                    label = f"{name}/{i}"
                    ordered.append(label)
                    frame_files[label] = path
                    by_anim.setdefault(name, []).append(path)
            if ordered:
                out["motion"] = _spritekit.sheet_report(
                    ordered, frame_files, no_loop=no_loop)
                previews: dict[str, str] = {}
                for anim, paths in by_anim.items():
                    lo = next(int(t.get("from", 0)) for t in tags
                              if str(t.get("name") or "default") == anim)
                    durs = [max(20, int(frames[lo + i].get("duration") or 100))
                            for i in range(len(paths))]
                    # Scale by CELL size, not sheet size — a 12-frame strip is
                    # 768px wide while its cells are still 64px sprites nobody
                    # can review unscaled.
                    first_rect = (frames[0].get("frame") or {}) if frames else {}
                    small = max(int(first_rect.get("w") or 0),
                                int(first_rect.get("h") or 0)) <= 128
                    dest = sheet_path.parent / f"{stem}_{anim}.gif"
                    got = _animgif.write_gif(paths, str(dest), durations=durs,
                                             loop=anim not in no_loop,
                                             scale=2 if small else 1)
                    if got.get("ok"):
                        previews[anim] = str(dest)
                if previews:
                    first = next(iter(previews.values()))
                    archived = _archive_preview(first, f"anim-{stem}")
                    if archived:
                        previews["_archived"] = archived
                    out["animation_previews"] = previews
        pinned = _artdirection.palette_pinned(str(root))
        if pinned:
            off = _artdirection.off_palette_fraction(sheet_path, pinned)
            out["palette"] = {
                "ok": off == 0.0, "source": "bible", "off_palette": round(off, 4),
                "note": ("on the pinned palette" if off == 0.0 else
                         f"{off:.1%} of the ink is off the pinned palette - a "
                         "hand edit introduced outside colours; re-conform or "
                         "extend the palette with palette_pin")}
        for key in ("motion", "palette"):
            if key in out:
                try:
                    _artifacts.record_check(str(root), str(sheet_path), key,
                                            out[key])
                except Exception:
                    pass
    except Exception as exc:                                    # noqa: BLE001
        out["review_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _ase_export_anchors(data: dict, sheet_path: _Path, stem: str) -> dict:
    """Slices -> rig sidecar labels + the runtime offsets file. {} when none.

    Slice labels replace only earlier SLICE labels; a label a person placed
    in spriteedit (`authored`) wins over one from the same person's Aseprite
    session only because the sidecar was there first and stomping it would
    lose work silently - the report says when that happened.
    """
    out: dict = {}
    try:
        from PIL import Image as _Img

        from bgate_core.art import asejson as _asejson
        from bgate_core.art import rigmap as _rigmap

        labels, skipped = _asejson.slice_labels(data)
        if not labels and not skipped:
            return {}
        rig = _rigmap.load(sheet_path)
        frames = data.get("frames") or []
        first = (frames[0].get("frame") or {}) if frames else {}
        with _Img.open(sheet_path) as im:
            sheet_size = im.size
        cw, ch = int(first.get("w") or 0), int(first.get("h") or 0)
        if cw and ch and sheet_size[0] % cw == 0 and sheet_size[1] % ch == 0:
            rig["grid"] = {"cell_w": cw, "cell_h": ch,
                           "cols": sheet_size[0] // cw,
                           "rows": sheet_size[1] // ch}
        # Tags become the sidecar's animations so offsets_json knows play order.
        anims = []
        for tag in (data.get("meta") or {}).get("frameTags") or []:
            lo, hi = int(tag.get("from", 0)), int(tag.get("to", -1))
            if 0 <= lo <= hi < len(frames):
                anims.append({"name": str(tag.get("name") or "default"),
                              "frames": list(range(lo, hi + 1)),
                              "loop": str(tag.get("repeat") or "") != "1"})
        if anims:
            rig["animations"] = anims
        kept = [lab for lab in rig.get("labels") or []
                if lab.get("source") != "slice"]
        taken = {(lab["slot"], lab["frame"]) for lab in kept}
        shadowed = []
        merged = list(kept)
        for lab in labels:
            if (lab["slot"], lab["frame"]) in taken:
                shadowed.append(f"{lab['slot']}@{lab['frame']}")
                continue
            merged.append(lab)
        rig["labels"] = merged
        _rigmap.save(sheet_path, rig, sheet_size=sheet_size)
        saved = _rigmap.load(sheet_path)
        offsets_path = sheet_path.parent / f"{stem}_offsets.json"
        slots = _rigmap.slots_used(saved)
        primary = "main_hand" if "main_hand" in slots else (slots[0] if slots else "")
        if primary:
            offsets_path.write_text(
                _json.dumps(_rigmap.offsets_json(saved, primary), indent=2),
                encoding="utf-8")
        out["anchors"] = {
            "slots": slots,
            "labels": len([lab for lab in saved["labels"]
                           if lab.get("source") == "slice"]),
            "rig": str(_rigmap.sidecar_path(sheet_path)),
            "offsets": str(offsets_path) if primary else "",
            **({"skipped_slices": skipped} if skipped else {}),
            **({"kept_authored": shadowed} if shadowed else {})}
    except Exception as exc:                                    # noqa: BLE001
        out["anchors_error"] = f"{type(exc).__name__}: {exc}"
    return out


def vfx_animate(key_frame: Annotated[str, Field(description='Path to the ONE approved key frame: the effect at its PEAK, alone on the keyed backdrop.')], name: Annotated[str, Field(description='Effect name; emits <name>_sheet.png and <name>_frames.tres.')], motion: Annotated[str, Field(description='Motion recipe to derive frames with (burst, and the others listed in the docstring). Default burst.')] = "burst",
                frames: Annotated[int, Field(description='Total output frames. Default 4.')] = 4, peak: Annotated[int, Field(description='Which output frame the key frame IS; frames before grow into it, after decay out. Default 1.')] = 1, cell: Annotated[Optional[list[int]], Field(description='[width, height] of one cell; omitted, sized from the key frame.')] = None,
                fps: Annotated[float, Field(description='Playback speed written into the SpriteFrames. Default 14.')] = 14.0, res_dir: Annotated[str, Field(description='res:// directory the sheet is meant to import under. Default assets/vfx.')] = "assets/vfx",
                out_dir: Annotated[str, Field(description="Where the files are written; empty uses the project's vfx output directory.")] = "", loop: Annotated[Optional[bool], Field(description='Force the animation to loop or not; omitted, the motion decides.')] = None,
                overrides: Annotated[Optional[dict], Field(description='Per-motion number tweaks: grow, expand, scatter, drift, fade, gravity, jitter, squash, chunk.')] = None) -> dict:
    """Turn ONE approved key frame into an effect ANIMATION, arithmetically.

    THE TOOL FOR PROJECTILE AND IMPACT VFX - do NOT buy an effect as a grid of
    frames from an image model. Generate ONE key frame at its PEAK on the
    keyed backdrop, then call this: frames before `peak` grow into it, frames
    after decay out of it. Emits <name>_sheet.png + <name>_frames.tres, every
    frame registered to the cell centre; read `notes` in the result.

    MOTIONS:
    {motions}
    `overrides` tunes one motion's numbers (grow/expand/scatter/drift/fade/
    gravity/jitter/squash/chunk). COSTS NOTHING AND CALLS NO MODEL.
    Full notes: docs/tools.md#vfx_animate
    """
    try:
        root = _Path(_root())
        # Derived, not bought: vfx_animate transforms one existing key frame
        # rather than generating N. Preflighted anyway, because a drained
        # account refuses this path exactly like any other.
        refused = _provider_gate(str(root), "image",
                                 f"animating {key_frame!r}")
        if refused:
            return refused
        rel = _assets.normalize_path(root, key_frame)
        src = root / rel
        if not src.exists():
            return {"ok": False, "error": f"no key frame at {rel}"}
        dest = (root / out_dir) if out_dir else src.parent
        res = _vfx.animate(
            str(src), str(dest), name, motion=motion, frames=int(frames),
            peak=int(peak), cell=tuple(cell) if cell else (64, 64),
            fps=float(fps), res_dir=res_dir, loop=loop, overrides=overrides,
            target_palette=_artdirection.palette_pinned(str(root)) or None)
        if not res.get("ok"):
            return res
        previews_gif = _gif_previews(
            {f"default/{i}": p for i, p in enumerate(res["frames"])},
            res["sheet"], name,
            {"default": {"loop": res["loop"]}}, float(fps))
        if previews_gif:
            res["animation_previews"] = previews_gif
        _register_artifact(name, res["sheet"], producer="vfx_animate",
                           refs=[str(src)],
                           metadata={"motion": motion, "frames": frames,
                                     "anchor": res["anchor"],
                                     "coverage": res["coverage"],
                                     "animation_previews": previews_gif})
        for key in ("sheet", "tres"):
            res[key] = _assets.normalize_path(root, res[key])
        res["frames"] = [_assets.normalize_path(root, p) for p in res["frames"]]
        return res
    except Exception as exc:
        return _fail(exc)


# The motion table is written ONCE, in bgate_core.art.vfx, and interpolated into the
# tool description here. This must happen BEFORE _tool is applied: functools.wraps
# copies __doc__ at decoration time and FastMCP reads it then, so a docstring
# built afterwards would never reach the model. (A `"""...""" % x` docstring is
# worse still - the % makes it an expression, so __doc__ is simply None and the
# whole description vanishes silently. It did, for one commit.)
vfx_animate.__doc__ = vfx_animate.__doc__.format(motions=_vfx.motion_help())
vfx_animate = _tool(vfx_animate)


# Frames the vision judge scores at/below this are flagged for regen.
#
# Raised 78 -> 90 by the director, on evidence: an 8-frame IT-rogue idle came
# back "no outliers, min 80" while the hoodie shifted teal -> dark -> teal, the
# yellow hair streak moved and changed shape, and the head size wandered
# between frames. A floor that passes that is not a gate, it is a formality.
# The cost is more re-rolls per run; the thing it buys is that a PASS means
# something when a human looks at the sheet.
_CONSISTENCY_FLOOR = 90
# Deterministic palette gates (opaque-pixel histograms, 4 bits/channel).
# Measured on the failed PM-Paladin batch: adjacent same-character frames
# intersect ~0.45; recolored frames vs their siblings crater to ~0.06; and
# ref-vs-frame runs low (~0.1-0.3) even for GOOD frames because the ref's
# rendering differs - so BATCH COHESION (each frame vs the batch median) is
# the primary gate, and vs-ref only trips on catastrophic recolors.
_PALETTE_COHESION_FLOOR = float(os.environ.get("BGATE_PALETTE_COHESION", "0.35"))
_PALETTE_REF_FLOOR = float(os.environ.get("BGATE_PALETTE_FLOOR", "0.10"))


def _palette_hist(path):
    from PIL import Image as _Img
    im = _Img.open(path).convert("RGBA")
    im.thumbnail((160, 160))
    h = [0.0] * 4096
    n = 0
    for r, g, b, a in im.getdata():
        if a > 96:
            h[(r >> 4) << 8 | (g >> 4) << 4 | (b >> 4)] += 1
            n += 1
    return [v / n for v in h] if n else h


def _hist_intersect(a, b) -> float:
    return sum(min(x, y) for x, y in zip(a, b))


def _vision_consistency(ref_path, frame_items, pass_floor=_CONSISTENCY_FLOOR):
    """Score generated frames against an approved reference for CHARACTER IDENTITY.

    Cheap pixel metrics (palette, silhouette) can't judge "same character" pose-
    invariantly, so this asks a vision model to score each frame 0-100 (identity
    only - pose/expression ignored). frame_items: list of (label, path). Returns
    {"ok": True, "frames": [{"label","score","reason","pass"}], "min", "flagged"}
    or {"ok": False, "error": ...} - callers must treat failure as non-blocking.
    """
    import base64 as _b64, io as _io, json as _json
    try:
        from PIL import Image as _Img
        from openai import OpenAI as _OpenAI

        def _url(p):
            im = _Img.open(p).convert("RGBA"); im.thumbnail((256, 256))
            bg = _Img.new("RGBA", im.size, (255, 255, 255, 255)); bg.alpha_composite(im)
            b = _io.BytesIO(); bg.convert("RGB").save(b, "PNG")
            return "data:image/png;base64," + _b64.b64encode(b.getvalue()).decode()

        labels = [lab for lab, _ in frame_items]
        # The threshold is INTERPOLATED, never written twice. It used to be the
        # literal "78" here while the constant was separate, so raising the
        # constant to 90 left the judge still calibrating to 78: nothing could
        # reach the new bar, every frame re-rolled to no purpose, and one
        # 8-frame run burned 24 image calls and $1.01 to ship a sheet where
        # 8/8 frames were flagged.
        content = [{"type": "text", "text":
            "The FIRST image is the APPROVED reference for a game character. The remaining "
            "images are generated frames of ONE animation of that character. Pose, action "
            "and expression WILL differ between frames - IGNORE those.\n"
            "Judge TWO things:\n"
            "(1) IDENTITY: score each frame 0-100 for being the SAME character as the "
            "reference (body proportions, art style, line weight, palette, defining "
            f"features). Score {pass_floor} or above ONLY when the frame could sit beside "
            f"the reference in the same sheet with no visible difference in build, palette "
            f"or line weight; below {pass_floor} means drift a player would notice.\n"
            "(2) FRAME-TO-FRAME CONSISTENCY: the frames must also look consistent WITH "
            "EACH OTHER - same build, proportions, weight, head size and style across the "
            "set. Mark outlier=true for any frame whose PROPORTIONS/BUILD/STYLE visibly "
            "differ from the majority of the other frames (e.g. suddenly buffer, rounder, "
            "bigger head, different line weight), even if it still resembles the reference.\n"
            "Respond ONLY as JSON {\"frames\":[{\"score\":0,\"outlier\":false,\"reason\":\"\"}...]} "
            "in the SAME order as the frames, one entry per frame."}]
        content.append({"type": "image_url", "image_url": {"url": _url(ref_path)}})
        for _, p in frame_items:
            content.append({"type": "image_url", "image_url": {"url": _url(p)}})

        cli = _OpenAI()
        r = cli.chat.completions.create(
            model=os.environ.get("BGATE_VISION_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"}, temperature=0)
        raw = _json.loads(r.choices[0].message.content).get("frames", [])
        # Deterministic palette gates - the vision judge kept passing
        # outfit/skin recolors. Primary: cohesion of each frame against the
        # BATCH MEDIAN histogram; secondary: catastrophic drift vs the ref.
        try:
            hists = [_palette_hist(p) for _, p in frame_items]
            med = [sorted(col)[len(col) // 2] for col in zip(*hists)]
            s = sum(med) or 1.0
            med = [v / s for v in med]
            ref_hist = _palette_hist(ref_path)
            cohesion = [round(_hist_intersect(h, med), 3) for h in hists]
            vs_ref = [round(_hist_intersect(h, ref_hist), 3) for h in hists]
        except Exception:
            cohesion = [None] * len(labels)
            vs_ref = [None] * len(labels)
        out = []
        for i, lab in enumerate(labels):
            e = raw[i] if i < len(raw) else {}
            sc = int(e.get("score", 0))
            outlier = bool(e.get("outlier", False))
            coh, vr = cohesion[i], vs_ref[i]
            pal_ok = ((coh is None or coh >= _PALETTE_COHESION_FLOOR)
                      and (vr is None or vr >= _PALETTE_REF_FLOOR))
            reason = str(e.get("reason", ""))[:160]
            if not pal_ok:
                reason = (f"PALETTE DRIFT (cohesion {coh} < "
                          f"{_PALETTE_COHESION_FLOOR} or vs-ref {vr} < "
                          f"{_PALETTE_REF_FLOOR}). " + reason)[:160]
            # A frame passes only if it matches the reference, isn't a
            # frame-to-frame outlier, AND holds the batch's palette.
            out.append({"label": lab, "score": sc, "outlier": outlier,
                        "palette_cohesion": coh, "palette_vs_ref": vr,
                        "reason": reason,
                        "pass": sc >= pass_floor and not outlier and pal_ok})
        flagged = [f["label"] for f in out if not f["pass"]]
        return {"ok": True, "frames": out, "floor": pass_floor,
                "min": min((f["score"] for f in out), default=None),
                "outliers": [f["label"] for f in out if f["outlier"]],
                "flagged": flagged}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _reference_sanity(path):
    """Structural gate for a freshly generated character reference - run BEFORE
    spending one edit per pose conditioned on it. gpt-image sometimes returns an
    'ok' result that is still unusable: a near-empty frame, or one whose
    background never keyed to transparent (a fully-filled rectangle). Identity
    can't be auto-judged with no ground truth, but 'is this a usable single
    transparent figure' can. Catching it here caps a broken run at ~1 spend
    instead of N poses that all inherit the flaw and all fail the pose gate.
    Returns (ok: bool, reason: str). Any checker error is treated as PASS - this
    must never block a good run; the per-pose consistency gate still runs after.
    """
    try:
        from PIL import Image as _Img
        im = _Img.open(path).convert("RGBA")
        im.thumbnail((128, 128))
        data = list(im.getdata())
        n = len(data) or 1
        opaque = sum(1 for _, _, _, a in data if a > 40)
        cov = opaque / n
        if cov < 0.04:
            return False, (f"near-empty reference (opaque coverage {cov:.3f}) - "
                           "the generation produced almost nothing")
        if cov > 0.93:
            return False, (f"background did not key to transparent (opaque "
                           f"coverage {cov:.3f}) - the reference is a filled "
                           "frame, not a cut-out character")
        return True, f"coverage {cov:.3f}"
    except Exception as exc:
        return True, f"sanity check skipped: {type(exc).__name__}"


# Chroma keying MOVED to bgate_core.art.chroma - it is a contract now, not a local
# trick. Generating on a solid backdrop the character never uses and keying it
# out is the ONLY way either provider yields alpha (gpt-image's transparent mode
# returns gradients and punches eye-whites to holes; Krea has no alpha parameter
# at all), so the picking + prompt clause + keying + audit had to live somewhere
# both providers can reach. These names stay as thin aliases: the sprite tool
# below and tests/adapters/test_mcp_adjust.py both call them.
_CHROMA = _chroma.CHROMA
_pick_chroma = _chroma.pick
_chroma_key = _chroma.key


@_tool
def sprite_plan(archetypes: Optional[list[str]] = None, view: str = "",
                quality: str = "medium") -> dict:
    """The key poses and timing for standard animations. FREE - spends nothing.

    Call this BEFORE image_sprites. With no arguments it returns the
    catalogue: every archetype, frames generated, steps played, and why. With
    `archetypes=["idle","walk4","attack"]` it returns the exact pose list,
    timing and cost that run would use. A walk is CONTACT/DOWN/PASSING/UP,
    an attack HOLDS its impact frame - this is what makes generated motion
    read as alive. `view` ("side view, facing right") is prepended to every
    description. Feed `poses`/`archetypes` straight to image_sprites.
    Full notes: docs/tools.md#sprite_plan
    """
    if not archetypes:
        return {"ok": True, "catalog": _animspec.catalog(),
                "note": "pass archetypes=[...] for the pose list and price of "
                        "a specific run. `generated` is what you pay for; "
                        "`steps` is how many frames actually play - they "
                        "differ where ping-pong is doing its job."}
    built = _animspec.plans(list(archetypes), view=view)
    from bgate_adapters import imagegen as _ig

    per = _ig.price_per_image(quality)
    details = []
    for entry in archetypes:
        key = _animspec.resolve(entry)
        if key:
            spec = _animspec.ARCHETYPES[key]
            details.append({"asked": entry, "archetype": key,
                            "why": spec["why"], "loop": spec["loop"],
                            "pingpong": spec["pingpong"], "fps": spec["fps"]})
    return {
        "ok": True,
        "animations": built["animations"],
        "poses": built["poses"],
        "timing": built["timing"],
        "generated": built["generated"],
        "steps": built["steps"],
        "usd": round(per * built["generated"] + per, 4),
        "detail": details,
        "note": f"{built['generated']} images to generate, {built['steps']} "
                f"frames of playback (the difference is ping-pong), plus one "
                f"reference. Pass archetypes={list(archetypes)!r} to "
                "image_sprites to run exactly this, or edit `poses` and pass "
                "those instead.",
    }


@_tool
def image_sprites(character_prompt: Annotated[str, Field(description='The character and art style (full body, single character); framing/transparency contracts are appended.')], poses: Annotated[list[dict], Field(description='[{"name", "description"}] - name becomes the animation, description the stance. Omit when passing `archetypes`.')], name: Annotated[str, Field(description='Character name; emits <name>_sheet.png and <name>_frames.tres.')],
                  ref_image: Annotated[Optional[str], Field(description='Approved reference to reuse instead of generating one; also how a single pose is regenerated later.')] = None, frame_width: Annotated[int, Field(description='Cell width in px. Default 160; at default it is read from the sprite contract when one exists.')] = 160,
                  frame_height: Annotated[int, Field(description='Cell height in px. Default 240; at default it is read from the sprite contract when one exists.')] = 240, quality: Annotated[str, Field(description='Per-pose image quality: low | medium | high. Default medium.')] = "medium",
                  ref_quality: Annotated[str, Field(description='Quality of the ONE reference generation. Default high.')] = "high", fps: Annotated[float, Field(description='Playback speed written into the SpriteFrames. Default 8.')] = 8.0,
                  res_dir: Annotated[str, Field(description='res:// directory the sheet imports under. Default assets/sprites.')] = "assets/sprites",
                  limits: Annotated[Optional[dict], Field(description='{max_retries, timeout, max_seconds}. Unknown keys are refused.')] = None, provider: Annotated[str, Field(description='"" uses the stored art.provider then identity routing; openai EDITS the reference, krea follows it as style.')] = "",
                  model: Annotated[str, Field(description='Provider model id; on krea defaults to nano-banana-2 (holds identity through pose changes).')] = "", ref_strength: Annotated[float, Field(description='How hard the reference pulls, 0-1 (krea). Default 0.6.')] = 0.6,
                  archetypes: Annotated[Optional[list[str]], Field(description='Catalogue animations (e.g. ["idle", "walk", "attack"]) used INSTEAD of `poses`; call sprite_plan first.')] = None, view: Annotated[str, Field(description='Camera convention prepended to every pose ("side view, facing right"); default reads the sprite contract.')] = "",
                  palette: Annotated[Optional[dict], Field(description='{"lock": auto|on|off, "colors": [...]}; locking quantises every frame to the reference palette.')] = None,
                  sheet_padding: Annotated[int, Field(description='Transparent gutter between cells in px. 0 is a plain strip; 1-2 for non-integer scaling with linear filtering.')] = 0, anchor_views: Annotated[int, Field(description='How many views condition every pose: 3 (default) adds three-quarter and profile views; 1 is front-only.')] = 3) -> dict:
    """PAINTED sprite set - REFERENCE-FIRST for consistency.

    Generates ONE reference (or reuses ref_image), then each pose as an EDIT
    conditioned on it; frames are stitched into <name>_sheet.png +
    <name>_frames.tres. Default frame size and `view` come from the sprite
    contract. Pass `archetypes` INSTEAD of `poses` for catalogue key poses
    (sprite_plan). anchor_views=3 conditions every pose on three angles - the
    highest-leverage knob. THE MOST EXPENSIVE TOOL HERE: the plan is priced
    first and the estimate is reported; unknown limit keys are refused.
    Full notes: docs/tools.md#image_sprites
    """
    try:
        # THE CONTRACT SUPPLIES WHAT THE CALLER DID NOT - same rule as the
        # tileset manifest in level_generate: sprite_contract_set declared
        # the game's sheet shape ONCE (cell size, view), so the caller who
        # types frame_width=160 next to a 96x80 contract is re-deriving a
        # settled fact and usually getting it wrong. Any explicitly
        # non-default value wins outright; `contract_used` in the result
        # says which authority shaped the sheet.
        contract_used = False
        try:
            from bgate_core.art import spritecontract as _sc
            _c = _sc.contract_for(_scratch_root(), character=name)
            cell = list(_c.get("cell") or ())
            if (frame_width, frame_height) == (160, 240) and len(cell) == 2:
                frame_width, frame_height = int(cell[0]), int(cell[1])
                contract_used = True
            if not view and _c.get("view"):
                # The contract stores the view as a NAME; this tool takes the
                # camera as PROSE prepended to every pose. VIEW_CLAUSES is the
                # one mapping between them - the same clause the bible path
                # uses, so the two cannot disagree about what isometric means.
                view = _sc.VIEW_CLAUSES.get(str(_c["view"]), str(_c["view"]))
                contract_used = True
        except Exception:
            pass  # no contract is the pre-contract world, not an error
        timing: dict = {}
        if archetypes:
            # The catalogue REPLACES a hand-written pose list rather than
            # merging with one: two sources for the same animation's frames is a
            # way to get walk/0 twice with different descriptions, and the
            # emitter would happily ship it.
            if poses:
                return {"ok": False, "stage": "plan", "name": name,
                        "error": "pass EITHER archetypes OR poses, not both - "
                                 "the catalogue writes the whole pose list "
                                 "including its frame numbering, and merging it "
                                 "with hand-written poses duplicates frames. "
                                 "Call sprite_plan(archetypes) to see the poses "
                                 "it would generate, then edit those if you want "
                                 "to hand-tune them."}
            built = _animspec.plans(list(archetypes), view=view)
            poses, timing = built["poses"], built["timing"]
        if not poses:
            raise ValueError("poses list is empty (and no archetypes given)")
        for p in poses:
            if "name" not in p:
                raise ValueError(f"each pose needs a 'name': {p}")
        root = _Path(_scratch_root())
        # The most expensive tool here never got the provider preflight the
        # cheaper image tools gained - so a drained board learned it from a
        # paid 402 on the reference call.
        refused = _provider_gate(str(root), "image",
                                 f"a painted sprite set ({name!r})")
        if refused:
            return refused
        # An unnamed provider is the preference, then the identity routing —
        # the old default was the literal string "openai", which agents never
        # overrode, so the routing rule and the stored preference were both
        # unreachable from the most expensive tool here.
        provider = _providers.provider_for("sheet", asked=provider, root=root)
        art_dir = root / ".bgate_out" / "art" / name
        from bgate_adapters import imagegen, sprites as _sp

        # FIVE DIALS, TWO DOORS. `max_retries`/`timeout`/`max_seconds` are all
        # one question - how long is this run allowed to take and how hard may
        # it retry - and `palette_lock`/`palette_colors` are one other. As five
        # top-level parameters they sat among the ones that decide what the
        # sprite LOOKS like, which is how a 23-parameter tool reads as
        # undifferentiated. Unknown keys refused by name: a silently-ignored
        # `max_second` is a limit that was never set.
        limits = dict(limits or {})
        unknown = sorted(set(limits) - set(_SPRITE_LIMITS))
        if unknown:
            return _fail(ValueError(
                f"limits has no key(s) {unknown} - it takes "
                f"{sorted(_SPRITE_LIMITS)}"))
        lim = {**_SPRITE_LIMITS, **limits}
        max_retries = int(lim["max_retries"])
        timeout = int(lim["timeout"])
        max_seconds = int(lim["max_seconds"])

        palette = dict(palette or {})
        unknown = sorted(set(palette) - set(_SPRITE_PALETTE))
        if unknown:
            return _fail(ValueError(
                f"palette has no key(s) {unknown} - it takes "
                f"{sorted(_SPRITE_PALETTE)}"))
        pal = {**_SPRITE_PALETTE, **palette}
        palette_lock = str(pal["lock"])
        palette_colors = int(pal["colors"])

        # PRICE THE RUN BEFORE BUYING ANY OF IT. One reference (skipped when an
        # approved ref_image is reused) plus one edit per pose, at this call's
        # qualities. Retries are deliberately NOT in the estimate - they are
        # bounded per pose and caught by the running check below; pricing the
        # worst case up front would refuse healthy runs.
        def _unit(q: str) -> float:
            """What ONE call of this run costs, on the provider it will run on.

            This used to read the gpt-image price table whichever provider was
            named, which quietly under-quoted every Krea run - and an estimate
            fed the wrong provider's prices is worse than none, because it is
            presented to a human as what this run will cost.
            Krea prices per model and payload rather than per quality, so `q` is
            simply not part of its answer.
            """
            if provider == "krea":
                from bgate_adapters import krea as _krea

                return _krea.price_for(model or _krea.model_for("animation"),
                                       style_refs=1)
            if provider in ("local", "comfy", "localgen"):
                return 0.0        # the user's own GPU, and the ledger says so
            return imagegen.price_per_image(q)

        per_pose = _unit(quality)
        # The model-sheet views are priced in. They are bought whether or not the
        # anchor was passed in - a supplied ref_image is one approved drawing,
        # and the extra angles are derived FROM it - so they are not conditional
        # on the anchor being generated here.
        extra_views = max(0, min(2, int(anchor_views) - 1))
        projected = round(
            (0.0 if ref_image else _unit(ref_quality))
            + _unit(ref_quality) * extra_views
            + per_pose * len(poses), 4)
        deadline = _time.monotonic() + max(60, int(max_seconds))
        call_timeout = float(max(30, int(timeout)))

        # The stored visual identity, if one exists - injected into EVERY
        # prompt so no generation depends on anyone's memory of the character.
        profile = None
        for key in ((str(ref_image),) if ref_image else ()) + (name, f"{name}-character"):
            profile = _refs.profile_get(root, key)
            if profile:
                break
        identity = ""
        if profile:
            identity = (f" IDENTITY (must hold exactly): {profile['traits']}. "
                        f"STYLE (must hold exactly): {profile['style']}. "
                        f"NEVER: {profile['negative']}.")

        # 1. The reference - the single source of who this character is.
        result: dict = {"poses_attempted": len(poses),
                        "profile_used": bool(profile)}
        # Rolled-up cost/latency for the WHOLE set (ref + every pose edit,
        # retries included) — what the providers reported for this run, carried
        # on the sheet artifact so a reviewer sees what it cost.
        tally = {"usd": 0.0, "seconds": 0.0, "calls": 0}

        def _tally(r: dict) -> dict:
            tally["usd"] += float(r.get("usd") or 0.0)
            tally["seconds"] += float(r.get("seconds") or 0.0)
            tally["calls"] += 1
            return r
        if ref_image:
            ref_path = _refs.resolve(root, str(ref_image))
        else:
            ref_path = str(art_dir / "reference.png")

            def _gen_ref():
                # The anchor is sprite-shaped, so it goes through the keyable
                # contract like every pose does. It used to ask gpt-image for
                # background=transparent and take whatever came back - measured
                # 2026-07-25, that is a brown gradient with holes where the eyes
                # should be, and every pose then inherited a dirty anchor.
                r = _chroma.generate(
                    character_prompt + " Exactly one character, full body head to "
                    "toe, neutral idle stance, centered, no text, no logo, no "
                    "ground shadow.",
                    ref_path, provider=provider, model=model,
                    task_kind="anchor",
                    size="1024x1536", quality=ref_quality, root=root,
                    logical_name=name, work_item_id=_work_item_id(),
                    timeout=call_timeout)
                _tally(r)
                result["reference_chroma"] = r.get("chroma")
                result["reference_alpha"] = r.get("alpha")
                if r.get("ok") or r.get("rejected_path"):
                    # Archive the preview even for an alpha rejection - the
                    # whole value of failing loudly is that someone can LOOK at
                    # the backdrop the model painted instead of guessing.
                    result["reference_preview"] = _archive_preview(
                        ref_path, f"ref-{name}")
                return r

            def _ref_gate(r):
                """One verdict over both anchor gates: did the flat backdrop
                actually key clean (chroma audit), and is this a cut-out figure
                rather than a filled frame (structural sanity)?

                A provider failure is NOT this gate's business - it returns
                stage 'reference' below, because "the API refused" and "the
                model painted something unusable" need different fixes.
                """
                if not r.get("ok"):
                    return False, str(r.get("error") or "generation failed")
                return _reference_sanity(ref_path)

            ref = _gen_ref()
            if not ref.get("ok") and not ref.get("rejected_path"):
                return {"ok": False, "stage": "reference", **ref}
            # REFERENCE GATE: validate the anchor and re-roll it BEFORE paying
            # for N poses. A broken reference makes every pose broken - every one
            # fails the pose gate, every one gets retried, and the run costs ~2N
            # against garbage. Catch it here at ~1 spend. A passed-in ref_image is
            # already approved and skips this.
            ok_ref, ref_reason = _ref_gate(ref)
            rtries = max(0, int(max_retries))
            while not ok_ref and rtries > 0:
                rtries -= 1
                ref = _gen_ref()
                if not ref.get("ok") and not ref.get("rejected_path"):
                    return {"ok": False, "stage": "reference", **ref}
                ok_ref, ref_reason = _ref_gate(ref)
            result["reference_gate"] = {"ok": ok_ref, "reason": ref_reason}
            if not ok_ref:
                return {"ok": False, "stage": "reference_gate",
                        "reference": ref_path,
                        "reference_preview": result.get("reference_preview"),
                        "error": f"reference failed the structural gate: "
                                 f"{ref_reason}. Not spending on poses against a "
                                 "broken anchor - adjust character_prompt and retry."}
        result["reference"] = ref_path

        # 2. Each pose derives from the reference - same fighter, new stance.
        # ANCHOR + ROLLING conditioning: every edit carries (a) the character
        # ANCHOR - always present, so identity re-grounds each call and drift
        # can't compound telephone-style, (b) the PREVIOUS successful frame - # motion continuity, (c) for the closing frame of a multi-frame
        # animation, that animation's FIRST frame - so cycles loop smoothly
        # (walk/2 flows back into walk/0). ONE frame per API call, always.
        pose_files: list[tuple[str, str]] = []
        pose_errors: list[dict] = []
        prev_frame: Optional[str] = None
        anim_first: dict[str, str] = {}
        anim_counts: dict[str, int] = {}
        for p in poses:
            anim_counts[p["name"].split("/", 1)[0]] = \
                anim_counts.get(p["name"].split("/", 1)[0], 0) + 1
        pose_desc: dict[str, str] = {}
        # The keyable-background contract does the whole dance now - pick a key
        # colour this character never uses, demand a flat backdrop of it, key it
        # out, and AUDIT the cut. The audit is why a pose can now fail here: a
        # frame with a halo or background bleed used to be shipped and only
        # caught (maybe) at consistency_check, after the sheet was assembled.
        result["chroma"] = _chroma.pick_report(ref_path)

        def _edit_pose(desc, refs, out_png):
            got = _chroma.generate(
                "This exact character from the reference image"
                + (" (shown again in the other image(s) in different poses of "
                   "the same motion)" if len(refs) > 1 else "")
                + " - identical design, colors, face, and art style. CRITICAL: "
                "keep the EXACT SAME BODY BUILD, musculature, height, weight, head "
                "size and limb proportions as the reference in EVERY frame - do NOT "
                "slim him down, bulk him up, change his muscle definition, or restyle "
                "the body between frames; ONLY the pose changes"
                f" - now in this stance: {desc}. ONE single full-body "
                "character head to toe, exactly one figure, no text, no cropping of "
                "limbs."
                + identity,
                out_png, provider=provider, model=model,
                task_kind="animation",
                ref_paths=[str(r) for r in refs], size="1024x1536",
                # gpt-image EDITS the reference, so strength is meaningless to
                # it and chroma ignores the argument. Krea instead conditions on
                # style references at a weight, and that weight is the whole
                # difference between "same character, new pose" and "a picture
                # that vaguely rhymes with the reference".
                ref_strength=ref_strength,
                quality=quality, root=root, logical_name=name,
                work_item_id=_work_item_id(), timeout=call_timeout)
            _tally(got)
            return got

        def _stop_reason(next_cost: float) -> str:
            """Why this run must not start another paid call - or "" to go on.
            Checked before EVERY pose, because retries and a slow API turn a
            plan into a run that is still going long after it should be."""
            if _time.monotonic() >= deadline:
                return (f"run deadline reached ({max_seconds}s) after "
                        f"{tally['calls']} image calls")
            return ""

        # ── THE MODEL SHEET ──────────────────────────────────────────────────
        # ONE FRONT VIEW IS THE WEAKEST ANCHOR THAT STILL LOOKS LIKE AN ANCHOR.
        #
        # Every reference this run carried was the same view of the character:
        # the front-facing idle, plus previous frames that are near-copies of it.
        # The reference-conditioning literature is consistent that this is the
        # weak configuration - two or three images from DISTINCT ANGLES improve
        # identity retention substantially, and four distinct angles carry more
        # information than ten near-identical front shots. It is also just what
        # animation has always done: a model sheet exists so the profile and
        # three-quarter views are on the desk, and proportions do not wander
        # between drawings.
        #
        # It matters most in the case this tool is usually used for. A side-view
        # action game asks for side-view poses against a front-view anchor, so
        # the model re-invents the profile on EVERY call, slightly differently
        # each time. That is not drift a re-roll fixes - it is drift the anchor
        # never constrained, and re-rolling it buys another guess at the same
        # missing information.
        #
        # Cost is two generations per character, once, against a run that pays
        # one per pose and up to one more per re-roll. A twelve-pose set goes
        # from 13 calls to 15 before it has prevented a single re-roll.
        views: list[str] = []
        _VIEWS = (
            ("three_quarter",
             "the SAME character turned to a three-quarter view, facing "
             "front-left, same neutral stance, same scale, head to toe"),
            ("profile",
             "the SAME character in exact side profile, facing left, same "
             "neutral stance, same scale, head to toe"),
        )
        for label, angle in _VIEWS[:max(0, int(anchor_views) - 1)]:
            if not _Path(ref_path).is_file():
                break
            stop = _stop_reason(imagegen.price_per_image(ref_quality))
            if stop:
                result.setdefault("model_sheet_skipped", []).append(
                    {"view": label, "reason": stop})
                break
            view_png = str(art_dir / f"reference_{label}.png")
            got = _chroma.generate(
                "This exact character from the reference image - identical "
                "design, colours, face, build and art style. Show " + angle
                + ". Exactly one character, no text, no ground shadow." + identity,
                view_png, provider=provider, model=model, task_kind="anchor",
                ref_paths=[ref_path], ref_strength=ref_strength,
                size="1024x1536", quality=ref_quality, root=root,
                logical_name=name, work_item_id=_work_item_id(),
                timeout=call_timeout)
            _tally(got)
            # An auxiliary view IMPROVES the anchor; it is not part of it. A bad
            # one is dropped and the run continues on the views it does have - # failing the whole set because the profile came back badly would be
            # strictly worse than the single-view behaviour this replaces.
            ok_view, why = ((False, str(got.get("error") or "generation failed"))
                            if not got.get("ok") else _reference_sanity(view_png))
            if ok_view:
                views.append(view_png)
            else:
                result.setdefault("model_sheet_dropped", []).append(
                    {"view": label, "reason": why})
        sheet_refs = [ref_path] + views
        result["model_sheet"] = sheet_refs

        for pose in poses:
            pname = pose["name"]
            desc = pose.get("description", pname)
            pose_desc[pname] = desc
            stop = _stop_reason(per_pose)
            if stop:
                # Stop BUYING, don't abort: the poses already painted still
                # assemble into a partial sheet, and the caller is told exactly
                # which ones never ran and why.
                pose_errors.append({"name": pname, "error": f"skipped - {stop}"})
                continue
            anim, _, idx = pname.partition("/")
            out_png = str(art_dir / f"pose_{pname.replace('/', '_')}.png")
            refs = list(sheet_refs)
            is_last_of_cycle = (idx.isdigit() and anim_counts[anim] > 1
                                and int(idx) == anim_counts[anim] - 1)
            if is_last_of_cycle and anim in anim_first and anim_first[anim] != prev_frame:
                refs.append(anim_first[anim])
            if prev_frame:
                refs.append(prev_frame)
            got = _edit_pose(desc, refs, out_png)
            if got.get("ok"):
                pose_files.append((pname, out_png))
                prev_frame = out_png
                if anim not in anim_first:
                    anim_first[anim] = out_png
                # Register each pose as a candidate the moment it exists: the
                # Assets gallery streams the batch live (reviewable mid-run)
                # instead of going dark for a 30-minute silent mega-call.
                try:
                    _register_artifact(
                        f"{name}_{pname.replace('/', '_')}", out_png,
                        producer="image_sprites",
                        prompt=str(desc)[:500])
                except Exception:
                    pass
            else:
                pose_errors.append({"name": pname, "error": got.get("error")})

        if not pose_files:
            return {"ok": False, "stage": "poses", "failed": pose_errors,
                    "reference": ref_path,
                    "error": "every pose generation failed"}

        # 3. Assemble + AUTO CONSISTENCY GATE, with bounded retry of flagged frames.
        # The gate scores every frame vs the reference AND for frame-to-frame build
        # drift; any flagged pose is re-rolled (on its rolling refs, not the bare
        # anchor - see _rolling_refs) up to max_retries, keeping whichever roll
        # scores best. This turns the gate from "detects drift" into "converges
        # on a consistent sheet".
        import shutil as _shutil
        pose_order = [p for p, _ in pose_files]
        pose_path = {p: fp for p, fp in pose_files}

        def _rolling_refs(pname):
            """Reconstruct the ANCHOR+ROLLING ref list a pose was first built
            with, so a RETRY keeps motion continuity. Re-rolling on the bare
            anchor (the old behavior) optimizes the gate's identity metric while
            silently dropping the cross-frame conditioning - a re-rolled mid-cycle
            frame could score better on identity yet pop out of the walk. The gate
            doesn't measure motion, so nothing caught it. Rebuild: the model
            sheet, plus the cycle's first frame for a closing frame, plus the
            previous frame."""
            anim, _, idx = pname.partition("/")
            refs = list(sheet_refs)
            if (idx.isdigit() and anim_counts.get(anim, 1) > 1
                    and int(idx) == anim_counts[anim] - 1):
                first = pose_path.get(f"{anim}/0")
                if first and first not in refs:
                    refs.append(first)
            i = pose_order.index(pname)
            if i > 0:
                prev = pose_path.get(pose_order[i - 1])
                if prev and prev not in refs:
                    refs.append(prev)
            return refs

        # PALETTE LOCK: "auto" asks the reference what kind of art it is. Flat,
        # cel and limited-palette work is quantised to the reference's own
        # colours, which costs nothing visible and makes drift unrepresentable;
        # painterly work with real gradients is left alone, because locking it
        # would band the shading and that is a downgrade nobody ordered.
        lock_mode = str(palette_lock or "auto").strip().lower()
        # A palette PINNED IN THE BIBLE outranks the auto guess: pinning is the
        # project saying "these colours, everywhere", and it names the target
        # (the auto path can only lock to the reference's own colours). An
        # explicit palette_lock="off" still wins — a human's off is an off.
        pinned_palette = _artdirection.palette_pinned(str(root))
        if lock_mode in ("auto", ""):
            if pinned_palette:
                do_lock = True
                lock_why = (f"the project pins a {len(pinned_palette)}-colour "
                            "palette in the art bible - every sheet conforms to it")
            else:
                do_lock = _spritekit.looks_limited_palette(ref_path)
                lock_why = ("the reference reads as flat / limited-palette art, "
                            "where locking is free" if do_lock else
                            "the reference reads as painterly (many near-identical "
                            "shades), where locking would band the shading - left off")
        else:
            do_lock = lock_mode in ("on", "true", "yes", "1")
            lock_why = f"palette_lock={palette_lock!r}, set explicitly"

        def _assemble_and_gate():
            # ARITHMETIC BEFORE MONEY: a pure-scale outlier is the same
            # drawing at the wrong size, so it is scaled to the set median
            # for free here — the re-roll loop below is for defects a
            # resize cannot fix.
            fixed = _spritekit.normalise_heights(
                [p for p, _ in pose_files], pose_path)
            asm = _sp.from_pose_images(
                [(p, pose_path[p]) for p in pose_order],
                out_dir=str(root / ".bgate_out" / "sprites"), name=name,
                frame_size=(frame_width, frame_height), res_dir=res_dir, fps=fps,
                ref_path=ref_path, timing=timing or None,
                palette_lock=do_lock, palette_colors=palette_colors,
                target_palette=pinned_palette or None,
                pad=max(0, int(sheet_padding)))
            asm.setdefault("failed", [])
            asm["failed"].extend(pose_errors)
            asm.setdefault("palette", {})["mode"] = lock_mode
            asm["palette"]["why"] = lock_why
            cons = {"ok": False}
            if asm.get("ok"):
                fm = asm.get("frames", {})
                cons = _vision_consistency(ref_path, [(p, fp) for p, fp in fm.items()])
                # THE GEOMETRY RUNS EVEN WHEN THE JUDGE CANNOT. The vision
                # judge is provider-gated and sits out without its key — and
                # for one whole build that meant NO gate ran and a walk with
                # two 40%-oversized frames shipped as ok:True. facing_report
                # is free and local: its height findings join the flag set,
                # so the re-roll loop chases them with or without a judge,
                # and a judge that sat out is said out loud instead of
                # reading as a pass.
                if not cons.get("ok"):
                    cons = {"ok": True, "min": None, "flagged": [],
                            "scores": {},
                            "judge": "unavailable - geometry only"}
                geom = _spritekit.facing_report(
                    [p for p in pose_order if p in fm], fm)
                cons["geometry"] = geom["findings"]
                cons["height_fix"] = fixed["scaled"]
                geo_flag = {fr for f in geom["findings"]
                            if f["kind"] == "height_outlier"
                            for fr in f["frames"]}
                cons["flagged"] = sorted(
                    set(cons.get("flagged") or []) | geo_flag)
            return asm, cons

        assembled, consistency = _assemble_and_gate()
        best_min = consistency.get("min") if consistency.get("ok") else None
        best_flags = len(consistency.get("flagged") or [])
        tries = max(0, int(max_retries))
        while (consistency.get("ok") and consistency.get("flagged") and tries > 0):
            tries -= 1
            flagged = list(consistency["flagged"])
            backups = {}
            for pname in flagged:
                if pname not in pose_path or pname not in pose_desc:
                    continue
                stop = _stop_reason(per_pose)
                if stop:
                    # Re-rolls are where an unbounded run actually happens: the
                    # gate can flag every frame every round. The cap applies to
                    # them exactly as it does to the first pass.
                    # Into pose_errors, not into `assembled`: every assemble
                    # re-extends its failed list from here, and `assembled` is
                    # about to be replaced by the next one.
                    pose_errors.append(
                        {"name": pname, "error": f"regen skipped - {stop}"})
                    tries = 0
                    break
                bak = pose_path[pname] + ".bak"
                try:
                    _shutil.copy2(pose_path[pname], bak); backups[pname] = bak
                except Exception:
                    pass
                # Re-roll WITH the rolling refs, not the bare anchor - keep motion
                # continuity while the gate chases identity (see _rolling_refs).
                _edit_pose(pose_desc[pname], _rolling_refs(pname), pose_path[pname])
            asm2, cons2 = _assemble_and_gate()
            new_min = cons2.get("min") if cons2.get("ok") else None
            # Better means: the judge's floor rose — or, when the judge sat
            # out and there is no floor to compare, fewer flagged frames.
            # Without the second clause a geometry-only re-roll could never
            # be kept: min stays None, None never beats None, every fix
            # reverted.
            better = (new_min is not None
                      and (best_min is None or new_min > best_min))
            if new_min is None and best_min is None:
                better = len(cons2.get("flagged") or []) < best_flags
            if better:
                best_min = new_min
                best_flags = len(cons2.get("flagged") or [])
                assembled, consistency = asm2, cons2
                for bak in backups.values():
                    try: os.remove(bak)
                    except Exception: pass
            else:
                for pname, bak in backups.items():   # revert: this roll was no better
                    try: _shutil.copy2(bak, pose_path[pname]); os.remove(bak)
                    except Exception: pass
                assembled, consistency = _assemble_and_gate()

        assembled["reference"] = ref_path
        # Which authority shaped the sheet - the declared contract or this
        # call's own arguments. On a set that arrived the wrong size, this is
        # the one-read answer to "did somebody hand-type the cell".
        assembled["contract_used"] = contract_used
        assembled["cell"] = [frame_width, frame_height]
        # The model sheet is part of the run's provenance, not a detail of it:
        # "which views was this character conditioned on" is the first question
        # to ask about a set that drifted, and the answer has to survive into the
        # result a caller actually reads (which is `assembled`, not `result`).
        for key in ("model_sheet", "model_sheet_dropped", "model_sheet_skipped"):
            if key in result:
                assembled[key] = result[key]
        assembled["chroma"] = result.get("chroma")
        assembled["spend"] = {
            "usd": round(tally["usd"], 4),
            "image_calls": tally["calls"],
            "seconds": round(tally["seconds"], 2),
            # WHAT IT WAS QUOTED AT, beside what it came to. Nothing refuses a
            # run on either number - this product keeps no ledger and holds no
            # budget - but a plan whose actual cost ran well past its estimate
            # is the one fact a human needs before they run it again.
            "estimated_usd": projected,
            "timed_out": _time.monotonic() >= deadline,
        }
        if "reference_preview" in result:
            assembled["reference_preview"] = result["reference_preview"]
        if assembled.get("ok"):
            archived = _archive_preview(assembled["sheet"], f"painted-{name}")
            if archived:
                assembled["preview"] = archived

            frame_map = assembled.get("frames", {})
            assembled["consistency"] = consistency

            # PLAYABLE previews, one GIF per animation, at the sheet's own
            # timing. Motion review has only ever had stills; a pop or a loop
            # hitch is obvious in two seconds of playback and invisible in a
            # grid. Best-effort, before registration so they ride metadata.
            previews_gif = _gif_previews(frame_map, assembled["sheet"], name,
                                         timing, fps)
            if previews_gif:
                assembled["animation_previews"] = previews_gif

            artifact = _register_artifact(
                name, assembled["sheet"], producer="image_sprites",
                prompt=character_prompt,
                refs=[str(ref_image)] if ref_image else [ref_path],
                metadata={"poses": poses, "frames": frame_map,
                          "failed": assembled.get("failed", []),
                          "preview": archived or "",
                          "animation_previews": previews_gif,
                          "consistency": consistency,
                          "sequence": assembled.get("sequence"),
                          "motion": assembled.get("motion"),
                          "palette": assembled.get("palette"),
                          "timing": timing or None,
                          "fps": fps,
                          "animations": assembled.get("animations", {}),
                          "seconds": round(tally["seconds"], 2),
                          "usd": round(tally["usd"], 4),
                          "image_calls": tally["calls"]})
            if artifact:
                assembled["artifact"] = artifact
                # Record the check on the revision so the dashboard shows it and the
                # sheet stops reading as "NOT CHECKED · consistency".
                try:
                    _artifacts.record_check(_root(), assembled["sheet"], "consistency",
                                            consistency)
                except Exception:
                    pass
            # PALETTE: recorded like consistency is, and GATED HERE rather than
            # in artdirection.check - check() runs on raw generations before
            # anything has conformed them, so a hard flag there would reject
            # every image the pipeline was about to fix. Here the conform has
            # either run or failed to, which is the fact worth gating on.
            pal = assembled.get("palette") or {}
            if pinned_palette:
                try:
                    off = _artdirection.off_palette_fraction(
                        assembled["sheet"], pinned_palette)
                    pal["off_palette"] = round(off, 4)
                except Exception as exc:
                    pal["off_palette_error"] = str(exc)
                assembled["palette"] = pal
                try:
                    _artifacts.record_check(_root(), assembled["sheet"], "palette",
                                            {"ok": bool(pal.get("ok")),
                                             **{k: pal[k] for k in
                                                ("colors", "source", "off_palette")
                                                if k in pal}})
                except Exception:
                    pass
                if do_lock and not pal.get("ok"):
                    assembled["ok"] = False
                    assembled["stage"] = "palette"
                    assembled["error"] = (
                        "the project pins a palette but this sheet could not be "
                        f"conformed to it: {pal.get('note') or 'conform failed'}. "
                        "The sheet was kept for inspection but MUST NOT be "
                        "installed as-is.")

            cons_note = ""
            if consistency.get("ok"):
                cons_note = (f", consistency min {consistency.get('min')}"
                             + (f" - REGEN {consistency['flagged']}" if consistency.get("flagged")
                                else " (all pass)"))
            # THE GATE HAS TO GATE. Retries are exhausted by this point, so a
            # sheet still carrying flagged frames is the best this run will do
            # - and shipping it as ok=True is how "no outliers, min 80" reached
            # a human as if it meant on-model, for a sheet whose every frame the
            # judge had rejected.
            #
            # The sheet, the preview and the artifact all still exist: the work
            # is paid for either way and is worth looking at. What changes is
            # that the CALLER is told this failed, with the scores, instead of
            # having to read `consistency` to discover it.
            if consistency.get("ok") and consistency.get("flagged"):
                worst = sorted(
                    ((f.get("score"), f.get("label")) for f in consistency.get("frames", [])
                     if f.get("label") in set(consistency["flagged"])))[:3]
                assembled["ok"] = False
                assembled["stage"] = "consistency"
                assembled["error"] = (
                    f"{len(consistency['flagged'])}/{len(consistency.get('frames', []))} "
                    f"frames are off-model after {max_retries} retries "
                    f"(floor {_CONSISTENCY_FLOOR}, best {consistency.get('min')}); "
                    f"worst: {', '.join(f'{lab} {sc}' for sc, lab in worst)}. "
                    "The sheet and preview were kept for inspection but MUST NOT be "
                    "installed as-is - tighten character_prompt on the drifting "
                    "detail, or lower the floor if this is as good as the model gets.")

            # The .aseprite master, built whether or not a gate flipped ok -
            # a flagged sheet is exactly the one somebody opens to fix by
            # hand, and the master is how they do that with onion-skin
            # instead of a flat strip. Best-effort: absent Aseprite, absent
            # key, nothing changes.
            ase = _ase_master_for(assembled["sheet"],
                                  (frame_width, frame_height),
                                  assembled.get("animations") or {},
                                  timing or None, fps)
            if ase is not None:
                assembled["aseprite"] = ase

            seq = assembled.get("sequence") or {}
            seq_note = (f", height-jitter in {seq['flagged']}"
                        if seq.get("flagged") else "")
            # The motion report is what the identity judge structurally cannot
            # see: a duplicated frame, a popped pose, a cycle that does not
            # close and a figure in two pieces are all perfectly on-model, so
            # every score above the floor is compatible with every one of them.
            motion = assembled.get("motion") or {}
            kinds = sorted({f["kind"]
                            for anim in (motion.get("animations") or {}).values()
                            for f in anim.get("findings", [])})
            motion_note = (f", MOTION {'/'.join(kinds)} in {motion['flagged']}"
                           if motion.get("flagged") else "")
            _log("sprites", f"painted sprite set {name!r} (reference-first): "
                            f"{len(frame_map)}/{len(poses)} poses"
                            + (f", {len(assembled['failed'])} FAILED" if assembled["failed"] else "")
                            + cons_note + seq_note + motion_note,
                 ref=assembled["sheet"])
        # A MINT IS NOT MOTION. These frames are independently painted
        # stills — identity holds, but there is no cycle, and a character
        # shipped straight from here reads stiff as a board in the running
        # game. A screenshot cannot fail a motion check that never ran,
        # which is exactly how one shipped.
        assembled["next"] = (
            "this sheet is the MINT: identity, anchors, a start frame per "
            "drawn direction. IF this character moves in-game, the motion "
            "comes from animation_generate (RD animates the character's own "
            "frames into real cycles, ~$0.14/direction) — "
            "sprite_contract_set first if the project has no contract. A "
            "character that never moves (portrait, static NPC) is done "
            "here. Watch the animation_previews GIF before shipping "
            "anything that moves; stiffness is invisible in a screenshot.")
        return assembled
    except Exception as exc:
        return _fail(exc)


@_tool
def image_talkhead(subject: Annotated[str, Field(description='Who the portrait is of; the prompt every frame is generated from.')], name: Annotated[str, Field(description='Asset name; emits <name>_talk.png and <name>_talk.tres.')], anchor: Annotated[str, Field(description='ref_pin name or path every frame conditions on; empty makes the first generated frame the anchor.')] = "",
                   res_dir: Annotated[str, Field(description='res:// directory the portrait imports under. Default assets/portraits.')] = "assets/portraits", cell: Annotated[int, Field(description='Square cell size in px. Default 128.')] = 128,
                   fps: Annotated[float, Field(description='Playback speed of the talk loop. Default 10.')] = 10.0, provider: Annotated[str, Field(description='Image provider; "" uses the project\'s configured routing.')] = "",
                   model: Annotated[str, Field(description='Provider model id; "" takes the provider default.')] = "", ref_strength: Annotated[float, Field(description='How hard the anchor pulls, 0-1. Default 0.7.')] = 0.7,
                   drift_limit: Annotated[float, Field(description='Colour drift past which a frame is regenerated; 0 uses the module default.')] = 0.0, max_retries: Annotated[int, Field(description='Regeneration attempts per drifted frame. Default 2.')] = 2,
                   quality: Annotated[str, Field(description='low | medium | high. Default medium.')] = "medium", timeout: Annotated[int, Field(description='Seconds allowed per image call. Default 300.')] = 300) -> dict:
    """ANIMATED TALKING PORTRAIT: a face whose mouth moves while it speaks.

    Every frame conditions on `anchor` (a ref_pin name or path; with none the
    first frame becomes the anchor), never on the previous frame. Mouths are
    generated, frames are registered on silhouette WIDTH, and any frame past
    `drift_limit` is regenerated up to `max_retries` times. Emits
    `<name>_talk.png` (rest, half, wide, blink) and `<name>_talk.tres` with a
    looping `talk` and a one-shot `blink`. Returns {ok, sheet, tres, frames:
    [{frame, drift, attempts}], worst_drift}.
    Full notes: docs/tools.md#image_talkhead
    """
    from bgate_core.art import talkhead as _th

    root = _Path(_scratch_root())
    # Same resolution as image_sprites: preference, then identity routing.
    # The old default was the literal "krea", which failed a krea-less
    # setup unless the agent thought to override it.
    provider = _providers.provider_for("portrait", asked=provider,
                                       root=root)
    refused = _provider_gate(str(root), "image",
                             f"painting a talking head for {name!r}")
    if refused:
        return refused
    limit = float(drift_limit or _th.DRIFT_LIMIT)
    stage = root / ".bgate_out" / "art" / "talkheads" / name
    stage.mkdir(parents=True, exist_ok=True)

    # An anchor may be a pinned reference NAME or a path. Resolving the pin
    # here means an art agent uses the same anchor the rest of the pipeline
    # already agreed on, instead of inventing a second source of truth.
    anchor_path = ""
    if anchor:
        try:
            from bgate_core.art import refs as _refs
            hit = _refs.resolve(root, anchor)
            anchor_path = str(hit) if hit else ""
        except Exception:
            anchor_path = ""
        if not anchor_path and _Path(anchor).is_file():
            anchor_path = anchor

    made: dict[str, str] = {}
    report: list[dict] = []
    for frame in _th.MOUTHS:
        dest = stage / f"{frame}.png"
        attempts = 0
        drift_val = None
        while attempts <= int(max_retries):
            attempts += 1
            res = _chroma.generate(
                _th.prompt_for(subject, frame,
                               has_anchor=bool(anchor_path)),
                dest, provider=provider, model=model, task_kind="portrait",
                keyed=True, size="1024x1024", quality=quality,
                ref_paths=[anchor_path] if anchor_path else (),
                ref_strength=ref_strength, timeout=timeout, root=root,
                logical_name=f"{name}_{frame}")
            if not res.get("ok"):
                return {"ok": False, "error": res.get("error"),
                        "frame": frame}
            made[frame] = str(dest)
            # The FIRST successful frame becomes the anchor when none was
            # given, which is what makes a no-anchor call still coherent.
            if not anchor_path:
                anchor_path = str(dest)
            if len(made) == 1:
                drift_val = 0.0
                break
            drift_val = _th.drift(made, limit=limit)[frame]["drift"]
            if drift_val <= limit:
                break
        report.append({"frame": frame, "drift": drift_val,
                       "attempts": attempts,
                       "ok": (drift_val or 0.0) <= limit})

    order = list(_th.MOUTHS)
    # res_dir and name arrive from the model, and this writes with pathlib
    # rather than the Write tool - so the PreToolUse lane hook never sees
    # it. "../../.." would land outside the project entirely; contain it
    # here, where the write happens.
    out_dir = (root / "game" / res_dir).resolve()
    try:
        out_dir.relative_to(root.resolve())
    except ValueError:
        return {"ok": False,
                "error": f"res_dir {res_dir!r} escapes the project"}
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        return {"ok": False,
                "error": f"name {name!r} must be a bare asset name"}
    stitched = _th.sheet([(f, made[f]) for f in order],
                         out_dir / f"{name}_talk.png", cell=cell)
    tres_path = out_dir / f"{name}_talk.tres"
    tres_path.write_text(
        _th.spriteframes(f"{name}_talk.png", cell=cell, fps=fps,
                         order=order), encoding="utf-8")

    worst = max((r["drift"] or 0.0) for r in report)
    result = {"ok": True, "sheet": stitched["path"], "tres": str(tres_path),
              "frames": report, "worst_drift": worst,
              "drift_limit": limit, "registration": stitched["registration"],
              "anchor": anchor_path}
    if worst > limit:
        # Reported, not raised: three good frames and one off-model is still
        # worth handing back, and the number tells the agent which to redo.
        result["warning"] = (
            f"{sum(1 for r in report if not r['ok'])} frame(s) still past "
            f"the drift limit after {max_retries} retries")

    archived = _archive_preview(stitched["path"], f"talkhead-{name}")
    if archived:
        result["preview"] = archived
    artifact = _register_artifact(
        f"{name}_talk", stitched["path"], producer="image_talkhead",
        model=model or provider, prompt=subject,
        metadata={"frames": order, "cell": cell, "fps": fps,
                  "worst_drift": worst, "preview": archived or ""})
    if artifact:
        result["artifact"] = artifact
    _log("art", f"talking portrait {name} ({len(order)} frames, "
                f"worst drift {worst})", ref=archived or stitched["path"])
    return result


# ---------------------------------------------------------------------------
# Godot
# ---------------------------------------------------------------------------
@_tool
def godot_status() -> dict:
    """Is Godot available, and which version? Check before engine work."""
    probe = _godot.available()
    return {**probe, **(_godot.version() if probe["available"] else {})}


def _script_source(script: str, godot_project: Optional[str]):
    """(source, path_it_came_from). A one-line argument that names a .gd file
    on disk is a PATH, not a program — nothing else is treated as one.

    Deliberately narrow: multi-line input is source, always. The only thing
    that reads as a path is a single line ending in .gd, which no valid
    GDScript program is.
    """
    raw = str(script or "").strip()
    if "\n" in raw or not raw.lower().endswith(".gd"):
        return None, ""
    from pathlib import Path as _P

    bases = [_P(raw)]
    if raw.startswith("res://") and godot_project:
        bases = [_P(godot_project) / raw[len("res://"):]]
    elif godot_project:
        bases.append(_P(godot_project) / raw)
    try:
        bases.append(_P(_root()) / raw)
    except Exception:                                             # noqa: BLE001
        pass
    for candidate in bases:
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8",
                                           errors="replace"), str(candidate)
        except OSError:
            continue
    return None, raw


@_tool
def godot_run(script: str, godot_project: Optional[str] = None,
              timeout: int = 120) -> dict:
    """Run a GDScript headless and capture its output.

    `script` is EITHER the source itself OR a path to a .gd file. It MUST
    `extends SceneTree`, do its work in `_init()`, and call `quit()` - without
    quit() it runs until the timeout. Returns stdout, stderr and any
    parse/script errors: Godot prints SCRIPT ERROR and still exits 0, so check
    `errors`, not the exit code. godot_project is the directory holding
    project.godot, not the Builders Gate root.
    Full notes: docs/tools.md#godot_run
    """
    _contained_path(godot_project, "godot_project")
    source, from_path = _script_source(script, godot_project)
    if from_path and source is None:
        return {"ok": False, "error": f"{script} looks like a path and is not "
                                      "readable — pass the source itself, or a "
                                      "path that exists"}
    got = _godot.run_script(source if source is not None else script,
                            project_dir=godot_project, timeout=timeout)
    return {**got, "ran_from": from_path} if from_path else got



# THE EXPORT IS A DIFFERENT PROGRAM FROM THE EDITOR RUN. MEASURED (Corniche,
# 2026-09-04): six cars carried per-instance texture overrides that resolved in
# every godot_screenshot and vanished in the exported pck - the human saw six
# identical cars while every agent's evidence showed six liveries. Nothing in the
# pipeline had ever loaded the pck. This runs a SceneTree script against it.
@_tool
def godot_export_probe(pck: Annotated[str, Field(description='The exported .pck (or .zip) to probe - e.g. build/windows/Game.pck. Absolute or relative to the project root.')],
                       script: Annotated[str, Field(description='GDScript source OR a path to a .gd file. MUST `extends SceneTree`, load what it wants to check from res:// (which IS the pck), print findings, and call quit().')],
                       godot_project: Optional[str] = None,
                       timeout: int = 120,
                       headless: bool = True) -> dict:
    """Run a SceneTree script against an EXPORTED pck - what the player gets.

    `res://` inside the script resolves to the pck, not the project, so scene
    overrides, imports and resources are exactly the shipped ones. Use it for
    the release gate and after any delivery whose evidence came from an
    editor run: load the scene, walk the nodes, print the property you are
    asserting. `headless=False` opens a window so the script can save a
    viewport image (root.get_viewport().get_texture().get_image()). Returns
    stdout/stderr/exit_code; a missing pck or a script without quit() is an
    error, not a timeout to wait for.
    """
    import tempfile
    from pathlib import Path as _P
    _contained_path(godot_project, "godot_project")
    root = _P(godot_project or _root())
    pck_path = _P(pck) if _P(pck).is_absolute() else root / pck
    if not pck_path.is_file():
        return {"ok": False, "error": f"no pck at {pck_path} - export first "
                                      "(godot --headless --export-release <preset> <exe>)"}
    source, from_path = _script_source(script, godot_project)
    if from_path and source is None:
        return {"ok": False, "error": f"{script} looks like a path and is not readable"}
    src = source if source is not None else script
    if "quit(" not in src:
        return {"ok": False, "error": "the script never calls quit() - it would run until the timeout"}
    exe = _godot.find_godot()
    tmp = _P(tempfile.mkdtemp(prefix="bgate_pckprobe_"))
    try:
        sp = tmp / "probe.gd"
        sp.write_text(src, encoding="utf-8")
        cmd = [exe]
        if headless:
            cmd.append("--headless")
        else:
            cmd += ["--position", "2000,2000", "--resolution", "1280x720"]
        cmd += ["--main-pack", str(pck_path), "--script", str(sp)]
        import subprocess as _sp
        try:
            proc = _godot._spawn(cmd, timeout=timeout, cwd=str(pck_path.parent))
        except _sp.TimeoutExpired:
            return {"ok": False, "error": f"probe timed out after {timeout}s", "pck": str(pck_path)}
        out = proc.stdout or ""
        err = proc.stderr or ""
        errors = [ln for ln in (out + "\n" + err).splitlines()
                  if "SCRIPT ERROR" in ln or ln.startswith("ERROR:")]
        return {"ok": proc.returncode == 0 and not any("SCRIPT ERROR" in e for e in errors),
                "pck": str(pck_path), "exit_code": proc.returncode,
                "stdout": out[-20000:], "stderr": err[-4000:], "errors": errors[:20]}
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)



# A LEVEL-DESIGN TOOL FOR DRIVING GAMES. MEASURED (Corniche, 2026-09-04): with no
# tool, the tech seat hand-wrote a 1,900-line generator over 12 hours and the
# director rescued it five times (a folded closure, terrain through the road,
# rails "shattered" on grades, facets that read as speed bumps, a tunnel open to
# the sky). Every one of those lessons is now IN the shipped template, and the
# spec is a page of JSON a designer can read.
@_tool
def track_generate(spec: Annotated[dict, Field(description='The circuit, as JSON: {out_scene, road_width, sectors:[{name, kind:"straight"|"arc", length|radius+turn_deg, speed (m/s target), elevation (m gained), tunnel, barrier_left, barrier_right, checkpoint}], closure:{radius, min_radius, max_length, name}, terrain:{enabled, cols, margin, sea_level, sea_side:"left"|"right"|"none", hill_height, hill_distance, road_sink}, environment:{sun_elevation_deg, sun_azimuth_deg, environment (res path), sky_top, sky_horizon}, materials:{road, shoulder, ground, sea, barrier, tunnel} (res paths), props:[{scene, sectors, spacing, offset, side, scale:[min,max], visibility_range}], grid_slots, lap_min, lap_max, checkpoint_script, bake_interval}')],
                   godot_project: Optional[str] = None,
                   refresh_template: bool = False,
                   timeout: int = 300) -> dict:
    """Generate a closed, drivable circuit scene from a JSON spec.

    Walks the sectors (straights and arcs with grade), SOLVES the closure back
    to the start line as an arc-line-arc, bakes the road at 1.5 m, and emits a
    node-shaped scene: Road (+RoadBody on layers 1 and 6), RacingLine with
    target_speeds, Checkpoints, pitched Barrier runs, Tunnel roof/walls with
    lamps and a rock mass, a terrain heightfield clamped under the road, a Sea
    plane, GridSlot markers, Sun + WorldEnvironment, and MultiMesh props. Then
    it MEASURES: per-sector minimum radius, closure length/radius bars, lap
    length bars, and a road-support sweep (terrain never above the tarmac).
    `report.ok` is false when a bar fails, with the fix named. The generator
    lands in <project>/scripts/tools/bgate_track_gen.gd (editable; pass
    refresh_template=True to overwrite it from the shipped copy).
    """
    import json as _json
    import shutil as _sh
    from pathlib import Path as _P
    _contained_path(godot_project, "godot_project")
    proj = _P(godot_project or _root())
    if not (proj / "project.godot").is_file():
        return {"ok": False, "error": f"{proj} holds no project.godot"}
    tpl = _P(__file__).resolve().parent.parent / "templates" / "shared" / "tools"
    tools_dir = proj / "scripts" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ("bgate_track_gen.gd", "bgate_track_closure.gd"):
        dst = tools_dir / name
        if refresh_template or not dst.is_file():
            _sh.copy2(tpl / name, dst)
            copied.append(str(dst))
    spec_path = proj / ".bgate_track_spec.json"
    spec_path.write_text(_json.dumps(spec, indent=2), encoding="utf-8")
    src = (tools_dir / "bgate_track_gen.gd").read_text(encoding="utf-8")
    got = _godot.run_script(src, project_dir=str(proj), timeout=timeout)
    report_path = proj / ".bgate_out" / "track_report.json"
    report: dict = {}
    if report_path.is_file():
        try:
            report = _json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            report = {}
    out = {"ok": bool(report.get("ok")) and bool(got.get("ok")),
           "report": report, "spec_path": str(spec_path),
           "generator": str(tools_dir / "bgate_track_gen.gd"),
           "template_copied": copied,
           "engine_errors": got.get("errors") or [],
           "stdout_tail": (got.get("stdout") or "")[-3000:]}
    if not out["ok"] and not report:
        out["error"] = "the generator did not write a report - read stdout_tail/engine_errors"
    return out


# UI THAT IS DESIGNED, NOT DEFAULTED. MEASURED (the user, 2026-09-04): every
# bgate project's title, menu, HUD and results look the same because Controls
# are laid out first and a theme is patched on after. This puts CONCEPT FRAMES
# first - painted screens conditioned on the project's own style pins - and
# derives a palette and a Theme from them, so the gameplay seat lays out
# against a look that already exists.
@_tool
def ui_concept(game_summary: Annotated[str, Field(description='One or two sentences: what the game is and its mood, e.g. "a golden-hour coastal arcade racer, warm and fast".')],
               screens: Annotated[Optional[list[str]], Field(description='Screens to concept. Default: title, main_menu, hud, results.')] = None,
               use_pinned: Annotated[str, Field(description='Which pins condition the frames: "style" (default), "all", "concept", or "" for none.')] = "style",
               style_note: Annotated[str, Field(description='Extra art-direction words applied to every screen (typeface feel, motifs, era).')] = "",
               out_dir: Annotated[str, Field(description='Where the Theme and brief land, relative to the project. Default assets/ui.')] = "assets/ui",
               quality: str = "medium", provider: str = "", model: str = "") -> dict:
    """Paint concept frames for the game's screens, then derive a palette and
    a Godot Theme from them. Costs one image per screen.

    Writes .bgate_out/art/ui/<screen>.png per screen (conditioned on the
    project's pins), <out_dir>/ui_brief.md (palette hexes, per-screen layout
    notes, what to build) and <out_dir>/theme_concept.tres (Button/Label/Panel
    colours and StyleBoxFlats from the measured palette). The gameplay seat
    builds Controls AGAINST these; art refines them. Never ship the scaffold
    theme when this exists.
    """
    from pathlib import Path as _P
    root = _P(_scratch_root())
    refused = _provider_gate(str(root), "image", "painting UI concept frames")
    if refused:
        return refused
    from bgate_adapters import imagegen  # noqa: F401  (provider registry side effects)
    wanted = [str(s).strip() for s in (screens or ["title", "main_menu", "hud", "results"]) if str(s).strip()]
    layouts = {
        "title": "the TITLE SCREEN: the game logo large and centred-top in a bespoke display typeface, a full-bleed painted key visual behind it, a small 'press start' line at the bottom, no other UI",
        "main_menu": "the MAIN MENU: the logo smaller at top, a vertical stack of three menu entries (RACE / OPTIONS / QUIT) as designed buttons with a visible focus state on the first, a blurred/darkened key visual behind, hints for controls in a footer",
        "hud": "the in-game HUD over a gameplay frame: lap counter top-left, position top-left under it, lap timer top-right, a big speed readout bottom-right, a minimal centre - all chrome designed to the game's look, legible, not stock",
        "results": "the RESULTS SCREEN: a headline banner for the finishing position, a two-column panel with lap times on the left and the finishing order with driver names and colour swatches on the right, two buttons (RESTART / MENU) at the bottom, over a dimmed gameplay frame",
        "pause": "the PAUSE overlay: a compact centred panel with the logo, PAUSED, three entries (RESUME / RESTART / MENU), over a dimmed gameplay frame",
        "options": "the OPTIONS screen: four labelled sliders (master, music, sfx, engine), two toggles, a BACK button, in the same panel language as the menu",
    }
    pinned_names, pinned_paths = _pinned_refs(root, use_pinned) if use_pinned else ([], [])
    frames: dict = {}
    costs = 0.0
    for screen in wanted:
        body = layouts.get(screen, f"the {screen.replace('_', ' ')} screen of the game, fully designed")
        prompt = (f"UI concept frame, 16:9, for {game_summary}. Design {body}. "
                  f"Bespoke, opinionated game UI - a real art-directed screen, not a generic template: "
                  f"custom typography, a palette taken from the game's world, motifs from its setting. "
                  f"{style_note}").strip()
        out = _art_out(root, f"ui/{screen}.png")
        result: dict = {}
        # One retry on a provider-side timeout (measured: kie 524 "generate
        # task timeout" on the second frame of the first live run).
        for attempt in range(2):
            result = _chroma.generate(prompt, str(out),
                                      provider=_providers.provider_for("concept", asked=provider, root=root),
                                      model=model, task_kind="concept", keyed=None,
                                      size="1536x864", quality=quality, transparent=False,
                                      ref_paths=list(pinned_paths), ref_strength=0.45,
                                      anchors=[], tileable=False, root=root,
                                      logical_name=f"ui_{screen}", work_item_id=_work_item_id())
            if result.get("ok") or "timeout" not in str(result.get("error", "")).lower():
                break
        frames[screen] = {"ok": bool(result.get("ok")), "path": result.get("path"),
                          "error": result.get("error")}
        try:
            costs += float(result.get("usd") or 0.0)
        except Exception:
            pass
        if result.get("ok"):
            _archive_preview(result["path"], f"ui-{screen}")
    good = [f["path"] for f in frames.values() if f.get("ok") and f.get("path")]
    palette = _ui_palette(good)
    brief_dir = root / out_dir
    brief_dir.mkdir(parents=True, exist_ok=True)
    theme_path = brief_dir / "theme_concept.tres"
    theme_path.write_text(_ui_theme_tres(palette), encoding="utf-8")
    lines = [f"# UI brief - {game_summary}", "",
             "Concept frames (LOOK at them; they are the spec the Controls are built against):"]
    for k, f in frames.items():
        lines.append(f"- {k}: {f.get('path') or 'FAILED: ' + str(f.get('error'))}")
    lines += ["", "Measured palette (dominant colours across the frames):"]
    for name, hexv in palette.items():
        lines.append(f"- {name}: {hexv}")
    lines += ["", f"Theme: {theme_path} - Button/Label/Panel colours and StyleBoxFlats from the palette above. "
                  "Fonts: pick a display face for headings and a clean sans for body; do not ship the engine default.",
              "Rule: every screen instances ONE theme; the HUD speed readout is sized on the node, not in the theme."]
    (brief_dir / "ui_brief.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ok": bool(good), "frames": frames, "palette": palette,
            "theme": str(theme_path), "brief": str(brief_dir / "ui_brief.md"),
            "usd": costs, "refs_used": pinned_names}


def _ui_palette(paths: list) -> dict:
    """Dominant colours across the frames -> named roles. PIL median-cut."""
    from PIL import Image as _Img
    counts: dict = {}
    for p in paths:
        try:
            im = _Img.open(p).convert("RGB").resize((96, 54))
            q = im.quantize(colors=8, method=_Img.Quantize.MEDIANCUT)
            pal = q.getpalette()[:24]
            for n, idx in q.getcolors():
                rgb = tuple(pal[idx * 3: idx * 3 + 3])
                counts[rgb] = counts.get(rgb, 0) + n
        except Exception:
            continue
    if not counts:
        return {"background": "#1a1b2c", "panel": "#363a69", "text": "#fbfbd3",
                "accent": "#f8cb8b", "accent_2": "#cf7d52"}
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])

    def lum(c):
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    def sat(c):
        mx, mn = max(c), min(c)
        return 0.0 if mx == 0 else (mx - mn) / mx

    cols = [c for c, _ in ranked]
    darkest = min(cols, key=lum)
    lightest = max(cols, key=lum)
    vivid = sorted(cols, key=lambda c: -sat(c))
    accent = vivid[0] if vivid else lightest
    accent2 = vivid[1] if len(vivid) > 1 else accent
    mid_target = (lum(darkest) + lum(lightest)) / 2.0
    mid = sorted(cols, key=lambda c: abs(lum(c) - mid_target))[0]

    def hx(c):
        return "#%02x%02x%02x" % c

    return {"background": hx(darkest), "panel": hx(mid), "text": hx(lightest),
            "accent": hx(accent), "accent_2": hx(accent2)}


def _ui_theme_tres(p: dict) -> str:
    def col(h: str, a: float = 1.0) -> str:
        h = h.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        return "Color(%.4f, %.4f, %.4f, %.2f)" % (r, g, b, a)

    def box(name: str, bg: str, border: str, bw: str, radius: int = 4, margins: bool = True) -> str:
        out = ['[sub_resource type="StyleBoxFlat" id="%s"]' % name, "bg_color = " + bg]
        out += bw.split("\n") if bw else []
        out.append("border_color = " + border)
        for side in ("top_left", "top_right", "bottom_right", "bottom_left"):
            out.append("corner_radius_%s = %d" % (side, radius))
        if margins:
            out += ["content_margin_left = 24.0", "content_margin_top = 10.0",
                    "content_margin_right = 24.0", "content_margin_bottom = 10.0"]
        return "\n".join(out) + "\n"

    all4 = "border_width_left = 2\nborder_width_top = 2\nborder_width_right = 2\nborder_width_bottom = 2"
    parts = [
        '[gd_resource type="Theme" load_steps=6 format=3]',
        "; GENERATED by ui_concept from the project's own concept frames. Edit freely;",
        "; re-running ui_concept overwrites it.",
        "",
        box("panel", col(p["panel"], 0.92), col(p["accent"], 0.8), all4, 6),
        box("btn_normal", col(p["background"], 0.85), col(p["accent"], 0.6), "border_width_bottom = 2"),
        box("btn_hover", col(p["panel"], 1.0), col(p["accent"]), "border_width_bottom = 3"),
        box("btn_pressed", col(p["accent_2"]), col(p["accent_2"]), ""),
        box("btn_focus", "Color(0, 0, 0, 0)", col(p["accent"]), all4, 4, False),
        "[resource]",
        "default_font_size = 22",
        "Button/colors/font_color = " + col(p["text"]),
        "Button/colors/font_hover_color = " + col(p["accent"]),
        "Button/colors/font_pressed_color = " + col(p["background"]),
        "Button/colors/font_focus_color = " + col(p["accent"]),
        "Button/font_sizes/font_size = 26",
        'Button/styles/normal = SubResource("btn_normal")',
        'Button/styles/hover = SubResource("btn_hover")',
        'Button/styles/pressed = SubResource("btn_pressed")',
        'Button/styles/focus = SubResource("btn_focus")',
        "Label/colors/font_color = " + col(p["text"]),
        "Label/font_sizes/font_size = 22",
        'Panel/styles/panel = SubResource("panel")',
        'PanelContainer/styles/panel = SubResource("panel")',
    ]
    return "\n".join(parts) + "\n"



# A SOUND THAT SOUNDS LIKE THE THING. sfx_generate is a chiptune synth and says
# so; this asks the generation gateway (kie/Suno sounds) for a real effect and
# lands it in the audio seat's lane with its recipe next to it.
@_tool
def sfx_prompt(prompt: Annotated[str, Field(description='What it should sound like, up to 500 characters: "a mid-engine race car idling, steady loop, no music".')],
               name: Annotated[str, Field(description='Logical name; takes land as audio/sfx/<name>_<n>.<ext> plus <name>.prompt.json.')],
               loop: Annotated[bool, Field(description='Ask for a seamless loop (engine, skid, wind).')] = False,
               tempo: Annotated[int, Field(description='BPM hint for rhythmic sounds, 0 = none.')] = 0,
               model: Annotated[str, Field(description='V5 (default) or V5_5.')] = "V5",
               out_dir: Annotated[str, Field(description='Relative to the project; default audio/sfx.')] = "audio/sfx",
               timeout: int = 600) -> dict:
    """Generate a REAL sound effect through kie (Suno sounds) - engines,
    skids, impacts, ambience, UI - and deliver it to audio/sfx/. Costs
    credits per call; several takes may come back, all are kept. Use
    sfx_generate only for retro/8-bit projects. LISTEN before wiring.
    """
    import json as _json
    from pathlib import Path as _P
    root = _P(_scratch_root())
    refused = _provider_gate(str(root), "music", f"generating sound {name!r}")
    if refused:
        return refused
    from bgate_adapters import kie as _kie
    target = root / out_dir
    target.mkdir(parents=True, exist_ok=True)
    stem = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in name.strip()) or "sound"
    result = _kie.generate_sound(prompt, str(target), name=stem, root=str(root),
                                 timeout=float(timeout), model=model, loop=loop, tempo=tempo)
    recipe = {"prompt": prompt, "loop": loop, "tempo": tempo, "model": model,
              "task_id": result.get("task_id"), "provider": "kie",
              "takes": [t.get("path") for t in result.get("tracks", [])]}
    (target / f"{stem}.prompt.json").write_text(_json.dumps(recipe, indent=2), encoding="utf-8")
    if result.get("ok"):
        for t in result.get("tracks", []):
            try:
                _register_artifact(stem, t["path"], producer="sfx_prompt",
                                   work_item_id=_work_item_id())
            except Exception:                                    # noqa: BLE001
                pass
    return {**result, "recipe": str(target / f"{stem}.prompt.json"), "dir": str(target)}


# THE QA SEAT HAD NO WAY TO RUN A TEST. Its mission is "Own tests, repro,
# regression" and the brief it is dispatched with demands "tests at the known
# baseline, no new failures" - a question that could only be answered by
# hand-rolling a godot_run call per script, reading raw stdout, and counting by
# eye. dispatch._verify_rule already NAMES this project's test scripts in every
# seat prompt (game/tests/*.gd); this is the tool that runs the thing the prompt
# points at, and returns a per-script verdict instead of a wall of engine chatter.
#
# The pass/fail convention is a marker in the output - a line containing FAIL is
# a failure, one containing PASS is a pass - because that is what the existing
# .gd test scripts already print and inventing a framework nobody's tests use
# would make this tool answer about nothing. Exit code alone is not enough:
# Godot prints SCRIPT ERROR and still exits 0, which is why godot_run reports
# `errors` separately and why a script that errors is failed here regardless of
# how many PASS lines it managed first.
@_tool
def godot_test_run(paths: Optional[list[str]] = None, timeout: int = 180,
                   godot_project: Optional[str] = None,
                   mode: str = "failures_only") -> dict:
    """Run this project's own Godot test scripts headless and score them.

    Discovers `<godot project>/tests/*.gd`; `paths` runs a subset. Dotfiles
    and underscore-prefixed files are skipped. `mode`: summary | failures_only
    (DEFAULT) | changed | full - the default is bounded on purpose. TWO
    SIGNALS: `assertions_ok` (the script's FAIL markers) and `process_ok` (the
    engine ran cleanly); read `engine_error_scripts` - an engine complaint is
    not noise. A project with NO test scripts answers ok=false, no_tests=true.
    Every run is recorded to .bgate/engine-tests.jsonl.
    Full notes: docs/tools.md#godot_test_run
    """
    _contained_path(godot_project, "godot_project")
    from bgate_core.runtime import enginetests as _tests

    try:
        return _tests.run(_root(), paths=paths, timeout=timeout,
                          godot_project=godot_project or "",
                          actor=_actor(), mode=mode)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "modes": list(_tests.MODES)}


@_tool
def godot_templates() -> dict:
    """What project templates are available to scaffold."""
    return {"templates": _scaffold.list_templates()}


@_tool
def godot_scaffold(name: str, kind: str = "2d", dest: Optional[str] = None,
                   force: bool = False, replace: bool = False) -> dict:
    """Create a runnable Godot project wired for playtesting.

    kind: 2d (platformer slice) | 3d (first-person slice). dest defaults to
    <project root>/game. The template ships the BGate telemetry autoload and a
    player whose feel tunables are exported AND emitted. A non-empty dest is
    refused unless force or replace, and they differ: force=True fills in WHAT
    IS MISSING and SKIPS files that differ; replace=True puts the template
    back over the top, copying each victim to <name>.bak. The result lists
    `created`, `unchanged`, `skipped`, `replaced`.
    Full notes: docs/tools.md#godot_scaffold
    """
    target = dest or str(_Path(_root()) / "game")
    result = _scaffold.new_project(target, name, kind=kind, force=force,
                                   replace=replace)
    _log("scaffold", f"scaffolded {kind} project {name!r}", ref=result["path"])
    return result


@_tool
def godot_check_project(godot_project: str, timeout: int = 180) -> dict:
    """Import/validate a project headless - the 'does it still build' check.

    godot_project: the directory holding project.godot.
    """
    _contained_path(godot_project, "godot_project")
    return _godot.check_project(godot_project, timeout=timeout)


@_tool
def godot_import_asset(godot_project: str, src_path: str, dest_rel: str = "assets",
                       timeout: int = 240) -> dict:
    """THE DELIVERY PATH FOR ANY ASSET - 2D or 3D. A PNG belongs here too.

    Copies the file in, DROPS THE STALE IMPORT CACHE, reimports headless,
    loads the resource IN-ENGINE and reports what Godot built; `freshness`
    says whether the cached product matches the bytes on disk. `src_path`
    must be OUTSIDE the project. The destination is keyed on the filename
    alone - `replaced` says when an existing asset was overwritten. IMPORT
    SEQUENTIALLY: two headless imports fight over `.godot/`. godot_project:
    the directory holding project.godot.
    Full notes: docs/tools.md#godot_import_asset
    """
    _contained_path(godot_project, "godot_project")
    result = _godot.import_asset(godot_project, src_path, dest_rel=dest_rel,
                                 timeout=timeout)
    warning = (result.get("alpha_mode") or {}).get("warning")
    if warning:
        _log("asset", f"alphaMode:MASK in {src_path} - {warning[:160]}")
    # Register the landed asset so asset_verify covers it from birth. Only
    # possible when the game project lives inside the bgate root.
    if result.get("ok") and result.get("copied_to"):
        try:
            result["registry"] = _assets.track(_root(), result["copied_to"])
        except Exception as exc:
            result["registry"] = {"tracked": False, "reason": str(exc)}
        tris = result.get("engine_view", {}).get("total_tris", "?")
        _log("asset", f"landed {result['res_path']} ({tris} tris in-engine)",
             ref=result["res_path"])
        try:
            linked = _artifacts.record_check(
                _root(), result["copied_to"], "engine_import", result)
            if linked is None:
                _artifacts.record_check(
                    _root(), src_path, "engine_import", result)
        except Exception:
            pass
    return result


def _delivery_shot(result: dict) -> list[str]:
    """The in-engine frame this delivery captured, for the image block."""
    path = result.get("screenshot")
    return [path] if path else []


@_tool(images=_delivery_shot)
def godot_deliver_asset(godot_project: Annotated[str, Field(description='Directory holding project.godot.')], glb: Annotated[str, Field(description='The .glb to deliver, e.g. the out_path blender_combine just wrote.')], name: Annotated[str, Field(description='Scene/node name; empty derives it from the .glb filename.')] = "",
                        dest_rel: Annotated[str, Field(description='Directory under the Godot project the mesh is copied into. Default assets.')] = "assets", scene_rel: Annotated[str, Field(description='Directory under the Godot project the .tscn is written into. Default scenes.')] = "scenes",
                        script_res: Annotated[str, Field(description='res:// path of a script to attach to the root body; empty attaches none.')] = "", physics: Annotated[str, Field(description='auto (body chosen from the mesh) | all (mesh shapes on every mesh) | none (importer defaults, capsule is the collider).')] = "auto",
                        shape_type: Annotated[str, Field(description='Importer collision shape: trimesh (default), decompose_convex, simple_convex, box, sphere, cylinder, capsule.')] = "trimesh", body_type: Annotated[str, Field(description='Importer physics body kind for generated colliders: static (default), dynamic, area.')] = "static",
                        character_body: Annotated[str, Field(description='auto picks CharacterBody3D for skinned meshes and StaticBody3D otherwise; pass RigidBody3D or any class name to override.')] = "auto",
                        at: Annotated[float, Field(description='Seconds into the run the in-engine screenshot is taken. Default 1.2.')] = 1.2, min_size_m: Annotated[float, Field(description='real_world_size fails below this longest axis in metres. Default 0.05.')] = 0.05,
                        max_size_m: Annotated[Optional[float], Field(description='real_world_size fails above this; unset uses 4 m for skinned and 50 m otherwise.')] = None,
                        nominal_size_m: Annotated[float, Field(description='Expected height in metres used to frame the shot. Default 1.8.')] = 1.8, with_camera: Annotated[bool, Field(description='Add a first-person Camera3D to the character. Leave OFF unless this scene IS the player.')] = False,
                        overwrite_scene: Annotated[bool, Field(description='Rewrite an existing <name>.tscn instead of rewiring it, throwing away hand edits.')] = False, label: Annotated[str, Field(description='Caption for the archived preview frame.')] = "",
                        timeout: Annotated[int, Field(description='Seconds allowed for the import and capture. Default 300.')] = 300) -> dict:
    """3D ONLY: take a finished .glb the rest of the way - engine, then scene.

    IT TAKES A MESH AND NOTHING ELSE; 2D assets go through godot_import_asset.
    Imports, picks ONE collision strategy, instances the mesh under the body
    its kind implies (skinned -> CharacterBody3D, unskinned -> StaticBody3D),
    and RETURNS GODOT'S OWN SCREENSHOT plus `checks`. A failing gate still
    writes the scene. An existing <name>.tscn is REWIRED, not rewritten,
    unless overwrite_scene=True. A same-named .glb in assets/ is overwritten
    (`replaced`). with_camera only when this scene IS the player.
    Full notes: docs/tools.md#godot_deliver_asset
    """
    stem = name or _Path(glb).stem
    try:
        _contained_path(godot_project, "godot_project")
        shot_dir = str(_Path(_root()) / ".bgate_out" / "3d" /
                       _run_tag(label or stem))
    except Exception:
        shot_dir = None  # no project: the adapter falls back inside the game
    try:
        result = _godot.deliver_asset(
            godot_project, glb, name=name or None, dest_rel=dest_rel,
            scene_rel=scene_rel, script_res=script_res, physics=physics,
            shape_type=shape_type, body_type=body_type,
            character_body=character_body, screenshot_dir=shot_dir, at=at,
            min_size_m=min_size_m, max_size_m=max_size_m,
            nominal_size_m=nominal_size_m, with_camera=with_camera,
            overwrite_scene=overwrite_scene, timeout=timeout)
        checks = result.get("checks") or []
        failed = [str(check.get("check")) for check in checks
                  if check.get("required") and not check.get("ok")]
        # Say WHICH gate row failed. Left to the normalizer, the reason comes
        # out of whichever nested `detail` it finds first, which is a sentence
        # about metres rather than the name of the check to go and fix.
        if failed and not result.get("error"):
            result["error"] = (
                "the asset was delivered but the gate failed on "
                + ", ".join(failed) + " - the scene and the screenshot were "
                "written anyway; look at the frame and the `checks` rows")
        shot = result.get("screenshot")
        if shot:
            # Registered whether or not the gate passed. The failing delivery is
            # the one a reviewer most needs to look at, and an unregistered
            # frame is one art QA cannot name.
            archived = _archive_preview(shot, f"delivered-{stem}")
            if archived:
                result["screenshot_preview"] = archived
            artifact = _register_artifact(
                f"{stem}-in-engine", shot, producer="godot_deliver_asset",
                refs=[str(glb)],
                metadata={"glb": str(glb), "res_path": result.get("res_path", ""),
                          "scene": result.get("scene", ""),
                          "preview_scene": result.get("preview", ""),
                          "delivered": bool(result.get("ok")),
                          "checks": checks,
                          "failed_checks": [c.get("check") for c in checks
                                            if c.get("required")
                                            and not c.get("ok")],
                          "preview": archived or ""})
            if artifact:
                result["artifact"] = artifact
                result["artifact_id"] = artifact["id"]
            else:
                result["artifact_note"] = (
                    "no artifact was registered for this frame - it was written "
                    "outside the project root, so art QA and the dashboard "
                    "cannot see it")
            _log("asset", f"delivered {stem} into the engine"
                          + (" (gate failed)" if not result.get("ok") else ""),
                 ref=archived or shot)
        return result
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Scene editing - the node-level surgery the dashboard has always had
# ---------------------------------------------------------------------------
# bgate_core.level.scenewire has parsed and edited .tscn text since the Atlas builder
# shipped: load_steps accounting, ext_resource ids, name uniquing, block spans,
# a dry run on every mutation and a backup on every write. All of it was
# reachable from a browser and none of it from here, so an agent told to place a
# prop or repoint a texture hand-edited the file as TEXT - inventing ids,
# guessing at load_steps, and finding out at godot_check_project.
#
# These are the same functions the dashboard's /api/scene/* routes call, with
# the same dry-run and backup contract, plus one thing the routes did not have
# until today: the lock is honoured. That matters more here than there. A human
# clicking a button is one writer; the board runs several agents at once.


def _res_declared_type(asset_disk: _Path) -> Optional[str]:
    """The class a .tres declares itself to be. None for anything else.

    Guessing from the suffix calls every .tres a SpriteFrames - right for what
    the sprite pipeline writes, wrong for the project's TileSet. An ext_resource
    with the wrong type loads as null: the node draws nothing and says nothing.
    """
    if asset_disk.suffix.lower() != ".tres":
        return None
    try:
        head = asset_disk.read_text(encoding="utf-8", errors="replace")[:400]
    except OSError:
        return None
    return _scenewire.resource_type_of(head)


def _scene_lock_conflict(scene_disk: _Path) -> Optional[dict]:
    """The seat blocking a write to this scene, or None if we may proceed.

    A seat holding its OWN lock is not blocked by it - that is what taking the
    lock was for. Everyone else is, and `force` is the deliberate override.
    """
    held = _assets.lock_holder(_root(), scene_disk)
    if not held:
        return None
    if held.get("lock_seat") and held.get("lock_seat") == _seat():
        return None
    return {"seat": held.get("lock_seat"), "owner": held.get("lock_owner")}


def _scene_edit(godot_project: str, scene: str, mutate, *,
                dry_run: bool = False, force: bool = False,
                summary: str = "") -> dict:
    """Shared shape for every scene mutation: resolve, lock, edit, back up.

    ``mutate`` takes the scene text and returns scenewire's ``{text, ...}``.
    Nothing here writes when ``dry_run`` - it returns the resulting text so the
    caller can read the diff before committing, which is the reviewable step a
    hand-edit never had.
    """
    scene_disk, scene_res = _res_pair(godot_project, scene, ".tscn")
    if not scene_disk.is_file():
        raise ValueError(f"no scene at {scene_res}")
    if not dry_run and not force:
        blocked = _scene_lock_conflict(scene_disk)
        if blocked:
            raise RuntimeError(
                f"{scene_disk.name} is locked by the {blocked['seat']} seat. "
                "Lock it yourself with asset_lock before editing, wait for the "
                "holder, or pass force=True if you know the holder is gone.")

    text = scene_disk.read_text(encoding="utf-8", errors="replace")
    result = mutate(text)
    written = _scenewire.apply(scene_disk, result["text"], root=_root(),
                               dry_run=dry_run)

    out = {k: v for k, v in result.items() if k != "text"}
    out.update({"ok": True, "scene": scene_res, "dry_run": bool(dry_run),
                **written})
    if dry_run:
        out["text"] = result["text"]
    else:
        # The node COUNT, not the outline. A baked plate has 1500 nodes and
        # returning them all as a receipt would cost more context than the whole
        # rest of the task; scene_outline is one call away when it is wanted.
        try:
            out["node_count"] = len(_scenewire.parse(result["text"])["nodes"])
        except Exception:
            pass
        _log("scene", summary or f"edited {scene_res}", ref=scene_res)
    return out


@_tool
def scene_outline(godot_project: str, scene: str, match: str = "",
                  role: str = "", parent: str = "", properties: bool = False,
                  limit: int = 120) -> dict:
    """Read a scene's node tree - paths, types, roles, scripts, resources.

    THE READ THAT MAKES THE EDITS SAFE: every other scene tool addresses
    nodes by PATH, and this is where a path comes from. FILTER BEFORE YOU
    LOOK: `match` is a substring of the name or path, `role` one of character,
    prop, visual, ui, collision, layer, camera, audio, controller, marker,
    instance; `parent` returns only what hangs under that node. `total` always
    reports the true count. `properties` is off by default.
    Full notes: docs/tools.md#scene_outline
    """
    scene_disk, scene_res = _res_pair(godot_project, scene, ".tscn")
    if not scene_disk.is_file():
        raise ValueError(f"no scene at {scene_res}")
    text = scene_disk.read_text(encoding="utf-8", errors="replace")
    nodes = _scenewire.outline(text)
    total = len(nodes)
    # Counted over the WHOLE scene, before filtering - "what is in here" is
    # the question this answers, and it must not change shape depending on
    # what the caller happened to search for.
    roles: dict[str, int] = {}
    for n in nodes:
        roles[n["role"]] = roles.get(n["role"], 0) + 1

    needle = match.strip().lower()
    if needle:
        nodes = [n for n in nodes if needle in n["name"].lower()
                 or needle in n["path"].lower()]
    if role.strip():
        nodes = [n for n in nodes if n["role"] == role.strip()]
    if parent.strip():
        want = parent.strip()
        nodes = [n for n in nodes
                 if n["path"] == want or n["path"].startswith(want + "/")]
    matched = len(nodes)
    if limit and limit > 0:
        nodes = nodes[:limit]
    if not properties:
        nodes = [{k: v for k, v in n.items() if k != "properties"}
                 for n in nodes]

    held = _assets.lock_holder(_root(), scene_disk)
    return {"ok": True, "scene": scene_res, "total": total,
            "matched": matched, "returned": len(nodes),
            "truncated": matched > len(nodes),
            "roles": roles, "nodes": nodes,
            "lock": {"seat": held.get("lock_seat"),
                     "owner": held.get("lock_owner")} if held else None}


@_tool
def scene_wire(godot_project: str, scene: str, asset: str,
               parent: str = ".", node_name: str = "", node_type: str = "",
               dry_run: bool = False, force: bool = False) -> dict:
    """Put an asset into a scene as a new node, wired correctly.

    The node type comes from the FILE (.png -> Sprite2D, SpriteFrames .tres ->
    AnimatedSprite2D, .tscn -> instance); `node_type` overrides it. Allocates a
    non-colliding ext_resource id, reuses an existing reference, bumps
    load_steps and uniquifies the node name. A .gd is not an asset here - use
    scene_attach_script.
    Full notes: docs/tools.md#scene_wire
    """
    asset_disk, asset_res = _res_pair(godot_project, asset, "")
    return _scene_edit(
        godot_project, scene,
        lambda text: _scenewire.wire(
            text, asset_res, node_name=node_name or None, parent=parent,
            node_type=node_type or None,
            res_type=_res_declared_type(asset_disk)),
        dry_run=dry_run, force=force,
        summary=f"wired {asset_res} into {scene}")


@_tool
def scene_unwire(godot_project: str, scene: str, node: str,
                 recursive: bool = False, dry_run: bool = False,
                 force: bool = False) -> dict:
    """Remove a node from a scene, and sweep any resource left referenced by nothing.

    Refuses a node that has children unless `recursive` - deleting a parent and
    silently orphaning its subtree is not a thing anyone means. Run it dry first
    if you are not certain what hangs off it; `scene_outline(parent=...)` says.
    """
    _contained_path(godot_project, "godot_project")
    return _scene_edit(
        godot_project, scene,
        lambda text: _scenewire.unwire(text, node, recursive=recursive),
        dry_run=dry_run, force=force,
        summary=f"removed {node} from {scene}")


@_tool
def scene_node_add(godot_project: str, scene: str, name: str, node_type: str,
                   parent: str = ".", props: Optional[dict] = None,
                   dry_run: bool = False, force: bool = False) -> dict:
    """Add a plain node - a Camera2D, a Timer, a CanvasLayer, a grouping Node2D.

    A scene is not only the files in it. `props` sets properties in the same
    call, in Godot's own literal syntax where the type needs it:
    {"position": "Vector2(96, 40)", "z_index": 5, "visible": false}.
    """
    _contained_path(godot_project, "godot_project")
    return _scene_edit(
        godot_project, scene,
        lambda text: _scenewire.add_node(
            text, name=name, node_type=node_type, parent=parent,
            props=props or {}),
        dry_run=dry_run, force=force,
        summary=f"added {node_type} {name} to {scene}")


@_tool
def scene_set_property(godot_project: str, scene: str, node: str, key: str,
                       value=None, clear: bool = False,
                       dry_run: bool = False, force: bool = False) -> dict:
    """Set one property on one node - position, z_index, visible, scale, a flag.

    THIS IS THE MOVE TOOL. Vector and colour values are Godot literals passed
    as strings ("Vector2(320, 96)", "Color(1, 0.5, 0, 1)"); numbers, bools and
    strings pass as themselves. `clear=True` removes the property so the node
    returns to the class default. On a GENERATED scene (bake output in the
    .tscn header) the generator's input is the authority and your write lasts
    until the next bake.
    Full notes: docs/tools.md#scene_set_property
    """
    _contained_path(godot_project, "godot_project")
    return _scene_edit(
        godot_project, scene,
        lambda text: _scenewire.set_property(
            text, node, key, None if clear else value),
        dry_run=dry_run, force=force,
        summary=f"set {node}.{key} in {scene}")


@_tool
def scene_swap_resource(godot_project: str, scene: str, node: str, asset: str,
                        property: str = "", dry_run: bool = False,
                        force: bool = False) -> dict:
    """Point a node at a different file - try that sheet, that music, that scene.

    By hand this is four steps (find the scene, add an ext_resource, retype the
    property, delete the resource that is now unused) and the fourth is the one
    everybody skips, which leaves the old asset looking referenced to every tool
    that counts references - including Atlas's dead-asset rail.
    """
    asset_disk, asset_res = _res_pair(godot_project, asset, "")
    return _scene_edit(
        godot_project, scene,
        lambda text: _scenewire.swap_resource(
            text, node, asset_res, prop=property or None,
            res_type=_res_declared_type(asset_disk)),
        dry_run=dry_run, force=force,
        summary=f"swapped {node} to {asset_res} in {scene}")


@_tool
def scene_attach_script(godot_project: str, scene: str, script: str,
                        node: str = ".", dry_run: bool = False,
                        force: bool = False) -> dict:
    """Attach a .gd to a node that already exists. Defaults to the scene root."""
    _, script_res = _res_pair(godot_project, script, ".gd")
    return _scene_edit(
        godot_project, scene,
        lambda text: _scenewire.attach_script(text, script_res, node=node),
        dry_run=dry_run, force=force,
        summary=f"attached {script_res} to {node} in {scene}")


@_tool
def scene_rename_node(godot_project: str, scene: str, node: str, name: str,
                      dry_run: bool = False, force: bool = False) -> dict:
    """Rename a node and repair every path in the file that named it.

    A rename is not a one-line edit: children carry their parent's path, and
    NodePath properties elsewhere in the scene point at the old name. Doing it
    by hand is how a scene loads with half its wiring pointing at nothing.
    """
    _contained_path(godot_project, "godot_project")
    return _scene_edit(
        godot_project, scene,
        lambda text: _scenewire.rename_node(text, node, name),
        dry_run=dry_run, force=force,
        summary=f"renamed {node} to {name} in {scene}")


@_tool
def scene_reparent_node(godot_project: str, scene: str, node: str,
                        parent: str = ".", dry_run: bool = False,
                        force: bool = False) -> dict:
    """Move a node and everything under it beneath a different parent.

    Godot stores a node's transform LOCAL to its parent, and this moves the
    declaration, not the maths - a node reparented under something offset will
    land somewhere else on screen. Reparent for structure (into a YSort, onto a
    CanvasLayer), then fix position with scene_set_property.
    """
    _contained_path(godot_project, "godot_project")
    return _scene_edit(
        godot_project, scene,
        lambda text: _scenewire.reparent(text, node, parent),
        dry_run=dry_run, force=force,
        summary=f"reparented {node} under {parent} in {scene}")


# ---------------------------------------------------------------------------
# Causal chains - DESIGN.md §8 over shipped telemetry, no engine required


def _telemetry_path(session: Optional[int], telemetry_path: str) -> str:
    """Resolve a telemetry file from either an explicit path or a session id."""
    if telemetry_path:
        return telemetry_path
    if session is None:
        raise ValueError("pass either session (a playtest id) or telemetry_path")
    row = _playtest.get(_root(), session)
    path = row.get("telemetry_path") or ""
    if not path:
        raise ValueError(f"playtest session {session} has no telemetry file")
    return path


@_tool
def causal_chains(spec: str, session: Optional[int] = None,
                  telemetry_path: str = "", actor: str = "",
                  outcome: str = "", failed_gate: str = "", move: str = "",
                  limit: int = 40) -> dict:
    """Why did that action fail? The gate ladder, reconstructed from telemetry.

    A log line says `whiffed reason=facing`; a chain says which gates PASSED
    before that one failed. Works on telemetry the game already emits, no
    engine needed. `spec` names one of THIS PROJECT's chain specs (see
    causal_specs; the harness ships none - draft one with causal_infer_spec).
    Filter with actor, outcome (landed, failed, blocked, refused, aborted,
    dropped, unresolved), failed_gate, or move.
    Full notes: docs/tools.md#causal_chains
    """
    path = _telemetry_path(session, telemetry_path)
    chains = _causal.chains_from_file(path, spec, _root())
    summary = _causal.summarize(chains)
    filtered = _causal.find(
        chains, actor=actor or None, outcome=outcome or None,
        failed_gate=failed_gate or None, move=move or None, limit=limit)
    return {
        "ok": True,
        "telemetry": path,
        "spec": spec,
        "summary": summary,
        "returned": len(filtered),
        "chains": filtered,
    }


@_tool
def causal_specs() -> dict:
    """This project's chain specs, and whether each one's gate order is trusted.

    Read before trusting a chain. Every PASS in a chain is an INFERENCE from
    gate ordering, not an observation - sound only while the ladder matches the
    game's real resolution order. `order_verified: false` means nobody has
    checked it against the source yet, and chains from it mark passed gates
    with '~'.
    """
    specs = _causal.load_specs(_root())
    if not specs:
        return {"ok": True, "specs": {}, "count": 0,
                "hint": "none defined for this project - run "
                        "causal_infer_spec against a telemetry file to "
                        "draft one from the events your game emits."}
    return {"ok": True, "count": len(specs),
            "specs": {name: _causal.describe_spec(s)
                      for name, s in specs.items()}}


@_tool
def causal_infer_spec(session: Optional[int] = None, telemetry_path: str = "",
                      name: str = "", family: str = "",
                      save: bool = False) -> dict:
    """Draft a chain spec by reading what your game actually emits.

    Clusters event kinds into pipelines by prefix, guesses the opener, finds
    the actor field and collects observed `reason` values. It CANNOT infer the
    ORDER of the gates, so the draft comes back `order_verified: false`; put
    the ladder in the order your resolution code checks, then set it true.
    Until then chains mark passed gates with '~'. save=True writes
    .bgate/causal_specs.json.
    Full notes: docs/tools.md#causal_infer_spec
    """
    path = _telemetry_path(session, telemetry_path)
    result = _causal.infer_spec(_causal.read_events(path), name=name,
                                family=family)
    if result.get("ok") and save:
        spec_name = next(iter(result["spec"]))
        spec = _causal.spec_from_dict(spec_name, result["spec"][spec_name])
        result["saved"] = _causal.save_spec(_root(), spec)
    result["telemetry"] = path
    return result


# ---------------------------------------------------------------------------
# Reference anchors
# ---------------------------------------------------------------------------
@_tool
def ref_pin(name: str, path: str, kind: str = "style", note: str = "") -> dict:
    """Pin an APPROVED image as a canonical reference anchor.

    The file is copied into .bgate/refs/ (durable, travels with the project)
    under the given name; every seat brief lists the pins, and image_edit /
    image_sprites accept pin names anywhere they accept paths. Pin a character's
    approved reference, the style anchor, concept mocks from the user - the
    things art must stay consistent WITH. Re-pinning a name upgrades the anchor
    in place. kind: character | style | ui | concept.
    """
    return _refs.pin(_root(), name, path, kind=kind, note=note)


@_tool
def ref_list(kind: Optional[str] = None) -> dict:
    """The pinned reference anchors. Check BEFORE generating character/style art."""
    return {"refs": _refs.list_refs(_root(), kind=kind)}


@_tool
def profile_set(name: str, traits: str, style: str, negative: str) -> dict:
    """Store a character's visual identity - written while LOOKING at the pinned
    reference, never from memory. Injected automatically into every
    image_sprites generation for this character, and consistency_check judges
    against it. traits = what the character IS; style = the rendering style
    every frame must hold; negative = what must never appear.
    """
    return _refs.profile_set(_root(), name, traits=traits, style=style,
                             negative=negative)


@_tool
def profile_get(name: str) -> dict:
    """A character's stored visual identity (or {missing: true})."""
    got = _refs.profile_get(_root(), name)
    return got if got else {"missing": True, "name": name}


@_tool
def consistency_check(candidate_path: str, character: str) -> dict:
    """Judge a generated frame against its character - from a BUILT comparison,
    never from memory. Composes reference | candidate side-by-side on a
    checkerboard (alpha honesty), archives it to the gallery, and returns the
    profile checklist + a palette-drift tripwire. YOU then look at the
    composite and verdict each checklist line. A frame only lands if every
    line passes. This exists because three off-style batches were approved by
    agents judging frames in isolation.
    """
    from PIL import Image

    root = _Path(_root())
    ref_path = _refs.resolve(root, character)
    profile = _refs.profile_get(root, character)

    def _board(img: Image.Image) -> Image.Image:
        board = Image.new("RGB", img.size, (140, 140, 140))
        tile = 16
        for y in range(0, img.size[1], tile):
            for x in range(0, img.size[0], tile):
                if (x // tile + y // tile) % 2:
                    board.paste((180, 180, 180), (x, y, min(x + tile, img.size[0]),
                                                  min(y + tile, img.size[1])))
        board.paste(img, (0, 0), img)
        return board

    ref = Image.open(ref_path).convert("RGBA")
    cand = Image.open(candidate_path).convert("RGBA")
    h = 512
    ref.thumbnail((h, h))
    cand.thumbnail((h, h))
    combo = Image.new("RGB", (ref.width + cand.width + 12, max(ref.height, cand.height)),
                      (24, 24, 28))
    combo.paste(_board(ref), (0, 0))
    combo.paste(_board(cand), (ref.width + 12, 0))
    # Per-call composite: the shared consistency_check.png meant a second
    # seat's comparison landed on the path the first seat was told to LOOK
    # at, so a frame could be judged against someone else's reference.
    out = (root / ".bgate_out" / "art" / "checks" /
           f"{_run_tag(_Path(candidate_path).stem)}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    combo.save(out)
    archived = _archive_preview(str(out),
                                f"check-{_Path(candidate_path).stem}"[:40])

    # Palette tripwire (advisory - catches color drift, blind to identity).
    def _pal(img, n=6):
        img = img.copy()
        img.thumbnail((128, 128))
        px = [(r, g, b) for r, g, b, a in img.getdata() if a > 64]
        if not px:
            return []
        q = Image.new("RGB", (len(px), 1))
        q.putdata(px)
        q = q.quantize(n)
        pal = q.getpalette()[:n * 3]
        return [tuple(pal[i * 3:i * 3 + 3]) for _, i in
                sorted(q.getcolors(), reverse=True)[:n]]

    pa, pb = _pal(ref), _pal(cand)
    drift = (round(sum(min(sum((x - y) ** 2 for x, y in zip(c, d)) ** 0.5
                           for d in pb) for c in pa) / len(pa), 1)
             if pa and pb else None)

    checklist = ["same character design (species/build/proportions)",
                 "same rendering style (brushwork/detail level - no added "
                 "texture like fur, hair, etched lines)",
                 "same palette family", "no extra elements (glow, shadow, props)"]
    if profile:
        checklist.insert(0, f"matches traits: {profile['traits'][:160]}")
        checklist.insert(1, f"holds style: {profile['style'][:160]}")
        checklist.append(f"nothing from the negative list: {profile['negative'][:160]}")

    # ALPHA / TRANSPARENCY TRIPWIRE (automated - the palette check above is
    # blind to transparency because it samples only a>64). White halos,
    # feathered fringes, opaque background bleed, dirty RGB under zero alpha
    # and hollow interiors are what a checklist-by-eye keeps missing. The
    # measurements live in bgate_core.art.chroma.audit, which is the SAME code
    # the keyable path gates on at generation time - a frame cannot pass one
    # and fail the other.
    try:
        alpha = _chroma.audit(candidate_path)
    except Exception as ae:
        alpha = {"flags": [], "clean": None, "error": str(ae)}

    checklist = (["ALPHA fail: " + f for f in alpha.get("flags", [])]
                 + ["ALPHA look: " + f for f in alpha.get("review", [])]
                 + checklist)

    result = {"composite": archived or str(out), "reference": ref_path,
              "palette_drift": drift,
              "palette_note": "advisory: >30 = color drift likely; low values "
                              "do NOT prove identity",
              "alpha": alpha,
              "auto_fail": bool(alpha.get("flags")),
              "checklist": checklist,
              "instruction": ("LOOK at the composite. Verdict every checklist "
                              "line explicitly. Any fail = do not land. If "
                              "alpha.flags is non-empty the frame AUTO-FAILS on "
                              "transparency (white halo / bleed / hollow / dirty "
                              "alpha) - regenerate; do not land it.")}
    try:
        _artifacts.record_check(
            _root(), candidate_path, "consistency", result)
    except Exception:
        pass
    return result


@_tool
def art_qa_verdict(artifact_id: int, verdict: str, score: int = 0,
                   reasons: str = "") -> dict:
    """Record an INDEPENDENT art-QA reviewer's verdict on a candidate artifact.

    For a seat that did NOT make the image, after consistency_check. score
    (0-100) and reasons are stored under metadata.qa_review. verdict 'fail'
    REJECTS the revision outright; 'pass' does NOT approve it - the revision
    stays a candidate, marked machine-checked, for a human to approve. Returns
    {ok, artifact_id, verdict, score, status, awaiting_human, logical_name,
    revision}.
    Full notes: docs/tools.md#art_qa_verdict
    """
    verdict = (verdict or "").strip().lower()
    if verdict not in ("pass", "fail"):
        return _fail(ValueError("verdict must be 'pass' or 'fail'"))
    try:
        root = _root()
        art = _artifacts.get(root, int(artifact_id))
        reviewed = _artifacts.qa_verdict(
            root, int(artifact_id), passed=verdict == "pass", score=int(score or 0),
            note=f"art-QA {verdict}: {reasons[:400]}")
        awaiting = reviewed["status"] == "candidate"
        return {"ok": True, "artifact_id": int(artifact_id), "verdict": verdict,
                "score": max(0, min(int(score or 0), 100)),
                "status": reviewed["status"], "awaiting_human": awaiting,
                "logical_name": art["logical_name"], "revision": art["revision"],
                **({"next": "a human approves this revision in the dashboard - "
                            "art-QA cannot promote it to 'approved'"}
                   if awaiting else {})}
    except LookupError as exc:
        return _fail(exc)
    except Exception as exc:
        return _fail(exc)


@_tool
def art_tournament_verdict(match_id: int, winner_artifact_id: int,
                           reasons: str = "") -> dict:
    """Record an INDEPENDENT reviewer's pick in ONE pairwise art match.

    Given two candidates shown in the order your tournament brief listed,
    say which is better. PICK A WINNER - do not skip a close match.
    winner_artifact_id must be one of the two candidates IN THIS match, and a
    second verdict on a decided match is refused, not overwritten. Returns
    {ok, match_id, logical_name, winner_id}; ratings come from
    art_tournament_standings.
    Full notes: docs/tools.md#art_tournament_verdict
    """
    root = _root()
    match = _art_tournament.record_verdict(
        root, int(match_id), winner_artifact_id=int(winner_artifact_id),
        reasons=reasons)
    return {"ok": True, "match_id": match["id"],
            "logical_name": match["logical_name"],
            "winner_id": match["winner_id"]}


@_tool
def art_tournament_standings(logical_name: str, tournament_ref: str = "") -> dict:
    """Elo standings for a target, derived fresh from its decided matches.

    `tournament_ref` scopes the result to ONE tournament; left empty, every
    match ever recorded for `logical_name` is pooled, which misleads once a
    target has been re-tournamented. Returns {logical_name, standings:
    [{artifact_id, rating, matches, wins}] ranked highest first,
    decided_matches, pending_matches}. An artifact with no matches does not
    appear.
    Full notes: docs/tools.md#art_tournament_standings
    """
    root = _root()
    return {"ok": True, **_art_tournament.standings(
        root, logical_name, tournament_ref=(tournament_ref or None))}


@_tool
def ref_unpin(name: str) -> dict:
    """Remove a pin (the file itself is kept - deleting canon art is a human call)."""
    return _refs.unpin(_root(), name)


# ---------------------------------------------------------------------------
# Assets - locks for the files git can't merge
# ---------------------------------------------------------------------------
@_tool
def asset_lock(path: str, seat: str) -> dict:
    """Claim a binary asset for one seat BEFORE editing it.

    Binary files (.blend, .glb, textures, audio) don't merge - two agents editing
    one .blend loses someone's work. Lock first, edit, then asset_release. A held
    lock errors rather than queues: decide to wait, or work on something else.
    Lock-before-create is the normal flow for new assets.
    """
    bound_seat, owner = _lock_identity(seat)
    return _assets.lock(_root(), path, bound_seat, owner=owner)


@_tool
def asset_release(path: str, seat: str, force: bool = False) -> dict:
    """Release a lock when the edit is done - records the new content hash.

    Only the holding seat can release. force=True breaks anyone's lock (for a
    dead agent's stale claim) - a human's call, not a convenience.
    """
    if force:
        return _assets.force_release(_root(), path)
    bound_seat, owner = _lock_identity(seat)
    return _assets.release(_root(), path, bound_seat, owner=owner)


@_tool
def asset_track(path: str) -> dict:
    """Register an existing file under its content hash (sha256)."""
    return _assets.track(_root(), path)


@_tool
def asset_status(kind: Optional[str] = None, locked_only: bool = False) -> dict:
    """List tracked assets, optionally by kind or only the locked ones."""
    return {"assets": _assets.list_assets(_root(), kind=kind,
                                          locked_only=locked_only)}


@_tool
def pending_decisions(limit: int = 40) -> dict:
    """EVERYTHING WAITING ON A HUMAN, in one call. The gate's read path.

    A pending decision is a blocking gate that looks like silence. Three
    classes: `review` (work items parked by the builder's gate - the hard
    block), `candidates` (artifact revisions nobody dispositioned), `questions`
    (open ask_human questions, oldest first). `gate` names the approval mode;
    a non-empty list under 'none' is worth reporting. This approves NOTHING -
    hand the list to the human and keep working on what it does not block.
    Full notes: docs/tools.md#pending_decisions
    """
    from bgate_core.board import gates as _gatemode
    from bgate_core.board import queue as _q
    from bgate_core.board import steerbox as _steerbox

    root = _root()
    cap = max(1, min(int(limit or 40), 200))

    parked = [{"item_id": int(r["id"]), "seat": r["seat"],
               "title": r["title"], "since": r["updated_at"],
               "result": (r["result"] or "")[:240]}
              for r in _q.awaiting_review(root)[:cap]]

    candidates = []
    for art in _artifacts.list_revisions(root, status="candidate",
                                         limit=cap):
        qa = (art.get("metadata") or {}).get("qa_review") or {}
        candidates.append({
            "artifact_id": int(art["id"]),
            "logical_name": art.get("logical_name") or "",
            "revision": art.get("revision"),
            "path": art.get("path") or "",
            "work_item_id": art.get("work_item_id"),
            "producer": art.get("producer") or "",
            # WHETHER A MACHINE HAS ALREADY LOOKED. A candidate with a
            # passing qa_review is a different ask than a raw one - the human
            # is confirming a check, not performing the first one.
            "machine_verdict": qa.get("verdict") or "",
            "machine_note": (qa.get("reasons") or "")[:200],
        })

    try:
        questions = _steerbox.open_questions(root)[:cap]
    except Exception:
        questions = []

    state = _gatemode.state(root)
    total = len(parked) + len(candidates) + len(questions)
    return {
        "gate": {"mode": state["mode"], "source": state.get("source", ""),
                 "env_override": state.get("env_override", "")},
        "blocked_chains": parked,
        "candidates": candidates,
        "questions": questions,
        "total": total,
        "note": (
            "nothing is waiting on a human" if not total else
            f"{len(parked)} chain(s) stopped, {len(candidates)} candidate(s) "
            f"and {len(questions)} question(s) waiting. Only a human clears "
            "these - surface the list, say what each blocks, and carry on "
            "with the work that is not behind one."
            + (" NOTE: the approval gate is 'none' for this project, so "
               "nothing here should be stopping for a human - report that "
               "rather than working around it."
               if state["mode"] == _gatemode.NONE else "")),
    }


@_tool
def asset_verify() -> dict:
    """PRESENCE IS NOT CORRECTNESS: is every asset intact, WIRED, and CURRENT?

    Three questions: `intact` ('modified' = content changed with no lock
    held), `integration` ('unreferenced', 'dangling', and
    'delivered_but_unwired' - candidates, since 'dynamic_load_sites' counts
    paths built at run time), `freshness` (is the ENGINE serving the bytes on
    disk; 'stale' names the ones to repair with godot_check_project). Costs
    nothing and spawns no engine. Run before builds and after any multi-agent
    session.
    Full notes: docs/tools.md#asset_verify
    """
    return _assets.verify(_root())


# ---------------------------------------------------------------------------
# Iterations
# ---------------------------------------------------------------------------
@_tool
def iteration_status(limit: int = 10) -> dict:
    """Causal iteration history: snapshots, assets, playtests, decisions, work, outcome."""
    return {"iterations": _iterations.list_iterations(_root(), limit=limit)}


@_tool
def iteration_record_checks(status: str, summary: str = "",
                            checks: Optional[dict] = None) -> dict:
    """Attach automated-check results to the active iteration and next snapshot."""
    return _iterations.record_checks(
        _root(), {"status": status, "summary": summary,
                  "checks": checks or {}})


# ---------------------------------------------------------------------------
# Playtest
# ---------------------------------------------------------------------------
@_tool
def playtest_devices(filter_text: str = "") -> dict:
    """List mic inputs and open windows - pick what to record before starting."""
    return {
        "inputs": _recorder.list_inputs(),
        "windows": _recorder.list_windows(filter_text),
        "note": "pass an input 'index' as mic_device, and a window 'title' "
                "as window_title",
    }


@_tool
def playtest_check(mic_device: Optional[int] = None,
                   window_title: Optional[str] = None,
                   native: bool = False) -> dict:
    """Preflight a session: ffmpeg, mic SIGNAL, transcriber, target window.

    ALWAYS run this before playtest_start. It records a short mic sample and
    measures level - a muted or unplugged mic records perfect digital silence,
    which looks identical to a working one until the transcript comes back empty
    and the whole playthrough is wasted.
    """
    return _playtest.preflight(
        mic_device=mic_device, window_title=window_title,
        root=_root(), native=native)


@_tool
def playtest_start(name: str, window_title: Optional[str] = None,
                   mic_device: Optional[int] = None, build_ref: str = "",
                   fps: int = 30, launch_native: bool = False,
                   game_cmd: str = "") -> dict:
    """Start recording a play session - game window video + your voice.

    Play the game and talk out loud about what you like and what needs changing.
    Say it near when it happens; feedback is matched to game events by timestamp.

    window_title: match the game window (None = whole desktop). build_ref: the
    commit/build under test. Set launch_native to let the backend launch Godot
    with BGATE_TELEMETRY already attached; game_cmd optionally overrides the
    default <root>/game project command.
    """
    return _playtest.start(_root(), name, window_title=window_title,
                           mic_device=mic_device, build_ref=build_ref, fps=fps,
                           launch_native=launch_native, game_cmd=game_cmd)


@_tool
def playtest_stop(session_id: Optional[int] = None, model: str = "base",
                  transcribe_now: bool = True) -> dict:
    """Stop recording, then transcribe, align, and classify feedback.

    Transcription runs a whisper model in a subprocess; expect roughly a minute
    per 10 minutes of audio on CPU (the first run also downloads the model).
    Items land as 'new' - nothing becomes work until you promote it.
    """
    return _playtest.stop(_root(), session_id, model=model,
                          transcribe_now=transcribe_now)


@_tool
def playtest_brief(session_id: int, include_transcript: bool = False,
                   window_s: float = 4.0) -> dict:
    """The session as agents should read it: video frames + feedback + telemetry.

    You CAN watch the recording: `video_frames` is an ordered strip of stills
    ({i, t, path}) sampled across the whole session - Read them in order to see
    what happened. Each feedback item also carries a frame at its own moment and
    the game events within window_s of it, and `transcript` is what the player
    said, timestamped. Line frames up with the transcript by t.
    """
    return _playtest.brief(_root(), session_id, window_s=window_s,
                           include_transcript=include_transcript)


@_tool
def playtest_list(status: Optional[str] = None) -> dict:
    """List play sessions. status: recording | processing | ready | failed."""
    return {"sessions": _playtest.list_sessions(_root(), status=status)}


@_tool
def playtest_promote(item_id: int, seat: Optional[str] = None,
                     kind: Optional[str] = None, ref: str = "") -> dict:
    """Accept a feedback item as real work, optionally re-routing it.

    This is the human's call. Do not promote items on the user's behalf without
    being asked - thinking out loud mid-play is not a decision to build.
    """
    return _playtest.promote(_root(), item_id, seat=seat, kind=kind, ref=ref)


@_tool
def playtest_dismiss(item_id: int) -> dict:
    """Drop a feedback item - noise, or already handled."""
    return _playtest.dismiss(_root(), item_id)


@_tool
def playtest_telemetry_contract() -> dict:
    """What the game must emit so spoken feedback becomes actionable numbers."""
    return _playtest.telemetry_contract()


# ---------------------------------------------------------------------------
# Seats - stable roles, write lanes, and the blackboard
# ---------------------------------------------------------------------------
@_tool
def seat_list() -> dict:
    """The project's seats: role, mission, write lanes. Adopt one before working."""
    return {"seats": list(_seats.roles_for(_root()).values())}


@_tool
def seat_brief(role: str) -> dict:
    """Everything a seat needs to start working, in one call.

    Mission, write lanes, the bible (with the scope cut applied), canon entities,
    the promoted playtest feedback routed to this seat, held/others' locks, and
    recent blackboard notes. Read this BEFORE doing seat work - it replaces
    re-deriving the project state from scratch.
    """
    return _seats.brief(_root(), role)


@_tool
def seat_can_write(role: str, path: str) -> dict:
    """May this seat write this path? Check BEFORE editing outside your obvious lane.

    Two gates: inside the seat's write lanes, and not locked by another seat.
    Fails closed for unknown/disabled seats. `allowed: false` DOES NOT ALWAYS
    MEAN THE WRITE WILL BE REFUSED: the result also carries `enforced`,
    `lane_mode` (collide | warn | block), `aegis_mode` (the project boundary,
    block by default) and `what_happens`. A lock or lease collision blocks in
    EVERY mode.
    Full notes: docs/tools.md#seat_can_write
    """
    from bgate_core.board import aegis as _aegis

    verdict = _seats.can_write(_root(), role, path)
    lane_mode = _seats.lane_mode()
    collision = bool(verdict.get("owner"))
    enforced = collision or lane_mode == "block"
    if verdict.get("allowed"):
        what = "this write is in lane and unlocked; it lands."
    elif collision:
        what = (f"BLOCKED in every mode: {verdict['owner']} holds this file "
                "right now. A collision is a fact about two live runs, not a "
                "rule about one.")
    elif lane_mode == "block":
        what = "BLOCKED: lanes are enforced on this board (BGATE_LANES=block)."
    elif lane_mode == "warn":
        what = ("NOT BLOCKED. Lanes are advisory here (BGATE_LANES=warn): the "
                "write will land and the human is told. It is still the wrong "
                "seat for this path - queue_add the owning seat instead of "
                "writing it yourself.")
    else:
        what = ("NOT BLOCKED. Lanes are waived here (BGATE_LANES=collide); "
                "only collisions and the project boundary bite.")
    return {**verdict, "enforced": enforced, "lane_mode": lane_mode,
            "aegis_mode": _aegis.mode(), "collision": collision,
            "what_happens": what}


@_tool
def seat_configure(role: str, enabled: Optional[bool] = None,
                   write_globs: Optional[list[str]] = None,
                   mission: Optional[str] = None,
                   persona: Optional[dict] = None) -> dict:
    """Override a seat for this project: change its mission or its look on the
    studio floor, or (human only) its write lanes and enabled flag.

    `mission` may be rewritten by any caller. `write_globs` and `enabled` are
    PERMISSIONS and an agent calling with them is refused. `persona` is merged
    key by key: style (manner appended to dispatch prompts), name, lines, cast,
    surface (carpet | tile | wood | vinyl | concrete), vibe. Returns the merged
    seat or {ok: false, error}, including on the permission refusal.
    Full notes: docs/tools.md#seat_configure
    """
    privileged = [name for name, value in
                  (("write_globs", write_globs), ("enabled", enabled))
                  if value is not None]
    if privileged and _caller_is_agent():
        raise PermissionError(
            f"{_actor() or 'an agent session'} may not change "
            f"{', '.join(privileged)} on seat {role!r} - write lanes and the "
            "enabled flag are a human's call, because a seat that can widen "
            "its own lanes has no lanes. Change the mission here if that is "
            "what you meant, or ask the human to edit the seat in the "
            "dashboard (Seats -> " + role + ").")
    return _seats.configure(_root(), role, enabled=enabled,
                            write_globs=write_globs, mission=mission,
                            persona=persona)


@_tool
def seat_post_note(role: str, body: str, topic: str = "") -> dict:
    """Leave a note on the blackboard for other seats.

    Post when your work changes another seat's world: an asset re-exported, a
    tunable renamed, a scope call made. Short and factual beats long and vague.
    """
    return _seats.post_note(_root(), role, body, topic=topic)


@_tool
def handoff_note(kind: str, text: str, refs: Optional[list] = None) -> dict:
    """Record IN-FLIGHT state on the project thread, for the next session.

    An append-only trail read at the start of the next session. CALL IT AS
    YOU GO, not at the end. kind: state (what is half-done), decision (WITH
    the reason; settled canon belongs in the bible - cite it in refs),
    deferred (what you chose NOT to do, and why), blocker (what is in the way
    and who owns it), next (the very next action). refs: ids/paths this note
    points at ("bible#12", "item 41"); cite, do not duplicate.
    Full notes: docs/tools.md#handoff_note
    """
    return _handoff.note(_root(), kind, text, refs=refs)


@_tool
def handoff_read(limit: int = 0, kind: str = "") -> dict:
    """The project thread, oldest first - what earlier sessions left behind.

    The SessionStart hook already injects the tail of this into every session, so
    reach for it when you need MORE than that: the whole history, or one kind
    (`deferred` before you "fix" something, `decision` before you re-litigate
    one). limit=0 is everything; a positive limit takes the most recent N.
    """
    trail = _handoff.read(_root(), limit=limit, kind=kind)
    return {"notes": trail, "count": len(trail),
            "path": str(_handoff.path_for(_root()))}


@_tool
def seat_notes(topic: Optional[str] = None, role: Optional[str] = None,
               limit: int = 20) -> dict:
    """Read the blackboard, newest first, optionally filtered by topic or role."""
    return {"notes": _seats.read_notes(_root(), topic=topic, role=role,
                                       limit=limit)}


# ---------------------------------------------------------------------------
# The decision register, and the list of things this project is NOT building
# ---------------------------------------------------------------------------
# THE LISTING TOOLS MATTER MORE THAN THE WRITING ONES. The director's mission has
# always said "an unsaid no gets built anyway", and until now the no was unsaid
# BY CONSTRUCTION: there was nowhere to write it, so every agent in every session
# started from a blank sheet and re-proposed whatever had been ruled out. An
# agent that cannot read the no-list builds the no. Call not_building_list before
# you file work, and decision_list before you re-litigate something.
#
# WRITING SPLITS THE SAME WAY THE HTTP ROUTES DO (see bgate_ui/routes/
# decisions.py, which argues it at length): a proposal is open to an agent, a
# ruling is not. A settled decision binds every other seat, and a refusal is read
# as binding with no acceptance test anyone can check it against - an agent that
# could write either would be authorising its own work. So a dispatched session
# gets 'open' and a message naming the verb that IS available to it, rather than
# a silent downgrade it would never notice.

@_tool
def decision_add(title: str, acceptance: str, leaves_dark: str,
                 state: str = "settled", work_item_id: Optional[int] = None,
                 session_id: Optional[int] = None) -> dict:
    """File a decision - with its acceptance test and what it leaves dark.

    All three are MANDATORY and the tool refuses without them. acceptance: how
    anyone checks the call was honoured ("the hub loads in under 2s on the
    3060 box"). leaves_dark: what this call deliberately does NOT cover. state:
    'settled' is a ruling and only a human session may file one; 'open' is a
    PROPOSAL for the director's rail - a dispatched agent asking for 'settled'
    gets a refusal. work_item_id / session_id link the ruling to its origin.
    Full notes: docs/tools.md#decision_add
    """
    if state == "settled" and _caller_is_agent():
        return _fail(PermissionError(
            f"{_actor() or 'an agent session'} may not SETTLE a decision - a "
            "settled decision binds every other seat, and an agent that settles "
            "its own decisions authorises its own work. File it with "
            "state='open' instead: it lands in the director's Awaiting a ruling "
            "rail with the acceptance test and left-dark you wrote, and a human "
            "turns it into a ruling."))
    try:
        out = _decisions.add(_root(), title, acceptance, leaves_dark,
                             state=state, work_item_id=work_item_id,
                             session_id=session_id)
        _log("decision", f"{out['state']} {out['title'][:60]!r}",
             ref=f"decision:{out['id']}")
        return out
    except Exception as exc:
        return _fail(exc)


@_tool
def decision_list(state: Optional[str] = None,
                  work_item_id: Optional[int] = None) -> dict:
    """What this project has settled, newest first - each with its test.

    READ THIS BEFORE RE-OPENING AN ARGUMENT. A decision here was made once, with
    a reason and a test; re-deciding it from scratch is the most expensive thing
    a fresh session does, and the second most expensive is quietly contradicting
    it.

    state: settled | open | superseded. No state returns all three, so a reader
    sees at a glance what is ruled, what is waiting on a human, and what was
    replaced. A superseded row keeps `superseded_by` pointing at whatever won - that pair is how you learn an idea was already tried.
    """
    rows = _decisions.list_decisions(_root(), state=state or "",
                                     work_item_id=work_item_id)
    return {"decisions": rows, "count": len(rows),
            "open": sum(1 for r in rows if r["state"] == "open")}


@_tool
def decision_settle(decision_id: int) -> dict:
    """Turn an open proposal into a ruling. Human sessions only.

    The proposal keeps its acceptance test and its left-dark; what changes is
    that it now binds the other seats, and `actor` becomes whoever settled it - accountability follows the ruling, not the draft.
    """
    if _caller_is_agent():
        return _fail(PermissionError(
            f"{_actor() or 'an agent session'} may not settle decision "
            f"{decision_id} - settling is the act that binds the other seats, "
            "and it is a human's. The proposal is already on the director's "
            "Awaiting a ruling rail, which is where it gets one."))
    try:
        out = _decisions.settle(_root(), decision_id)
        _log("decision", f"settled {out['title'][:60]!r}",
             ref=f"decision:{decision_id}")
        return out
    except Exception as exc:
        return _fail(exc)


@_tool
def not_building_add(text: str, reason: str, tag: str = "",
                     decision_id: Optional[int] = None) -> dict:
    """Write down something this project is deliberately NOT building.

    Human sessions only: an agent that wants to refuse something calls
    decision_add(state='open') and a human turns it into a line here. reason
    is mandatory - an unexplained no is re-proposed every few weeks. tag is
    free-form and optional ('scope', 'engine', 'v2').
    Full notes: docs/tools.md#not_building_add
    """
    if _caller_is_agent():
        return _fail(PermissionError(
            f"{_actor() or 'an agent session'} may not write the no-list - "
            "every agent reads it as binding and nothing can check it was "
            "right, so it is a human's list. Call decision_add with "
            "state='open' to propose the refusal instead."))
    try:
        out = _decisions.refuse(_root(), text, reason, tag=tag,
                                decision_id=decision_id)
        _log("decision", f"not building {out['text'][:60]!r}",
             ref=f"not_building:{out['id']}")
        return out
    except Exception as exc:
        return _fail(exc)


@_tool
def not_building_list(tag: Optional[str] = None) -> dict:
    """What this project has said no to. CALL THIS BEFORE YOU FILE WORK.

    Each row carries the thing refused and WHY. A refusal is the current
    answer with its reason attached, not a permanent law: if the reason no
    longer holds, say so - do not build the thing anyway, and do not work
    around it, because a workaround for a deliberate no is the no getting
    built with extra steps.
    Full notes: docs/tools.md#not_building_list
    """
    rows = _decisions.list_not_building(_root(), tag=tag or "")
    return {"not_building": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# Quests
# ---------------------------------------------------------------------------
# THE THIRD NOUN IN THE NARRATIVE SEAT'S MISSION. "Own the lore graph, quests,
# and dialogue" - and until now two of those three had tools and the middle one
# had nothing, so an agent holding the seat could write the world and the
# conversations in it and had no way to record what the player is asked to DO.
#
# THE ONE RULE THESE TOOLS ENFORCE is that every step names the observable that
# closes it. `done_when` is refused blank, with a sentence saying what to type,
# for the same reason decision_add refuses a blank acceptance test: a step that
# nothing can finish is not a step, and the moment to find that out is while
# writing it rather than while implementing it.
#
# CALL canon_check YOURSELF ON THE PREMISE FIRST. The HTTP route runs it on the
# write path because a browser has no other way to; here you have the tool, and
# the seat's brief already tells you to run it on every narrative write BEFORE it
# lands. quest_add does not run it for you, so that a quest deliberately
# introducing a new character is not fighting the checker.

@_tool
def quest_add(title: str, steps: list, premise: str = "", reward: str = "",
              giver: Optional[str] = None, state: str = "draft") -> dict:
    """Write a quest and its ordered steps.

    steps is a list of {text, done_when, optional?}. done_when is MANDATORY:
    the observable that closes the step ("the signed form is in the
    inventory"). If EVERY step is optional the quest can never finish and the
    verdict says so. giver is a lore entity slug or name; a giver naming no
    entity is refused - omit it for a quest from the world. Steps go in with
    the quest in ONE call; a quest with no steps is refused. The returned row
    carries `ok` and `problems`.
    Full notes: docs/tools.md#quest_add
    """
    out = _quests.add(_root(), title, steps=steps, premise=premise,
                      reward=reward, giver=giver, state=state)
    _log("narrative", f"quest {out['title'][:60]!r}", ref=f"quest:{out['id']}")
    return out


@_tool
def quest_step_add(quest: str, text: str, done_when: str,
                   optional: bool = False) -> dict:
    """Append one step to an existing quest. Order continues the sequence."""
    return _quests.add_step(_root(), quest, text, done_when,
                            optional=optional)


@_tool
def quest_step_cut(step_id: int) -> dict:
    """Remove a step and close the gap in the numbering.

    Renumbering is the point: `ord` is what "step 3" means, and a sequence with
    a hole makes the panel, the agent and whoever implements the quest disagree
    about which step that is.
    """
    return _quests.cut_step(_root(), step_id)


@_tool
def quest_update(quest: str, premise: Optional[str] = None,
                 reward: Optional[str] = None, state: Optional[str] = None,
                 giver: Optional[str] = None) -> dict:
    """Change a quest's own fields. state: draft | active | done | cut.

    There is no delete here on purpose. 'cut' keeps the row - "we are not
    shipping this, and here is what it was" is the most useful thing the next
    person to propose it can read, exactly as with a superseded decision.
    """
    return _quests.update(_root(), quest, premise=premise, reward=reward,
                          state=state, giver=giver)


@_tool
def quest_list(state: Optional[str] = None) -> dict:
    """Every quest, with its steps and its verdict. READ BEFORE WRITING ONE.

    The verdict travels with the listing so a broken quest is visible without
    opening it - a rail of eight titles that makes you open each to find the two
    that do not hold together gets read once.

    state: draft | active | done | cut. No state returns all of them.
    """
    out = _quests.brief(_root(), state or "")
    out["count"] = len(out["quests"])
    return out


@_tool
def quest_read(quest: str) -> dict:
    """One quest, whole: fields, giver resolved, steps in order, verdict."""
    return _quests.get(_root(), quest)


# ---------------------------------------------------------------------------
# Voice - Deepgram speech, the half of it that has two doors
# ---------------------------------------------------------------------------
# MCP PARITY, AND WHERE IT DELIBERATELY STOPS. The dashboard's voice surface is
# three endpoints (bgate_ui/routes/voice.py): a status read, a TTS POST, and a
# websocket that relays a live microphone. The first two are ordinary
# request/response capabilities and they get tools here, because a capability
# behind one front door is a capability half the system lacks.
#
# THE LISTEN SOCKET GETS NO TOOL, and this is the stated reason rather than an
# omission. It is not a request/response at all: it is a duplex stream whose
# input is a microphone that only exists in front of the browser and whose value
# is entirely in the INTERIM results it emits while a human is still speaking.
# An MCP tool has one call and one return; the only thing it could offer is
# "transcribe this file", which is a different capability that this product
# already has locally in bgate_adapters/transcribe.py and does not need a
# metered hosted copy of. A tool that wrapped the socket would be a worse
# version of a thing we have, wearing the name of a thing we could not offer.
@_tool
def voice_status() -> dict:
    """Can this project do speech in and out, and if not, what is missing?

    Presence and reasons only - never the key. Two independent things can be
    absent (DEEPGRAM_API_KEY, and the `websockets` extra) and they need
    different actions from the human, so both are reported separately.
    """
    from bgate_adapters import deepgram as _deepgram
    verdict = dict(_deepgram.available(_root()))
    verdict["speak_models"] = list(_deepgram.SPEAK_MODELS)
    verdict["listen_models"] = list(_deepgram.LISTEN_MODELS)
    verdict["max_speak_chars"] = _deepgram.MAX_SPEAK_CHARS
    return verdict


@_tool
def voice_speak(text: str, out_path: str = "",
                model: str = "") -> dict:
    """Say `text` out loud with Deepgram Aura and WRITE THE WAV to the project.

    The dashboard's twin of this streams the bytes straight to an <audio> tag;
    an MCP caller has no speaker, so this one lands a file and returns its path.
    Same adapter, same 2000-character cap.

    out_path   project-relative .wav path. Default: a timestamped file under
               .bgate/voice/, which is inside the already-gitignored .bgate dir
               so a generated read-aloud never turns up in the game's history.
    model      an Aura voice; default aura-2-thalia-en. voice_status lists them.
    """
    _contained_path(out_path, "out_path")
    import time as _time
    from pathlib import Path as _Path

    from bgate_adapters import deepgram as _deepgram

    root = _root()
    verdict = _deepgram.available(root)
    if not verdict["available"]:
        raise RuntimeError(verdict["reason"])

    # Explicit ask, then the stored preference, then the adapter default.
    speak_model = str(model or "").strip()
    if not speak_model:
        try:
            from bgate_core.store import settings as _settings_mod

            speak_model = str(_settings_mod.get(root, "voice.model")
                              or "").strip()
        except Exception:
            speak_model = ""
    speak_model = speak_model or str(_deepgram.DEFAULT_SPEAK_MODEL)
    result = _deepgram.speak(str(text), model=speak_model)
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error") or "speech failed"))

    rel = str(out_path or f".bgate/voice/speak-{int(_time.time())}.wav")
    target = (_Path(root) / rel).resolve()
    # The same refusal deps.safe_under makes on the HTTP side: a path that
    # leaves the project is refused before anything is written, not after.
    # relative_to, NOT startswith: a prefix compare passes
    # C:\proj-evil\x.wav for a root of C:\proj, which is an escape into
    # any sibling directory sharing the root's name prefix.
    try:
        target.relative_to(_Path(root).resolve())
    except ValueError:
        raise ValueError(f"{rel} escapes the project root")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(result["audio"])

    return {"ok": True, "path": rel, "bytes": len(result["audio"]),
            "chars": result.get("chars"), "usd": result.get("usd"),
            "model": result.get("model"),
            "request_id": result.get("request_id")}


# ---------------------------------------------------------------------------
# SFX - synthesized, not generated
# ---------------------------------------------------------------------------
# THE AUDIO SEAT COULD NOT MAKE A GAME SOUND. Its mission opens "Own SFX and
# music hooks" and every tool it had was a paid, keyed provider - music_*
# for beds, voice_* for speech - so a project without a key produced no audio at
# all, and even with one there was no path to a coin pickup or a laser. These
# three tools need no key, no network and no money: an SFX is four oscillator
# parameters and an envelope, and synthesis genuinely beats generation there.
@_tool
def sfx_kinds() -> dict:
    """What game sounds this can synthesize, and the aliases each answers to.

    Read this before guessing a kind - `sfx_generate("pew")` fails, `"laser"`
    and its alias `"shoot"` do not. Every entry says how long it comes out and
    what its base frequency is, which are the two knobs worth moving first.
    """
    from bgate_core.audio import sfx as _sfx

    return {"kinds": _sfx.kinds(), "sample_rate": _sfx.DEFAULT_RATE,
            "max_seconds": _sfx.MAX_SECONDS}


@_tool
def sfx_generate(kind: Annotated[str, Field(description='Preset from sfx_kinds(): blip, pickup, jump, laser, explosion, hit, powerup, sweep, or an alias (coin, shoot, thud).')], name: Annotated[str, Field(description="Becomes <name>.wav (and <name>.synth.json) in the audio seat's lane.")], seed: Annotated[Optional[int], Field(description='Noise seed; omitted, derived from kind+name so the same call gives the same bytes.')] = None,
                 base_hz: Annotated[float, Field(description="Scales every pitch in the preset; 0 keeps the preset's own.")] = 0.0, duration_s: Annotated[float, Field(description='Scales every time in the preset; 0 keeps its nominal length.')] = 0.0,
                 gain: Annotated[float, Field(description='Output amplitude multiplier. Default 1.0.')] = 1.0, sample_rate: Annotated[int, Field(description='Output sample rate in Hz. Default 44100.')] = 44100,
                 bits: Annotated[int, Field(description='3-8 bit-crushes for the retro sound; 0 leaves it clean.')] = 0, out_dir: Annotated[str, Field(description='Override the output directory; empty writes into the audio lane.')] = "") -> dict:
    """Synthesize a game sound effect into the project. No key, no provider.

    kind is one of sfx_kinds() (blip, pickup, jump, laser, explosion, hit,
    powerup, sweep, plus aliases). name becomes <name>.wav in the audio lane.
    base_hz scales every pitch, duration_s every time (0 keeps the preset's
    own); bits 3-8 bit-crushes, 0 leaves it clean. WRITES TWO FILES:
    `<name>.synth.json` beside the wav carries the full recipe, which
    sfx_rerender rebuilds byte-identically. Deterministic per kind+name; pass
    a seed for a different roll.
    Full notes: docs/tools.md#sfx_generate
    """
    _contained_path(out_dir, "out_dir")
    from bgate_core.audio import sfx as _sfx

    root = _root()
    result = _sfx.generate(root, kind, name, out_dir=out_dir or None,
                           seed=seed, base_hz=base_hz,
                           duration_s=duration_s, gain=gain,
                           sample_rate=sample_rate, bits=bits)
    artifact = _register_artifact(
        f"sfx_{result['name']}", result["path"], producer="sfx_generate",
        model="procedural",
        prompt=f"{result['kind']} sfx",
        metadata={"kind": result["kind"], "seed": result["seed"],
                  "seconds": result["seconds"],
                  "recipe": result["recipe_rel_path"]})
    if artifact:
        result["artifact"] = artifact
    _log("audio", f"synthesized {result['kind']} sfx {result['name']} "
                  f"({result['seconds']}s)", ref=result["rel_path"])
    result.pop("recipe", None)     # the sidecar holds it; don't echo it twice
    return {"ok": True, **result}


@_tool
def sfx_rerender(recipe_path: str, out_path: str = "") -> dict:
    """Rebuild a .wav from its `<name>.synth.json` recipe ALONE.

    This is the proof the recipe is a recipe: `identical` says whether the bytes
    match the wav already on disk. A sidecar that renders something else is
    worse than no sidecar - it looks like provenance and is not - so run this
    after hand-editing a recipe, and read that field rather than assuming.

    recipe_path may be absolute or relative to the project root.
    """
    _contained_path(out_path, "out_path")
    from bgate_core.audio import sfx as _sfx

    root = _root()
    path = _Path(recipe_path)
    if not path.is_absolute():
        path = _Path(root) / recipe_path
    result = _sfx.rerender(path, out_path=out_path or None)
    _log("audio", f"re-rendered {result['name']} from its recipe "
                  f"(identical={result['identical']})", ref=result["path"])
    result.pop("recipe", None)
    return {"ok": True, **result}


@_tool
def sfx_list() -> dict:
    """Every synthesized effect in the project, and which ones lost their recipe.

    A wav with no `.synth.json` is listed with has_recipe=false rather than
    hidden: it is exactly the dead end the audio house rule exists to prevent,
    and the seat can only fix what it can see.
    """
    from bgate_core.audio import sfx as _sfx

    root = _root()
    found = _sfx.list_sfx(root)
    return {"dir": str(_sfx.sfx_dir(root)), "count": len(found),
            "sfx": found,
            "without_recipe": [f["name"] for f in found
                               if not f["has_recipe"]]}


# ---------------------------------------------------------------------------
# Dialogue - the narrative seat's own artifact
# ---------------------------------------------------------------------------
@_tool
def dialogue_write(name: str, nodes: list[dict], start: str = "",
                   title: str = "", summary: str = "",
                   allow_canon_conflict: bool = False) -> dict:
    """Author a dialogue tree as an engine-loadable resource, validated first.

    nodes: [{id, speaker, text, choices: [{text, goto}], end}]; `end: true`
    must have no choices; start defaults to the first node. THE WRITE IS
    REFUSED, NOT WARNED, when the graph is broken (a choice to a missing node,
    an unreachable node, a node from which no ending is reachable), naming the
    node. canon_check runs on lines and choice labels: a hard conflict refuses
    unless allow_canon_conflict=True. Lands at <godot
    project>/dialogue/<name>.dialogue.json.
    Full notes: docs/tools.md#dialogue_write
    """
    from bgate_core.design import dialogue as _dialogue

    result = _dialogue.write(_root(), name, nodes, start=start, title=title,
                             summary=summary,
                             allow_canon_conflict=bool(allow_canon_conflict))
    _log("narrative", f"wrote dialogue {result['name']} "
                      f"({result['nodes']} nodes, {result['choices']} choices, "
                      f"canon {result['canon']['verdict']})",
         ref=result["rel_path"])
    return {"ok": True, **result}


@_tool
def dialogue_read(name: str) -> dict:
    """One dialogue tree, whole - nodes, choices, start and ends.

    Read before editing: dialogue_write replaces the file outright, so a partial
    node list silently deletes every branch it left out.
    """
    from bgate_core.design import dialogue as _dialogue

    return {"ok": True, **_dialogue.read(_root(), name)}


@_tool
def dialogue_list() -> dict:
    """Every dialogue tree in the project, and whether each still validates.

    A file that no longer passes the graph checks is reported with ok=false and
    the reason - hand edits and merges break `goto` targets, and the listing is
    the cheapest place to find that out.
    """
    from bgate_core.design import dialogue as _dialogue

    root = _root()
    found = _dialogue.list_dialogues(root)
    return {"dir": str(_dialogue.dialogue_dir(root)), "count": len(found),
            "dialogues": found,
            "broken": [d["name"] for d in found if not d["ok"]]}


# ---------------------------------------------------------------------------
# Work queue
# ---------------------------------------------------------------------------
@_tool
def queue_list(status: Optional[str] = None, seat: Optional[str] = None,
               limit: int = 40, full: bool = False,
               order: str = "id") -> dict:
    """The work queue. status: queued | dispatched | done | failed.

    WORK-ITEM IDs ARE CREATION IDENTIFIERS, NOT EXECUTION ORDER, and are never
    renumbered. order="id" (default) is creation order; order="execution" is
    topological, with `execution_position` and `execution_state` (ready |
    running | waiting | blocked | held | done | failed) per row. Queued rows
    carry `waiting_on`, naming the blocker. BRIEFS ARE PREVIEWS and the list
    is PAGED: get one brief with queue_get(item_id); pass full=True only when
    you need several, with a small limit.
    Full notes: docs/tools.md#queue_list
    """
    from bgate_core.board import queue as _q
    root = _root()
    rows = _q.list_items(root, status=status, seat=seat)
    cap = max(1, min(int(limit or 40), 200))
    shown = rows[:cap]
    items = []
    for row in shown:
        item = dict(row)
        brief = item.get("brief") or ""
        result = item.get("result") or ""
        if not full:
            item["brief"] = brief[:240]
            item["brief_len"] = len(brief)
            item["result"] = result[:240]
        # WHY IS THIS NOT RUNNING - answered on the row, because a queued
        # item that nothing will ever dispatch used to look exactly like
        # one that is next in line, and the difference lived in three
        # different modules. Best-effort: an unreadable blocker is not a
        # reason to fail the whole listing.
        if item.get("status") == "queued":
            stage_hold = ""
            try:
                from bgate_core.design import greenlight as _gl

                stage_hold = _gl.allows(root, str(item.get("seat") or ""))[1]
            except Exception:
                stage_hold = ""
            if stage_hold:
                # The production stage outranks the other reasons on the row:
                # a seat the stage is holding will not dispatch whatever its
                # dependencies say, and reporting the dependency instead sends
                # the reader off to fix the wrong thing.
                item["waiting_on"] = stage_hold
            elif str(item.get("source") or "") in _q.HELD_SOURCES:
                item["waiting_on"] = (
                    f"held: source {item.get('source')!r} is never "
                    "auto-dispatched - a human (or the director session) "
                    "must take it")
            else:
                try:
                    blk = _q.blocker(root, int(item["id"]))
                except Exception:
                    blk = None
                if blk is not None:
                    # THE TITLE, NOT JUST THE ID. "#43 QUEUED" made a correct
                    # insertion look like a skipped item; an id alone sends the
                    # reader off to look it up, and the whole defect is that
                    # people were not looking things up.
                    also = blk.get("also_waiting_on") or []
                    item["waiting_on"] = (
                        f"WAITING ON #{blk['id']} {blk['title']} "
                        f"({blk['status']})"
                        + (f" and {len(also)} more" if also else "")
                        + ("" if blk["status"] in ("queued", "dispatched",
                                                   "review")
                           else " - that predecessor will never reach "
                                "'done' on its own; queue_reopen it or "
                                "queue_cut_dependency to release this"))
                    item["waiting_on_id"] = int(blk["id"])
            # THE HARNESS STOPPED BUYING ROUNDS FOR THIS ONE. Previously
            # indistinguishable from fresh work: same status, same row, and the
            # only tell was reading two counters and comparing them to a
            # setting by hand.
            if item.get("exhausted_at"):
                item["waiting_on"] = (
                    "EXHAUSTED - the harness stopped retrying this: "
                    + str(item.get("exhausted_why") or "")
                    + " It is not claimable; queue_reopen it (with a changed "
                      "brief or a fixed blocker) to start it again.")
        items.append(item)
    out = {
        "items": items,
        "shown": len(items),
        "total": len(rows),
        "truncated": len(rows) > len(items),
        "order": order,
        "note": ("briefs are previews - queue_get(item_id) returns one item "
                 "whole" if not full else "full briefs; keep limit small"),
    }
    if str(order).lower() == "execution":
        try:
            graph = _q.graph(root)
        except Exception as exc:                                  # noqa: BLE001
            out["order_error"] = f"{type(exc).__name__}: {exc}"
            return out
        rank = {n["id"]: n for n in graph["nodes"]}
        for item in items:
            node = rank.get(int(item["id"]))
            if node:
                item["execution_position"] = node["execution_position"]
                item["execution_state"] = node["execution_state"]
                item["unblocks"] = node["unblocks"]
                item["depends_on_all"] = node["depends_on"]
                if node["waiting_line"] and not item.get("waiting_on"):
                    item["waiting_on"] = node["waiting_line"]
        items.sort(key=lambda r: r.get("execution_position", 10 ** 6))
        out["items"] = items
        out["execution_path"] = [
            {"id": n["id"], "title": n["title"],
             "state": n["execution_state"], "waits_on": n["unresolved"]}
            for n in graph["nodes"]]
        out["cycles"] = graph["cycles"]
        out["note"] += (". `order=execution` is a DISPLAY order derived from "
                        "the dependency graph - ids are creation identifiers "
                        "and are never renumbered. work_item.depends_on and "
                        "work_item_dep are presented as ONE graph; which table "
                        "holds a link is not a question you should have to "
                        "answer.")
    return out


@_tool
def queue_get(item_id: int) -> dict:
    """One work item, whole - brief, result, lineage, cost, status.

    The other half of queue_list's preview: scan the board with the list, read
    the one item you are about to act on with this.
    """
    from bgate_core.board import queue as _q
    return _q.get(_root(), int(item_id))


@_tool
def worktree_integrations() -> dict:
    """Chaos-mode branches waiting for Director review and integration."""
    from bgate_core.board import gitwork as _gitwork
    from bgate_core.board import queue as _q

    root = _root()
    rows = []
    for integration in _gitwork.integrations(root, pending=True):
        try:
            item = _q.get(root, int(integration["item_id"]))
        except (LookupError, ValueError, TypeError):
            item = {}
        rows.append({**integration,
                     "seat": item.get("seat") or "",
                     "title": item.get("title") or "",
                     "result": item.get("result") or ""})
    return {"pending": rows, "count": len(rows)}


@_tool
def worktree_merge(item_id: int) -> dict:
    """Merge one prepared Chaos worktree into the current working branch.

    Read the item's diff and result first. A conflict is aborted before this
    returns, leaving the working branch clean for diagnosis and a deliberate
    retry after the branch is corrected.
    """
    from bgate_core.board import gitwork as _gitwork
    from bgate_core.board import queue as _q

    root = _root()
    item = _q.get(root, int(item_id))
    if _seat() not in ("", "director"):
        return {"ok": False, "error": "only the Director may merge Chaos worktrees"}
    if item.get("status") not in ("integrating", "done", "review"):
        return {"ok": False, "error": f"item {int(item_id)} is "
                f"{item.get('status')}, not finished"}
    pending = _gitwork.integration(root, int(item_id))
    if not pending.get("pending"):
        return {"ok": False, "error": f"item {int(item_id)} has no pending "
                "Chaos integration", "integration": pending}
    result = _gitwork.merge_worktree(root, int(item_id))
    if not result.get("integrated"):
        if result.get("failed") and item.get("status") == "integrating":
            _q.complete(root, int(item_id), failed=True,
                        result="Chaos integration abandoned: "
                               + str(result.get("reason") or "merge failed"))
            result["item_status"] = "failed"
        return {"ok": False, "error": result.get("reason") or "merge failed",
                **result}
    if item.get("status") == "integrating":
        completed = _q.complete(root, int(item_id), result=item.get("result") or "")
        result["item_status"] = completed.get("status")
    _log("director", f"merged Chaos worktree for item {int(item_id)}",
         ref=str(item_id))
    return {"ok": True, **result}



def _near_duplicate(_q, title: str, seat: str) -> Optional[dict]:
    """The open item whose title shares most of its words with `title`, if any.

    Token Jaccard over words of 4+ letters, threshold 0.5: cheap, and it is the
    shape every measured duplicate had - the same nouns in a different order
    ('Hero car mesh ... split Body + 4 wheels' twice, 'Swap art materials ...'
    twice, three re-pins of one test file).
    """
    def toks(t: str) -> set:
        return {w for w in _re.findall(r"[a-z0-9_]+", (t or "").lower()) if len(w) >= 4}
    mine = toks(title)
    if len(mine) < 3:
        return None
    best, best_j = None, 0.0
    for r in _q.list_items(_root()):
        if r.get("status") not in ("queued", "dispatched", "review"):
            continue
        theirs = toks(r.get("title") or "")
        if not theirs:
            continue
        j = len(mine & theirs) / float(len(mine | theirs))
        if j > best_j:
            best, best_j = r, j
    return best if best_j >= 0.5 else None


@_tool
def queue_add(seat: str, title: str, brief: str = "", priority: int = 0,
              depends_on: Optional[int] = None) -> dict:
    """Queue work for a seat. Use when your work uncovers work that isn't yours.

    ``depends_on`` is an EXISTING item id this work must not start before;
    pass it whenever the new item reads or edits something another queued
    item is about to produce. PRIORITY IS NOT ORDER: it is a preference among
    things that are all ready and does not stop two agents starting in the
    same tick - only a dependency does. Use queue_add_chain when filing a
    whole ordered group; use this to hang a follow-up off work already on the
    board. A dependency on a missing item is refused.
    Full notes: docs/tools.md#queue_add
    """
    from bgate_core.board import queue as _q
    # A SPAWNED AGENT FILES AT MOST TWO ITEMS, AND NEVER A DUPLICATE. MEASURED
    # (Corniche, 2026-09-04): 11 of 16 QA items and 6 duplicates of work the
    # director had already queued were filed agent-to-agent - re-pins, re-checks,
    # audits of green work - and every one dispatched a full run. The director
    # is the one participant who sees the whole board; an agent that thinks more
    # work exists says so in its RESULT NOTE and the director files it.
    own_item = (os.environ.get("BGATE_WORK_ITEM") or "").strip()
    if own_item:
        filed = [r for r in _q.list_items(_root())
                 if str(r.get("source_ref") or "") == own_item]
        if len(filed) >= 2:
            return {"ok": False, "refused": "filing_cap",
                    "error": f"item {own_item} has already filed {len(filed)} item(s) "
                             "- the cap for a spawned agent is two. Put the rest in "
                             "your queue_complete result note; the director files "
                             "what the board actually needs.",
                    "already_filed": [r["id"] for r in filed]}
    dup = _near_duplicate(_q, title, seat)
    if dup is not None:
        return {"ok": False, "refused": "duplicate",
                "error": f"an open item with the same work already exists: #{dup['id']} "
                         f"[{dup['seat']}] {dup['title'][:90]} - steer it or let it "
                         "run instead of filing a twin",
                "existing": dup["id"]}
    item = _q.add(_root(), seat, title, brief=brief, priority=priority,
                  source=f"seat:{_seat() or 'unknown'}",
                  source_ref=own_item,
                  depends_on=depends_on)
    if depends_on is None:
        return item
    # SAY WHAT THE BOARD WILL DO WITH IT. A caller that files a dependency
    # and sees an ordinary queued row has no way to tell the wait took, and
    # the difference only becomes visible when the item does (or does not)
    # start.
    try:
        blocker = _q.get(_root(), int(depends_on))
        waiting = blocker["status"] != "done"
    except Exception:
        blocker, waiting = {}, True
    return {**item, "waits_for": {
        "item": int(depends_on),
        "title": str(blocker.get("title") or "")[:120],
        "status": blocker.get("status") or "",
        "note": (f"#{item['id']} will not dispatch until #{depends_on} is "
                 "done" + (" - approved, if this project runs an approval "
                           "gate" if waiting else "")
                 if waiting else
                 f"#{depends_on} is already done, so #{item['id']} is "
                 "ready now")}}


@_tool
def queue_add_chain(links: list, chain_id: str = "") -> dict:
    """File DEPENDENT work as one ordered chain instead of N loose items.

    USE THIS WHENEVER THE SPLIT YOU JUST MADE HAS AN ORDER. ``links`` is an
    ORDERED list of dicts taking queue_add's fields {seat, title, brief,
    priority}; link N waits for link N-1 to reach 'done'. Chains are strictly
    linear and CANNOT BE APPENDED TO - hang a later follow-up off the board
    with queue_add(depends_on=...). Write each brief as if its predecessor
    already landed. Returns {chain_id, items} in running order.
    Full notes: docs/tools.md#queue_add_chain
    """
    from bgate_core.board import queue as _q
    rows = _q.add_chain(
        _root(),
        [dict(link) for link in (links or [])],
        chain_id=chain_id, source=f"seat:{_seat() or 'unknown'}")
    return {"chain_id": rows[0]["chain_id"], "count": len(rows),
            "items": [{"id": r["id"], "seat": r["seat"], "title": r["title"],
                       "chain_pos": r["chain_pos"],
                       "depends_on": r["depends_on"]} for r in rows]}


@_tool
def queue_update(item_id: int, title: Optional[str] = None, brief: Optional[str] = None,
                 seat: Optional[str] = None, priority: Optional[int] = None,
                 steer_running: bool = False) -> dict:
    """Edit an existing work item in place (title/brief/seat/priority).

    Only the fields you pass change; brief REPLACES, it does not append. THIS
    DOES NOT REACH A RUNNING AGENT: a brief change on a DISPATCHED item is
    refused with steer_running=False (default, naming the agent_steer call to
    make instead); steer_running=True updates the row AND delivers the change
    as a steer. Every result carries `live_delivered`.
    Full notes: docs/tools.md#queue_update
    """
    from bgate_core.board import queue as _q, steerbox as _steerbox

    root = _root()
    item = _q.get(root, int(item_id))
    running = str(item.get("status")) == "dispatched"
    changes_the_work = brief is not None or seat is not None

    if running and changes_the_work and not steer_running:
        return {
            "ok": False, "live_delivered": False, "status": item["status"],
            "error": (f"#{item_id} is RUNNING. Editing its brief changes what "
                      "the next reader sees and NOTHING about what the agent "
                      "is doing - it would look like a mid-run correction and "
                      "silently not be one. Either agent_steer(item_id, text) "
                      "to reach the live agent, or call this again with "
                      "steer_running=True to do both."),
            "instead": "agent_steer",
        }

    updated = _q.update(root, item_id, title=title, brief=brief,
                        seat=seat, priority=priority)
    delivered = False
    steer: dict = {}
    if running and changes_the_work and steer_running:
        summary = (str(brief or "")[:900] if brief is not None
                   else f"this item has been reassigned to the {seat} seat")
        steer = _steerbox.post_long(
            root, int(item_id),
            "YOUR BRIEF HAS BEEN CHANGED MID-RUN. The row now reads as "
            "follows; work to this, not to what you were spawned with.\n\n"
            + summary, by=_actor(), note="queue_update")
        delivered = True
    return {**updated, "live_delivered": delivered,
            "steer": {k: steer.get(k) for k in ("id", "note_path")} if steer else {},
            "why": ("delivered to the running agent as a steer, and recorded "
                    "in the item's steer history" if delivered else
                    "the item is not running, so there was nobody to deliver "
                    "to - the next reader gets the new text"
                    if not running else
                    "only title/priority changed; the agent's instructions are "
                    "unaffected")}


@_tool
def queue_next(seat: str) -> dict:
    """The highest-priority queued item for a seat - what to work on next.

    A READ: it changes nothing and reserves nothing. A dispatched worker that
    wants to actually take the item it sees here uses queue_claim_next, which
    claims atomically - acting on this read alone races the dashboard.
    """
    from bgate_core.board import queue as _q
    item = _q.next_for(_root(), seat)
    return item if item else {"empty": True, "seat": seat}


@_tool
def board_digest(hours: int = 12) -> dict:
    """WHAT HAPPENED WHILE YOU WERE AWAY - finished, failed, blocked, spent.

    The morning report. Read ``blocked`` first: queued work with nothing
    running is a dead dashboard or a floor refusal (most often a dirty tree),
    and this names which. ``stage`` says which SEATS the production stage
    holds. ``restart_cost`` is what killing the MCP server now would orphan -
    READ IT BEFORE RESTARTING; ``orphaned`` is what a previous one already
    did. Spends nothing.
    Full notes: docs/tools.md#board_digest
    """
    from bgate_core.design import gameplan as _gameplan

    root = _root()
    out = _gameplan.digest(root, hours=int(hours))
    try:
        from bgate_core.design import greenlight as _gl

        state = _gl.state(root)
        out["stage"] = {"stage": state["stage"], "label": state["label"],
                        "held_seats": state["held_seats"],
                        "blockers": state["blockers"]}
    except Exception:                                             # noqa: BLE001
        pass
    # IS THE AGENT YOU ARE WATCHING RUNNING THE CODE YOU ARE READING? Every
    # run stamps the harness fingerprint it spawned against; Python caches
    # modules per process, so a fix landing mid-run reaches the NEXT agent and
    # not this one. drift() existed and nothing called it - the answer to "my
    # fix did nothing" was computable and never computed.
    try:
        from bgate_core.store import harness as _harness

        live = _harness.recently_edited(within_s=120.0)
        if live:
            out["harness_editing"] = {
                "files": live[:6],
                "why": ("the harness source was written in the last two "
                        "minutes. Agents already running still execute the "
                        "old copy; agents spawned mid-save can import a "
                        "half-written module. See bgate_core.store.harness."),
            }
    except Exception:                                             # noqa: BLE001
        pass
    try:
        from bgate_core.board import inflight as _inflight

        warning = _inflight.restart_warning(root)
        if warning:
            out["restart_cost"] = warning
        lost = _inflight.orphaned(root)
        if lost:
            out["orphaned"] = lost[:10]
    except Exception:                                             # noqa: BLE001
        pass
    return out


@_tool
def queue_cut_dependency(item_id: int, depends_on: int) -> dict:
    """Release an item from a predecessor that will never land. THE REPAIR VERB.

    Only 'done' satisfies a dependency, so a CANCELLED predecessor blocks its
    successors forever. Use this when the predecessor was cut, superseded or
    unnecessary - NOT to jump a queue: the released item starts writing
    against whatever the predecessor was meant to produce. The cut is recorded
    with your identity. queue_get shows what an item waits on.
    Full notes: docs/tools.md#queue_cut_dependency
    """
    from bgate_core.board import queue as _q
    return _q.cut_dependency(_root(), int(item_id), int(depends_on),
                             by=_actor())


@_tool
def queue_add_dependency(item_id: int, depends_on: int) -> dict:
    """Make an item wait for one MORE predecessor - dependencies are a graph.

    queue_add's own depends_on takes a single parent, which is all a chain
    ever needed. A real feature is a fan-in: the scene needs the sprite AND
    the sound AND the script. Call this for each extra parent; the item does
    not dispatch until every one of them reaches 'done'.
    """
    from bgate_core.board import queue as _q
    return _q.add_dependency(_root(), int(item_id), int(depends_on))


@_tool
def plan_status() -> dict:
    """Coverage: what the game consists of vs what is built - THE completeness
    read.

    An empty queue is NOT a finished game. Reads the game-plan manifest
    (written when a human deploys a brainstorm plan) joined live against the
    board: spec / on_board / built / lost per row, slice completeness, and
    `remaining` - the rows the board does not hold. The director reads it
    before declaring anything finished and queue_adds uncovered rows.
    Full notes: docs/tools.md#plan_status
    """
    from bgate_core.design import gameplan as _gameplan
    return _gameplan.status(_root())


# ── the production stage ────────────────────────────────────────────────────
# A project does not go from a premise straight to a specialist fan-out. See
# bgate_core/design/greenlight.py for the four stages and what each one holds.

@_tool
def greenlight_status(section: str = "") -> dict:
    """WHAT STAGE IS THIS PROJECT AT, and what is it holding.

    THE FIRST TOOL TO CALL WHEN A QUEUED ITEM WILL NOT DISPATCH: whole SEATS
    (art, audio, cinematic) are held until gameplay proves the core loop in a
    graybox and the director rules on it, and a held item looks blocked from
    the board. `section`: '' (stage, thesis, graybox, held seats), 'encounter',
    'scale', 'rooms', 'presentation' (what a release candidate still owes;
    takes no waiver).
    Full notes: docs/tools.md#greenlight_status
    """
    from bgate_core.design import greenlight as _gl

    root = _root()
    section = (section or "").strip().lower()
    if section == "encounter":
        from bgate_core.level import encounter as _enc
        return _enc.state(root)
    if section == "scale":
        from bgate_core.three_d import scalecontract as _scale
        return _scale.state(root)
    if section == "rooms":
        from bgate_core.level import roomqa as _roomqa
        return _roomqa.state(root)
    if section == "presentation":
        return _gl.presentation_check(root)
    if section == "findings":
        from bgate_core.board import findings as _findings
        return {"standing": _findings.standing(root),
                "all": _findings.ledger(root),
                "supersessions": _findings.supersessions(root),
                "note": "every finding carries the tool, inputs and "
                        "measurement behind it. greenlight_supersede retracts "
                        "one that a better measurement has disproved; it stops "
                        "blocking and stays visible."}
    if section == "default_scene":
        from bgate_core.level import sceneproof as _proof
        return _proof.state(root)
    if section:
        raise ValueError(
            "section is '', 'encounter', 'scale', 'rooms', 'presentation', "
            "'findings' or 'default_scene'")
    return _gl.state(root)


@_tool
def greenlight_supersede(finding_id: str, why: str, measurement: str = "",
                         tool: str = "") -> dict:
    """RETRACT a gate finding that a better measurement has disproved.

    A later, AUTHORITATIVE measurement supersedes an earlier one: the finding
    stops blocking and stays readable, carrying what replaced it. `why` must
    say what was measured instead and why it outranks the finding -
    "superseded" with no antecedent is the same erasure as a delete.
    greenlight_status(section='findings') lists findings with ids.
    Full notes: docs/tools.md#greenlight_supersede
    """
    from bgate_core.board import findings as _findings

    try:
        return _findings.supersede(
            _root(), str(finding_id), why=why, tool=tool or "greenlight_supersede",
            measured={"measurement": str(measurement)[:600]} if measurement else {},
            by=_actor())
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@_tool
def evidence_assert(scene: str, frame: str, says: str,
                    views: Optional[list[str]] = None,
                    subject: str = "") -> dict:
    """Record what you SAW in a captured frame. CAPTURING IS NOT EXAMINING.

    `says` is prose - no static check reads a picture; what makes it evidence
    is the file, its DIGEST at the moment of the claim, who said it and when.
    Regenerate the frame and the claim is reported stale. `views` names which
    of front/back/left/right this covers; `subject` groups views of one
    character. Refuses a sentence short enough to be a shrug - "looks fine" is
    what the two-tailed cat shipped under.
    Full notes: docs/tools.md#evidence_assert
    """
    from bgate_core.level import sceneproof as _proof

    try:
        row = _proof.assert_content(_root(), scene, frame=frame, says=says,
                                    by=_actor(), views=views, subject=subject)
    except (ValueError, FileNotFoundError) as exc:
        return {"ok": False, "error": str(exc)}
    gaps = _proof.character_gaps(_root(), subject) if subject else []
    return {"ok": True, **row, "missing_views": gaps,
            "why": (f"{subject} has still not been described from: "
                    + ", ".join(gaps) if gaps else
                    "recorded against this frame's digest")}


@_tool
def greenlight_thesis_set(sentence: str, options: list, stakes: str,
                          tension: str, dominant_strategy: str,
                          cadence: str) -> dict:
    """Settle the MECHANICAL THESIS - the one sentence the game is built on.

    A feature list is refused: the sentence must name an act of choosing, with
    `options` (two or more), `stakes` (what the wrong pick costs), `tension`
    (why the answer is not the same every time - the field that matters),
    `dominant_strategy` (the play that would COLLAPSE the decision, named so
    QA can hunt it) and `cadence` (how often it comes round). Settling a
    thesis does not advance the stage - greenlight_advance does.
    Full notes: docs/tools.md#greenlight_thesis_set
    """
    from bgate_core.design import greenlight as _gl

    return _gl.set_thesis(_root(), {
        "sentence": sentence, "options": list(options or []),
        "stakes": stakes, "tension": tension,
        "dominant_strategy": dominant_strategy, "cadence": cadence},
        by=_actor())


@_tool
def greenlight_graybox_submit(scene: str, evidence: list,
                              notes: str = "") -> dict:
    """Say the core loop is PLAYABLE in one ugly test room, with proof.

    The gameplay seat's move at the graybox stage. `scene` must be a real file
    under the project and `evidence` must be something a person can look at —
    a playtest recording, a screenshot, telemetry from an actual run. The
    director is about to be asked whether the interaction is interesting and
    cannot answer that from a scene path.

    Untextured is the point. Do not wait for art; art is what this gate exists
    to hold back until the answer is yes.
    """
    from bgate_core.design import greenlight as _gl

    return _gl.graybox_submit(_root(), scene=scene,
                              evidence=list(evidence or []), notes=notes,
                              by=_actor())


@_tool
def greenlight_graybox_verdict(verdict: str, interesting: bool,
                               why: str) -> dict:
    """Rule on the graybox: is the interaction actually interesting?

    THE DIRECTOR'S CALL, and the one that decides whether a whole production
    run happens. Play it. If the loop reduces to attack + dodge + hold
    interact, fail it and say so — that is a cheap no now and an expensive one
    after the assets exist.

    `why` is required in both directions. A pass with no reason is the rubber
    stamp this gate exists to stop; a fail with no reason sends gameplay back
    with nothing to change.
    """
    from bgate_core.design import greenlight as _gl

    return _gl.graybox_verdict(_root(), verdict=verdict,
                               interesting=bool(interesting), why=why,
                               by=_actor())


@_tool
def greenlight_advance(stage: str) -> dict:
    """Move the project to the next production stage, or learn why it cannot.

    thesis -> graybox -> production -> release. Moving BACKWARD is always
    allowed. Forward: graybox needs a settled thesis; production needs a
    graybox the director passed plus an enemy roster of interactions and more
    than one objective shape; release needs the presentation gate (every room
    reviewed whole, every asset measured, every audio cue heard). THE RELEASE
    BOUNDARY TAKES NO WAIVER - greenlight_status('presentation') lists what is
    owed.
    Full notes: docs/tools.md#greenlight_advance
    """
    from bgate_core.design import greenlight as _gl

    return _gl.advance(_root(), stage, by=_actor())


@_tool
def greenlight_waive(seat: str, reason: str, withdraw: bool = False) -> dict:
    """Let ONE seat through the current stage hold, on the record.

    For the true case — a tech seat building the graybox's own tooling, an art
    seat making its placeholder blocks — not as the route around the gate. It
    costs a sentence naming what this seat has to do before the loop is proven
    and why it cannot wait, and the waiver shows in greenlight_status forever.

    `withdraw=True` takes it back. There is no waiver for the release gate.
    """
    from bgate_core.design import greenlight as _gl

    root = _root()
    if withdraw:
        return {"waivers": _gl.unwaive(root, seat)}
    return _gl.waive(root, seat, reason, by=_actor())


@_tool
def encounter_design_set(roster: Optional[list] = None,
                         objectives: Optional[list] = None) -> dict:
    """Declare enemies as INTERACTIONS and tasks as COMMITMENT SHAPES.

    roster rows are {name, pressure, alters:[{enemy, effect}], role?,
    counterplay?} - `alters` is the point: how THIS enemy changes the read of
    THAT one. objective rows are {name, shape, costs, notes?} with `shape`
    from a closed vocabulary (dwell, carry, escort, defend, route, timing,
    disarm, restrict, manipulate, spend, gather; greenlight_status('encounter')
    defines them). Both optional, but a roster of isolated machines or a task
    list that is one shape eight times blocks the move to production.
    Full notes: docs/tools.md#encounter_design_set
    """
    from bgate_core.level import encounter as _enc

    root, out = _root(), {}
    if roster is not None:
        out.update(_enc.set_roster(root, roster, by=_actor()))
    if objectives is not None:
        out.update(_enc.set_objectives(root, objectives, by=_actor()))
    if not out:
        return _enc.state(root)
    out["blockers"] = _enc.production_blockers(root)
    return out


@_tool
def scale_contract_set(player_height_px: Optional[int] = None,
                       tile_px: Optional[int] = None,
                       classes: Optional[dict] = None) -> dict:
    """Declare the REFERENCE SCALE every asset is measured against.

    `player_height_px` is the unit. `classes` overrides the default bands,
    multiples of that height: {"door": {"low": 1.05, "high": 1.5}}. The
    classes are prop, furniture, door, ui and enemy; every one has a band,
    because "no expectation" is how a mug ends up chair-sized.
    Full notes: docs/tools.md#scale_contract_set
    """
    from bgate_core.three_d import scalecontract as _scale

    return _scale.set_contract(_root(), player_height_px=player_height_px,
                               tile_px=tile_px, classes=classes, by=_actor())


@_tool
def scale_record_3d(path: str, klass: str, longest_axis_m: float,
                    height_m: float = 0.0,
                    player_height_m: float = 1.8) -> dict:
    """Record a 3D asset's ENGINE-MEASURED scale against the contract.

    The affirmative half of scale_check's 3D refusal. Pass the numbers the
    ENGINE gave you - godot_inspect_resource's size_check.longest_axis_m and
    the vertical extent as height_m - never a pixel count. Grades against the
    class band (players = metres / player_height_m), records under the key
    the release gate reads, and retracts the standing "no measurement"
    finding when it passes. A failing measurement blocks.
    Full notes: docs/tools.md#scale_record_3d
    """
    from bgate_core.three_d import scalecontract as _scale

    return _scale.record_3d(_root(), path, klass,
                            longest_axis_m=longest_axis_m, height_m=height_m,
                            player_height_m=player_height_m)


@_tool
def scale_check(path: str, klass: str, frames: int = 1) -> dict:
    """Measure one asset AT GAME SCALE and record the result on its revision.

    Measures the opaque bounding box — the box, not the canvas, because a
    512x512 sheet holding a 40px mug is a 40px mug — and divides by the
    declared player height. `klass` is prop, furniture, door, ui or enemy.
    `frames` divides the width for a horizontal strip so a 6-frame sheet is
    graded as one sprite rather than a six-player-wide prop.

    The result rides on the artifact revision, so a regenerated asset is
    unmeasured again. Every delivered image needs one before a release
    candidate will close.
    """
    from bgate_core.three_d import scalecontract as _scale

    return _scale.record(_root(), path, klass, frames=int(frames or 1))


@_tool
def room_review(scene: str, shot: str, verdict: str, notes: str,
                bounds: Optional[list] = None) -> dict:
    """Review a WHOLE ROOM against a full-room screenshot.

    A cropped shot is refused rather than accepted with a caveat. Alongside
    your judgement it MEASURES the scene tree: empty floor, perimeter hugging,
    prop scale spread, whether any region holds the eye, lanes between
    obstacles. A pass is refused while any measured finding stands - answer
    them with room_override or fail the room. `bounds` is [x0,y0,x1,y1] when
    the room is larger than what is placed in it.
    Full notes: docs/tools.md#room_review
    """
    from bgate_core.level import roomqa as _roomqa

    return _roomqa.review(_root(), scene, shot=shot, verdict=verdict,
                          notes=notes,
                          bounds=list(bounds) if bounds else None,
                          by=_actor())


@_tool
def room_override(scene: str, finding: str, reason: str) -> dict:
    """Accept ONE measured room finding, with a reason on the record.

    Per finding, never per room and never per project: "the thresholds do not
    suit this game" would switch the gate off for everything, and a gate that
    can be switched off wholesale is the one that was. Paste the finding text
    from room_review and say why this room is right and the measurement is
    wrong.
    """
    from bgate_core.level import roomqa as _roomqa

    return _roomqa.override(_root(), scene, finding, reason, by=_actor())


@_tool
def audio_listen_record(capture: str, cues: list, verdict: str,
                        notes: str) -> dict:
    """Record an IN-GAME listening pass — the audio check metrics cannot make.

    Peaks, RMS, wiring and duplicate detection all pass on a cue that is wrong
    for the moment it fires, buried under the music, or three frames late. The
    only thing that catches those is hearing them in context.

    `capture` is a gameplay recording that exists on disk (video, or a capture
    of the bus). `cues` are the event names you actually heard firing in it —
    that list is the coverage, and a release candidate does not close while a
    wired cue has never been heard.
    """
    from bgate_core.audio import audiohooks as _hooks

    return _hooks.listen_record(_root(), capture=capture,
                                cues=list(cues or []), verdict=verdict,
                                notes=notes, by=_actor())


@_tool
def queue_claim_next() -> dict:
    """Claim the next READY item for YOUR seat and keep this session working.

    THE PICKUP LOOP. ORDER MATTERS: CLAIM FIRST, THEN queue_complete - the
    dashboard closes this session shortly after your current item settles
    unless you already hold a claim. Atomic against the dispatcher, honours
    the same holds autodeploy does, and only claims for the seat you hold.
    Your original cost and runtime ceilings bound the whole session; if the
    claimed work will not fit, do not claim it. Returns the item or
    {empty: true} - then queue_complete and finish.
    Full notes: docs/tools.md#queue_claim_next
    """
    from bgate_core.board import queue as _q
    from bgate_core.store import settings as _settings
    seat = _seat()
    origin = _work_item_id()
    if not seat or not origin:
        raise PermissionError(
            "queue_claim_next is the dispatched worker's pickup loop, and "
            "this session was not dispatched against a work item. File "
            "work with queue_add (it dispatches when `bgate serve` is up) "
            "instead of claiming it.")
    if (os.environ.get("BGATE_DISPATCH_MODE") or
            str(_settings.get(_root(), "dispatch.mode") or "structured")) == "chaos":
        return {"empty": True, "seat": seat,
                "note": "Chaos mode gives every task its own worktree; finish "
                        "this item and let the scheduler start the next one."}
    item = _q.claim_next(_root(), seat, actor=f"agent:item-{int(origin)}")
    if item is None:
        return {"empty": True, "seat": seat,
                "note": "nothing ready for your seat - queue_complete "
                        "your current item and finish."}
    _log("queue", f"claimed #{item['id']} to continue after "
                  f"item {origin}", ref=str(item["id"]))
    return {**item, "claimed": True,
            "note": (f"item #{item['id']} is yours. queue_complete your "
                     f"current item (#{origin}) first, then work this one "
                     "under the same lanes and rules, and queue_complete "
                     "it too when it lands. Claim again before that "
                     "completion if you still have room for more.")}


@_tool
def queue_complete(item_id: int, result: str, failed: bool = False,
                   evidence: str = "", next_approach: str = "",
                   premise_refuted: Optional[dict] = None) -> dict:
    """Close out a work item with an honest one-paragraph result.

    failed=True when the work did not land. THE EYES GATE: a run that WROTE
    SCENES (.tscn) may not report 'done' without a render - either a
    godot_screenshot this run took, or `evidence` = the path to a render you
    judged; failed=True never needs evidence. Under the agent gate the item
    goes to 'done' and QA is spawned; under the builder's gate to 'review' -
    do not "fix" that by re-reporting. `premise_refuted` = {"claim",
    "measured", "did_instead"} records that the brief carried a measured claim
    that is not true; all three fields required.

    `next_approach` (FAILURES ONLY) is the ONE concrete thing you would try
    next, and it buys the item an extra automatic round that starts FROM it.
    Use it when you narrowed the problem and ran out of turns - NOT when you
    are blocked: a missing key, a credit block, an absent asset or an
    unwritable lane fails identically every round and should fail fast and
    stay failed. It is not a substitute for trying the idea. If it is in your
    lane and you can afford it, RUN IT before you close.
    Full notes: docs/tools.md#queue_complete
    """
    from bgate_core.board import queue as _q
    root = _root()
    refutation = {}
    if premise_refuted:
        try:
            refutation = _q.premise_refuted(
                root, int(item_id),
                claim=str(premise_refuted.get("claim") or ""),
                measured=str(premise_refuted.get("measured") or ""),
                did_instead=str(premise_refuted.get("did_instead") or ""),
                by=_actor())
        except ValueError as exc:
            return {"ok": False, "error": str(exc),
                    "note": "the item was NOT closed - fix the refutation and "
                            "call again, or drop premise_refuted"}
    if not failed:
        refused = _evidence_gate(root, int(item_id), evidence)
        if refused:
            return {**refused, "premise_refuted": refutation}
    # ONLY ON A FAILURE. The retry router reads this marker off the result
    # text, so a next move recorded against a "done" leaves a signal on the
    # board that no branch can ever act on - and an item nobody will reopen.
    if failed:
        result = _q.with_next_approach(result, next_approach)
    if not failed:
        try:
            from bgate_core.store import settings as _settings
            item = _q.get(root, int(item_id))
            mode = (os.environ.get("BGATE_DISPATCH_MODE") or
                    str(_settings.get(root, "dispatch.mode") or "structured"))
            chaos = (mode == "chaos" and bool(item.get("worktree")))
        except Exception:
            chaos = False
        closed = (_q.set_status(root, item_id, "integrating", result=result)
                  if chaos else _q.complete(root, item_id, result=result,
                                             failed=False))
    else:
        closed = _q.complete(root, item_id, result=result, failed=True)
    return {**closed, "premise_refuted": refutation} if refutation else closed


# Extensions whose write means "this run changed what a player SEES the scene
# do" - the gate keys on scenes rather than on images because a written image
# is itself viewable evidence, while a written scene proves nothing about how
# it renders.
_SCENE_SUFFIXES = (".tscn",)
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _note_tool_write(root, path) -> None:
    """A dispatched run's server-side file write lands in its writelog.

    The hook records only the agent's OWN Write/Edit/Bash; a file this server
    process writes on the agent's behalf (a fresh scene from level_generate,
    a godot_screenshot shot) was invisible to everything writelog feeds -
    including the evidence gate above, which could neither see the scene nor
    credit the shot. Best-effort: bookkeeping never fails the work.
    """
    try:
        owner = _work_item_id()
        if not owner:
            return
        from bgate_core.store import writelog as _writelog
        rel = _Path(path).resolve().relative_to(_Path(root).resolve())
        _writelog.record(root, str(rel), _seat(), f"item-{owner}",
                         tool=_CALL_TOOL.get() or "")
    except Exception:
        pass


def _evidence_gate(root: str, item_id: int, evidence: str) -> Optional[dict]:
    """Refuse a 'done' over scene writes with no render, or None to proceed.

    Enforced HERE, on the agent's own claim, and deliberately not in
    queue.complete: the reaper banks dead runs through that same function,
    and a corpse cannot be asked for a screenshot. Best-effort on its own
    faults - an unreadable writelog must not dam a completion.
    """
    try:
        from bgate_core.store import settings as _settings
        try:
            if not _settings.get(root, "qa.require_evidence"):
                return None
        except Exception:
            pass  # unreadable registry keeps the shipped default: on
        from bgate_core.store import writelog as _writelog
        writes = _writelog.paths_for(root, f"item-{item_id}")
        scenes = [p for p in writes
                  if p.lower().endswith(_SCENE_SUFFIXES)
                  and not p.startswith(".bgate/")]
        if not scenes:
            return None
        # A shot taken this run is the usual proof - godot_screenshot lands
        # under .bgate_out/shots/ and the hook records the write like any
        # other, so looking-before-claiming passes with nothing extra.
        rendered = any("shots" in _Path(p).parts
                       and p.lower().endswith(_IMAGE_SUFFIXES)
                       for p in writes)
        if rendered:
            return None
        if evidence:
            shot = _Path(evidence)
            shot = shot if shot.is_absolute() else _Path(root) / shot
            if shot.is_file() and shot.suffix.lower() in _IMAGE_SUFFIXES:
                return None
            return {"ok": False, "stage": "evidence_gate",
                    "error": (f"evidence {evidence!r} is not an image file on "
                              "disk - pass the path of a render you actually "
                              "looked at, or take one: godot_screenshot("
                              f"scene={scenes[0]!r})")}
        return {"ok": False, "stage": "evidence_gate", "scenes": scenes[:6],
                "error": (
                    "this run wrote scene(s) "
                    + ", ".join(scenes[:3])
                    + (" …" if len(scenes) > 3 else "")
                    + " and no render was taken, so 'done' would be a claim "
                      "about pixels nobody has seen. Geometry stats and green "
                      "checks are one check, never the full check. Take the "
                      f"picture - godot_screenshot(scene={scenes[0]!r}) - "
                      "LOOK at it, then complete again (the shot itself "
                      "satisfies this gate; `evidence=<path>` also works for "
                      "a render you already have). Reporting failed=True "
                      "never needs evidence.")}
    except Exception:
        return None


@_tool
def queue_reopen(item_id: int, reason: str) -> dict:
    """Send a done/failed item back to 'queued' for another round.

    The QA gate's FAIL path: reason is the ranked nitpick list, APPENDED to the
    brief so the next agent reads exactly what to fix, and recorded as the
    result. Routed through queue.reopen so ``attempts`` counts the round (the
    QA max-rounds cap reads it) and the record of what the last attempt wrote
    carries into the new brief.
    Full notes: docs/tools.md#queue_reopen
    """
    from bgate_core.board import queue as _q
    root = _root()
    item = _q.get(root, item_id)
    if item["status"] not in ("done", "failed"):
        # queue.reopen also accepts 'cancelled', deliberately not offered
        # here: cancelled is a human calling work off, and a machine
        # un-cancelling it is the human's call being overridden.
        raise ValueError(
            f"item {item_id} is {item['status']!r} - only done/failed "
            "items can be reopened")
    return _q.reopen(root, item_id, (reason or "").strip())


@_tool
def agent_steer_all(text: str) -> dict:
    """Say ONE thing to every agent running right now.

    For the correction that is not about one item: the art direction changed,
    the file everybody is about to touch is moving. Same delivery and caps as
    agent_steer (steer inbox, read when each agent's current step ends).
    Returns one row per item reached; a runner with no live channel is
    reported as refused. Aim carefully - this reaches seats on unrelated work.
    Full notes: docs/tools.md#agent_steer_all
    """
    from bgate_core.board import steerbox as _steerbox
    from bgate_core.board import queue as _q
    root = _root()
    said = str(text or "").strip()
    if not said:
        return {"ok": False, "error": "steer text is empty"}
    running = [it for it in _q.list_items(root, status="dispatched")]
    if not running:
        return {"ok": True, "count": 0, "sent": [],
                "note": "nothing is running - there was nobody to steer"}
    sent = []
    for item in running:
        posted = _steerbox.post_long(root, int(item["id"]), said,
                                     by=f"seat:{_seat() or 'director'}")
        sent.append({"item_id": int(item["id"]), "seat": item.get("seat") or "",
                     "steer_id": posted["id"]})
    return {"ok": True, "count": len(sent), "sent": sent,
            "delivery": "queued for the dashboard to hand over; each agent "
                        "reads it when its current step ends"}


@_tool
def agent_steer(item_id: int, text: str) -> dict:
    """Say something to the agent currently running a work item, mid-run.

    Left in the steer inbox and delivered by the dashboard: it needs `bgate
    serve` running, the agent reads it when its CURRENT step ends, and an item
    with no live agent gets no delivery (check queue_list(status='dispatched');
    use queue_update or queue_reopen for work not running). CAPPED AT 2000
    CHARACTERS: past that the text goes to a file and the agent gets the
    opening paragraph plus the path - never truncated. A correction that
    should outlive the run goes in queue_update's brief or queue_reopen.
    Full notes: docs/tools.md#agent_steer
    """
    from bgate_core.board import steerbox as _steerbox
    from bgate_core.board import queue as _q
    root = _root()
    item = _q.get(root, int(item_id))
    if item["status"] != "dispatched":
        return {"ok": False,
                "error": f"item {item_id} is {item['status']!r}, not running "
                         " - there is no agent to steer",
                "status": item["status"]}
    posted = _steerbox.post_long(root, int(item_id), text,
                                 by=f"seat:{_seat() or 'director'}")
    out = {"ok": True, "item_id": int(item_id), "steer_id": posted["id"],
           "delivery": "queued for the dashboard to hand over; the agent "
                       "reads it when its current step ends"}
    if posted.get("excerpted"):
        out["note_path"] = posted["note_path"]
        out["delivery"] += (f" - over {_steerbox.MAX_TEXT} characters, so it "
                            "was handed over as an excerpt plus the path to "
                            "the full text")
    return out


@_tool
def agent_activity(item_id: int, limit: int = 30) -> dict:
    """Watch a dispatched agent work - its recent steps, result and liveness.

    Everything comes off disk (the item's stream-json log plus the agent
    registry), so it works from any session whether or not `bgate serve` is
    up. Returns the last ``limit`` steps (say / tool / result / steer), the
    final result event, `running` (True/False, or null when the host will not
    vouch for the pid), the log path, and `step_count`/`truncated`. Read this
    BEFORE clearing or re-dispatching a failed item.
    Full notes: docs/tools.md#agent_activity
    """
    from bgate_core.board import agentlog as _agentlog
    root = _root()
    out = _agentlog.tail(root, int(item_id), limit=int(limit))
    try:
        from bgate_core.board import queue as _q
        item = _q.get(root, int(item_id))
        out["status"] = item["status"]
        out["seat"] = out.get("seat") or item.get("seat") or ""
        out["title"] = item.get("title") or ""
    except LookupError:
        out["status"] = None
    return {"ok": True, **out}


@_tool
def ask_human(question: str, refs: Optional[list] = None,
              to: str = "human") -> dict:
    """Ask ONE question of a NAMED recipient - and keep working.

    `to`: human (DEFAULT), director (FAILS if no live director session, never
    silently rerouted), seat:<name> (blackboard plus a steer to that seat's
    agent), decision (a FORMAL decision for the register). It returns
    immediately and DOES NOT BLOCK - do not poll or idle; say what you
    assumed. Not a work item: it lands on the event bus. The answer arrives as
    a steer if you are still running, else as a handoff `decision` note. Make
    it decidable ("A or B?"); `refs` are ids/paths to look at.
    Full notes: docs/tools.md#ask_human
    """
    from bgate_core.board import steerbox as _steerbox
    root = _root()
    item_id = _work_item_id() or 0
    to = str(to or "human").strip().lower()

    if to.startswith(_steerbox.SEAT_PREFIX):
        target = to[len(_steerbox.SEAT_PREFIX):]
        got = _steerbox.ask_seat(root, target, question, refs=refs,
                                 item_id=item_id, by=_actor())
        _log("question", f"asked the {target} seat: {str(question)[:100]}",
             ref=str(item_id))
        return got
    if to == "director":
        got = _steerbox.ask_director(root, question, refs=refs,
                                     item_id=item_id, by=_actor())
        _log("question", f"asked the director: {str(question)[:100]}",
             ref=str(item_id))
        return got
    if to == "decision":
        return {"ok": False, "delivered_to": "",
                "error": "a formal decision is filed, not asked. Call "
                         "decision_add(title, acceptance, ...) - it needs an "
                         "acceptance test, which is the whole difference "
                         "between a settled decision and an opinion."}
    if to != "human":
        return {"ok": False, "delivered_to": "",
                "error": f"unknown recipient {to!r}; it is one of "
                         f"{_steerbox.RECIPIENTS} or "
                         f"'{_steerbox.SEAT_PREFIX}<seat name>'. The recipient "
                         "is never guessed: a question delivered to somebody "
                         "other than the one you named is a delivery failure "
                         "wearing a success."}

    result = _steerbox.ask(root, question, refs=refs, item_id=item_id,
                           seat=_seat() or "director", by=_actor())
    _log("question", f"asked the human: {str(question)[:120]}",
         ref=str(item_id or result["seq"]))
    if item_id:
        arrives = ("as a steer into this run if you are still going when "
                   "they answer, otherwise as a handoff decision note for "
                   "the next session")
    else:
        arrives = ("as a handoff decision note - this session is not a "
                   "dispatched work item, so there is no run to steer")
    return {**result, "answer_arrives": arrives, "delivered_to": "human",
            "note": "returns immediately - do not wait for the answer"}


# ---------------------------------------------------------------------------
# Cutout characters: parts on a skeleton, animated once per template
# ---------------------------------------------------------------------------
# FOUR TOOLS, NOT SEVEN. The public tool list is a budget an agent reads in
# full, and the kit-generation and part-rerun verbs belong with the art
# generation path rather than here - they are labelled as not-yet-built rather
# than stubbed, so nobody calls one and gets a shrug.


def _cutout_dir(root: str, name: str) -> "_Path":
    """Where a character's document, parts and scene live.

    Inside game/assets/**, which is the ART SEAT'S EXISTING WRITE LANE. A bare
    characters/** would be out of lane for every seat and the PreToolUse hook
    would refuse the writes - the feature would be unusable by the seat that
    owns it.
    """
    return _Path(root) / "game" / "assets" / "characters" / name


@_tool
def cutout_templates() -> dict:
    """The cutout rig templates, and what a kit for each has to contain.

    A CUTOUT CHARACTER IS THE OTHER WAY TO ANIMATE IN 2D: about ten parts,
    animation authored once per TEMPLATE and free forever, equipment a texture
    swap on one slot. The cost: a puppet, not a painting - rigid Sprite2Ds on
    Node2D bones, no deformation. `parts_to_generate` is the actual generation
    list; far-side limbs reuse the near-side drawings with a tint.
    Full notes: docs/tools.md#cutout_templates
    """
    from bgate_core.three_d import cutout as _cutout
    return {"ok": True, "templates": _cutout.templates(),
            "layout": "game/assets/characters/<name>/",
            "not_built_yet": [
                "cutout_kit_generate - generating the parts themselves "
                "still goes through image_generate/chroma by hand against "
                "a pinned reference",
                "cutout_part_rerun - regenerate one part in place",
            ]}


@_tool
def cutout_assemble(name: str, parts: dict, template: str = "biped_v1",
                    adjustments: Optional[dict] = None, notes: str = "",
                    force: bool = False) -> dict:
    """Build a cutout character from its parts and emit a scene that moves.

    `parts` maps SLOT -> image path (cutout_templates lists slots); missing
    slots get no sprite. Emits `<name>.cutout.json` (the editable rig),
    `<name>.tscn` and `<name>.anims.tres` (six clips baked on THIS rest pose).
    Slots ending _far reuse the matching _near image with a tint.
    `adjustments` nudges the template per character ({"arm_near": {"rot":
    -8}}). REFUSES to overwrite a .tscn that changed since it last wrote one;
    `force=True` discards those changes.
    Full notes: docs/tools.md#cutout_assemble
    """
    try:
        from bgate_core.three_d import cutout as _cutout, cutoutwire as _wire
        root = _root()
    except Exception as exc:
        return _fail(exc)
    try:
        home = _cutout_dir(root, name)
        doc = _cutout.empty(name, template)
        spec = _cutout.template(template)
        skin = {}
        for slot, path in (parts or {}).items():
            target = _Path(path)
            if not target.is_absolute():
                target = _Path(root) / path
            if not target.is_file():
                return {"ok": False,
                        "error": f"part {slot!r} points at {target}, which is "
                                 "not on disk"}
            skin[slot] = {"texture": str(target),
                          "part_hash": _cutout.part_hash(target)}
        # The far side, unless the caller filled it in themselves.
        for far, near in (spec.get("reuse") or {}).items():
            if far not in skin and near in skin:
                skin[far] = {"texture": skin[near]["texture"],
                             "part_hash": skin[near]["part_hash"],
                             "reuse_of": near,
                             "far_tint": spec.get("far_tint")}
        doc["skin"] = skin
        doc["adjustments"] = adjustments or {}
        doc["notes"] = notes
        doc = _cutout.normalise(doc)

        sizes = {}
        try:
            from PIL import Image
            for slot, entry in doc["skin"].items():
                with Image.open(entry["texture"]) as img:
                    sizes[slot] = img.size
        except Exception:
            # A missing size means a part hangs from its top-left instead of
            # its pivot: visibly wrong, and better than a guessed offset.
            pass

        doc_path = _cutout.save(home / f"{name}{_cutout.SUFFIX}", doc)
        emitted = _wire.emit(doc, project_dir=root,
                             scene_path=home / f"{name}.tscn",
                             sizes=sizes, force=force)
        if not emitted.get("ok"):
            return {**emitted, "doc": str(doc_path)}
        status = _cutout.status(doc, root=root)
        _log("cutout",
             f"assembled {name}: {emitted['sprites']} sprites, "
             f"{len(emitted['clips'])} clips",
             ref=emitted["scene_res"])
        _register_artifact(f"{name}.tscn", emitted["scene"],
                           producer="cutout_assemble",
                           metadata={"template": template,
                                     "slots": len(doc["slots"]),
                                     "filled": len(doc["skin"])})
        return {**emitted, "doc": str(doc_path), "status": status,
                "how": [f"instance {emitted['scene_res']} in a scene",
                        'call play("walk") on it - the rig script is on the root',
                        "connect its anim_event signal for hit frames"]}
    except Exception as exc:
        return _fail(exc)


@_tool
def cutout_status(name: str) -> dict:
    """What is wrong with a cutout character, and what is merely unfinished.

    Reports rather than refuses. `missing` is slots with no part yet;
    `problems` needs action: missing_texture (the file is not there),
    stale_pivot (a hand-placed pivot on a drawing since regenerated), origin
    (the rig's feet are not on the ground line).
    Full notes: docs/tools.md#cutout_status
    """
    from bgate_core.three_d import cutout as _cutout
    root = _root()
    doc = _cutout.load(_cutout_dir(root, name) / f"{name}{_cutout.SUFFIX}")
    return {"ok": True, **_cutout.status(doc, root=root)}


@_tool
def cutout_equip(name: str, slot: str, texture: str, pivot: Optional[list] = None,
                 force: bool = False) -> dict:
    """Put a different part in one slot and re-emit - a hat, a sword, an arm.

    Swapping equipment is one texture on one slot. This changes what the
    character ships wearing; the runtime `equip()` does the same at runtime.
    `pivot` is [x, y] as a fraction of the new part's bounding box, y measured
    UP from the bottom; passing it records it as AUTHORED so cutout_status
    flags it if the part is later regenerated.
    Full notes: docs/tools.md#cutout_equip
    """
    from bgate_core.three_d import cutout as _cutout, cutoutwire as _wire
    root = _root()
    home = _cutout_dir(root, name)
    doc = _cutout.load(home / f"{name}{_cutout.SUFFIX}")
    target = _Path(texture)
    if not target.is_absolute():
        target = _Path(root) / texture
    if not target.is_file():
        return {"ok": False, "error": f"no part at {target}"}
    entry = dict(doc["skin"].get(slot) or {})
    entry["texture"] = str(target)
    entry["part_hash"] = _cutout.part_hash(target)
    if pivot:
        entry["pivot"] = list(pivot)
        entry["pivot_source"] = "authored"
    doc["skin"][slot] = entry
    doc = _cutout.normalise(doc)
    sizes = {}
    try:
        from PIL import Image
        for name_, ent in doc["skin"].items():
            with Image.open(ent["texture"]) as img:
                sizes[name_] = img.size
    except Exception:
        pass
    _cutout.save(home / f"{name}{_cutout.SUFFIX}", doc)
    emitted = _wire.emit(doc, project_dir=root,
                         scene_path=home / f"{name}.tscn",
                         sizes=sizes, force=force)
    if emitted.get("ok"):
        _log("cutout", f"equipped {name}.{slot} -> {target.name}",
             ref=emitted["scene_res"])
    return {**emitted, "slot": slot, "texture": str(target),
            "pivot_source": entry.get("pivot_source", "default")}


# ---------------------------------------------------------------------------
# kie.ai - Suno music and Seedance video
# ---------------------------------------------------------------------------
# THE TWO CAPABILITIES THIS PRODUCT HAS NEVER HAD. audiolab MIXES audio and has
# never generated a note; nothing anywhere has generated a frame of video. Both
# arrive behind one key, so both get a tool here rather than waiting on a UI
# surface - a capability with no door is a capability the system does not have,
# which is the rule tests/mcp/test_brainstorm_mcp.py::TestParity was added to hold.
#
# MUSIC IS A PIPELINE ASSET NOW; VIDEO STILL IS NOT. These tools used to say the
# same thing about both - "nothing downstream imports this, listen to it and
# that is all". That is still true of a .mp4: no importer, no gate, no manifest
# kind. It is no longer true of a track. bgate_core.audio.music registers every
# generated take as a candidate artifact revision and installs a KEPT one under
# the engine project, so music now runs the same candidate -> human decision ->
# asset path the art seat does, with the same 'only a human may approve' rule.
#
# WHICH IS WHY THE MUSIC TOOLS GO THROUGH bgate_core.audio.music, NOT THE ADAPTER.
# kie.generate_music still exists and still just writes files; calling it
# directly from here would produce paid-for .mp3s in a scratch directory with no
# provenance row, no ledger entry and no way for the audio seat to see them - # the same reason kie's IMAGES get no tool of their own and reach the pipeline
# through image_generate(provider="kie") instead. One door per capability.
@_tool
def kie_status() -> dict:
    """Is kie.ai usable, and what does it reach? Never exposes the key.

    kie is one key over three capabilities: images (Nano Banana, FLUX.2, Qwen),
    Suno music, and Seedance video. No 3D - that stays on Krea.
    """
    root = _root()  # triggers .env load
    from bgate_adapters import kie

    got = dict(kie.available(root))
    got["models"] = kie.models()
    return {"ok": True, **got}


@_tool
def music_options() -> dict:
    """What a music generation may ask for: models, per-mode character limits,
    which model takes a duration, and whether kie is reachable at all.

    Read this BEFORE writing a long prompt. Every ceiling here is enforced
    locally before the request is sent, so exceeding one costs a refusal rather
    than a round trip and a 422 that does not say which field was too long.
    """
    from bgate_core.audio import music as _music

    return {"ok": True, **_music.options(_root())}


@_tool
def music_generate(prompt: Annotated[str, Field(description='Simple mode: a description up to 500 characters. custom=True: the lyrics (3,000-5,000 chars by model).')], name: Annotated[str, Field(description='Logical name the batch is registered under; candidates land in .bgate_out/audio/<name>/.')] = "", instrumental: Annotated[bool, Field(description='True (default) for background music with no vocalist; False for a title song or diegetic track.')] = True,
                       model: Annotated[str, Field(description='Suno model id; "" takes the default. music_options lists them with their ceilings.')] = "", custom: Annotated[bool, Field(description='False (simple mode) lets Suno write everything; True takes lyrics plus style and title.')] = False, style: Annotated[str, Field(description='Custom mode only: the musical style tags. Refused in simple mode.')] = "",
                       title: Annotated[str, Field(description='Custom mode only: the track title. Refused in simple mode.')] = "", negative_tags: Annotated[str, Field(description='Styles to steer away from.')] = "",
                       vocal_gender: Annotated[str, Field(description="'m' or 'f'; applies only when instrumental=False.")] = "", duration: Annotated[Optional[int], Field(description='Seconds, 10-360. V5_5 ONLY; refused on every other model.')] = None,
                       timeout: Annotated[float, Field(description='Seconds to wait for the whole batch. Default 900.')] = 900.0) -> dict:
    """Generate MUSIC with Suno through kie.ai. Costs real credits.

    ONE REQUEST RETURNS SEVERAL TRACKS, registered as candidates under one
    logical name: audition (music_candidates), music_keep ONE, music_discard
    the rest. instrumental defaults to TRUE; vocal_gender ('m'/'f') applies
    only with False. custom=False takes a 500-char description; custom=True
    takes lyrics plus `style`/`title`, refused in simple mode. duration is
    V5_5 ONLY (10-360s). Downloads inside this call. Cost may be UNPRICED
    (`credits_source`, `accounted`). Blocks one to three minutes.
    Full notes: docs/tools.md#music_generate
    """
    from bgate_core.audio import music as _music

    suno: dict = {"instrumental": bool(instrumental), "custom": bool(custom)}
    for key, value in (("model", model), ("style", style), ("title", title),
                       ("negative_tags", negative_tags),
                       ("vocal_gender", vocal_gender)):
        if value:
            suno[key] = value
    if duration is not None:
        suno["duration"] = int(duration)
    refused = _provider_gate(_root(), "music", "a music generation")
    if refused:
        return refused
    return _music.generate(_root(), prompt, name=name,
                           work_item_id=_work_item_id(),
                           timeout=float(timeout), **suno)


@_tool
def music_status(task_id: str) -> dict:
    """Where a Suno task got to, straight from kie. Costs nothing.

    Reports the eight documented statuses. CALLBACK_EXCEPTION is the subtle one:
    it means kie could not deliver its webhook, NOT that the music failed, so it
    is only a failure when the record also carries no audio.

    This only LOOKS. When it says `recoverable`, music_recover is what puts
    the audio on disk - do not re-run music_generate to get files for a task
    that already has them, that is paying twice.
    """
    from bgate_core.audio import music as _music

    return _music.status(_root(), task_id)


@_tool
def music_stuck_tracks(older_than_s: int = 0, poll: bool = True) -> dict:
    """Music generations that were PAID FOR and never collected. Finds money.

    A batch is charged at SUBMIT; the poll, download and absorb can all die
    afterwards. Run it after any crash, kill or dashboard restart.
    `recoverable` is the list to act on - finished generations music_recover
    can still collect inside the provider's retention window, which the
    result names. `poll=False` answers from local tickets alone and reaches no
    provider.
    Full notes: docs/tools.md#music_stuck_tracks
    """
    from bgate_core.audio import music as _music
    return _music.stuck_tracks(
        _root(), older_than_s=int(older_than_s) or _music.STUCK_AFTER_S,
        poll=bool(poll))


@_tool
def music_recover(task_id: str, name: str = "") -> dict:
    """Download and register the tracks of a task ALREADY PAID FOR. Costs nothing.

    For anything that went wrong after music_generate's submit - a timeout, a
    dropped connection, a CDN refusing the download. kie holds the audio for
    FOURTEEN DAYS. IDEMPOTENT: takes already registered are `skipped`. NO COST
    IS CLAIMED against this call - the charge happened at submit time.
    Full notes: docs/tools.md#music_recover
    """
    from bgate_core.audio import music as _music

    return _music.recover(_root(), task_id, name=name,
                          work_item_id=_work_item_id())


@_tool
def music_candidates(logical_name: str = "", limit: int = 100) -> dict:
    """Generated tracks awaiting a keep-or-discard decision, plus what was kept.

    Every row carries its artifact_id (what music_keep / music_discard take),
    its on-disk path, its prompt and what it cost. LISTEN to a candidate before
    keeping it - the dashboard's audio seat plays them inline; from a shell the
    path is a real file.
    """
    from bgate_core.audio import music as _music

    root = _root()
    cap = max(1, min(int(limit), 500))
    return {"ok": True,
            "candidates": _music.candidates(root, logical_name=logical_name,
                                            limit=cap),
            "kept": _music.kept(root, limit=cap)}


@_tool
def music_keep(artifact_id: int, note: str = "") -> dict:
    """Keep one candidate: install it under the engine project and approve it.

    The file is copied to game/assets/audio/music/ (inside the audio lane and
    the Godot project) and only then is the revision approved. APPROVAL IS A
    HUMAN'S CALL: this goes through artifacts.review, which refuses an agent
    unless the project turned its approval gate off. If refused, say which
    candidate you would keep and why; do not look for another route.
    Full notes: docs/tools.md#music_keep
    """
    from bgate_core.audio import music as _music

    return _music.keep(_root(), int(artifact_id), note=note)


@_tool
def music_install(artifact_id: int) -> dict:
    """Put an ALREADY-APPROVED take where the game can load it. The repair verb.

    On a project whose approval gate is off, takes are approved as filed, so
    there is no candidate and no keep - and no installed file. Use it when
    music_candidates shows a kept track with `installed: false`, or an
    approved track's file was deleted from the engine project. Idempotent;
    does not change review state.
    Full notes: docs/tools.md#music_install
    """
    from bgate_core.audio import music as _music

    return _music.install(_root(), int(artifact_id))


@_tool
def music_discard(artifact_id: int, note: str = "") -> dict:
    """Reject a candidate track. Refusing to ship something is an agent's call
    to make, so unlike music_keep this needs no human.

    The file is left where it is - under .bgate_out, gitignored and outside the
    engine project. Only the decision is recorded, with the reason, against an
    immutable revision row. Say what was wrong with it; 'discarded' teaches the
    next generation nothing.
    """
    from bgate_core.audio import music as _music

    return _music.discard(_root(), int(artifact_id), note=note)


@_tool
def kie_video_generate(prompt: Annotated[str, Field(description='What the clip shows.')], filename: Annotated[str, Field(description='Output name under .bgate_out/video/.')], seconds: Annotated[Optional[int], Field(description='Clip length; Seedance takes 4-15, ranges move per model. Omitted, the model default.')] = None,
                       quality: Annotated[str, Field(description='480p | 720p | 1080p | 4k on Seedance; translated per model.')] = "", shape: Annotated[str, Field(description='16:9 | 9:16 | 1:1 | 4:3 | 3:4 | 21:9 | adaptive on Seedance; translated per model.')] = "",
                       first_frame: Annotated[str, Field(description="Public URL or local path to open on; a local file is uploaded to kie's store first.")] = "", audio: Annotated[Optional[bool], Field(description='Generate audio baked into the clip; it cannot be removed later. Omitted, the model default.')] = None,
                       model: Annotated[str, Field(description='Registered video model name; "" takes the default (cinematic_options lists them).')] = "", timeout: Annotated[float, Field(description='Seconds to wait for the clip. Default 1800.')] = 1800.0) -> dict:
    """Generate a VIDEO CLIP through kie.ai. Costs real credits, and video is the
    most expensive thing this product can buy.

    Arguments are INTENT, translated per registered model (cinematic_options
    lists ranges): seconds, quality (480p..4k), shape (16:9, 9:16, 1:1, ...),
    audio (BAKED IN). first_frame may be a URL or a local path. Runs in
    MINUTES; filename lands under .bgate_out/video/. THIS IS THE RAW DOOR: one
    unmanaged .mp4 Godot CANNOT PLAY. For anything the project ships use
    cinematic_plan / cinematic_generate_shot / cinematic_keep.
    Full notes: docs/tools.md#kie_video_generate
    """
    root = _Path(_root())
    from bgate_adapters import kie

    # THE ACCOUNT, BEFORE THE MOST EXPENSIVE UNIT THIS PRODUCT BUYS. A drained
    # kie balance refuses this regardless of what the shot would cost, and
    # learning that from a paid 402 is the expensive way to find out.
    refused = _provider_gate(str(root), "video",
                             f"a {seconds}s video shot")
    if refused:
        return refused

    # kie reports an unknown price as None, never 0.0, and that distinction
    # survives into the result: an estimate nobody could produce is a fact the
    # caller is owed before they buy, not something to bury under a figure that
    # reads as free.
    priced = None
    try:
        quote = kie.estimate_usd(model=model, seconds=seconds)
        if isinstance(quote, dict) and quote.get("usd") is not None:
            priced = float(quote["usd"])
    except Exception:
        priced = None
    base = (root / ".bgate_out" / "video").resolve()
    out = (base / (filename or "clip.mp4")).resolve()
    try:
        out.relative_to(base)
    except ValueError:
        return {"ok": False,
                "error": "filename must stay inside .bgate_out/video - "
                         f"refusing {filename!r}"}
    result = kie.generate_video(
        prompt, str(out), model=model or kie.DEFAULT_VIDEO_MODEL,
        seconds=seconds, quality=quality, shape=shape,
        first_frame=first_frame, audio=audio,
        root=str(root), logical_name=out.stem,
        work_item_id=_work_item_id(), timeout=float(timeout))
    if result.get("ok"):
        _log("video", f"generated a {result.get('model')} clip {out.name}",
             ref=result["path"])
    # The forward estimate travels WITH the result. None means kie publishes no
    # price for this model, which is a fact the caller is owed rather than a
    # figure that reads as free.
    return {**result, "estimated_usd": priced}


# ---------------------------------------------------------------------------
# THE DOMAIN MODULES, IMPORTED LAST ON PURPOSE. Each one does
# `from bgate_mcp.server import ...`, which is legal only because every
# name above this line already exists when these imports run. The star
# import re-binds each domain's tools into this namespace, so
# `server.<tool>` keeps answering for tests and internal callers.
# ---------------------------------------------------------------------------
from bgate_mcp.tools_blender import *  # noqa: E402,F401,F403
from bgate_mcp.tools_brainstorm import *  # noqa: E402,F401,F403
from bgate_mcp.tools_cinematic import *  # noqa: E402,F401,F403
from bgate_mcp.tools_level import *  # noqa: E402,F401,F403
# THE TEST SEAMS THE STAR IMPORTS SKIP. A pile of tests stub the blender
# adapter by mutating the MODULE OBJECT through this namespace
# (`setattr(server._blender, "combine", ...)`) - that works from any module
# that shares the object, so the alias is re-exposed here rather than every
# patch site rewritten; same for the private diagnostics they call directly.
# ruff sees these as unused, which is exactly how the aliases got dropped
# and 23 tests broke on CI - hence the explicit noqa.
from bgate_adapters import blender as _blender  # noqa: E402,F401
from bgate_mcp.tools_blender import _imageto3d_summary  # noqa: E402,F401

# ---------------------------------------------------------------------------
# The map of the surface. Registered LAST, on purpose: it reads the live
# registry, and everything above plus the four tools_* modules has to be in it
# first.
# ---------------------------------------------------------------------------
def _registry_rows() -> list[tuple[str, str]]:
    """(name, description) for every tool THIS process serves.

    The live registry rather than a written list, because the two ways a
    written list goes wrong are both invisible: it names a tool this session's
    seat or module choices removed, or it omits one that was added last week.
    """
    return [(t.name, t.description or "")
            for t in mcp._tool_manager.list_tools()]


@_tool
def tool_index(task: str = "") -> dict:
    """THE MAP OF EVERY TOOL YOU CAN CALL, grouped by craft. Free, no side effects.

    Call this FIRST when you do not know which tool does a job - before
    guessing a name, before hand-rolling the work, and before asking the human
    which tool to use. The tool list you were handed is flat and alphabetical;
    this is the same set with shape.

    task  "" returns the whole map, one line per tool. Otherwise a search:
          every word must appear in a tool's name or first line, so
          "sprite sheet" is narrower than "sprite" rather than louder.

    The lines are first sentences, not documentation - the parameters are in
    each tool's own schema, which is already in your context. This exists to
    tell you WHICH schema to read.
    """
    rows = _registry_rows()
    return {"ok": True, "count": len(rows), "task": task,
            "index": _toolindex.render(rows, task=task, seat=_seat(),
                                       hidden=_parked_by_craft())}


def _parked_by_craft() -> dict[str, list[str]]:
    """{craft: [tool names]} this seat could unlock but has not."""
    from bgate_core.store import modules as _modules

    out: dict[str, list[str]] = {}
    for name in sorted(_PARKED):
        for craft in sorted(_modules.crafts_owning(name)) or ["spine"]:
            out.setdefault(craft, []).append(name)
    return out


@_tool
def tool_unlock(craft: str) -> dict:
    """Register a craft's tools this seat does not hold, for the rest of the session.

    The director seat boots with the arbitration and evidence surfaces and
    delegates generation; when the human wants a sprite from THIS session,
    unlock "image" and the image_* schemas arrive. The client is sent a
    tools/list_changed notification; a client that ignores it needs a restart
    with BGATE_SEAT_TOOLS=all. `tool_index()` lists what is unlockable. A
    craft the project switched off in Settings > Modules cannot be unlocked.
    """
    from bgate_core.store import modules as _modules

    craft = str(craft or "").strip().lower()
    if craft not in _modules.CRAFTS:
        return _fail(ValueError(
            f"unknown craft {craft!r}; one of {', '.join(sorted(_modules.CRAFTS))}"))
    names = _parked_by_craft().get(craft, [])
    added = []
    for name in names:
        wrapper = _PARKED.pop(name, None)
        if wrapper is None:
            continue
        mcp.tool()(wrapper)
        added.append(name)
    notified = False
    if added:
        try:
            session = mcp.get_context().session
            anyio.from_thread.run(session.send_tool_list_changed)
            notified = True
        except Exception:
            logging.getLogger(__name__).info(
                "tool_unlock: no session to notify", exc_info=True)
        _reinstall_tool_index()
    return {"ok": True, "craft": craft, "added": added, "notified": notified,
            "note": ("" if added else
                     "nothing to add: already held, or switched off by the "
                     "project's modules")}


def _install_tool_index() -> None:
    """Fold the compact map into the server's `instructions`.

    A tool nobody knows about is not a discoverability fix, and `instructions`
    is the one string every client shows the model before its first turn - so
    the names ride along there and `tool_index()` is how the agent gets the
    lines. Names only: the full render is ~230 lines and belongs in a result
    the agent asked for.

    THROUGH THE PRIVATE ATTRIBUTE because FastMCP exposes `instructions` as a
    read-only property over `_mcp_server.instructions`, and the string cannot
    be built at construction time - the registry it describes does not exist
    until every module above has imported. Guarded rather than asserted: an
    MCP release that moves the attribute must cost this index, not the server.
    """
    try:
        compact = _toolindex.compact(_registry_rows(), seat=_seat(),
                                     hidden=_parked_by_craft())
        base = mcp._mcp_server.instructions or ""
        _INSTRUCTIONS_BASE.setdefault("text", base)
        mcp._mcp_server.instructions = (
            _INSTRUCTIONS_BASE["text"] + "\n\n" + _PROJECT_DIR_NOTE + "\n\n" + compact)
    except Exception:
        logging.getLogger(__name__).warning(
            "the tool index could not be folded into instructions; "
            "tool_index() still serves it", exc_info=True)


# The instructions as built by seats.py, before the index was appended - so a
# tool_unlock can rebuild the tail without stacking a second copy.
_INSTRUCTIONS_BASE: dict[str, str] = {}
_reinstall_tool_index = _install_tool_index

_install_tool_index()


def _report_orphans() -> None:
    """Say what the LAST server was holding when it died, before serving.

    stdout is the MCP transport, so this goes to stderr — which is where the
    client shows server logs, and the only channel available before a single
    tool has been called. The one moment this information exists is the first
    read after a restart: nothing else records that a provider call was in
    flight when its process was killed, and the file it produced has no
    provenance without this line.
    """
    try:
        from bgate_core.board import inflight as _inflight

        root = os.environ.get("BGATE_ROOT", "").strip() or _root_hint()
        if not root:
            root = str(_project.require_root())
        notice = _inflight.startup_notice(root)
    except Exception:                                             # noqa: BLE001
        return
    if notice:
        print(notice, file=_sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# AN ARGUMENT THE TOOL DOES NOT HAVE IS A MISTAKE, NOT A NO-OP
# ---------------------------------------------------------------------------
#
# MEASURED. An agent meaning to run one test script called
# `godot_test_run(only=["tests/door_test.gd"])`. The real parameter is `paths`.
# FastMCP validates arguments against each tool's JSON schema, and pydantic
# IGNORES properties the schema does not mention — so `only` was dropped on the
# floor, the tool ran with its defaults, and all fifteen scripts executed. The
# result came back green and said nothing about the argument it had discarded.
#
# That is the same failure shape as everything else in this pass: the caller
# believed it had scoped the run, the harness knew it had not, and the two
# never met. A typo in a parameter name should cost one refusal, not a full
# suite run plus the wrong conclusion drawn from it.
#
# WHY HERE AND NOT IN THE `_tool` WRAPPER. The wrapper never sees the extra
# key — FastMCP has already stripped it by then. The check has to sit at the
# call boundary, before validation, which is the last place the caller's actual
# words still exist.
_ORIGINAL_CALL_TOOL = mcp.call_tool


async def _call_tool_checked(name, arguments, *args, **kwargs):
    """Refuse a call carrying arguments its tool does not declare."""
    try:
        tool = mcp._tool_manager.get_tool(name)
        known = set((tool.parameters or {}).get("properties") or {})
    except Exception:                                             # noqa: BLE001
        known = set()                    # unknown tool: let the real path 404
    if known and isinstance(arguments, dict):
        stray = [k for k in arguments if k not in known]
        if stray:
            # Name the near-miss. Half of these are one letter out, and an
            # agent that is told "unknown argument" without being told the real
            # one usually guesses again.
            import difflib

            hints = []
            for key in stray:
                close = difflib.get_close_matches(key, sorted(known), n=2)
                hints.append(f"{key!r}" + (f" (did you mean {' or '.join(repr(c) for c in close)}?)"
                                           if close else ""))
            return _to_content({
                "ok": False,
                "refused": "unknown_argument",
                "error": (f"{name} does not take " + ", ".join(hints)
                          + ". The call was REFUSED rather than run with the "
                            "argument dropped: a run that silently ignores "
                            "the parameter you used to scope it does the "
                            "wrong work and reports success. Its parameters "
                            "are: " + ", ".join(sorted(known)) + "."),
                "unknown_arguments": stray,
                "parameters": sorted(known),
            })
    return await _ORIGINAL_CALL_TOOL(name, arguments, *args, **kwargs)


def _to_content(payload: dict):
    """The refusal, in the shape call_tool's caller expects."""
    import json as _json

    from mcp.types import TextContent
    return [TextContent(type="text", text=_json.dumps(payload))]


mcp.call_tool = _call_tool_checked


def main() -> None:
    _report_orphans()
    mcp.run()


if __name__ == "__main__":
    main()
