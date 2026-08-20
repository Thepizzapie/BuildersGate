"""THE stream-json parser - one vocabulary for an agent's log, owned here.

An agent run writes `.bgate/agents/item-<id>.log` in the CLI's stream-json
(claude's bare event names, codex's dotted ones - one file may carry both,
because a re-dispatch can switch runners under an existing log). Three
separate readers had each grown their own copy of the folding rules: the
dashboard's live feed (bgate_ui.dispatch), the history browser
(bgate_ui.routes.history), and the MCP server's agent_activity. The forks
were not theoretical - the first cut of this module misquoted STEER_MARKER,
so the from-anywhere reader silently dropped every steer, which is exactly
the class of bug a third copy of a constant produces.

So the folding rules live HERE, in core, where every process can import
them: what counts as a step, which payload key is a tool call's SUBJECT,
how a re-dispatch resets the feed, what the final result event carries.
The dashboard keeps its byte-cursor cache and Popen liveness on top;
history keeps its byte-offset index and path mining; both fold events
through this vocabulary or share its constants.

Also here: :func:`tail`, the from-anywhere reader (disk + the agentreg
registry, no server required) that backs the MCP `agent_activity` tool.

Step shapes: {"kind": "say"|"tool"|"result"|"steer", ...} plus a "ts"
stamp; the final result event lands in state["final"], not in the steps.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Union

# The dashboard rolls the log at 10MB; half of one is far more history than a
# "what is it doing" question needs, and keeps the worst-case read bounded.
MAX_TAIL_BYTES = 512 * 1024

# The marker dispatch prepends to a mid-run steer turn. The PARSERS key on it,
# the WRITER (dispatch's steer delivery) embeds it - one constant, or a reader
# silently stops seeing steers (measured: see module docstring).
STEER_MARKER = "STEER FROM THE DIRECTOR (act on this now): "

# Matches runners.MCP_SERVER_NAME ("builders-gate"), spelled out because this
# module is core and runners is UI; test_agentlog pins the agreement.
MCP_TOOL_PREFIX = "mcp__builders-gate__"

# codex items that are narration about thinking, not actions taken.
CODEX_QUIET_ITEMS = {"reasoning", "todo_list"}


def log_path(root, item_id: int) -> Path:
    return Path(root) / ".bgate" / "agents" / f"item-{int(item_id)}.log"


def blocks(ev: dict) -> list:
    """Content blocks of a stream-json message. Some CLI builds send
    ``content`` as a bare string; iterating that yields characters and
    explodes on .get."""
    content = (ev.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] \
        if isinstance(content, list) else []


def tool_subject(name: str, inp: dict) -> str:
    """WHAT a call was about, not merely which tool it was.

    The inspector drew a run as a row of verbs — Read, Grep, Grep, Read,
    Bash — so a forty-step run was thirty-five interchangeable words. The
    subject was always in the payload; this is the one place that decides
    which key carries it, because the answer is per-tool: Grep's subject is
    its PATTERN and the path is context, while Read's subject is the path
    itself. The path still rides along on the end for the searches, because
    phases.look() mines this string for the files a step touched.
    """
    def val(key: str) -> str:
        got = inp.get(key)
        return " ".join(str(got).split()) if isinstance(got, (str, int)) else ""

    if name in ("Grep", "Glob"):
        return " · ".join(x for x in (val("pattern"), val("path")) if x)
    if name == "Bash":
        return val("command")
    if name in ("Read", "Write", "Edit", "MultiEdit"):
        return val("file_path") or val("path")
    if name == "NotebookEdit":
        return val("notebook_path")
    if name in ("WebFetch", "WebSearch"):
        return val("url") or val("query")
    if name in ("Task", "Agent"):
        return val("description") or val("subagent_type")
    if name == "TodoWrite":
        # Its input is the whole list; any one item of it is a misleading label.
        return ""
    return (val("path") or val("file_path") or val("name") or val("title")
            or val("role") or val("query") or val("pattern")
            or val("description") or val("command") or val("prompt"))


def add_step(state: dict, step: dict, max_steps: int = 0) -> None:
    """Append one step; a positive ``max_steps`` bounds the ring.

    What falls off the front is counted (``step_count``), not forgotten -
    the dashboard's ``dropped``/``truncated`` fields are derived from the
    difference. Every step is stamped with when it was PARSED - a few
    milliseconds after the CLI wrote it and the only clock the log offers -
    which is what lets a phase attribute artifacts to the part of the run
    that produced them. Rounded to the second by the consumer that stores it.
    """
    step.setdefault("ts", time.time())
    state["steps"].append(step)
    state["step_count"] = int(state.get("step_count") or 0) + 1
    if max_steps and len(state["steps"]) > max_steps:
        del state["steps"][:len(state["steps"]) - max_steps]


def _reset(state: dict) -> None:
    """A re-dispatch. The log appends across runs and showing run 1's result
    as run 2's current state was a real observed bug - everything before the
    marker belongs to a run that is over, including its session id (resuming
    run 1 while looking at run 2 would open an unrelated transcript)."""
    state["steps"].clear()
    state["step_count"] = 0
    state["final"] = None
    state["session_id"] = ""


def fold_line(state: dict, raw: Union[bytes, str], max_steps: int = 0) -> None:
    """Fold one raw log line into the feed. Tolerant of anything: a partial
    or non-JSON line is a moment in a file two processes share, not an error."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    line = raw.strip()
    if not line:
        return
    try:
        ev = json.loads(line)
    except (ValueError, TypeError):
        return
    if isinstance(ev, dict):
        fold_event(state, ev, max_steps)


