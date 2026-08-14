"""Quests: the mandatory done_when, the three shapes, and the HTTP gate.

The assertions here are chosen so that deleting the feature would fail them —
each one names a behaviour the module exists for, not merely that a row round
trips.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core import db, lore, quests


@pytest.fixture()
def root(tmp_path):
    db.connect(tmp_path)
    lore.add_entity(tmp_path, "character", "Accounting Wizard", status="canon")
    return tmp_path


def _steps(n=2):
    return [{"text": f"step {i}", "done_when": f"flag_{i} is true"}
            for i in range(n)]


# --- the mandatory column --------------------------------------------------

def test_blank_done_when_is_refused_and_says_what_to_type(root):
    with pytest.raises(ValueError) as exc:
        quests.add(root, "A quest", steps=[{"text": "talk to the wizard",
                                            "done_when": "   "}])
    msg = str(exc.value)
    assert "done_when" in msg
    # The error teaches: it carries an example of an observable, which is the
    # whole reason the field is refused rather than defaulted.
    assert "inventory" in msg


def test_blank_step_text_is_refused(root):
    with pytest.raises(ValueError):
        quests.add(root, "A quest", steps=[{"text": "", "done_when": "x"}])


def test_a_step_survives_with_both_halves(root):
    q = quests.add(root, "The unsigned form", steps=_steps())
    assert [s["done_when"] for s in q["steps"]] == ["flag_0 is true",
                                                    "flag_1 is true"]


# --- the three shapes ------------------------------------------------------

def test_no_steps_is_a_problem_not_an_exception(root):
    """A quest with no steps must be WRITABLE and marked, not refused.

    It is the state a half-written quest is legitimately in, and a panel cannot
    show you what is wrong with a row that could not be saved.
    """
    q = quests.add(root, "Premise only")
    assert q["ok"] is False
    assert [p["kind"] for p in q["problems"]] == ["no-steps"]


def test_all_optional_can_never_be_finished(root):
    q = quests.add(root, "All optional", steps=[
        {"text": "a", "done_when": "b", "optional": True},
        {"text": "c", "done_when": "d", "optional": True},
    ])
    assert q["ok"] is False
    assert [p["kind"] for p in q["problems"]] == ["all-optional"]


def test_one_required_step_among_optionals_is_fine(root):
    q = quests.add(root, "Mixed", steps=[
        {"text": "a", "done_when": "b", "optional": True},
        {"text": "c", "done_when": "d"},
    ])
    assert q["ok"] is True


def test_cutting_a_step_closes_the_gap_in_the_numbering(root):
    """The regression this guards: a delete that leaves ord = [0, 2].

    That is the broken-order problem, and a tool that creates it while removing
    a step is worse than no tool.
    """
    q = quests.add(root, "Three steps", steps=_steps(3))
    after = quests.cut_step(root, q["steps"][1]["id"])
    assert [s["ord"] for s in after["steps"]] == [0, 1]
    assert [s["text"] for s in after["steps"]] == ["step 0", "step 2"]
    assert after["ok"] is True


# --- the giver hangs off the graph -----------------------------------------

def test_giver_resolves_to_the_lore_entity(root):
    q = quests.add(root, "Given", steps=_steps(1), giver="accounting-wizard")
    assert q["giver"]["name"] == "Accounting Wizard"


def test_unknown_giver_is_refused_by_name(root):
    with pytest.raises(ValueError) as exc:
        quests.add(root, "Ghost", steps=_steps(1), giver="nobody-here")
    assert "nobody-here" in str(exc.value)


def test_no_giver_is_allowed(root):
    """A quest from the world, not from somebody. Legitimate, not a defect."""
    q = quests.add(root, "From the world", steps=_steps(1))
    assert q["giver"] is None and q["ok"] is True


def test_retiring_the_giver_keeps_the_quest(root):
    """SET NULL, not CASCADE: deleting a character must not delete the work."""
    q = quests.add(root, "Outlives", steps=_steps(1), giver="accounting-wizard")
    with db.tx(root) as conn:
        conn.execute("DELETE FROM lore_entity WHERE slug = 'accounting-wizard'")
    again = quests.get(root, q["id"])
    assert again["title"] == "Outlives"
    assert again["giver"] is None


# --- listing ---------------------------------------------------------------

def test_the_listing_carries_the_verdict(root):
    """So a broken quest is visible without opening it."""
    quests.add(root, "Fine", steps=_steps(1))
    quests.add(root, "Broken")
    rows = quests.list_quests(root)
    assert {r["title"]: r["ok"] for r in rows} == {"Fine": True, "Broken": False}


def test_listing_and_read_agree_about_the_giver(root):
    """The regression: the listing carried `giver_slug` and the read carried a
    nested `giver`, so the panel — which reads the LISTING — drew every quest as
    coming from nobody while every row had a giver."""
    quests.add(root, "Given", steps=_steps(1), giver="accounting-wizard")
    listed = quests.list_quests(root)[0]
    read = quests.get(root, listed["id"])
    assert listed["giver"] == read["giver"]
    assert listed["giver"]["name"] == "Accounting Wizard"


def test_listing_says_none_when_there_is_no_giver(root):
    quests.add(root, "Worldly", steps=_steps(1))
    assert quests.list_quests(root)[0]["giver"] is None


def test_duplicate_title_is_refused(root):
    quests.add(root, "Same", steps=_steps(1))
    with pytest.raises(ValueError):
        quests.add(root, "Same", steps=_steps(1))


def test_brief_offers_only_characters_and_factions_as_givers(root):
    lore.add_entity(root, "place", "Floor 40")
    givers = {g["slug"] for g in quests.brief(root)["givers"]}
    assert "accounting-wizard" in givers and "floor-40" not in givers


# --- the HTTP door ---------------------------------------------------------

@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    from bgate_ui import app as _app
    return TestClient(_app.app)


def test_http_write_lands_and_reports_canon(client):
    r = client.post("/api/quests", json={
        "title": "The unsigned form",
        "premise": "A form was never signed.",
        "steps": [{"text": "find it", "done_when": "form_found is true"}],
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["quest"]["ok"] is True
    # The gate ran and said something, even if only "ok".
    assert body["canon"]["verdict"] in ("ok", "review")


def test_http_write_refuses_a_canon_conflict(client, root):
    """A retired entity in new content is canon.check's hard flag.

    Writing it anyway would put the contradiction in the database where the next
    agent reads it as settled — which is the whole reason the route runs the
    check instead of trusting the caller to.
    """
    lore.add_entity(root, "character", "Ghost Auditor", status="retired")
    r = client.post("/api/quests", json={
        "title": "Meet the Ghost Auditor",
        "steps": [{"text": "find them", "done_when": "met_ghost is true"}],
    })
    assert r.status_code == 400
    assert "Ghost Auditor" in r.text


def test_http_write_refuses_a_blank_done_when(client):
    r = client.post("/api/quests", json={
        "title": "No condition",
        "steps": [{"text": "do a thing", "done_when": ""}],
    })
    assert r.status_code == 400
    assert "done_when" in r.text


def test_http_index_lists_quests_and_givers(client):
    client.post("/api/quests", json={
        "title": "Listed", "steps": [{"text": "a", "done_when": "b"}]})
    data = client.get("/api/quests").json()["data"]
    assert [q["title"] for q in data["quests"]] == ["Listed"]
    assert data["states"] == list(quests.STATES)


# --- the PATCH gate ---------------------------------------------------------
# routes/quests.py argues that HTTP writes are allowed HERE because the write
# path runs canon_check. PATCH accepted `premise` and `reward` — narrative prose,
# the exact material canon reads — and called quests.update directly, so the
# quieter door was the one that got past the gate.

def test_patch_refuses_prose_that_contradicts_canon(client, root):
    lore.add_entity(root, "character", "Ghost Auditor", status="retired")
    q = client.post("/api/quests", json={
        "title": "Fine quest", "steps": [{"text": "a", "done_when": "b is true"}],
    }).json()["data"]["quest"]
    r = client.patch(f"/api/quests/{q['slug']}",
                     json={"premise": "You meet the Ghost Auditor at dawn."})
    assert r.status_code == 400, r.text
    assert "Ghost Auditor" in r.text


def test_patch_accepts_clean_prose_and_reports_the_verdict(client):
    q = client.post("/api/quests", json={
        "title": "Clean quest", "steps": [{"text": "a", "done_when": "b is true"}],
    }).json()["data"]["quest"]
    r = client.patch(f"/api/quests/{q['slug']}",
                     json={"premise": "An ordinary morning at the desk."})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["canon"]["verdict"] in ("ok", "review")


def test_patch_of_state_alone_runs_no_check(client):
    """`state` is a four-value enum with nothing for canon to read — checking it
    would cost a scan of the whole quest to validate the word 'active'."""
    q = client.post("/api/quests", json={
        "title": "State only", "steps": [{"text": "a", "done_when": "b is true"}],
    }).json()["data"]["quest"]
    r = client.patch(f"/api/quests/{q['slug']}", json={"state": "active"})
    assert r.status_code == 200, r.text
    assert "canon" not in r.json()["data"]
