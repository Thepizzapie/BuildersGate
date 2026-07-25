"""Dispatch — the dashboard spawns real Claude seat sessions against work items.

Why this architecture wins: a session spawned with cwd = the game project gets
(1) the builders-gate MCP tools NATIVELY (the server resolves the project by
cwd — no runner scripts, no kwargs files), and (2) the PreToolUse lane/lock
hook with BGATE_SEAT set — actual enforcement, not honor-system. The dashboard
is user-run software, so a dispatch click is the USER launching the agent.

One live session per work item; state is in-memory plus a log file per item
(.bgate/agents/item-<id>.log) so a dashboard restart loses handles, not history.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from bgate_core import assets as _assets
from bgate_core import gitwork as _git
from bgate_core import queue as _queue
from bgate_core import scope as _scope
from bgate_core import spend as _spend

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_live: dict[int, dict] = {}
_lock = threading.Lock()


def _refuse(code: str, message: str, **detail) -> dict:
    """A refusal the UI can both show and switch on.

    dispatch() answers plain dicts (app.py returns them verbatim) and callers
    already read ``error`` as a sentence, so the machine-readable ``code`` rides
    alongside rather than replacing it — the string stays a string.
    """
    return {"ok": False, "error": message, "code": code, "detail": detail}


def _user_msg(text: str) -> str:
    """A stream-json user turn — the wire format the CLI reads from stdin."""
    return json.dumps({"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": text}]}}) + "\n"


def find_claude() -> Optional[str]:
    exe = shutil.which("claude")
    if exe:
        return exe
    fallback = Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    return str(fallback) if fallback.exists() else None


# Seat-specific house rules injected UNCONDITIONALLY into the dispatch prompt —
# the one channel every agent sees even if it skips seat_brief (item 56 did:
# it hand-rolled 8 loose image_edit frames for an animation task and never
# stitched a sheet). Keep these short and imperative.
SEAT_RULES = {
    "narrative": (
        "NARRATIVE HOUSE RULE — NO FIRST-THOUGHT JOKES:\n"
        "• Before landing ANY name/line/bark, generate 5 candidates and kill "
        "every one that is the FIRST joke anyone would make on the premise "
        "(the obvious pun, the meme format, the joke every parody of this "
        "subject already made). Ship the one that surprises.\n"
        "• Obey the project's OWN tone tests (read the tone guide / bible "
        "before writing; if you wrote one this session, your content must "
        "pass it — self-contradiction is an automatic fail). No winks, no "
        "lampshading, no decade-old meme formats.\n"
        "• Specificity beats snark: a line should only make sense in THIS "
        "world. If it could be pasted into any generic parody of the genre, "
        "cut it.\n"
        "• Read every deliverable back OUT LOUD (to yourself) against the "
        "tone tests before landing. Land fewer, better lines."
    ),
    "audio": (
        "AUDIO HOUSE RULE — EVERY SYNTHESIZED ASSET SHIPS ITS RECIPE:\n"
        "• Alongside each .wav/.ogg you synthesize, write a `<name>.synth.json` "
        "sidecar capturing the FULL parametric recipe: wave type(s), ADSR, "
        "pitch/glide, noise mix, filter, duration, sample rate — and for music "
        "beds the complete note/step pattern per channel + tempo/key. Another "
        "process must be able to re-render the identical asset from the recipe "
        "alone (the upcoming Audio Studio edits these knobs and re-renders — "
        "a .wav without its recipe is a dead end).\n"
        "• Keep the synthesis code you used in the project's .bgate/ scratch "
        "so the recipe->render path is reproducible."
    ),
    "art": (
        "ART HOUSE RULE — ANIMATIONS ARE SINGLE-GEN SHEETS (identity by "
        "construction), THEN SLICED:\n"
        "• For ANY animation, generate the WHOLE animation as ONE image: "
        "image_edit conditioned on the approved anchor ref, prompting 'an "
        "N-frame <anim> animation sprite sheet of THIS EXACT character, N "
        "poses side-by-side left-to-right in one row, evenly spaced, "
        "transparent background, same character same colors in every frame'. "
        "A model CANNOT drift identity inside one image — per-pose generation "
        "drifted skin/outfit colors across frames and is now the FALLBACK, "
        "not the default.\n"
        "• Slice + normalize + emit the engine sheet with the pipeline: "
        "python -c from bgate_adapters.sprites import from_painted_sheet — it "
        "cuts the row into equal cells, alpha-trims, bottom-centers, and "
        "writes <name>_sheet.png + <name>_frames.tres. Verify every cell "
        "sliced cleanly (no half-characters: regenerate the sheet with a "
        "stricter even-spacing instruction, don't hand-fix).\n"
        "• Cross-sheet identity: every animation sheet conditions on the SAME "
        "anchor ref, and you LOOK at all sheets side-by-side before landing — "
        "same character, same colors across the whole set.\n"
        "• image_edit single-frame fixes and per-pose image_sprites remain "
        "available ONLY to repair one bad cell of an otherwise-good sheet.\n"
        "• Before landing: run consistency_check on every frame AND clear its "
        "alpha flags (no white halo, no feathered fringe, no background bleed, no "
        "hollow interior, transparent = truly empty). Any alpha flag = do not land.\n"
        "\n"
        "HUD / UI CHROME SHIPS AS SEPARATE LAYERED PARTS, NEVER ONE BAKED "
        "COMPOSITE:\n"
        "• A UI element with dynamic or independently-driven sub-parts — a meter = "
        "frame + segmented FILL + icon + counter badge; a health bar = frame + "
        "FILL; a card = frame + portrait + label plate — MUST ship as SEPARATE "
        "transparent PNGs, one per layer, NOT fused into a single image. The "
        "scene/designer stacks and drives each independently: the fill depletes in "
        "code BEHIND a hollow frame, segments light one by one, the icon/badge are "
        "their own nodes. Gening the whole element in one go leaves nothing the "
        "designer can wire — it is not shippable.\n"
        "• Frames are HOLLOW: a fully transparent window where the code-driven fill "
        "shows through. NEVER bake a colored fill into a frame. For every frame, "
        "post the exact fill-window rect (x,y,w,h) in your seat note.\n"
        "• Keep the parts on a consistent pixel grid / shared registration so they "
        "stack cleanly at the target rect. A single composed PREVIEW mock is fine "
        "FOR REVIEW, but the SHIPPED assets are the separate layers.\n"
        "• Match the pinned concept: crop the target element out of the concept "
        "ref, condition generation on that crop, and build your own "
        "concept-vs-output comparison — iterate until it matches, don't ship "
        "isolated bare bars.\n"
        "\n"
        "WORLD / ENVIRONMENT ASSETS ARE INDIVIDUAL GENS, NEVER A SLICED SCENE:\n"
        "• Concept mocks are COMPOSITES — inspiration, not assets. A shippable "
        "world asset is generated ON ITS OWN: one prop (desk, plant, vending "
        "machine, printer shrine), one tile, one unit sprite per gen, "
        "transparent background, consistent scale against the project's grid "
        "(e.g. a 32px-tile world: props sized in tile multiples, characters to "
        "their tile footprint). NEVER generate a full scene and cut pieces out "
        "of it — sliced fragments have baked lighting/overlap and never "
        "composite cleanly.\n"
        "• Tilesets: gen each tile type separately (or a strict uniform grid "
        "sheet where every cell is one clean tile), then assemble the atlas "
        "with code — cells must be seamlessly tileable with their neighbors.\n"
        "• Scale/registration discipline: every asset in a batch states its "
        "intended pixel size; verify against the grid before landing so the "
        "engine drops it in without per-asset fudging.\n"
        "• ISO UNITS SHIP THE FULL FACING MATRIX: 2 generated base facings "
        "(SE front-right + NE back-right) x every animation, named "
        "<anim>_<facing> (idle_se, walk_ne, ...) through the standard sheet "
        "pipeline; SW/NW are mirrored in-engine (flip_h), never generated. A "
        "partial facing x anim matrix is an automatic fail — check the "
        "project bible's unit-sprite contract before landing any unit.\n"
        "• ISO PROPS DECLARE A ROTATION CLASS (see the project bible's "
        "prop-rotation contract): SYMMETRIC = 1 gen reused; MIRRORABLE = 2 "
        "gens + flip_h (NO text/logos/handedness); FULL = 4 gens (anything "
        "with readable text/signage — mirrored text is an automatic fail). "
        "All views of one prop conditioned on the SAME prop ref so it reads "
        "as one object rotated; state the tile footprint per prop.\n"
        "\n"
        "DELIVERY FIDELITY — WHAT WAS APPROVED IS WHAT SHIPS:\n"
        "• The engine-ready file you deliver must be a MECHANICAL derivation "
        "of the approved artifact revision: trim, downscale, alpha-clean — "
        "NOTHING ELSE. Never redraw, re-generate, or 'improve' an asset at "
        "the delivery step; a delivered file whose content differs from its "
        "approved source is an automatic reject (observed failure: floors "
        "shipped with an invented X-bevel that existed in no approved rev).\n"
        "• Name the source in your seat note per delivered file "
        "(delivered X <- approved revision N) so the trail is auditable."
    ),
}


def _prompt_for(item: dict) -> str:
    from bgate_core.seats import SEAT_IDENTITY

    seat_rule = SEAT_RULES.get(item["seat"], "")
    return (
        SEAT_IDENTITY + "\n\n"
        f"You are the {item['seat'].upper()} seat of the Builders Gate game project "
        "in the current directory. The builders-gate MCP tools are available to you "
        "NATIVELY — no runner scripts.\n\n"
        f"WORK ITEM #{item['id']} ({item['source']}): {item['title']}\n"
        f"{item['brief']}\n\n"
        + (seat_rule + "\n\n" if seat_rule else "")
        + "Protocol, in order:\n"
        "1. seat_brief for your role — mission, lanes, bible, pinned refs, notes.\n"
        f"2. Read .bgate/progress/item-{item['id']}.jsonl if it exists (a "
        "predecessor's trail); append one JSON line "
        '{"step":...,"artifacts":[...],"next":...} after EVERY unit of work.\n'
        "3. Do the work inside your lanes (the PreToolUse hook enforces them; "
        "seat_can_write is the oracle). Lock binaries before editing.\n"
        "4. Verify per house norms: godot_check_project after structural changes; "
        "run game/tests/fight_test.gd via godot_run when combat code moved "
        "(fail=0 or report exactly why); godot_screenshot when the change is "
        "visible; LOOK at what you produce.\n"
        "5. seat_post_note with what changed.\n"
        f"6. Mark the item: call queue_complete with item_id={item['id']} and a "
        "one-paragraph result (status 'done', or 'failed' with the honest reason).\n"
    )


def _live_count() -> int:
    return sum(1 for e in _live.values() if e["proc"].poll() is None)


def dispatch(root: str, item_id: int, *, permission_mode: str = "acceptEdits",
             model: Optional[str] = None, max_runtime_s: Optional[int] = None,
             max_cost_usd: Optional[float] = None,
             allow_dirty: Optional[bool] = None,
             actor: str = "") -> dict:
    """Spawn a Claude session against a queued item. One per item.

    Four things must be true before a process exists: the CLI is there, the item
    is dispatchable, the fleet is under its concurrency cap, and the projected
    cost fits the budget. Then the git boundary is captured — without a
    base_commit nothing downstream can show or undo what the agent did.
    """
    claude = find_claude()
    if not claude:
        return {"ok": False, "error": "claude CLI not found on PATH"}
    item = _queue.get(root, item_id)
    if item["status"] != "queued":
        return {"ok": False, "error": f"item {item_id} is {item['status']}, not queued"}

    # The cut line, enforced at the last possible moment. queue.add refuses to
    # FILE work below the line, but the line moves — an item queued legitimately
    # can be retroactively out of scope by the time anyone dispatches it, and
    # spending an agent on it is exactly the gold-plating the tier system exists
    # to stop.
    verdict = _scope.check(root, item["scope_tier_id"])
    if not verdict["allowed"]:
        return _refuse("out_of_scope", verdict["reason"], **{
            k: v for k, v in verdict.items()
            if k in ("tier", "cut_line", "cut_line_rank")},
            scope_code=verdict["code"])
    with _lock:
        if item_id in _live and _live[item_id]["proc"].poll() is None:
            return {"ok": False, "error": f"item {item_id} already has a live agent"}
        # Server-side, because the dashboard's "dispatch all" loops every queued
        # item with no cap of its own — 20 agents is 20 claude trees, each with
        # its own MCP children, on one laptop.
        cap = int(_spend.budget(root).get("max_concurrent") or 0)
        running = _live_count()
        if cap and running >= cap:
            return _refuse("concurrency_limit",
                           f"{running} agents already running — the cap is {cap}",
                           running=running, max_concurrent=cap)

    # Ceilings: the item's own overrides win, then this call's, then the budget.
    ceiling_usd = float(max_cost_usd or _spend.item_ceiling(root, item) or 0)
    ceiling_s = int(max_runtime_s or _spend.runtime_ceiling(root, item) or 0)
    verdict = _spend.check(root, projected_usd=ceiling_usd)
    if not verdict["allowed"]:
        return _refuse("budget_exceeded", verdict["reason"],
                       projected_usd=ceiling_usd, **{
                           k: v for k, v in verdict.items()
                           if k in ("scope", "spent", "ceiling")})

    # The git boundary. A run dispatched on top of uncommitted work produces a
    # diff that cannot tell the agent's edits from the human's, so mixing them
    # has to be asked for.
    if allow_dirty is None:
        allow_dirty = os.environ.get("BGATE_ALLOW_DIRTY", "").strip().lower() in {
            "1", "true", "yes", "on"}
    state = _git.dirty(root)
    if state["available"] and state["dirty"] and not allow_dirty:
        return _refuse("dirty_tree",
                       f"{len(state['paths'])} uncommitted change(s) in the tree — "
                       "commit or stash first, or dispatch with allow_dirty",
                       paths=state["paths"][:50])
    base_commit = _git.head(root) if state["available"] else ""
    branch, worktree = "", ""
    cwd = str(root)
    if base_commit and _git.isolation_enabled():
        made = _git.make_worktree(root, item_id, base=base_commit)
        if not made["available"]:
            return _refuse("worktree_failed", made["reason"])
        branch, worktree = made["branch"], made["worktree"]
        cwd = worktree

    log_dir = Path(root) / ".bgate" / "agents"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"item-{item_id}.log"

    env = {
        **os.environ,
        "BGATE_SEAT": item["seat"],
        "BGATE_ROOT": str(root),
        "BGATE_WORK_ITEM": str(item_id),
        "BGATE_LOCK_OWNER": f"item-{item_id}",
        # Director directive: gpt-image-2 is banned — force 1 for every gen.
        "BGATE_IMAGE_MODEL": os.environ.get("BGATE_IMAGE_MODEL", "gpt-image-1"),
    }
    # stream-json OUTPUT makes claude emit one NDJSON event per step AS IT WORKS
    # (tool calls, messages) instead of buffering to the end -- that feeds the
    # live activity view. stream-json INPUT keeps stdin open as a channel: the
    # initial prompt is the first user message, and steer() can inject more
    # user turns WHILE the agent runs. --replay-user-messages echoes injected
    # steers back into the output log so they show in the activity feed. The
    # process waits on stdin, so it only exits when we close the pipe (done in
    # status() once the agent self-reports via queue_complete).
    args = [claude, "-p", "--permission-mode", permission_mode,
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--replay-user-messages",
            "--allowedTools", "mcp__builders-gate", "Read", "Edit", "Write",
            "Glob", "Grep", "Bash"]
    if model:
        args += ["--model", model]

    log_handle = open(log_path, "ab")
    # RUN BOUNDARY: the log appends across re-dispatches, and both the activity
    # view and the steer-echo scanner must only look at THIS run — the stale
    # first-run result being shown as current, and old echoes falsely marking
    # fresh steers consumed, were real observed bugs. Marker + byte offset.
    import time as _time
    log_handle.write((json.dumps({"type": "bgate_run_start",
                                  "item_id": item_id,
                                  "base_commit": base_commit,
                                  "ts": _time.time()}) + "\n").encode("utf-8"))
    log_handle.flush()
    run_start_pos = log_handle.tell()
    proc = subprocess.Popen(args, cwd=cwd, env=env,
                            stdin=subprocess.PIPE, stdout=log_handle,
                            stderr=log_handle, creationflags=_NO_WINDOW)
    # Deliver the task as the first streamed user message, then leave stdin open.
    try:
        proc.stdin.write(_user_msg(_prompt_for(item)).encode("utf-8"))
        proc.stdin.flush()
    except OSError as exc:
        proc.kill()
        return {"ok": False, "error": f"could not send prompt to agent: {exc}"}
    with _lock:
        _live[item_id] = {"proc": proc, "log": str(log_path), "handle": log_handle,
                          "stdin": proc.stdin, "steers": [], "stdin_closed": False,
                          "log_scan_pos": run_start_pos,
                          "run_start_pos": run_start_pos,
                          "cost_scan_pos": run_start_pos,
                          "started_at": _time.monotonic(),
                          "max_runtime_s": ceiling_s, "max_cost_usd": ceiling_usd,
                          "base_commit": base_commit, "cwd": cwd,
                          "branch": branch, "worktree": worktree}
    _queue.set_status(root, item_id, "dispatched")
    _queue.set_run_fields(root, item_id, base_commit=base_commit, branch=branch,
                          worktree=worktree, actor=actor or None,
                          max_cost_usd=ceiling_usd or None,
                          max_runtime_s=ceiling_s or None)
    _record_pid(root, proc.pid, item_id)
    # The streamed session waits on stdin forever; close it once the agent
    # self-reports so it exits even when no dashboard is polling /api/agents.
    threading.Thread(target=_watch_completion, args=(root, item_id),
                     daemon=True).start()
    return {"ok": True, "item_id": item_id, "pid": proc.pid, "log": str(log_path),
            "base_commit": base_commit, "branch": branch, "worktree": worktree,
            "max_runtime_s": ceiling_s, "max_cost_usd": ceiling_usd}


def _observed_cost(entry: dict) -> float:
    """The highest ``total_cost_usd`` the CLI has reported THIS run.

    The session only prints cost at result boundaries, so this is a step
    function, not a live meter — it trips at the first boundary past the
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


