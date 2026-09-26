"""Pre-production research: comparables in, cited verdicts out, a plan the human adopts.

The rules under test:

  * the research agent's answer is parsed and validated before a row is
    written - a malformed finding is dropped and reported, not stored, and a
    finding with no source may not claim more than "low" confidence;
  * nothing is torn down until a human (or the director) confirms it;
  * a re-run of a teardown replaces that comparable's findings;
  * drafting a plan writes the draft and nothing else;
  * adopting writes the bible, not-building, open decisions and the thesis,
    upserts the bible by title, and refuses a second identical adopt;
  * the kickoff prompt carries the research-first step only when asked.

The research agent itself is replaced by a function returning canned text, so
none of this spawns a CLI or touches the network.
"""
from __future__ import annotations

import json

import pytest

from bgate_core.design import bible, decisions, greenlight, kickoff, research
from bgate_core.store import db, project


@pytest.fixture()
def root(tmp_path):
    project.init(tmp_path, "Emberfall", pitch="a cozy lighthouse-keeping roguelite")
    yield tmp_path
    db.close_all()


def _thinker(payload, calls=None):
    def think(root, *, system, prompt, kind):
        if calls is not None:
            calls.append({"kind": kind, "system": system, "prompt": prompt})
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return {"ok": True, "text": text, "model": "fake", "seconds": 0.1,
                "usd": 0.0}
    return think


TEARDOWN = {
    "summary": "Tight runs, generous meta-progression.",
    "findings": [
        {"system": "Meta-Progression", "verdict": "worked", "confidence": "high",
         "claim": "Unlocks between runs kept players returning.",
         "evidence": "Reviews single it out.",
         "sources": [{"url": "https://example.com/review", "title": "Review"}]},
        {"system": "economy", "verdict": "failed", "confidence": "high",
         "claim": "Currency inflated late.", "sources": []},
        {"system": "onboarding", "verdict": "great", "claim": "x"},
        {"verdict": "worked", "claim": "no system"},
    ],
}

PLAN = {
    "summary": "A cozy roguelite about keeping the light lit.",
    "pillars": [{"title": "The light is the clock", "body": "Every run is a night."},
                {"title": "Cozy, never cruel", "body": "No permadeath of friends."}],
    "core_loop": "Tend the lamp, row out to rescue ships, trade salvage for upgrades.",
    "setting": "A windswept northern coast in the age of sail.",
    "art_direction": "Warm pixel art, limited palette, side-on camera.",
    "stances": [{"system": "meta-progression", "stance": "keep",
                 "note": "Hades taught us unlocks keep players returning."},
                {"system": "economy", "stance": "avoid",
                 "note": "Inflation sank the comparable's late game."}],
    "not_building": [{"text": "A crafting tree",
                      "reason": "The comparable's crafting was its weakest system."}],
    "decisions": [{"title": "Does the keeper age between runs?",
                   "acceptance": "Playtesters can say why they started a new run.",
                   "leaves_dark": "Whether the story has an ending."}],
    "thesis": {"sentence": "Each night the keeper chooses which ship to risk rowing out for.",
               "options": ["the near ship", "the rich ship"],
               "stakes": "The lamp dims while you are away.",
               "tension": "Weather and lamp oil change every night.",
               "dominant_strategy": "Never leave the lamp at all.",
               "cadence": "Several times per night."},
}


def _researched(root) -> dict:
    comp = research.add_comp(root, "Dredge", "same coast, same dread", "setting")
    research.teardown(root, comp["id"], think=_thinker(TEARDOWN))
    return comp


def test_suggest_proposes_and_dedupes(root):
    research.add_comp(root, "Dredge", status="confirmed")
    calls: list = []
    out = research.suggest(root, think=_thinker({"comps": [
        {"title": "Dredge", "relation": "setting", "why": "dup"},
        {"title": "Hades", "relation": "loop", "why": "run structure"},
        {"title": "No Man's Sky at launch", "relation": "cautionary",
         "why": "overpromised"},
        {"relation": "loop"},
    ]}, calls))
    assert [c["title"] for c in out["proposed"]] == ["Hades",
                                                     "No Man's Sky at launch"]
    assert all(c["status"] == "proposed" for c in out["proposed"])
    assert len(out["skipped"]) == 2
    # The pitch reaches the agent and so does what is already on the list.
    assert "lighthouse" in calls[0]["prompt"]
    assert "Dredge" in calls[0]["prompt"]


