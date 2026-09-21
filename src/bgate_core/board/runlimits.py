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


# MEASURED 2026-09-21 (EXIT 67, item 29): the shipped defaults of 3600 s / no
# turn cap killed chained runs mid-work — a queue_claim_next chain that had
# claimed three or four items in a row was still on its first item's clock and
# got killed with the later items' work half-done. The human raised these by
# hand to 9800 s / 800 turns and it held; that is now the shipped default
# rather than a value only known from a support chat.
DEFAULT_RUNTIME_CEILING_S = 9800
DEFAULT_MAX_TURNS = 800


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
    return int(limits(root).get("max_runtime_s") or DEFAULT_RUNTIME_CEILING_S)


def max_turns(root: str | os.PathLike[str], item: dict) -> int:
    """Turn-count ceiling for one run. 0 means uncapped.

    Mirrors ``runtime_ceiling``: an item's own ``max_turns`` wins, otherwise
    the project default. See ``DEFAULT_MAX_TURNS`` for why 800.
    """
    override = (item or {}).get("max_turns")
    if override:
        return int(override)
    stored = limits(root).get("max_turns")
    return int(stored) if stored else DEFAULT_MAX_TURNS