def _trip(root: str, item_id: int, entry: dict, reason: str) -> None:
    """A budget the agent blew through: kill the tree and say why on the item."""
    _kill_tree(entry["proc"].pid)
    _unrecord_pid(root, entry["proc"].pid)
    try:
        _queue.set_status(root, item_id, "failed", result=reason)
    except LookupError:
        pass
    _finalize(root, item_id, entry)


def _watch_completion(root: str, item_id: int, poll_s: float = 4.0,
                      exit_grace_s: float = 90.0) -> None:
    """Close the agent's stdin once it has queue_complete'd, so the waiting
    process reaches EOF and exits — then make SURE it exits. EOF alone proved
    unreliable (agents wedged on child MCP servers piled up 14 orphaned
    claude.exe at peak), so after a grace period the process tree is killed;
    the item is already done, nothing of value is lost.

    This is also the wall clock. The kill grace above only starts once the item
    ALREADY reached done/failed, which is no help against the failure that
    actually costs money: an agent that never self-reports and runs all night.
    Runtime and cost are checked every poll from the moment it spawned."""
    import time
    while True:
        time.sleep(poll_s)
        with _lock:
            entry = _live.get(item_id)
            if not entry:
                return
            if entry["proc"].poll() is not None:
                _unrecord_pid(root, entry["proc"].pid)
                _finalize(root, item_id, entry)
                return  # already gone; status() will reap the table entry

            # The ceilings, enforced from spawn — not from completion.
            limit_s = int(entry.get("max_runtime_s") or 0)
            if limit_s and time.monotonic() - entry["started_at"] >= limit_s:
                _trip(root, item_id, entry,
                      f"killed: exceeded the {limit_s // 60}-minute runtime budget")
                return
            limit_usd = float(entry.get("max_cost_usd") or 0)
            if limit_usd:
                spent = _observed_cost(entry)
                if spent > limit_usd:
                    _trip(root, item_id, entry,
                          f"killed: spent ${spent:.2f} against a "
                          f"${limit_usd:.2f} ceiling")
                    return
            if not entry.get("stdin_closed"):
                try:
                    if _queue.get(root, item_id)["status"] in ("done", "failed"):
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
            # setdefault matters: status() (the dashboard poll) often closes
            # stdin FIRST and doesn't stamp eof_at — without this, the default
            # re-evaluated to now() every pass and the kill NEVER fired (the
            # observed doom-loop zombie).
            entry.setdefault("eof_at", time.monotonic())
            if time.monotonic() - entry["eof_at"] >= exit_grace_s:
                _kill_tree(entry["proc"].pid)
                _unrecord_pid(root, entry["proc"].pid)
                return


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
    JSON response — nothing summed it, so no one could answer what a night of
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
        if isinstance(cost, (int, float)) and cost > 0:
            # spend.record also increments work_item.total_cost_usd — one write,
            # one truth; do not add it a second time here.
            _spend.record(root, float(cost), kind="agent", work_item_id=item_id,
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


def _pids_path(root: str) -> Path:
    return Path(root) / ".bgate" / "agents" / "pids.json"


def _record_pid(root: str, pid: int, item_id: int) -> None:
    """Persist spawned-agent pids so a server restart can sweep survivors."""
    import json, time
    try:
        path = _pids_path(root)
        data = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
        data[str(pid)] = {"item_id": item_id, "spawned_at": time.time()}
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _unrecord_pid(root: str, pid: int) -> None:
    import json
    try:
        path = _pids_path(root)
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.pop(str(pid), None) is not None:
            path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


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


def reap_orphans(root: str) -> dict:
    """Sweep agents orphaned by a previous server run.

    _live dies with the server process, but the spawned claude.exe trees do
    not — they sit waiting on a pipe nobody will ever close. The pids ledger
    survives restarts; anything in it that is not in the CURRENT _live and is
    still a running claude process gets its tree killed."""
    import json
    killed, cleared = [], []
    path = _pids_path(root)
    if not path.is_file():
        return {"killed": [], "cleared": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"killed": [], "cleared": ["unreadable ledger — reset"],
                "reset": bool(path.write_text("{}", encoding="utf-8"))}
    with _lock:
        live_pids = {e["proc"].pid for e in _live.values()}
    for pid_s, meta in list(data.items()):
        pid = int(pid_s)
        if pid in live_pids:
            continue  # owned by this server run
        # Verify it's still OUR kind of process before killing (pid reuse).
        name = ""
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, creationflags=_NO_WINDOW,
                timeout=15).stdout
            name = out.split(",")[0].strip('" ').lower() if "," in out else ""
        except Exception:
            pass
        if name.startswith("claude"):
            _kill_tree(pid)
            killed.append({"pid": pid, "item_id": meta.get("item_id")})
        data.pop(pid_s)
        cleared.append(pid)
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass
    return {"killed": killed, "cleared": cleared}


