"""The steer pump — deliver messages left in the inbox to the live agents.

``bgate_core.steerbox`` lets any process leave a message for a running agent;
only this one can hand it over, because only this one owns the stdin pipes. The
pump is the join between them: it runs beside the dashboard, drains the inbox,
and writes each message into the session it names.

An undeliverable message is ANSWERED, not deleted quietly. Three ways it can
fail and all three are worth saying out loud, because the party that wrote it
(usually the director agent, mid-run) has no other way to find out:

  * the item has no live agent — the run finished before the message arrived;
  * the agent is already finishing and its stdin is closed;
  * the message sat in the box past STALE_S and steering the NEXT run of that
    item with an old complaint would be worse than dropping it.

All three land in the activity ledger against the item, which is where anyone
looking at "why didn't my correction apply" is already looking.
"""
from __future__ import annotations

import os
import threading
import time

from bgate_core import activity, steerbox

POLL_S = 2.0

# Per project, for the same reason autodeploy is: the active project can change
# under a long-lived server and a latched flag pumps the wrong one.
_started: set[str] = set()
_lock = threading.Lock()


def drain(root: str | os.PathLike[str]) -> dict:
    """One pass. Returns what was delivered and what could not be."""
    from bgate_ui import dispatch as _dispatch

    delivered: list[dict] = []
    failed: list[dict] = []
    # take() has ALREADY removed every message from disk, so a raise part-way
    # through this loop does not retry them — it destroys the rest of the batch
    # silently, because the caller's guard swallows it. Each message is handled
    # on its own.
    for message in steerbox.take(root):
        try:
            item_id = int(message.get("item_id") or 0)
        except (TypeError, ValueError):
            failed.append({**message, "error": "unreadable item id"})
            continue
        who = message.get("by") or "someone"
        if message.get("stale"):
            failed.append({**message, "error": "expired before an agent read it"})
            _say(root, item_id,
                 f"steer for #{item_id} from {who} expired undelivered — the "
                 "run it was meant for had already ended")
            continue
        try:
            result = _dispatch.steer(str(root), item_id,
                                     str(message.get("text") or ""))
        except Exception as exc:  # a wedged pipe must not take the batch down
            failed.append({**message, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if result.get("ok"):
            delivered.append(message)
            _say(root, item_id,
                 f"{who} steered #{item_id}: {str(message.get('text'))[:120]}")
        else:
            failed.append({**message, "error": result.get("error", "undeliverable")})
            _say(root, item_id,
                 f"steer for #{item_id} from {who} was not delivered — "
                 f"{result.get('error', 'undeliverable')}")
    return {"delivered": delivered, "failed": failed}


def _say(root, item_id: int, summary: str) -> None:
    """Ledger write that cannot abort the batch (a locked DB is transient)."""
    try:
        activity.log(root, "steer", summary, ref=str(item_id))
    except Exception:
        pass


def _run(root: str) -> None:
    while True:
        time.sleep(POLL_S)
        try:
            drain(root)
        except Exception:
            pass  # fail-safe: the pump must never take the dashboard down


def start(root: str | os.PathLike[str]) -> bool:
    key = str(root)
    with _lock:
        if key in _started:
            return True
        _started.add(key)
    threading.Thread(target=_run, args=(key,), daemon=True,
                     name="bgate-steerpump").start()
    return True
