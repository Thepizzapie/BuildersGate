from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core.art import art_tournament
from bgate_core.store import artifacts
from bgate_ui.app import app


# ---------------------------------------------------------------------------
# Elo aggregation — pure, no database
# ---------------------------------------------------------------------------

def test_elo_ratings_rewards_the_winner():
    matches = [{"candidate_a_id": 1, "candidate_b_id": 2, "winner_id": 1}]
    ratings = art_tournament.elo_ratings(matches)
    assert ratings[1]["rating"] > art_tournament.DEFAULT_ELO
    assert ratings[2]["rating"] < art_tournament.DEFAULT_ELO
    assert ratings[1]["wins"] == 1
    assert ratings[2]["wins"] == 0
    assert ratings[1]["matches"] == ratings[2]["matches"] == 1


def test_elo_ratings_ignores_undecided_matches():
    matches = [{"candidate_a_id": 1, "candidate_b_id": 2, "winner_id": None}]
    assert art_tournament.elo_ratings(matches) == {}


def test_elo_ratings_a_four_win_streak_beats_a_fresh_opponent():
    # 1 beats 2 four times straight — a real dominance signal, unlike the
    # single-game cases below where path-dependence can go either way.
    matches = [{"candidate_a_id": 1, "candidate_b_id": 2, "winner_id": 1}] * 4
    ratings = art_tournament.elo_ratings(matches)
    assert ratings[1]["rating"] > art_tournament.DEFAULT_ELO
    assert ratings[1]["rating"] > ratings[2]["rating"]
    assert ratings[1]["wins"] == 4
    assert ratings[2]["wins"] == 0


def test_elo_ratings_a_split_record_stays_near_the_start():
    """One win each, in sequence, drifts only slightly from 1000 — Elo is
    path-dependent (the loser of game 1 enters game 2 as the 'weaker'
    player, so their win counts as a bigger upset), not perfectly
    symmetric, but the drift from one game each should stay small."""
    matches = [
        {"candidate_a_id": 1, "candidate_b_id": 2, "winner_id": 1},
        {"candidate_a_id": 1, "candidate_b_id": 2, "winner_id": 2},
    ]
    ratings = art_tournament.elo_ratings(matches)
    assert abs(ratings[1]["rating"] - art_tournament.DEFAULT_ELO) < 3.0
    assert abs(ratings[2]["rating"] - art_tournament.DEFAULT_ELO) < 3.0
    assert ratings[1]["matches"] == ratings[2]["matches"] == 2


# ---------------------------------------------------------------------------
# The match log, against a real (temp) project database
# ---------------------------------------------------------------------------

def _candidate(root, name, content):
    image = root / ".bgate_out" / "art" / f"{name}.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(content)
    return artifacts.register(root, "hero", image, producer="image_generate")


def test_record_and_close_a_match(root):
    a = _candidate(root, "a", b"one")
    b = _candidate(root, "b", b"two")
    match = art_tournament.record_match(
        root, logical_name="hero", candidate_a_id=a["id"],
        candidate_b_id=b["id"], shown_first="b")
    assert match["winner_id"] is None
    assert match["shown_first"] == "b"

    closed = art_tournament.record_verdict(
        root, match["id"], winner_artifact_id=a["id"], reasons="cleaner silhouette")
    assert closed["winner_id"] == a["id"]
    assert closed["reasons"] == "cleaner silhouette"
    assert closed["decided_at"]


def test_record_verdict_refuses_a_winner_outside_the_match(root):
    a = _candidate(root, "a", b"one")
    b = _candidate(root, "b", b"two")
    other = _candidate(root, "c", b"three")
    match = art_tournament.record_match(
        root, logical_name="hero", candidate_a_id=a["id"], candidate_b_id=b["id"])
    with pytest.raises(ValueError):
        art_tournament.record_verdict(root, match["id"], winner_artifact_id=other["id"])


