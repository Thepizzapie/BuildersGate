"""Builders Gate MCP server (FastMCP, stdio).

Every tool takes an optional `project_dir` and resolves the project from it,
then BGATE_ROOT, then the cwd by walking up for a .bgate dir — so an agent
working inside a game repo never has to pass paths around, but a fleet sharing
one server can always be explicit.

There used to be a module-level `_ACTIVE_ROOT` that project_select mutated, and
which every tool read. That made "which game does this call affect" a function
of call ORDER: two agents on one server, one selects project A, the other
selects B, and the first one's next write lands in B. Deleted. The per-call
`project_dir` travels with the call, and the fallback for a call that omits it
is a contextvar bound for the duration of THAT call only — nothing a concurrent
call can reach in.

Tool errors return a dict with an "error" key rather than raising: a raised
exception inside a tool call reads to the model as a broken server, while an
error payload reads as a fact it can act on.

FAILURE SHAPE — ONE PREDICATE, EVERY TOOL. A result is a failure if and only if
it carries a truthy "error"; every failure also carries "ok": false, and the two
are always set together, so either key answers the question. Legacy shapes are
kept alongside rather than replaced, because callers already read them: a tool
that used to answer {"available": false, "reason": ...} still answers with those
keys AND with ok/error, and a tool that answered a bare {"ok": false, ...} gains
the "error" string built from whatever reason it did state. Success payloads are
left exactly as they were — an absent "error" is the success signal, and no tool
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

import contextvars
import functools
import inspect
import itertools
import json as _json
import os
import threading
import time as _time
from pathlib import Path as _Path
from typing import Annotated, Callable, Optional

import anyio
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from bgate_adapters import blender as _blender
from bgate_adapters import godot as _godot
from bgate_adapters import recorder as _recorder
from bgate_adapters import sprites as _sprites
from bgate_core import activity as _activity
from bgate_core import assets as _assets
from bgate_core import autotile as _autotile
from bgate_core import levelgen as _levelgen
from bgate_core import scenewire as _scenewire
from bgate_core import tilemap as _tilemap
from bgate_core import artifacts as _artifacts
from bgate_core import refs as _refs
from bgate_core import seats as _seats
from bgate_core import bible as _bible
from bgate_core import playtest as _playtest
from bgate_core import scaffold as _scaffold
from bgate_core import canon as _canon
from bgate_core import chroma as _chroma
from bgate_core import causal as _causal
from bgate_core import db as _db
from bgate_core import handoff as _handoff
from bgate_core import lore as _lore
from bgate_core import iterations as _iterations
from bgate_core import items as _items
from bgate_core import project as _project
from bgate_core import search as _search
from bgate_core import vfx as _vfx

# THE ONE CHANNEL THAT CANNOT BE DROPPED BY CHANGING DIRECTORY.
#
# The working process used to be communicated four ways, and every one of them
# was conditional: tool docstrings (only if the agent reads the schema), the
# CLAUDE.md managed block (only if the project was init/adopt-ed AND the agent
# is standing in it), seat_brief (only if the agent thinks to call it), and the
# dispatch prompt (only for agents the dashboard spawned). A human-started
# session in a fresh checkout hit none of them and saw a bare tool list — so it
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
# session's identity for its whole life — the same fact `_seat()` below relies on.
#
# The root is resolved here too, and best-effort: a seatless session's brief
# quotes the DIRECTOR SEAT's own mission, so a project that rewrote it with
# seat_configure gets its wording rather than the shipped default. A server can
# legitimately start outside any project, so `None` is an ordinary answer and
# seats.py falls back to the code default — this must never stop a boot.
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


_PROJECT_DIR_DOC = (
    "Absolute path to the Builders Gate project root (the directory holding "
    ".bgate). Omit it and the server falls back to BGATE_ROOT, then to walking "
    "up from the working directory. Pass it explicitly whenever more than one "
    "project could be in play — it is the only way a call is guaranteed to land "
    "in the game you mean."
)

# The per-call project override. A ContextVar, deliberately: it is set on the
# way into ONE tool call and reset on the way out, so it cannot be observed by
# any other call. The module-level `_ACTIVE_ROOT` this replaces could — that was
# the race, and this is the whole reason the contextvar exists.
_CALL_ROOT: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "bgate_call_root", default=None)


def _root_hint() -> Optional[str]:
    """The root this call was given, if any — before falling back to discovery."""
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
            f"filename must stay inside .bgate_out/art — refusing {filename!r}"
        ) from None
    return out


def _root() -> str:
    """The project root for THIS call: project_dir > BGATE_ROOT > walk up from cwd.
    Also loads the project's .env (once) so secrets live with the project."""
    override = _root_hint()
    if override:
        root = override
    else:
        try:
            root = str(_project.require_root())
        except LookupError as exc:
            # core's own hint still points at project_select, which no longer
            # switches anything. Restate it in terms of what actually works now.
            raise LookupError(
                f"{exc} Pass project_dir=<absolute path to the project root> on "
                "this call, or export BGATE_ROOT, or run project_init to create "
                "one here.") from None
    try:
        from bgate_core import envfile
        envfile.load_project_env(root)
    except Exception:
        pass
    return root


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
    exists to produce — and the standard response to a tool that looks broken is
    to retry it or move on, not to turn the lights down.

    Only entries that themselves claim failure contribute, so the three good
    frames of a four-frame turnaround stay quiet, and only the top level's own
    values are inspected — this reads a result, it does not walk a tree.
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
    in result` is the whole test. Legacy keys stay put — the dashboard and the
    seat scripts already read `available` and `reason`, and a normalizer that
    renames things breaks callers to please a schema.

    Only the top level is touched, and only when the result claims failure: a
    doctor report whose `blender` row is unavailable is a SUCCESSFUL answer to
    "what is installed", and stamping an error on it would be a lie.

    A REASON THE TOOL DID STATE IS NEVER REPLACED. That includes one stated per
    item — see _nested_reasons. The generic string is the last resort, not the
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
# response, and every defect these frames exist to catch — blown out, black,
# imported nothing, wrong colour entirely — survives a long edge of 512.
_IMAGE_RETURN_EDGE = 512
_IMAGE_RETURN_CAP = 6


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
    binding is reset in a finally — a tool that raises must not leave its root
    behind for the next call on this thread.

    The bodies are blocking by nature: subprocesses (Blender, Godot, ffmpeg),
    sqlite, and image-model calls that legitimately run for tens of minutes.
    Run as a plain sync def, FastMCP would await them ON the loop and one
    image_sprites batch would freeze the dashboard, the queue and every other
    seat's tool call behind it — the exact failure the transcribe adapter's
    docstring says this design exists to avoid. So the wrapper is async and the
    body goes to a worker thread.

    That hop is why the ContextVar is bound inside a FRESH copied context rather
    than around the await: anyio reuses worker threads, so a `set` left on a
    pooled thread's default context could be seen by whatever call lands on that
    thread next. Each call gets its own contextvars.Context, sets the root in
    there, and drops it — call N's project_dir cannot reach call N+1 no matter
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
            try:
                payload = _normalize(fn(*args, **kwargs))
                if images is None or not isinstance(payload, dict):
                    return payload
                try:
                    blocks = _image_blocks(images(payload))
                except Exception:
                    blocks = []  # a picture is a bonus; never lose the result
                return [*blocks, payload] if blocks else payload
            finally:
                _CALL_ROOT.reset(token)

        # abandon_on_cancel stays False (the default): a cancelled client must
        # not leave a half-written .blend or a half-downloaded image behind.
        return await anyio.to_thread.run_sync(contextvars.copy_context().run, _call)

    wrapper.__signature__ = signature.replace(
        parameters=[*signature.parameters.values(), inspect.Parameter(
            "project_dir", inspect.Parameter.KEYWORD_ONLY, default=None,
            annotation=Annotated[Optional[str],
                                 Field(default=None, description=_PROJECT_DIR_DOC)])])
    return mcp.tool()(wrapper)


def _fail(exc: Exception) -> dict:
    # ok=false alongside the message: one predicate for every failure in this
    # module, whatever the tool. See _normalize.
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


_RUN_SEQ = itertools.count()
_RUN_SEQ_LOCK = threading.Lock()


def _run_tag(label: str = "") -> str:
    """A token unique to ONE tool call, for output paths nobody else can clobber.

    Fixed output names (shot.png, consistency_check.png, .bgate_out/render.png)
    are fine with one seat and silently destructive with several: two seats
    screenshotting at once, and the second write lands under the first one's
    returned path, so the first seat reviews the second seat's game. The pid is
    in there because seats are separate PROCESSES — a counter alone repeats
    across them — and the counter is because two calls in one process can start
    inside the same clock second.
    """
    with _RUN_SEQ_LOCK:
        seq = next(_RUN_SEQ)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:32]
    stamp = f"{_time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{seq:04d}"
    return f"{safe}-{stamp}" if safe else stamp


def _actor() -> str:
    """Who this server process acts as — the identity artifacts.review checks."""
    return _activity.current_actor()


def _caller_is_agent() -> bool:
    """Is this server an AGENT's, rather than the human's own session?

    Two signals, either is enough. BGATE_ACTOR carries the `agent:` prefix the
    core's actor model uses; but dispatch.py stamps a spawned seat with
    BGATE_SEAT / BGATE_WORK_ITEM and does NOT stamp BGATE_ACTOR, so trusting the
    actor alone would let every dispatched agent read as the human at the
    keyboard — which is precisely the caller a permission gate must catch.
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
        from bgate_core import activity
        activity.log(_root(), kind, summary, seat=_seat(), ref=ref)
    except Exception:
        pass


def _archive_preview(src: str, label: str) -> Optional[str]:
    """Copy a render into .bgate/previews/ so the dashboard keeps a history.

    Renders land on a fixed path (render.png) and each run overwrites the last —
    without archiving, the dashboard could only ever show the newest one.
    """
    try:
        import shutil
        import time

        root = _Path(_root())
        previews = root / ".bgate" / "previews"
        previews.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40]
        dest = previews / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe or 'render'}.png"
        shutil.copy2(src, dest)
        return str(dest)
    except Exception:
        return None


def _work_item_id() -> Optional[int]:
    """The work item this session is executing, if any — the key the spend
    ledger charges against."""
    raw = os.environ.get("BGATE_WORK_ITEM", "").strip()
    return int(raw) if raw.isdigit() else None


def _run_ceiling(root: str, override_usd: float = 0.0) -> float:
    """The dollar ceiling for ONE tool call. 0.0 means uncapped.

    Three sources, most specific first: an explicit argument on the call, the
    max_cost_usd of the work item this session is executing, then the project
    budget's per_item_usd. spend.item_ceiling already knows the last two — this
    only has to find the work item, which the ledger keys spend against anyway.
    """
    if override_usd and float(override_usd) > 0:
        return float(override_usd)
    from bgate_core import spend as _spend

    item: dict = {}
    work_item = _work_item_id()
    if work_item:
        try:
            from bgate_core import queue as _q
            item = _q.get(root, work_item) or {}
        except Exception:
            item = {}
    try:
        return float(_spend.item_ceiling(root, item) or 0.0)
    except Exception:
        return 0.0


def _spend_gate(root: str, projected_usd: float, what: str,
                ceiling_usd: float = 0.0) -> Optional[dict]:
    """Refuse a paid run BEFORE the first call, or None to proceed.

    A cap that only reports what a run cost is an invoice, not a cap. Both
    ceilings are consulted: the per-run one (see _run_ceiling) and the project
    /day budget (spend.check), which is the same gate the dispatcher asks before
    spawning — an overnight fan-out must not be bounded in one leg and unbounded
    in the other. The refusal names the number so the caller can decide, rather
    than saying no and leaving the model to guess by how much.
    """
    from bgate_core import spend as _spend

    projected = round(max(0.0, float(projected_usd or 0.0)), 4)
    if ceiling_usd and projected > ceiling_usd:
        return {"ok": False, "stage": "spend_gate", "estimated_usd": projected,
                "ceiling_usd": round(float(ceiling_usd), 4),
                "error": f"{what} is estimated at ${projected:.2f}, over the "
                         f"${float(ceiling_usd):.2f} ceiling for one run — cut "
                         "poses or quality, or pass max_cost_usd to confirm the "
                         "spend deliberately"}
    try:
        verdict = _spend.check(root, projected_usd=projected)
    except Exception:
        verdict = {"allowed": True}  # no ledger is not a licence to refuse work
    if not verdict.get("allowed", True):
        return {"ok": False, "stage": "spend_gate", "estimated_usd": projected,
                "budget": verdict,
                "error": f"{what} (~${projected:.2f}) is refused by the project "
                         f"budget: {verdict.get('reason') or 'ceiling reached'}"}
    return None


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
    try:
        target = root or _root_hint() or os.getcwd()
        return _project.init(target, name, pitch=pitch, engine=engine, dimension=dimension)
    except Exception as exc:
        return _fail(exc)


@_tool
def project_select(project: str = "") -> dict:
    """Resolve a Builders Gate project by registered name or path. DEPRECATED as
    a mode switch — it no longer changes what later calls affect.

    It used to latch the choice into a server-wide variable, which meant the
    project a tool touched depended on who called project_select last. Now it
    only ANSWERS: it verifies the project exists, registers it so it stays
    discoverable, and hands back its absolute root. Feed that root to the
    `project_dir` parameter that every tool carries (or export BGATE_ROOT before
    spawning the server) — then the target of a call is written on the call.

    Empty arg: report the root this session resolves to plus every known project.
    Returns {active, known} or {active, project, use_project_dir, deprecated}.
    """
    try:
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
                              "active project — pass project_dir=<active> on "
                              "each tool call instead"}
    except Exception as exc:
        return _fail(exc)


@_tool
def bgate_doctor(refresh: bool = False) -> dict:
    """Can this machine actually do the work? One call, every dependency.

    Run this FIRST, and any time a tool fails with "not found" — instead of
    calling blender_status, godot_status, image_status, playtest_check and
    playtest_devices one after another to assemble the same picture.

    Nothing here opens the microphone, renders a frame, launches an engine or
    downloads a model: playtest_check does open the mic (deliberately — a muted
    mic is invisible any other way), so it stays the pre-SESSION check, not the
    is-my-toolchain-here check. Results are cached a few seconds, so polling
    this is cheap; pass refresh=True right after installing something.

    Returns {blender, godot, ffmpeg, ffprobe, whisper, openai_key, python},
    each {available: bool, path, version, min_required, reason}. `reason` is
    filled in when available is False (missing, too old, or the probe hung) and
    says what to install or which BGATE_* env var points at it. Never raises.
    """
    try:
        from bgate_core import doctor as _doctor

        root = None
        try:
            root = _root()  # only to pick up the project's .env for the API key
        except Exception:
            pass
        return _doctor.check(root, refresh=bool(refresh))
    except Exception as exc:
        return _fail(exc)


@_tool
def project_status() -> dict:
    """The project's identity plus a count of what's in the bible and lore."""
    try:
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
        return {"project": _project.get(root), "root": root, "counts": counts}
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Design bible
# ---------------------------------------------------------------------------
@_tool
def bible_add(kind: str, title: str, body: str = "", rank: int = 0) -> dict:
    """Add a bible section.

    kind: pillar | loop | scope_tier | cut_line | constraint | reference.
    rank orders within a kind; for scope_tier, LOWER rank = higher priority, and
    anything ranked at or below the cut_line's rank is explicitly not being built.
    """
    try:
        return _bible.add(_root(), kind, title, body=body, rank=rank)
    except Exception as exc:
        return _fail(exc)


@_tool
def bible_update(section_id: int, title: Optional[str] = None,
                 body: Optional[str] = None, rank: Optional[int] = None) -> dict:
    """Update a bible section in place. Omitted fields keep their current value."""
    try:
        return _bible.update(_root(), section_id, title=title, body=body, rank=rank)
    except Exception as exc:
        return _fail(exc)


@_tool
def bible_read(kind: Optional[str] = None) -> dict:
    """Read the bible. No kind: the grouped overview with the scope cut applied."""
    try:
        root = _root()
        if kind:
            return {"kind": kind, "sections": _bible.list_sections(root, kind)}
        return _bible.overview(root)
    except Exception as exc:
        return _fail(exc)


@_tool
def scope_check(rank: int) -> dict:
    """Is work at this rank above the cut line? Call before building anything."""
    try:
        root = _root()
        line = _bible.cut_line(root)
        return {
            "rank": rank,
            "in_scope": _bible.in_scope(root, rank),
            "cut_line": line,
            "note": "no cut line set — scope call not yet made" if line is None else "",
        }
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Lore
# ---------------------------------------------------------------------------
@_tool
def lore_add(kind: str, name: str, summary: str = "", body: str = "",
             status: str = "draft") -> dict:
    """Create a lore entity.

    kind: faction | character | place | event | item | concept | species.
    status: draft | canon | retired. Names are unique — update, don't duplicate.
    """
    try:
        return _lore.add_entity(_root(), kind, name, summary=summary, body=body,
                                status=status)
    except Exception as exc:
        return _fail(exc)


@_tool
def lore_update(ref: str, summary: Optional[str] = None, body: Optional[str] = None,
                status: Optional[str] = None) -> dict:
    """Update an entity by slug or name. Promote draft to canon with status='canon'."""
    try:
        return _lore.update_entity(_root(), ref, summary=summary, body=body, status=status)
    except Exception as exc:
        return _fail(exc)


@_tool
def lore_brief(ref: str) -> dict:
    """Everything about one entity — record, facts, and edges. Read before writing it."""
    try:
        return _lore.brief(_root(), ref)
    except Exception as exc:
        return _fail(exc)


@_tool
def lore_list(kind: Optional[str] = None, status: Optional[str] = None) -> dict:
    """List entities, optionally filtered by kind and/or status."""
    try:
        return {"entities": _lore.list_entities(_root(), kind=kind, status=status)}
    except Exception as exc:
        return _fail(exc)


@_tool
def lore_link(src: str, rel: str, dst: str, note: str = "") -> dict:
    """Connect two entities. rel is free-form: 'rules', 'allied_with', 'born_in'."""
    try:
        return _lore.link(_root(), src, rel, dst, note=note)
    except Exception as exc:
        return _fail(exc)


@_tool
def lore_fact(ref: str, statement: str, source: str = "", locked: bool = False) -> dict:
    """Assert ONE atomic fact about an entity — canon_check compares against these.

    Keep it to a single checkable claim ("The siege lasted seven years"), not a
    paragraph. locked=True marks it immovable: conflicts against it are hard.
    """
    try:
        return _lore.add_fact(_root(), ref, statement, source=source, locked=locked)
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Canon + recall
# ---------------------------------------------------------------------------
@_tool
def canon_check(text: str, entities: Optional[list[str]] = None) -> dict:
    """Check text against canon BEFORE it lands. Run on every narrative write.

    Returns verdict (ok | review | conflict), the entities it touches, the canon
    facts in play, and flags. Deterministic lexical checks: catches retired
    entities, invented proper nouns, polarity flips, and number disagreements.
    It does not judge tone or theme — 'ok' means nothing mechanical is wrong.
    """
    try:
        return _canon.check(_root(), text, entities=entities)
    except Exception as exc:
        return _fail(exc)


@_tool
def recall(query: str, limit: int = 10, kind: Optional[str] = None) -> dict:
    """Search the bible and lore. Call this BEFORE inventing anything."""
    try:
        conn = _db.connect(_root())
        return {"query": query, "results": _search.find(conn, query, limit=limit, kind=kind)}
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Blender
# ---------------------------------------------------------------------------
@_tool
def blender_status() -> dict:
    """Is Blender available to this machine, and which version? Check before modeling.

    Also reports `generate`: whether image-to-3D is reachable, and from where.
    Folded in here rather than given its own tool — one question ("what can I
    build with?") should cost one call.
    """
    try:
        probe = _blender.available()
        out = {**probe, **(_blender.version() if probe["available"] else {})}
        out["generate"] = _imageto3d_summary()
        return out
    except Exception as exc:
        return _fail(exc)


def _imageto3d_summary() -> dict:
    """A few lines, not the whole catalogue.

    imageto3d.status() carries every backend's full licence prose — right for
    a doctor row a human reads once, far too expensive to hand an agent on
    every status call. Names what is usable and why the rest is not, and
    leaves the reading to blender_generate's own failure.
    """
    try:
        from bgate_adapters import imageto3d as _i3d
    except Exception:
        return {"available": False, "reason": "adapter unavailable"}
    try:
        full = _i3d.status()
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    gpu = full.get("gpu") or {}
    usable = list(full.get("usable") or [])
    blocked = {b["backend"]: b.get("reason", "")
               for b in full.get("backends") or []
               if not b.get("available") and b.get("implemented")}
    return {"available": bool(usable), "usable": usable,
            "gpu": gpu.get("name", ""), "vram_gb": gpu.get("vram_gb"),
            "blocked": blocked,
            "note": ("nothing configured — see .env.example; a generated mesh "
                     "is a DRAFT and still has to be cleaned, scaled, oriented "
                     "and rigged before it is an asset")
            if not usable else
            "a generated mesh is a DRAFT: clean, scale, orient and rig it"}


@_tool
def blender_run(script: str, blend_file: Optional[str] = None, render: bool = False,
                engine: str = "BLENDER_WORKBENCH", timeout: int = 180,
                label: str = "", kit: bool = True) -> dict:
    """Run a bpy script in headless Blender and get the scene back as facts.

    `bpy` is already imported. Returns per-object tri/vert counts (evaluated, so
    modifiers count), UV warnings, materials, your print() output, and — with
    render=True — a PNG of the active camera view (archived to the project's
    preview gallery; give a `label` so humans can tell renders apart).

    THE MODELLING KIT IS ALREADY THERE (kit=True, the default). Do not write your
    own material/UV/hygiene helpers — an agent burned 33 KB and most of an hour
    doing exactly that on the first real character run. Available:
      bg_help()                      PRINTS A COMPLETE WORKED LAYER SCRIPT — a
                                     humanoid built from one head-height, a
                                     named rig with roll, the checks, bg_finish
                                     last. Read it before writing your first one.
      bg_wipe()                      empty the scene (no default cube)
      bg_box/bg_cyl/bg_ball/bg_plane named primitives
      bg_mirror/bg_smooth/bg_taper   symmetry, subsurf, limb taper
      bg_join(objs, name)            one layer should leave as ONE mesh
      bg_clean(obj)                  doubles/loose/degenerate/normals — THIS is
                                     what makes automatic weighting work later
      bg_unwrap(obj)                 smart-project UVs (no UVs = no texture)
      bg_mat(obj, name, rgb)         a BLOCKING-IN colour, not a shipped surface
      bg_bone_chain(name, bones)     an armature with NAMED bones. Entries are
                                     (name, head, tail, parent=None, roll_deg=0);
                                     order does not matter, parents are wired in
                                     a second pass, and ROLL IS IN DEGREES — set
                                     it on limbs or a humanoid retarget gives you
                                     the twisted-forearm look.
      bg_finish(obj, colour=...)     clean + apply + unwrap + material, in order
      bg_stats(obj)                  verts/faces/loose/nonmanifold/ngons/flipped
                                     PLUS world-space dims/centre/min/max
      bg_bounds(obj)                 world-space min/max/dims/centre, in metres
      bg_flipped(obj)                how many faces point INWARD (count, measured
                                     on a throwaway copy — the mesh is untouched)
      bg_overlap(a, b)               do two layers' world bounds intersect, and
                                     by how much. Layers are built in isolated
                                     scenes, so "is the cap sunk into the head"
                                     is a question NOTHING else in the pipeline
                                     can ask until they are already combined.

    bg_bone_chain RAISES — deliberately, and it is the only thing in the kit that
    does. Everything else swallows its problems because a helper that raises
    takes the whole run down; a rig cannot afford that trade, because a wrong rig
    looks built and comes apart in the engine several steps later. It refuses: a
    parent no bone in the list defines (which used to produce silent parentless
    roots), a duplicate bone name, head == tail (Blender DELETES zero-length
    bones on leaving edit mode and says nothing, so the bone simply is not in the
    armature you get back), and a name Blender had to rename or truncate (bind=
    'bone:Head' then matches nothing in blender_combine). Every message names the
    bone. Read the message and fix the chain — do not wrap it in a try.

    START A BODY FROM THE BASE MESH LIBRARY, NOT FROM PRIMITIVES. Same kit, same
    namespace, no import:
      bg_human(height=1.8, heads=7.5, build, limbs, shoulders, detail,
               pose="t"|"a", convention="godot"|"blender", rig=True)
      bg_quadruped(...) / bg_prop_frame(...)
                                     each returns {"obj","rig","marks","props",
                                     "convention","pose"} — a correctly
                                     proportioned, closed, unwrapped,
                                     weight-ready body with a NAMED skeleton.
      bg_proportions(...)            45 measurements out of one number
      bg_mark(base, "head_top")      one landmark: position, radius, girth.
                                     RAISES on a name that is not there.
      bg_fit(obj, mark, mode="at"|"on"|"around"|"in", clearance, scale)
                                     places AND resizes a layer onto a landmark
      bg_shell / bg_human_chain / bg_human_skeleton / bg_roll
      bg_bone(base, "hand.R")        the real bone name (RAISES on an unknown
                                     role); BG_BONE_NAMES carries Godot's
                                     SkeletonProfileHumanoid spelling by default
      bg_weight(obj, rig)            binds AND counts what stayed unweighted
      bg_base_report / bg_base_assert  the base's own self-check (assert RAISES)
      bg_base_help()                 prints BG_BASE_EXAMPLE, the worked script
      BG_UNIT="metre", BG_HUMAN_HEIGHT=1.8, BG_GROUND=0.0, BG_FORWARD=(0,1,0),
      BG_LEFT=(-1,0,0), BG_SIDES — the base FACES +Y, which the glTF exporter
                                     turns into -Z, which is what Godot calls
                                     forward. Author faces, visors and emblems
                                     on the +Y side; the figure's own left is -X.
      bg_unit_check / bg_unit_assert (RAISES) / bg_rescale

    FIT LAYERS ONTO LANDMARKS INSTEAD OF GUESSING COORDINATES. MEASURED: a cap
    placed with bg_fit(cap, bg_mark(base, "head_top"), "on") rests on the crown
    at 10% overlap; the same cap at a hand-typed 1.7 m is 89% INSIDE the skull
    and passed every check the old pipeline had. The honest limit — the base has
    no face and no fingers. It is a correctly-proportioned blockout to build the
    character ONTO, not a finished character.

    Pass kit=False only for a script that must run against bare bpy.

    A broken script is a normal result with ok=False plus the traceback, so read
    the result and iterate rather than assuming it worked. engine:
    BLENDER_WORKBENCH (fast preview) | BLENDER_EEVEE_NEXT | CYCLES.
    """
    try:
        # Per-call render directory. The adapter always writes <out_dir>/render.png,
        # so a shared out_dir means the second seat rendering at the same moment
        # overwrites the first seat's frame at the very path the first call just
        # returned — silent, and it looks like the render simply came out wrong.
        out_dir = str(_Path(_root()) / ".bgate_out" / "renders" / _run_tag(label))
    except Exception:
        out_dir = None  # modeling before project_init is allowed
    try:
        result = _blender.run_script(script, blend_file=blend_file, render=render,
                                     out_dir=out_dir, engine=engine, timeout=timeout,
                                     kit=kit)
        rendered = result.get("render", {}) if isinstance(result.get("render"), dict) else {}
        if rendered.get("rendered") and rendered.get("path"):
            archived = _archive_preview(rendered["path"], label or "render")
            if archived:
                result["render"]["preview"] = archived
            artifact = _register_artifact(
                label or "blender-render", rendered["path"],
                producer="blender_run",
                metadata={"engine": engine, "preview": archived or "",
                          "scene": result.get("scene", {})})
            if artifact:
                result["render"]["artifact"] = artifact
                _log("render", f"rendered {label or 'a preview'} "
                               f"({result['scene']['totals']['tris']} tris)",
                     ref=archived)
        elif result.get("ok"):
            _log("blender", f"blender run: {label}" if label else
                 f"blender run ({result.get('scene', {}).get('totals', {}).get('tris', '?')} tris)")
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_warmup(engine: str = "BLENDER_EEVEE_NEXT") -> dict:
    """Pay the GPU cold-start cost up front. Run once per machine boot.

    A GPU engine's first render after a cold boot can take MINUTES of shader
    warmup (then ~1-2s forever after). Call this at pipeline start so no agent's
    real render is the one that stalls. Not needed for BLENDER_WORKBENCH.
    """
    try:
        out_dir = str(_Path(_root()) / ".bgate_out" / "renders" / _run_tag("warmup"))
    except Exception:
        out_dir = None
    try:
        return _blender.warmup(engine, out_dir=out_dir)
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_scene_stats(blend_file: str) -> dict:
    """Report an existing .blend without modifying it — objects, tris, materials."""
    try:
        return _blender.scene_stats(blend_file)
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_export_gltf(out_path: str, blend_file: Optional[str] = None,
                        script: str = "pass", timeout: int = 240) -> dict:
    """Export a .blend (or a bpy-script-built scene) to .glb for Godot.

    Modifiers are APPLIED on export — Blender defaults that off, which silently
    ships the base mesh and makes an asset look right in Blender and wrong in the
    engine. Also returns game-readiness issues (no UVs, n-gons, unapplied scale)
    worth fixing before the asset reaches a level. Pair with godot_import_asset.
    """
    try:
        return _blender.export_gltf(out_path, blend_file=blend_file,
                                    script=script, timeout=timeout)
    except Exception as exc:
        return _fail(exc)


def _register_assembly(result: dict, out_path: str, *, root_name: str,
                       rig: str, producer: str) -> Optional[dict]:
    """Put an assembled .glb on the artifact ledger. One shape, two callers.

    blender_combine and blender_layer_rerun produce the SAME asset by the same
    route, so they must register it the same way — one logical name means a
    re-run is revision N+1 of the character rather than a second unrelated one,
    which is the whole reason the QA gate can see a 3D asset at all.
    """
    layers = result.get("parts") or []
    return _register_artifact(
        root_name or _Path(out_path).stem, out_path, producer=producer,
        metadata={"layers": [layer.get("name", "") for layer in layers],
                  "sources": [layer.get("source", "") for layer in layers],
                  "armature": result.get("armature", ""),
                  "rig": rig,
                  "checks": result.get("checks") or [],
                  "warnings": result.get("warnings") or [],
                  "manifest": result.get("manifest", ""),
                  "tris": sum(int(layer.get("tris") or 0) for layer in layers)})


@_tool
def blender_combine(parts: list, out_path: str, rig: str = "",
                    root_name: str = "Assembled", timeout: int = 300) -> dict:
    """Assemble separately-modelled LAYERS into one rigged .glb, and test it.

    The end of the layered 3D path: model body, clothing, hard accessories and
    any logo as their own files, then join them here. Built in ONE pass instead,
    a figure comes back with the parts that lost the attention budget deformed —
    on a real baseball player, the hands, the cap, and a scrambled team logo.

    `parts` is the layer list, each a path or a dict:
      {"path": "out/uniform.glb",   # .glb / .gltf / .blend
       "name": "uniform",           # how it is reported and referenced
       "at": [0,0,0], "rotate": [0,0,0], "scale": 1.0,
       "bind": "deform",            # deform | bone:<Name> | none
       "decal_on": "cap"}           # conform to that layer's surface

    A LOGO OR ANY TEXT GOES IN AS ITS OWN LAYER WITH decal_on. Flush against the
    surface it z-fights and tears in-engine; shrinkwrap plus an offset fixes it.
    Hard geometry rides a bone (a cap does not bend), soft geometry deforms.
    `rig` names the layer holding the armature — without it nothing binds, which
    is right for a prop and a shipped statue for a character.

    Returns per-layer objects/tris/binding, plus `checks`: `unbound` and
    `unweighted_verts` name the layer that detaches or tears the first time it
    animates, so you re-run that layer instead of the whole character.

    The assembled file is REGISTERED as a candidate artifact (`artifact_id`),
    which is what puts it under the same QA gate every 2D asset passes through.
    Write out_path inside the project — an artifact cannot be recorded for a
    file outside it, and an unregistered asset is one no reviewer ever sees.
    """
    try:
        result = _blender.combine(parts, out_path, rig=rig,
                                  root_name=root_name, timeout=timeout)
        if result.get("ok"):
            layers = result.get("parts") or []
            artifact = _register_assembly(result, out_path, root_name=root_name,
                                          rig=rig, producer="blender_combine")
            if artifact:
                result["artifact"] = artifact
                result["artifact_id"] = artifact["id"]
            _log("blender", f"assembled {root_name!r} from {len(layers)} layers",
                 ref=str(out_path))
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def character_generate(prompt: str, out_dir: str, name: str = "character",
                       provider: str = "", backend: str = "",
                       height: float = 1.8, budget: int = 45000,
                       size: str = "1024x1536", godot_project: str = "",
                       dry_run: bool = True, timeout: int = 2400) -> dict:
    """"I want a model that looks like X." Plate, mesh, rig, into the engine.

    THE WHOLE CHARACTER PATH AS ONE CALL. Every stage was already reachable and
    a caller still had to know: condition the plate on the humanoid template or
    the skeleton will not fit; key it or the backdrop arrives as geometry no
    bone can reach; which backend takes which knobs; that a bind reports success
    having weighted nothing. Get any of those wrong and it costs ten GPU minutes
    to find out. They are the same five steps in the same order every time.

    Each stage gates the next, so a failure costs the stage that found it.
    Measured on the runs this was built from — an unkeyed plate took 605 s and
    came back 21% non-manifold, refused by the quality gate, against 216 s and
    16% for the same subject keyed; a collapse met its triangle budget with
    20,799 of 39,803 faces inside out; a bind created all 22 vertex groups and
    filled NONE, 64,878 of 64,878 vertices carrying no weight with every other
    check green.

    DRY_RUN IS TRUE BY DEFAULT. It quotes the backend and stops. This spends
    real money at the plate and again at the mesh, and a tool that bills on the
    first call is a tool nobody trusts twice — pass dry_run=False to run it.

    backend   "" asks choose(), which REFUSES to pick a backend whose licence
              carries conditions. That refusal is the design: this tool does not
              know your revenue, territory or monthly actives. Name one after
              reading its terms.
    godot_project  set it and the rigged .glb is imported, given a body and
              collider suited to what it is, wired into a .tscn and loaded
              through the engine to prove it opens. Leave it empty and nothing
              is written into a game project.

    Returns every artifact by path, the gate result from each stage, and `stage`
    naming where it stopped. `ok` is True only if a RIGGED character came out —
    a mesh that failed to bind reports ok=False with the unweighted count, and
    that is a refusal, not a warning.
    """
    # The decorator injects project_dir on every tool and _root() reads it, so
    # keys and spend land in the project the CALL named rather than whatever a
    # previous call left behind.
    try:
        root = _root()
    except Exception:
        root = None
    try:
        return _blender.character(
            prompt, out_dir, name=name, provider=provider, backend=backend,
            height=height, budget=budget, size=size,
            godot_project=godot_project, root=root, dry_run=dry_run,
            timeout=timeout)
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_humanoid_template() -> dict:
    """The shipped humanoid skeleton and the pose plate to generate against.

    START A CHARACTER HERE. Every generated mesh used to invent its own
    proportions, so the skeleton had to be bent to fit each one and no two
    characters could share an animation. Conditioning the PLATE on this
    reference inverts that — the art conforms to the skeleton, and a clip
    authored for one character plays on the next.

    Measured on one character, bones further than 6 cm from any mesh vertex:
      template scaled by height only ............ 16 of 24
      landmark fitting alone ..................... 5 of 23
      plate conditioned on this reference alone .. 8 of 23
      BOTH ....................................... 0 of 23, and 0 unweighted

    Returns the reference image to pass as `ref_images` to image_generate, the
    prompt clause that holds the stance, and the 23 Godot-profile bone names
    every humanoid from this pipeline carries — so BoneMap retargeting works
    and animations move between characters.

    The five-step path:
      1. image_generate(prompt + pose_clause, ref_images=[pose_front])
      2. key it — an opaque plate becomes geometry, measured 2.8x slower and
         21% non-manifold against 16% keyed
      3. blender_generate(plate, out)          draft mesh
      4. blender_rig(mesh, out)                adopt, fit, bind, PROVE it
      5. godot_deliver_asset(project, rigged)  .tscn, verified in-engine
    """
    try:
        return _blender.humanoid_template()
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_rig(model: str, out_path: str, kind: str = "humanoid",
                height: float = 1.8, budget: int = 0, orient: bool = True,
                armature_name: str = "Skeleton", symmetrize: str = "auto",
                timeout: int = 900) -> dict:
    """Take a GENERATED mesh to a bound, weighted character an engine can move.

    Every image-to-3D backend returns `rigged: false` — geometry and nothing
    else. This is the missing step between that and a character: adopt the mesh
    (weld, decimate, scale, orient, ground), fit a skeleton to its own measured
    height, bind it, and PROVE the bind took.

    THE PROOF IS `unweighted`, AND NOTHING CHEAPER WORKS. Blender's parent_set
    returns cleanly, creates all 22 vertex groups, and can leave every one of
    them empty. The modifier attaches. Godot loads it and shows a Skeleton3D.
    The character animates not at all. MEASURED on a real generation: 64,878 of
    64,878 vertices carrying no weight with every other check green.

    Adopt and bind happen in ONE Blender session on purpose. Round-tripping
    through a file between them is what produced that failure: glTF re-import
    carries a root transform, the skeleton lands in a different space from the
    mesh, and heat finds no vertices near any bone. Same mesh in one session:
    3 of 19,556.

    Bone heat is tried first because it deforms properly; ARMATURE_ENVELOPE is
    the fallback and is rigid, so elbows and shoulders pinch. `bound_with` says
    which one shipped. **`rigged` False means the asset is not animatable** —
    it is not a warning to pass along, it is a refusal.

    kind    "humanoid" reads a front from foot reach; "none" refuses to guess.
            A subject with no feet (a prop, a bust) wants "none", and then
            orientation is NEVER ESTABLISHED — check the turnaround yourself.
    budget  0 leaves the density alone. A local backend with no face_count knob
            hands back ~280k faces, and post-decimation here is the only lever
            those users have. 8k shattered a character; 45-60k was clean.

    symmetrize  "auto" (default) mirrors the skin weights across the body's own
            centre plane, but ONLY when the audit says the two sides are within
            2% of the character's height of each other. Heat fails differently
            on each side — one clean elbow and one bound to the ribs is the
            normal outcome — and averaging the pair fixes it without picking a
            winner. "off" skips it. "force" runs it on an asymmetric body, which
            is right for a cosmetic asymmetry (one pauldron, a cloak) and wrong
            for anything else.

    THE REPORT NOW CARRIES `audit` BEFORE THE BIND, and it is the part worth
    reading first. `audit.shells` is the fragmentation count — a real user's
    character arrived as 940 separate shells, which passes every
    well-formedness gate and guarantees a bad bind, because heat will not cross
    the gaps and loose islands weight to whichever bone is nearest.
    `audit.symmetry.mean` is how far the body is from its own mirror image.

    AND `rigged: true` IS STILL NOT "ANIMATABLE". Run blender_flex on the
    output: it bends the thing and measures what bending it did.

    `coverage` (kind="humanoid" only) is a fast pre-check for the 15 bone
    names godot_retarget_check calls essential — Hips, the spine/head chain,
    both arms, both legs, under the EXACT name a BoneMap-free retarget
    matches by. It cannot see hierarchy or binding, only naming, so a pass
    here is not a substitute for retarget_check against the real engine —
    it just means a naming problem shows up now instead of after the Godot
    round-trip.
    """
    try:
        result = _blender.rig(model, out_path, kind=kind, height=height,
                              budget=budget, orient=orient,
                              armature_name=armature_name,
                              symmetrize=symmetrize, timeout=timeout)
        if result.get("ok"):
            coverage = result.get("coverage") or {}
            note = ""
            if coverage:
                note = (" [coverage OK]" if coverage.get("passed")
                        else f" [coverage MISSING {coverage.get('missing')}]")
            _log("blender",
                 f"rigged {model} -> {result.get('bound_with')} "
                 f"({result.get('unweighted_pct')}% unweighted){note}",
                 ref=str(out_path))
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_flex(model: str, out_dir: str = "", stem: str = "flex",
                 render: bool = True, engine: str = "BLENDER_WORKBENCH",
                 volume_tolerance: float = 0.18, pinch_tolerance: float = 0.60,
                 timeout: int = 600) -> dict:
    """Bend a rigged character and report what bending it did to the body.

    THE SECOND HALF OF THE RIG PROOF. `blender_rig` answers "were weights
    written" with the unweighted count, and that is the only thing it can
    answer. It says NOTHING about whether the elbow survives being bent, and a
    rig with zero unweighted vertices routinely collapses a joint to a straw,
    loses a quarter of its volume in one bend, or drives the forearm through the
    ribs. Every number stays green while the character animates like a bag of
    spanners. Run this before you deliver one.

    Poses each joint a walk cycle moves, ONE AT A TIME so a failure is
    diagnosable, and per pose measures:

      volume_ratio      posed volume over rest volume. A good bind costs 2-6%.
      worst_pinch       the joint that lost the most cross-section. 1.0 is
                        rigid, 0.6 is a visible waist, under 0.4 is a straw.
      new_self_pairs    faces that intersect in this pose and did not at rest.
                        The increase, not the count — a generated mesh arrives
                        with overlapping shells and the absolute number is
                        meaningless.
      render            a PNG of the pose. LOOK AT IT. The whole lesson of this
                        pipeline is that green gates are not evidence.

    `verdict.passed` False is a refusal, not a warning: those weights are not
    animatable as they stand. The usual fixes, in order — raise `budget` on the
    rig so the joint has enough loops to bend, check `audit.shells` for a
    fragmented mesh heat could not cross, and re-run the rig with
    symmetrize='force' when only one side failed.
    """
    try:
        result = _blender.flex(model, out_dir, stem=stem, render=render,
                               engine=engine,
                               volume_tolerance=volume_tolerance,
                               pinch_tolerance=pinch_tolerance,
                               timeout=timeout)
        verdict = result.get("verdict") or {}
        if result.get("ok"):
            _log("blender",
                 f"flexed {model} -> "
                 f"{'passed' if verdict.get('passed') else 'FAILED'} "
                 f"({len(verdict.get('issues') or [])} issues over "
                 f"{verdict.get('checked', 0)} poses)",
                 ref=str(model))
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_weights(model: str, threshold: float = 0.02,
                    min_largest_fraction: float = 0.9,
                    min_bleed_vertices: int = 3, timeout: int = 300) -> dict:
    """Per deform bone, does its weight paint cover one patch of the mesh or two.

    A THIRD RIG PROOF, ALONGSIDE `blender_rig` AND `blender_flex`. Neither of
    those catches this: `rig()`'s `unweighted` count only sees vertices with
    NO weight, and `flex()` only sees a joint after it bends. Bleed is
    neither — a hand painted mostly to Hand but partly to Spine, because a
    brush stroke crossed empty space in the viewport rather than the mesh
    surface, has full weight coverage and may not even move wrong at any of
    flex's six test poses if the bleed region is small. It still reads as a
    seam-tearing glitch the moment the spine and the hand pose differently.

    Reports each deform bone's weighted vertices as connected components on
    the mesh surface (edge adjacency, not proximity — a seam does not count
    as touching). One component is healthy. `verdict.passed` False names
    which bones split and how many vertices sit off their own patch.
    """
    try:
        report = _blender.weight_islands(model, threshold=threshold, timeout=timeout)
        if report.get("ok"):
            verdict = _blender.weight_islands_verdict(
                report, min_largest_fraction=min_largest_fraction,
                min_bleed_vertices=min_bleed_vertices)
            report["verdict"] = verdict
            _log("blender",
                 f"weight-islands {model} -> "
                 f"{'passed' if verdict.get('passed') else 'FAILED'} "
                 f"({len(verdict.get('issues') or [])} bleeding bones over "
                 f"{verdict.get('checked', 0)} checked)",
                 ref=str(model))
        return report
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_texture(model: str, image: str, out_path: str, material: str = "",
                    all_slots: bool = False, roughness: str = "",
                    metallic: str = "", normal: str = "", emission: str = "",
                    normal_strength: float = 1.0, alpha: str = "auto",
                    alpha_cutoff: float = 0.5,
                    backface_cull: Optional[bool] = None, decal: bool = False,
                    timeout: int = 240) -> dict:
    """Put GENERATED maps on a 3D layer's material and re-export it.

    The surface half of the layered path. Measured on the first real character
    run: the assembled asset carried 21 materials and ZERO images — every
    surface a flat colour an agent typed by hand, because nothing connected the
    image adapter to the 3D layers. Generate the maps with image_generate
    (task_kind="texture", conditioned on the pinned refs via use_pinned), then
    apply them here, per layer, before blender_combine.

    `image` is the albedo / base colour and is what the one-image call has
    always meant. The rest are optional and each drives its own BSDF input.
    WITHOUT THEM EVERY SURFACE IS THE SAME PLASTIC — the modelling kit types
    rough=0.6, metal=0.0, so cloth, leather, skin and steel all ship as one
    dielectric and colour is the only thing that varies across an asset:
      roughness   how glossy, per texel        metallic  0 dielectric, 1 metal
      normal      tangent-space normals        emission  what glows
    Those four are DATA and are loaded Non-Color; `image` and `emission` feed
    colour sockets and stay sRGB. Pass image="" to apply maps without changing
    the base colour. normal_strength scales the Normal Map node.

    ALPHA — auto | opaque | clip | blend. MEASURED: a decal needs alpha="clip"
    to export `alphaMode: MASK`. Without it the logo layer ships as a solid
    rectangle of key colour glued over the cap, which is worse than the
    z-fighting the decal layer exists to prevent. `auto` inspects the base image
    and picks clip only when it ACTUALLY carries transparent pixels — an opaque
    PNG with an RGBA header is not a cut-out — so say clip explicitly when you
    know it is one. alpha_cutoff is the MASK threshold. decal=True is shorthand
    for a conformed graphic and implies backface culling; backface_cull
    overrides it either way.

    `material` names ONE slot. IT IS EFFECTIVELY REQUIRED on a model carrying
    more than one authored material: `all_slots=True` is the explicit opt-in
    that says you meant to paint every slot, because that used to be the DEFAULT
    and it put one image over skin, eyes and mouth and called the layer
    textured. A named material matching no slot is a failure, not a cheerful
    ok=True with an empty list. Meshes with no UVs are unwrapped first — a map
    on an unwrapped mesh is silently ignored, which looks exactly like the
    generation having failed.

    The re-exported layer is REGISTERED as a candidate artifact (`artifact_id`)
    and carries the maps it was given, so the surface a reviewer is judging can
    be traced to the images that produced it. Write out_path inside the
    project; a file outside it cannot be recorded.
    """
    try:
        maps = {"roughness": roughness, "metallic": metallic,
                "normal": normal, "emission": emission}
        result = _blender.apply_texture(
            model, image or None, out_path, material=material,
            all_slots=all_slots,
            **{kind: (path or None) for kind, path in maps.items()},
            normal_strength=normal_strength, alpha=alpha,
            alpha_cutoff=alpha_cutoff, backface_cull=backface_cull,
            decal=decal, timeout=timeout)
        if result.get("ok"):
            given = {kind: str(path) for kind, path in
                     {"base_color": image, **maps}.items() if path}
            artifact = _register_artifact(
                _Path(out_path).stem, out_path, producer="blender_texture",
                refs=list(given.values()),
                metadata={"model": str(model), "texture": str(image),
                          "material": material, "all_slots": bool(all_slots),
                          "maps": given, "decal": bool(decal),
                          # The mode the adapter RESOLVED, not the one asked
                          # for: `auto` is the common call and the answer it
                          # reached is what decides alphaMode in the glTF.
                          "alpha": result.get("alpha") or alpha,
                          "alpha_cutoff": alpha_cutoff,
                          "textured": result.get("textured") or [],
                          "unwrapped": result.get("unwrapped") or []})
            if artifact:
                result["artifact"] = artifact
                result["artifact_id"] = artifact["id"]
            _log("blender", f"textured {_Path(out_path).name} with "
                            f"{len(given)} map(s)", ref=str(out_path))
        return result
    except Exception as exc:
        return _fail(exc)


def _turnaround_frames(result: dict) -> list[str]:
    """The frame files this turnaround actually wrote, for the image blocks."""
    return [frame["path"] for frame in (result.get("renders") or [])
            if isinstance(frame, dict) and frame.get("exists") and frame.get("path")]


@_tool(images=_turnaround_frames)
def blender_turnaround(model: str, out_dir: str, stem: str = "turnaround",
                       width: int = 640, height: int = 960,
                       engine: str = "BLENDER_EEVEE_NEXT",
                       exposure: float = 0.0, timeout: int = 480) -> dict:
    """Render a model from four angles under a fixed rig — and JUDGE each frame.

    THE FRAMES COME BACK IN THIS RESULT AS IMAGES, not as paths you are trusted
    to go and open. Measured: four turnarounds of a correctly-coloured model
    came back white because the lights were far too hot, and were reported as
    finished without anybody opening them. The model was fine; the render was
    not, and nothing could tell the difference. Look at what you were handed,
    and read the verdicts — they are the half of the check you cannot argue with.

    Camera and three-point lighting are scaled to the subject's own bounding
    box, so a giant and a doll both frame correctly. Every frame returns a
    `blown`/`mean` reading and a verdict; `ok` is False when any frame is
    unreadable, and the verdict of the frame that failed is the `error`. A
    failing frame is a lighting problem, not a modelling one — do not go back
    and change the mesh because a render was white.

    Each frame is archived to the preview gallery and REGISTERED as a candidate
    artifact, so a turnaround can be handed to an independent reviewer by
    `artifact_id` (see art_qa_verdict) and shows up in the dashboard beside the
    2D work. Point out_dir INSIDE the project — frames written outside it cannot
    be registered, and an unregistered render is one nobody reviews.
    """
    try:
        result = _blender.turnaround(model, out_dir, stem=stem,
                                     size=(width, height), engine=engine,
                                     exposure=exposure, timeout=timeout)
        frames = [f for f in (result.get("renders") or []) if isinstance(f, dict)]
        registered = []
        for frame in frames:
            path = frame.get("path")
            if not path or not frame.get("exists"):
                continue
            label = str(frame.get("label") or "frame")
            archived = _archive_preview(path, f"{stem}-{label}")
            if archived:
                frame["preview"] = archived
            # One logical name PER ANGLE: a re-render after fixing the lights is
            # revision 2 of "hero-front", not a second unrelated artifact, which
            # is what lets a reviewer see that the white one was superseded.
            artifact = _register_artifact(
                f"{stem}-{label}", path, producer="blender_turnaround",
                metadata={"model": str(model), "angle": label,
                          "degrees": frame.get("degrees"),
                          "engine": engine, "exposure": exposure,
                          "blown": frame.get("blown"), "mean": frame.get("mean"),
                          "readable": bool(frame.get("ok")),
                          "verdict": frame.get("verdict") or "",
                          "preview": archived or ""})
            if artifact:
                frame["artifact"] = artifact
                frame["artifact_id"] = artifact["id"]
                registered.append(artifact["id"])
        if registered:
            result["artifact_ids"] = registered
        elif frames:
            result["artifact_note"] = (
                "no artifact was registered for these frames — out_dir is "
                "outside the project root, so art QA and the dashboard cannot "
                "see them; re-render into the project to put them on the ledger")
        if frames:
            unreadable = len(result.get("unreadable") or [])
            _log("render", f"turnaround {stem!r}: {len(frames)} frames"
                           + (f", {unreadable} unreadable" if unreadable else ""),
                 ref=str(out_dir))
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_generate(image: str, out_path: str, backend: str = "",
                     label: str = "", timeout: int = 900,
                     dry_run: bool = False, parts: bool = False,
                     options: Optional[dict] = None) -> dict:
    """Turn ONE generated image into a draft mesh. The other way to get geometry.

    The primitive path (blender_run + the kit) is for props, vehicles, terrain
    and block-out — things made of boxes and cylinders. It tops out at a
    proportioned blockout with no face and no fingers, so a hero character
    seen close up comes from here instead: generate the plate with
    image_generate, then hand it over.

    WHAT COMES BACK IS A DRAFT, NOT AN ASSET. Expect dense, unpredictable
    topology, no armature, no unit convention, and possibly baked lighting in
    the texture. It has to be scaled to 1.8 m, faced +Y, cleaned, unwrapped
    and weighted to a skeleton before blender_combine will make anything of
    it — bg_human's rig is the one to weight it to. `draft` is True in the
    result and `next_steps` says so; there is no path straight to
    godot_deliver_asset and that is deliberate.

    Nothing runs until you configure a backend (see .env.example) — this
    machine ships no model and downloads none. blender_status reports what is
    reachable. A local backend costs nothing per generation; a hosted one is
    priced before it submits, and `dry_run=True` returns that quote plus the
    licence verdict without spending anything.

    LICENCE IS PART OF THE RESULT. A local server is only a transport, so the
    model must be declared (BGATE_LOCAL_MODEL) — undeclared reads as unknown,
    never as permission. Some grants exclude whole territories and some
    forbid commercial use outright, which is a shipping problem rather than a
    technical one, so read `licence` before building on the mesh.

    parts=True ASKS FOR A BODY IN PIECES, and for a character it is the better
    request. A monolithic generation gives one blob — measured on a real user's
    asset, 940 disconnected shells with no relationship to anatomy — and bone
    heat then has to guess where the arm stops and the torso starts, which is
    how fingers end up weighted to a hip. A part-aware graph returns a head, a
    torso, arms and legs as SEPARATE meshes, and every step after it gets
    easier: `out_path` is read as a DIRECTORY, the result carries `parts` and a
    `combine` list ready for blender_combine, and a run that comes back with
    one mesh is flagged rather than reported as a success.

    It needs its own workflow (BGATE_COMFY_PARTS_WORKFLOW) whose saver writes
    one file per part. Without it this says so instead of quietly falling back
    to the monolith, because a silent fallback here is indistinguishable from
    the feature working.
    """
    try:
        from bgate_adapters import imageto3d as _i3d
    except Exception as exc:
        return _fail(exc)
    try:
        root = _root()
    except Exception:
        root = None                        # modelling before project_init is allowed
    try:
        plate = _i3d.check_input(image)
        if not plate.get("ok"):
            return {"ok": False, "error": plate.get("reason", "unusable plate"),
                    "input": plate}
        picked = backend or ("comfy-parts" if parts else
                             (_i3d.choose(root) or {}).get("backend", ""))
        if parts and not _i3d.supports(picked, "parts"):
            return {"ok": False,
                    "error": f"backend {picked!r} does not generate parts — "
                             "the part-aware path needs a graph exported to "
                             "BGATE_COMFY_PARTS_WORKFLOW that saves each part "
                             "separately",
                    "capabilities": _i3d.capabilities(picked)}
        if not picked:
            return {"ok": False, "error": "no image-to-3D backend is configured "
                    "— see .env.example; blender_status reports what is reachable",
                    "status": _imageto3d_summary()}
        opts = dict(options or {})
        quote = {"backend": picked,
                 "usd": _i3d.price_for(picked, **{k: v for k, v in opts.items()
                                                  if k in ("texture", "quad", "rig")}),
                 "licence": _i3d.model_licence(_i3d.declared_model())}
        if dry_run:
            return {"ok": True, "dry_run": True, "quote": quote,
                    "input": plate, "next_steps": list(_i3d.NEXT_STEPS)}
        if parts:
            got = _i3d.generate_parts(image, out_path, backend=picked,
                                      root=root, timeout=float(timeout),
                                      logical_name=label, **opts)
            got.setdefault("quote", quote)
            # EVERY PART REGISTERED, not just the first. A part left
            # unregistered is invisible to the dashboard and to art QA, and an
            # unreviewed limb is exactly the one that ships wrong.
            if got.get("ok") and root:
                registered = []
                for part in got.get("parts") or []:
                    try:
                        registered.append(_register_artifact(
                            root, part["path"],
                            f"{label or _Path(out_path).name}_{part['name']}",
                            producer="blender_generate", refs=[str(image)],
                            metadata={"backend": picked, "draft": True,
                                      "part": part["name"],
                                      "licence": got.get("licence")
                                                 or quote["licence"],
                                      "plate": str(image)}))
                    except Exception:
                        pass
                got["artifacts"] = registered
            return got
        got = _i3d.generate(image, out_path, backend=picked, root=root,
                            timeout=float(timeout), logical_name=label,
                            **opts)
        got.setdefault("quote", quote)
        # generate() names the written file `path`, the same key every other
        # adapter here returns. This asked for `out_path` — the name of THIS
        # function's argument, never a key on the result — so the guard was
        # always false and the mesh landed on disk unregistered: invisible to
        # the dashboard and to art QA, which is the one failure a generated
        # draft must not have.
        if got.get("ok") and root and got.get("path"):
            try:
                got["artifact"] = _register_artifact(
                    root, got["path"], label or _Path(out_path).stem,
                    producer="blender_generate", refs=[str(image)],
                    metadata={"backend": picked, "draft": True,
                              "licence": got.get("licence") or quote["licence"],
                              "plate": str(image)})
            except Exception:
                pass                       # a mesh on disk beats a bookkeeping raise
        return got
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_sweep(out_path: str, dry_run: bool = True,
                  keep_renders: bool = True) -> dict:
    """Delete a finished asset's intermediate layer files, keeping the record.

    A character run leaves a per-layer .glb each, a .blend rig, the assembled
    asset and its renders — fourteen files for one request. This removes the
    layer sources listed in that asset's manifest and NOTHING ELSE, so a
    neighbouring asset's layers survive.

    Kept: the assembled file, its manifest, the renders. What was removed is
    written back into the manifest, so the run's history outlives its files and
    a single layer can still be identified and rebuilt later.

    Defaults to dry_run=True. Look at the list, then call again with
    dry_run=False.
    """
    try:
        return _blender.sweep(out_path, dry_run=dry_run,
                              keep_renders=keep_renders)
    except Exception as exc:
        return _fail(exc)


def _manifest_layers(asset: str) -> dict:
    """The assembled manifest's per-layer record, by name. {} if unreadable.

    Read BEFORE re-assembling: combine rewrites the manifest at the same path,
    so the tri counts and object lists a re-run is compared against exist only
    until the moment it succeeds.
    """
    try:
        doc = _json.loads(_blender.manifest_path(asset).read_text(encoding="utf-8"))
        return {str(layer.get("name", "")): layer
                for layer in (doc.get("layers") or [])}
    except Exception:
        return {}


@_tool
def blender_layer_rerun(asset: str, layer: str, script: str = "",
                        source: str = "", kit: bool = True,
                        out_path: str = "", timeout: int = 300) -> dict:
    """Rebuild ONE layer of an assembled asset and re-assemble it. Not the
    character — the layer.

    "Re-run that one layer, not the whole character" is the promise the layered
    3D path is built on, and until this tool existed there was no way to keep
    it: the recipe lived in the manifest and nothing read it back, so a bad cap
    meant re-modelling, re-texturing and re-assembling everything beside it.
    blender_combine names the layer that failed (`checks`: unbound,
    unweighted_verts, and the per-layer tri counts) — this is what you do with
    that name.

    `asset` is the ASSEMBLED .glb (the manifest sits beside it). `layer` is the
    layer name as blender_combine reported it. Then ONE of:
      script   bpy source for that layer, run and exported over the layer's own
               file. The modelling kit is injected (kit=True) exactly as in
               blender_run, and the script is recorded beside the layer so the
               next re-run has it.
      source   a .glb/.gltf/.blend you already built — used in place, nothing
               is run.
      neither  the layer's RECORDED script is re-run. After blender_sweep the
               layer files are gone and this is the recovery path: each swept
               layer's manifest entry carries the script that built it. If the
               file is still on disk and no script is given, it is reused as-is.

    Everything else — placement, rotation, scale, binding, decal_on, which layer
    holds the rig, the root name — comes back off the manifest untouched. A
    layer put back at the origin unrotated is a different asset, which is why
    those arguments are recorded rather than re-typed.

    Refuses BEFORE spending time in Blender when another layer's source is
    missing, and names those layers: combine would otherwise assemble happily
    around the hole and hand back a character with no arms. Re-run those first.

    The re-assembled file is registered under the SAME logical name, so it is
    revision N+1 of the asset a reviewer already saw, not a new one. Returns the
    combine result plus `changed` — the layer's tri and object counts before and
    after — so "did that fix it" is a number rather than an impression.
    """
    try:
        recipe = _blender.manifest_recipe(asset)
        parts = [dict(part) for part in recipe.get("parts") or []]
        names = [str(part.get("name", "")) for part in parts]
        index = next((i for i, name in enumerate(names) if name == layer), -1)
        if index < 0:
            return {"ok": False, "error": (
                f"{layer!r} is not a layer of {_Path(asset).name} — this asset's "
                f"layers are: {', '.join(n for n in names if n) or 'none'}")}
        target = parts[index]
        before = _manifest_layers(asset).get(layer, {})
        recorded = {entry.get("name", ""): entry
                    for entry in recipe.get("missing") or []}

        # 1. Every OTHER layer has to be on disk, or the assembly quietly loses
        #    it — combine assembles happily around the hole and hands back a
        #    character with no arms, ok=True. Refuse FIRST, before a rebuild
        #    spends minutes in Blender on an assembly that cannot happen, and
        #    say which of the missing ones still carry a script.
        gone = [part for i, part in enumerate(parts)
                if i != index and not _Path(str(part.get("path") or "")).is_file()]
        if gone:
            return {"ok": False, "error": (
                "cannot re-assemble: "
                + "; ".join(
                    f"layer {part.get('name')!r} has no file at "
                    f"{part.get('path')} ("
                    + ("its script is in the manifest — re-run it too"
                       if recorded.get(part.get("name", ""), {}).get("script")
                       else "and the manifest recorded no script for it")
                    + ")" for part in gone))}

        # 2. Put the layer's file back, by whichever of the three routes applies.
        built: dict = {}
        if source:
            replacement = _Path(source)
            if not replacement.is_file():
                return {"ok": False, "error": f"no such layer file: {source}"}
            target["path"] = str(replacement.resolve())
            rebuilt = "file"
        else:
            text = script or (recorded.get(layer, {}).get("script")
                              or _blender.read_layer_record(
                                  target.get("path", "")).get("script", ""))
            if text:
                built = _blender.run_script(text, export_glb=target["path"],
                                            kit=kit, timeout=timeout)
                if not built.get("ok"):
                    return {**built, "ok": False, "layer": layer,
                            "stage": "layer",
                            "error": built.get("error")
                                     or f"the script for layer {layer!r} failed"}
                rebuilt = "script"
            elif _Path(target.get("path", "")).is_file():
                rebuilt = "reused"
            else:
                return {"ok": False, "error": (
                    f"layer {layer!r} has no file at {target.get('path')!r} and "
                    "the manifest recorded no script for it — pass script= to "
                    "rebuild it, or source= to point at a file you already have")}

        out = str(out_path or asset)
        # The SAME name the first assembly used, so the re-run supersedes it
        # rather than sitting beside it as an unrelated asset.
        root_name = recipe.get("root_name", "") or _Path(asset).stem
        result = _blender.combine(parts, out, rig=recipe.get("rig", ""),
                                  root_name=root_name, timeout=timeout)
        after = next((part for part in (result.get("parts") or [])
                      if part.get("name") == layer), {})
        result.update({
            "layer": layer, "rebuilt": rebuilt, "source": target.get("path", ""),
            "asset": out,
            "layer_run": {k: built.get(k) for k in ("ok", "seconds", "print")
                          if k in built},
            "changed": {
                "tris_before": before.get("tris"), "tris_after": after.get("tris"),
                "objects_before": before.get("objects") or [],
                "objects_after": after.get("objects") or [],
                "bound_before": before.get("bound"),
                "bound_after": after.get("bound"),
            },
            "reassembled": [name for name in names if name],
        })
        if result.get("ok"):
            artifact = _register_assembly(
                result, out, root_name=root_name, rig=recipe.get("rig", ""),
                producer="blender_layer_rerun")
            if artifact:
                result["artifact"] = artifact
                result["artifact_id"] = artifact["id"]
            _log("blender", f"re-ran layer {layer!r} ({rebuilt}) and re-assembled "
                            f"{_Path(out).name}", ref=out)
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_sprites(base_script: str, poses: list[dict], name: str = "sprite",
                    width: int = 128, height: int = 128,
                    engine: str = "BLENDER_EEVEE_NEXT", fps: float = 8.0,
                    res_dir: str = "assets/sprites", out_dir: Optional[str] = None,
                    timeout: int = 420) -> dict:
    """Render a Blender-built character as a transparent 2D sprite set.

    THE 2D art path: build the model once in base_script (bpy; lights included —
    camera optional, an auto-framed ORTHO one is added if missing), then each
    pose in poses=[{"name","script"}] tweaks the scene and renders one frame.
    Output: per-pose PNGs + <name>_sheet.png + <name>_frames.tres (a Godot
    SpriteFrames with one animation per pose) ready for an AnimatedSprite2D via
    godot_import_asset into res_dir. Rendered sprites cannot drift between
    poses the way hand-drawn ones do — same rig, camera, light every frame.

    A pose script that errors fails only that pose; check `failed` in the result.
    The sheet is archived to the preview gallery.
    """
    try:
        out = out_dir or str(_Path(_root()) / ".bgate_out" / "sprites")
    except Exception:
        out = out_dir or "sprites_out"
    try:
        result = _sprites.render_sprites(base_script, poses, out_dir=out,
                                         name=name, size=(width, height),
                                         engine=engine, fps=fps,
                                         res_dir=res_dir, timeout=timeout)
        if result.get("ok"):
            archived = _archive_preview(result["sheet"], f"sprites-{name}")
            if archived:
                result["preview"] = archived
            artifact = _register_artifact(
                name, result["sheet"], producer="blender_sprites",
                metadata={"poses": [p.get("name", "") for p in poses],
                          "frames": result.get("frames", {}),
                          "failed": result.get("failed", []),
                          "engine": engine, "preview": archived or "",
                          "fps": fps,
                          "animations": result.get("animations", {}),
                          "sequence": result.get("sequence")})
            if artifact:
                result["artifact"] = artifact
            _log("sprites", f"rendered {len(result['frames'])} sprite frames "
                            f"for {name!r}" +
                            (f" ({len(result['failed'])} failed)" if result["failed"] else ""),
                 ref=result["sheet"])
        return result
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Painted art (gpt-image)
# ---------------------------------------------------------------------------
@_tool
def image_status() -> dict:
    """Is the painted-art leg usable — hosted, local, or neither?

    Reports BOTH legs, because "no API key" stopped meaning "no art". The
    hosted answer checks the key without exposing it; the local answer says
    whether a ComfyUI on this machine is reachable and configured, what model
    was declared, and what that model's licence permits — which is the question
    that decides whether the output can ship in a game you sell.
    """
    try:
        root = _root()  # triggers .env load
        from bgate_adapters import imagegen

        legs = {}
        legs["openai"] = dict(imagegen.available())
        # KREA IS A FIRST-CLASS PROVIDER AND THIS TOOL DID NOT KNOW IT EXISTED.
        # It probed OPENAI_API_KEY alone and answered for the whole painted-art
        # leg, so a project holding a working Krea key — which image_generate
        # will happily auto-select — was told the leg was unavailable. It cost a
        # support cycle in a real run. blender_status has always reported
        # per-backend; this is that shape.
        try:
            from bgate_adapters import krea
            legs["krea"] = dict(krea.available(root))
        except Exception as exc:
            legs["krea"] = {"available": False,
                            "reason": f"{type(exc).__name__}: {exc}"}
        try:
            from bgate_adapters import localgen
            legs["local"] = dict(localgen.status(probe=True))
        except Exception as exc:
            legs["local"] = {"available": False,
                             "reason": f"{type(exc).__name__}: {exc}"}

        usable = [name for name, leg in legs.items() if leg.get("available")]
        return {
            # `available` answers about the LEG, not about one adapter: any
            # usable provider means painted art is available. A caller that only
            # reads this key gets the honest answer now.
            "available": bool(usable),
            "providers": usable,
            "auto_picks": (usable[0] if usable else ""),
            "legs": legs,
            "reason": "" if usable else
                      "no image provider is configured — set OPENAI_API_KEY or "
                      "KREA_API_KEY in the project's .env, or configure a local "
                      "ComfyUI (see the local leg's `how`)",
        }
    except Exception as exc:
        return _fail(exc)


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
    image_edit — so obeying the brief meant knowing a path the brief never
    stated. A pin already knows where it lives; this is the tool asking.
    """
    want = str(spec or "").strip().lower()
    if not want:
        return [], []
    kind = None if want in _PINNED_ALL else want
    pins = _refs.list_refs(root, kind=kind)
    if not pins and not kind:
        raise LookupError(
            "this project has no pinned references — use_pinned asked for "
            "anchors that do not exist. Pin the approved art with ref_pin, or "
            "drop use_pinned to generate unconditioned deliberately.")
    if not pins and kind:
        kinds = sorted({p.get("kind", "") for p in _refs.list_refs(root)})
        raise LookupError(
            f"no pinned reference of kind {kind!r} — pinned kinds here: "
            f"{', '.join(k for k in kinds if k) or 'none'}. Pass "
            f"use_pinned='all', a kind that exists, or pin one with ref_pin.")
    chosen = pins[:_PINNED_REF_CAP]
    return ([p["name"] for p in chosen], [p["path"] for p in chosen])


def _pick_provider(asked: str = "") -> str:
    """Which image provider to use: what was asked for, else what is CONFIGURED.

    Defaulting to a constant is how this broke. `image_generate` was pinned to
    openai, so a project holding only KREA_API_KEY — a key `.env.example` and
    the setup docs both tell people to set — got "OPENAI_API_KEY not set" from
    the one tool most likely to be reached first, while krea sat configured and
    unused two functions away.

    An explicit argument always wins, including when its key is missing: the
    caller gets that provider's own error, which names the key to set, rather
    than a silent substitution that generates against a model they did not ask
    for and bills them for it.
    """
    asked = (asked or "").strip().lower()
    if asked:
        return asked
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("KREA_API_KEY"):
        return "krea"
    # Neither configured: return the historical default so the error a caller
    # sees is the familiar "OPENAI_API_KEY not set", not a surprise about a
    # provider they never mentioned.
    return "openai"


@_tool
def image_generate(prompt: str, filename: str, size: str = "1024x1024",
                   quality: str = "medium", transparent: bool = False,
                   ref_images: Optional[list[str]] = None,
                   use_pinned: str = "", anchors: Optional[list[str]] = None,
                   task_kind: str = "", tileable: bool = False,
                   ref_strength: float = 0.5, provider: str = "",
                   model: str = "") -> dict:
    """Generate PAINTED art — portraits, select-screen cards, title splashes,
    textures, decals, stage paint-overs. Costs real money per image
    (~$0.02-0.19).

    provider  "" picks from what is CONFIGURED — openai if OPENAI_API_KEY is
              set, else krea if KREA_API_KEY is. Name one to force it, and you
              get that provider's own error if its key is missing rather than a
              silent substitution that bills you for a model you did not ask
              for. This was pinned to openai, so a project holding only a Krea
              key could not reach this tool at all while krea sat configured
              and unused.
    model     provider-specific; "" takes that provider's default.

    Division of labor: use blender_sprites for anything needing the SAME
    character across multiple frames (an image model can't hold a rig steady);
    use this for one-off illustrated pieces and for the maps that go onto 3D
    layers via blender_texture.

    CONDITIONING ON THE PINNED REFERENCES IS PART OF THIS TOOL NOW. It used to
    take no reference at all while every seat brief said to generate "against
    the pinned refs", so the instruction could only be obeyed by switching to
    image_edit or by not obeying it:
      ref_images   pin NAMES (see ref_list — preferred) or absolute paths.
                   `name@r2` reaches an older revision.
      use_pinned   pull the project's own anchors with NO paths passed by hand:
                   a ref kind (character | style | ui | concept) or "all".
                   Capped at the first 4 pins, explicit ref_images first.
      anchors      extra images used ONLY to choose the key colour, never sent
                   to the model — the identity whose palette the chroma must
                   avoid colliding with.
      ref_strength how hard a reference pulls (Krea-side; 0-1).

    task_kind names WHAT IS BEING MADE and changes real decisions, not wording:
      texture   forced square (a non-square map stretches across a unit UV and
                nothing downstream can undo it), given the flat-albedo clause —
                no baked light, no camera angle — and not keyed, because the
                surface IS the whole frame. Pair with tileable=True for a
                repeating field; the seam guarantee is a mirrored post-pass, not
                a sentence in the prompt.
      decal     a logo, wordmark or insignia, where THE TEXT IS THE SUBJECT.
                Keyed to real alpha like a sprite, with the one variant of the
                background contract that does not forbid lettering. Do not pass
                transparent=True for these; the keying is automatic.
      anchor/animation/item/sprite/... keyed sprite work.
      background/tile/ui/concept      full-bleed plates, never keyed.
    Omit it and nothing changes: keying then follows `transparent` exactly as
    it always did.

    transparent=True does NOT ask the API for alpha — measured, gpt-image
    answers that request with a gradient. It runs the KEYABLE-BACKGROUND
    contract instead: flat chroma backdrop, keyed out, then audited. A cut that
    comes back haloed/bled FAILS with the flag rather than being handed back as
    a sprite with dirty alpha.

    filename is relative to the project's .bgate_out/art/ (e.g. "tommy_portrait.png").
    The result is archived to the preview gallery — LOOK at it before importing
    into the game with godot_import_asset.
    """
    try:
        root = _Path(_root())
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
        # openai, so a user whose only key is KREA_API_KEY — a key the setup
        # docs tell them to configure — could not reach this tool at all, and
        # Krea images were only obtainable through image_sprites and
        # image_talkhead, the two tools that happened to expose `provider`.
        # chroma.generate has dispatched to either since it was written; the
        # tool simply never passed the choice along.
        result = _chroma.generate(prompt, str(out), provider=_pick_provider(provider),
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
        if result.get("ok"):
            archived = _archive_preview(result["path"], f"art-{_Path(filename).stem}")
            if archived:
                result["preview"] = archived
            artifact = _register_artifact(
                _Path(filename).stem, result["path"], producer="image_generate",
                model=result.get("model", ""), prompt=prompt, refs=named,
                metadata={"size": size, "quality": quality,
                          "transparent": transparent,
                          "task_kind": task_kind, "tileable": tileable,
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
    except Exception as exc:
        return _fail(exc)


@_tool
def image_edit(prompt: str, ref_images: list[str], filename: str,
               size: str = "1024x1536", quality: str = "medium",
               transparent: bool = False) -> dict:
    """Generate an image CONDITIONED ON reference image(s) — the consistency
    primitive, exposed raw. Use it to regenerate a single sprite pose against a
    character's existing reference (~$0.04 at medium) instead of re-buying the
    whole set, or to derive variants that must stay on-model.

    ref_images: PINNED REFERENCE NAMES (see ref_list — preferred) or absolute
    paths. filename lands under the project's .bgate_out/art/. Result is
    archived to the gallery — LOOK at it. transparent=True runs the
    keyable-background contract (flat chroma backdrop -> keyed -> audited), not
    the API's background=transparent, which does not reliably return alpha.
    """
    try:
        root = _Path(_root())
        out = _art_out(root, filename)
        from bgate_adapters import imagegen
        resolved = [_refs.resolve(root, r) for r in ref_images]
        result = _chroma.generate(prompt, str(out), provider="openai",
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
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# The item-art pipeline — item-as-object, class-templated, Codex-drivable.
# Variants are cheap and classes are expensive: one prompt template per class
# holds framing/light/scale/background invariant, a parameter grid mints the
# variants. See bgate_core/items.py for the taxonomy and the pure builders.
# ---------------------------------------------------------------------------
@_tool
def item_classes() -> dict:
    """The item-art taxonomy: the classes, their equip slot, and the variant
    axes. This IS the contract to drive item_generate / item_variants — read it
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
    Naming a character with no profile raises — silently minting unstyled gear
    would LOOK like a result."""
    if not character.strip():
        return ""
    for key in (character, f"{character}-character"):
        profile = _refs.profile_get(root, key)
        if profile:
            return profile.get("style", "")
    raise ValueError(
        f"no visual profile for {character!r} — set one with profile_set "
        "(or drop the character param to mint unstyled)")


def _index_item(root: _Path, man: dict) -> bool:
    """Upsert one manifest into .bgate_out/items/_index.json — the one-shot
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
    if not index:  # first write or corrupt — rebuild from the loose manifests
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

    Gear is a LAYER — it hangs on a fighter, so its background is not part of
    the asset. That makes it sprite-shaped and it goes through the keyable
    contract: the items STYLE clause asks for "fully transparent background",
    which is a wish no model in either provider grants."""
    from bgate_adapters import imagegen
    rel = _items.rel_art_path(spec["item_class"], spec["name"])
    out = root / rel
    result = _chroma.generate(spec["prompt"], str(out), provider="openai",
                              task_kind="item", quality=quality, root=root,
                              logical_name=spec["name"],
                              work_item_id=_work_item_id())
    if not result.get("ok"):
        return {"ok": False, "name": spec["name"], "error": result.get("error"),
                "alpha": result.get("alpha"), "prompt": spec["prompt"]}

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
    return {"ok": True, "name": spec["name"], "item_class": spec["item_class"],
            "slot": spec["slot"], "sprite": rel,
            "manifest": _items.rel_manifest_path(spec["name"]),
            "indexed": indexed,
            "preview": archived or result["path"]}


@_tool
def item_generate(item_class: str, name: str, descriptor: str,
                  material: str = "", element: str = "", tier: str = "",
                  quality: str = "medium", character: str = "",
                  force: bool = False) -> dict:
    """Mint ONE gear/item icon — transparent, class-templated, tracked.

    item_class is one of item_classes() (main_hand, off_hand, head, body, feet,
    consumable, throwable, ranged). descriptor names the item ("curved saber").
    material/element/tier are the variant axes. `character` names a pinned ref
    with a visual profile (profile_set) — its style is appended so worn gear
    reads as the same set as the fighter it hangs on. An already-minted item
    (manifest on disk) is skipped, not re-bought; force=true regenerates.
    Costs real money per image (~$0.02-0.19 at `quality`). For a batch, use
    item_variants. LOOK at the preview before importing into the game."""
    try:
        root = _Path(_root())
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
                        "note": "already minted — manifest exists; pass "
                                "force=true to re-buy"}
        res = _mint_item(root, spec, quality)
        if res.get("ok"):
            res["estimated_cost_usd"] = _items.estimate_cost(1, quality)
            res["style_rail"] = bool(style_clause)
            _log("art", f"minted {item_class} item {spec['name']}",
                 ref=res["preview"])
        return res
    except Exception as exc:
        return _fail(exc)


@_tool
def item_variants(item_class: str, base_name: str, descriptor: str,
                  materials: Optional[list[str]] = None,
                  elements: Optional[list[str]] = None,
                  tiers: Optional[list[str]] = None,
                  quality: str = "medium", limit: int = 12,
                  character: str = "", force: bool = False) -> dict:
    """Mint a BATCH of variants of one class from a parameter grid — the
    cartesian product of the axes you pass, each a self-contained item.

    This is the "plethora of gear, easily" engine: pass materials=[...],
    tiers=[...], elements=[...] and get one on-set icon per combination.
    `character` names a pinned ref with a visual profile — its style is woven
    into every prompt so the whole set matches the fighter that wears it.
    Already-minted variants (manifest on disk) are skipped and reported, so a
    re-run finishes a batch instead of re-buying it; force=true re-buys.
    Every image costs money, so `limit` caps what a run may BUY (default 12) —
    the plan and its $ estimate are reported and refused if new images exceed
    the cap, so you confirm the spend before it happens. LOOK at the set
    before importing."""
    try:
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
    except Exception as exc:
        return _fail(exc)


@_tool
def item_to_spriteframes(sprite: str, name: str, res_dir: str = "assets/gear",
                         frame_size: Optional[list[int]] = None) -> dict:
    """Wrap a single item PNG into a 1-frame Godot SpriteFrames .tres so it drops
    straight into an equip slot — the bridge from the item pipeline to the
    equip/layer system (templates/2d gear_rig.gd).

    A static held weapon/shield with one frame is the honest v1 for worn gear: it
    shows in-hand and rides the fighter's facing, before the per-frame worn-gear
    rig exists. sprite is a repo-relative or absolute PNG path. Emits the .tres
    next to the sheet the equip layer will load from res://<res_dir>/."""
    try:
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
        # The single frame IS the sheet — copy under the sheet name the tres
        # expects, so the pair imports together like every other SpriteFrames.
        from shutil import copyfile
        copyfile(src, out_dir / sheet_name)
        tres = _sprites._sprite_frames_tres(  # noqa: SLF001 — shared emitter
            sheet_name, [("default", 1)], (int(size[0]), int(size[1])),
            1.0, res_dir)
        tres_rel = out_dir / f"{slug}_frames.tres"
        tres_rel.write_text(tres, encoding="utf-8")
        return {"ok": True, "tres": _assets.normalize_path(root, tres_rel),
                "sheet": _assets.normalize_path(root, out_dir / sheet_name),
                "animation": "default", "res_dir": res_dir}
    except Exception as exc:
        return _fail(exc)


def vfx_animate(key_frame: str, name: str, motion: str = "burst",
                frames: int = 4, peak: int = 1, cell: Optional[list[int]] = None,
                fps: float = 14.0, res_dir: str = "assets/vfx",
                out_dir: str = "", loop: Optional[bool] = None,
                overrides: Optional[dict] = None) -> dict:
    """Turn ONE approved key frame into an effect ANIMATION, arithmetically.

    THE TOOL FOR PROJECTILE AND IMPACT VFX. Do NOT buy an effect animation as a
    grid of frames from an image model — that returns N INDEPENDENT DRAWINGS,
    not an animation, and the faults are not promptable away: a mug shatters and
    is intact again in frame 4, a cloud's palette pops mid-set, a "fading"
    effect ends at full opacity, a trail's frames point different ways. Identity
    over time is the one thing the model cannot hold and the one thing
    arithmetic gets free.

    THE WORKFLOW, in order:
      1. Generate ONE key frame — the effect at its PEAK, alone on the keyed
         backdrop, via image_generate/image_edit. One image, so you can LOOK at
         it and re-roll it cheaply.
      2. Call this. It derives every other frame from those pixels: frames
         before `peak` grow into it, frames after decay out of it. Frame 3 is
         provably the same art as frame 2 because it is made of it.
      3. Read `notes` in the result. They are findings, not decoration.

    Emits <name>_sheet.png + <name>_frames.tres through the same emitters the
    character pipeline uses, every frame registered to the cell centre — so the
    effect stacks on the projectile it belongs to without anyone computing an
    offset. `anchor` in the result is the pixel a runtime manifest should place.

    MOTIONS:
{motions}
    `peak` is which output frame the key frame IS. A burst drawn at its widest
    wants peak=1 of 4 — one frame snapping in, two coming apart.

    `overrides` tunes one motion's numbers (grow/expand/scatter/drift/fade/
    gravity/jitter/squash/chunk) without inventing a new one.

    COSTS NOTHING AND CALLS NO MODEL."""
    try:
        root = _Path(_root())
        rel = _assets.normalize_path(root, key_frame)
        src = root / rel
        if not src.exists():
            return {"ok": False, "error": f"no key frame at {rel}"}
        dest = (root / out_dir) if out_dir else src.parent
        res = _vfx.animate(
            str(src), str(dest), name, motion=motion, frames=int(frames),
            peak=int(peak), cell=tuple(cell) if cell else (64, 64),
            fps=float(fps), res_dir=res_dir, loop=loop, overrides=overrides)
        if not res.get("ok"):
            return res
        _register_artifact(name, res["sheet"], producer="vfx_animate",
                           refs=[str(src)],
                           metadata={"motion": motion, "frames": frames,
                                     "anchor": res["anchor"],
                                     "coverage": res["coverage"]})
        for key in ("sheet", "tres"):
            res[key] = _assets.normalize_path(root, res[key])
        res["frames"] = [_assets.normalize_path(root, p) for p in res["frames"]]
        return res
    except Exception as exc:
        return _fail(exc)


# The motion table is written ONCE, in bgate_core.vfx, and interpolated into the
# tool description here. This must happen BEFORE _tool is applied: functools.wraps
# copies __doc__ at decoration time and FastMCP reads it then, so a docstring
# built afterwards would never reach the model. (A `"""...""" % x` docstring is
# worse still — the % makes it an expression, so __doc__ is simply None and the
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
# rendering differs — so BATCH COHESION (each frame vs the batch median) is
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


def _palette_similarity(ref_path, frame_path) -> float:
    """Histogram intersection (0..1) of opaque-pixel colors."""
    return _hist_intersect(_palette_hist(ref_path), _palette_hist(frame_path))


def _vision_consistency(ref_path, frame_items, pass_floor=_CONSISTENCY_FLOOR):
    """Score generated frames against an approved reference for CHARACTER IDENTITY.

    Cheap pixel metrics (palette, silhouette) can't judge "same character" pose-
    invariantly, so this asks a vision model to score each frame 0-100 (identity
    only — pose/expression ignored). frame_items: list of (label, path). Returns
    {"ok": True, "frames": [{"label","score","reason","pass"}], "min", "flagged"}
    or {"ok": False, "error": ...} — callers must treat failure as non-blocking.
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
            "and expression WILL differ between frames — IGNORE those.\n"
            "Judge TWO things:\n"
            "(1) IDENTITY: score each frame 0-100 for being the SAME character as the "
            "reference (body proportions, art style, line weight, palette, defining "
            f"features). Score {pass_floor} or above ONLY when the frame could sit beside "
            f"the reference in the same sheet with no visible difference in build, palette "
            f"or line weight; below {pass_floor} means drift a player would notice.\n"
            "(2) FRAME-TO-FRAME CONSISTENCY: the frames must also look consistent WITH "
            "EACH OTHER — same build, proportions, weight, head size and style across the "
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
        # Deterministic palette gates — the vision judge kept passing
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
    """Structural gate for a freshly generated character reference — run BEFORE
    spending one edit per pose conditioned on it. gpt-image sometimes returns an
    'ok' result that is still unusable: a near-empty frame, or one whose
    background never keyed to transparent (a fully-filled rectangle). Identity
    can't be auto-judged with no ground truth, but 'is this a usable single
    transparent figure' can. Catching it here caps a broken run at ~1 spend
    instead of N poses that all inherit the flaw and all fail the pose gate.
    Returns (ok: bool, reason: str). Any checker error is treated as PASS — this
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
            return False, (f"near-empty reference (opaque coverage {cov:.3f}) — "
                           "the generation produced almost nothing")
        if cov > 0.93:
            return False, (f"background did not key to transparent (opaque "
                           f"coverage {cov:.3f}) — the reference is a filled "
                           "frame, not a cut-out character")
        return True, f"coverage {cov:.3f}"
    except Exception as exc:
        return True, f"sanity check skipped: {type(exc).__name__}"


# Chroma keying MOVED to bgate_core.chroma — it is a contract now, not a local
# trick. Generating on a solid backdrop the character never uses and keying it
# out is the ONLY way either provider yields alpha (gpt-image's transparent mode
# returns gradients and punches eye-whites to holes; Krea has no alpha parameter
# at all), so the picking + prompt clause + keying + audit had to live somewhere
# both providers can reach. These names stay as thin aliases: the sprite tool
# below and tests/test_mcp_adjust.py both call them.
_CHROMA = _chroma.CHROMA
_pick_chroma = _chroma.pick
_chroma_key = _chroma.key


@_tool
def image_sprites(character_prompt: str, poses: list[dict], name: str,
                  ref_image: Optional[str] = None, frame_width: int = 160,
                  frame_height: int = 240, quality: str = "medium",
                  ref_quality: str = "high", fps: float = 8.0,
                  res_dir: str = "assets/sprites", max_retries: int = 1,
                  max_cost_usd: float = 0.0, timeout: int = 300,
                  max_seconds: int = 1800, provider: str = "openai",
                  model: str = "", ref_strength: float = 0.6) -> dict:
    """PAINTED sprite set — REFERENCE-FIRST for consistency.

    provider: "openai" (gpt-image, default) or "krea". They condition on the
    reference in genuinely different ways, and it changes what you get:
    gpt-image EDITS the reference image, which holds identity hard but drags
    the reference's own lighting along; krea takes it as a STYLE REFERENCE at
    `ref_strength`, which follows an art style more faithfully and holds a
    specific face less. Pick per job; `model` names the exact model within the
    provider (see bgate_adapters.krea.MODELS) or defaults per provider.


    How it works (and why): a fresh generation invents a new character every
    time, and asking for many poses in one image comes back misaligned. So:
    (1) generate ONE reference character (or pass ref_image to reuse an approved
    one — reusing the ref is also how you REGENERATE a single pose later without
    changing the fighter); (2) each pose is an EDIT conditioned on that
    reference — same character, new stance; (3) frames are alpha-trimmed,
    bottom-centered, stitched into <name>_sheet.png + <name>_frames.tres (one
    animation per pose) — drop-in for AnimatedSprite2D.

    character_prompt: the character + art style (full body, single character —
    framing/transparency contracts are appended automatically).
    poses: [{"name": "jab", "description": "lead fist fully extended right,
    body driving forward"}] — name becomes the animation; description is the
    stance. LOOK at the reference preview before the poses run wild, and at the
    sheet preview before importing. Cost: 1 ref + 1 edit per pose (~$0.04-0.25
    each by quality). Failed poses are listed, never silently shipped.

    THIS IS THE MOST EXPENSIVE TOOL HERE and it is capped like it. The plan is
    priced before anything is bought and REFUSED if it exceeds max_cost_usd (or,
    unset, the work item's ceiling / the project's per_item_usd) or the project
    /day budget; the running tally is re-checked before every pose, so a retry
    storm stops mid-set instead of discovering the overrun on the invoice.
    `timeout` bounds ONE image call and `max_seconds` the whole run — past the
    deadline the remaining poses are reported as skipped and whatever was made
    is still assembled, because half a sheet plus a reason beats a hung call.

    Returns the assembled sheet result, or {ok: false, stage, error} when the
    spend gate, the reference gate or every pose fails.
    """
    try:
        if not poses:
            raise ValueError("poses list is empty")
        for p in poses:
            if "name" not in p:
                raise ValueError(f"each pose needs a 'name': {p}")
        root = _Path(_root())
        art_dir = root / ".bgate_out" / "art" / name
        from bgate_adapters import imagegen, sprites as _sp

        # PRICE THE RUN BEFORE BUYING ANY OF IT. One reference (skipped when an
        # approved ref_image is reused) plus one edit per pose, at this call's
        # qualities. Retries are deliberately NOT in the estimate — they are
        # bounded per pose and caught by the running check below; pricing the
        # worst case up front would refuse healthy runs.
        ceiling = _run_ceiling(str(root), max_cost_usd)
        per_pose = imagegen.price_per_image(quality)
        projected = round(
            (0.0 if ref_image else imagegen.price_per_image(ref_quality))
            + per_pose * len(poses), 4)
        refused = _spend_gate(
            str(root), projected,
            f"painting {len(poses)} poses for {name!r}", ceiling)
        if refused:
            return {**refused, "poses_attempted": 0, "name": name}
        deadline = _time.monotonic() + max(60, int(max_seconds))
        call_timeout = float(max(30, int(timeout)))

        # The stored visual identity, if one exists — injected into EVERY
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

        # 1. The reference — the single source of who this character is.
        result: dict = {"poses_attempted": len(poses),
                        "profile_used": bool(profile)}
        # Rolled-up spend/latency for the WHOLE set (ref + every pose edit,
        # retries included). imagegen already charged the ledger per call; this
        # is what the sheet artifact carries so a reviewer sees what it cost.
        tally = {"estimated_usd": 0.0, "seconds": 0.0, "calls": 0}

        def _tally(r: dict) -> dict:
            tally["estimated_usd"] += float(r.get("estimated_usd") or 0.0)
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
                # background=transparent and take whatever came back — measured
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
                    # Archive the preview even for an alpha rejection — the
                    # whole value of failing loudly is that someone can LOOK at
                    # the backdrop the model painted instead of guessing.
                    result["reference_preview"] = _archive_preview(
                        ref_path, f"ref-{name}")
                return r

            def _ref_gate(r):
                """One verdict over both anchor gates: did the flat backdrop
                actually key clean (chroma audit), and is this a cut-out figure
                rather than a filled frame (structural sanity)?

                A provider failure is NOT this gate's business — it returns
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
            # for N poses. A broken reference makes every pose broken — every one
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
                                 "broken anchor — adjust character_prompt and retry."}
        result["reference"] = ref_path

        # 2. Each pose derives from the reference — same fighter, new stance.
        # ANCHOR + ROLLING conditioning: every edit carries (a) the character
        # ANCHOR — always present, so identity re-grounds each call and drift
        # can't compound telephone-style, (b) the PREVIOUS successful frame —
        # motion continuity, (c) for the closing frame of a multi-frame
        # animation, that animation's FIRST frame — so cycles loop smoothly
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
        # The keyable-background contract does the whole dance now — pick a key
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
                + " — identical design, colors, face, and art style. CRITICAL: "
                "keep the EXACT SAME BODY BUILD, musculature, height, weight, head "
                "size and limb proportions as the reference in EVERY frame — do NOT "
                "slim him down, bulk him up, change his muscle definition, or restyle "
                "the body between frames; ONLY the pose changes"
                f" — now in this stance: {desc}. ONE single full-body "
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
            """Why this run must not start another paid call — or "" to go on.
            Checked before EVERY pose: the estimate up front is a plan, and a
            plan is not a cap once retries and a slow API get involved."""
            if _time.monotonic() >= deadline:
                return (f"run deadline reached ({max_seconds}s) after "
                        f"{tally['calls']} image calls")
            if ceiling and tally["estimated_usd"] + next_cost > ceiling:
                return (f"run ceiling reached (~${tally['estimated_usd']:.2f} "
                        f"spent of ${ceiling:.2f})")
            return ""

        for pose in poses:
            pname = pose["name"]
            desc = pose.get("description", pname)
            pose_desc[pname] = desc
            stop = _stop_reason(per_pose)
            if stop:
                # Stop BUYING, don't abort: the poses already painted still
                # assemble into a partial sheet, and the caller is told exactly
                # which ones never ran and why.
                pose_errors.append({"name": pname, "error": f"skipped — {stop}"})
                continue
            anim, _, idx = pname.partition("/")
            out_png = str(art_dir / f"pose_{pname.replace('/', '_')}.png")
            refs = [ref_path]
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
        # anchor — see _rolling_refs) up to max_retries, keeping whichever roll
        # scores best. This turns the gate from "detects drift" into "converges
        # on a consistent sheet".
        import shutil as _shutil
        pose_order = [p for p, _ in pose_files]
        pose_path = {p: fp for p, fp in pose_files}

        def _rolling_refs(pname):
            """Reconstruct the ANCHOR+ROLLING ref list a pose was first built
            with, so a RETRY keeps motion continuity. Re-rolling on the bare
            anchor (the old behavior) optimizes the gate's identity metric while
            silently dropping the cross-frame conditioning — a re-rolled mid-cycle
            frame could score better on identity yet pop out of the walk. The gate
            doesn't measure motion, so nothing caught it. Rebuild: anchor, plus
            the cycle's first frame for a closing frame, plus the previous frame."""
            anim, _, idx = pname.partition("/")
            refs = [ref_path]
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

        def _assemble_and_gate():
            asm = _sp.from_pose_images(
                [(p, pose_path[p]) for p in pose_order],
                out_dir=str(root / ".bgate_out" / "sprites"), name=name,
                frame_size=(frame_width, frame_height), res_dir=res_dir, fps=fps,
                ref_path=ref_path)
            asm.setdefault("failed", [])
            asm["failed"].extend(pose_errors)
            cons = {"ok": False}
            if asm.get("ok"):
                fm = asm.get("frames", {})
                cons = _vision_consistency(ref_path, [(p, fp) for p, fp in fm.items()])
            return asm, cons

        assembled, consistency = _assemble_and_gate()
        best_min = consistency.get("min") if consistency.get("ok") else None
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
                        {"name": pname, "error": f"regen skipped — {stop}"})
                    tries = 0
                    break
                bak = pose_path[pname] + ".bak"
                try:
                    _shutil.copy2(pose_path[pname], bak); backups[pname] = bak
                except Exception:
                    pass
                # Re-roll WITH the rolling refs, not the bare anchor — keep motion
                # continuity while the gate chases identity (see _rolling_refs).
                _edit_pose(pose_desc[pname], _rolling_refs(pname), pose_path[pname])
            asm2, cons2 = _assemble_and_gate()
            new_min = cons2.get("min") if cons2.get("ok") else None
            if new_min is not None and (best_min is None or new_min > best_min):
                best_min = new_min; assembled, consistency = asm2, cons2
                for bak in backups.values():
                    try: os.remove(bak)
                    except Exception: pass
            else:
                for pname, bak in backups.items():   # revert: this roll was no better
                    try: _shutil.copy2(bak, pose_path[pname]); os.remove(bak)
                    except Exception: pass
                assembled, consistency = _assemble_and_gate()

        assembled["reference"] = ref_path
        assembled["chroma"] = result.get("chroma")
        assembled["spend"] = {
            "estimated_usd": round(tally["estimated_usd"], 4),
            "image_calls": tally["calls"],
            "seconds": round(tally["seconds"], 2),
            "ceiling_usd": round(ceiling, 4) if ceiling else None,
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

            artifact = _register_artifact(
                name, assembled["sheet"], producer="image_sprites",
                prompt=character_prompt,
                refs=[str(ref_image)] if ref_image else [ref_path],
                metadata={"poses": poses, "frames": frame_map,
                          "failed": assembled.get("failed", []),
                          "preview": archived or "",
                          "consistency": consistency,
                          "sequence": assembled.get("sequence"),
                          "fps": fps,
                          "animations": assembled.get("animations", {}),
                          "seconds": round(tally["seconds"], 2),
                          "estimated_usd": round(tally["estimated_usd"], 4),
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
            cons_note = ""
            if consistency.get("ok"):
                cons_note = (f", consistency min {consistency.get('min')}"
                             + (f" — REGEN {consistency['flagged']}" if consistency.get("flagged")
                                else " (all pass)"))
            # THE GATE HAS TO GATE. Retries are exhausted by this point, so a
            # sheet still carrying flagged frames is the best this run will do
            # — and shipping it as ok=True is how "no outliers, min 80" reached
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
                    "installed as-is — tighten character_prompt on the drifting "
                    "detail, or lower the floor if this is as good as the model gets.")

            seq = assembled.get("sequence") or {}
            seq_note = (f", motion-jitter in {seq['flagged']}"
                        if seq.get("flagged") else "")
            _log("sprites", f"painted sprite set {name!r} (reference-first): "
                            f"{len(frame_map)}/{len(poses)} poses"
                            + (f", {len(assembled['failed'])} FAILED" if assembled["failed"] else "")
                            + cons_note + seq_note,
                 ref=assembled["sheet"])
        return assembled
    except Exception as exc:
        return _fail(exc)


@_tool
def image_talkhead(subject: str, name: str, anchor: str = "",
                   res_dir: str = "assets/portraits", cell: int = 128,
                   fps: float = 10.0, provider: str = "krea",
                   model: str = "", ref_strength: float = 0.7,
                   drift_limit: float = 0.0, max_retries: int = 2,
                   quality: str = "medium", timeout: int = 300) -> dict:
    """ANIMATED TALKING PORTRAIT: a face whose mouth moves while it speaks.

    Different asset class from image_sprites, and the difference is the point.
    A sprite set animates a BODY through space, so its frames differ by pose.
    This animates a FACE at rest: every frame is meant to be identical except
    the mouth, and "identical twice" is exactly what a generator will not give
    you. So the work here is holding everything still, not posing anything.

    Worth the four generations: a dialogue card showing a still bust reads as a
    picture of a character. The same bust with a mouth moving while the line
    types reads as the character talking to you.

    HOW IT HOLDS STILL, and each of these was learned by it not doing so:

      * ONE ANCHOR, N SIBLINGS. Every frame conditions on `anchor`, never on the
        frame before it. Chained conditioning drifts, and on a face drift is
        instantly legible: three frames in, the ears have moved and it is a
        different character. Pass a `ref_pin` name or a path as `anchor`; with
        none, the first frame generated becomes the anchor for the rest.
      * MOUTHS ARE GENERATED, NOT DERIVED. Elsewhere the rule is derive what you
        can, because a mirrored facing is a transform. There is no transform
        from a closed mouth to an open one.
      * REGISTERED ON SILHOUETTE WIDTH. Independent generations do not share a
        pixel grid, and a head that jumps two pixels reads as a flinch. Width is
        the rigid measurement; an open jaw grows the silhouette downward, so
        aligning on height shrinks the face every time it speaks.
      * DRIFT IS MEASURED AND RETRIED. "Same colours" in the prompt works about
        three times in four, which is the dangerous amount: the fourth comes
        back colour-shifted, invisible at 128px and obvious as flicker at 10fps.
        Any frame past `drift_limit` is regenerated up to `max_retries` times.

    Emits `<name>_talk.png` (4 cells: rest, half, wide, blink) and
    `<name>_talk.tres` with a looping `talk` animation over rest/half/wide/half
    and a one-shot `blink`. Blink is kept out of the cycle so it cannot land
    mid-syllable. Drop the .tres on an AnimatedSprite2D.

    Returns {ok, sheet, tres, frames:[{frame, drift, attempts}], worst_drift}.
    """
    try:
        from bgate_core import talkhead as _th

        root = _Path(_root())
        limit = float(drift_limit or _th.DRIFT_LIMIT)
        stage = root / ".bgate_out" / "art" / "talkheads" / name
        stage.mkdir(parents=True, exist_ok=True)

        # An anchor may be a pinned reference NAME or a path. Resolving the pin
        # here means an art agent uses the same anchor the rest of the pipeline
        # already agreed on, instead of inventing a second source of truth.
        anchor_path = ""
        if anchor:
            try:
                from bgate_core import refs as _refs
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
        # rather than the Write tool — so the PreToolUse lane hook never sees
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
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Godot
# ---------------------------------------------------------------------------
@_tool
def godot_status() -> dict:
    """Is Godot available, and which version? Check before engine work."""
    try:
        probe = _godot.available()
        return {**probe, **(_godot.version() if probe["available"] else {})}
    except Exception as exc:
        return _fail(exc)


@_tool
def godot_run(script: str, godot_project: Optional[str] = None,
              timeout: int = 120) -> dict:
    """Run a GDScript headless and capture its output.

    The script MUST `extends SceneTree`, do its work in `_init()`, and call
    `quit()` — without quit() it runs until the timeout. Returns stdout, stderr,
    and any parse/script errors (Godot prints SCRIPT ERROR and still exits 0, so
    check `errors`, not just the exit code).

    godot_project is the GODOT project directory (the one holding project.godot),
    not the Builders Gate root — that one is `project_dir`.
    """
    try:
        return _godot.run_script(script, project_dir=godot_project, timeout=timeout)
    except Exception as exc:
        return _fail(exc)


@_tool
def godot_templates() -> dict:
    """What project templates are available to scaffold."""
    try:
        return {"templates": _scaffold.list_templates()}
    except Exception as exc:
        return _fail(exc)


@_tool
def godot_scaffold(name: str, kind: str = "2d", dest: Optional[str] = None,
                   force: bool = False, replace: bool = False) -> dict:
    """Create a runnable Godot project wired for playtesting.

    kind: 2d (platformer slice) | 3d (first-person slice). dest defaults to
    <project root>/game.

    The template ships the BGate telemetry autoload already registered, and a
    player whose feel tunables (gravity, fall_multiplier, coyote_time) are both
    exported AND emitted on jump/land — so the first playtest already produces
    the telemetry join.

    A non-empty dest is refused unless force or replace, and THOSE TWO ARE NOT
    THE SAME THING:

      force=True    fill in WHAT IS MISSING. A file that already matches is left
                    alone; a file that differs is the user's and is SKIPPED, not
                    overwritten. This is the one to reach for to top up a
                    missing addon or a deleted script.
      replace=True  put the template back over the top, and copy each victim to
                    <name>.bak first.

    force used to mean what replace means now, and it was a data-loss bug in a
    feature's clothing: someone topping up one missing file lost their
    project.godot, their player.gd and their export_presets.cfg in place. That
    last one is unrecoverable in the usual case — the .gitignore this same
    template stamps excludes export_presets.cfg, so the customised export
    targets were not in git either.

    The result lists `created`, `unchanged`, `skipped` and `replaced`, so say
    what happened rather than letting the user find it in a diff.
    """
    try:
        target = dest or str(_Path(_root()) / "game")
        result = _scaffold.new_project(target, name, kind=kind, force=force,
                                       replace=replace)
        _log("scaffold", f"scaffolded {kind} project {name!r}", ref=result["path"])
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def godot_check_project(godot_project: str, timeout: int = 180) -> dict:
    """Import/validate a project headless — the 'does it still build' check.

    godot_project: the directory holding project.godot.
    """
    try:
        return _godot.check_project(godot_project, timeout=timeout)
    except Exception as exc:
        return _fail(exc)


@_tool
def godot_import_asset(godot_project: str, src_path: str, dest_rel: str = "assets",
                       timeout: int = 240) -> dict:
    """Bring an asset (e.g. a Blender .glb) into a project and VERIFY the engine loads it.

    Copies the file in, triggers a headless import, then loads the resource
    IN-ENGINE and reports the meshes Godot actually built — tri counts, UVs,
    materials, bounding box. Copying a file in is not integration: an asset that
    imports with zero surfaces is a silent failure, and this catches it by
    checking the engine's view, not the file's presence. The end of the
    Blender→Godot round trip.

    THE DESTINATION IS KEYED ON THE FILENAME ALONE, so a second `hero.glb` from
    a different output directory lands on the first one and wins. Keeping the
    existing .import and its uid is right — every .tscn in the project points at
    that uid — but the mesh underneath it has changed, and `replaced` in the
    result is where that is said. Read it before telling anyone the import was
    clean.

    godot_project: the directory holding project.godot.
    """
    try:
        result = _godot.import_asset(godot_project, src_path, dest_rel=dest_rel,
                                     timeout=timeout)
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
    except Exception as exc:
        return _fail(exc)


def _delivery_shot(result: dict) -> list[str]:
    """The in-engine frame this delivery captured, for the image block."""
    path = result.get("screenshot")
    return [path] if path else []


@_tool(images=_delivery_shot)
def godot_deliver_asset(godot_project: str, glb: str, name: str = "",
                        dest_rel: str = "assets", scene_rel: str = "scenes",
                        script_res: str = "", physics: str = "auto",
                        shape_type: str = "trimesh", body_type: str = "static",
                        character_body: str = "auto",
                        at: float = 1.2, min_size_m: float = 0.05,
                        max_size_m: Optional[float] = None,
                        nominal_size_m: float = 1.8, with_camera: bool = False,
                        overwrite_scene: bool = False, label: str = "",
                        timeout: int = 300) -> dict:
    """Take a finished .glb the rest of the way — into the engine, into a scene.

    THE STEP THE 3D PATH WAS MISSING. Everything before this ends at a file:
    blender_combine writes a .glb, and blender_turnaround photographs a BLENDER
    scene under BLENDER lights. Neither asks the engine anything, so a rig that
    did not import, a texture that did not travel, a 40x scale and an asset with
    no collider were invisible by construction. THIS is where an asset stops
    being a file and becomes a thing in the game: imported, given ONE collision
    strategy, instanced under the body its mesh implies in its own .tscn, stood
    on a lit floor, and photographed by Godot's own renderer.

    THE SCREENSHOT COMES BACK IN THIS RESULT AS AN IMAGE, and it is the first
    time anyone — you included — sees the asset under the renderer that will
    ship it. Look at it, then read `checks`: the measurements are the half you
    cannot argue with.

    `checks` is the gate. loads_in_engine, has_geometry,
    materials_carry_a_texture, real_world_size and has_collider are required;
    has_skeleton / has_animations / has_blend_shapes report without failing, so
    a prop is not marked broken for having no rig. A FAILING GATE STILL WRITES
    THE SCENE AND TAKES THE SCREENSHOT, deliberately — a 2880 m `giant_hero`
    fails real_world_size and you still get the frame, because a gate that hides
    the asset is one you cannot debug.

    THE BODY IS CHOSEN FROM WHAT THE MESH IS, and it decides the collider with
    it. Skinned (it has a skin, so a Skeleton3D and joints) → CharacterBody3D
    with a capsule fitted to the TORSO. Unskinned → StaticBody3D whose colliders
    the importer builds from the real geometry, and no capsule. Pass
    character_body="RigidBody3D" for a prop that should fall and be pushed, or
    any class name to override; `root_body` and `collision` in the result say
    what it became. Every asset used to be wrapped in CharacterBody3D — which
    only moves when code calls move_and_slide(), so a crate delivered that way
    never simulated at all — AND carried both an accurate trimesh and an
    invisible person-shaped capsule on two different bodies at once.

    physics: auto (the strategy above) | all (mesh shapes on every mesh, capsule
    stands down) | none (importer defaults, capsule is the collider). Leave
    max_size_m unset and the bound comes from what the asset IS: 4 m skinned,
    50 m otherwise.

    THE CAPSULE IS SIZED FROM THE TORSO, NOT THE POSE. It used to come off the
    widest horizontal axis of the merged bounds, so an A-pose handed it the ARM
    SPAN: a 1.75 m character shipped inside a 1.63 m wide cylinder that could
    not fit through a human door, and passed the gate because has_collider only
    counted shapes. has_collider now fails a capsule wider than half the figure
    it wraps.

    DELIVERING A .glb WHOSE FILENAME IS ALREADY IN assets/ OVERWRITES IT. The
    destination is keyed on the filename alone, so two different `hero.glb` from
    two different output directories collide and the second wins under the
    first's uid. `replaced` in the result names what was overwritten and whether
    the bytes actually differ.

    REDELIVERING DOES NOT CLOBBER THE SCENE. If <name>.tscn already exists its
    model ext_resource is repointed at the new import and the node tree is left
    exactly as the human left it — scripts, extra nodes, tweaked transforms all
    survive, and `scene_action` in the result says which happened (written /
    rewired / left_alone). Pass overwrite_scene=True to deliberately throw those
    edits away. The old behaviour rewrote the file every time, which during one
    session destroyed the same hand edit five times running.

    with_camera adds a first-person Camera3D to the character. OFF by default,
    and do not turn it on for anything you intend to instance into a level:
    Godot makes the first camera into the tree current, and an OBSERVED boot
    came up looking out of the delivered character's eye sockets instead of the
    player's. Turn it on only when this scene IS the player (templates/3d's
    player.gd requires a $Camera3D child).

    The frame is archived to the preview gallery and REGISTERED as an artifact
    (`artifact_id`), so art_qa_verdict can be pointed at the in-engine shot
    rather than at a Blender render of a Blender scene.

    godot_project: the directory holding project.godot. `glb`: the asset to
    deliver, e.g. the out_path blender_combine just wrote.
    """
    stem = name or _Path(glb).stem
    try:
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
                + ", ".join(failed) + " — the scene and the screenshot were "
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
                    "no artifact was registered for this frame — it was written "
                    "outside the project root, so art QA and the dashboard "
                    "cannot see it")
            _log("asset", f"delivered {stem} into the engine"
                          + (" (gate failed)" if not result.get("ok") else ""),
                 ref=archived or shot)
        return result
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Level generation
# ---------------------------------------------------------------------------
_WALL_LAYOUTS = ("blob47", "grid16", "solid", "none")
_EMPTY_SCENE = ('[gd_scene load_steps=1 format=3]\n\n'
                '[node name="{root}" type="Node2D"]\n')


def _terrain(layout: str, source: int, atlas_x: int, atlas_y: int,
             columns: int, name: str):
    """One of the built-in terrain layouts, or a refusal naming the choices."""
    if layout == "solid":
        return _autotile.Terrain.solid(source, (atlas_x, atlas_y), name=name)
    if layout == "grid16":
        return _autotile.Terrain.grid16(source, columns=columns,
                                        origin=(atlas_x, atlas_y), name=name)
    if layout == "blob47":
        return _autotile.Terrain.blob47(source, columns=columns,
                                        origin=(atlas_x, atlas_y), name=name)
    raise ValueError(f"layout {layout!r} is not one of {_WALL_LAYOUTS}")


def _res_pair(godot_project: str, path: str, suffix: str) -> tuple[_Path, str]:
    """A res:// path and its file on disk, from either form."""
    gd = _Path(godot_project).expanduser().resolve()
    if not (gd / "project.godot").is_file():
        raise ValueError(f"no project.godot in {gd} — that is not a Godot project")
    rel = path[len("res://"):] if path.startswith("res://") else path
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel.endswith(suffix):
        raise ValueError(f"expected a {suffix} path, got {path!r}")
    disk = (gd / rel).resolve()
    if gd not in disk.parents:
        raise ValueError(f"{path!r} points outside the Godot project")
    return disk, f"res://{rel}"


@_tool
def level_plan(width: int = 48, height: int = 32, seed: int = 0,
               min_leaf: int = 10, min_room: int = 4, margin: int = 1,
               max_depth: int = 5, corridor_width: int = 1) -> dict:
    """Lay out a room-and-corridor level and show it, WITHOUT touching a scene.

    BSP: cut the map in two until a piece holds one room, put a room in each
    piece, then join the two halves of every cut on the way back up. That join
    is the guarantee — it builds a spanning tree over the rooms, so every room
    is reachable from every other by construction rather than by luck. The
    result says `connected` and it is checked with a flood fill, not asserted.

    Read the `ascii` field. It is the fastest way to see that a level is one big
    room, or two halves joined by nothing, and it costs no engine and no
    screenshot. Iterate on `seed` here until the shape is right, THEN call
    level_generate with the same numbers to write it.

    Knobs that actually change the shape:
      seed            same seed, same level, forever.
      min_leaf        bigger -> fewer, larger rooms. Must be at least
                      min_room + 2*margin or nothing fits and it says so.
      max_depth       caps how many times the map is cut, so it caps room count.
      corridor_width  1 reads as a dungeon, 2+ as a complex.
      margin          gap between a room and its leaf's edge; 0 lets neighbouring
                      rooms fuse into one L-shaped cavity.
    """
    try:
        level = _levelgen.plan(width, height, seed=seed, min_leaf=min_leaf,
                               min_room=min_room, margin=margin,
                               max_depth=max_depth,
                               corridor_width=corridor_width)
        return {"ok": True, "seed": seed, "width": width, "height": height,
                "rooms": level["rooms"], "room_count": len(level["rooms"]),
                "corridor_count": len(level["corridors"]),
                "floor_cells": len(level["floor"]),
                "wall_cells": len(level["walls"]),
                "connected": level["connected"], "spawn": level["spawn"],
                "exit": level["exit"], "ascii": _levelgen.ascii_map(level)}
    except Exception as exc:
        return _fail(exc)


@_tool
def level_generate(godot_project: str, scene: str, tileset: str,
                   width: int = 48, height: int = 32, seed: int = 0,
                   floor_source: int = 0, floor_atlas_x: int = 0,
                   floor_atlas_y: int = 0,
                   wall_source: int = 0, wall_layout: str = "blob47",
                   wall_atlas_x: int = 0, wall_atlas_y: int = 0,
                   wall_columns: int = 8,
                   min_leaf: int = 10, min_room: int = 4, margin: int = 1,
                   max_depth: int = 5, corridor_width: int = 1,
                   parent: str = ".", floor_name: str = "Floor",
                   wall_name: str = "Walls", create: bool = False,
                   dry_run: bool = False) -> dict:
    """Generate a level and write it into a scene as TileMapLayer nodes.

    The whole chain: BSP layout -> neighbour-bitmask autotiling -> the packed
    binary Godot stores tiles in -> a .tscn edit, backed up. No engine and no
    editor involved, so it runs headless and is a normal reviewable diff.

    WHICH TILE GOES WHERE is decided by a neighbour bitmask, the same job the
    Godot editor's terrain sets do — and they only run in the editor, which is
    why it is redone here. `wall_layout` says how the wall sheet is arranged:

      blob47   8-bit mask, 47 tiles, row-major from (wall_atlas_x, wall_atlas_y),
               `wall_columns` wide, masks ascending. Sides plus corners.
      grid16   4-bit mask, 16 tiles, same layout rule. Sides only — right for a
               wall one cell thick.
      solid    one tile everywhere. No autotiling.
      none     no wall layer at all; floor only.

    THAT ORDER IS A CONVENTION, NOT A STANDARD. A sheet authored in Tilesetter
    or bought from an asset pack has its own order, and a wrong order draws a
    complete, confident, wrong-looking level. Check the first screenshot. If
    `unmapped` in the result is non-empty, the sheet is missing shapes the level
    needs and that field says which masks and how often — that is what to hand
    an artist.

    Re-running REPLACES the layers it wrote rather than adding more, so
    iterating on `seed` leaves one Floor and one Walls, not eight.

    godot_project: the directory holding project.godot.
    scene/tileset: res:// paths, or paths relative to that directory.
    """
    try:
        if wall_layout not in _WALL_LAYOUTS:
            raise ValueError(
                f"wall_layout {wall_layout!r} is not one of {_WALL_LAYOUTS}")
        scene_disk, scene_res = _res_pair(godot_project, scene, ".tscn")
        tiles_disk, tiles_res = _res_pair(godot_project, tileset, ".tres")

        if not tiles_disk.is_file():
            raise ValueError(f"no tileset at {tiles_res} — generate or import it "
                             "first; a level cannot pick tiles from nothing")
        parsed_set = _tilemap.parse_tileset(
            tiles_disk.read_text(encoding="utf-8", errors="replace"))
        have = sorted(parsed_set["sources"])
        wanted = {floor_source} | ({wall_source} if wall_layout != "none" else set())
        missing = sorted(w for w in wanted if w not in parsed_set["sources"])
        if missing:
            raise ValueError(
                f"{tiles_res} has no source {missing} — it has {have}. Source "
                "ids are not indexes; a tileset numbers them however it likes.")

        fresh = not scene_disk.is_file()
        if not fresh:
            text = scene_disk.read_text(encoding="utf-8", errors="replace")
        elif create:
            text = _EMPTY_SCENE.format(root=scene_disk.stem.title() or "Level")
        else:
            raise ValueError(
                f"no scene at {scene_res}. Pass create=true to start a new one, "
                "or point at an existing scene to add the layers to.")

        level = _levelgen.plan(width, height, seed=seed, min_leaf=min_leaf,
                               min_room=min_room, margin=margin,
                               max_depth=max_depth,
                               corridor_width=corridor_width)
        layers = _levelgen.layers(
            level,
            floor=_terrain("solid", floor_source, floor_atlas_x, floor_atlas_y,
                           1, floor_name),
            wall=(None if wall_layout == "none" else
                  _terrain(wall_layout, wall_source, wall_atlas_x, wall_atlas_y,
                           wall_columns, wall_name)),
            floor_name=floor_name, wall_name=wall_name)

        # THE CHECK THAT MATTERS. The built-in layouts are complete by
        # construction — every mask has an entry — so "unmapped" can only ever
        # catch a hand-written table. What actually goes wrong is the layout
        # pointing at atlas coordinates the SHEET does not define: Godot places
        # nothing there, reports nothing, and the level is invisible in exactly
        # the places the shape is most complicated. The .tres lists the tiles it
        # defines, so this is knowable before anything is written.
        absent = {}
        for layer in layers:
            want = {(c["source"], c["ax"], c["ay"]) for c in layer["cells"]}
            gaps = sorted(
                (ax, ay) for src, ax, ay in want
                if (ax, ay) not in set(map(tuple,
                                           parsed_set["sources"][src]["tiles"])))
            if gaps:
                absent[layer["name"]] = [list(g) for g in gaps]
        if absent:
            raise ValueError(
                f"{tiles_res} does not define these atlas tiles: "
                + "; ".join(f"{name} wants {coords}"
                            for name, coords in absent.items())
                + ". A cell pointing at an undefined tile draws nothing and "
                  "says nothing — add the tiles to the atlas, move the layout "
                  "with *_atlas_x/_atlas_y, or change wall_layout.")

        wired = _scenewire.wire_tilemap(text, tiles_res, layers, parent=parent)
        if dry_run:
            written = {"written": False, "backup": None}
        elif fresh:
            # A brand-new scene has no previous bytes to back up, and apply()
            # refuses a missing file on purpose — that refusal is what catches a
            # typo'd path everywhere else.
            scene_disk.parent.mkdir(parents=True, exist_ok=True)
            scene_disk.write_text(wired["text"], encoding="utf-8")
            written = {"written": True, "backup": None, "created": True}
        else:
            written = _scenewire.apply(scene_disk, wired["text"], root=_root())

        result = {
            "ok": True, "scene": scene_res, "tileset": tiles_res,
            "seed": seed, "size": [width, height],
            "rooms": len(level["rooms"]),
            "corridors": len(level["corridors"]),
            "connected": level["connected"],
            "spawn": level["spawn"], "exit": level["exit"],
            "layers": wired["layers"], "summary": wired["summary"],
            "written": written.get("written", False),
            "backup": written.get("backup"),
            "created": bool(written.get("created")),
            "dry_run": bool(dry_run),
            "ascii": _levelgen.ascii_map(level),
        }
        if not dry_run:
            _log("level", f"generated {width}x{height} level seed {seed} "
                          f"({len(level['rooms'])} rooms) into {scene_res}",
                 ref=scene_res)
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def godot_screenshot(godot_project: str, at: float = 1.0, scene: Optional[str] = None,
                     label: str = "", timeout: int = 120) -> dict:
    """Run the ACTUAL game and capture the viewport to a PNG at `at` seconds.

    The look-iteration loop: headless checks prove the game boots, this shows
    what it LOOKS like. A game window appears briefly on the user's screen
    (rendering needs a display) and closes itself after the capture. The shot
    is archived to the preview gallery — check it before and after visual work.

    godot_project: the directory holding project.godot.
    """
    try:
        # One file per capture. A single shot.png meant two seats screenshotting
        # at the same moment each got back a path holding the OTHER one's game.
        out = str(_Path(_root()) / ".bgate_out" / "shots" /
                  f"{_run_tag(label or 'game')}.png")
    except Exception:
        out = f"bgate_shot_{_run_tag()}.png"
    try:
        result = _godot.screenshot(godot_project, out, at=at, scene=scene,
                                   timeout=timeout)
        if result.get("ok"):
            archived = _archive_preview(result["path"], f"shot-{label or 'game'}")
            if archived:
                result["preview"] = archived
            _log("screenshot", f"captured the running game at t={at}s"
                               + (f" ({label})" if label else ""),
                 ref=archived or result["path"])
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def godot_inspect_resource(godot_project: str, res_path: str, timeout: int = 180) -> dict:
    """Load a res:// resource in-engine and report what it actually became.

    Meshes, tri counts, per-surface UV/material, bounding box — the engine's
    view of an asset already in the project.

    godot_project: the directory holding project.godot.
    """
    try:
        return _godot.inspect_resource(godot_project, res_path, timeout=timeout)
    except Exception as exc:
        return _fail(exc)


@_tool
def godot_retarget_check(godot_project: str, res_path: str,
                         bone_map_res: str = "", timeout: int = 180) -> dict:
    """Ask the ENGINE whether a rigged character is a humanoid it can retarget.

    The rigs this pipeline builds carry Godot's own SkeletonProfileHumanoid bone
    names, and the whole point of that is that any humanoid animation library
    then plays on the character. Nothing tested that claim until this tool. A
    .glb can export 23 perfectly-named bones in a FLAT hierarchy — blender_rig
    reports 0 unweighted, godot_deliver_asset photographs it happily, and the
    character can be animated by nothing except a clip authored for it alone.

    Three answers, and they fail independently:

      missing / extra   coverage against the profile, by exact name.
      chain[].propagates  rotating a shoulder moves the hand. This is the one
                        that catches a lost hierarchy, and it is invisible to
                        every other check in the product.
      clip.drives       a profile-authored rotation track actually turns the
                        bone. A NodePath that resolves to nothing plays
                        silently and moves zero.

    `retargetable` is the verdict. False means the humanoid animation ecosystem
    is unavailable to this asset — treat it the way you treat `rigged: false`.

    bone_map_res: a res:// path to save the BoneMap to, or "" to skip. Written,
    it is what the user's import settings point at to retarget real clips.

    res_path must already be imported — godot_import_asset first.
    """
    try:
        result = _godot.retarget_check(godot_project, res_path,
                                       bone_map_res=bone_map_res,
                                       timeout=timeout)
        if result.get("ok"):
            _log("godot",
                 f"retarget check {res_path}: "
                 f"{'retargetable' if result.get('retargetable') else 'NOT retargetable'} "
                 f"({result.get('mapped')}/{result.get('profile_bones')} profile bones)",
                 ref=res_path)
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def godot_evidence(godot_project: str, at: float = 1.0, scene: Optional[str] = None,
                   overlay: bool = True, label: str = "",
                   timeout: int = 120) -> dict:
    """Capture a frame PLUS a screen-space manifest of what is actually where.

    The upgrade over godot_screenshot. A PNG shows what the game looks like; it
    cannot tell you whether the health bar matches the fighter's real hp,
    whether a hitbox lines up with its sprite, or whether an entity is on
    screen at all. This runs the game the same way, then walks the live tree at
    capture time and reports every measurable node as screen-pixel bounds,
    visibility, z, and — for progress bars and labels — its RUNTIME VALUE.

    Returns beauty.png, an overlay.png with collision shapes (red) and other
    bounds (blue) stroked over the frame, and manifest.json with `entities` and
    `ui`. Pair with `causal_chains` — the manifest says what was on screen, the
    chains say why it happened.

    godot_project: the directory holding project.godot.
    """
    try:
        out_dir = str(_Path(_root()) / ".bgate_out" / "evidence" /
                      _run_tag(label or "frame"))
    except Exception:
        out_dir = f"bgate_evidence_{_run_tag()}"
    try:
        result = _godot.evidence(godot_project, out_dir, at=at, scene=scene,
                                 overlay=overlay, timeout=timeout)
        if result.get("ok"):
            for key, tag in (("beauty", "beauty"), ("overlay", "overlay")):
                path = result.get(key)
                if path:
                    archived = _archive_preview(
                        path, f"evidence-{tag}-{label or 'frame'}")
                    if archived:
                        result[f"{key}_preview"] = archived
            counts = result.get("counts", {})
            _log("evidence",
                 f"captured {counts.get('entities', 0)} entities / "
                 f"{counts.get('ui', 0)} ui elements at t={at}s"
                 + (f" ({label})" if label else ""),
                 ref=result.get("beauty_preview") or result.get("beauty") or "")
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def evidence_check_ui(manifest_path: str, expect: dict,
                      tolerance: float = 0.5) -> dict:
    """Assert HUD values from an evidence manifest against expected state.

    `expect` maps a UI node name to the value it should be showing, e.g.
    {"PlayerHealth": 92}. Numeric checks use `tolerance` so a bar mid-tween
    does not fail as a bug. This is the assertion godot_screenshot could never
    support: proof the HUD agrees with the sim, not a picture of a bar.
    """
    try:
        manifest = _json.loads(_Path(manifest_path).read_text(encoding="utf-8"))
        return _godot.check_ui_matches(manifest, expect, tolerance=tolerance)
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Scene editing — the node-level surgery the dashboard has always had
# ---------------------------------------------------------------------------
# bgate_core.scenewire has parsed and edited .tscn text since the Atlas builder
# shipped: load_steps accounting, ext_resource ids, name uniquing, block spans,
# a dry run on every mutation and a backup on every write. All of it was
# reachable from a browser and none of it from here, so an agent told to place a
# prop or repoint a texture hand-edited the file as TEXT — inventing ids,
# guessing at load_steps, and finding out at godot_check_project.
#
# These are the same functions the dashboard's /api/scene/* routes call, with
# the same dry-run and backup contract, plus one thing the routes did not have
# until today: the lock is honoured. That matters more here than there. A human
# clicking a button is one writer; the board runs several agents at once.


def _res_declared_type(asset_disk: _Path) -> Optional[str]:
    """The class a .tres declares itself to be. None for anything else.

    Guessing from the suffix calls every .tres a SpriteFrames — right for what
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

    A seat holding its OWN lock is not blocked by it — that is what taking the
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
    Nothing here writes when ``dry_run`` — it returns the resulting text so the
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
    """Read a scene's node tree — paths, types, roles, scripts, resources.

    THE READ THAT MAKES THE EDITS SAFE. Every other tool here addresses nodes by
    PATH ("Characters/Desk_12"), and this is where a path comes from. Guessing
    one costs a failed call; reading one costs nothing.

    FILTER BEFORE YOU LOOK. A hand-authored scene has thirty nodes and a baked
    floor plate has fifteen hundred — dumping that whole tree would bury the
    task in furniture. `match` is a substring of the node name or path, `role`
    is one of the roles the builder groups by (character, prop, visual, ui,
    collision, layer, camera, audio, controller, marker, instance), `parent`
    returns only what hangs under that node. `total` always reports the true
    count so a truncated answer says so.

    `properties` is off by default: property maps are the bulkiest part of a
    node and are only wanted once you know which node you mean.
    """
    try:
        scene_disk, scene_res = _res_pair(godot_project, scene, ".tscn")
        if not scene_disk.is_file():
            raise ValueError(f"no scene at {scene_res}")
        text = scene_disk.read_text(encoding="utf-8", errors="replace")
        nodes = _scenewire.outline(text)
        total = len(nodes)
        # Counted over the WHOLE scene, before filtering — "what is in here" is
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
    except Exception as exc:
        return _fail(exc)


@_tool
def scene_wire(godot_project: str, scene: str, asset: str,
               parent: str = ".", node_name: str = "", node_type: str = "",
               dry_run: bool = False, force: bool = False) -> dict:
    """Put an asset into a scene as a new node, wired correctly.

    The node type comes from the FILE, not from you: a .png becomes a Sprite2D,
    a SpriteFrames .tres an AnimatedSprite2D, a .tscn an instance. `node_type`
    overrides that when the default is wrong (a background .png that wants to be
    a TextureRect), and is otherwise better left alone.

    What this does that editing the text does not: allocates a non-colliding
    ext_resource id, reuses the existing one if the scene already references the
    file, bumps load_steps, and uniquifies the node name against its siblings —
    the four things a hand-written block gets wrong, three of which the engine
    reports as something else entirely.

    A .gd is not an asset here; a script attaches to a node that already exists,
    which is scene_attach_script.
    """
    try:
        asset_disk, asset_res = _res_pair(godot_project, asset, "")
        return _scene_edit(
            godot_project, scene,
            lambda text: _scenewire.wire(
                text, asset_res, node_name=node_name or None, parent=parent,
                node_type=node_type or None,
                res_type=_res_declared_type(asset_disk)),
            dry_run=dry_run, force=force,
            summary=f"wired {asset_res} into {scene}")
    except Exception as exc:
        return _fail(exc)


@_tool
def scene_unwire(godot_project: str, scene: str, node: str,
                 recursive: bool = False, dry_run: bool = False,
                 force: bool = False) -> dict:
    """Remove a node from a scene, and sweep any resource left referenced by nothing.

    Refuses a node that has children unless `recursive` — deleting a parent and
    silently orphaning its subtree is not a thing anyone means. Run it dry first
    if you are not certain what hangs off it; `scene_outline(parent=...)` says.
    """
    try:
        return _scene_edit(
            godot_project, scene,
            lambda text: _scenewire.unwire(text, node, recursive=recursive),
            dry_run=dry_run, force=force,
            summary=f"removed {node} from {scene}")
    except Exception as exc:
        return _fail(exc)


@_tool
def scene_node_add(godot_project: str, scene: str, name: str, node_type: str,
                   parent: str = ".", props: Optional[dict] = None,
                   dry_run: bool = False, force: bool = False) -> dict:
    """Add a plain node — a Camera2D, a Timer, a CanvasLayer, a grouping Node2D.

    A scene is not only the files in it. `props` sets properties in the same
    call, in Godot's own literal syntax where the type needs it:
    {"position": "Vector2(96, 40)", "z_index": 5, "visible": false}.
    """
    try:
        return _scene_edit(
            godot_project, scene,
            lambda text: _scenewire.add_node(
                text, name=name, node_type=node_type, parent=parent,
                props=props or {}),
            dry_run=dry_run, force=force,
            summary=f"added {node_type} {name} to {scene}")
    except Exception as exc:
        return _fail(exc)


@_tool
def scene_set_property(godot_project: str, scene: str, node: str, key: str,
                       value=None, clear: bool = False,
                       dry_run: bool = False, force: bool = False) -> dict:
    """Set one property on one node — position, z_index, visible, scale, a flag.

    THIS IS THE MOVE TOOL. "Put the desk two cells left" is this call with
    key="position". Vector and colour values are Godot literals passed as
    strings — "Vector2(320, 96)", "Color(1, 0.5, 0, 1)" — while numbers, bools
    and strings pass through as themselves.

    `clear=True` removes the property instead of setting it, which is how a node
    goes back to the class default rather than to a hardcoded copy of it.

    ON A GENERATED SCENE THIS IS THE WRONG FILE. If the .tscn header says it is
    bake output, the generator's input is the authority and your write survives
    exactly until the next bake. Read the top of the file before moving anything
    in it.
    """
    try:
        return _scene_edit(
            godot_project, scene,
            lambda text: _scenewire.set_property(
                text, node, key, None if clear else value),
            dry_run=dry_run, force=force,
            summary=f"set {node}.{key} in {scene}")
    except Exception as exc:
        return _fail(exc)


@_tool
def scene_swap_resource(godot_project: str, scene: str, node: str, asset: str,
                        property: str = "", dry_run: bool = False,
                        force: bool = False) -> dict:
    """Point a node at a different file — try that sheet, that music, that scene.

    By hand this is four steps (find the scene, add an ext_resource, retype the
    property, delete the resource that is now unused) and the fourth is the one
    everybody skips, which leaves the old asset looking referenced to every tool
    that counts references — including Atlas's dead-asset rail.
    """
    try:
        asset_disk, asset_res = _res_pair(godot_project, asset, "")
        return _scene_edit(
            godot_project, scene,
            lambda text: _scenewire.swap_resource(
                text, node, asset_res, prop=property or None,
                res_type=_res_declared_type(asset_disk)),
            dry_run=dry_run, force=force,
            summary=f"swapped {node} to {asset_res} in {scene}")
    except Exception as exc:
        return _fail(exc)


@_tool
def scene_attach_script(godot_project: str, scene: str, script: str,
                        node: str = ".", dry_run: bool = False,
                        force: bool = False) -> dict:
    """Attach a .gd to a node that already exists. Defaults to the scene root."""
    try:
        _, script_res = _res_pair(godot_project, script, ".gd")
        return _scene_edit(
            godot_project, scene,
            lambda text: _scenewire.attach_script(text, script_res, node=node),
            dry_run=dry_run, force=force,
            summary=f"attached {script_res} to {node} in {scene}")
    except Exception as exc:
        return _fail(exc)


@_tool
def scene_rename_node(godot_project: str, scene: str, node: str, name: str,
                      dry_run: bool = False, force: bool = False) -> dict:
    """Rename a node and repair every path in the file that named it.

    A rename is not a one-line edit: children carry their parent's path, and
    NodePath properties elsewhere in the scene point at the old name. Doing it
    by hand is how a scene loads with half its wiring pointing at nothing.
    """
    try:
        return _scene_edit(
            godot_project, scene,
            lambda text: _scenewire.rename_node(text, node, name),
            dry_run=dry_run, force=force,
            summary=f"renamed {node} to {name} in {scene}")
    except Exception as exc:
        return _fail(exc)


@_tool
def scene_reparent_node(godot_project: str, scene: str, node: str,
                        parent: str = ".", dry_run: bool = False,
                        force: bool = False) -> dict:
    """Move a node and everything under it beneath a different parent.

    Godot stores a node's transform LOCAL to its parent, and this moves the
    declaration, not the maths — a node reparented under something offset will
    land somewhere else on screen. Reparent for structure (into a YSort, onto a
    CanvasLayer), then fix position with scene_set_property.
    """
    try:
        return _scene_edit(
            godot_project, scene,
            lambda text: _scenewire.reparent(text, node, parent),
            dry_run=dry_run, force=force,
            summary=f"reparented {node} under {parent} in {scene}")
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Causal chains — DESIGN.md §8 over shipped telemetry, no engine required


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

    A log line says `whiffed reason=facing`. A causal chain says the attack was
    thrown, cleared its cooldown, reached contact, PASSED the range gate at
    dist=104 vs reach=115, and only then failed on facing — a completely
    different bug from failing on range, which the raw event cannot distinguish.

    Works on telemetry the game ALREADY emits: no engine, no new store, no
    change to the game. The inference is sound because resolution gates run in
    a fixed order, so the gate that failed implies every earlier one passed.

    `spec` names one of THIS PROJECT's chain specs (see `causal_specs`). The
    harness ships none — event kinds are your game's vocabulary, not Builders
    Gate's. Draft one from a telemetry file with `causal_infer_spec`.

    Filter with actor, outcome ("landed", "failed", "blocked", "refused",
    "aborted", "dropped", "unresolved"), failed_gate, or move.
    """
    try:
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
    except Exception as exc:
        return _fail(exc)


@_tool
def causal_specs() -> dict:
    """This project's chain specs, and whether each one's gate order is trusted.

    Read before trusting a chain. Every PASS in a chain is an INFERENCE from
    gate ordering, not an observation — sound only while the ladder matches the
    game's real resolution order. `order_verified: false` means nobody has
    checked it against the source yet, and chains from it mark passed gates
    with '~'.
    """
    try:
        specs = _causal.load_specs(_root())
        if not specs:
            return {"ok": True, "specs": {}, "count": 0,
                    "hint": "none defined for this project — run "
                            "causal_infer_spec against a telemetry file to "
                            "draft one from the events your game emits."}
        return {"ok": True, "count": len(specs),
                "specs": {name: _causal.describe_spec(s)
                          for name, s in specs.items()}}
    except Exception as exc:
        return _fail(exc)


@_tool
def causal_infer_spec(session: Optional[int] = None, telemetry_path: str = "",
                      name: str = "", family: str = "",
                      save: bool = False) -> dict:
    """Draft a chain spec by reading what your game actually emits.

    Bootstraps `causal_chains` for a game the harness has never seen. Clusters
    event kinds into pipelines by shared prefix, guesses the opener, finds the
    actor field, and collects the `reason` values it observes.

    It CANNOT infer the one thing that matters most: the ORDER of the gates.
    Order is a property of your resolution code, not of its telemetry, and the
    whole passed-gate inference rests on it. So the draft comes back
    `order_verified: false` — open your resolution function, put the ladder in
    the order it actually checks, add each gate's detail fields, then set
    order_verified true. Until you do, chains mark passed gates with '~'.

    save=True writes it to .bgate/causal_specs.json.
    """
    try:
        path = _telemetry_path(session, telemetry_path)
        result = _causal.infer_spec(_causal.read_events(path), name=name,
                                    family=family)
        if result.get("ok") and save:
            spec_name = next(iter(result["spec"]))
            spec = _causal.spec_from_dict(spec_name, result["spec"][spec_name])
            result["saved"] = _causal.save_spec(_root(), spec)
        result["telemetry"] = path
        return result
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Reference anchors
# ---------------------------------------------------------------------------
@_tool
def ref_pin(name: str, path: str, kind: str = "style", note: str = "") -> dict:
    """Pin an APPROVED image as a canonical reference anchor.

    The file is copied into .bgate/refs/ (durable, travels with the project)
    under the given name; every seat brief lists the pins, and image_edit /
    image_sprites accept pin names anywhere they accept paths. Pin a character's
    approved reference, the style anchor, concept mocks from the user — the
    things art must stay consistent WITH. Re-pinning a name upgrades the anchor
    in place. kind: character | style | ui | concept.
    """
    try:
        return _refs.pin(_root(), name, path, kind=kind, note=note)
    except Exception as exc:
        return _fail(exc)


@_tool
def ref_list(kind: Optional[str] = None) -> dict:
    """The pinned reference anchors. Check BEFORE generating character/style art."""
    try:
        return {"refs": _refs.list_refs(_root(), kind=kind)}
    except Exception as exc:
        return _fail(exc)


@_tool
def profile_set(name: str, traits: str, style: str, negative: str) -> dict:
    """Store a character's visual identity — written while LOOKING at the pinned
    reference, never from memory. Injected automatically into every
    image_sprites generation for this character, and consistency_check judges
    against it. traits = what the character IS; style = the rendering style
    every frame must hold; negative = what must never appear.
    """
    try:
        return _refs.profile_set(_root(), name, traits=traits, style=style,
                                 negative=negative)
    except Exception as exc:
        return _fail(exc)


@_tool
def profile_get(name: str) -> dict:
    """A character's stored visual identity (or {missing: true})."""
    try:
        got = _refs.profile_get(_root(), name)
        return got if got else {"missing": True, "name": name}
    except Exception as exc:
        return _fail(exc)


@_tool
def consistency_check(candidate_path: str, character: str) -> dict:
    """Judge a generated frame against its character — from a BUILT comparison,
    never from memory. Composes reference | candidate side-by-side on a
    checkerboard (alpha honesty), archives it to the gallery, and returns the
    profile checklist + a palette-drift tripwire. YOU then look at the
    composite and verdict each checklist line. A frame only lands if every
    line passes. This exists because three off-style batches were approved by
    agents judging frames in isolation.
    """
    try:
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

        # Palette tripwire (advisory — catches color drift, blind to identity).
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
                     "same rendering style (brushwork/detail level — no added "
                     "texture like fur, hair, etched lines)",
                     "same palette family", "no extra elements (glow, shadow, props)"]
        if profile:
            checklist.insert(0, f"matches traits: {profile['traits'][:160]}")
            checklist.insert(1, f"holds style: {profile['style'][:160]}")
            checklist.append(f"nothing from the negative list: {profile['negative'][:160]}")

        # ALPHA / TRANSPARENCY TRIPWIRE (automated — the palette check above is
        # blind to transparency because it samples only a>64). White halos,
        # feathered fringes, opaque background bleed, dirty RGB under zero alpha
        # and hollow interiors are what a checklist-by-eye keeps missing. The
        # measurements live in bgate_core.chroma.audit, which is the SAME code
        # the keyable path gates on at generation time — a frame cannot pass one
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
                                  "alpha) — regenerate; do not land it.")}
        try:
            _artifacts.record_check(
                _root(), candidate_path, "consistency", result)
        except Exception:
            pass
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def art_qa_verdict(artifact_id: int, verdict: str, score: int = 0,
                   reasons: str = "") -> dict:
    """Record an INDEPENDENT art-QA reviewer's verdict on a candidate artifact.

    For the art-consistency reviewer (a seat that did NOT make the image) after
    it has run consistency_check and looked at the produced image beside its
    reference. The score (0-100 similarity) and reasons are stored on the
    revision under metadata.qa_review so the dashboard can show why.

    verdict 'fail' REJECTS the revision outright — refusing to ship something is
    a call a machine is allowed to make alone. verdict 'pass' does NOT approve
    it: the pass is recorded and the revision stays a candidate, marked
    machine-checked and queued for a human to approve in the dashboard. An LLM's
    opinion is evidence; only a person promotes evidence to canon.

    Returns {ok, artifact_id, verdict, score, status, awaiting_human,
    logical_name, revision}. `status` is the revision's status AFTER the call:
    'rejected' on fail, still 'candidate' on pass.
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
                **({"next": "a human approves this revision in the dashboard — "
                            "art-QA cannot promote it to 'approved'"}
                   if awaiting else {})}
    except LookupError as exc:
        return _fail(exc)
    except Exception as exc:
        return _fail(exc)


@_tool
def ref_unpin(name: str) -> dict:
    """Remove a pin (the file itself is kept — deleting canon art is a human call)."""
    try:
        return _refs.unpin(_root(), name)
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Assets — locks for the files git can't merge
# ---------------------------------------------------------------------------
@_tool
def asset_lock(path: str, seat: str) -> dict:
    """Claim a binary asset for one seat BEFORE editing it.

    Binary files (.blend, .glb, textures, audio) don't merge — two agents editing
    one .blend loses someone's work. Lock first, edit, then asset_release. A held
    lock errors rather than queues: decide to wait, or work on something else.
    Lock-before-create is the normal flow for new assets.
    """
    try:
        bound_seat, owner = _lock_identity(seat)
        return _assets.lock(_root(), path, bound_seat, owner=owner)
    except Exception as exc:
        return _fail(exc)


@_tool
def asset_release(path: str, seat: str, force: bool = False) -> dict:
    """Release a lock when the edit is done — records the new content hash.

    Only the holding seat can release. force=True breaks anyone's lock (for a
    dead agent's stale claim) — a human's call, not a convenience.
    """
    try:
        if force:
            return _assets.force_release(_root(), path)
        bound_seat, owner = _lock_identity(seat)
        return _assets.release(_root(), path, bound_seat, owner=owner)
    except Exception as exc:
        return _fail(exc)


@_tool
def asset_track(path: str) -> dict:
    """Register an existing file under its content hash (sha256)."""
    try:
        return _assets.track(_root(), path)
    except Exception as exc:
        return _fail(exc)


@_tool
def asset_status(kind: Optional[str] = None, locked_only: bool = False) -> dict:
    """List tracked assets, optionally by kind or only the locked ones."""
    try:
        return {"assets": _assets.list_assets(_root(), kind=kind,
                                              locked_only=locked_only)}
    except Exception as exc:
        return _fail(exc)


@_tool
def asset_verify() -> dict:
    """Audit every tracked asset against disk — catches silent clobbers.

    'modified' means content changed with NO lock held: an unlocked write or an
    outside edit. Locked files are expected to differ and aren't drift. Run this
    before builds and after any multi-agent session.
    """
    try:
        return _assets.verify(_root())
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Iterations
# ---------------------------------------------------------------------------
@_tool
def iteration_status(limit: int = 10) -> dict:
    """Causal iteration history: snapshots, assets, playtests, decisions, work, outcome."""
    try:
        return {"iterations": _iterations.list_iterations(_root(), limit=limit)}
    except Exception as exc:
        return _fail(exc)


@_tool
def iteration_record_checks(status: str, summary: str = "",
                            checks: Optional[dict] = None) -> dict:
    """Attach automated-check results to the active iteration and next snapshot."""
    try:
        return _iterations.record_checks(
            _root(), {"status": status, "summary": summary,
                      "checks": checks or {}})
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Playtest
# ---------------------------------------------------------------------------
@_tool
def playtest_devices(filter_text: str = "") -> dict:
    """List mic inputs and open windows — pick what to record before starting."""
    try:
        return {
            "inputs": _recorder.list_inputs(),
            "windows": _recorder.list_windows(filter_text),
            "note": "pass an input 'index' as mic_device, and a window 'title' "
                    "as window_title",
        }
    except Exception as exc:
        return _fail(exc)


@_tool
def playtest_check(mic_device: Optional[int] = None,
                   window_title: Optional[str] = None,
                   native: bool = False) -> dict:
    """Preflight a session: ffmpeg, mic SIGNAL, transcriber, target window.

    ALWAYS run this before playtest_start. It records a short mic sample and
    measures level — a muted or unplugged mic records perfect digital silence,
    which looks identical to a working one until the transcript comes back empty
    and the whole playthrough is wasted.
    """
    try:
        return _playtest.preflight(
            mic_device=mic_device, window_title=window_title,
            root=_root(), native=native)
    except Exception as exc:
        return _fail(exc)


@_tool
def playtest_start(name: str, window_title: Optional[str] = None,
                   mic_device: Optional[int] = None, build_ref: str = "",
                   fps: int = 30, launch_native: bool = False,
                   game_cmd: str = "") -> dict:
    """Start recording a play session — game window video + your voice.

    Play the game and talk out loud about what you like and what needs changing.
    Say it near when it happens; feedback is matched to game events by timestamp.

    window_title: match the game window (None = whole desktop). build_ref: the
    commit/build under test. Set launch_native to let the backend launch Godot
    with BGATE_TELEMETRY already attached; game_cmd optionally overrides the
    default <root>/game project command.
    """
    try:
        return _playtest.start(_root(), name, window_title=window_title,
                               mic_device=mic_device, build_ref=build_ref, fps=fps,
                               launch_native=launch_native, game_cmd=game_cmd)
    except Exception as exc:
        return _fail(exc)


@_tool
def playtest_stop(session_id: Optional[int] = None, model: str = "base",
                  transcribe_now: bool = True) -> dict:
    """Stop recording, then transcribe, align, and classify feedback.

    Transcription runs a whisper model in a subprocess; expect roughly a minute
    per 10 minutes of audio on CPU (the first run also downloads the model).
    Items land as 'new' — nothing becomes work until you promote it.
    """
    try:
        return _playtest.stop(_root(), session_id, model=model,
                              transcribe_now=transcribe_now)
    except Exception as exc:
        return _fail(exc)


@_tool
def playtest_brief(session_id: int, include_transcript: bool = False,
                   window_s: float = 4.0) -> dict:
    """The session as agents should read it: video frames + feedback + telemetry.

    You CAN watch the recording: `video_frames` is an ordered strip of stills
    ({i, t, path}) sampled across the whole session — Read them in order to see
    what happened. Each feedback item also carries a frame at its own moment and
    the game events within window_s of it, and `transcript` is what the player
    said, timestamped. Line frames up with the transcript by t.
    """
    try:
        return _playtest.brief(_root(), session_id, window_s=window_s,
                               include_transcript=include_transcript)
    except Exception as exc:
        return _fail(exc)


@_tool
def playtest_list(status: Optional[str] = None) -> dict:
    """List play sessions. status: recording | processing | ready | failed."""
    try:
        return {"sessions": _playtest.list_sessions(_root(), status=status)}
    except Exception as exc:
        return _fail(exc)


@_tool
def playtest_promote(item_id: int, seat: Optional[str] = None,
                     kind: Optional[str] = None, ref: str = "") -> dict:
    """Accept a feedback item as real work, optionally re-routing it.

    This is the human's call. Do not promote items on the user's behalf without
    being asked — thinking out loud mid-play is not a decision to build.
    """
    try:
        return _playtest.promote(_root(), item_id, seat=seat, kind=kind, ref=ref)
    except Exception as exc:
        return _fail(exc)


@_tool
def playtest_dismiss(item_id: int) -> dict:
    """Drop a feedback item — noise, or already handled."""
    try:
        return _playtest.dismiss(_root(), item_id)
    except Exception as exc:
        return _fail(exc)


@_tool
def playtest_telemetry_contract() -> dict:
    """What the game must emit so spoken feedback becomes actionable numbers."""
    try:
        return _playtest.telemetry_contract()
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Seats — stable roles, write lanes, and the blackboard
# ---------------------------------------------------------------------------
@_tool
def seat_list() -> dict:
    """The project's seats: role, mission, write lanes. Adopt one before working."""
    try:
        return {"seats": list(_seats.roles_for(_root()).values())}
    except Exception as exc:
        return _fail(exc)


@_tool
def seat_brief(role: str) -> dict:
    """Everything a seat needs to start working, in one call.

    Mission, write lanes, the bible (with the scope cut applied), canon entities,
    the promoted playtest feedback routed to this seat, held/others' locks, and
    recent blackboard notes. Read this BEFORE doing seat work — it replaces
    re-deriving the project state from scratch.
    """
    try:
        return _seats.brief(_root(), role)
    except Exception as exc:
        return _fail(exc)


@_tool
def seat_can_write(role: str, path: str) -> dict:
    """May this seat write this path? Check BEFORE editing outside your obvious lane.

    Two gates, both must pass: the path must be inside the seat's write lanes,
    and the file must not be locked by another seat — being in-lane does not
    excuse stomping a locked binary. Fails closed for unknown/disabled seats.
    """
    try:
        return _seats.can_write(_root(), role, path)
    except Exception as exc:
        return _fail(exc)


@_tool
def seat_configure(role: str, enabled: Optional[bool] = None,
                   write_globs: Optional[list[str]] = None,
                   mission: Optional[str] = None) -> dict:
    """Override a seat for this project: change its mission, or (human only)
    its write lanes and enabled flag.

    `mission` is prose about what a seat should focus on and any caller may
    rewrite it. `write_globs` and `enabled` are PERMISSIONS, and an agent
    calling this is refused: write_globs=['**'] is a seat granting itself the
    whole repo, and enabled=false is a seat switching off the QA that would
    have caught it. A lane change that comes from a machine is not a lane
    system, it is a suggestion. Ask the human to make the change in the
    dashboard, or state the case in a work item and let them decide.

    Returns the merged seat {role, title, mission, write_globs, enabled}, or
    {ok: false, error} — including on the permission refusal, which is a normal
    result to read and route around, not a crash.
    """
    try:
        privileged = [name for name, value in
                      (("write_globs", write_globs), ("enabled", enabled))
                      if value is not None]
        if privileged and _caller_is_agent():
            raise PermissionError(
                f"{_actor() or 'an agent session'} may not change "
                f"{', '.join(privileged)} on seat {role!r} — write lanes and the "
                "enabled flag are a human's call, because a seat that can widen "
                "its own lanes has no lanes. Change the mission here if that is "
                "what you meant, or ask the human to edit the seat in the "
                "dashboard (Seats -> " + role + ").")
        return _seats.configure(_root(), role, enabled=enabled,
                                write_globs=write_globs, mission=mission)
    except Exception as exc:
        return _fail(exc)


@_tool
def seat_post_note(role: str, body: str, topic: str = "") -> dict:
    """Leave a note on the blackboard for other seats.

    Post when your work changes another seat's world: an asset re-exported, a
    tunable renamed, a scope call made. Short and factual beats long and vague.
    """
    try:
        return _seats.post_note(_root(), role, body, topic=topic)
    except Exception as exc:
        return _fail(exc)


@_tool
def handoff_note(kind: str, text: str, refs: Optional[list] = None) -> dict:
    """Record IN-FLIGHT state on the project thread, for the next session.

    The board says what was dispatched and the bible says what was settled.
    Neither says what you were halfway through, why you chose the thing you
    chose, or what you deliberately did not do — and that is what evaporates
    when a session ends. This is an append-only trail read back at the start of
    the next one, so a death costs a successor one read instead of an
    investigation.

    CALL IT AS YOU GO, not at the end. A closed window, a kill and a crash all
    fire nothing, and those are the sessions most worth resuming.

    kind:
      state     where things stand; what is half-done.
      decision  a call you made, WITH the reason. If it is settled canon it
                belongs in the bible — bible_add it and cite the section in
                `refs` rather than restating it here.
      deferred  something you chose NOT to do, and why. An unlabelled deferral
                is the most expensive thing to lose: the next agent finds it and
                "fixes" it as a bug.
      blocker   what is in the way, and who owns it.
      next      the very next action.

    refs: ids/paths this note points at — "bible#12", "item 41",
    "game/data/loot/floor_0.json". Cite, do not duplicate.
    """
    try:
        return _handoff.note(_root(), kind, text, refs=refs)
    except Exception as exc:
        return _fail(exc)


@_tool
def handoff_read(limit: int = 0, kind: str = "") -> dict:
    """The project thread, oldest first — what earlier sessions left behind.

    The SessionStart hook already injects the tail of this into every session, so
    reach for it when you need MORE than that: the whole history, or one kind
    (`deferred` before you "fix" something, `decision` before you re-litigate
    one). limit=0 is everything; a positive limit takes the most recent N.
    """
    try:
        trail = _handoff.read(_root(), limit=limit, kind=kind)
        return {"notes": trail, "count": len(trail),
                "path": str(_handoff.path_for(_root()))}
    except Exception as exc:
        return _fail(exc)


@_tool
def seat_notes(topic: Optional[str] = None, role: Optional[str] = None,
               limit: int = 20) -> dict:
    """Read the blackboard, newest first, optionally filtered by topic or role."""
    try:
        return {"notes": _seats.read_notes(_root(), topic=topic, role=role,
                                           limit=limit)}
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Work queue
# ---------------------------------------------------------------------------
@_tool
def queue_list(status: Optional[str] = None, seat: Optional[str] = None,
               limit: int = 40, full: bool = False) -> dict:
    """The work queue. status: queued | dispatched | done | failed.

    BRIEFS ARE PREVIEWS and the list is PAGED. This used to answer with every
    work item a project had ever had, brief text and all — on a real board that
    is 150,000 characters, which does not fit in a tool result at all: the call
    failed, the CLI spilled it to a file, and the agent spent its next two turns
    grepping a dump of its own queue instead of doing the work. A board is a
    list of titles you scan; the one brief you actually need comes from
    queue_get(item_id).

    Pass full=True only when you genuinely need brief text for several items at
    once, and keep the limit small when you do.
    """
    try:
        from bgate_core import queue as _q
        rows = _q.list_items(_root(), status=status, seat=seat)
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
            items.append(item)
        return {
            "items": items,
            "shown": len(items),
            "total": len(rows),
            "truncated": len(rows) > len(items),
            "note": ("briefs are previews — queue_get(item_id) returns one item "
                     "whole" if not full else "full briefs; keep limit small"),
        }
    except Exception as exc:
        return _fail(exc)


@_tool
def queue_get(item_id: int) -> dict:
    """One work item, whole — brief, result, lineage, cost, status.

    The other half of queue_list's preview: scan the board with the list, read
    the one item you are about to act on with this.
    """
    try:
        from bgate_core import queue as _q
        return _q.get(_root(), int(item_id))
    except Exception as exc:
        return _fail(exc)


@_tool
def queue_add(seat: str, title: str, brief: str = "", priority: int = 0) -> dict:
    """Queue work for a seat. Use when your work uncovers work that isn't yours."""
    try:
        from bgate_core import queue as _q
        return _q.add(_root(), seat, title, brief=brief, priority=priority,
                      source=f"seat:{_seat() or 'unknown'}")
    except Exception as exc:
        return _fail(exc)


@_tool
def queue_add_chain(links: list, chain_id: str = "") -> dict:
    """File DEPENDENT work as one ordered chain instead of N loose items.

    USE THIS WHENEVER THE SPLIT YOU JUST MADE HAS AN ORDER. The tell is a brief
    that has to say "AFTER #41 lands" or "this needs the scene from the tech
    item": if one agent must not start before another finishes, priority cannot
    express it. Priority is a preference among things that are all ready; a chain
    is what decides which are ready. Filed as separate items, both agents start
    in the same auto-deploy tick and the second writes against a file that does
    not exist yet — reports done, and the failure surfaces two items later
    looking like something else.

    ``links`` is an ORDERED list of dicts, each taking queue_add's fields:
    {"seat": ..., "title": ..., "brief": ..., "priority": ...}. Link N waits for
    link N-1 to reach 'done' — approved, if this project runs an approval gate.
    Chains are strictly linear; model a fan-out as separate chains that share a
    first link, or as one link whose brief covers both halves.

    WRITE EACH BRIEF AS IF ITS PREDECESSOR ALREADY LANDED, because it will have.
    Name what it produced (the file, the function, the scene) rather than saying
    "wait for it" — the waiting is now the board's job, not the brief's.

    Returns {chain_id, items: [...]} in running order. Nothing dispatches until
    `bgate serve` is up, exactly as with queue_add.
    """
    try:
        from bgate_core import queue as _q
        rows = _q.add_chain(
            _root(),
            [dict(link) for link in (links or [])],
            chain_id=chain_id, source=f"seat:{_seat() or 'unknown'}")
        return {"chain_id": rows[0]["chain_id"], "count": len(rows),
                "items": [{"id": r["id"], "seat": r["seat"], "title": r["title"],
                           "chain_pos": r["chain_pos"],
                           "depends_on": r["depends_on"]} for r in rows]}
    except Exception as exc:
        return _fail(exc)


@_tool
def queue_update(item_id: int, title: Optional[str] = None, brief: Optional[str] = None,
                 seat: Optional[str] = None, priority: Optional[int] = None) -> dict:
    """Edit an existing work item in place (title/brief/seat/priority).

    For enriching a ticket without re-filing it — e.g. rewriting a transcript-
    era brief to add the frames, timestamps, and telemetry you saw while
    watching the recording. Only the fields you pass change; status and lineage
    stay put. Pass the full new brief text (this replaces, it does not append).
    """
    try:
        from bgate_core import queue as _q
        return _q.update(_root(), item_id, title=title, brief=brief,
                         seat=seat, priority=priority)
    except Exception as exc:
        return _fail(exc)


@_tool
def queue_next(seat: str) -> dict:
    """The highest-priority queued item for a seat — what to work on next."""
    try:
        from bgate_core import queue as _q
        item = _q.next_for(_root(), seat)
        return item if item else {"empty": True, "seat": seat}
    except Exception as exc:
        return _fail(exc)


@_tool
def queue_complete(item_id: int, result: str, failed: bool = False) -> dict:
    """Close out a work item with an honest one-paragraph result.

    failed=True when the work did not land — say why plainly; a false 'done'
    poisons the queue's trustworthiness for everyone.

    WHAT "CLOSED" MEANS DEPENDS ON THE PROJECT'S APPROVAL GATE, and the returned
    row says which happened. Under the agent gate the item goes to 'done' and a
    QA agent is spawned to verify the claim; under the builder's gate it goes to
    'review' and waits for the human — you are finished either way, but anything
    chained behind it does not start until it reaches 'done'. Do not "fix" a
    'review' status by re-reporting: it is the gate working.
    """
    try:
        from bgate_core import queue as _q
        return _q.complete(_root(), item_id, result=result, failed=failed)
    except Exception as exc:
        return _fail(exc)


@_tool
def queue_reopen(item_id: int, reason: str) -> dict:
    """Send a done/failed item back to 'queued' for another round.

    The QA gate's FAIL path: reason is the ranked nitpick list (specific
    problems + fixes). It is APPENDED to the item's brief so the next
    dispatched agent reads exactly what to fix, and recorded as the result.
    """
    try:
        from bgate_core import queue as _q
        root = _root()
        item = _q.get(root, item_id)
        if item["status"] not in ("done", "failed"):
            raise ValueError(
                f"item {item_id} is {item['status']!r} — only done/failed "
                "items can be reopened")
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("reason is required — say exactly what to fix")
        stamp = ("\n\n--- REOPENED (QA gate) ---\n" + reason)[:3000]
        _q.update(root, item_id, brief=(item["brief"] or "") + stamp)
        return _q.set_status(root, item_id, "queued",
                             result=f"reopened: {reason[:1900]}")
    except Exception as exc:
        return _fail(exc)


@_tool
def agent_steer(item_id: int, text: str) -> dict:
    """Say something to the agent currently running a work item, mid-run.

    The director's other half. queue_add hands work OUT; this is how you correct
    it while it is happening — "that pose is off-model, use the pinned ref",
    "stop widening the scope, ship the three screens" — without killing the run
    and paying for it twice.

    The message is left in the project's steer inbox and delivered by the
    dashboard, which is the process that owns the agent's input pipe. So:

      * it needs `bgate serve` to be running to land;
      * the agent reads it when its CURRENT step ends, not instantly;
      * an item with no live agent gets no delivery — check queue_list(
        status='dispatched') first, and use queue_update or queue_reopen for
        work that is not running.

      * A STEER IS CAPPED AT 2000 CHARACTERS and a longer one is refused
        outright, not truncated. It is an interruption, not a brief: anything
        that needs more than a couple of paragraphs is a change to the work
        rather than a correction to it, so put it in queue_update's brief or
        reopen the item with it.

    Delivery, and any failure to deliver, is recorded in the activity ledger
    against the item.
    """
    try:
        from bgate_core import steerbox as _steerbox
        from bgate_core import queue as _q
        root = _root()
        item = _q.get(root, int(item_id))
        if item["status"] != "dispatched":
            return {"ok": False,
                    "error": f"item {item_id} is {item['status']!r}, not running "
                             "— there is no agent to steer",
                    "status": item["status"]}
        posted = _steerbox.post(root, int(item_id), text,
                                by=f"seat:{_seat() or 'director'}")
        return {"ok": True, "item_id": int(item_id), "steer_id": posted["id"],
                "delivery": "queued for the dashboard to hand over; the agent "
                            "reads it when its current step ends"}
    except Exception as exc:
        return _fail(exc)


@_tool
def ask_human(question: str, refs: Optional[list] = None) -> dict:
    """Ask the human who owns this project ONE question — and keep working.

    The director's ping. Use it for the calls that are genuinely not yours: which
    of two directions to take, whether a scope cut is acceptable, whether a thing
    you just finished is what they meant. It returns immediately and DOES NOT
    BLOCK — do not poll for the answer, and do not sit idle waiting for one. Say
    in your result note that you asked and what you assumed in the meantime.

    NOT A WORK ITEM, DELIBERATELY. A question that becomes a queued row is a row
    somebody has to dispatch in order to read, which is how "ask the human" turns
    into "spawn an agent to ask the human" — paid, laned, and still in front of
    nobody. This lands on the event bus instead, so it reaches the console card,
    the drawer and any notification channel the project has switched on.

    WHERE THE ANSWER COMES BACK depends on whether you are still running when it
    arrives, and you do not have to do anything either way:

      * still running -> it arrives as a steer, the same channel a mid-run
        correction uses; you read it when your current step ends;
      * already finished -> it is filed as a handoff `decision` note (so the next
        session reads it from one file) and attached to the question itself.

    Unanswered questions are reminded about ONCE, past notify.question_stale_h.
    So: ask one thing, make it decidable, and name the options — "A or B?" gets an
    answer, "any thoughts?" does not. `refs` are ids or paths the human should
    look at ("item 41", "bible#12", "game/scenes/hub.tscn"); cite, do not paste.
    """
    try:
        from bgate_core import steerbox as _steerbox
        root = _root()
        item_id = _work_item_id() or 0
        result = _steerbox.ask(root, question, refs=refs, item_id=item_id,
                               seat=_seat() or "director", by=_actor())
        _log("question", f"asked the human: {str(question)[:120]}",
             ref=str(item_id or result["seq"]))
        if item_id:
            arrives = ("as a steer into this run if you are still going when "
                       "they answer, otherwise as a handoff decision note for "
                       "the next session")
        else:
            arrives = ("as a handoff decision note — this session is not a "
                       "dispatched work item, so there is no run to steer")
        return {**result, "answer_arrives": arrives,
                "note": "returns immediately — do not wait for the answer"}
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Cutout characters: parts on a skeleton, animated once per template
# ---------------------------------------------------------------------------
# FOUR TOOLS, NOT SEVEN. The public tool list is a budget an agent reads in
# full, and the kit-generation and part-rerun verbs belong with the art
# generation path rather than here — they are labelled as not-yet-built rather
# than stubbed, so nobody calls one and gets a shrug.


def _cutout_dir(root: str, name: str) -> "_Path":
    """Where a character's document, parts and scene live.

    Inside game/assets/**, which is the ART SEAT'S EXISTING WRITE LANE. A bare
    characters/** would be out of lane for every seat and the PreToolUse hook
    would refuse the writes — the feature would be unusable by the seat that
    owns it.
    """
    return _Path(root) / "game" / "assets" / "characters" / name


@_tool
def cutout_templates() -> dict:
    """The cutout rig templates, and what a kit for each has to contain.

    A CUTOUT CHARACTER IS THE OTHER WAY TO ANIMATE IN 2D. The frame pipeline
    pays per character per animation — six clips at eight frames is 48 paid
    generations that all have to agree with each other, and a new hat means
    regenerating every one. A cutout kit is about ten parts, the animation is
    authored once per TEMPLATE and free forever after, and equipment is a
    texture swap on one slot.

    What it costs you: a puppet, not a painting. Parts are rigid Sprite2Ds on
    Node2D bones — no mesh deformation, no squash, no per-frame redraw. For a
    hero seen in close-up the frame pipeline is still the better answer.

    `parts_to_generate` is the actual generation list: the far-side limbs reuse
    the near-side drawings with a tint, which is what makes a side-view kit ten
    images instead of sixteen.
    """
    try:
        from bgate_core import cutout as _cutout
        return {"ok": True, "templates": _cutout.templates(),
                "layout": "game/assets/characters/<name>/",
                "not_built_yet": [
                    "cutout_kit_generate — generating the parts themselves "
                    "still goes through image_generate/chroma by hand against "
                    "a pinned reference",
                    "cutout_part_rerun — regenerate one part in place",
                ]}
    except Exception as exc:
        return _fail(exc)


@_tool
def cutout_assemble(name: str, parts: dict, template: str = "biped_v1",
                    adjustments: Optional[dict] = None, notes: str = "",
                    force: bool = False) -> dict:
    """Build a cutout character from its parts and emit a scene that moves.

    `parts` maps SLOT -> image path (absolute, or relative to the project root).
    cutout_templates lists the slots. Anything you leave out simply does not get
    a sprite, so a half-generated kit assembles and shows what it has.

    What comes out: `<name>.cutout.json` (the rig document — the editable
    thing), `<name>.tscn` (bones, sprites, z order, an AnimationPlayer) and
    `<name>.anims.tres` (six clips, baked onto THIS character's rest pose).
    Drop the .tscn into a scene and call play("walk") on it.

    THE FAR SIDE IS FREE. Slots ending _far reuse the matching _near image with
    a tint unless you pass them explicitly, so a side-view kit is ten images.

    `adjustments` nudges the template per character — {"arm_near": {"rot": -8}}
    — and those survive every clip, because the animation is baked as deltas on
    top of them rather than as absolute poses.

    It REFUSES to overwrite a .tscn that has changed since it last wrote one
    (someone opened it in Godot, or edited it). `force=True` discards those
    changes deliberately.
    """
    try:
        from bgate_core import cutout as _cutout, cutoutwire as _wire
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
                        'call play("walk") on it — the rig script is on the root',
                        "connect its anim_event signal for hit frames"]}
    except Exception as exc:
        return _fail(exc)


@_tool
def cutout_status(name: str) -> dict:
    """What is wrong with a cutout character, and what is merely unfinished.

    Reports rather than refuses — a half-generated kit is the normal state
    while a character is being made. `missing` is slots with no part yet;
    `problems` is the list that actually needs action:

      missing_texture  the document points at a file that is not there.
      stale_pivot      a pivot placed BY HAND against a drawing that has since
                       been regenerated. The pivot is still at the same
                       fraction of a different picture, so the hand now hangs
                       off the middle of the forearm and nothing says why.
      origin           the rig's feet are not on the ground line, so it hovers
                       or sinks in every scene it is placed in.
    """
    try:
        from bgate_core import cutout as _cutout
        root = _root()
        doc = _cutout.load(_cutout_dir(root, name) / f"{name}{_cutout.SUFFIX}")
        return {"ok": True, **_cutout.status(doc, root=root)}
    except Exception as exc:
        return _fail(exc)


@_tool
def cutout_equip(name: str, slot: str, texture: str, pivot: Optional[list] = None,
                 force: bool = False) -> dict:
    """Put a different part in one slot and re-emit — a hat, a sword, an arm.

    This is what the whole pipeline is for: swapping equipment is one texture
    on one slot, not a re-drawn character. The scene carries a pivot table so
    the runtime `equip()` can do the same swap at RUNTIME; this tool is for
    changing what the character ships wearing.

    `pivot` is [x, y] as a fraction of the new part's own bounding box, y
    measured UP from the bottom. Pass it and it is recorded as AUTHORED, which
    means cutout_status will tell you if the part is later regenerated under it
    rather than letting the pivot quietly point somewhere else.
    """
    try:
        from bgate_core import cutout as _cutout, cutoutwire as _wire
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
    except Exception as exc:
        return _fail(exc)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
