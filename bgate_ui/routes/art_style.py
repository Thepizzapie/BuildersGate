"""Training a style from this project's pinned anchors, and choosing to use it.

WHY THIS IS A ROUTE AND NOT AN MCP TOOL. Training is 5-15 minutes at a price Krea
does not publish, which means `spend.check` cannot bound it — every other money
path in this tool is capped by a number the dispatcher can compare against, and
this one has no number to compare. A tool an agent can call is therefore a tool
that can spend an unknown amount on its own initiative, so the only caller is a
human, through `api.require_human`, the same rule that guards the budget row and
the revert.

The dataset is the PINNED REFERENCES — the images a human already approved
through `ref_pin`. See bgate_core.styles for why that is the point rather than a
shortcut.

Training runs on a thread and the route returns immediately with a job id. A
15-minute request would hold a worker, time out in the browser, and leave the
caller unable to tell "still training" from "died".
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from fastapi import APIRouter, Request

from bgate_core import activity as _activity
from bgate_core import styles as _styles
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

# One training run per project at a time. Two runs on the same anchors produce
# two styles that differ by nothing anybody chose, and both get charged for.
_running: dict[str, dict] = {}
_lock = threading.Lock()


def _state(project) -> dict:
    with _lock:
        return dict(_running.get(str(project)) or {})


@router.get("/api/art/style")
def style_view() -> dict:
    """The trained styles, the active one, the mode, and the dataset verdict."""
    project = root()
    got = _styles.describe(project)
    got["running"] = _state(project)
    return api.ok(got)


@router.post("/api/art/style/train")
def style_train(request: Request, payload: Optional[dict] = None) -> dict:
    """Start a training run against the pinned anchors. Returns immediately.

    ``names`` narrows the dataset to specific pins; omitted means every pin that
    clears the floor. ``name`` is what the style is called afterwards — it is how
    you will tell two of them apart, so it is required rather than defaulted to
    a timestamp.
    """
    api.require_human(api.current_actor(request), "train a style")
    payload = payload or {}
    project = root()
    key = str(project)

    name = str(payload.get("name") or "").strip()
    if not name:
        raise api.bad_request("a trained style needs a name — it is how you "
                              "tell two of them apart later")

    live = _state(project)
    if live and live.get("status") == "running":
        raise api.conflict(
            "a training run is already going on this project — two runs on the "
            "same anchors produce two styles that differ by nothing anybody "
            "chose, and both are charged for", job_id=live.get("job_id", ""))

    names = [str(n) for n in (payload.get("names") or [])]
    verdict = _styles.dataset(project, names or None)
    if not verdict.get("ok"):
        # 400 with the per-anchor reasons: "not enough images" without saying
        # WHICH ones were dropped and why is a dead end for the person who has
        # to fix the dataset.
        raise api.bad_request(
            verdict.get("reason") or "this dataset cannot train a style",
            rejected=verdict.get("rejected") or [],
            usable=verdict.get("usable_names") or [],
            warnings=verdict.get("warnings") or [])

    from bgate_adapters import krea

    reachable = krea.available(project)
    if not reachable.get("available"):
        raise api.unavailable(reachable.get("reason")
                              or "no Krea API key on this project")

    with _lock:
        _running[key] = {"status": "running", "name": name, "job_id": "",
                         "images": len(verdict["usable"]),
                         "started_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                                     time.gmtime())}

    def _run() -> None:
        # Everything inside is guarded: this is a daemon thread, and an
        # exception here would leave the panel showing "running" forever with no
        # way to find out otherwise.
        try:
            got = krea.train(
                name, verdict["usable"], root=project,
                kind=str(payload.get("kind") or "Style"),
                trigger_word=str(payload.get("trigger_word") or ""),
                max_train_steps=payload.get("max_train_steps"),
                learning_rate=payload.get("learning_rate"))
            if got.get("ok") and got.get("style_id"):
                _styles.record(project, {
                    "style_id": got["style_id"], "name": name,
                    "trigger_word": got.get("trigger_word") or "",
                    "model": got.get("model") or "", "kind": got.get("kind") or "",
                    "images": got.get("images") or 0,
                    "sources": verdict.get("usable_names") or [],
                }, make_active=bool(payload.get("make_active", True)))
                with _lock:
                    _running[key] = {"status": "done", "name": name,
                                     "style_id": got["style_id"],
                                     "job_id": got.get("job_id", "")}
                return
            with _lock:
                _running[key] = {"status": "failed", "name": name,
                                 "error": got.get("error") or "training failed"}
        except Exception as exc:                                # noqa: BLE001
            with _lock:
                _running[key] = {"status": "failed", "name": name,
                                 "error": f"{type(exc).__name__}: {exc}"}
        finally:
            try:
                _activity.log(project, "style",
                              f"training run for {name} ended: "
                              f"{_state(project).get('status')}", seat="art")
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True, name="bgate-style-train").start()
    return api.ok({"started": True, "name": name,
                   "images": len(verdict["usable"]),
                   "sources": verdict.get("usable_names") or [],
                   "warnings": verdict.get("warnings") or [],
                   "note": "5-15 minutes. Poll GET /api/art/style."})


@router.post("/api/art/style/active")
def style_activate(request: Request, payload: dict) -> dict:
    """Point generations at a different trained style, or at none."""
    api.require_human(api.current_actor(request), "change the active style")
    try:
        got = _styles.set_active(root(), str((payload or {}).get("style_id") or ""))
    except LookupError as exc:
        raise api.not_found(str(exc))
    return api.ok({"active": got})


@router.delete("/api/art/style/{style_id}")
def style_forget(request: Request, style_id: str) -> dict:
    """Forget a trained style. It still exists on Krea's side — this is the
    project's record, not a delete, and the response says so rather than letting
    a user believe money was reclaimed."""
    api.require_human(api.current_actor(request), "forget a trained style")
    if not _styles.forget(root(), style_id):
        raise api.not_found(f"no trained style {style_id!r} on this project")
    return api.ok({"forgotten": style_id,
                   "note": "removed from this project's record; the style still "
                           "exists on your Krea account"})
