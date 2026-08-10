"""The work item's own endpoints: read it, edit it, retry it, undo it.

``queue.update`` and the reopen flow existed in core and as MCP tools but had no
HTTP route, so from the dashboard a failed item was a dead end and a typo'd
brief was unfixable — the most common motion in an agent runner had no button.
The other half is the review surface: an item now carries the commit its run
started from, so what the agent did is readable (``/diff``) and undoable
(``/revert``) instead of being a sha256 in an iteration row.

Auto-registers via routes/__init__.py. Everything answers the api.py envelope;
ValueError from core is a 400, LookupError a 404, and a project with no git is
``{available: false, reason}`` rather than a 500.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request

from bgate_core import gitwork as _git
from bgate_core import queue as _queue
from bgate_core import settings as _settings
from bgate_core import spend as _spend
from bgate_ui import api
from bgate_ui import dispatch as _dispatch
from bgate_ui.deps import root
# The budget PATCH is an alias over the settings registry, so it shares that
# router's validate-then-write helper. One import between two route modules is
# cheaper than a third copy of the same loop, which is what this alias exists to
# stop.
from bgate_ui.routes import settings as _settings_routes

router = APIRouter()


def _item(item_id: int) -> dict:
    try:
        return _queue.get(root(), item_id)
    except LookupError:
        raise api.not_found(f"no work item {item_id}", item_id=item_id)


def _base_commit(item: dict) -> str:
    """The boundary this item's last run started from.

    Prefer the column; fall back to the on-disk run record, which survives a
    dashboard restart and is written even when the DB write lost a race."""
    return (item.get("base_commit")
            or _dispatch.read_run_record(root(), item["id"]).get("base_commit", ""))


@router.get("/api/queue/{item_id:int}")
def queue_item(item_id: int) -> dict:
    return api.ok(_item(item_id))


@router.patch("/api/queue/{item_id:int}")
def queue_patch(item_id: int, payload: dict) -> dict:
    """Fix the request without losing its lineage — title, brief, seat,
    priority, and the per-item ceilings. Status is deliberately not editable
    here; that is what reopen/cancel are for."""
    _item(item_id)
    fields = {k: payload[k] for k in
              ("title", "brief", "seat", "priority", "max_cost_usd", "max_runtime_s")
              if k in payload and payload[k] is not None}
    if not fields:
        raise api.bad_request(
            "nothing to change — send title, brief, seat, priority, "
            "max_cost_usd or max_runtime_s")
    try:
        return api.ok(_queue.update(root(), item_id, **fields))
    except ValueError as exc:
        raise api.bad_request(str(exc))


@router.post("/api/queue/{item_id:int}/reopen")
def queue_reopen(item_id: int, payload: Optional[dict] = None) -> dict:
    """Send a done/failed/cancelled item back to the queue for another attempt.
    The reason is appended to the brief so the next agent reads what to fix."""
    _item(item_id)
    reason = (payload or {}).get("reason", "")
    try:
        return api.ok(_queue.reopen(root(), item_id, reason))
    except ValueError as exc:
        raise api.bad_request(str(exc), item_id=item_id)


@router.post("/api/queue/{item_id:int}/cancel")
def queue_cancel(item_id: int, payload: Optional[dict] = None) -> dict:
    """Call the work off. A live agent is killed first — leaving one running
    against a cancelled item is how orphans and surprise spend happen."""
    item = _item(item_id)
    if item["status"] == "cancelled":
        return api.ok(item)
    reason = (payload or {}).get("reason", "cancelled from the dashboard")
    stopped = _dispatch.stop(item_id) if item["status"] == "dispatched" else {}
    result = _queue.set_status(root(), item_id, "cancelled", result=str(reason)[:2000])
    return api.ok(result, agent_stopped=bool(stopped.get("ok")))


@router.get("/api/queue/{item_id:int}/diff")
def queue_diff(item_id: int, path: Optional[str] = None) -> dict:
    """What the run actually changed, per file, since its base commit."""
    item = _item(item_id)
    base = _base_commit(item)
    if not base:
        return api.ok({"available": False, "base": "",
                       "reason": "this item has no recorded base commit — it "
                                 "was dispatched before git tracking, or the "
                                 "project is not a git repository",
                       "files": []})
    work_root = item.get("worktree") or root()
    got = _git.diff(work_root, base, paths=[path] if path else None)
    got["item_id"] = item_id
    got["worktree"] = item.get("worktree") or ""
    return api.ok(got)


@router.post("/api/queue/{item_id:int}/revert")
def queue_revert(request: Request, item_id: int,
                 payload: Optional[dict] = None) -> dict:
    """Put back everything this run touched, and nothing else.

    Scoped to the run's own paths and guarded by the fingerprint taken when it
    ended: if any of those files changed since, the whole revert is refused
    rather than quietly discarding a human's later edit. Pass ``force`` to
    revert anyway once you have looked at the diff."""
    api.require_human(api.current_actor(request), "revert an agent's changes")
    item = _item(item_id)
    base = _base_commit(item)
    if not base:
        raise api.bad_request(
            "this item has no recorded base commit — there is nothing to revert to",
            item_id=item_id)
    payload = payload or {}
    record = _dispatch.read_run_record(root(), item_id)
    expect = None if payload.get("force") else (record.get("paths") or None)
    work_root = item.get("worktree") or root()
    got = _git.revert(work_root, base, expect=expect,
                      paths=payload.get("paths") or None)
    if not got["available"]:
        raise api.bad_request(got["reason"], item_id=item_id)
    if got["conflicts"]:
        raise api.conflict(got["reason"], conflicts=got["conflicts"],
                           item_id=item_id)
    return api.ok(got)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

@router.get("/api/spend")
def spend_view() -> dict:
    """Spent, by day/project/kind, against the ceilings that stop dispatch.

    ``project_usd`` and ``today_usd`` are REAL money — vendor-invoiced image,
    mesh and audio generation. Agent sessions report a notional API-equivalent
    price that a subscription already covers, and they live under
    ``subscription`` with the tokens that actually meter the plan. Summing the
    two gave this project a $1,307 total of which $13.91 had ever been charged.
    """
    return api.ok(_spend.totals(root()))


def _budget_keys() -> dict[str, str]:
    """``spend_budget`` column -> the registry key that describes it.

    Derived from the registry rather than listed here, so a new budget column is
    one ``Setting`` entry and this alias picks it up. Note the deliberate
    asymmetry it exposes: ``max_concurrent`` is described as
    ``dispatch.max_concurrent`` because that is what it means to a human, even
    though it lives in the budget row.
    """
    return {s.store[1]: s.key for s in _settings.SETTINGS
            if s.store and s.store[0] == "budget"}


@router.patch("/api/spend/budget")
def spend_budget(request: Request, payload: dict) -> dict:
    """Raising a ceiling is a human decision — an agent must not be able to
    widen the gate that bounds it.

    A THIN ALIAS over ``bgate_core.settings`` since the registry landed. The
    payload and the ``data`` shape are unchanged (columns in, the whole
    ``spend_budget`` row out) because the console's money panel is built on them,
    but the validation is no longer a second copy living here: two write paths for
    one SQL row is exactly the duplication the registry deletes, and the copy that
    was here only checked three of the six fields for a lower bound and nothing
    for an upper one. Values that the registry's declared range refuses now come
    back as a 400 naming the range — including some this route used to accept.
    """
    api.require_human(api.current_actor(request), "change the budget")
    columns = _budget_keys()
    changes: dict[str, object] = {}
    ignored: list[str] = []
    for column, value in dict(payload or {}).items():
        if value is None:
            continue  # unchanged: what "PATCH loosely" has always meant here
        key = columns.get(str(column))
        if key is None:
            # Ignored, not refused: set_budget dropped unknown keys silently and
            # the UI PATCHes the whole form back. They are named in the response
            # so a typo is visible instead of merely ineffective.
            ignored.append(str(column))
            continue
        changes[key] = value
    if not changes:
        return api.ok(_spend.budget(root()), applied=[], ignored=ignored)
    applied = _settings_routes.apply_changes(
        root(), changes, actor=api.current_actor(request))
    by_key = {row["key"]: row for row in applied}
    for column, key in columns.items():
        if key in by_key:
            by_key[key]["field"] = column
    return api.ok(_spend.budget(root()), applied=applied, ignored=ignored)
