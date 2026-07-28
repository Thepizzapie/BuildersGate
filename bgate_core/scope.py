"""The cut line, with teeth.

The bible could already describe scope — tiers ranked above and below a
``cut_line`` section — but nothing in the product ever read that description
before doing work, so the line was decoration and the fleet gold-plated exactly
the way the bible said not to. This module is the enforcement: one deterministic
verdict, shaped like :mod:`bgate_core.spend`'s, that the queue and the dispatcher
call before work is accepted or a process is spawned.

Rank is priority: LOWER rank is higher priority, the cut line sits at some rank,
and a tier at or below it is not being built. Three honest outcomes:

  allowed + clean    the tier is above the line, or no line has been drawn yet
  allowed + flagged  the work points at no tier — uncheckable, so it is loud
  refused            the tier is at or below the line, or the pointer is junk

An untiered item is deliberately not refused. Refusing it would make the first
cut line anyone draws reject the entire existing queue, and the predictable fix
would be to turn the gate off — which is how a gate stops gating.
"""
from __future__ import annotations

import os

from . import activity, bible, db
from .util import rows


class OutOfScope(ValueError):
    """Raised by :func:`enforce`. A ValueError so existing core call sites that
    already map ValueError to a 400 need no new except clause."""

    def __init__(self, verdict: dict) -> None:
        super().__init__(verdict["reason"])
        self.verdict = verdict


def cut_line(root: str | os.PathLike[str]) -> dict | None:
    """The section that draws the line, or None if the call hasn't been made."""
    return bible.cut_line(root)


def tiers(root: str | os.PathLike[str]) -> list[dict]:
    """Every scope tier in rank order, each told whether it is under the line."""
    line = cut_line(root)
    out = []
    for tier in bible.list_sections(root, "scope_tier"):
        tier = dict(tier)
        tier["below_cut"] = bool(line is not None and tier["rank"] >= line["rank"])
        out.append(tier)
    return out


def tier_of(root: str | os.PathLike[str], item: dict | int) -> dict | None:
    """The scope tier a work item is filed under, or None if it is untiered."""
    if isinstance(item, int):
        row = db.connect(root).execute(
            "SELECT scope_tier_id FROM work_item WHERE id = ?", (item,)).fetchone()
        if row is None:
            raise LookupError(f"no work item {item}")
        tier_id = row["scope_tier_id"]
    else:
        tier_id = item.get("scope_tier_id")
    if tier_id is None:
        return None
    try:
        return bible.get(root, int(tier_id))
    except LookupError:
        return None


def check(root: str | os.PathLike[str], scope_tier_id: int | None) -> dict:
    """Is work under this tier allowed to proceed? See the module docstring.

    Returns ``{allowed, flagged, code, reason, tier, cut_line, cut_line_rank}``.
    ``code`` is stable and switchable; ``reason`` is a sentence worth showing.
    """
    line = cut_line(root)
    line_rank = None if line is None else int(line["rank"])
    base = {"tier": None, "cut_line": line, "cut_line_rank": line_rank}

    if scope_tier_id is None:
        if line is None:
            return {**base, "allowed": True, "flagged": False, "code": "no_cut_line",
                    "reason": "no cut line has been drawn — everything is in scope "
                              "until the team makes the scope call"}
        return {**base, "allowed": True, "flagged": True, "code": "untiered",
                "reason": f"not filed under a scope tier, so it cannot be checked "
                          f"against the cut line ({line['title']!r}) — file it or "
                          f"accept it unscoped"}

    try:
        tier = bible.get(root, int(scope_tier_id))
    except (LookupError, TypeError, ValueError):
        return {**base, "allowed": False, "flagged": True, "code": "unknown_tier",
                "reason": f"scope tier {scope_tier_id!r} is not a section of this "
                          "bible — refusing rather than guessing"}
    base = {**base, "tier": tier}

    if tier["kind"] != "scope_tier":
        return {**base, "allowed": False, "flagged": True, "code": "not_a_tier",
                "reason": f"section {tier['id']} ({tier['title']!r}) is a "
                          f"{tier['kind']}, not a scope tier"}
    if line is None:
        return {**base, "allowed": True, "flagged": False, "code": "no_cut_line",
                "reason": "no cut line has been drawn — everything is in scope "
                          "until the team makes the scope call"}
    if int(tier["rank"]) >= line_rank:
        return {**base, "allowed": False, "flagged": True, "code": "below_cut_line",
                "reason": f"{tier['title']!r} is below the cut line "
                          f"({line['title']!r}) — it is explicitly not being built"}
    return {**base, "allowed": True, "flagged": False, "code": "in_scope",
            "reason": f"{tier['title']!r} is above the cut line ({line['title']!r})"}