def test_proposed_comp_cannot_be_torn_down(root):
    comp = research.add_comp(root, "Hades", status="proposed")
    with pytest.raises(ValueError, match="confirm it"):
        research.teardown(root, comp["id"], think=_thinker(TEARDOWN))
    research.set_status(root, comp["id"], "confirmed")
    assert research.teardown(root, comp["id"], think=_thinker(TEARDOWN))["kept"] == 2


def test_teardown_validates_row_by_row(root):
    comp = research.add_comp(root, "Dredge")
    out = research.teardown(root, comp["id"], think=_thinker(
        "Here you go:\n```json\n" + json.dumps(TEARDOWN) + "\n```"))
    assert out["kept"] == 2
    assert {r["why"] for r in out["rejected"]} == {
        "verdict 'great' is not one of ('worked', 'failed', 'mixed')",
        "no system"}
    rows = {f["system"]: f for f in research.findings(root)}
    assert rows["meta-progression"]["confidence"] == "high"
    assert rows["meta-progression"]["sources"][0]["url"].startswith("https://")
    # Unsourced may not claim to be well-founded.
    assert rows["economy"]["confidence"] == "low"
    assert research.get_comp(root, comp["id"])["status"] == "researched"


def test_teardown_rerun_replaces_findings(root):
    comp = _researched(root)
    research.teardown(root, comp["id"], think=_thinker({"findings": [
        {"system": "economy", "verdict": "mixed", "claim": "Patched later."}]}))
    rows = research.findings(root)
    assert [(r["system"], r["verdict"]) for r in rows] == [("economy", "mixed")]


def test_teardown_with_nothing_usable_writes_nothing(root):
    comp = research.add_comp(root, "Dredge")
    with pytest.raises(ValueError, match="no usable findings"):
        research.teardown(root, comp["id"], think=_thinker({"findings": [
            {"system": "x", "verdict": "sure", "claim": "y"}]}))
    assert research.get_comp(root, comp["id"])["status"] == "confirmed"


def test_agent_failure_is_raised_not_stored(root):
    comp = research.add_comp(root, "Dredge")

    def broken(root, **_):
        return {"ok": False, "error": "claude CLI not found on PATH"}
    with pytest.raises(RuntimeError, match="not found"):
        research.teardown(root, comp["id"], think=broken)
    with pytest.raises(ValueError, match="no JSON"):
        research.teardown(root, comp["id"], think=_thinker("I could not find it."))


def test_findings_query_and_search_index(root):
    _researched(root)
    assert [f["system"] for f in research.findings(root, system="progression")] \
        == ["meta-progression"]
    assert [f["verdict"] for f in research.findings(root, verdict="failed")] \
        == ["failed"]
    assert research.findings(root, query="inflated")[0]["system"] == "economy"
    hits = db.connect(root).execute(
        "SELECT ref FROM search_idx WHERE kind = 'research.finding'").fetchall()
    assert len(hits) == 2


def test_dropped_comp_leaves_findings_and_grid(root):
    comp = _researched(root)
    research.set_status(root, comp["id"], "dropped")
    assert research.findings(root) == []
    assert research.grid(root)["rows"] == []


def test_draft_needs_research_and_writes_only_the_draft(root):
    with pytest.raises(ValueError, match="nothing researched"):
        research.draft_plan(root, think=_thinker(PLAN))
    _researched(root)
    calls: list = []
    out = research.draft_plan(root, "keep it cozy", think=_thinker(PLAN, calls))
    assert out["plan"]["pillars"][0]["title"] == "The light is the clock"
    assert "Dredge" in calls[0]["prompt"] and "keep it cozy" in calls[0]["prompt"]
    # Nothing but the draft.
    assert bible.list_sections(root, kind="pillar") == []
    assert decisions.list_not_building(root) == []
    assert greenlight.thesis(root) == {}
    grid = research.grid(root)
    ours = {r["system"]: r["ours"] for r in grid["rows"]}
    assert ours["economy"]["stance"] == "avoid"
    assert grid["comps"] == ["Dredge"]