def test_standings_ranks_by_derived_rating(root):
    a = _candidate(root, "a", b"one")
    b = _candidate(root, "b", b"two")
    c = _candidate(root, "c", b"three")
    for winner, loser in ((a, b), (a, c), (b, c)):
        m = art_tournament.record_match(
            root, logical_name="hero", candidate_a_id=winner["id"],
            candidate_b_id=loser["id"])
        art_tournament.record_verdict(root, m["id"], winner_artifact_id=winner["id"])

    result = art_tournament.standings(root, "hero")
    assert result["decided_matches"] == 3
    assert result["pending_matches"] == 0
    order = [row["artifact_id"] for row in result["standings"]]
    assert order == [a["id"], b["id"], c["id"]]


def test_standings_counts_pending_matches_separately(root):
    a = _candidate(root, "a", b"one")
    b = _candidate(root, "b", b"two")
    art_tournament.record_match(
        root, logical_name="hero", candidate_a_id=a["id"], candidate_b_id=b["id"])
    result = art_tournament.standings(root, "hero")
    assert result["pending_matches"] == 1
    assert result["decided_matches"] == 0
    assert result["standings"] == []


# ---------------------------------------------------------------------------
# The HTTP surface — /api/art-tournament/*
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


def test_start_requires_a_logical_name(client):
    got = client.post("/api/art-tournament/start", json={})
    assert got.status_code == 400


def test_start_requires_at_least_two_candidates(client, root):
    _candidate(root, "a", b"one")
    got = client.post("/api/art-tournament/start", json={"logical_name": "hero"})
    assert got.status_code == 404


def test_start_opens_a_round_robin_and_attempts_dispatch(client, root, monkeypatch):
    from bgate_ui.agents import dispatch
    # This sandbox has no real claude binary — same honesty check test_queue.py
    # uses. The interesting assertion here is that the matches were opened
    # (and randomised) regardless of whether the dispatch itself can spawn.
    monkeypatch.setattr(dispatch, "find_claude", lambda: None)

    _candidate(root, "a", b"one")
    _candidate(root, "b", b"two")
    _candidate(root, "c", b"three")

    got = client.post("/api/art-tournament/start", json={"logical_name": "hero"}).json()
    assert got["ok"] is False  # dispatch itself failed (no claude binary)
    assert got["candidate_count"] == 3
    assert got["match_count"] == 3  # C(3,2) round robin
    assert len(got["match_ids"]) == 3

    standings = client.get(
        "/api/art-tournament/standings", params={"logical_name": "hero"}).json()
    assert standings["pending_matches"] == 3
    assert standings["decided_matches"] == 0


def test_verdicts_close_matches_and_standings_update(client, root):
    a = _candidate(root, "a", b"one")
    b = _candidate(root, "b", b"two")
    match = art_tournament.record_match(
        root, logical_name="hero", candidate_a_id=a["id"], candidate_b_id=b["id"])

    # art_tournament_verdict is an MCP tool, not a route — exercised at the
    # bgate_core layer here since the MCP server needs a live stdio session
    # to invoke through; server.py's tool body is a thin wrapper over exactly
    # this call (see art_tournament_verdict in bgate_mcp/server.py).
    art_tournament.record_verdict(root, match["id"], winner_artifact_id=a["id"],
                                  reasons="cleaner silhouette")

    standings = client.get(
        "/api/art-tournament/standings", params={"logical_name": "hero"}).json()
    assert standings["decided_matches"] == 1
    assert standings["standings"][0]["artifact_id"] == a["id"]


# ---------------------------------------------------------------------------
# Silent-loss guards
#
# Every one of these was a path where the tournament reported success while
# quietly discarding or duplicating a reviewer's judgement.
# ---------------------------------------------------------------------------

def test_record_verdict_refuses_to_overwrite_a_decided_match(root):
    """The brief walks an agent through 28 of these in a row, so a retry after
    a dropped response was rewriting a recorded pick in silence — two activity
    lines both claiming a win, and a ranking that moved under a caller who
    thought they were re-sending the same thing."""
    a, b = _candidate(root, "a", b"one"), _candidate(root, "b", b"two")
    match = art_tournament.record_match(
        root, logical_name="hero", candidate_a_id=a["id"],
        candidate_b_id=b["id"], shown_first="a")
    art_tournament.record_verdict(root, match["id"], winner_artifact_id=a["id"])
    with pytest.raises(ValueError, match="already decided"):
        art_tournament.record_verdict(root, match["id"],
                                      winner_artifact_id=b["id"])
    assert art_tournament.get_match(root, match["id"])["winner_id"] == a["id"]


