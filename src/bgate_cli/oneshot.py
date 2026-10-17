"""``bgate oneshot``: a brief becomes a running, self-advancing board.

THE SPEC IS THE HAND-RUN KICKOFF. The first "one-shot" game (gnomefall,
2026-10-15) was started from a brief with the stated goal of minimal human
input, and every one of these steps still needed a hand on the wheel:

  1. ``init`` does not ``git init``, and dispatch refuses a tree with no repo;
  2. the hook, the dispatch settings and the run limit were set one by one;
  3. the director's first turn (thesis, bible, no-list, risks, scale
     contract, eight domain plans, advance to slice) ran in a console a
     human had to open - and the domain plans had to be filed in an order
     the human worked out from set_plan's refusals;
  4. ``promote_slice`` was called from a Python prompt, ``bgate serve`` by
     hand on a free port;
  5. nothing re-entered the director after the slice check passed.

This module is those steps as one command. It makes no design decision: the
thesis, the pillars and the plans are the director's, exactly as they were
when the human pasted the brief - the director just runs here, once,
non-interactively, through the same session the console uses
(bgate_ui.agents.directorsession), with the project's MCP tools.

Every external edge is a module-level function (``run_director``,
``start_serve``, ``wait_for_dispatch``) so the CLI path is testable without
a CLI, a server or sixty seconds.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

USAGE = """\
bgate oneshot NAME --brief FILE [--kind 2d|3d] [--engine godot|web] [--dir DIR]
                   [--port N] [--concurrency N] [--auto-advance]
                   [--without floor,music,...] [--ref IMAGE ...]

    A brief becomes a running board with no director session to babysit:
      1. init the project (a NEW directory, like `bgate init`), git-init and
         commit the scaffold and the brief when no .git exists;
      2. install the lane hook; store the dispatch settings
         (dispatch.max_concurrent = --concurrency [4], allow_dirty on,
         max_attempts 4, board.brief_chars 3000, board.brief_lanes 4,
         limits.max_runtime_s 3600);
      3. run the DIRECTOR once, non-interactively, on the kickoff prompt: the
         thesis, pillars/loop/constraints, the no-list, 1-4 key risks, the
         scale contract, every domain plan, then advance to slice. Narrative
         and cinematic seats are switched off unless the brief names a
         story. Gaps are re-prompted once;
      4. promote the slice to the board and start `bgate serve --port N`
         detached; confirm the first dispatch within 60 s.
    --auto-advance turns on autopilot.stage_advance: when the slice check
    passes and the feel check has run, the follow-up router files ONE director
    item that retires the risks on the board's evidence and advances to
    production (and later, on a clean presentation check, to release).
