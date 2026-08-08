"""Pairwise art-quality judging, aggregated to a rating.

art_qa_verdict (see artifacts.qa_verdict) answers "is this on-model" — a
drift check against a reference. This module answers a different question,
"which of these candidates is better", and answers it the way the research
behind this project (docs/visual-taste-research.md) found the VLM-as-judge
literature is unusually consistent about: a judge asked to compare two
things and pick a winner agrees with human raters far better than a judge
asked for an absolute score, even when the two disagree about nothing but
HOW the same underlying opinion gets reported. So there is no score column
here — every decision is a match with a winner, and a rating is a derived
view over the match log, recomputed on read rather than stored, so a
corrected or re-judged match can never leave a stale number behind.
"""
from __future__ import annotations

import os
from typing import Optional

from . import activity, db
from .util import rows

DEFAULT_ELO = 1000.0
DEFAULT_K = 32.0


def record_match(root: str | os.PathLike[str], *, logical_name: str,
                 candidate_a_id: int, candidate_b_id: int,
                 shown_first: str = "a", tournament_ref: str = "",
                 actor: Optional[str] = None) -> dict:
    """Open one match: two candidates, a coin already flipped on which one
    the reviewer sees first. Returns the pending row (winner_id is NULL
    until record_verdict closes it).
    """
    who = actor if actor is not None else activity.current_actor()
    shown_first = shown_first if shown_first in ("a", "b") else "a"
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO art_match (logical_name, candidate_a_id, "
            "candidate_b_id, shown_first, reviewer, tournament_ref) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (logical_name, int(candidate_a_id), int(candidate_b_id),
             shown_first, who, tournament_ref))
        match_id = cur.lastrowid
    return get_match(root, match_id)


def get_match(root: str | os.PathLike[str], match_id: int) -> dict:
    row = db.connect(root).execute(
        "SELECT * FROM art_match WHERE id = ?", (match_id,)).fetchone()
    if row is None:
        raise LookupError(f"no art_match {match_id}")
    return dict(row)


def record_verdict(root: str | os.PathLike[str], match_id: int, *,
                   winner_artifact_id: int, reasons: str = "",
                   actor: Optional[str] = None) -> dict:
    """Close a match: the reviewer's pick, and why.

    `winner_artifact_id` must be one of the match's own two candidates — a
    reviewer citing an id from a different match is a mistake worth
    refusing loudly rather than silently recording a meaningless winner.
    """
    match = get_match(root, match_id)
    winner = int(winner_artifact_id)
    if winner not in (match["candidate_a_id"], match["candidate_b_id"]):
        raise ValueError(
            f"winner {winner} is not one of this match's candidates "
            f"({match['candidate_a_id']}, {match['candidate_b_id']})")
    who = actor if actor is not None else activity.current_actor()
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE art_match SET winner_id = ?, reasons = ?, reviewer = ?, "
            "decided_at = datetime('now') WHERE id = ?",
            (winner, (reasons or "").strip()[:1000], who, match_id))
    activity.log(root, "art_match",
                 f"{match['logical_name']}: candidate {winner} won a "
                 f"pairwise match ({(reasons or '').strip()[:120]})",
                 ref=str(match_id), actor=who)
    return get_match(root, match_id)


def list_matches(root: str | os.PathLike[str], *,
                 logical_name: Optional[str] = None,
                 tournament_ref: Optional[str] = None,
                 decided_only: bool = False, limit: int = 500) -> list[dict]:
    clauses, params = [], []
    if logical_name:
        clauses.append("logical_name = ?")
        params.append(logical_name)
    if tournament_ref:
        clauses.append("tournament_ref = ?")
        params.append(tournament_ref)
    if decided_only:
        clauses.append("winner_id IS NOT NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(int(limit))
    return rows(db.connect(root).execute(
        f"SELECT * FROM art_match {where} ORDER BY id ASC LIMIT ?", params))


def elo_ratings(matches: list[dict], *, k: float = DEFAULT_K,
                initial: float = DEFAULT_ELO) -> dict[int, dict]:
    """Standard Elo, one update per decided match, processed in match order.

    A pure function over rows a caller already has — no query inside it —
    so re-rating after a correction is "call this again", not a migration.
    Returns {artifact_id: {rating, matches, wins}}.
    """
    ratings: dict[int, float] = {}
    played: dict[int, int] = {}
    wins: dict[int, int] = {}

    def _get(cid: int) -> float:
        return ratings.setdefault(cid, initial)

    for m in matches:
        winner = m.get("winner_id")
        if winner is None:
            continue
        a, b = m["candidate_a_id"], m["candidate_b_id"]
        loser = b if winner == a else a
        ra, rb = _get(winner), _get(loser)
        expected_winner = 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))
        delta = k * (1.0 - expected_winner)
        ratings[winner] = ra + delta
        ratings[loser] = rb - delta
        played[winner] = played.get(winner, 0) + 1
        played[loser] = played.get(loser, 0) + 1
        wins[winner] = wins.get(winner, 0) + 1

    return {cid: {"rating": round(r, 1), "matches": played.get(cid, 0),
                 "wins": wins.get(cid, 0)}
            for cid, r in ratings.items()}


def standings(root: str | os.PathLike[str], logical_name: str, *,
             tournament_ref: Optional[str] = None) -> dict:
    """Every candidate that has played a match for this target, ranked."""
    matches = list_matches(root, logical_name=logical_name,
                           tournament_ref=tournament_ref)
    decided = [m for m in matches if m.get("winner_id") is not None]
    ratings = elo_ratings(decided)
    ranked = sorted(
        ({"artifact_id": cid, **stats} for cid, stats in ratings.items()),
        key=lambda r: r["rating"], reverse=True)
    pending = [m for m in matches if m.get("winner_id") is None]
    return {"logical_name": logical_name, "standings": ranked,
            "decided_matches": len(decided), "pending_matches": len(pending)}