def enforce(root: str | os.PathLike[str], scope_tier_id: int | None) -> dict:
    """:func:`check`, but a refusal raises. The one-line form for call sites that
    should simply not proceed — ``scope.enforce(root, scope_tier_id)``."""
    verdict = check(root, scope_tier_id)
    if not verdict["allowed"]:
        raise OutOfScope(verdict)
    return verdict


def assign(root: str | os.PathLike[str], item_id: int,
           scope_tier_id: int | None) -> dict:
    """File a work item under a tier (or None to unfile it).

    Assignment is itself a gate: you cannot park work under a tier that is
    already cut, because that is just a rename of "build it anyway".
    """
    conn = db.connect(root)
    if conn.execute("SELECT 1 FROM work_item WHERE id = ?", (item_id,)).fetchone() is None:
        raise LookupError(f"no work item {item_id}")
    if scope_tier_id is not None:
        enforce(root, int(scope_tier_id))
    with db.tx(root) as conn:
        conn.execute("UPDATE work_item SET scope_tier_id = ?, "
                     "updated_at = datetime('now') WHERE id = ?",
                     (None if scope_tier_id is None else int(scope_tier_id), item_id))
    tier = tier_of(root, item_id)
    activity.log(root, "scope",
                 f"item {item_id} filed under "
                 f"{tier['title'][:60] if tier else 'no tier'}", ref=str(item_id))
    return {"item_id": item_id, "tier": tier,
            "check": check(root, None if tier is None else tier["id"])}


def stranded(root: str | os.PathLike[str]) -> list[dict]:
    """Open work sitting at or below the line — what moving the cut line just
    orphaned. The line is retroactive or it is theatre."""
    line = cut_line(root)
    if line is None:
        return []
    return rows(db.connect(root).execute(
        """
        SELECT w.id, w.seat, w.title, w.status, w.scope_tier_id,
               b.title AS tier_title, b.rank AS tier_rank
          FROM work_item w JOIN bible_section b ON b.id = w.scope_tier_id
         WHERE w.status IN ('queued', 'dispatched')
           AND b.kind = 'scope_tier' AND b.rank >= ?
         ORDER BY b.rank, w.id
        """,
        (int(line["rank"]),)))


def overview(root: str | os.PathLike[str]) -> dict:
    """Everything the scope panel needs: the tiers, the line, what is under it,
    what is untiered, and the work the current line already invalidates."""
    all_tiers = tiers(root)
    counts = {int(r["scope_tier_id"]): dict(r) for r in db.connect(root).execute(
        """
        SELECT scope_tier_id,
               COUNT(*) AS total,
               SUM(status IN ('queued', 'dispatched')) AS open
          FROM work_item WHERE scope_tier_id IS NOT NULL
         GROUP BY scope_tier_id
        """).fetchall()}
    for tier in all_tiers:
        got = counts.get(tier["id"], {})
        tier["items"] = {"total": int(got.get("total") or 0),
                         "open": int(got.get("open") or 0)}
    untiered = db.connect(root).execute(
        "SELECT COUNT(*) AS n FROM work_item WHERE scope_tier_id IS NULL "
        "AND status IN ('queued', 'dispatched')").fetchone()
    line = cut_line(root)
    return {
        "cut_line": line,
        "cut_line_rank": None if line is None else int(line["rank"]),
        "tiers": all_tiers,
        "in_scope": [t for t in all_tiers if not t["below_cut"]],
        "cut": [t for t in all_tiers if t["below_cut"]],
        "untiered_open": int(untiered["n"] if untiered else 0),
        "stranded": stranded(root),
        "enforced": True,
    }