"""

#: The disciplines a one-shot board must have planned before the slice goes
#: on the board, in the order a director should file them when writing one at
#: a time (cross-domain depends_on must already exist; `plans=` in one call
#: removes the need). narrative and cinematic are added when the brief names
#: a story.
REQUIRED_DOMAINS = ("tech", "art", "gameplay", "level", "animation", "ui",
                    "audio", "qa")
STORY_DOMAINS = ("narrative", "cinematic")
_STORY = re.compile(r"\b(story|stories|narrative|plot|dialogue|dialog|cutscene|"
                    r"cinematic|lore|campaign|characters?\s+arc|protagonist)\b",
                    re.I)

SETTINGS = (
    ("dispatch.allow_dirty", True),
    ("dispatch.max_attempts", 4),
    ("board.brief_chars", 3000),
    ("board.brief_lanes", 4),
    ("limits.max_runtime_s", 3600),
)

DISPATCH_WAIT_S = 60.0
#: One director turn that writes eight plans can run well past the console's
#: 15-minute default; the run limit the board gets is the ceiling here too.
DIRECTOR_TURN_S = 3600.0


def brief_names_a_story(brief: str) -> bool:
    return bool(_STORY.search(brief or ""))


def oneshot_section(root, *, story: bool, auto_advance: bool) -> str:
    """The addendum to kickoff.prompt(): what this run must leave behind."""
    domains = list(REQUIRED_DOMAINS) + (list(STORY_DOMAINS) if story else [])
    lines = [
        "",
        "--- ONE-SHOT ---",
        "There is no human in this conversation. Nobody will answer a "
        "question, so decide every open point yourself from the brief, "
        "record the decision (decision_add state='settled', or the bible), "
        "and move on; ask_human only for money or an account. Finish "
        "EVERYTHING below in this one turn, with the tools, in this order:",
        "1. greenlight_thesis_set: sentence, options, stakes, tension, "
        "dominant_strategy, cadence.",
        "2. bible_add: the pillars (kind=pillar), the core loop (kind=loop), "
        "and the constraints (kind=constraint) - the look, the engine rules, "
        "the scope ceiling.",
        "3. not_building_add for everything the brief implies and this "
        "project will not build, each with its reason.",
        "4. greenlight_risks_set: 1-4 KEY RISKS, each a binary hypothesis "
        "the vertical slice can settle, with retire_by.",
        "5. scale_contract_set(player_height_px=<the player's on-screen "
        "height in px>). UNITS: classes are player-height RATIO bands "
        "{low, high} (a door is {low: 1.05, high: 1.5}); stages are px "
        "heights. Never pass pixel sizes as classes.",
        "6. encounter_design_set when the game has enemies or objectives "
        "(a roster of interactions, more than one objective shape).",
        f"7. DOMAIN PLANS for ALL of: {', '.join(domains)}. Read "
        "domain_plan_template(<domain>) for each, then write them ALL IN "
        "ONE CALL: domain_plan_set(plans={domain: {goal, done_when, "
        "entries, leaves_dark, open_questions}}) - the batch is validated "
        "as a whole and filed in dependency order, so cross-domain "
        "depends_on may name rows another plan in the batch creates. (If "
        "you write them one at a time instead, file in this order: " +
        ", ".join(domains) + " - a depends_on must already exist.) Mark "
        "the vertical slice with slice: true on the few entries that make "
        "the game playable; the slice is what gets built first. "
        "open_questions must be EMPTY - you decide them here.",
        "8. domain_plan_status: every one of those domains must read "
        "planned, and findings must name nothing a plan change would fix.",
        "9. greenlight_advance('slice'). If it refuses, do what the refusal "
        "lists and call it again.",
        "Do NOT plan_promote or queue_add anything: the harness promotes "
        "the slice itself when you are done. Do not change settings or "
        "seats. End your reply with the line 'ONESHOT: DONE' and the list "
        "of domains you planned.",
    ]
    if auto_advance:
        lines.append(
            "This board auto-advances: when the slice check passes you will "
            "be handed an item to retire the risks on evidence and advance "
            "to production, so write risks whose retire_by names evidence "
            "the board will produce (a test, a feel check, a screenshot).")
    return "\n".join(lines)


def kickoff_prompt(root, seeded: dict, *, name: str, story: bool,
                   auto_advance: bool) -> str:
    from bgate_core.design import kickoff as _kickoff

    base = _kickoff.prompt(root, seeded, project_name=name)
    # The kickoff prompt ends with the brief; the one-shot section goes
    # BEFORE it so the instructions are not buried under 10 kB of text.
    marker = "\n--- BRIEF ---\n"
    section = oneshot_section(root, story=story, auto_advance=auto_advance)
    if marker in base:
        head, tail = base.split(marker, 1)
        return head + section + "\n" + marker + tail
    return base + "\n" + section


# ── the external edges ─────────────────────────────────────────────────────

def run_director(root, prompt: str) -> dict:
    """One non-interactive director turn through the console's session.

    Same runner, model and tool surface as the dashboard's director chat
    (bgate_ui.agents.directorsession), so what this turn can do is exactly
    what the human's console could. Returns ``{ok, text}``.
    """
    from bgate_ui.agents import directorsession as _director

    got: dict = {"ok": False, "text": ""}

    def settle(text: str, failed: bool) -> None:
        got["ok"] = not failed
        got["text"] = text or ""

    held = _director.TURN_TIMEOUT_S
    _director.TURN_TIMEOUT_S = max(float(held), DIRECTOR_TURN_S)
    try:
        _director._turn(str(root), prompt, "", settle, record=True)
    except _director.Unavailable as exc:
        got["text"] = str(exc)
    except Exception as exc:                                      # noqa: BLE001
        got["text"] = f"the director session crashed: {type(exc).__name__}: {exc}"
    finally:
        _director.TURN_TIMEOUT_S = held
    return got


def start_serve(root, port: int) -> dict:
    """Start ``bgate serve --port N`` on this project, detached from this
    console, its output in ``.bgate/serve.log``. Returns ``{ok, pid, url, log}``."""
    log = Path(root) / ".bgate" / "serve.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["BGATE_ROOT"] = str(root)
    env.pop("BGATE_SEAT", None)
    env.pop("BGATE_WORK_ITEM", None)
    flags = 0
    if sys.platform == "win32":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
    handle = open(log, "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "bgate_cli", "serve", "--port", str(int(port))],
            cwd=str(root), env=env, stdin=subprocess.DEVNULL,
            stdout=handle, stderr=handle, close_fds=True,
            creationflags=flags, start_new_session=(sys.platform != "win32"))
    except OSError as exc:
        handle.close()
        return {"ok": False, "error": str(exc), "url": f"http://127.0.0.1:{port}",
                "log": str(log)}
    finally:
        try:
            handle.close()
        except OSError:
            pass
    return {"ok": True, "pid": proc.pid, "url": f"http://127.0.0.1:{int(port)}",
            "log": str(log)}


def wait_for_dispatch(root, seconds: float = DISPATCH_WAIT_S,
                      poll_s: float = 2.0) -> dict:
    """Did the board start something? Polls the queue until an item reads
    dispatched (or done) or the window closes. Returns ``{ok, dispatched, why}``."""
    from bgate_core.board import queue as _queue
    from bgate_core.store import db

    deadline = time.monotonic() + float(seconds)
    while True:
        live = [i for i in _queue.list_items(root)
                if i.get("status") in ("dispatched", "done", "review")]
        if live:
            first = live[0]
            return {"ok": True, "dispatched": [int(i["id"]) for i in live],
                    "why": f"#{first['id']} [{first.get('seat')}] "
                           f"{str(first.get('title') or '')[:60]}"}
        if time.monotonic() >= deadline:
            queued = _queue.list_items(root, status="queued")
            return {"ok": False, "dispatched": [],
                    "why": (f"{len(queued)} item(s) queued, none dispatched in "
                            f"{int(seconds)} s - read .bgate/serve.log and "
                            "queue_list's waiting_on")}
        db.close_all()
        time.sleep(poll_s)


# ── verification ───────────────────────────────────────────────────────────

def gaps(root, *, story: bool) -> list[str]:
    """What the director's turn left undone. Empty means the board is ready."""
    from bgate_core.design import domainplan as _dp
    from bgate_core.design import greenlight as _gl

    out: list[str] = []
    if not _gl.thesis(root):
        out.append("no mechanical thesis is settled (greenlight_thesis_set)")
    if not _gl.risks(root):
        out.append("no key risks are named (greenlight_risks_set)")
    wanted = list(REQUIRED_DOMAINS) + (list(STORY_DOMAINS) if story else [])
    try:
        state = _dp.status(root)["domains"]
    except Exception as exc:                                      # noqa: BLE001
        return out + [f"domain_plan_status failed: {exc}"]
    for d in wanted:
        row = state.get(d) or {}
        if not row.get("planned"):
            out.append(f"the {d} domain has no plan (domain_plan_set)")
        elif row.get("open_questions"):
            out.append(f"the {d} plan has {len(row['open_questions'])} open "
                       "question(s) - decide them, nobody else will")
    slice_rows = sum(int((state.get(d) or {}).get("slice") or 0) for d in wanted)
    if not slice_rows and not any("no plan" in g for g in out):
        out.append("no plan entry is marked slice: true - nothing would be "
                   "built first")
    if _gl.stage(root) == _gl.THESIS:
        out.append("the project is still at thesis (greenlight_advance('slice'))")
    return out