def fold_event(state: dict, ev: dict, max_steps: int = 0) -> None:
    """Fold one parsed event - claude or codex shaped - into the feed.

    ``state`` needs: steps (list), final, session_id, step_count. Extra keys
    (a byte cursor, a cache stamp) are the caller's business and untouched.
    """
    # THE CLAUDE SESSION THIS RUN IS, which is what makes it resumable: the
    # id is the handle for `claude --resume`. Taken from the first line that
    # has one and then left alone (measured: one id per run across a log).
    if not state.get("session_id"):
        sid = ev.get("session_id")
        if isinstance(sid, str) and sid:
            state["session_id"] = sid

    etype = ev.get("type")
    if etype == "bgate_run_start":
        _reset(state)
    elif etype == "assistant":
        for block in blocks(ev):
            if block.get("type") == "text" \
                    and str(block.get("text", "")).strip():
                add_step(state, {"kind": "say",
                                 "text": str(block["text"]).strip()[:1000]},
                         max_steps)
            elif block.get("type") == "tool_use":
                name = str(block.get("name", "?"))
                inp = block.get("input") \
                    if isinstance(block.get("input"), dict) else {}
                short = name.replace(MCP_TOOL_PREFIX, "")
                # 200, not 120: a Bash hint is a command line, and the old
                # cap cut most of them off inside the `cd "<long path>" &&`
                # prefix every dispatched agent opens with.
                add_step(state, {"kind": "tool", "name": short,
                                 "hint": tool_subject(short, inp)[:200]},
                         max_steps)
    elif etype == "user":
        for block in blocks(ev):
            if block.get("type") == "tool_result":
                c = block.get("content")
                txt = c if isinstance(c, str) else (
                    c[0].get("text", "") if isinstance(c, list) and c
                    and isinstance(c[0], dict) else "")
                txt = str(txt).strip()
                if txt:
                    add_step(state, {"kind": "result", "text": txt[:600],
                                     "truncated": len(txt) > 600}, max_steps)
            elif block.get("type") == "text":
                # Replayed user turns - the FIRST is the dispatch prompt,
                # which is plumbing, not activity. Only steers surface.
                txt = str(block.get("text", ""))
                if STEER_MARKER in txt:
                    add_step(state, {"kind": "steer",
                                     "text": txt.split(STEER_MARKER, 1)[1]
                                     .strip()[:600]}, max_steps)
    elif etype == "result":
        # The agent's actual answer. NOT truncated to a preview length - it
        # is the deliverable sentence; the cap only stops a runaway result
        # from pinning memory.
        state["final"] = {"subtype": ev.get("subtype"),
                          "text": str(ev.get("result", ""))[:20000],
                          "cost": ev.get("total_cost_usd"),
                          "turns": ev.get("num_turns"),
                          # The tokens, and which model spent them. `cost` on
                          # a subscription is what this WOULD have cost on the
                          # API; these are what the usage window meters.
                          "model": final_model(ev),
                          "tokens": final_tokens(ev)}
    elif etype and etype.startswith(("thread.", "turn.", "item.")):
        _fold_codex(state, str(etype), ev, max_steps)


def final_tokens(ev: dict) -> dict:
    """The run's token usage, in this vocabulary's four names. Read off
    `usage` rather than summed from per-turn events: the CLI already totals
    it there."""
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


def final_model(ev: dict) -> str:
    """Which model actually ran, as the CLI names it - including the
    context-window suffix, which is the distinction that meters differently.
    Recorded because nothing else in the system knew."""
    usage = ev.get("modelUsage")
    if isinstance(usage, dict) and usage:
        return max(usage, key=lambda k: (usage[k] or {})
                   .get("cacheReadInputTokens", 0)
                   if isinstance(usage[k], dict) else 0)
    return str(ev.get("model") or "")


