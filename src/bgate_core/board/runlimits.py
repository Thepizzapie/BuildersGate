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
    allowed = {"max_runtime_s", "max_concurrent",
               "small_runtime_s", "small_turns",
               "medium_runtime_s", "medium_turns",
               "large_runtime_s", "large_turns"}
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


# GRIPE 40 (EXIT 67 postmortem, 2026-09-21): hours were burned on ONE item
# because every item got the same ceiling regardless of how big the ask was.
# `size` is a column on work_item now (migration 0052); these are the
# per-size defaults, checked against the retrospective's own examples
# (a "small" task is a single focused edit — 15 minutes and 60 turns is
# already generous for one). `large` intentionally falls back to the
# project's existing default (max_runtime_s / dispatch.max_turns) rather than
# inventing a bigger number — it is what every item already got before this
# gripe, unchanged.
SIZES = ("small", "medium", "large")
DEFAULT_SIZE = "medium"
SIZE_LIMITS = {
    "small": (900, 60),
    "medium": (3600, 300),
    "large": (0, 0),          # 0 => "no size-specific override, use default"
}


def size_of(item: dict) -> str:
    """The item's size, defaulted and normalized. Never raises."""
    size = str((item or {}).get("size") or DEFAULT_SIZE).strip().lower()
    return size if size in SIZES else DEFAULT_SIZE


def ceiling_for_size(root: str | os.PathLike[str], size: str) -> tuple[int, int]:
    """``(runtime_s, turn_cap)`` for a size, project override first.

    A project can move the ceiling per size (Settings, or set_limits) the
    same way it already could move max_runtime_s; 0/None on either side means
    "no override for this size" and the SIZE_LIMITS default applies. Unknown
    sizes fall back to DEFAULT_SIZE rather than raising, because a caller
    building an item from a stale or hand-typed size string should get a
    sane ceiling, not a crash mid-dispatch.
    """
    size = size if size in SIZES else DEFAULT_SIZE
    row = limits(root)
    def_runtime, def_turns = SIZE_LIMITS[size]
    runtime_s = int(row.get(f"{size}_runtime_s") or def_runtime or 0)
    turns = int(row.get(f"{size}_turns") or def_turns or 0)
    return runtime_s, turns


def runtime_ceiling_for_item(root: str | os.PathLike[str], item: dict) -> int:
    """Wall-clock ceiling that accounts for the item's `size` as well as its
    own ``max_runtime_s`` override and the project default (in that order).

    ``large`` (or an item with no size-specific ceiling) falls through to the
    unchanged ``runtime_ceiling`` behaviour — this function only narrows the
    ceiling for small/medium, it never widens what a project already set.
    """
    override = (item or {}).get("max_runtime_s")
    if override:
        return int(override)
    size = size_of(item)
    runtime_s, _turns = ceiling_for_size(root, size)
    if runtime_s:
        return runtime_s
    return int(limits(root).get("max_runtime_s") or 0)


def turn_cap_for_item(root: str | os.PathLike[str], item: dict) -> int:
    """Turn cap that accounts for the item's `size`. 0 means uncapped (the
    project's own dispatch.max_turns setting, read by the dispatcher, wins
    when this returns 0)."""
    size = size_of(item or {})
    _runtime_s, turns = ceiling_for_size(root, size)
    return turns


def budget_fraction(elapsed_s: float, ceiling_s: int) -> float:
    """How far through its budget a run is, in [0, +inf). 0.0 when the
    ceiling is 0 (uncapped) — there is no fraction of "no limit" to report."""
    if not ceiling_s:
        return 0.0
    return max(0.0, float(elapsed_s)) / float(ceiling_s)


# The two checkpoints the "LAND WHAT YOU HAVE" steer fires at, and the text
# posted at each. Kept here (not in dispatch.py) so the thresholds and the
# ceiling math live beside each other — a change to one is a change most
# likely to need the other.
LAND_WHAT_YOU_HAVE_CHECKPOINTS = (0.60, 0.85)


def land_what_you_have_text(elapsed_s: float, ceiling_s: int, pct: float) -> str:
    """The steer text for a budget checkpoint. Pure so it is testable without
    a live dispatch loop."""
    remaining_s = max(0, int(ceiling_s - elapsed_s))
    remaining_m = remaining_s // 60
    return (
        f"BUDGET CHECK: {int(pct * 100)}% of this item's time budget is gone "
        f"(~{remaining_m} min left). Land what you have — a verified partial "
        "with an honest next_approach beats polish you do not have budget "
        "left to finish. If the named check already passes, complete now."
    )
