"""Pairwise art-tournament endpoints.

art_qa.py dispatches an independent reviewer to check candidates against
their reference (a drift check). This dispatches a DIFFERENT independent
review: given a set of candidates for one target, judge them against EACH
OTHER, pairwise, and let a rating fall out of the match log. See
bgate_core/art/art_tournament.py and docs/visual-taste-research.md for why
pairwise rather than an absolute score.

Kept in its own module so it auto-registers (routes/__init__.py) without an
edit to app.py — same convention art_qa.py uses.
"""
from __future__ import annotations

import itertools
import random
import time
from typing import Optional

from fastapi import APIRouter, HTTPException

from bgate_core.art import art_tournament as _art_tournament
from bgate_core.store import artifacts as _artifacts
from bgate_core.store import db as _db
from bgate_core.board import queue as _queue
from bgate_ui.agents import dispatch as _dispatch
from bgate_ui.deps import root

router = APIRouter()

# All-pairs round-robin is exact and simple, and art-generation batches are
# small (a handful of variants per request) — this cap is a sanity backstop,
# not a real limit anyone should hit: 8 candidates is already 28 matches for
# one reviewer session to work through.
MAX_CANDIDATES = 8


def _gather_candidates(r, logical_name: str) -> list[dict]:
    """Every candidate, NOT the first MAX_CANDIDATES of them.

    Asking the query for MAX_CANDIDATES rows made the caller's "too many
    candidates" check unreachable — `len(candidates) > MAX_CANDIDATES` can
    never be true of a list the database already truncated. Twelve candidates
    silently became a round-robin over an arbitrary eight of them, reported as
    a complete tournament. Fetch past the cap so the refusal can see it.
    """
    return _artifacts.list_revisions(
        r, logical_name=logical_name, status="candidate",
        limit=MAX_CANDIDATES + 1)


