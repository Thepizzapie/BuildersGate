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


@router.get("/api/queue/graph")
def queue_graph() -> dict:
    """The dependency graph, normalised, with a topological DISPLAY order.

    THE READABILITY DEFECT THIS ANSWERS. The board showed

        #42 enlarge rooms       done
        #45 swap in furniture   running
        #43 rebuild routes      queued

    which reads as a scheduler that skipped #43. It did not: #43 was filed
    after #42, and #45 was inserted BETWEEN them later, because the route
    measurements had to wait for real furniture dimensions. The dependency
    engine was correct the whole time. The presentation made it look broken,
    and an operator who believes the scheduler is broken starts working around
    it.

    IDS ARE NOT RENUMBERED. They are in briefs, commit messages and people's
    heads. `execution_position` is a display index derived from the graph;
    `execution_state` is the one word a card colours by. `work_item.depends_on`
    and `work_item_dep` are presented as ONE graph — which table holds a link is
    not a question anybody should have to answer.
    """
    return api.ok(_queue.graph(root()))


@router.get("/api/queue/{item_id:int}/path")
def queue_path(item_id: int) -> dict:
    """Everything that has to happen before this item, in the order it happens.

    `#42 -> #45 -> #43` as data, so a blocked card can show the chain it is
    waiting behind rather than a status word.
    """
    _item(item_id)
    return api.ok({"item": item_id,
                   "path": _queue.execution_path(root(), item_id),
                   "waiting_line": _queue.waiting_line(root(), item_id)})


@router.get("/api/queue/stalled")
def queue_stalled(seat: Optional[str] = None) -> dict:
    """Queued work NO dispatcher will take, and what would release each row.

    `/api/queue` answers "what is on the board". Nothing answered "what is
    sitting here that nothing will ever start", and the difference between
    those two lists is an operator's whole morning. An item whose retries are
    spent, whose source is human-held, or whose seat the stage is holding
    looked identical to fresh work; the only tell was reading the retry
    counters off the row by hand.
    """
    return api.ok({"items": _queue.stalled(root(), seat=seat or "")})


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
    # THE ONE SAFE PLACE TO DROP AN ISOLATED RUN'S WORKTREE, and the only thing
    # that ever drained them: dispatch cuts .bgate/work/item-N when
    # `dispatch.isolation` is on and nothing removed it, so every isolated run
    # left a full checkout and a bgate/item-N branch behind forever.
    #
    # NOT at run finish, which is where it looks like it belongs. The agent's
    # edits are UNCOMMITTED inside that worktree and nothing merges the branch,
    # so `worktree remove --force` there would delete the run's entire output —
    # and /diff, /revert, /peek and the history previews all resolve a finished
    # item's files through item["worktree"] for as long as the item exists.
    # A completed full revert is the moment that stops being true: the human
    # has just put every path this run touched back to base, so what is left in
    # the checkout is base, and there is nothing in it to lose.
    if item.get("worktree") and not payload.get("paths"):
        got["worktree_removed"] = _git.remove_worktree(root(), item_id)
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