def regap_prompt(missing: list[str]) -> str:
    return ("ONE-SHOT, SECOND PASS. The first pass left these gaps; close every "
            "one with the tools, in this turn, deciding anything open yourself:\n- "
            + "\n- ".join(missing)
            + "\nThen domain_plan_status, greenlight_advance('slice') if the "
              "project is still at thesis, and end with 'ONESHOT: DONE'.")


# ── the command ────────────────────────────────────────────────────────────

def run(name: str, brief: str, *, kind: str = "2d", engine: str = "",
        dest: str = "", port: int = 7788, concurrency: int = 4,
        auto_advance: bool = False, without: str = "",
        refs: Optional[list] = None,
        init: Optional[Callable[..., int]] = None,
        out: Callable[[str], None] = print) -> int:
    """The whole command. Returns the exit code; prints as it goes, because
    the steps take minutes and a silent minute reads as a hang."""
    from bgate_core.board import gitwork as _gitwork
    from bgate_core.board import seats as _seats
    from bgate_core.design import domainplan as _dp
    from bgate_core.design import greenlight as _gl
    from bgate_core.design import kickoff as _kickoff
    from bgate_core.runtime import engines as _engines
    from bgate_core.store import settings as _settings
    from bgate_core.store.util import slugify
    from bgate_cli import main as _main

    if not name.strip():
        out("error: a project needs a name, bgate oneshot <name> --brief FILE")
        return 2
    if not brief:
        out("error: --brief FILE is required; the brief is what the director "
            "builds the board from")
        return 2
    brief_path = Path(brief).expanduser()
    if not brief_path.is_file():
        out(f"error: cannot read --brief {brief}: no such file")
        return 2
    text = brief_path.read_text(encoding="utf-8")
    if not text.strip():
        out(f"error: --brief {brief} is empty")
        return 2
    try:
        port = int(port)
        concurrency = int(concurrency)
    except (TypeError, ValueError):
        out("error: --port and --concurrency take whole numbers")
        return 2
    if concurrency < 1:
        out("error: --concurrency is at least 1")
        return 2
    engine = (engine or _engines.DEFAULT).strip().lower()
    root = Path(dest).expanduser().resolve() if dest else (
        Path.cwd() / slugify(name)).resolve()

    # 1. init (a NEW directory; refuses an existing game, as init does).
    out(f"[1/5] init {name} ({kind}, {engine}) at {root}")
    code = (init or _main.init_project)(name, kind=kind, dest=str(root),
                                        without=without, engine=engine,
                                        brief=str(brief_path), refs=refs or [])
    if code != 0:
        return int(code)
    if not (root / ".bgate").is_dir():
        out(f"error: init reported success but {root / '.bgate'} is missing")
        return 1
    repo = _gitwork.initialize(root, message=f"{name}: scaffold and brief")
    if repo.get("created"):
        out(f"      git init + first commit {str(repo.get('head') or '')[:10]}")
    elif repo.get("available"):
        out(f"      git repo already here at {str(repo.get('head') or '')[:10]}")
    else:
        out(f"error: {repo.get('reason')} - dispatch refuses a project with no "
            "repo, so nothing would run")
        return 1

    # 2. hook + settings.
    out("[2/5] hook and dispatch settings")
    hooked = _main.install_hook(str(root), scope="project")
    if not hooked.get("ok"):
        out(f"error: hook install failed: {hooked}")
        return 1
    out(f"      hook -> {hooked.get('settings')}")
    _settings.set(root, "dispatch.max_concurrent", concurrency)
    for key, value in SETTINGS:
        _settings.set(root, key, value)
    if auto_advance:
        _settings.set(root, "autopilot.stage_advance", True)
    out(f"      max_concurrent={concurrency} allow_dirty=on max_attempts=4 "
        f"brief_chars=3000 brief_lanes=4 max_runtime_s=3600"
        + (" stage_advance=on" if auto_advance else ""))
    story = brief_names_a_story(text)
    if not story:
        for role in STORY_DOMAINS:
            try:
                _seats.configure(root, role, enabled=False)
            except Exception as exc:                              # noqa: BLE001
                out(f"      warning: could not disable the {role} seat: {exc}")
        out("      narrative + cinematic seats off (the brief names no story)")

    # 3. the director, once; re-prompt once on gaps.
    seeded = {"brief": str(_kickoff.BRIEF_PATH).replace(os.sep, "/"),
              "brief_section": None, "refs_section": None, "pinned": [],
              "skipped": []}
    try:
        from bgate_core.design import bible as _bible
        for section in _bible.list_sections(root, kind="reference"):
            if section.get("title") == _kickoff.BRIEF_TITLE:
                seeded["brief_section"] = section.get("id")
            if section.get("title") == _kickoff.REFS_TITLE:
                seeded["refs_section"] = section.get("id")
    except Exception:                                             # noqa: BLE001
        pass
    prompt = kickoff_prompt(root, seeded, name=name, story=story,
                            auto_advance=auto_advance)
    out("[3/5] director: thesis, bible, risks, scale, domain plans, slice "
        "(this takes a while; the transcript is .bgate/console/)")
    got = run_director(root, prompt)
    if not got.get("ok"):
        out(f"error: the director turn failed: {str(got.get('text') or '')[:400]}")
        return 1
    out("      first pass done: " + _last_line(got.get("text") or ""))
    missing = gaps(root, story=story)
    if missing:
        out(f"      {len(missing)} gap(s) after the first pass; re-prompting once:")
        for line in missing:
            out(f"        - {line}")
        got = run_director(root, regap_prompt(missing))
        if not got.get("ok"):
            out(f"error: the second director turn failed: "
                f"{str(got.get('text') or '')[:400]}")
            return 1
        missing = gaps(root, story=story)
    only_the_button = all("still at thesis" in m for m in missing)
    if _gl.stage(root) == _gl.THESIS and only_the_button:
        # The director planned everything and did not press the button:
        # the human running this command is the human the gate wants.
        try:
            _gl.advance(root, _gl.SLICE, by="oneshot")
            missing = [m for m in missing if "still at thesis" not in m]
            out("      advanced to slice")
        except _gl.StageRefused as exc:
            missing.append(str(exc))
    if missing:
        out("error: the board is not ready after two director passes:")
        for line in missing:
            out(f"  - {line}")
        out("  the project exists; open the dashboard and finish the kickoff by "
            "hand, or re-run the director from the console")
        return 1
    out("      every domain planned, thesis and risks set, stage=slice")

    # 4. the slice onto the board, and the dashboard.
    filed = _dp.promote_slice(root)
    out(f"[4/5] promote_slice: {len(filed)} item(s) filed"
        + ("" if filed else " - no plan entry is marked slice: true, so "
                            "nothing will dispatch until one is"))
    served = start_serve(root, port)
    if not served.get("ok"):
        out(f"error: could not start bgate serve: {served.get('error')}")
        return 1
    out(f"      dashboard {served['url']}  (pid {served.get('pid')}, log "
        f"{served.get('log')})")

    # 5. prove it moves.
    out(f"[5/5] waiting up to {int(DISPATCH_WAIT_S)} s for the first dispatch")
    moved = wait_for_dispatch(root, DISPATCH_WAIT_S) if filed else {
        "ok": False, "dispatched": [], "why": "nothing was filed"}
    if moved.get("ok"):
        out(f"      dispatch started: {moved['why']}")
    else:
        out(f"      NOT dispatching yet: {moved['why']}")
    out("")
    out(served["url"])
    out(f"project {root}")
    if auto_advance:
        out("auto-advance is on: the board moves to production on its own once "
            "the slice check passes and the feel check has run")
    return 0 if moved.get("ok") else 1


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    return (lines[-1] if lines else "(no reply text)")[:200]