def test_invalid_plan_is_refused_not_repaired(root):
    _researched(root)
    bad = {**PLAN, "decisions": [{"title": "Open?", "acceptance": "", "leaves_dark": "x"}]}
    with pytest.raises(ValueError, match="acceptance"):
        research.draft_plan(root, think=_thinker(bad))
    with pytest.raises(ValueError, match="pillars"):
        research.validate_plan({**PLAN, "pillars": []})
    with pytest.raises(ValueError, match="stance"):
        research.validate_plan({**PLAN, "stances": [{"system": "x", "stance": "love"}]})


def test_adopt_writes_the_design_database(root):
    _researched(root)
    research.draft_plan(root, think=_thinker(PLAN))
    out = research.adopt(root, by="human")
    pillars = [s["title"] for s in bible.list_sections(root, kind="pillar")]
    assert pillars == ["The light is the clock", "Cozy, never cruel"]
    assert [s["title"] for s in bible.list_sections(root, kind="loop")] == ["Core loop"]
    constraints = {s["title"] for s in bible.list_sections(root, kind="constraint")}
    assert {"Setting", "Art direction"} <= constraints
    ref = [s for s in bible.list_sections(root, kind="reference")
           if s["title"] == research.RESEARCH_TITLE][0]
    assert "Dredge" in ref["body"] and "economy: avoid" in ref["body"]
    assert [n["text"] for n in decisions.list_not_building(root)] == ["A crafting tree"]
    assert [d["state"] for d in decisions.list_decisions(root)] == ["open"]
    assert greenlight.thesis(root)["sentence"].startswith("Each night")
    assert out["thesis_skipped"] is None
    assert research.status(root)["plan"] == "adopted"


def test_adopt_twice_is_refused_and_bible_upserts(root):
    _researched(root)
    research.draft_plan(root, think=_thinker(PLAN))
    research.adopt(root)
    with pytest.raises(research.AlreadyAdopted):
        research.adopt(root)
    edited = {**PLAN, "pillars": [{"title": "The light is the clock",
                                   "body": "Every run is one night, dusk to dawn."}],
              "not_building": [], "decisions": []}
    research.adopt(root, edited)
    pillars = bible.list_sections(root, kind="pillar")
    assert [p["body"] for p in pillars if p["title"] == "The light is the clock"] \
        == ["Every run is one night, dusk to dawn."]
    assert len(decisions.list_not_building(root)) == 1


def test_adopt_keeps_a_plan_whose_thesis_the_gate_rejects(root):
    _researched(root)
    weak = {**PLAN, "thesis": {"sentence": "A cozy game about a lighthouse keeper."}}
    out = research.adopt(root, weak)
    assert out["thesis_skipped"]
    assert greenlight.thesis(root) == {}
    assert len(bible.list_sections(root, kind="pillar")) == 2


def test_status_walks_the_steps(root):
    assert "research_suggest" in research.status(root)["next"]
    comp = research.add_comp(root, "Dredge", status="proposed")
    assert "confirm" in research.status(root)["next"]
    research.set_status(root, comp["id"], "confirmed")
    assert "research_teardown" in research.status(root)["next"]
    research.teardown(root, comp["id"], think=_thinker(TEARDOWN))
    assert "research_plan_draft" in research.status(root)["next"]
    research.draft_plan(root, think=_thinker(PLAN))
    assert "adopt" in research.status(root)["next"]


def test_kickoff_prompt_research_first_only_when_asked(root):
    assert "RESEARCH FIRST" not in kickoff.prompt(root, {})
    text = kickoff.prompt(root, {}, research=True)
    assert "RESEARCH FIRST" in text and "do not adopt it for me" in text
    assert text.index("RESEARCH FIRST") < text.index("settle the mechanical thesis")
