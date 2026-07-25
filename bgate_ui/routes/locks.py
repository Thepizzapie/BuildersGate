"""Who is holding what, and who is stuck behind them.

Contention was invisible: a lock failed instantly, the blocked agent moved on or
died quietly, and the dashboard showed nothing at all. One read answers the
three questions a human actually asks when the studio stalls — what is held,
who is waiting on it, and which text files a run is currently inside.

Expired claims never appear here: the accessors compare leases against the clock
before returning, so this is the live picture rather than the recorded one.
"""
from __future__ import annotations

from fastapi import APIRouter

from bgate_core import assets as _assets
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


@router.get("/api/locks")
def locks() -> dict:
    r = root()
    held = [item for item in _assets.list_assets(r, locked_only=True)]
    waiting = _assets.waiters(r)
    by_path: dict[str, list[dict]] = {}
    for waiter in waiting:
        by_path.setdefault(waiter["asset_path"], []).append(waiter)

    return api.ok({
        "held": [{
            "path": item["path"],
            "kind": item["kind"],
            "seat": item["lock_seat"],
            "owner": item["lock_owner"] or "",
            "actor": item["lock_actor"] or "",
            "work_item_id": item["work_item_id"],
            "since": item["lock_at"],
            "heartbeat_at": item["heartbeat_at"],
            "lease_expires_at": item["lease_expires_at"],
            "waiters": by_path.get(item["path"], []),
        } for item in held],
        "waiters": waiting,
        "path_leases": _assets.list_path_leases(r),
    })