def parse(rest: list) -> dict:
    """``bgate oneshot`` argv (after the verb) to run()'s keyword arguments.
    Raises ValueError with the usage line to print."""
    def opt(flag: str, default: str = "") -> str:
        if flag in rest:
            index = rest.index(flag) + 1
            if index < len(rest):
                return rest[index]
            raise ValueError(f"{flag} takes a value")
        return default

    flagged = {"--kind", "--dir", "--engine", "--brief", "--ref", "--port",
               "--concurrency", "--without"}
    skip: set[int] = set()
    for i, token in enumerate(rest):
        if token in flagged:
            skip.update({i, i + 1})
    unknown = [a for i, a in enumerate(rest) if i not in skip
               and a.startswith("-") and a not in ("--auto-advance",)]
    if unknown:
        raise ValueError(f"unknown option {unknown[0]}")
    positional = [a for i, a in enumerate(rest)
                  if i not in skip and not a.startswith("-")]
    if not positional:
        raise ValueError("a project needs a name")
    refs = [rest[i + 1] for i, token in enumerate(rest)
            if token == "--ref" and i + 1 < len(rest)]
    try:
        port = int(opt("--port", "7788"))
        concurrency = int(opt("--concurrency", "4"))
    except ValueError:
        raise ValueError("--port and --concurrency take whole numbers") from None
    return {"name": positional[0], "brief": opt("--brief"),
            "kind": opt("--kind", "2d"), "engine": opt("--engine"),
            "dest": opt("--dir"), "port": port, "concurrency": concurrency,
            "auto_advance": "--auto-advance" in rest,
            "without": opt("--without"), "refs": refs}


def main(rest: list) -> int:
    if not rest or "--help" in rest or "-h" in rest:
        print(USAGE)
        return 0 if rest else 2
    try:
        kwargs = parse(rest)
    except ValueError as exc:
        print(f"error: {exc}")
        print(USAGE)
        return 2
    return run(**kwargs)