def steer(root: str, item_id: int, text: str) -> dict:
    """Inject a live user message into a running agent — course-correction
    without killing and re-dispatching. Lands as a new user turn mid-work."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "steer text is empty"}
    with _lock:
        entry = _live.get(item_id)
        if not entry or entry["proc"].poll() is not None:
            return {"ok": False, "error": "no live agent for this item"}
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


def _scan_steer_echoes(entry: dict) -> None:
    """Mark steers consumed by finding their --replay-user-messages echoes.

    A steer lands in stdin instantly, but the model only READS it when the
    current turn ends — that gap is the 'late to inject' feeling. The echo of
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
    """Seconds since the agent last produced ANY observable output — log write
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
    budget = 400  # max entries visited — keep the poll snappy
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
    """Live agent table for the dashboard; reaps finished processes."""
    out = []
    with _lock:
        for item_id, entry in list(_live.items()):
            code = entry["proc"].poll()
            if code is not None:
                entry["handle"].close()
                # The agent should have queue_complete'd itself; a nonzero exit
                # with the item still 'dispatched' means it died — mark failed.
                try:
                    item = _queue.get(root, item_id)
                    if item["status"] == "dispatched":
                        _queue.set_status(
                            root, item_id,
                            "done" if code == 0 else "failed",
                            result=f"session exited {code} without self-reporting")
                except LookupError:
                    pass
                _finalize(root, item_id, entry)
                _unrecord_pid(root, entry["proc"].pid)
                del _live[item_id]
                out.append({"item_id": item_id, "state": "exited", "code": code})
            else:
                # The streamed session waits on stdin forever. Once the agent
                # has self-reported (queue_complete -> status no longer
                # 'dispatched'), close stdin so it hits EOF and exits.
                if not entry.get("stdin_closed"):
                    try:
                        item = _queue.get(root, item_id)
                        if item["status"] in ("done", "failed"):
                            entry["stdin"].close()
                            entry["stdin_closed"] = True
                            import time as _t
                            entry["eof_at"] = _t.monotonic()  # start the kill clock
                    except LookupError:
                        pass
                _assets.heartbeat(root, f"item-{item_id}")
                _scan_steer_echoes(entry)
                steers = [s for s in entry.get("steers", ())
                          if isinstance(s, dict)]
                consumed = [s for s in steers if s.get("consumed_at")]
                latencies = [round(s["consumed_at"] - s["sent_at"], 1)
                             for s in consumed]
                out.append({"item_id": item_id, "state": "running",
                            "pid": entry["proc"].pid, "log": entry["log"],
                            "steers": len(steers),
                            "steers_pending": len(steers) - len(consumed),
                            "steer_latency_s": latencies,
                            "last_output_s": _last_output_age_s(root, entry)})
    return out


def read_activity(root: str, item_id: int, limit: int = 40) -> dict:
    """Parse an agent's stream-json log into a readable live activity feed:
    what tools it's calling, what it's saying, and its final result."""
    import json

    log_path = Path(root) / ".bgate" / "agents" / f"item-{item_id}.log"
    if not log_path.is_file():
        return {"steps": [], "running": item_id in _live, "final": None}

    steps: list[dict] = []
    final = None
    text = log_path.read_text(encoding="utf-8", errors="replace")
    # Only THIS run: the log appends across re-dispatches, and showing a prior
    # run's final result as current was a real observed bug. Runs are separated
    # by bgate_run_start markers written at dispatch time.
    marker = text.rfind('"bgate_run_start"')
    if marker != -1:
        nl = text.find("\n", marker)
        if nl != -1:
            text = text[nl + 1:]
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    steps.append({"kind": "say", "text": block["text"].strip()[:280]})
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {})
                    hint = (inp.get("path") or inp.get("file_path") or inp.get("role")
                            or inp.get("title") or inp.get("query") or inp.get("prompt")
                            or inp.get("command") or "")
                    steps.append({"kind": "tool", "name": name.replace("mcp__builders-gate__", ""),
                                  "hint": str(hint)[:80]})
        elif etype == "user":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    c = block.get("content")
                    txt = c if isinstance(c, str) else (
                        c[0].get("text", "") if isinstance(c, list) and c else "")
                    if txt.strip():
                        steps.append({"kind": "result", "text": txt.strip()[:160]})
                elif block.get("type") == "text":
                    # Replayed user turns. The initial prompt is one too; only
                    # surface live steers (they carry the director marker).
                    txt = block.get("text", "")
                    marker = "STEER FROM THE DIRECTOR (act on this now): "
                    if marker in txt:
                        steps.append({"kind": "steer",
                                      "text": txt.split(marker, 1)[1].strip()[:200]})
        elif etype == "result":
            final = {"subtype": ev.get("subtype"),
                     "text": str(ev.get("result", ""))[:400],
                     "cost": ev.get("total_cost_usd"),
                     "turns": ev.get("num_turns")}
    live = item_id in _live and _live[item_id]["proc"].poll() is None
    return {"steps": steps[-limit:], "running": live, "final": final,
            "step_count": len(steps)}


def stop(item_id: int) -> dict:
    with _lock:
        entry = _live.get(item_id)
        if not entry or entry["proc"].poll() is not None:
            return {"ok": False, "error": "no live agent for this item"}
        entry["proc"].terminate()
    return {"ok": True, "item_id": item_id}
