"""How long a run may take, and how many may run at once.

Builders Gate does not meter money and does not hold a budget. It used to: a
ledger summed every paid call, a ``spend_budget`` row carried dollar ceilings,
and a reservation gate refused work before it started. All of it is gone. The
only spending figure this product will ever show is the one the provider
reports for the user's own key — see ``provider_status`` and ``kie_status``,
which read the account balance rather than a number this database invented.

What survives is the pair of limits that were never about money: an agent that
runs forever, and a fan-out that spawns more processes than the machine can
hold. Those are operational, they stop real runaway work, and they are the
reason the ``run_limits`` row still exists (migration 0045).
"""
from __future__ import annotations

import os

from ..store import db


def limits(root: str | os.PathLike[str]) -> dict:
    """The single limits row. ``{}`` if the project has none."""
    try:
        row = db.connect(root).execute(
            "SELECT * FROM run_limits WHERE id = 1").fetchone()
    except Exception:
        return {}
    return dict(row) if row else {}


def set_limits(root: str | os.PathLike[str], **fields) -> dict:
    """Update the limits. Unknown keys are ignored so a UI can PATCH loosely."""
    allowed = {"max_runtime_s", "max_concurrent"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if sets:
        assignments = ", ".join(f"{k} = ?" for k in sets)
        with db.tx(root) as conn:
            conn.execute(
                f"UPDATE run_limits SET {assignments}, "
                "updated_at = datetime('now') WHERE id = 1",
                list(sets.values()))
    return limits(root)


def concurrency_cap(root: str | os.PathLike[str]) -> int:
    """How many agents may run at once. 0 means uncapped."""
    try:
        return int(limits(root).get("max_concurrent") or 0)
    except Exception:
        return 0


def runtime_ceiling(root: str | os.PathLike[str], item: dict) -> int:
    """Wall-clock ceiling for one run in seconds. 0 means uncapped.

    An item's own ``max_runtime_s`` wins; otherwise the project default. This
    is the ceiling that actually stopped runaway runs in every benchmark — a
    run that is not progressing burns time whether or not it costs anything.
    """
    override = (item or {}).get("max_runtime_s")
    if override:
        return int(override)
    return int(limits(root).get("max_runtime_s") or 0)
