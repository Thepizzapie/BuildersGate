"""Money. The one thing in this product that can run away unattended.

Agent sessions and image generation both spend real money with no natural stop.
Before this module ``total_cost_usd`` was parsed off the final result event and
returned in an ephemeral JSON response — never persisted, never summed — so
nothing could answer "worst case, what does tonight cost".

Two halves: a ledger (:func:`record`) that every paying call writes to, and a
budget (:func:`check`) the dispatcher consults *before* spawning. The budget is
a refusal, not a warning.
"""
from __future__ import annotations

import os
from typing import Optional

from bgate_core import db

KINDS = ("agent", "image", "audio", "other")


def budget(root: str | os.PathLike[str]) -> dict:
    row = db.connect(root).execute("SELECT * FROM spend_budget WHERE id = 1").fetchone()
    return dict(row) if row else {}


def set_budget(root: str | os.PathLike[str], **fields) -> dict:
    """Update the ceilings. Unknown keys are ignored so a UI can PATCH loosely."""
    allowed = {"per_item_usd", "per_day_usd", "per_project_usd",
               "max_runtime_s", "max_concurrent", "enforced"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if sets:
        assignments = ", ".join(f"{k} = ?" for k in sets)
        with db.tx(root) as conn:
            conn.execute(
                f"UPDATE spend_budget SET {assignments}, "
                "updated_at = datetime('now') WHERE id = 1",
                list(sets.values()))
    return budget(root)


def record(root: str | os.PathLike[str], usd: float, *, kind: str = "agent",
           work_item_id: Optional[int] = None, logical_name: str = "",
           detail: str = "") -> None:
    """Append a spend event. Never raises — losing the ledger must not lose the
    work that produced it."""
    if not usd or usd <= 0:
        return
    if kind not in KINDS:
        kind = "other"
    try:
        with db.tx(root) as conn:
            conn.execute(
                "INSERT INTO spend_event (kind, work_item_id, logical_name, usd, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (kind, work_item_id, logical_name, float(usd), detail[:500]))
            if work_item_id:
                conn.execute(
                    "UPDATE work_item SET total_cost_usd = total_cost_usd + ? "
                    "WHERE id = ?", (float(usd), work_item_id))
    except Exception:
        pass


def totals(root: str | os.PathLike[str]) -> dict:
    """What has been spent, at the three granularities the budget cares about."""
    conn = db.connect(root)
    row = conn.execute("""
        SELECT
          COALESCE(SUM(usd), 0)                                        AS project_usd,
          COALESCE(SUM(CASE WHEN created_at >= date('now')
                            THEN usd END), 0)                          AS today_usd,
          COUNT(*)                                                     AS events
        FROM spend_event
    """).fetchone()
    out = dict(row)
    by_kind = conn.execute(
        "SELECT kind, COALESCE(SUM(usd), 0) AS usd FROM spend_event GROUP BY kind"
    ).fetchall()
    out["by_kind"] = {r["kind"]: round(r["usd"], 4) for r in by_kind}
    out["project_usd"] = round(out["project_usd"], 4)
    out["today_usd"] = round(out["today_usd"], 4)
    out["budget"] = budget(root)
    return out


def for_logical(root: str | os.PathLike[str], logical_name: str) -> float:
    """Total spent on one logical asset — the number the art lab header shows."""
    row = db.connect(root).execute(
        "SELECT COALESCE(SUM(usd), 0) AS usd FROM spend_event WHERE logical_name = ?",
        (logical_name,)).fetchone()
    return round(row["usd"], 4) if row else 0.0


def check(root: str | os.PathLike[str], *, projected_usd: float = 0.0) -> dict:
    """Would spending ``projected_usd`` now breach a ceiling?

    Returns ``{allowed, reason, ...}``. Callers refuse on ``allowed=False`` —
    this is the gate that makes a 20-item overnight fan-out bounded.
    """
    b = budget(root)
    if not b or not b.get("enforced"):
        return {"allowed": True, "reason": "", "enforced": False}
    t = totals(root)
    day, project = t["today_usd"], t["project_usd"]

    if b["per_day_usd"] and day + projected_usd > b["per_day_usd"]:
        return {"allowed": False, "enforced": True,
                "reason": f"daily budget reached — ${day:.2f} spent today of "
                          f"${b['per_day_usd']:.2f}",
                "scope": "day", "spent": day, "ceiling": b["per_day_usd"]}
    if b["per_project_usd"] and project + projected_usd > b["per_project_usd"]:
        return {"allowed": False, "enforced": True,
                "reason": f"project budget reached — ${project:.2f} spent of "
                          f"${b['per_project_usd']:.2f}",
                "scope": "project", "spent": project,
                "ceiling": b["per_project_usd"]}
    return {"allowed": True, "reason": "", "enforced": True,
            "today_usd": day, "project_usd": project}


def item_ceiling(root: str | os.PathLike[str], item: dict) -> float:
    """The per-run cost ceiling for one item: its own override, else the default."""
    override = (item or {}).get("max_cost_usd")
    if override:
        return float(override)
    return float(budget(root).get("per_item_usd") or 0)


def runtime_ceiling(root: str | os.PathLike[str], item: dict) -> int:
    override = (item or {}).get("max_runtime_s")
    if override:
        return int(override)
    return int(budget(root).get("max_runtime_s") or 0)
