"""Dispatch - the dashboard spawns real Claude seat sessions against work items.

Why this architecture wins: a session spawned with cwd = the game project gets
(1) the builders-gate MCP tools NATIVELY (the server resolves the project by
cwd - no runner scripts, no kwargs files), and (2) the PreToolUse lane/lock
hook with BGATE_SEAT set - actual enforcement, not honor-system. The dashboard
is user-run software, so a dispatch click is the USER launching the agent.

One live session per work item; state is in-memory plus a log file per item
(.bgate/agents/item-<id>.log) so a dashboard restart loses handles, not history.

Who writes what, because getting this wrong cost us the whole lifecycle once:
status() and read_activity() are READS - the dashboard polls them every few
seconds and they must not touch the DB. Everything that settles a run
(_reap/_trip/sweep/reconcile) is driven by the per-run watchdog thread, or
called explicitly at startup, so a run finishes correctly whether or not
anybody has a browser tab open.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from bgate_core import aegis as _aegis
from bgate_core import agentreg as _agentreg
from bgate_core import assets as _assets
from bgate_core import gates as _gates
from bgate_core import gitwork as _git
from bgate_core import queue as _queue
from bgate_core import settings as _settings
from bgate_core import spend as _spend
from bgate_ui import runners as _runners

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_live: dict[int, dict] = {}
_lock = threading.Lock()

# Items between "we decided to start it" and "there is a process". Held under
# _lock; see dispatch() for the race it closes.
_starting: set[int] = set()

# The parsed-feed cursors below are now touched by two threads - the per-run
# watchdog (which reads the feed every couple of seconds to see whether the run
# errored out) and whatever request thread is painting the console. Advancing
# the same byte cursor twice absorbs the same lines twice: duplicated steps,
# doubled step counts, and a clobbered partial-line remainder.
_feed_lock = threading.Lock()

# Finished runs, kept so their result can actually be READ. A reaped run used to
# leave the table on the very next poll, taking the agent's final word with it - # the one thing the user was waiting three minutes to see was on screen for
# three seconds. Bounded by count and age; keyed (project, item).
_done: dict[tuple[str, int], dict] = {}
RETAIN_RUNS = 20
RETAIN_S = 30 * 60

# Parsed activity per (project, item) - see read_activity: the log is read
# FORWARD FROM A BYTE CURSOR exactly once, never re-parsed per poll.
_activity: dict[tuple[str, int], dict] = {}
MAX_STEPS = 500      # ring per run; what falls off is counted, not hidden
MAX_FEEDS = 200      # parsed feeds held at once

# The two backstops that make "no rogue agents" true rather than aspirational.
#
# HARD_RUNTIME_S is the ceiling that applies when the project's budget names
# none (max_runtime_s = 0 used to mean forever). STALL_S kills a session that is
# alive but has produced no observable output at all - no log line, no file - # for that long: that is a wedged process holding a concurrency slot, not work.
# Both are deliberately generous; they are the difference between a bad run and
# an unbounded one, not a performance policy.
HARD_RUNTIME_S = int(os.environ.get("BGATE_MAX_RUNTIME_S") or 2 * 60 * 60)
STALL_S = int(os.environ.get("BGATE_STALL_S") or 25 * 60)

# Projects whose stranded-item reconciliation has already run this process.
_reconciled: set[str] = set()


def _pkey(root) -> str:
    """One stable key per project for the module-level tables."""
    try:
        return str(Path(root).resolve())
    except OSError:
        return str(root)


def _refuse(code: str, message: str, **detail) -> dict:
    """A refusal the UI can both show and switch on.

    dispatch() answers plain dicts (app.py returns them verbatim) and callers
    already read ``error`` as a sentence, so the machine-readable ``code`` rides
    alongside rather than replacing it - the string stays a string.
    """
    return {"ok": False, "error": message, "code": code, "detail": detail}


def _user_msg(text: str) -> str:
    """A stream-json user turn - the wire format the CLI reads from stdin."""
    return json.dumps({"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": text}]}}) + "\n"


def _emit(root, kind: str, ref: str = "", payload: Optional[dict] = None) -> None:
    """Put one event on the bus, never at the cost of the dispatch that caused it.

    Same shape and same reasoning as ``queue._emit``: the event log is a
    notification substrate, so a locked database - or an events module that will
    not import at all, on a project whose migration has not run - loses the line
    and nothing else. Without it ``agent.spawned``/``agent.exited`` and
    ``budget.refused`` are three of the fourteen kinds the vocabulary offers a
    notification checkbox for and nobody writes.
    """
    try:
        from bgate_core import events as _events

        _events.emit(root, kind, ref=ref, payload=payload)
    except Exception:
        pass


def _flag(root, key: str, env_name: str) -> bool:
    """A boolean dispatch switch, through the settings registry.

    The registry declares ``env=<env_name>`` for these, so the variable still
    wins - what this adds is that the STORED value is read at all. Before it,
    both switches were read straight from ``os.environ``, so the Settings panel
    offered a toggle whose only effect was to write a doc nothing looked at.
    Falls back to the bare variable if the registry will not read: a dispatch
    must not be blocked by a settings doc.
    """
    try:
        return bool(_settings.get(root, key))
    except Exception:
        return os.environ.get(env_name, "").strip().lower() in {
            "1", "true", "yes", "on"}


def find_claude() -> Optional[str]:
    """Kept as the module's own name because callers and tests import it from
    here; the lookup itself moved to runners.py, where the second CLI lives."""
    return _runners.find_claude()


def _executable(runner: "_runners.Runner") -> Optional[str]:
    """Where this runner's CLI is.

    The claude lookup deliberately still goes through THIS module's
    ``find_claude``. It has been dispatch's public seam since there was one
    runner - the lifecycle tests stand a fake CLI up by patching it, and so does
    anything else that ever needed to. Moving the resolution wholesale into the
    table would have silently stopped honouring that, which is a worse trade
    than one branch with a reason on it.
    """
    return find_claude() if runner.name == "claude" else runner.find()


def _runner_for(root: str, seat: str) -> "_runners.Runner":
    """Which CLI this seat's agent runs on.

    ONLY THE ART SEAT IS ROUTABLE, and that is a deliberate ceiling rather than
    an unfinished generalisation. The alternative runner is here because it
    generates images; no other seat gains anything from it, and every seat that
    moves onto it loses live steering and the cost ceiling. A single global
    switch would put the whole board one wrong click away from that.
    """
    if (seat or "").strip().lower() != "art":
        return _runners.get(_runners.DEFAULT_RUNNER)
    try:
        return _runners.get(str(_settings.get(root, "art.runner")))
    except Exception:
        return _runners.get(_runners.DEFAULT_RUNNER)


def _model_for(root: str, seat: str) -> Optional[str]:
    """Which model this seat's agent runs on.

    NOTHING PASSED --model BEFORE THIS. Every dispatch call site - autodeploy,
    the QA gate, follow-up, the console, the orchestrator - called dispatch()
    without a model, so `["--model", model] if model else []` in runners.py
    always evaluated to the empty branch and every seat inherited whatever the
    CLI happened to default to. On 2026-08-08 that was opus-5[1m]: 82 runs,
    1.19 billion input-side tokens, a 5-hour session allowance gone in three
    and a half. The default was never chosen; it was just never overridden.

    Art is the seat that gets the exception, and it is the only one. Its output
    is judged on taste - a portrait that is merely syntactically valid is a
    failed portrait - whereas a seat editing GDScript is judged on whether the
    engine loads the scene, which a cheaper model settles just as well.

    An unreadable setting returns None rather than a guess, which restores the
    old inherit-the-default behaviour instead of silently pinning a model this
    machine might not have.
    """
    key = ("dispatch.model_art"
           if (seat or "").strip().lower() == "art" else "dispatch.model")
    try:
        chosen = str(_settings.get(root, key) or "").strip()
        if not chosen and key == "dispatch.model_art":
            chosen = str(_settings.get(root, "dispatch.model") or "").strip()
        return chosen or None
    except Exception:
        return None


def _max_turns(root: str) -> int:
    """The per-run turn ceiling, or 0 for none.

    Separate from the cost ceiling because they fail differently: _observed_cost
    only sees a price at a result boundary, so an agent grinding through a long
    tool loop can run far past its dollar ceiling without ever offering the
    dispatcher a place to stop it. Turns are counted by the CLI itself.
    """
    try:
        return max(0, int(_settings.get(root, "dispatch.max_turns") or 0))
    except Exception:
        return 0


def _native_images(root: str, runner: "_runners.Runner") -> bool:
    """Does this run generate its own pixels, or call image_generate?

    Falls back to the bgate pipeline whenever the runner has no image tool of
    its own, so the setting can never ask for a capability the process does not
    have - a switch reading `native` next to an agent that cannot generate is
    the kind of lie that costs an afternoon.
    """
    if runner.name != "codex":
        return False
    try:
        return str(_settings.get(root, "art.image_backend")) == "native"
    except Exception:
        return False


# Seat-specific house rules injected UNCONDITIONALLY into the dispatch prompt - # the one channel every agent sees even if it skips seat_brief (item 56 did:
# it hand-rolled 8 loose image_edit frames for an animation task and never
# stitched a sheet). Keep these short, imperative, and PROJECT-AGNOSTIC: this
# dict ships with the tool and is read by every project on the machine, so
# anything naming a specific game's assets, characters or test scenes belongs
# in that project's own seat_rules.json (see :func:`seat_rules`), not here.
SEAT_RULES = {
    "narrative": (
        "NARRATIVE HOUSE RULE - NO FIRST-THOUGHT JOKES:\n"
        "• Before landing ANY name/line/bark, generate 5 candidates and kill "
        "every one that is the FIRST joke anyone would make on the premise "
        "(the obvious pun, the meme format, the joke every parody of this "
        "subject already made). Ship the one that surprises.\n"
        "• Obey the project's OWN tone tests (read the tone guide / bible "
        "before writing; if you wrote one this session, your content must "
        "pass it - self-contradiction is an automatic fail). No winks, no "
        "lampshading, no decade-old meme formats.\n"
        "• Specificity beats snark: a line should only make sense in THIS "
        "world. If it could be pasted into any generic parody of the genre, "
        "cut it.\n"
        "• Read every deliverable back OUT LOUD (to yourself) against the "
        "tone tests before landing. Land fewer, better lines."
    ),
    "audio": (
        "AUDIO HOUSE RULE - EVERY SYNTHESIZED ASSET SHIPS ITS RECIPE:\n"
        "• Alongside each .wav/.ogg you synthesize, write a `<name>.synth.json` "
        "sidecar capturing the FULL parametric recipe: wave type(s), ADSR, "
        "pitch/glide, noise mix, filter, duration, sample rate - and for music "
        "beds the complete note/step pattern per channel + tempo/key. Another "
        "process must be able to re-render the identical asset from the recipe "
        "alone (the upcoming Audio Studio edits these knobs and re-renders - "
        "a .wav without its recipe is a dead end).\n"
        "• Keep the synthesis code you used in the project's .bgate/ scratch "
        "so the recipe->render path is reproducible."
    ),
    "art": (
        "ART HOUSE RULE - ANIMATIONS ARE SINGLE-GEN SHEETS (identity by "
        "construction), THEN SLICED:\n"
        "• For ANY animation, generate the WHOLE animation as ONE image: "
        "image_edit conditioned on the approved anchor ref, prompting 'an "
        "N-frame <anim> animation sprite sheet of THIS EXACT character, N "
        "poses side-by-side left-to-right in one row, evenly spaced, "
        "transparent background, same character same colors in every frame'. "
        "A model CANNOT drift identity inside one image - per-pose generation "
        "drifted skin/outfit colors across frames and is now the FALLBACK, "
        "not the default.\n"
        "• Slice + normalize + emit the engine sheet with the pipeline: "
        "python -c from bgate_adapters.sprites import from_painted_sheet - it "
        "cuts the row into equal cells, alpha-trims, bottom-centers, and "
        "writes <name>_sheet.png + <name>_frames.tres. Verify every cell "
        "sliced cleanly (no half-characters: regenerate the sheet with a "
        "stricter even-spacing instruction, don't hand-fix).\n"
        "• Cross-sheet identity: every animation sheet conditions on the SAME "
        "anchor ref, and you LOOK at all sheets side-by-side before landing - "
        "same character, same colors across the whole set.\n"
        "• image_edit single-frame fixes and per-pose image_sprites remain "
        "available ONLY to repair one bad cell of an otherwise-good sheet.\n"
        "• Before landing: run consistency_check on every frame AND clear its "
        "alpha flags (no white halo, no feathered fringe, no background bleed, no "
        "hollow interior, transparent = truly empty). Any alpha flag = do not land.\n"
        "\n"
        "HUD / UI CHROME SHIPS AS SEPARATE LAYERED PARTS, NEVER ONE BAKED "
        "COMPOSITE:\n"
        "• A UI element with dynamic or independently-driven sub-parts - a meter = "
        "frame + segmented FILL + icon + counter badge; a health bar = frame + "
        "FILL; a card = frame + portrait + label plate - MUST ship as SEPARATE "
        "transparent PNGs, one per layer, NOT fused into a single image. The "
        "scene/designer stacks and drives each independently: the fill depletes in "
        "code BEHIND a hollow frame, segments light one by one, the icon/badge are "
        "their own nodes. Gening the whole element in one go leaves nothing the "
        "designer can wire - it is not shippable.\n"
        "• Frames are HOLLOW: a fully transparent window where the code-driven fill "
        "shows through. NEVER bake a colored fill into a frame. For every frame, "
        "post the exact fill-window rect (x,y,w,h) in your seat note.\n"
        "• Keep the parts on a consistent pixel grid / shared registration so they "
        "stack cleanly at the target rect. A single composed PREVIEW mock is fine "
        "FOR REVIEW, but the SHIPPED assets are the separate layers.\n"
        "• Match the pinned concept: crop the target element out of the concept "
        "ref, condition generation on that crop, and build your own "
        "concept-vs-output comparison - iterate until it matches, don't ship "
        "isolated bare bars.\n"
        "\n"
        "WORLD / ENVIRONMENT ASSETS ARE INDIVIDUAL GENS, NEVER A SLICED SCENE:\n"
        "• Concept mocks are COMPOSITES - inspiration, not assets. A shippable "
        "world asset is generated ON ITS OWN: one prop (desk, plant, vending "
        "machine, printer shrine), one tile, one unit sprite per gen, "
        "transparent background, consistent scale against the project's grid "
        "(e.g. a 32px-tile world: props sized in tile multiples, characters to "
        "their tile footprint). NEVER generate a full scene and cut pieces out "
        "of it - sliced fragments have baked lighting/overlap and never "
        "composite cleanly.\n"
        "• Tilesets: gen each tile type separately (or a strict uniform grid "
        "sheet where every cell is one clean tile), then assemble the atlas "
        "with code - cells must be seamlessly tileable with their neighbors.\n"
        "• Scale/registration discipline: every asset in a batch states its "
        "intended pixel size; verify against the grid before landing so the "
        "engine drops it in without per-asset fudging.\n"
        "• ISO UNITS SHIP THE FULL FACING MATRIX: 2 generated base facings "
        "(SE front-right + NE back-right) x every animation, named "
        "<anim>_<facing> (idle_se, walk_ne, ...) through the standard sheet "
        "pipeline; SW/NW are mirrored in-engine (flip_h), never generated. A "
        "partial facing x anim matrix is an automatic fail - check the "
        "project bible's unit-sprite contract before landing any unit.\n"
        "• ISO PROPS DECLARE A ROTATION CLASS (see the project bible's "
        "prop-rotation contract): SYMMETRIC = 1 gen reused; MIRRORABLE = 2 "
        "gens + flip_h (NO text/logos/handedness); FULL = 4 gens (anything "
        "with readable text/signage - mirrored text is an automatic fail). "
        "All views of one prop conditioned on the SAME prop ref so it reads "
        "as one object rotated; state the tile footprint per prop.\n"
        "\n"
        "DELIVERY FIDELITY - WHAT WAS APPROVED IS WHAT SHIPS:\n"
        "• The engine-ready file you deliver must be a MECHANICAL derivation "
        "of the approved artifact revision: trim, downscale, alpha-clean - "
        "NOTHING ELSE. Never redraw, re-generate, or 'improve' an asset at "
        "the delivery step; a delivered file whose content differs from its "
        "approved source is an automatic reject (observed failure: floors "
        "shipped with an invented X-bevel that existed in no approved rev).\n"
        "• Name the source in your seat note per delivered file "
        "(delivered X <- approved revision N) so the trail is auditable."
    ),
}


SEAT_RULES_FILENAME = "seat_rules.json"


def seat_rules(root: str, seat: str) -> str:
    """The house rules injected into THIS project's prompt for this seat.

    ``<root>/.bgate/seat_rules.json`` ({"art": "...", "narrative": ""}) is the
    project's override and wins outright - including an empty string, which is
    how a project turns a built-in off. Rules are prompt text, not schema, so
    they live in a file the project edits and diffs rather than in the seat
    table. Absent an override, the shipped built-in applies.
    """
    try:
        data = json.loads((Path(root) / ".bgate" / SEAT_RULES_FILENAME)
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict) and seat in data:
        return str(data[seat] or "").strip()
    return SEAT_RULES.get(seat, "")


def _verify_rule(root: str) -> str:
    """Step 4's verification line, DERIVED from this project.

    It used to name another game's test scene (game/tests/fight_test.gd) and
    order every seat of every project to run it - an instruction most projects
    cannot follow and none should inherit. Nothing is invented here: the engine
    comes from the project row (or a project.godot on disk), the test scripts
    from what actually exists, and a project with neither is simply told to look
    at what it produced.
    """
    root_path = Path(root)
    engine = ""
    try:
        from bgate_core import project as _project
        engine = str(_project.get(root).get("engine") or "")
    except Exception:
        pass
    godot = engine == "godot" or (root_path / "game" / "project.godot").is_file()
    if not godot:
        return "LOOK at what you produce and prove it works before you land it."
    parts = ["godot_check_project after structural changes"]
    try:
        tests = sorted(p for p in (root_path / "game" / "tests").glob("*.gd"))
    except OSError:
        tests = []
    if tests:
        named = ", ".join(p.relative_to(root_path).as_posix() for p in tests[:4])
        parts.append("run this project's own test scripts via godot_run when the "
                     f"code they cover moved ({named}) - fail=0 or report exactly "
                     "why")
    parts.append("godot_screenshot when the change is visible")
    parts.append("LOOK at what you produce")
    return "; ".join(parts) + "."


# The toolchain the agent INHERITS, resolved once by the dashboard.
#
# WHY THIS EXISTS, observed 2026-08-09: two agents spawned a minute apart each
# ran `find / -iname godot*.exe` against the whole filesystem - one of them with
# no -maxdepth at all. They were still walking 22 minutes later, ~20 CPU-minutes
# each, having outlived the agents that started them; nothing was left to read
# their output. The search was never needed. find_godot() checks a handful of
# known install roots and BGATE_GODOT short-circuits even that.
#
# The cost is not the CPU. A drive-wide find outruns the Bash tool's timeout, so
# the agent gets nothing back, tries again with different flags, and each of
# those turns re-sends the entire context - the same mechanism the SPEND TURNS
# section below was written for. An agent searches when nobody handed it a path,
# so this hands it the path.
#
# Successes are cached, misses are NOT: a dashboard left running while the user
# installs Blender should pick it up on the next dispatch rather than at the
# next restart. A finder that raises is not fatal here - a project with no
# Blender still dispatches every seat that never calls it, and the adapter
# raises its own explanatory error at the call site if one ever does.
_TOOLCHAIN: dict[str, str] = {}
_TOOLCHAIN_LOCK = threading.Lock()


def _toolchain_env() -> dict[str, str]:
    """{BGATE_GODOT: path, ...} for every tool that resolves right now."""
    from bgate_adapters.blender import find_blender
    from bgate_adapters.godot import find_godot
    from bgate_adapters.recorder import find_ffmpeg

    finders = (("BGATE_GODOT", find_godot),
               ("BGATE_BLENDER", find_blender),
               ("BGATE_FFMPEG", find_ffmpeg))
    with _TOOLCHAIN_LOCK:
        for var, finder in finders:
            if var in _TOOLCHAIN:
                continue
            # An override already in the dashboard's environment WINS. It is how
            # a human pins one specific build, and re-resolving would discard
            # that for whatever the glob happens to rank highest.
            existing = os.environ.get(var, "").strip()
            if existing:
                _TOOLCHAIN[var] = existing
                continue
            try:
                path = finder()
            except Exception:
                continue
            if path:
                _TOOLCHAIN[var] = str(path)
        return dict(_TOOLCHAIN)


def _toolchain_rule() -> str:
    """The one line that stops an agent going looking for a binary."""
    resolved = _toolchain_env()
    if not resolved:
        return ("- Never search the filesystem for a program. bgate_doctor "
                "reports every dependency, with its path, in ONE call.\n")
    names = ", ".join("$" + var for var in sorted(resolved))
    return ("- The toolchain is ALREADY RESOLVED in your environment: "
            f"{names}. Never search for a binary - no `find /`, no drive-wide "
            "glob. One of those ran for 22 minutes, outlived the agent, and "
            "returned nothing to anybody. bgate_doctor reports every "
            "dependency, with its path, in ONE call.\n")


def _image_policy(root: str, item: dict, native: bool) -> str:
    """What the art seat is told about where pixels come from.

    ONLY THE GENERATION CALL MOVES. Everything the art seat does around a
    generation - reading the pinned refs, holding the lock, checking the alpha
    and the consistency, registering the artifact, delivering into the engine - is the same work on either backend, and an agent that reads "generate
    natively" as "skip the pipeline" produces an unregistered PNG that no
    review, no ledger and no consistency gate has ever seen. So the native
    branch spends most of its words on what has NOT changed.

    The bgate branch says nothing at all: image_generate is already the only
    image tool in the process (runners.py disables the CLI's own), the seat
    brief already covers it, and a paragraph restating the default would just
    be prompt weight on every art dispatch.
    """
    if not native or (item.get("seat") or "").strip().lower() != "art":
        return ""
    return (
        "IMAGE BACKEND FOR THIS RUN: NATIVE.\n"
        "Generate the pixels with your own image tool rather than "
        "image_generate. That is the ONLY step that changes, and the rest is "
        "not optional because you made the file yourself:\n"
        "- Read the pinned references FIRST (ref_list) and condition on them. "
        "Nothing else is enforcing the project's look on a native generation.\n"
        "- asset_lock the target path before you write it, asset_release after.\n"
        "- consistency_check every frame you intend to keep, and clear the "
        "alpha flags. A native generation is not exempt from the halo.\n"
        "- Register what you kept so it exists to the ledger and to review: "
        "asset_track, then the artifact record with producer set to the CLI "
        "that made it and the prompt you actually used.\n"
        "- Deliver into the engine and verify there (godot_import_asset), "
        "because the engine's view is still the truth.\n"
        "BGATE_IMAGE_MODEL DOES NOT REACH YOUR OWN TOOL. If this project bans a "
        "model, honour it yourself - nothing downstream will catch it for you.\n"
        "If your tool cannot do what the task needs, fall back to "
        "image_generate rather than shipping something worse."
    )


def _persona_line(root: str, seat: str) -> str:
    """This project's PERSONALITY for the seat, as an instruction to the agent.

    THE ONE THING ON THE FLOOR'S PERSONA THAT CHANGES WHAT AN AGENT DOES, and
    it is deliberately the last thing in the prompt rather than the first: it
    is a note about MANNER, and it must not be read as a change of job. The
    wording below says that in the prompt itself, because a personality that
    quietly outranks the seat's mission is a way to talk an agent out of its
    lanes with a text box that looks like a bit of fun.

    Empty for every project that has not written one, which is all of them
    until somebody types in the seat's Look panel - so the default dispatch
    prompt is byte for byte what it was.
    """
    try:
        from bgate_core import seats as _s
        table = _s.roles_for(root)
        style = ((table.get(seat) or {}).get("persona") or {}).get("style")
    except Exception:
        return ""
    style = " ".join(str(style or "").split())
    if not style:
        return ""
    return (
        "HOW THIS SEAT CARRIES ITSELF (this project's own note, and it is "
        "about MANNER ONLY):\n"
        f"{style}\n"
        "It changes your tone, not your job. It does not widen your write "
        "lanes, relax a gate, skip a protocol step, or override anything above "
        "- where it conflicts with your mission or these instructions, they "
        "win and you carry on as normal."
    )


def _prompt_for(root: str, item: dict, native_images: bool = False) -> str:
    from bgate_core.seats import SEAT_IDENTITY

    seat_rule = seat_rules(root, item["seat"])
    policy = _image_policy(root, item, native_images)
    persona = _persona_line(root, item["seat"])
    return (
        SEAT_IDENTITY + "\n\n"
        f"You are the {item['seat'].upper()} seat of the Builders Gate game project "
        "in the current directory. The builders-gate MCP tools are available to you "
        "NATIVELY - no runner scripts.\n\n"
        f"WORK ITEM #{item['id']} ({item['source']}): {item['title']}\n"
        f"{item['brief']}\n\n"
        + (seat_rule + "\n\n" if seat_rule else "")
        + (policy + "\n\n" if policy else "")
        + "Protocol, in order:\n"
        "1. seat_brief for your role, ONCE. It carries mission, lanes, bible, "
        "pinned refs, notes and the BOARD - what every other agent is queued "
        "for or working on right now. Read the board before touching a file a "
        "dispatched peer owns or duplicating queued work; a second brief call "
        "returns the same payload and costs what the first one did.\n"
        "2. Do the work inside your lanes. The PreToolUse hook enforces them, "
        "so write and let it refuse - seat_can_write is for when you need to "
        "know BEFORE a long generation, not a checkpoint before every edit. "
        "Lock binaries before editing.\n"
        "   OUT-OF-LANE WORK IS ROUTED, NEVER DROPPED. If this task needs a "
        "write another seat owns, queue_add(<that seat>, title, brief) files "
        "it for an agent that CAN write it - pass depends_on="
        f"{item['id']} when it needs this item's output - then keep working "
        "on what is yours. A seat note, a LEFTOVERS block or a 'blocked' "
        "result dispatches NOBODY; only a queue row does. Handing work on IS "
        "part of finishing yours, and a result paragraph that names the items "
        "you filed is a finished handoff.\n"
        f"3. Verify per house norms: {_verify_rule(root)}\n"
        f"4. Mark the item: call queue_complete with item_id={item['id']} and a "
        "one-paragraph result (status 'done', or 'failed' with the honest "
        "reason). That paragraph IS the record - no separate note is owed.\n"
        "5. KEEP THE SEAT WARM. Before queue_complete, you may call "
        "queue_claim_next() to claim the next READY item for your seat - then "
        "complete this item and continue with the claimed one under the same "
        "rules. Claim FIRST: once your item completes with nothing claimed, "
        "the harness closes this session. An empty claim means you are done - "
        "just complete and finish.\n"
        "\n"
        "Also: seat_post_note only when another seat must know something your "
        f"result paragraph will not tell them. Append to .bgate/progress/item-"
        f"{item['id']}.jsonl only on a handoff or a failure worth resuming from "
        " - it is a trail for whoever picks this up, not a log of your turns.\n"
        # WHY THIS SECTION EXISTS, measured on 2026-08-08 across 60 runs: 8,304
        # model calls, 42 of them on average before the first productive one,
        # and 1.19 BILLION input-side tokens against 77k of output. Nothing was
        # expensive because agents wrote a lot. It was expensive because every
        # turn re-sends the whole context, so a wasted turn is not free - it is
        # priced at the size of everything read so far. That makes turn count,
        # not token count, the thing an agent controls.
        "\n"
        "SPEND TURNS LIKE THEY COST SOMETHING - they do, and it is not linear. "
        "Every turn re-sends everything already in your context, so turn 90 "
        "bills for all 89 before it:\n"
        "- Read and Grep are for files. sed/head/cat/`python -c` in Bash pay a "
        "full round-trip to do what one Read does, and last run that was 2,171 "
        "shell calls against 890 reads.\n"
        "- Read a file, a screenshot or a reference ONCE. It stays in context; "
        "re-reading buys nothing and is charged on every turn after it. One "
        "run re-read the same paths 22 times.\n"
        "- Batch independent commands into one call rather than one per line.\n"
        "- Orient enough to act, then act. Reading more of the codebase is the "
        "most expensive way to avoid starting.\n"
        + _toolchain_rule()
        # What "done" costs and who checks it, stated up front. An agent that
        # thinks its word closes the item writes a thinner result note than one
        # that knows a picky reviewer - or the owner - reads it next.
        + "\n" + _gates.describe(root, item["seat"]) + "\n"
        + (f"\nCHAIN: this item is link {item['chain_pos']} of chain "
           f"{item['chain_id']}. Work waiting on yours does not start until this "
           "item reaches 'done', so an honest 'failed' is cheaper than a "
           "hopeful one - a wrong 'done' releases the next agent onto a "
           "foundation that is not there.\n" if item.get("chain_id") else "")
        # LAST, AND ON PURPOSE. A note about manner belongs after the job, the
        # lanes and the gates, so it reads as colour on top of the work rather
        # than as the brief itself.
        + ("\n" + persona + "\n" if persona else "")
    )


# WHAT THE SPAWNED AGENT'S ENVIRONMENT IS ALLOWED TO CONTAIN.
#
# This used to be `{**os.environ, ...}`. The dashboard is long-lived, started
# from the user's own shell, and holds every credential that shell holds - not
# just the ones Builders Gate uses. An agent with a Bash tool and an unfiltered
# copy of that environment can read a cloud token, a database URL or a signing
# key that has nothing to do with making a game, and the first place it would
# show up is a log file it wrote itself.
#
# THE FAILURE MODE ON THE OTHER SIDE IS WORSE, so this list is generous where a
# variable is plainly infrastructure and stingy only where it is plainly a
# secret. Breaking a working agent costs a run and a confused user; the leak
# costs a credential. Every entry below says why it survives.
_ENV_PASSTHROUGH: tuple[str, ...] = (
    # --- the process can start at all -------------------------------------
    # PATH finds the CLI and everything it shells out to. PATHEXT and COMSPEC
    # are how Windows resolves a bare `git` or `npm` to git.exe / a .cmd shim;
    # without PATHEXT a node-installed CLI is unlaunchable.
    "PATH", "PATHEXT", "COMSPEC",
    # SystemRoot is load-bearing on Windows in a way it does not look: the
    # socket stack (WSAStartup) fails to initialise without it, so an agent
    # spawned without it starts and then cannot make a single HTTPS call.
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE",
    "OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
    # ProgramFiles is how find_godot/find_blender name their install roots, and
    # how a child that re-resolves them finds the same thing this process did.
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA",
    # --- scratch space ------------------------------------------------------
    # Every atomic write, every Blender and ffmpeg intermediate, and the aegis
    # allowlist's own temp-dir entry resolve through these. Denying them does
    # not contain anything; it breaks the toolchain.
    "TEMP", "TMP", "TMPDIR",
    # --- who the user is ----------------------------------------------------
    # git reads ~/.gitconfig, the CLIs read their own config under the profile,
    # and Path.home() is these variables. An agent with no home directory
    # commits as nobody and re-authenticates on every run.
    "HOME", "HOMEDRIVE", "HOMEPATH", "USERPROFILE", "USERNAME", "USER",
    "APPDATA", "LOCALAPPDATA",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    # --- the network the toolchain has to reach -----------------------------
    # A corporate machine reaches the API only through the proxy, and only
    # trusts the MITM root through these bundle vars. Dropping them turns every
    # provider call into an unexplained TLS failure. They are addresses and
    # file paths, not credentials.
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    # --- the interpreter that hosts the MCP server --------------------------
    # The agent's CLI spawns `sys.executable -m bgate_mcp.server` (runners.py,
    # mcp_overrides). That import only resolves if the venv it was installed
    # into is still described here. PYTHONUTF8/IOENCODING keep a non-ASCII
    # asset path from raising UnicodeEncodeError at the process boundary.
    "PYTHONPATH", "PYTHONHOME", "PYTHONUTF8", "PYTHONIOENCODING",
    "VIRTUAL_ENV", "CONDA_PREFIX",
    # --- the agent CLI's own identity and config ----------------------------
    # CLAUDE_CONFIG_DIR and CODEX_HOME are already read by first-party code, so
    # a dashboard started with either pointed somewhere non-default must hand
    # the child the same one or the child reads a different account's config.
    # ANTHROPIC_* / OPENAI_BASE_URL are the CLI's endpoint and key: the run does
    # not happen without them, which is the entire point of the spawn.
    "CLAUDE_CONFIG_DIR", "CODEX_HOME",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
    "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
    # --- provider keys the MCP tools genuinely require ----------------------
    # NOT a second copy of the provider table: the registered ones are pulled
    # from bgate_core.providers below, so a provider added there is not
    # silently scrubbed into a "key not set" that names the wrong cause. These
    # two are the ones that live outside that table - STABILITY_API_KEY is
    # declared inline in the imageto3d backend table, and TWITCH_CHANNEL is the
    # half of the Twitch pair that is a channel name rather than a token.
    "STABILITY_API_KEY", "TWITCH_CHANNEL",
)

# Everything in the harness's own namespace rides through as a prefix rather
# than a list. Two reasons it is safe: these are settings, paths and ids that
# this process itself wrote or that the user set FOR Builders Gate, and the
# registry (settings.py) keeps adding to them - an allowlist enumerated by hand
# would silently stop honouring the newest toggle, which is a bug that looks
# like "the Settings panel does nothing".
_ENV_PREFIXES: tuple[str, ...] = ("BGATE_",)


def _provider_env_vars() -> tuple[str, ...]:
    """Every credential var a registered provider reads, from the table itself.

    Derived rather than duplicated so that adding a Provider row cannot leave
    its key scrubbed out of the one process that needs it. A failure here is
    not fatal: the hand-written list above still covers the toolchain, and an
    art call answers with its own "key not set" message.
    """
    try:
        from bgate_core.providers import PROVIDERS
    except Exception:
        return ()
    return tuple(p.env for p in PROVIDERS if getattr(p, "env", ""))


def _scrubbed_environ() -> dict[str, str]:
    """os.environ, reduced to what a seat needs.

    Case-insensitive matching because Windows environment names are, and the
    same variable arrives as SystemRoot, SYSTEMROOT or systemroot depending on
    who set it. The ORIGINAL name is what gets passed on - CreateProcess does
    not care, but a POSIX child does, and this module is not the place to
    rename someone's variable.
    """
    allowed = {name.upper() for name in _ENV_PASSTHROUGH}
    allowed.update(name.upper() for name in _provider_env_vars())
    prefixes = tuple(p.upper() for p in _ENV_PREFIXES)
    kept: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in allowed or upper.startswith(prefixes):
            kept[name] = value
    return kept


def _contained(path: str, root: str) -> bool:
    """Is ``path`` the project root or something inside it?

    normcase + normpath because the supported platform is Windows, where
    C:\\Games\\Ember and c:/games/ember are the same directory and a plain
    string compare says they are not. Symlinks are resolved first so a junction
    pointing out of the tree cannot be laundered into looking contained.
    """
    try:
        target = os.path.normcase(os.path.normpath(str(Path(path).resolve())))
        base = os.path.normcase(os.path.normpath(str(Path(root).resolve())))
    except OSError:
        return False
    return target == base or target.startswith(base + os.sep)


# Flags that hand a CLI a directory it may work in. Checked by value, not by
# trusting runners.py to stay correct: neither runner passes anything outside
# the project today (claude takes no --add-dir here, codex takes --cd cwd), and
# this is what keeps that true after the next flag is added.
_DIR_GRANTING_FLAGS = ("--add-dir", "--cd", "--workdir", "--directory")


def _escaping_dir_flag(args: list[str], root: str) -> Optional[str]:
    """The first directory-granting flag pointing outside ``root``, if any."""
    for flag, value in zip(args, args[1:]):
        if flag in _DIR_GRANTING_FLAGS and not _contained(value, root):
            return f"{flag} {value}"
    return None


def _live_count() -> int:
    return sum(1 for e in _live.values() if e["proc"].poll() is None)


def dispatch(root: str, item_id: int, **kwargs) -> dict:
    """Spawn one agent for one item, with the start RESERVED against a race.

    Everything _spawn does between its `_live` check and the actual Popen - the
    scope check, the budget check, git dirty-state, cutting a worktree - takes
    seconds, and the lock is not held across it. Two callers racing through that
    window both saw `_live` empty and both spawned a claude tree; the second
    entry overwrote the first in `_live`, so the first process was never reaped,
    never budget-checked and never killed. It billed until somebody found it in
    Task Manager, and it also let the concurrency cap be exceeded.

    That race is now routine rather than theoretical: the auto-deploy thread
    ticks every few seconds and the autopilot endpoint calls tick() inline on a
    request thread. So the reservation is taken under the lock, before any of
    the slow work, and released in a finally whatever happens.
    """
    with _lock:
        if item_id in _live and _live[item_id]["proc"].poll() is None:
            return {"ok": False, "error": f"item {item_id} already has a live agent"}
        if item_id in _starting:
            return _refuse("already_starting",
                           f"item {item_id} is already being dispatched")
        _starting.add(item_id)
    try:
        return _spawn(root, item_id, **kwargs)
    finally:
        with _lock:
            _starting.discard(item_id)


def _spawn(root: str, item_id: int, *, permission_mode: str = "acceptEdits",
           model: Optional[str] = None, max_runtime_s: Optional[int] = None,
           max_cost_usd: Optional[float] = None,
           allow_dirty: Optional[bool] = None,
           actor: str = "") -> dict:
    """Spawn a Claude session against a queued item. One per item.

    Four things must be true before a process exists: the CLI is there, the item
    is dispatchable, the fleet is under its concurrency cap, and the projected
    cost fits the budget. Then the git boundary is captured - without a
    base_commit nothing downstream can show or undo what the agent did.
    """
    try:
        item = _queue.get(root, item_id)
    except LookupError:
        # A stale dashboard tab dispatching a deleted item used to escape as a
        # LookupError and land in the user's face as a 500 stack.
        return _refuse("not_found", f"work item {item_id} does not exist",
                       item_id=item_id)
    if item["status"] != "queued":
        extra = ("" if item["status"] != "review" else
                 " - it is finished and waiting for approval, not for an agent")
        return {"ok": False,
                "error": f"item {item_id} is {item['status']}, not queued{extra}"}

    # THE CHAIN. A link whose predecessor has not landed must not start, and this
    # is the last gate before a process exists - autodeploy filters blocked items
    # out of its candidate list, but "dispatch all" and the per-row button do not.
    held = _queue.blocker(root, item_id)
    if held:
        return _refuse("blocked_on_dependency",
                       f"item {item_id} waits on #{held['id']} "
                       f"[{held['seat']}] which is {held['status']}: "
                       f"{str(held['title'])[:70]}",
                       item_id=item_id, waiting_on=held["id"],
                       waiting_on_status=held["status"])

    # A cut-line re-check used to sit here, refusing to spend an agent on an
    # item whose scope tier had fallen below the line since it was queued. The
    # tier system is gone (nothing was ever filed under a tier, so this check
    # never refused a dispatch either); the chain gate above is now the last
    # thing between a queued item and a process.
    with _lock:
        if item_id in _live and _live[item_id]["proc"].poll() is None:
            return {"ok": False, "error": f"item {item_id} already has a live agent"}
        # Server-side, because the dashboard's "dispatch all" loops every queued
        # item with no cap of its own - 20 agents is 20 claude trees, each with
        # its own MCP children, on one laptop.
        cap = int(_spend.budget(root).get("max_concurrent") or 0)
        running = _live_count()
        if cap and running >= cap:
            return _refuse("concurrency_limit",
                           f"{running} agents already running - the cap is {cap}",
                           running=running, max_concurrent=cap)

    # Ceilings: the item's own overrides win, then this call's, then the budget.
    ceiling_usd = float(max_cost_usd or _spend.item_ceiling(root, item) or 0)
    ceiling_s = int(max_runtime_s or _spend.runtime_ceiling(root, item) or 0)
    verdict = _spend.check(root, projected_usd=ceiling_usd)
    if not verdict["allowed"]:
        # budget.refused ships in the default notify.kinds, and this is the
        # refusal it is for: a board that stops dispatching because it hit a
        # ceiling looks exactly like a board with nothing to do, and the console
        # shows the reason only in the "last refusal" slot of whoever asked.
        _emit(root, "budget.refused", ref=str(item_id),
              payload={"what": "agent dispatch", "item": item_id,
                       "seat": item.get("seat") or "",
                       "title": str(item.get("title") or "")[:200],
                       "reason": str(verdict.get("reason") or ""),
                       "projected_usd": ceiling_usd,
                       "scope": verdict.get("scope"),
                       "spent": verdict.get("spent"),
                       "ceiling": verdict.get("ceiling")})
        return _refuse("budget_exceeded", verdict["reason"],
                       projected_usd=ceiling_usd, **{
                           k: v for k, v in verdict.items()
                           if k in ("scope", "spent", "ceiling")})

    # THE RESERVATION - the queued->dispatched transition, taken atomically and
    # FIRST. There are two dispatchers now (this function, and a worker's
    # queue_claim_next in the MCP server process), and the old shape - read
    # 'queued' here, write 'dispatched' after Popen - left a seconds-wide window
    # in which both proceeded and one item got two agents. The UPDATE's WHERE
    # clause means exactly one caller wins; every refusal past this point puts
    # the item back through release(), which touches nothing but the status.
    if not _queue.reserve(root, item_id):
        return _refuse("not_queued",
                       f"item {item_id} was taken by another dispatcher between "
                       "the read and the reservation", item_id=item_id)

    def _refused(code: str, message: str, **detail) -> dict:
        _queue.release(root, item_id)
        return _refuse(code, message, **detail)

    # The git boundary. A run dispatched on top of uncommitted work produces a
    # diff that cannot tell the agent's edits from the human's, so mixing them
    # has to be asked for.
    if allow_dirty is None:
        allow_dirty = _flag(root, "dispatch.allow_dirty", "BGATE_ALLOW_DIRTY")
    state = _git.dirty(root)
    if state["available"] and state["dirty"] and not allow_dirty:
        return _refused(
            "dirty_tree",
            f"{len(state['paths'])} uncommitted change(s) in the tree, so the "
            "agent's edits could not be told from yours. Commit or stash them "
            "and the board resumes on its own. (Finished runs commit their own "
            "files when dispatch.auto_commit is on, which it is by default - so "
            "what is dirty here is work the harness did not do.)",
            paths=state["paths"][:50])
    base_commit = _git.head(root) if state["available"] else ""
    branch, worktree = "", ""
    cwd = str(root)
    # Through the registry (which declares BGATE_GIT_ISOLATION as the supplying
    # var, so the variable still wins) rather than gitwork's env-only reader,
    # which left the Settings toggle writing a value nothing consulted.
    if base_commit and _flag(root, "dispatch.isolation", "BGATE_GIT_ISOLATION"):
        made = _git.make_worktree(root, item_id, base=base_commit)
        if not made["available"]:
            return _refused("worktree_failed", made["reason"])
        branch, worktree = made["branch"], made["worktree"]
        cwd = worktree

    # WHICH CLI, and can it start HERE. Both checks belong after cwd is final:
    # a worktree moves it, and codex's git-repo precondition is about the
    # directory the agent will actually run in. Refusing now costs a message;
    # refusing later costs a process that reports success and writes into a
    # sandbox shadow.
    runner = _runner_for(root, item.get("seat") or "")
    exe = _executable(runner)
    blocked = _runners.preflight(runner, cwd, exe=exe)
    if blocked:
        return _refused("runner_unavailable", blocked, runner=runner.name)
    native_images = _native_images(root, runner)

    log_dir = Path(root) / ".bgate" / "agents"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _refused("spawn_failed", f"cannot create the agent log dir: {exc}")
    log_path = log_dir / f"item-{item_id}.log"

    env = {
        # NOT os.environ. See _scrubbed_environ: the dashboard's whole
        # environment used to reach the agent, credentials the game has no use
        # for included.
        **_scrubbed_environ(),
        # Resolved paths, so no agent ever has a reason to go looking for a
        # binary. Placed after the scrub deliberately, because _toolchain_env has
        # already preferred any override that was set there.
        **_toolchain_env(),
        "BGATE_SEAT": item["seat"],
        "BGATE_ROOT": str(root),
        # THE PROJECT BOUNDARY, STATED ONCE. The hook (bgate_cli/hook.py) and
        # the MCP server both ask aegis.mode() how hard to enforce it, and both
        # read it from this variable. Passing the dashboard's NORMALISED mode
        # rather than letting each child re-read a raw string means a typo in
        # the user's shell cannot have the hook enforcing one policy while the
        # server enforces another. aegis.mode() maps anything unrecognised to
        # the default, and it maps it the same way for everybody here.
        "BGATE_AEGIS": _aegis.mode(),
        "BGATE_WORK_ITEM": str(item_id),
        "BGATE_LOCK_OWNER": f"item-{item_id}",
        # Who this session is, for anything that asks whether a human is
        # responsible. Without it a spawned agent inherits the dashboard user's
        # identity and can approve its own work - it did, until this line.
        "BGATE_ACTOR": f"agent:item-{item_id}",
        # Director directive: gpt-image-2 is banned - force 1 for every gen.
        "BGATE_IMAGE_MODEL": os.environ.get("BGATE_IMAGE_MODEL", "gpt-image-1"),
    }
    # The flags each CLI needs to stream its work live are that CLI's business - # see runners.py, which also records what each one CANNOT do (steering, cost)
    # so the rest of this module stops assuming both.
    # An explicit model on the call wins; otherwise the seat's configured one.
    # Before _model_for existed this was `model` alone, which every caller left
    # as None - see _model_for for what that cost.
    model = model or _model_for(root, item.get("seat") or "")
    args = runner.build_args(exe, permission_mode=permission_mode,
                             model=model, cwd=cwd, native_images=native_images,
                             max_turns=_max_turns(root))

    # CONTAINMENT, CHECKED AT THE SPAWN RATHER THAN TRUSTED.
    #
    # BGATE_ROOT above is what the agent is TOLD it owns; these two lines are
    # what make it true of the process. cwd is either the root or a worktree
    # under <root>/.bgate/work/, so no case needs excusing. Anything else got
    # here through a caller passing a directory that is not this project's, and
    # an agent that starts outside its root writes outside its root while the
    # hook is still judging it against the project it was pinned to.
    if not _contained(cwd, root):
        return _refused("cwd_escape",
                        f"the agent's working directory {cwd} is outside the "
                        f"project it was dispatched for ({root})")
    escape = _escaping_dir_flag(args, root)
    if escape:
        return _refused("arg_escape",
                        f"{runner.name} would be given `{escape}`, which grants "
                        f"the agent a directory outside {root}")

    try:
        log_handle = open(log_path, "ab")
    except OSError as exc:
        return _refused("spawn_failed", f"cannot open the agent log: {exc}")
    # RUN BOUNDARY: the log appends across re-dispatches, and both the activity
    # view and the steer-echo scanner must only look at THIS run - the stale
    # first-run result being shown as current, and old echoes falsely marking
    # fresh steers consumed, were real observed bugs. Marker + byte offset.
    import time as _time
    log_handle.write((json.dumps({"type": "bgate_run_start",
                                  "item_id": item_id,
                                  "base_commit": base_commit,
                                  "ts": _time.time()}) + "\n").encode("utf-8"))
    log_handle.flush()
    run_start_pos = log_handle.tell()
    try:
        proc = subprocess.Popen(args, cwd=cwd, env=env,
                                stdin=subprocess.PIPE, stdout=log_handle,
                                stderr=log_handle, creationflags=_NO_WINDOW)
    except OSError as exc:
        log_handle.close()
        return _refused("spawn_failed", f"could not start the agent CLI: {exc}")
    # Deliver the task. A streaming runner takes it as the first user message
    # and keeps the pipe open as a steer channel; `codex exec` reads stdin ONCE
    # and acts on what it got, so the pipe has to be closed or the run never
    # starts - the difference between "waiting for more input" and "hung" is
    # invisible from out here, which is why prompt_via is declared rather than
    # inferred.
    prompt = _prompt_for(root, item, native_images=native_images)
    try:
        if runner.prompt_via == "stream":
            proc.stdin.write(_user_msg(prompt).encode("utf-8"))
            proc.stdin.flush()
        else:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.flush()
            proc.stdin.close()
    except OSError as exc:
        proc.kill()
        _queue.release(root, item_id)
        return {"ok": False, "error": f"could not send prompt to agent: {exc}"}
    with _lock:
        _live[item_id] = {"proc": proc, "log": str(log_path), "handle": log_handle,
                          "stdin": proc.stdin, "steers": [],
                          "stdin_closed": runner.prompt_via != "stream",
                          # Carried on the run, not looked up later: the setting
                          # can be changed while this agent is mid-flight, and
                          # what it is running under is a fact about the run.
                          "runner": runner.name,
                          # Carried for the auto-commit message: _finalize runs
                          # after the item may already have been reopened, and
                          # re-reading the row then would name the wrong round.
                          "seat": item.get("seat") or "",
                          "cost_tracked": runner.cost_tracked,
                          "steerable": runner.steerable,
                          "native_images": native_images,
                          "log_scan_pos": run_start_pos,
                          "run_start_pos": run_start_pos,
                          "cost_scan_pos": run_start_pos,
                          "started_at": _time.monotonic(),
                          "max_runtime_s": ceiling_s, "max_cost_usd": ceiling_usd,
                          "base_commit": base_commit, "cwd": cwd,
                          # The project this run belongs to. status()/stop() and
                          # the sweep all need it and only the spawner knows it;
                          # stop(item_id) has no root argument to be given one.
                          "root": str(root), "actor": actor,
                          "branch": branch, "worktree": worktree}
    # Status is already 'dispatched' - the reservation above wrote it before any
    # of the slow work, which is what makes two dispatchers safe against each
    # other. Writing it again here would wipe the item's result note for nothing.
    _queue.set_run_fields(root, item_id, base_commit=base_commit, branch=branch,
                          worktree=worktree, actor=actor or None,
                          max_cost_usd=ceiling_usd or None,
                          max_runtime_s=ceiling_s or None)
    _record_pid(root, proc.pid, item_id)
    # And in the MACHINE-WIDE registry, which is the one that survives this
    # process. pids.json above is per project and is read by the orphan sweep;
    # this entry is what lets a restarted dashboard - or a second one, or one
    # opened on a different project entirely - see and stop this agent at all.
    _agentreg.record(proc.pid, item_id=item_id, seat=item.get("seat") or "",
                     root=str(root), runner=runner.name, log=str(log_path))
    # The streamed session waits on stdin forever; close it once the agent
    # self-reports so it exits even when no dashboard is polling /api/agents.
    threading.Thread(target=_watch_completion, args=(root, item_id),
                     daemon=True).start()
    _emit(root, "agent.spawned", ref=str(item_id),
          payload={"item": item_id, "seat": item.get("seat") or "",
                   "title": str(item.get("title") or "")[:200],
                   "source": item.get("source") or "",
                   "chain_id": item.get("chain_id") or "",
                   "chain_pos": int(item.get("chain_pos") or 0),
                   "attempts": int(item.get("attempts") or 0),
                   "pid": proc.pid, "actor": actor,
                   "max_cost_usd": ceiling_usd, "max_runtime_s": ceiling_s,
                   "runner": runner.name, "cost_tracked": runner.cost_tracked,
                   "native_images": native_images,
                   "worktree": worktree})
    return {"ok": True, "item_id": item_id, "pid": proc.pid, "log": str(log_path),
            "base_commit": base_commit, "branch": branch, "worktree": worktree,
            "max_runtime_s": ceiling_s, "max_cost_usd": ceiling_usd,
            "runner": runner.name, "cost_tracked": runner.cost_tracked,
            "native_images": native_images}


def _observed_cost(entry: dict) -> float:
    """The highest ``total_cost_usd`` the CLI has reported THIS run.

    The session only prints cost at result boundaries, so this is a step
    function, not a live meter - it trips at the first boundary past the
    ceiling, which still bounds the damage. Incremental tail read (own cursor,
    separate from the steer scanner's) so polling a 10MB log costs nothing."""
    best = float(entry.get("cost_usd", 0.0))
    try:
        with open(entry["log"], "rb") as fh:
            fh.seek(entry.get("cost_scan_pos", entry.get("run_start_pos", 0)))
            chunk = entry.get("cost_scan_rem", b"") + fh.read()
            entry["cost_scan_pos"] = fh.tell()
    except OSError:
        return best
    lines = chunk.split(b"\n")
    entry["cost_scan_rem"] = lines.pop()  # possibly-partial last line
    for line in lines:
        if b"total_cost_usd" not in line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        val = ev.get("total_cost_usd") if isinstance(ev, dict) else None
        if isinstance(val, (int, float)) and float(val) > best:
            best = float(val)
    entry["cost_usd"] = best
    return best


# Terminal ``result`` subtypes: the CLI saying this session is over and it did
# not work. "success" is deliberately absent - see the call site in
# _watch_completion for why a successful result must NOT settle the run.
_ERROR_SUBTYPES = ("error", "error_during_execution", "error_max_turns",
                   "error_max_tokens")


def _terminal_error(root: str, item_id: int) -> str:
    """The sentence to fail this run with, or "" if it has not errored out.

    Read off the CLI's own terminal event, so it carries the CLI's words - "Failed to authenticate: OAuth session expired" is an actionable line, and
    a generic 'the agent stopped' is not.
    """
    final = _final_event(root, item_id)
    if not final:
        return ""
    subtype = str(final.get("subtype") or "")
    said = str(final.get("text") or "").strip()
    errored = final.get("is_error") is True or subtype in _ERROR_SUBTYPES
    if not errored and subtype and subtype != "success":
        # Some builds report an auth/setup failure as a plain result with no
        # error flag at all; the CLI's own wording is the only signal left.
        # Gated on the subtype NOT being success, because an agent reporting
        # ABOUT auth - "failed to authenticate against the test fixture, so I
        # stubbed it" - is a run that worked, and reaping it throws the work
        # away over a sentence.
        low = said.lower()
        errored = low.startswith("failed to authenticate") or (
            "oauth" in low and "expired" in low)
    if not errored:
        return ""
    return (f"the session ended in error ({subtype or 'error'})"
            + (f": {said[:400]}" if said else "")
            + " - it was not going to do anything else, so it was reaped")


# Where the WRAP-UP steer goes out, as a fraction of the cost ceiling. A hard
# kill at the ceiling is the last resort and it destroys the run's own account of
# what it did; a message at 80% gives the agent a turn or two to save, report and
# call queue_complete with a partial result, which is the difference between a
# recoverable stop and an autopsy.
WRAP_AT = 0.8

WRAP_TEXT = (
    "BUDGET STOP. You have spent ${spent:.2f} of a ${limit:.2f} ceiling on this "
    "item. Start NOTHING new - no new generations, no new files, no further "
    "exploration. Finish or abandon the edit in front of you, then call "
    "queue_complete IMMEDIATELY with a partial result that says plainly: what is "
    "done and verified, what is half-done and where, and what is untouched. A "
    "partial result you report yourself is worth far more than the kill that is "
    "coming at ${limit:.2f}, which reports nothing.")


def _send(entry: dict, text: str) -> bool:
    """Push a user turn into a live agent, from inside the watchdog.

    dispatch.steer() is the public path and takes the registry lock; the
    watchdog is already inside its own critical section, so this is the same
    write without the re-entry.
    """
    if not entry.get("steerable", True) or entry.get("stdin_closed"):
        return False
    try:
        entry["stdin"].write(_user_msg(text).encode("utf-8"))
        entry["stdin"].flush()
        return True
    except (OSError, AttributeError):
        return False


def _trip(root: str, item_id: int, entry: dict, reason: str,
          recoverable: bool = True) -> None:
    """A ceiling the agent blew through: stop the tree and BANK what it wrote.

    THE KILL USED TO THROW THE WORK AWAY. MEASURED: an item was killed at $7.17
    against a $5 ceiling having already written every file it was asked for - player.tscn, player_controller.gd and two more, all on disk and all correct.
    The money was spent, the deliverables existed, and the item went to `failed`
    with its result replaced by the kill reason. A human had to go and look at
    the filesystem to find out that the run had actually succeeded.

    So the result now leads with the fact that this was a STOP rather than a
    fault, and `set_status` appends the harness's own observed-writes record
    underneath it - the list a human, or the reopened agent, needs in order to
    pick the work up rather than repeat it.
    """
    _kill_tree(entry["proc"].pid)
    entry["stop_reason"] = reason
    note = reason
    if recoverable:
        note = (reason + "\n\nSTOPPED, NOT FAILED. The ceiling ended this run; "
                "it is not a report that the work is wrong. Anything listed "
                "below EXISTS ON DISK and was written by this run. Read it "
                "before reopening: the next agent should continue from these "
                "files, not regenerate them.")
    try:
        _queue.set_status(root, item_id, "failed", result=note)
    except LookupError:
        pass
    else:
        # ANNOUNCE THE KILL. set_status never emits (by design - see
        # queue.complete), and _reap skips complete() because the item is no
        # longer 'dispatched' - so every ceiling kill was a failure with no
        # item.failed event: no bell, no webhook, no auto-reopen, an item that
        # just sat there. item.failed is in the default notify kinds; the most
        # expensive failures this system has should be the loudest, not the
        # only silent ones.
        try:
            _queue.emit_terminal(root, item_id)
        except Exception:
            pass
    _reap(root, item_id, entry, entry["proc"].poll())


def _watch_completion(root: str, item_id: int, poll_s: float = 2.0,
                      exit_grace_s: float = 90.0) -> None:
    """Close the agent's stdin once it has queue_complete'd, so the waiting
    process reaches EOF and exits - then make SURE it exits. EOF alone proved
    unreliable (agents wedged on child MCP servers piled up 14 orphaned
    claude.exe at peak), so after a grace period the process tree is killed;
    the item is already done, nothing of value is lost.

    This is also the wall clock. The kill grace above only starts once the item
    ALREADY reached done/failed, which is no help against the failure that
    actually costs money: an agent that never self-reports and runs all night.
    Runtime and cost are checked every poll from the moment it spawned.

    And it is where a finished run is BANKED (_reap). It used to be the
    dashboard's GET that did that; a read handler is the wrong owner, and an
    agent whose dashboard nobody had open never got settled at all. The lock is
    held only long enough to look the entry up - every DB and process call below
    happens outside it, or _reap (which takes the lock itself) would deadlock."""
    with _lock:
        watched = _live.get(item_id)
    if watched is None:
        return
    while True:
        time.sleep(poll_s)
        with _lock:
            entry = _live.get(item_id)
        # Bound to THIS run, not to the item id: a re-dispatch of the same item
        # installs a new entry, and a watchdog that kept going would bank the
        # successor's process against the predecessor's project.
        if entry is None or entry is not watched:
            return
        code = entry["proc"].poll()
        if code is not None:
            _reap(root, item_id, entry, code)
            return

        # The ceilings, enforced from spawn - not from completion.
        #
        # NOTHING RUNS UNBOUNDED. The budget's max_runtime_s is settable to 0,
        # which used to mean "no wall clock at all" - an agent that never
        # self-reports then runs until someone notices, which on a machine left
        # alone overnight is the single most expensive failure this system can
        # have. 0 now means the hard cap, not infinity.
        limit_s = int(entry.get("max_runtime_s") or 0) or HARD_RUNTIME_S
        if time.monotonic() - entry["started_at"] >= limit_s:
            _trip(root, item_id, entry,
                  f"killed: exceeded the {limit_s // 60}-minute runtime budget")
            return

        # HUNG, as distinct from slow. A wedged agent - one whose MCP child
        # died holding the pipe - is alive, costs nothing more, and will sit
        # there occupying a concurrency slot until the wall clock finally fires
        # half an hour later. Silence is measured against real output (the log
        # AND files under .bgate_out / game assets) precisely so that a 30-minute
        # atomic image batch, which writes nothing until it returns, is not
        # mistaken for a corpse.
        silent = _last_output_age_s(root, entry)
        if silent is not None and silent >= STALL_S:
            _trip(root, item_id, entry,
                  f"killed: no output of any kind for {silent // 60} minutes - "
                  "the session was hung, not working")
            return
        # THE COST CEILING ONLY EXISTS WHERE COST IS REPORTED. A runner that
        # emits tokens and no price makes _observed_cost read 0.00 forever, so
        # this branch would sit there looking like a live guard while spending
        # nothing it can see. Skipping it explicitly is the honest shape: the
        # run is marked cost_tracked=False at spawn and shown that way, and the
        # runtime and stall limits above - which do not depend on price - are
        # what actually bound it.
        limit_usd = float(entry.get("max_cost_usd") or 0)
        if limit_usd and entry.get("cost_tracked", True):
            spent = _observed_cost(entry)
            # THE CEILING ASKS BEFORE IT KILLS. Seven of nine items in one
            # overnight run breached their ceiling, the worst at 3.3x, and every
            # breach ended as a kill that discarded the run's own account of
            # itself. A kill is a terrible instrument for a limit: it lands
            # mid-turn, after the money is spent, and it cannot bank anything.
            # One message at 80% costs a turn and routinely saves the item.
            if (not entry.get("wrap_sent")
                    and spent >= limit_usd * WRAP_AT
                    and spent <= limit_usd):
                entry["wrap_sent"] = True
                if _send(entry, WRAP_TEXT.format(spent=spent, limit=limit_usd)):
                    entry["steers"].append(
                        {"text": f"budget wrap-up at ${spent:.2f}",
                         "sent_at": time.time(), "consumed_at": None})
                    try:
                        # NOT `_activity` - that name is this module's parsed
                        # per-run log cache, and a dict has no .log(). The
                        # try/except would have swallowed the AttributeError
                        # forever and the wrap-up would have gone unrecorded.
                        from bgate_core import activity as _act

                        _act.log(root, "dispatch",
                                 f"item {item_id}: wrap-up sent at "
                                 f"${spent:.2f} of ${limit_usd:.2f}",
                                 ref=str(item_id))
                    except Exception:
                        pass
            if spent > limit_usd:
                _trip(root, item_id, entry,
                      f"stopped: spent ${spent:.2f} against a "
                      f"${limit_usd:.2f} ceiling"
                      + (" (the wrap-up message went out at "
                         f"${limit_usd * WRAP_AT:.2f} and was not acted on)"
                         if entry.get("wrap_sent") else ""))
                return
        # Renew what this run holds. It lives here rather than in status()
        # because a lease must not depend on somebody having the dashboard open.
        try:
            _assets.heartbeat(root, f"item-{item_id}")
        except Exception:
            pass

        # A RUN THAT ALREADY FAILED IS NOT STILL WORKING.
        #
        # The CLI reports a terminal error - expired OAuth, max turns, an
        # execution error - as a result event and then goes right back to
        # waiting on the stdin we deliberately hold open for steering. Nothing
        # else here notices: the item stays 'dispatched', the process stays
        # alive, and the board says "thinking" for however long the runtime
        # ceiling is (half an hour by default, or forever with no ceiling) over
        # a session that is never going to do anything again. An expired login
        # then reads to the user as a hung dashboard.
        #
        # Only ERROR results settle a run. A successful result event without a
        # queue_complete is an agent pausing mid-work - steerable, and settling
        # that would break the channel this whole file exists to keep open.
        if not entry.get("stdin_closed") and not entry.get("stop_reason"):
            failure = _terminal_error(root, item_id)
            if failure:
                _trip(root, item_id, entry, failure)
                return
        if not entry.get("stdin_closed"):
            try:
                if _queue.get(root, item_id)["status"] in ("done", "failed"):
                    # THE PICKUP LOOP KEEPS THE PIPE OPEN. A worker that called
                    # queue_claim_next before completing its item is still
                    # working - closing stdin here would kill it mid-claim,
                    # which turns the sanctioned loop into a trap. The run's
                    # ceilings (runtime, stall, cost) still bound the whole
                    # session, so this is not an escape from any limit.
                    if _open_claims(root, item_id):
                        continue
                    try:
                        entry["stdin"].close()
                    except OSError:
                        pass
                    entry["stdin_closed"] = True
                    entry["eof_at"] = time.monotonic()
            except LookupError:
                return
            continue
        # stdin closed: give the process the grace period, then kill its
        # whole tree (the agent's own MCP-server children orphan too).
        # setdefault matters: another path (stop, a manual sweep) may close
        # stdin FIRST without stamping eof_at - without this, the default
        # re-evaluated to now() every pass and the kill NEVER fired (the
        # observed doom-loop zombie).
        entry.setdefault("eof_at", time.monotonic())
        if time.monotonic() - entry["eof_at"] >= exit_grace_s:
            _kill_tree(entry["proc"].pid)
            # Do NOT return: the next pass sees the dead process and banks the
            # run. Returning here left the entry in _live with a corpse in it,
            # which is exactly the stuck row this file keeps growing scars over.
            entry["eof_at"] = time.monotonic()


def _open_claims(root: str, item_id: int) -> list[dict]:
    """Items this run claimed via queue_claim_next and has not yet settled.

    The claim stamp is the actor column (agent:item-<original id>), written by
    queue.claim_next in the MCP server process - the DB is the only channel the
    two processes share, which is why this is a query and not a field on the
    _live entry. Best-effort: an unreadable DB answers 'no claims', which errs
    toward closing a session rather than keeping a corpse alive.
    """
    try:
        return _queue.claimed_open(root, f"agent:item-{item_id}")
    except Exception:
        return []


def run_record_path(root: str, item_id: int) -> Path:
    """Where a finished run's git fingerprint lives (see :func:`_finalize`)."""
    return Path(root) / ".bgate" / "agents" / f"item-{item_id}.run.json"


def read_run_record(root: str, item_id: int) -> dict:
    try:
        return json.loads(run_record_path(root, item_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _finalize(root: str, item_id: int, entry: dict) -> None:
    """Bank what the run cost and what it touched, exactly once.

    Cost used to be parsed off the final result event and handed to an ephemeral
    JSON response - nothing summed it, so no one could answer what a night of
    fan-out cost. It now lands on the item and in the spend ledger.

    The fingerprint is the other half of revert: hashes of every path this run
    changed, taken the moment it ended. Revert compares against it and refuses
    rather than discarding an edit someone made afterwards."""
    if entry.get("finalized"):
        return
    entry["finalized"] = True
    try:
        final = read_activity(root, item_id).get("final") or {}
        cost = final.get("cost")
        turns = final.get("turns")
        if turns:
            _queue.set_run_fields(root, item_id, num_turns=int(turns))
        tokens = final.get("tokens") or {}
        if (isinstance(cost, (int, float)) and cost > 0) or any(tokens.values()):
            # spend.record also increments work_item.total_cost_usd - one write,
            # one truth; do not add it a second time here. Tokens ride along
            # because on a subscription they, not the dollars, are what runs
            # out - and a run under a plan reporting no price is still a run
            # worth having in the ledger.
            _spend.record(root, float(cost or 0), kind="agent",
                          work_item_id=item_id, model=str(final.get("model") or ""),
                          tokens=tokens,
                          detail=f"agent session for item {item_id}")
    except Exception:
        pass
    base = entry.get("base_commit") or ""
    if not base:
        return
    try:
        scope = _git.touched(entry.get("cwd") or root, base)
        if scope["available"]:
            record = {"base_commit": base, "branch": entry.get("branch", ""),
                      "worktree": entry.get("worktree", ""),
                      "paths": _git.fingerprint(entry.get("cwd") or root,
                                                scope["paths"])}
            run_record_path(root, item_id).write_text(
                json.dumps(record), encoding="utf-8")
    except Exception:
        pass
    _auto_commit(root, item_id, entry)


def _auto_commit(root: str, item_id: int, entry: dict) -> None:
    """Commit this run's own files, so the NEXT dispatch is not refused.

    THE DEADLOCK. Nothing in this system committed anything, and dispatch
    refuses a dirty tree by default - so the first agent to write a file
    blocked every later dispatch, permanently, with a 20-second retry loop.
    `dirty_tree` is a FLOOR refusal, so it stopped the whole board rather than
    one item. Measured consequence: an overnight queue of thirty items woke up
    with three done, twenty-six never started, and the autopilot toggle still
    green.

    ONLY THIS RUN'S PATHS (gitwork.commit_paths, never `commit -a`), so a
    human's unrelated uncommitted work is not swept into an agent's commit - and if THAT is what makes the tree dirty, it stays dirty and the next
    dispatch is still refused, which is the rule working rather than being
    bypassed.

    Skipped for a worktree run: those already live on their own branch, which
    is the isolation this exists to substitute for. Best effort throughout - the work is on disk either way, and a failed commit must never turn a
    finished run into a failed one.
    """
    if entry.get("worktree"):
        return
    if not _flag(root, "dispatch.auto_commit", "BGATE_AUTO_COMMIT"):
        return
    base = entry.get("base_commit") or ""
    try:
        scope = _git.touched(root, base)
        if not scope.get("available") or not scope.get("paths"):
            return
        seat = str(entry.get("seat") or "")
        made = _git.commit_paths(
            root, scope["paths"],
            f"bgate: item #{item_id}" + (f" [{seat}]" if seat else "")
            + " - committed by the harness so the board keeps moving")
        if made.get("ok"):
            from bgate_core import activity as _act

            _act.log(root, "dispatch",
                     f"item {item_id}: committed {len(made['committed'])} "
                     f"file(s) as {made['commit'][:8]}", ref=str(item_id))
    except Exception:
        pass


def _final_event(root: str, item_id: int) -> dict:
    """The CLI's terminal ``result`` event for this run, or {}."""
    try:
        return read_activity(root, item_id, limit=0).get("final") or {}
    except Exception:
        return {}


def _exit_verdict(root: str, item_id: int, code, entry: dict) -> tuple[str, str]:
    """What a dead process means for its item.

    EXIT 0 IS NOT SUCCESS. A `claude` that dies on startup, gets killed by the
    OS, or does nothing at all can still exit 0 - and marking that 'done'
    silently books work nobody did, on an item nobody will ever look at again.
    The evidence that a run happened is the CLI's own terminal ``result`` event:
    exited 0 AND self-reported = done; exited 0 saying nothing = a session with
    nothing to account for, which is a failure and has to read like one.
    """
    stopped = entry.get("stop_reason")
    if stopped:
        return "failed", stopped
    final = _final_event(root, item_id)
    said = str(final.get("text") or "").strip()
    tail = f"; its last words: {said[:400]}" if said else ""
    if code != 0:
        return "failed", f"session exited {code} without self-reporting{tail}"
    if not final:
        return "failed", ("session exited 0 without ever reporting a result - "
                          "nothing was accounted for, so this is not done "
                          "(a crashed or no-op run looks exactly like this)")
    if final.get("subtype") != "success":
        return "failed", (f"session ended as {final.get('subtype')!r} without "
                          f"self-reporting{tail}")
    return "done", ("session exited cleanly and reported success without calling "
                    f"queue_complete{tail}")


def _reap(root: str, item_id: int, entry: dict, code) -> dict:
    """Bank a run whose process is gone - exactly once - and retain the result.

    The ONE place a finished run writes its item status. It used to live in
    status(), i.e. in the dashboard's GET: two concurrent polls raced each other
    through the same transition, and a headless run was never settled at all.
    """
    with _lock:
        if entry.get("reaped"):
            return _done.get((_pkey(root), item_id), {})
        entry["reaped"] = True
        if _live.get(item_id) is entry:
            del _live[item_id]
    for key in ("handle", "stdin"):
        try:
            entry[key].close()
        except Exception:
            pass
    outcome, result = _exit_verdict(root, item_id, code, entry)
    try:
        # Only if the agent never spoke for itself: queue_complete's own result
        # is the better answer and must never be overwritten. Through complete()
        # rather than set_status so a session that exits cleanly without
        # self-reporting still lands in the approval gate instead of skipping it.
        if _queue.get(root, item_id)["status"] == "dispatched":
            _queue.complete(root, item_id, result=result,
                            failed=(outcome != "done"))
    except LookupError:
        pass
    # CLAIMS DIE WITH THE RUN, BUT THE WORK DOES NOT. An item the agent claimed
    # (queue_claim_next) and never completed goes back on the board as 'queued'
    # rather than dying as a stranded 'dispatched' row - autodeploy re-dispatches
    # it fresh, and nothing about it was attempted enough to call it failed.
    try:
        for claimed in _open_claims(root, item_id):
            _queue.set_status(
                root, int(claimed["id"]), "queued",
                result=f"requeued: claimed by the agent on item {item_id}, "
                       "which exited before starting or finishing it")
    except Exception:
        pass
    _finalize(root, item_id, entry)
    try:
        _unrecord_pid(root, entry["proc"].pid)
    except Exception:
        pass
    # The machine-wide entry goes with it. An entry left behind is what
    # agentreg.reconcile() reads as "this run died without being banked", and
    # this run has just been banked - leaving it would have the next dashboard
    # start by failing an item that finished cleanly.
    try:
        _agentreg.forget(entry["proc"].pid)
    except Exception:
        pass
    row = {"item_id": item_id, "state": "exited", "code": code,
           "pid": getattr(entry.get("proc"), "pid", None),
           "log": entry.get("log", ""), "ended_at": time.time(),
           "outcome": outcome, "result": result,
           "stopped_by": entry.get("stopped_by", ""),
           "final": _final_event(root, item_id)}
    with _lock:
        _done[(_pkey(root), item_id)] = row
    _prune_retained()
    # After the status write, so a subscriber that goes and reads the item sees
    # the state this event is describing rather than 'dispatched'. Emitted for
    # every exit including a kill: "the agent is gone" is the fact, and the item's
    # own item.done/item.failed says whether the work landed.
    # Tell autodeploy how this went. A runner that fails instantly on every
    # item (an expired CLI login is the observed case) otherwise burns the whole
    # queue to 'failed' inside ten minutes, because a dispatch that SPAWNS
    # counts as a success and never triggers a cooldown.
    try:
        from bgate_ui import autodeploy as _auto

        _auto.note_run_ended(
            root, item_id, outcome,
            float(max(0.0, time.monotonic() - float(entry["started_at"])))
            if entry.get("started_at") else 0.0)
    except Exception:
        pass
    _emit(root, "agent.exited", ref=str(item_id),
          payload={"item": item_id, "outcome": outcome, "code": code,
                   "stopped_by": entry.get("stopped_by", ""),
                   "cost_usd": round(float(entry.get("cost_usd") or 0), 4),
                   # started_at is a monotonic stamp, so 0 means "not recorded"
                   # rather than "the epoch" - subtracting it would report the
                   # machine's uptime as the run's duration.
                   "seconds": (int(max(0.0, time.monotonic()
                                       - float(entry["started_at"])))
                               if entry.get("started_at") else 0),
                   "actor": entry.get("actor") or "",
                   "result": str(result or "")[:400]})
    return row


def _prune_retained() -> None:
    """Bounded retention: the newest RETAIN_RUNS per project, nothing older than
    RETAIN_S. Evicting a run drops its parsed feed too - the two are the same
    memory story."""
    now = time.time()
    with _lock:
        per_project: dict[str, list] = {}
        for key, row in list(_done.items()):
            if now - row["ended_at"] > RETAIN_S:
                _done.pop(key, None)
                _activity.pop(key, None)
                continue
            per_project.setdefault(key[0], []).append((row["ended_at"], key[1]))
        for project, ended in per_project.items():
            for _ts, item_id in sorted(ended, reverse=True)[RETAIN_RUNS:]:
                _done.pop((project, item_id), None)
                _activity.pop((project, item_id), None)


def sweep(root: str) -> dict:
    """Advance this project's bookkeeping - the WRITER half of status().

    status() is a pure read now, so something explicit has to bank runs whose
    process died, renew what live runs hold, and expire retained results. The
    per-run watchdog thread does it for anything this server spawned; this is
    the belt-and-braces pass a caller can run on a timer or at startup, and the
    only thing that un-strands items a previous server left behind.
    """
    root = str(root)
    project = _pkey(root)
    if project not in _reconciled:
        _reconciled.add(project)
        reconcile(root)
    with _lock:
        entries = [(i, e) for i, e in _live.items()
                   if _pkey(e.get("root") or root) == project]
    reaped = []
    for item_id, entry in entries:
        code = entry["proc"].poll()
        if code is None:
            try:
                _assets.heartbeat(root, f"item-{item_id}")
            except Exception:
                pass
            continue
        _reap(root, item_id, entry, code)
        reaped.append(item_id)
    _prune_retained()
    return {"reaped": reaped, "live": _live_count()}


def reconcile(root: str) -> dict:
    """Un-strand items left 'dispatched' by a dashboard that restarted.

    _live is the only record that a run exists and it dies with the server, so
    an item that was mid-flight when the dashboard went down stayed 'dispatched'
    FOREVER: not queued, not finished, absent from the agent table, and refused
    by dispatch() because it is 'not queued'. The run's own log outlives the
    process, so that is what the item is settled against - a log that ends in a
    success result is a run that finished and only lost its bookkeeping.
    """
    try:
        stranded = _queue.list_items(root, status="dispatched")
    except Exception:
        return {"settled": []}  # no project here (or no DB yet) - nothing to do
    settled = []
    pids = _read_pids(root)
    adopted_items = {int(m["item_id"]) for m in pids.values()
                     if isinstance(m, dict) and m.get("item_id")
                     and _recent_progress(root, m)}
    for item in stranded:
        item_id = int(item["id"])
        with _lock:
            if item_id in _live:
                continue  # this server run owns it
        # STILL WORKING IS NOT STRANDED. reap_orphans now ADOPTS an inherited
        # agent that is making progress instead of killing it - but this pass
        # still settled its item as "the process did not survive", which is a
        # lie about a process that is generating art right now. The item then
        # failed, the follow-up router reopened it, and the next attempt paid
        # again for everything the first had already made. Observed on #357:
        # marked failed at 02:54:07, generated another sheet at 02:54:22.
        if item_id in adopted_items:
            continue
        # A CLAIMED ITEM IS NOT A RUN OF ITS OWN. Its log lives under the
        # CLAIMING item's id, so settling it against item-<id>.log below would
        # always read "no result" and fail work that was merely queued behind a
        # restart. If the claiming run is still alive (this server or adopted),
        # leave the claim alone; otherwise requeue it untouched.
        actor = str(item.get("actor") or "")
        if actor.startswith("agent:item-"):
            try:
                origin = int(actor.split("agent:item-", 1)[1])
            except ValueError:
                origin = 0
            with _lock:
                origin_live = origin in _live
            if origin and (origin_live or origin in adopted_items):
                continue
            try:
                _queue.set_status(
                    root, item_id, "queued",
                    result="requeued: claimed by another run that did not "
                           "survive a dashboard restart")
                settled.append({"item_id": item_id, "status": "requeued"})
            except Exception:
                pass
            continue
        final = _final_event(root, item_id)
        if final.get("subtype") == "success":
            outcome = "done"
            said = str(final.get("text") or "").strip()[:400]
            result = ("the dashboard restarted before this was banked; the "
                      "agent's log ends in success" + (f": {said}" if said else ""))
        else:
            outcome = "failed"
            result = ("stranded by a dashboard restart - the process did not "
                      "survive it and never reported a result")
        try:
            _queue.complete(root, item_id, result=result,
                            failed=(outcome != "done"))
        except Exception:
            continue
        settled.append({"item_id": item_id, "status": outcome})
    return {"settled": settled}


def _pids_path(root: str) -> Path:
    return Path(root) / ".bgate" / "agents" / "pids.json"


def _read_pids(root: str) -> dict:
    try:
        return json.loads(_pids_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _record_pid(root: str, pid: int, item_id: int) -> None:
    """Persist spawned-agent pids so a server restart can sweep survivors.

    The identity fields matter as much as the pid: a pid is reused within
    minutes on Windows, and the sweep's job is to kill OUR agent, never the
    claude session the user started themselves. See :func:`_is_recorded_agent`.
    """
    try:
        path = _pids_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _read_pids(root)
        ident = _proc_identity(pid)
        data[str(pid)] = {"item_id": item_id, "spawned_at": time.time(),
                          "name": ident.get("name", ""),
                          "started": ident.get("started")}
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _unrecord_pid(root: str, pid: int) -> None:
    try:
        path = _pids_path(root)
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.pop(str(pid), None) is not None:
            path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _proc_identity(pid: int) -> dict:
    """``{"name", "started"}`` for a running pid, as much as this host can say.

    psutil answers both and is used when it is installed (it is NOT a declared
    dependency, so it can never be required); otherwise tasklist answers the
    name only. An empty dict means 'no such process, or unknowable' - and the
    sweep treats unknowable as 'do not touch'.
    """
    try:
        import psutil  # optional; see docstring
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            proc = psutil.Process(int(pid))
            return {"name": (proc.name() or "").lower(),
                    "started": round(float(proc.create_time()), 3)}
        except Exception:
            return {}
    if sys.platform != "win32":
        return {}
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, creationflags=_NO_WINDOW,
            timeout=15).stdout
    except Exception:
        return {}
    return {"name": out.split(",")[0].strip('" ').lower()} if "," in out else {}


def _is_recorded_agent(pid: int, meta: dict) -> bool:
    """Is this pid still the process we spawned, or a stranger wearing its number?

    Killing anything whose image name starts with 'claude' is how you kill the
    user's OWN claude session: pids are recycled, and this ledger is best-effort
    (a crash can leave entries for processes that died hours ago). The process
    START TIME is the part a recycled pid cannot fake, so when the ledger
    recorded one it is the check that decides. Entries without it - written by
    an older build, or on a host where the start time was unreadable - fall back
    to the name check and stay explicitly best-effort.
    """
    live = _proc_identity(int(pid))
    name = str(live.get("name") or "")
    if not name.startswith("claude"):
        return False
    recorded = meta.get("started")
    if recorded and live.get("started") is not None:
        return abs(float(live["started"]) - float(recorded)) < 1.0
    return True


def _kill_tree(pid: int) -> None:
    """Kill a process and its children (Windows: taskkill /T; else SIGKILL)."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, creationflags=_NO_WINDOW,
                           timeout=15)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def kill_all(root: str, *, reason: str = "", actor: str = "") -> dict:
    """THE KILL SWITCH. Stop every agent on this project, now, and keep it off.

    For the moment where something is wrong and you do not yet know what: a run
    editing files it should not, a delegation that multiplied, a bill climbing
    while you read this. It does four things in the order that matters, because
    doing them in any other order leaves a gap something can restart through:

      1. auto-deploy OFF, first - killing agents while the loop is still on is
         how you get a fresh one dispatched before the old one has finished
         dying;
      2. every live agent in THIS process killed by tree (stop(), so the item
         records that a human stopped it rather than 'exited without reporting');
      3. every recorded pid from ANY previous or parallel dashboard reaped - the ledger on disk outlives the process that wrote it, which is the
         whole reason orphans exist;
      4. anything still sitting in 'dispatched' settled, so the board does not
         claim work is running after this returns.

    Safe to call twice; safe to call with nothing running.
    """
    stopped, errors = [], []
    with _lock:
        live_ids = [k for k, e in _live.items() if e["proc"].poll() is None]

    auto = None
    try:
        from bgate_ui import autodeploy as _autodeploy
        if _autodeploy.enabled(root):
            _autodeploy.set_enabled(root, False)
            auto = "auto-deploy turned off"
    except Exception as exc:  # never let the switch fail on a side concern
        errors.append(f"auto-deploy: {type(exc).__name__}: {exc}")

    for item_id in live_ids:
        try:
            result = stop(item_id, actor=actor or "the kill switch")
            (stopped if result.get("ok") else errors).append(
                item_id if result.get("ok") else f"#{item_id}: {result.get('error')}")
        except Exception as exc:  # one wedged pipe must not save the others
            errors.append(f"#{item_id}: {type(exc).__name__}: {exc}")

    orphans = {}
    try:
        orphans = reap_orphans(root)
    except Exception as exc:
        errors.append(f"orphan sweep: {type(exc).__name__}: {exc}")

    # The brainstorm room's thinking partners. They hold no tools and can write
    # nothing, so they are not agents in the sense the rest of this function
    # means - but they ARE spawned CLI processes on this project, and a kill
    # switch that leaves processes running is a kill switch nobody trusts twice.
    # Lazily imported and fully guarded: the emergency stop must not fail on a
    # module it only touches as a courtesy.
    thinking = []
    try:
        from bgate_ui import brainsession as _brainsession

        thinking = _brainsession.stop_all(root).get("stopped") or []
    except Exception as exc:
        errors.append(f"brainstorm partners: {type(exc).__name__}: {exc}")

    settled = {}
    try:
        settled = reconcile(root)
    except Exception as exc:
        errors.append(f"reconcile: {type(exc).__name__}: {exc}")

    note = reason or "emergency stop"
    try:
        from bgate_core import activity as _activity
        _activity.log(root, "killswitch",
                      f"KILL SWITCH - {len(stopped)} agent(s) stopped, "
                      f"{len(orphans.get('killed') or [])} orphan(s) reaped: {note}",
                      actor=actor or None)
    except Exception:
        pass
    return {"ok": True, "stopped": stopped, "orphans": orphans.get("killed") or [],
            "settled": settled.get("cleared") or settled.get("settled") or [],
            "brainstorms": thinking,
            "autopilot": auto, "errors": errors, "reason": note}


"""How long an inherited agent may be silent before it is treated as stuck.

Generous on purpose. The cost of waiting is a process that idles a few more
minutes; the cost of being wrong the other way is killing live work, failing its
item, and paying a second time for everything it had already generated."""
AGENT_SILENT_AFTER_S = 600.0


def _recent_progress(root: str, meta: dict) -> bool:
    """Has this agent done anything lately? Any evidence counts.

    Three independent signals, because each can be absent for an innocent
    reason: an agent that has not yet written its manifest, an item whose row
    has not been touched since dispatch, a run that logs but does not write.
    Any ONE of them being fresh is enough to leave the process alone.
    """
    item_id = meta.get("item_id")
    if not item_id:
        return False

    # REUSE THE HEARTBEAT THAT ALREADY WORKS. The first version of this checked
    # the progress manifest, the ledger's start time and work_item.updated_at,
    # and got all three wrong: the manifest is optional, the ledger writes
    # `spawned_at` (not `started_at`), and a work_item row is NOT touched while
    # an agent runs - so a process generating a sheet every twenty seconds read
    # as silent. _last_output_age_s is purpose-built for exactly this question
    # and watches what actually moves: the agent's log plus file mtimes under
    # .bgate_out and the game assets.
    log_path = str(Path(root) / ".bgate" / "agents" / f"item-{int(item_id)}.log")
    age = _last_output_age_s(root, {"log": log_path})
    if age is not None:
        return age < AGENT_SILENT_AFTER_S

    # No observable output at all yet. A just-spawned agent has not written
    # anything, so fall back to how long ago it was spawned rather than calling
    # it stuck the moment it starts.
    try:
        spawned = float(meta.get("spawned_at") or meta.get("started") or 0.0)
    except (TypeError, ValueError):
        return False
    return bool(spawned) and (time.time() - spawned) < AGENT_SILENT_AFTER_S


def reap_orphans(root: str) -> dict:
    """Sweep agents orphaned by a previous server run.

    _live dies with the server process, but the spawned claude.exe trees do
    not - they sit waiting on a pipe nobody will ever close. The pids ledger
    survives restarts; anything in it that is not in the CURRENT _live and is
    still verifiably OUR agent process gets its tree killed. The items those
    orphans were working on are settled in the same pass (reconcile) - killing
    the process without settling the item just moves the strand somewhere
    else."""
    killed, cleared = [], []
    path = _pids_path(root)
    if not path.is_file():
        _reconcile_quietly(root)
        return {"killed": [], "cleared": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _reconcile_quietly(root)
        return {"killed": [], "cleared": ["unreadable ledger - reset"],
                "reset": bool(path.write_text("{}", encoding="utf-8"))}
    with _lock:
        live_pids = {e["proc"].pid for e in _live.values()}
    adopted = []
    for pid_s, meta in list(data.items()):
        pid = int(pid_s)
        if pid in live_pids:
            continue  # owned by this server run
        info = meta if isinstance(meta, dict) else {}
        if _is_recorded_agent(pid, info):
            # AN ORPHAN IS A STUCK PROCESS, NOT AN INHERITED ONE. This used to
            # kill every recorded agent that was not in _live - and _live is
            # EMPTY at startup, which is the only time this runs. So restarting
            # the dashboard executed every agent then working, the follow-up
            # router reopened their items, and the next attempt re-bought art
            # the dead attempt had already paid for. Observed: item #357
            # generated a sheet at 02:36:20 and was reaped at 02:36:21, twice,
            # each lap spending again.
            #
            # A dispatched agent does not need this server: it is not waiting on
            # our stdin, it is working. Only the STEER pump needs the pipe, and
            # losing steerability is a far smaller harm than killing live work.
            # So: if it has shown progress recently, leave it alone, leave it in
            # the ledger, and leave its item unsettled - it will finish and
            # settle itself, or fall quiet and be reaped by a later sweep.
            if _recent_progress(root, info):
                adopted.append({"pid": pid, "item_id": info.get("item_id")})
                continue
            _kill_tree(pid)
            killed.append({"pid": pid, "item_id": info.get("item_id")})
        data.pop(pid_s)
        cleared.append(pid)
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass
    _reconcile_quietly(root)
    return {"killed": killed, "cleared": cleared, "adopted": adopted}


def _reconcile_quietly(root: str) -> None:
    """Reconcile once per project, never raising - reap_orphans runs at server
    startup and its return shape is a contract, so this cannot add keys or
    blow up on a directory that has no project in it."""
    project = _pkey(root)
    if project in _reconciled:
        return
    _reconciled.add(project)
    try:
        reconcile(root)
    except Exception:
        pass


def steer(root: str, item_id: int, text: str) -> dict:
    """Inject a live user message into a running agent - course-correction
    without killing and re-dispatching. Lands as a new user turn mid-work."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "steer text is empty"}
    with _lock:
        entry = _live.get(item_id)
        if not entry or entry["proc"].poll() is not None:
            return {"ok": False, "error": "no live agent for this item"}
        # NOT STEERABLE is a different fact from CHANNEL CLOSING, and saying the
        # wrong one sends the operator to wait for a message that can never
        # arrive. `codex exec` consumes stdin once, at launch; there is no live
        # channel to have closed.
        if not entry.get("steerable", True):
            return {"ok": False, "item_id": item_id, "runner": entry.get("runner"),
                    "error": f"the {entry.get('runner') or 'this'} runner takes "
                             "its prompt once at launch and has no live steer "
                             "channel - stop the item and re-dispatch it with "
                             "the correction in the brief"}
        if entry.get("stdin_closed"):
            return {"ok": False, "error": "agent is finishing; steer channel closed"}
        try:
            entry["stdin"].write(_user_msg(f"STEER FROM THE DIRECTOR (act on this now): {text}").encode("utf-8"))
            entry["stdin"].flush()
        except OSError as exc:
            return {"ok": False, "error": f"agent not accepting input: {exc}"}
        import time as _time
        entry["steers"].append({"text": text, "sent_at": _time.time(),
                                "consumed_at": None})
    return {"ok": True, "item_id": item_id, "steers": len(entry["steers"])}


def steer_all(root: str, text: str) -> dict:
    """Say one thing to EVERY agent running right now.

    The case this exists for is the one where a correction is not about one
    item: the art direction changed, a file everybody is about to touch is
    moving, stop writing to the old path. Steering that agent by agent means
    opening four panels and retyping the same sentence four times, and the last
    one gets it a minute after the first.

    ONE REPORT PER AGENT, INCLUDING THE REFUSALS, because the failure mode of a
    broadcast is a partial one that reads as a whole one. A codex run takes its
    prompt at launch and has no steer channel; an agent finishing has already
    closed stdin. Both are reported by item, so "who did not get this" is
    answerable rather than assumed.

    Snapshot the ids under the lock and steer outside it: `steer` takes the same
    lock, and holding it across a fan-out would serialise every other caller
    behind the slowest pipe.
    """
    said = (text or "").strip()
    if not said:
        return {"ok": False, "error": "steer text is empty"}
    with _lock:
        ids = [int(i) for i, entry in _live.items()
               if entry.get("proc") and entry["proc"].poll() is None]
    sent, refused = [], []
    for item_id in sorted(ids):
        got = steer(root, item_id, said)
        (sent if got.get("ok") else refused).append(
            {"item_id": item_id, **({} if got.get("ok")
                                    else {"error": got.get("error") or "refused"})})
    return {"ok": True, "text": said, "live": len(ids),
            "sent": sent, "refused": refused,
            "count": len(sent), "refused_count": len(refused)}


def _scan_steer_echoes(entry: dict) -> None:
    """Mark steers consumed by finding their --replay-user-messages echoes.

    A steer lands in stdin instantly, but the model only READS it when the
    current turn ends - that gap is the 'late to inject' feeling. The echo of
    the injected user message in the output log is the moment of consumption,
    so sent_at -> echo time IS the injection latency. Incremental tail read
    (cursor per entry), so a 10MB log costs nothing per poll."""
    import time as _time
    pending = [s for s in entry.get("steers", ()) if isinstance(s, dict)
               and s.get("consumed_at") is None]
    if not pending:
        return
    try:
        with open(entry["log"], "rb") as fh:
            fh.seek(entry.get("log_scan_pos", 0))
            chunk = entry.get("log_scan_rem", b"") + fh.read()
            entry["log_scan_pos"] = fh.tell()
    except OSError:
        return
    lines = chunk.split(b"\n")
    entry["log_scan_rem"] = lines.pop()  # possibly-partial last line
    now = _time.time()
    for line in lines:
        if b"STEER FROM THE DIRECTOR" not in line or b'"user"' not in line:
            continue
        for s in entry["steers"]:
            if isinstance(s, dict) and s.get("consumed_at") is None:
                s["consumed_at"] = now
                break


def _last_output_age_s(root: str, entry: dict) -> Optional[int]:
    """Seconds since the agent last produced ANY observable output - log write
    or a file under .bgate_out / game assets. Long atomic MCP calls (a 30-min
    image_sprites batch) log nothing until they return, which made healthy
    agents look hung and got them manually killed; file mtimes are the real
    heartbeat. Shallow capped scan, cheap enough for the dashboard poll."""
    import time as _t
    newest = 0.0
    try:
        newest = os.path.getmtime(entry["log"])
    except OSError:
        pass
    budget = 400  # max entries visited - keep the poll snappy
    stack = [(Path(root) / ".bgate_out", 0), (Path(root) / "game" / "assets", 0)]
    while stack and budget > 0:
        d, depth = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    budget -= 1
                    if budget <= 0:
                        break
                    try:
                        m = e.stat().st_mtime
                        if m > newest:
                            newest = m
                        if e.is_dir() and depth < 2:
                            stack.append((Path(e.path), depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return int(_t.time() - newest) if newest else None


def status(root: str) -> list[dict]:
    """The agent table for the dashboard - a PURE READ.

    This used to reap: a GET that closed pipes, flipped item statuses and wrote
    the spend ledger. Two dashboards (or one dashboard and one long-poll) raced
    each other through the same transition, and the whole lifecycle depended on
    somebody keeping a browser tab open. The writing moved to _reap/sweep, which
    the per-run watchdog drives; the only state touched here is the in-memory
    steer-echo cursor, which is a log READ.

    Finished runs stay in the table for a bounded window (see RETAIN_RUNS) so
    the result the user was waiting for is still there to read.
    """
    out = []
    project = _pkey(root)
    with _lock:
        entries = [(i, e) for i, e in _live.items()
                   if _pkey(e.get("root") or root) == project]
    for item_id, entry in entries:
        if entry["proc"].poll() is not None:
            continue  # dead but not banked yet - the watchdog/sweep owns that
        _scan_steer_echoes(entry)
        steers = [s for s in entry.get("steers", ()) if isinstance(s, dict)]
        consumed = [s for s in steers if s.get("consumed_at")]
        latencies = [round(s["consumed_at"] - s["sent_at"], 1) for s in consumed]
        out.append({"item_id": item_id, "state": "running",
                    "pid": entry["proc"].pid, "log": entry["log"],
                    "steers": len(steers),
                    "steers_pending": len(steers) - len(consumed),
                    "steer_latency_s": latencies,
                    # WHAT THIS RUN'S GUARDS ACTUALLY ARE. A cost ceiling that
                    # cannot bite and a steer box that goes nowhere must be
                    # visible as facts about the run, not discovered by trying
                    # them - that is the whole price of allowing a second runner.
                    "runner": entry.get("runner", "claude"),
                    "cost_tracked": bool(entry.get("cost_tracked", True)),
                    "steerable": bool(entry.get("steerable", True)),
                    "native_images": bool(entry.get("native_images")),
                    # HOW LONG AND HOW MUCH, WHILE IT IS STILL RUNNING. Both were
                    # on the exit event only, so the console could say what a run
                    # had cost the moment it no longer mattered and nothing at
                    # all while you were deciding whether to steer or stop it.
                    # started_at is MONOTONIC: 0 means "not recorded" rather than
                    # the epoch, and subtracting the epoch would report the
                    # machine's uptime as the run's duration.
                    "seconds": (int(max(0.0, time.monotonic()
                                        - float(entry["started_at"])))
                                if entry.get("started_at") else 0),
                    "cost_usd": round(float(entry.get("cost_usd") or 0), 4),
                    "last_output_s": _last_output_age_s(root, entry)})
    # AGENTS THIS PROCESS DID NOT SPAWN ARE STILL RUNNING AGENTS. _live is an
    # in-memory table, so an agent inherited across a dashboard restart - or
    # dispatched from another process - was invisible here: /api/agents returned
    # an empty list, the deck showed "0 running", and the work item's panel said
    # "no live steps" while the agent generated a sheet every twenty seconds.
    # The reaper no longer kills those and reconcile no longer fails them, and
    # both fixes were still invisible to the person watching, which is the half
    # of the bug they actually experience.
    #
    # Reported with what is genuinely known and no more: the pid and the item
    # are on record, the steer pipe is NOT ours (steerable=False, and that is
    # true rather than pessimistic - only the spawning process holds the stdin).
    known = {item_id for item_id, _ in entries}
    # MOST RECENTLY SPAWNED FIRST. An item that was re-dispatched has several
    # entries in the ledger and only the last is the live agent; listing both
    # showed one item as two running agents. Ordered by spawn time, NOT by pid:
    # a pid is not a clock, and sorting by the number reported the dead attempt
    # as the running one whenever the OS handed the retry a lower pid.
    ledger = sorted(_read_pids(root).items(),
                    key=lambda kv: float((kv[1] or {}).get("spawned_at") or 0.0)
                    if isinstance(kv[1], dict) else 0.0, reverse=True)
    for pid_s, meta in ledger:
        if not isinstance(meta, dict):
            continue
        item_id = meta.get("item_id")
        if not item_id or int(item_id) in known:
            continue
        # Claimed only once it PASSES: a re-dispatched item has a dead pid in
        # the ledger beside its live one, and marking the item seen before the
        # liveness check let the dead entry shadow the working agent - which
        # reported nothing running while it generated a sheet every 20 seconds.
        if not (_is_recorded_agent(int(pid_s), meta) and
                _recent_progress(root, meta)):
            continue
        known.add(int(item_id))
        # The pids ledger records identity, not paths, so the log is derived
        # from the convention /api/agent-log already reads.
        log_path = str(Path(root) / ".bgate" / "agents" / f"item-{int(item_id)}.log")
        out.append({"item_id": int(item_id), "state": "running",
                    "pid": int(pid_s), "log": log_path,
                    "adopted": True,
                    "steers": 0, "steers_pending": 0, "steer_latency_s": [],
                    "runner": str(meta.get("runner") or "claude"),
                    "cost_tracked": False,
                    "steerable": False,
                    "native_images": False,
                    "seconds": 0,
                    "cost_usd": 0.0,
                    "last_output_s": _last_output_age_s(
                        root, {"log": log_path})})

    with _lock:
        finished = [dict(row) for key, row in _done.items() if key[0] == project]
    out.extend(sorted(finished, key=lambda r: r["ended_at"]))
    return out


STEER_MARKER = "STEER FROM THE DIRECTOR (act on this now): "


def _add_step(state: dict, step: dict) -> None:
    """Append one step to the ring. What falls off the front is counted, not
    forgotten - see the ``dropped``/``truncated`` fields read_activity returns.

    Every step is stamped with when it was PARSED. That is a few milliseconds
    after the CLI wrote it and is the only clock the log offers, but it is what
    lets a phase say which renders and which sound files came out of it: an
    artifact row carries created_at, a step did not carry anything to compare it
    to, so the work an agent produced could not be attributed to the part of the
    run that produced it. Rounded to the second, which is the resolution the
    artifact table stores anyway.
    """
    step.setdefault("ts", time.time())
    state["steps"].append(step)
    state["step_count"] += 1
    if len(state["steps"]) > MAX_STEPS:
        del state["steps"][:len(state["steps"]) - MAX_STEPS]


def _blocks(ev: dict) -> list:
    """Content blocks of a stream-json message. Some CLI builds send ``content``
    as a bare string; iterating that yields characters and explodes on .get."""
    content = (ev.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _absorb(state: dict, raw: bytes) -> None:
    """Fold one log line into the parsed feed."""
    line = raw.strip()
    if not line:
        return
    try:
        ev = json.loads(line)
    except (ValueError, TypeError):
        return
    if not isinstance(ev, dict):
        return
    # THE CLAUDE SESSION THIS RUN IS, which is what makes it resumable.
    #
    # Every line the CLI writes carries the same `session_id`, and Claude Code
    # keeps that session's transcript under ~/.claude/projects - so the id is
    # the handle for `claude --resume`, and somebody who would rather read a run
    # in a terminal than in this dashboard can pick it up exactly where the
    # agent left it, with its whole context.
    #
    # TAKEN FROM THE FIRST LINE THAT HAS ONE AND THEN LEFT ALONE. Measured: one
    # id per run across a whole log. A later line overwriting it would matter if
    # that ever stopped being true, and the first is the one that names the
    # session that was started.
    if not state.get("session_id"):
        sid = ev.get("session_id")
        if isinstance(sid, str) and sid:
            state["session_id"] = sid

    etype = ev.get("type")
    if etype == "bgate_run_start":
        # A RE-DISPATCH. The log appends across runs and showing run 1's result
        # as run 2's current state was a real observed bug - everything before
        # this marker belongs to a run that is over.
        state["steps"].clear()
        state["step_count"] = 0
        state["final"] = None
        # A re-dispatch is a DIFFERENT Claude session, so the old id must go
        # with the old steps. Resuming run 1 while looking at run 2 would open
        # a transcript that has nothing to do with what is on screen.
        state["session_id"] = ""
    elif etype == "assistant":
        for block in _blocks(ev):
            if block.get("type") == "text" and str(block.get("text", "")).strip():
                _add_step(state, {"kind": "say",
                                  "text": str(block["text"]).strip()[:1000]})
            elif block.get("type") == "tool_use":
                name = str(block.get("name", "?"))
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                hint = (inp.get("path") or inp.get("file_path") or inp.get("role")
                        or inp.get("title") or inp.get("query") or inp.get("prompt")
                        or inp.get("command") or "")
                _add_step(state, {"kind": "tool",
                                  "name": name.replace("mcp__builders-gate__", ""),
                                  "hint": str(hint)[:120]})
    elif etype == "user":
        for block in _blocks(ev):
            if block.get("type") == "tool_result":
                c = block.get("content")
                txt = c if isinstance(c, str) else (
                    c[0].get("text", "") if isinstance(c, list) and c
                    and isinstance(c[0], dict) else "")
                txt = str(txt).strip()
                if txt:
                    _add_step(state, {"kind": "result", "text": txt[:600],
                                      "truncated": len(txt) > 600})
            elif block.get("type") == "text":
                # Replayed user turns - and the FIRST of them is the dispatch
                # prompt, which carries the seat-identity preamble and the whole
                # house-rules block. That is internal plumbing, not something to
                # show a human reading their agent's activity: only turns
                # carrying the director marker (live steers) are surfaced.
                txt = str(block.get("text", ""))
                if STEER_MARKER in txt:
                    _add_step(state, {"kind": "steer",
                                      "text": txt.split(STEER_MARKER, 1)[1].strip()[:600]})
    elif etype == "result":
        # The agent's actual answer. NOT truncated to a preview length - this is
        # the deliverable sentence the user opened the panel to read; the cap is
        # only here so a runaway result cannot pin the process's memory.
        state["final"] = {"subtype": ev.get("subtype"),
                          "text": str(ev.get("result", ""))[:20000],
                          "cost": ev.get("total_cost_usd"),
                          "turns": ev.get("num_turns"),
                          # The tokens, and which model spent them. `cost` on a
                          # subscription is what this WOULD have cost on the
                          # API; these are what the rolling usage window
                          # actually meters, and cache_read dominates them by
                          # four orders of magnitude over output.
                          "model": _final_model(ev),
                          "tokens": _final_tokens(ev)}
    elif etype and etype.startswith(("thread.", "turn.", "item.")):
        _absorb_codex(state, etype, ev)


def _final_tokens(ev: dict) -> dict:
    """The run's token usage, in this module's four names.

    Read off `usage` rather than summed from the per-turn assistant events:
    the CLI already totals it there, and re-adding 8,000 assistant messages to
    reach a number the result event carries is the kind of work this feed's
    byte cursor exists to avoid.
    """
    usage = ev.get("usage")
    if not isinstance(usage, dict):
        return {}
    def n(key: str) -> int:
        try:
            return max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            return 0
    return {"input": n("input_tokens"), "output": n("output_tokens"),
            "cache_read": n("cache_read_input_tokens"),
            "cache_write": n("cache_creation_input_tokens")}


def _final_model(ev: dict) -> str:
    """Which model actually ran, as the CLI names it.

    Recorded because nothing else in this system knew. Every dispatch left
    --model unset, so the answer was whatever the CLI defaulted to that day - unlogged, unqueryable, and discovered only by reading a raw session log
    after the bill looked wrong. `modelUsage` is keyed by the resolved name
    including its context-window suffix, which is the distinction that matters:
    an opus-5[1m] run and an opus-5 run meter differently.
    """
    usage = ev.get("modelUsage")
    if isinstance(usage, dict) and usage:
        return max(usage, key=lambda k: (usage[k] or {}).get("cacheReadInputTokens", 0)
                   if isinstance(usage[k], dict) else 0)
    return str(ev.get("model") or "")


# ---------------------------------------------------------------------------
# The other vocabulary
# ---------------------------------------------------------------------------
# `codex exec --json` speaks a different, smaller event language than claude's
# stream-json. The two do not collide - dotted names against bare ones - so one
# reader handles a log of either kind without being told which runner wrote it,
# which matters because the log is per ITEM and a re-dispatch may switch runners
# under an existing file.
#
# The mapping is deliberately lossy in one direction: codex reports a shell
# command and its whole aggregated output, where claude reports a tool name and
# a structured result. Both land as the same {kind: tool} / {kind: result} steps
# the feed already renders, because the person reading the panel wants to know
# what the agent DID, not which vendor's noun it used.
_CODEX_QUIET_ITEMS = {"reasoning", "todo_list"}


def _absorb_codex(state: dict, etype: str, ev: dict) -> None:
    if etype == "thread.started":
        # Same job as bgate_run_start: a resumed or re-dispatched thread must
        # not show the previous run's steps as current.
        state["steps"].clear()
        state["step_count"] = 0
        state["final"] = None
        # A re-dispatch is a DIFFERENT Claude session, so the old id must go
        # with the old steps. Resuming run 1 while looking at run 2 would open
        # a transcript that has nothing to do with what is on screen.
        state["session_id"] = ""
        return
    if etype == "turn.completed":
        # NO PRICE HERE, ON PURPOSE - see runners.Runner.cost_tracked. Tokens
        # are recorded so the run is not a black box, but nothing downstream may
        # read them as dollars.
        usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
        state["usage"] = {
            "input_tokens": usage.get("input_tokens"),
            "cached_input_tokens": usage.get("cached_input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_output_tokens": usage.get("reasoning_output_tokens"),
        }
        return
    if etype != "item.completed":
        # item.started is the same item arriving twice; taking only the
        # completion keeps one step per action instead of a doubled feed.
        return

    item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
    kind = str(item.get("type") or "")
    if kind in _CODEX_QUIET_ITEMS:
        return
    if kind == "agent_message":
        text = str(item.get("text") or "").strip()
        if text:
            _add_step(state, {"kind": "say", "text": text[:1000]})
            # The LAST message of the run is the deliverable, and codex has no
            # separate result event to carry it. Overwritten each time, so
            # whatever it said last stands.
            state["final"] = {"subtype": "success", "text": text[:20000],
                              "cost": None, "turns": None}
        return
    if kind == "command_execution":
        command = str(item.get("command") or "")
        _add_step(state, {"kind": "tool", "name": "Bash",
                          "hint": command[:120]})
        output = str(item.get("aggregated_output") or "").strip()
        code = item.get("exit_code")
        if output or code:
            _add_step(state, {"kind": "result",
                              "text": (f"exit {code}\n" if code else "")
                                      + output[:600],
                              "truncated": len(output) > 600})
        return
    if kind in ("mcp_tool_call", "tool_call"):
        name = str(item.get("tool") or item.get("name") or "?")
        args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
        hint = (args.get("path") or args.get("prompt") or args.get("name")
                or args.get("seat") or args.get("title") or "")
        _add_step(state, {"kind": "tool",
                          "name": name.replace(f"mcp__{_runners.MCP_SERVER_NAME}__", ""),
                          "hint": str(hint)[:120]})
        return
    if kind in ("file_change", "patch_apply"):
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        paths = ", ".join(str((c or {}).get("path") or "") for c in changes[:4])
        _add_step(state, {"kind": "tool", "name": "Edit",
                          "hint": (paths or str(item.get("path") or ""))[:120]})
        return
    # An item type this version has never seen still belongs in the feed - # silence would make a new codex capability look like an agent doing nothing.
    label = str(item.get("text") or item.get("command") or kind)
    _add_step(state, {"kind": "tool", "name": kind or "item", "hint": label[:120]})


def _is_running(item_id: int) -> bool:
    entry = _live.get(item_id)
    return bool(entry) and entry["proc"].poll() is None


def read_activity(root: str, item_id: int, limit: int = 40,
                  offset: int = 0) -> dict:
    """An agent's stream-json log as a readable feed - parsed FORWARD FROM A
    BYTE CURSOR, once. Serialized: see _feed_lock.

    The log is documented as 10MB and the dashboard polls every ~3s per live
    agent; read_text() + json.loads on every line of it, every poll, re-parsed
    the entire session to display its last 40 steps. The steer-echo scanner a
    hundred lines up already solved that with a per-run byte cursor - this is
    the same trick, plus a bounded ring so a long session cannot grow without
    limit in memory.

    Truncation is now REPORTED rather than silent: ``step_count`` is everything
    the run has done, ``dropped`` is what aged out of the ring, ``truncated``
    says the window is not the whole story, and ``offset`` (steps back from the
    newest) pages through what is still held. ``limit=0`` returns the full ring.
    """
    key = (_pkey(root), int(item_id))
    log_path = Path(root) / ".bgate" / "agents" / f"item-{item_id}.log"
    # ONE READER AT A TIME per process. The cursor advance and the remainder
    # buffer are read-modify-write, and there are two callers now: the request
    # thread painting the console, and the per-run watchdog checking whether the
    # session errored out. Interleaved, they absorb the same byte range twice - # duplicate steps, doubled counts - or one clobbers the other's partial
    # line and a step is lost outright.
    with _feed_lock:
        try:
            size = log_path.stat().st_size
        except OSError:
            _activity.pop(key, None)
            return {"steps": [], "running": _is_running(item_id), "final": None,
                    "step_count": 0, "dropped": 0, "truncated": False,
                    "session_id": ""}

        state = _activity.get(key)
        if state is None or state["pos"] > size:
            # First look, or the log was replaced/truncated under us (a cursor past
            # EOF means the bytes we already parsed are gone) - start clean.
            state = {"pos": 0, "rem": b"", "steps": [], "final": None,
                     "step_count": 0, "bytes_read": 0, "session_id": ""}
            _activity[key] = state
        state["touched"] = time.time()
        if size > state["pos"]:
            try:
                with open(log_path, "rb") as fh:
                    fh.seek(state["pos"])
                    chunk = fh.read()
                    state["pos"] = fh.tell()
            except OSError:
                chunk = b""
            state["bytes_read"] += len(chunk)
            lines = (state["rem"] + chunk).split(b"\n")
            state["rem"] = lines.pop()  # possibly-partial last line
            for raw in lines:
                _absorb(state, raw)
        _prune_feeds()

        kept = state["steps"]
        end = max(0, len(kept) - max(0, int(offset)))
        start = max(0, end - int(limit)) if limit else 0
        window = kept[start:end]
        return {"steps": window, "running": _is_running(item_id),
                "final": state["final"], "step_count": state["step_count"],
                "dropped": state["step_count"] - len(kept),
                "truncated": len(window) < state["step_count"],
                "session_id": state.get("session_id") or "",
                "offset": max(0, int(offset)), "limit": int(limit)}


def _prune_feeds() -> None:
    """Cap the parsed-feed cache. Every item ever opened in the UI gets one, and
    a dashboard left running for a week must not accumulate them forever."""
    if len(_activity) <= MAX_FEEDS:
        return
    oldest = sorted(_activity.items(), key=lambda kv: kv[1].get("touched", 0))
    for key, _state in oldest[:len(_activity) - MAX_FEEDS]:
        _activity.pop(key, None)


def stop(item_id: int, actor: str = "", root: str = "") -> dict:
    """End a run deliberately: kill the TREE, and record it as a stop.

    Two bugs in one line. terminate() killed the `claude` parent only, leaving
    its MCP-server children alive holding the pipe - the orphan pile-up the rest
    of this file keeps fighting. And the run was then banked as 'session exited
    N without self-reporting', so the one place a user looks to find out what
    happened told them their agent mysteriously died, when in fact they stopped
    it. The stop lands on the item immediately, with a name on it.

    ``root`` NAMES THE PROJECT THE CALLER MEANT, and is checked rather than
    assumed. Item ids are per project - every game has its own #14 - and _live
    is keyed by item id alone, machine-wide. The fleet view lists agents across
    every registered project, so a stop aimed at another game's #14 would have
    killed whatever this process happened to be running under that number.
    Callers that genuinely mean "whatever holds this id here" (the per-project
    board buttons, kill_all) pass nothing and are unaffected.
    """
    with _lock:
        entry = _live.get(item_id)
        if not entry or entry["proc"].poll() is not None:
            return {"ok": False, "error": "no live agent for this item"}
        # An entry with no root recorded predates that field and cannot be
        # proved to be the wrong project, so it is left stoppable - refusing on
        # an unknown is how a stop button starts doing nothing.
        here = str(entry.get("root") or "")
        if root and here and _pkey(here) != _pkey(root):
            return {"ok": False,
                    "error": f"item {item_id} here belongs to {here}, not to "
                             f"{root} - nothing was stopped"}
        if not actor:
            try:
                from bgate_ui import api as _api
                actor = _api.current_actor()
            except Exception:
                actor = ""
        actor = actor or "the dashboard"
        reason = (f"stopped by {actor} - this run was ended by hand, "
                  "it did not die on its own")
        entry["stopped_by"] = actor
        entry["stop_reason"] = reason
        pid = entry["proc"].pid
        root = entry.get("root") or ""
    _kill_tree(pid)
    # Written here, not at reap time: the reap only writes while the item is
    # still 'dispatched', so recording the stop now is what keeps the crash
    # story from being told over it.
    #
    # queue.stop rather than set_status: the prose is for the human reading the
    # card, and it stamps `stopped_by` so anything that is not a human can tell
    # this apart from a crash without parsing the sentence. See migration 0019.
    if root:
        try:
            if _queue.get(root, item_id)["status"] == "dispatched":
                _queue.stop(root, item_id, by=actor, reason=reason)
        except LookupError:
            pass
    return {"ok": True, "item_id": item_id, "pid": pid, "actor": actor,
            "reason": reason}
