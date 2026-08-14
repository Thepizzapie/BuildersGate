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


@router.post("/api/locks/release")
def lock_release(payload: dict) -> dict:
    """Drop a lease from the dashboard.

    WHY THIS EXISTS. Both binary seats reached the same wall: the panel could
    show a lock and could not clear one, because release lived only behind the
    MCP tool. So a lease left behind by a run that died — the common case, since
    a killed process never releases — could be cleared only by opening an agent
    session in another window to unstick a file a human was looking at. Art's
    audit ended up printing the literal `asset_release path="…"` call for the
    reader to type somewhere else, which is a UI admitting it is a dead end.

    THE HOLDER RULE IS THE BACKEND'S AND STAYS THERE. `assets.release` refuses a
    caller that is not the holder, and this does not work around it: a human
    clearing somebody else's stale lease passes `seat`, and the refusal comes
    back as a 400 with the holder named. Forcing it open is deliberately not
    offered here — a lease that is genuinely live belongs to a running agent,
    and yanking it mid-write is how two processes end up interleaved in one
    binary, which is the exact failure locks exist to prevent.
    """
    path = str((payload or {}).get("path") or "").strip()
    if not path:
        raise api.bad_request("which lock? send {path}")
    seat = str((payload or {}).get("seat") or "").strip()
    try:
        out = _assets.release(root(), path, seat or "", owner=str(
            (payload or {}).get("owner") or ""))
    except LookupError as exc:
        raise api.not_found(str(exc))
    except (PermissionError, ValueError) as exc:
        # "you are not the holder" is a 400 with the holder's name in it, not a
        # 500 — it is an answer, and the reader's next move depends on it.
        raise api.bad_request(str(exc))
    return api.ok(out)
