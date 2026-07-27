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
from bgate_core import lore as _lore
from bgate_core import iterations as _iterations
from bgate_core import items as _items
from bgate_core import project as _project
from bgate_core import search as _search

mcp = FastMCP("builders-gate")


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
    """
    if not isinstance(result, dict):
        return result
    failed = (result.get("ok") is False or bool(result.get("error"))
              or result.get("available") is False)
    if not failed:
        return result
    reason = ""
    for key in _REASON_KEYS:
        value = result.get(key)
        text = value.strip() if isinstance(value, str) else ""
        if text:
            reason = text
            break
    return {**result, "ok": False,
            "error": reason or "the call failed without stating a reason"}


def _tool(fn: Callable) -> Callable:
    """Register a function as an MCP tool, with `project_dir` bolted on, run OFF
    the event loop, and its failures normalized to one shape.

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
                return _normalize(fn(*args, **kwargs))
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
    """Is Blender available to this machine, and which version? Check before modeling."""
    try:
        probe = _blender.available()
        return {**probe, **(_blender.version() if probe["available"] else {})}
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_run(script: str, blend_file: Optional[str] = None, render: bool = False,
                engine: str = "BLENDER_WORKBENCH", timeout: int = 180,
                label: str = "") -> dict:
    """Run a bpy script in headless Blender and get the scene back as facts.

    `bpy` is already imported. Returns per-object tri/vert counts (evaluated, so
    modifiers count), UV warnings, materials, your print() output, and — with
    render=True — a PNG of the active camera view (archived to the project's
    preview gallery; give a `label` so humans can tell renders apart).

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
                                     out_dir=out_dir, engine=engine, timeout=timeout)
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
    """Is the painted-art leg (gpt-image) usable? Checks the key without exposing it."""
    try:
        _root()  # triggers .env load
        from bgate_adapters import imagegen
        return imagegen.available()
    except Exception as exc:
        return _fail(exc)


@_tool
def image_generate(prompt: str, filename: str, size: str = "1024x1024",
                   quality: str = "medium", transparent: bool = False) -> dict:
    """Generate PAINTED art via gpt-image — portraits, select-screen cards,
    title splashes, stage paint-overs. Costs real money per image (~$0.02-0.19).

    Division of labor: use blender_sprites for anything needing the SAME
    character across multiple frames (an image model can't hold a rig steady);
    use this for one-off illustrated pieces. transparent=True for art that
    composites over the game; false for full backdrops.

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
        result = _chroma.generate(prompt, str(out), provider="openai",
                                  keyed=bool(transparent), size=size,
                                  quality=quality, transparent=False, root=root,
                                  logical_name=_Path(filename).stem,
                                  work_item_id=_work_item_id())
        if result.get("ok"):
            archived = _archive_preview(result["path"], f"art-{_Path(filename).stem}")
            if archived:
                result["preview"] = archived
            artifact = _register_artifact(
                _Path(filename).stem, result["path"], producer="image_generate",
                model=result.get("model", ""), prompt=prompt,
                metadata={"size": size, "quality": quality,
                          "transparent": transparent,
                          "chroma": result.get("chroma"),
                          "alpha": result.get("alpha"),
                          "preview": archived or "",
                          **imagegen.cost_meta(result)})
            if artifact:
                result["artifact"] = artifact
            _log("art", f"generated painted art {filename} ({size}, {quality})",
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
            needs_review = bool(consistency.get("ok") and consistency.get("flagged"))

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
                   force: bool = False) -> dict:
    """Create a runnable Godot project wired for playtesting.

    kind: 2d (platformer slice) | 3d (first-person slice). dest defaults to
    <project root>/game.

    The template ships the BGate telemetry autoload already registered, and a
    player whose feel tunables (gravity, fall_multiplier, coyote_time) are both
    exported AND emitted on jump/land — so the first playtest already produces
    the telemetry join. Refuses a non-empty dest unless force=True.
    """
    try:
        target = dest or str(_Path(_root()) / "game")
        result = _scaffold.new_project(target, name, kind=kind, force=force)
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
def queue_list(status: Optional[str] = None, seat: Optional[str] = None) -> dict:
    """The work queue. status: queued | dispatched | done | failed."""
    try:
        from bgate_core import queue as _q
        return {"items": _q.list_items(_root(), status=status, seat=seat)}
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
    """
    try:
        from bgate_core import queue as _q
        return _q.set_status(_root(), item_id, "failed" if failed else "done",
                             result=result)
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