# ---------------------------------------------------------------------------
# The other vocabulary: `codex exec --json` speaks a smaller, dotted event
# language. The two do not collide - dotted names against bare ones - so one
# reader handles a log of either kind without being told which runner wrote
# it. The mapping is deliberately lossy toward what the agent DID, not which
# vendor's noun it used.
# ---------------------------------------------------------------------------
def _fold_codex(state: dict, etype: str, ev: dict, max_steps: int = 0) -> None:
    if etype == "thread.started":
        _reset(state)      # same job as bgate_run_start
        return
    if etype == "turn.completed":
        # NO PRICE HERE, ON PURPOSE - see runners.Runner.cost_tracked. Tokens
        # are recorded so the run is not a black box; nothing downstream may
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
    if kind in CODEX_QUIET_ITEMS:
        return
    if kind == "agent_message":
        text = str(item.get("text") or "").strip()
        if text:
            add_step(state, {"kind": "say", "text": text[:1000]}, max_steps)
            # The LAST message of the run is the deliverable; codex has no
            # separate result event to carry it. Overwritten each time.
            state["final"] = {"subtype": "success", "text": text[:20000],
                              "cost": None, "turns": None}
        return
    if kind == "command_execution":
        add_step(state, {"kind": "tool", "name": "Bash",
                         "hint": str(item.get("command") or "")[:120]},
                 max_steps)
        output = str(item.get("aggregated_output") or "").strip()
        code = item.get("exit_code")
        if output or code:
            add_step(state, {"kind": "result",
                             "text": (f"exit {code}\n" if code else "")
                             + output[:600],
                             "truncated": len(output) > 600}, max_steps)
        return
    if kind in ("mcp_tool_call", "tool_call"):
        name = str(item.get("tool") or item.get("name") or "?")
        args = item.get("arguments") \
            if isinstance(item.get("arguments"), dict) else {}
        short = name.replace(MCP_TOOL_PREFIX, "")
        add_step(state, {"kind": "tool", "name": short,
                         "hint": (tool_subject(short, args)
                                  or str(args.get("seat") or ""))[:200]},
                 max_steps)
        return
    if kind in ("file_change", "patch_apply"):
        changes = item.get("changes") \
            if isinstance(item.get("changes"), list) else []
        paths = ", ".join(str((c or {}).get("path") or "") for c in changes[:4])
        add_step(state, {"kind": "tool", "name": "Edit",
                         "hint": (paths or str(item.get("path") or ""))[:120]},
                 max_steps)
        return
    # An item type this version has never seen still belongs in the feed -
    # silence would make a new codex capability look like an agent doing
    # nothing.
    label = str(item.get("text") or item.get("command") or kind)
    add_step(state, {"kind": "tool", "name": kind or "item",
                     "hint": label[:120]}, max_steps)


# ---------------------------------------------------------------------------
# The from-anywhere reader (backs the MCP agent_activity tool)
# ---------------------------------------------------------------------------
def _liveness(root, item_id: int) -> dict:
    """Is a process on this machine genuinely running this item, per the
    on-disk registry? Best-effort: an unreadable registry reads as unknown."""
    try:
        from . import aegis, agentreg
        for entry in agentreg.live():
            if int(entry.get("item_id") or 0) != int(item_id):
                continue
            here = entry.get("root") or ""
            if here and aegis.within(Path(here), Path(root)) \
                    or aegis.within(Path(root), Path(here)):
                return {"running": True, "pid": entry.get("pid"),
                        "seat": entry.get("seat") or "",
                        "started_at": entry.get("started_at")}
        return {"running": False}
    except Exception:
        return {"running": None}


def tail(root, item_id: int, limit: int = 30,
         max_bytes: int = MAX_TAIL_BYTES) -> dict:
    """The last ``limit`` steps of an item's agent log, plus liveness.

    Cache-free and cursor-free on purpose: this serves an occasional MCP
    call from ANY process (CLI, desktop, a second dashboard), so there is no
    state to go stale between them; the dashboard's polling feed keeps its
    own byte cursor and folds through the same functions above.

    ``truncated`` says the window opened mid-run (the byte cap cut the head
    off, or ``limit`` did) - a caller that needs everything reads the log
    file the result names.
    """
    path = log_path(root, item_id)
    out = {"item_id": int(item_id), "log": str(path),
           **_liveness(root, item_id)}
    try:
        size = path.stat().st_size
    except OSError:
        return {**out, "steps": [], "final": None, "step_count": 0,
                "truncated": False, "session_id": "",
                "note": "no agent log for this item - it has never been "
                        "dispatched here, or the log was cleaned up"}
    clipped = size > max_bytes
    try:
        with open(path, "rb") as fh:
            if clipped:
                fh.seek(size - max_bytes)
            data = fh.read(max_bytes)
    except OSError:
        return {**out, "steps": [], "final": None, "step_count": 0,
                "truncated": False, "session_id": "",
                "note": "the agent log exists but could not be read"}
    lines = data.split(b"\n")
    if clipped:
        lines = lines[1:]  # the first line is almost certainly partial
    state: dict = {"steps": [], "final": None, "session_id": "",
                   "step_count": 0}
    for raw in lines:
        fold_line(state, raw)
    steps = state["steps"]
    window = steps[-limit:] if limit else steps
    return {**out,
            "steps": window,
            "final": state["final"],
            "step_count": state["step_count"],
            "truncated": clipped or len(window) < len(steps),
            "session_id": state["session_id"]}
