"""The quality ladder, published to the UI.

The Studio lets a user say WHAT they are making and HOW GOOD it needs to be —
never a model name. That only works if the browser can read the same ladder the
server resolves against, so this endpoint is the single source of truth for the
tier picker: the rungs, what each resolves to, what it costs, and which rungs
are duplicates.

That last one matters. `animation` genuinely tops out at krea-2-large today —
nothing available lays out a multi-frame sheet better — so "hero" resolves to
the same model as "standard". A picker that offered it as an upgrade would be
selling something that does not exist, so `flat` names those rungs and the UI
greys them.
"""
from __future__ import annotations

from fastapi import APIRouter

from bgate_core import tiers as _tiers
from bgate_ui import api

router = APIRouter()


@router.get("/api/tiers")
def tier_catalogue() -> dict:
    """Every asset kind, its ladder, and the rungs that are not real upgrades."""
    ladders: dict[str, list[dict]] = {}
    flat: dict[str, list[str]] = {}
    for kind in _tiers.kinds():
        try:
            ladders[kind] = _tiers.ladder(kind)
            flat[kind] = _tiers.flat_rungs(kind)
        except _tiers.NoSuchTier as exc:
            # A misconfigured ladder must be visible, not silently absent —
            # resolve() refuses models that cannot do the job, and the UI needs
            # to say so rather than render an empty picker.
            ladders[kind] = []
            flat[kind] = []
            ladders[f"{kind}__error"] = str(exc)  # type: ignore[assignment]
    return api.ok({
        "tiers": list(_tiers.TIERS),
        "default_tier": _tiers.DEFAULT_TIER,
        "kinds": _tiers.kinds(),
        "ladders": ladders,
        "flat": flat,
    })


@router.get("/api/tiers/{kind}")
def tier_for_kind(kind: str, tier: str = "") -> dict:
    """One rung — what a node will actually call, and what it will cost."""
    try:
        return api.ok(_tiers.resolve(kind, tier or _tiers.DEFAULT_TIER))
    except _tiers.NoSuchTier as exc:
        raise api.bad_request(str(exc), kind=kind, tier=tier)