def test_standings_rates_every_match_not_the_oldest_five_hundred(root):
    """list_matches caps at 500 rows ordered oldest-first, and standings took
    that default — so past 500 comparisons a target was ranked on its OLDEST
    verdicts while every newer one was dropped without a word."""
    a, b = _candidate(root, "a", b"one"), _candidate(root, "b", b"two")
    for _ in range(505):
        match = art_tournament.record_match(
            root, logical_name="hero", candidate_a_id=a["id"],
            candidate_b_id=b["id"], shown_first="a")
        art_tournament.record_verdict(root, match["id"],
                                      winner_artifact_id=a["id"])
    assert art_tournament.standings(root, "hero")["decided_matches"] == 505


def test_list_matches_treats_an_empty_tournament_ref_as_a_filter(root):
    """record_match stores "" as its own default, so it is a real value a
    caller can ask for — `if tournament_ref:` silently widened that request
    to every tournament ever run for the name."""
    a, b = _candidate(root, "a", b"one"), _candidate(root, "b", b"two")
    art_tournament.record_match(root, logical_name="hero",
                                candidate_a_id=a["id"], candidate_b_id=b["id"],
                                shown_first="a", tournament_ref="")
    art_tournament.record_match(root, logical_name="hero",
                                candidate_a_id=a["id"], candidate_b_id=b["id"],
                                shown_first="a", tournament_ref="round-2")
    assert len(art_tournament.list_matches(root, logical_name="hero")) == 2
    assert len(art_tournament.list_matches(
        root, logical_name="hero", tournament_ref="")) == 1
    assert len(art_tournament.list_matches(
        root, logical_name="hero", tournament_ref="round-2")) == 1


def test_discard_matches_leaves_decided_ones_alone(root):
    a, b = _candidate(root, "a", b"one"), _candidate(root, "b", b"two")
    open_match = art_tournament.record_match(
        root, logical_name="hero", candidate_a_id=a["id"],
        candidate_b_id=b["id"], shown_first="a")
    closed = art_tournament.record_match(
        root, logical_name="hero", candidate_a_id=a["id"],
        candidate_b_id=b["id"], shown_first="a")
    art_tournament.record_verdict(root, closed["id"], winner_artifact_id=a["id"])
    assert art_tournament.discard_matches(
        root, [open_match["id"], closed["id"]]) == 1
    assert art_tournament.get_match(root, closed["id"])["winner_id"] == a["id"]


def test_start_refuses_more_candidates_than_the_round_robin_cap(client, root):
    """The cap check was unreachable: the query already truncated to 8, so
    `len(candidates) > 8` could never be true and twelve candidates silently
    became a round-robin over an arbitrary eight of them."""
    for i in range(12):
        _candidate(root, f"c{i}", b"x" * (i + 1))
    got = client.post("/api/art-tournament/start", json={"logical_name": "hero"})
    assert got.status_code == 400
    assert "cap" in got.text
    assert art_tournament.list_matches(root, logical_name="hero") == []


def test_start_refuses_a_second_open_tournament_for_the_same_target(client, root):
    """standings() pools every match for a logical_name, so a duplicate
    round-robin did not sit harmlessly beside the first — it weighted the
    same comparison twice in the Elo."""
    _candidate(root, "a", b"one")
    _candidate(root, "b", b"two")
    first = client.post("/api/art-tournament/start", json={"logical_name": "hero"})
    assert first.status_code == 200
    again = client.post("/api/art-tournament/start", json={"logical_name": "hero"})
    assert again.status_code == 409
    assert art_tournament.standings(root, "hero")["pending_matches"] == 1


def test_start_explains_the_auto_approve_interaction(client, root):
    """With art.auto_approve on, register() promotes on the spot and nothing
    ever reaches status "candidate" — so this endpoint 404s on every target,
    and a bare "found 0 candidates" points at the wrong thing entirely."""
    from bgate_core.store import settings
    settings.set(root, "art.auto_approve", True)
    _candidate(root, "a", b"one")
    _candidate(root, "b", b"two")
    got = client.post("/api/art-tournament/start", json={"logical_name": "hero"})
    assert got.status_code == 404
    assert "auto_approve" in got.text
