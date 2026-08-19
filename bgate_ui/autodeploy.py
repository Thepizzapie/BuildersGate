"""Auto-deploy — the hands-off handoff between agents.

A delegation produces children: the director splits one ask into five queued
items and then stops. Somebody has to press dispatch on each of them, and while
nobody does, the board sits full of work with no agent on it. That pause is the
whole reason a five-seat floor feels like a to-do list instead of a studio.

This is the loop that removes it. While it is ON, every queued item is
dispatched as a slot frees up — priority first, oldest first inside a priority —
so a director's children fire the moment the parent finishes and the QA gate's
follow-ups fire the moment they are filed.

Runs as a daemon thread inside the dashboard, the same shape as qa_gate: core
stays pure data, spawning agents stays a UI-server concern.

Three things it deliberately does NOT do:

  * It never dispatches a ``qa-gate-escalation``. That item exists precisely
    because two agents could not agree and a human has to decide; auto-spending
    another agent on it is the money pump the escalation was invented to stop.
  * It never retries a refused item on the next tick. dispatch() refuses for
    real reasons (dirty tree, budget, scope, concurrency) and a 4-second retry
    loop against a dirty tree is a hot loop that writes nothing but noise. A
    refused item goes on a cooldown; a refusal that is about the FLOOR rather
    than the item (concurrency, budget, a dirty tree) ends the tick.
  * It never hides a refusal. The last one is kept and served with the state,
    because an autopilot that is quietly doing nothing looks exactly like an
    autopilot that is working and has nothing to do — and the first person to
    hit that spends ten minutes wondering why the board is frozen.

The switch itself is persisted per project (workspace doc ``director/autopilot``)
so it survives a restart, and read fresh on every tick so flipping it in one tab
takes effect everywhere.

IT IS READ THROUGH ``bgate_core.settings`` (key ``autopilot.on``) — same doc,
same field, one precedence rule. The reason that matters here is
``BGATE_AUTODEPLOY``: it decides whether the thread starts, so a project whose
stored switch says ON looked ON in the console while nothing was ever dispatched.
The registry makes that var coerce the value it is really controlling, and
``state()`` now says which layer won.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

from bgate_core import activity, settings as _settings, workspace as _ws
from bgate_ui.pump import Pump

SEAT = "director"
KEY = "autopilot"
SETTING = "autopilot.on"
POLL_S = 4.0

# Sources auto-deploy will not touch, and why.
#   qa-gate-escalation — two agents could not agree and a human has to decide.
#   chat — a message to the director. The console dispatches those itself the
#     moment they are written, and the queue panel offers a deploy button for
#     one that was refused; autopilot grabbing them is how a turn got dispatched
#     with the placeholder brief still in it.
#
# DEFINED IN queue.py NOW, because a worker's queue_claim_next is a second
# automatic dispatcher in a second process and must hold the same items back;
# two copies of this tuple is how one of them grabs an escalation. The names
# stay importable from here — this module is where the policy is explained.
from bgate_core.queue import HELD_SOURCES, PLACEHOLDER_BRIEF  # noqa: E402

# A row created in two statements — INSERT with a placeholder, then UPDATE with
# the real text — is briefly dispatchable with nothing in it. Both the console
# turn and the delegate item are built that way (the brief has to name the row's
# own id), so autopilot (and claim_next) skips anything still wearing the
# placeholder rather than spending an agent on the word "(preparing)".

# How long an item that refused sits out before it is offered again.
ITEM_COOLDOWN_S = 90.0
# Refusals that are about the floor, not the item — the whole tick stops.
FLOOR_CODES = ("concurrency_limit", "budget_exceeded", "dirty_tree",
               "worktree_failed")
FLOOR_COOLDOWN_S = 20.0

# Which projects have a loop running, and the kill switch that decides whether
# one starts at all. Keyed by root, not a single flag: the active project can
# change under a long-lived server (BGATE_ROOT, `bgate use`), and a latched flag
# meant the thread kept dispatching into the project the user had already left.
_pump = Pump("bgate-autodeploy", lambda: POLL_S, lambda root: tick(root),
             env_var="BGATE_AUTODEPLOY")
_lock = threading.Lock()
# root -> {"cool": {item_id: until}, "floor_until": float, "last": {...}}
_MEM: dict[str, dict] = {}


# How many runs may die instantly, back to back, before the board stops
# feeding the queue into whatever is broken — and how long it then waits.
BURN_LIMIT = 3
BURN_COOLDOWN_S = 300.0
# A run that lasted less than this and reported nothing did not do any work.
QUICK_FAIL_S = 45


def _mem(root: str) -> dict:
    with _lock:
        return _MEM.setdefault(str(root), {"cool": {}, "floor_until": 0.0,
                                           "last": None, "dispatched": 0,
                                           "quick_fails": 0})


def note_run_ended(root, item_id: int, outcome: str, seconds: float) -> None:
    """Told by the dispatcher how a run finished, so the loop can notice a
    runner that is failing every time.

    Counts CONSECUTIVE instant failures: any run that produced work — or that
    merely lasted a while before failing — resets the counter, because a slow
    failure is a real attempt at real work and a fast one is the runner
    refusing to start.
    """
    mem = _mem(str(root))
    with _lock:
        if outcome == "done" or seconds >= QUICK_FAIL_S:
            mem["quick_fails"] = 0
        else:
            mem["quick_fails"] = int(mem.get("quick_fails") or 0) + 1


def _burning(root: str, mem: dict) -> bool:
    return int(mem.get("quick_fails") or 0) >= BURN_LIMIT


def enabled(root: str | os.PathLike[str]) -> bool:
    """Is autopilot on, after the env kill switch has had its say.

    Falls back to the raw doc if the registry will not read: a tick that cannot
    answer this question is a tick that dispatches nothing, and an unreadable
    settings doc should not silently freeze the board.
    """
    try:
        return bool(_settings.get(root, SETTING))
    except Exception:
        return bool(_ws.get(root, SEAT, KEY, {}).get("on"))


def state(root: str | os.PathLike[str]) -> dict:
    """The switch plus what it has actually been doing.

    ``last`` is the last refusal, not the last success: a success is visible on
    the board as a running agent, a refusal is visible nowhere else.

    ``on`` is the EFFECTIVE value — with ``BGATE_AUTODEPLOY=0`` set, the stored
    switch is not what the board is doing, and reporting the stored value there
    is how a console shows a green autopilot on a loop that never started.
    ``stored_on``/``source``/``env_override`` say which layer won.
    """
    doc = _ws.get(root, SEAT, KEY, {})
    mem = _mem(str(root))
    try:
        on, source = bool(_settings.get(root, SETTING)), _settings.source(root, SETTING)
    except Exception:
        on, source = bool(doc.get("on")), _settings.SOURCE_STORED
    forced = _pump.disabled()
    return {
        "on": on,
        "stored_on": bool(doc.get("on")),
        "source": source,
        "setting": SETTING,
        "env_override": ("BGATE_AUTODEPLOY=0 keeps auto-deploy off — the loop is "
                         "not running in this process") if forced else "",
        "since": doc.get("since") or "",
        "by": doc.get("by") or "",
        "dispatched": int(mem.get("dispatched") or 0),
        "last_refusal": mem.get("last"),
        "held_sources": list(HELD_SOURCES),
        "running": _pump.running(root),
    }


def set_enabled(root: str | os.PathLike[str], on: bool) -> dict:
    """Flip the switch, clear the cooldowns, log it.

    Still writes the doc directly rather than going through ``settings.set``:
    this path also has to drop the per-item cooldowns (otherwise turning
    autopilot off and on again leaves 90 seconds of items still sitting out) and
    write the activity line. Same doc, same field, and the value is validated by
    the registry so the two writers cannot disagree about what "on" means — a
    route that passed the string "0" set it to True before this.
    """
    try:
        on = _settings.coerce(SETTING, on)
    except _settings.SettingError:
        on = bool(on)
    doc = _ws.get(root, SEAT, KEY, {})
    doc["on"] = bool(on)
    doc["since"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    doc["by"] = activity.current_actor()
    _ws.set(root, SEAT, KEY, doc)
    mem = _mem(str(root))
    with _lock:
        mem["cool"] = {}
        mem["floor_until"] = 0.0
        if on:
            mem["last"] = None
    activity.log(root, "autodeploy",
                 "auto-deploy ON — queued work dispatches itself" if on
                 else "auto-deploy OFF — dispatch is manual again",
                 seat="director")
    return state(root)


def _candidates(root: str) -> list[dict]:
    """Queued items, most deserving first, minus the ones we hold back.

    A chained item whose predecessor has not landed is filtered out HERE rather
    than left to dispatch()'s refusal, because a refusal costs the item a
    cooldown and fills the "last refusal" slot with a non-event: the board is
    working exactly as designed, the link is simply next rather than ready. The
    tick that follows its predecessor's completion picks it up on its own.
    """
    # THE READINESS RULE IS queue.ready(), shared verbatim with a worker's
    # claim_next - two dispatchers, one definition of "may start". The limit
    # is 120, raised from 40: chain links default to priority 0 and QA /
    # escalation rows run 7-9, so on a busy board forty higher-priority rows
    # pushed every chain successor past the window and the chain simply never
    # advanced — starvation with no refusal recorded anywhere.
    from bgate_core import queue as _q
    return _q.ready(root, limit=120)


def tick(root: str | os.PathLike[str], *, force: bool = False) -> dict:
    """One pass. Returns what it dispatched and what refused.

    Safe to call directly — the endpoint uses it to make the toggle feel
    immediate instead of up-to-four-seconds late, and the tests use it instead
    of sleeping on a thread.
    """
    from bgate_ui import dispatch as _dispatch

    root = str(root)
    if not force and not enabled(root):
        return {"on": False, "dispatched": [], "refused": []}
    mem = _mem(root)
    now = time.monotonic()
    if mem["floor_until"] > now:
        return {"on": True, "dispatched": [], "refused": [],
                "held": "floor cooldown"}
    # No CLI is a floor condition, not forty identical per-item refusals: every
    # candidate would fail the same way and each failure re-probes the PATH.
    if not _dispatch.find_claude():
        entry = {"item_id": None, "code": "no_cli",
                 "message": "claude CLI not found on PATH — nothing can be "
                            "dispatched, automatically or otherwise",
                 "at": time.strftime("%H:%M:%S")}
        with _lock:
            mem["last"] = entry
            mem["floor_until"] = now + FLOOR_COOLDOWN_S
        return {"on": True, "dispatched": [], "refused": [entry]}

    sent: list[int] = []
    refused: list[dict] = []
    for item in _candidates(root):
        item_id = int(item["id"])
        if mem["cool"].get(item_id, 0) > now:
            continue
        # INSTANT-FAILURE BACKOFF. A dispatch that SPAWNS is a success here, so
        # nothing rate-limited the case where every spawn dies on contact — an
        # expired CLI login passes the PATH check, starts, exits without
        # reporting, and is banked as failed. With no cooldown on a successful
        # dispatch, autopilot walked the whole queue at four a tick and burned
        # thirty items to 'failed' in about ten minutes, then idled till morning.
        # Consecutive same-shaped failures are the signal; the fix is to stop
        # feeding the queue into a broken runner, not to retry harder.
        if _burning(root, mem):
            entry = {"item_id": None, "code": "runner_failing",
                     "message": (f"{mem.get('quick_fails', 0)} runs in a row "
                                 "ended immediately without reporting — the "
                                 "agent CLI is probably not able to start "
                                 "(expired login?). Holding the board rather "
                                 "than burning the queue; fix the CLI and it "
                                 "resumes on its own."),
                     "at": time.strftime("%H:%M:%S")}
            with _lock:
                mem["last"] = entry
                mem["floor_until"] = now + BURN_COOLDOWN_S
            refused.append(entry)
            break
        result = _dispatch.dispatch(root, item_id, actor="autodeploy")
        if result.get("ok"):
            sent.append(item_id)
            with _lock:
                mem["dispatched"] = int(mem.get("dispatched") or 0) + 1
                mem["cool"].pop(item_id, None)
            activity.log(root, "autodeploy",
                         f"auto-dispatched #{item_id} to {item['seat']}: "
                         f"{str(item['title'])[:70]}",
                         seat=item["seat"], ref=str(item_id))
            continue
        code = str(result.get("code") or result.get("error_code") or "")
        entry = {"item_id": item_id, "code": code or "refused",
                 "message": str(result.get("error") or "dispatch refused"),
                 "at": time.strftime("%H:%M:%S")}
        refused.append(entry)
        with _lock:
            mem["last"] = entry
            mem["cool"][item_id] = now + ITEM_COOLDOWN_S
        if code in FLOOR_CODES:
            # Nothing else will get out either — stop asking until the floor
            # has had a chance to change.
            with _lock:
                mem["floor_until"] = now + FLOOR_COOLDOWN_S
            break
    return {"on": True, "dispatched": sent, "refused": refused}


def start(root: str | os.PathLike[str]) -> bool:
    """Idempotently start the loop for this server process.

    The thread runs whether or not the switch is on — it reads the switch every
    tick, so turning it on in the browser must not require a restart. Returns
    False only for ``BGATE_AUTODEPLOY=0``, which is read once and decides whether
    the thread exists at all.
    """
    return _pump.start(root)


def reset(root: Optional[str | os.PathLike[str]] = None) -> None:
    """Drop the in-memory cooldowns. Tests use this; nothing else should.

    It deliberately does NOT clear the pump's started-latch. This loop is the one
    that spends money — un-latching it would let the next ``start`` put a SECOND
    dispatching thread on the same project, and a test that only wanted its
    cooldowns cleared would be running two autopilots.
    """
    with _lock:
        if root is None:
            _MEM.clear()
        else:
            _MEM.pop(str(root), None)
