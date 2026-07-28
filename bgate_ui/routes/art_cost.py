"""What the art actually cost — the numbers the iteration lab shows.

The price table and the ledger both existed; nothing ever put a dollar sign in
front of a human. This is the read side: a per-logical-asset total the lab
header renders, plus the adapter's own price table so the UI can estimate a
batch BEFORE it is bought (never a price invented in JavaScript).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

from bgate_adapters import imagegen
from bgate_core import db, spend
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()


@router.get("/api/art/cost")
def art_cost(logical_name: Optional[str] = None) -> dict:
    """Spend for one logical asset, or the whole per-asset map.

    ``prices`` is imagegen.IMAGE_PRICE_USD verbatim — the UI multiplies it by a
    candidate count for the live "~$X.XX" estimate, so the estimate and the
    charge can never drift apart.
    """
    project = root()
    payload = {
        "prices": dict(imagegen.IMAGE_PRICE_USD),
        "default_quality": "medium",
    }
    if logical_name:
        payload["logical_name"] = logical_name
        payload["usd"] = spend.for_logical(project, logical_name)
        return api.ok(payload)

    by_logical: dict[str, float] = {}
    try:
        rows = db.connect(project).execute(
            "SELECT logical_name, COALESCE(SUM(usd), 0) AS usd "
            "FROM spend_event WHERE kind = 'image' AND logical_name != '' "
            "GROUP BY logical_name").fetchall()
        by_logical = {r["logical_name"]: round(r["usd"], 4) for r in rows}
    except Exception:
        # A project older than migration 0011 has no ledger — an empty map is
        # the honest answer, and beats blanking the panel.
        by_logical = {}
    payload["by_logical"] = by_logical
    payload["totals"] = spend.totals(project)
    return api.ok(payload)