def _reviewer_brief(logical_name: str, matches: list[dict],
                    candidates_by_id: dict[int, dict]) -> str:
    """The reviewer's instructions: every image read ONCE, every verdict in ONE call.

    It used to say "open BOTH images" per match, so 8 candidates cost 56 image
    reads and 28 tool calls for what is 8 images and one decision table. The
    images stay in the reviewer's context once read; the per-match FIRST/SECOND
    order is still coin-flipped, and the candidate list itself is shuffled, so
    position bias is cancelled in the order the reviewer is asked to compare.
    """
    ids = list(candidates_by_id)
    random.shuffle(ids)
    label = {cid: f"#{cid}" for cid in ids}
    images = "\n".join(f"- {label[cid]} (artifact_id={cid}): "
                       f"{candidates_by_id[cid]['path']}" for cid in ids)
    lines: list[str] = []
    for m in matches:
        a_id, b_id = m["candidate_a_id"], m["candidate_b_id"]
        first, second = (a_id, b_id) if m["shown_first"] == "a" else (b_id, a_id)
        lines.append(f"- match_id={m['id']}: FIRST {label[first]} vs "
                     f"SECOND {label[second]}")
    listing = "\n".join(lines) if lines else "(no matches were opened)"
    return (
        "You are an INDEPENDENT art-quality reviewer running a PAIRWISE "
        "tournament - you did NOT make these images. Judge which image of each "
        "pair is better: stronger silhouette, cleaner linework, better palette "
        "harmony, more appealing pose. This is a comparative judgement, not a "
        "checklist - trust your eye on which one you would ship.\n\n"
        f"REVIEW TARGET: {logical_name}\n\n"
        "CANDIDATES - open each of these ONCE with the Read tool, in this "
        "order, before judging anything:\n"
        f"{images}\n\n"
        "MATCHES (FIRST/SECOND is the order to compare them in, not a hint "
        "about which is better - it was randomised):\n"
        f"{listing}\n\n"
        "Then call the MCP tool `art_tournament_verdict` ONCE with every "
        "match: `art_tournament_verdict(verdicts=[{\"match_id\": <id>, "
        "\"winner_artifact_id\": <the artifact_id you preferred>, "
        "\"reasons\": \"<one line, specific>\"}, ...])`. PICK A WINNER in "
        "every match, even a close one - do not skip any.\n\n"
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
    named_ref = (payload.get("tournament_ref") or "").strip()
    # A FRESH REF PER START. Left at "", a second tournament over a target
    # pooled into the first one's standings, ranking candidates that had since
    # been approved or rejected beside the new ones.
    tournament_ref = named_ref or f"t{int(time.time() * 1000)}"
    r = root()

    candidates = _gather_candidates(r, logical_name)
    if len(candidates) < 2:
        # NAME THE REAL CAUSE. A project with art.auto_approve on never leaves
        # a revision at status "candidate" — register() promotes it on the spot
        # — so this endpoint 404s on every target, and "found 0 candidates"
        # sends the reader looking for missing renders that are all present and
        # approved.
        why = ""
        if _artifacts._auto_approve(r):
            why = (" — art.auto_approve is ON for this project, so generated "
                   "revisions are approved at registration and never sit as "
                   "candidates. A tournament ranks candidates; turn the "
                   "setting off for targets you want reviewed this way.")
        raise HTTPException(
            404, f"need at least 2 candidates to run a tournament for "
                 f"{logical_name!r}, found {len(candidates)}{why}")
    if len(candidates) > MAX_CANDIDATES:
        raise HTTPException(
            400, f"{len(candidates)} candidates exceeds the "
                 f"{MAX_CANDIDATES}-candidate round-robin cap for one "
                 "tournament — approve or reject some first")

    # ONE OPEN TOURNAMENT AT A TIME PER TARGET. Nothing deduplicated these, so
    # a second POST — a retry after a timeout, a double-click — opened the whole
    # round-robin again over the same pairs. standings() pools every match for a
    # logical_name, so the duplicate did not sit harmlessly beside the first: it
    # weighted the same comparison twice in the Elo.
    open_already = [m for m in _art_tournament.list_matches(
        r, logical_name=logical_name, tournament_ref=named_ref or None,
        limit=None) if m.get("winner_id") is None]
    if open_already:
        raise HTTPException(
            409, f"{len(open_already)} match(es) for {logical_name!r} are still "
                 "open — judge or discard those (POST "
                 "/api/art-tournament/discard) before opening another "
                 "tournament over the same candidates, or pass a distinct "
                 "tournament_ref")

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
        # The matches are already committed at this point. Roll them back rather
        # than orphaning a round-robin that no path could reach or clear.
        _art_tournament.discard_matches(r, [m["id"] for m in matches])
        raise HTTPException(400, str(exc))

    dispatched = _dispatch.dispatch(str(r), item["id"])
    return {
        "ok": bool(dispatched.get("ok")),
        "review_item_id": item["id"],
        "dispatched": dispatched.get("pid"),
        "tournament_ref": tournament_ref,
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


def _latest_ref(r, logical_name: str) -> Optional[str]:
    last = _art_tournament.list_matches(r, logical_name=logical_name,
                                        tournament_ref=None, limit=None)
    return last[-1].get("tournament_ref") if last else None


@router.get("/api/art-tournament/status")
def tournament_status(logical_name: str) -> dict:
    """The newest tournament for a target: standings with each candidate's
    path and current status, open matches, and the reviewer item's state.
    {ok, running: false} when the target has never been tournamented."""
    r = root()
    ref = _latest_ref(r, logical_name)
    if ref is None:
        return {"ok": True, "logical_name": logical_name, "tournament_ref": None,
                "standings": [], "decided_matches": 0, "pending_matches": 0,
                "review": None}
    out = _art_tournament.standings(r, logical_name, tournament_ref=ref)
    for row in out["standings"]:
        try:
            art = _artifacts.get(r, row["artifact_id"])
        except LookupError:
            continue
        row.update(path=art.get("path"), revision=art.get("revision"),
                   status=art.get("status"))
    row = _db.connect(r).execute(
        "SELECT id, status FROM work_item WHERE source = 'art-tournament' "
        "AND source_ref = ? ORDER BY id DESC LIMIT 1", (logical_name,)).fetchone()
    review = {"id": row["id"], "status": row["status"]} if row else None
    return {"ok": True, "tournament_ref": ref, "review": review, **out}


@router.post("/api/art-tournament/discard")
def tournament_discard(payload: Optional[dict] = None) -> dict:
    """Drop a target's UNDECIDED matches, so a tournament whose reviewer died
    stops blocking the next one. Decided matches are kept."""
    payload = payload or {}
    logical_name = (payload.get("logical_name") or "").strip()
    if not logical_name:
        raise HTTPException(400, "logical_name is required")
    r = root()
    open_ids = [m["id"] for m in _art_tournament.list_matches(
        r, logical_name=logical_name, tournament_ref=None, limit=None)
        if m.get("winner_id") is None]
    return {"ok": True, "discarded": _art_tournament.discard_matches(r, open_ids)}
