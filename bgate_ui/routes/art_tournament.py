"""Pairwise art-tournament endpoints.

art_qa.py dispatches an independent reviewer to check candidates against
their reference (a drift check). This dispatches a DIFFERENT independent
review: given a set of candidates for one target, judge them against EACH
OTHER, pairwise, and let a rating fall out of the match log. See
bgate_core/art_tournament.py and docs/visual-taste-research.md for why
pairwise rather than an absolute score.

Kept in its own module so it auto-registers (routes/__init__.py) without an
edit to app.py — same convention art_qa.py uses.
"""
from __future__ import annotations

import itertools
import random
from typing import Optional

from fastapi import APIRouter, HTTPException

from bgate_core import art_tournament as _art_tournament
from bgate_core import artifacts as _artifacts
from bgate_core import queue as _queue
from bgate_ui import dispatch as _dispatch
from bgate_ui.deps import root

router = APIRouter()

# All-pairs round-robin is exact and simple, and art-generation batches are
# small (a handful of variants per request) — this cap is a sanity backstop,
# not a real limit anyone should hit: 8 candidates is already 28 matches for
# one reviewer session to work through.
MAX_CANDIDATES = 8


def _gather_candidates(r, logical_name: str) -> list[dict]:
    return _artifacts.list_revisions(
        r, logical_name=logical_name, status="candidate", limit=MAX_CANDIDATES)


def _reviewer_brief(logical_name: str, matches: list[dict],
                    candidates_by_id: dict[int, dict]) -> str:
    lines: list[str] = []
    for m in matches:
        a_id, b_id = m["candidate_a_id"], m["candidate_b_id"]
        first_id, second_id = (a_id, b_id) if m["shown_first"] == "a" else (b_id, a_id)
        first, second = candidates_by_id[first_id], candidates_by_id[second_id]
        lines.append(
            f"- match_id={m['id']}\n"
            f"    FIRST  (artifact_id={first_id}): {first['path']}\n"
            f"    SECOND (artifact_id={second_id}): {second['path']}"
        )
    listing = "\n".join(lines) if lines else "(no matches were opened)"
    return (
        "You are an INDEPENDENT art-quality reviewer running a PAIRWISE "
        "tournament — you did NOT make these images. For each match below, "
        "open BOTH images with the Read tool and decide which one is "
        "better: stronger silhouette, cleaner linework, better palette "
        "harmony, more appealing pose. This is a comparative judgement, not "
        "a checklist — trust your eye on which one you would ship.\n\n"
        f"REVIEW TARGET: {logical_name}\n\n"
        "MATCHES (FIRST/SECOND is the order to look at them in, not a hint "
        "about which is better — it was randomised before you saw it):\n"
        f"{listing}\n\n"
        "For EACH match above, in order:\n"
        "1. Read both images.\n"
        "2. Call the MCP tool `art_tournament_verdict(match_id=<the id>, "
        "winner_artifact_id=<the artifact_id of whichever image you preferred>, "
        "reasons='<one line, specific>')`. PICK A WINNER even on a close call — "
        "do not skip a match.\n\n"
        "When every match has a verdict, call `queue_complete` with a one-"
        "paragraph summary of how the field shook out."
    )


@router.post("/api/art-tournament/start")
def start_tournament(payload: Optional[dict] = None) -> dict:
    """Open a round-robin of pairwise matches over a target's candidates and
    dispatch an independent reviewer to judge them.

    body: {logical_name: str, tournament_ref?: str}. Every unordered pair of
    current candidates gets one match, with shown_first coin-flipped per
    match to cancel position bias (see MT-Bench / MLLM-as-Judge — this is a
    documented failure mode of exactly this judging pattern, not a
    theoretical one).
    """
    payload = payload or {}
    logical_name = (payload.get("logical_name") or "").strip()
    if not logical_name:
        raise HTTPException(400, "logical_name is required")
    tournament_ref = (payload.get("tournament_ref") or "").strip()
    r = root()

    candidates = _gather_candidates(r, logical_name)
    if len(candidates) < 2:
        raise HTTPException(
            404, f"need at least 2 candidates to run a tournament for "
                 f"{logical_name!r}, found {len(candidates)}")
    if len(candidates) > MAX_CANDIDATES:
        raise HTTPException(
            400, f"{len(candidates)} candidates exceeds the "
                 f"{MAX_CANDIDATES}-candidate round-robin cap for one "
                 "tournament — approve or reject some first")

    candidates_by_id = {c["id"]: c for c in candidates}
    matches = []
    for a, b in itertools.combinations(candidates, 2):
        shown_first = random.choice(("a", "b"))
        matches.append(_art_tournament.record_match(
            r, logical_name=logical_name, candidate_a_id=a["id"],
            candidate_b_id=b["id"], shown_first=shown_first,
            tournament_ref=tournament_ref))

    try:
        item = _queue.add(
            r, "qa",
            title=f"Art tournament: pairwise ranking of {logical_name}",
            brief=_reviewer_brief(logical_name, matches, candidates_by_id),
            source="art-tournament", source_ref=logical_name)
    except (ValueError, LookupError) as exc:
        raise HTTPException(400, str(exc))

    dispatched = _dispatch.dispatch(str(r), item["id"])
    return {
        "ok": bool(dispatched.get("ok")),
        "review_item_id": item["id"],
        "dispatched": dispatched.get("pid"),
        "candidate_count": len(candidates),
        "match_count": len(matches),
        "match_ids": [m["id"] for m in matches],
        "error": dispatched.get("error"),
    }


@router.get("/api/art-tournament/standings")
def tournament_standings(logical_name: str,
                         tournament_ref: Optional[str] = None) -> dict:
    r = root()
    return {"ok": True, **_art_tournament.standings(
        r, logical_name, tournament_ref=tournament_ref)}
