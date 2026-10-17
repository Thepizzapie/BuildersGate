"""domain_plan_set(plans=...): several domains, validated as a whole, filed in
dependency order - so a director need not know that level depends on
gameplay depends on tech.
"""
from __future__ import annotations

import pytest

from bgate_core.design import domainplan
from bgate_core.store import db

TECH = {
    "goal": "One match-state owner drives every turn, and nothing else holds "
            "turn order.",
    "done_when": ["godot_test_run: turn order survives a death"],
    "entries": [{"name": "match_state", "acceptance": "turn order test green",
                 "owns": "whose turn it is", "interfaces": ["turn_started"],
                 "slice": True}],
}
GAMEPLAY = {
    "goal": "Lobbing a pinecone is the one verb, and every lob is a risk the "
            "player chose.",
    "done_when": ["a lob carves terrain in a test"],
    "entries": [{"name": "lob", "acceptance": "lands within 2px of the prediction",
                 "verb": "lob a pinecone", "feedback": "thunk and crater",
                 "tunables": ["gravity=400"], "slice": True,
                 "depends_on": ["match_state"]}],
}
LEVEL = {
    "goal": "One back yard arena where cover is real and the water rises.",
    "done_when": ["room_audit clean on the yard"],
    "entries": [{"name": "backyard", "acceptance": "both teams spawn on solid ground",
                 "purpose": "test", "introduces": ["lob"], "slice": True,
                 "depends_on": ["lob"]}],
}


def _rows(root) -> dict:
    return {r["name"]: r["domain"] for r in db.connect(root).execute(
        "SELECT name, domain FROM plan_row").fetchall()}


class TestSetPlans:
    def test_the_filing_order_does_not_matter(self, root):
        # level -> gameplay -> tech is the WRONG order for set_plan
        got = domainplan.set_plans(root, {"level": LEVEL, "gameplay": GAMEPLAY,
                                          "tech": TECH})
        assert got["ok"] and got["written"] == ["tech", "gameplay", "level"]
        assert _rows(root) == {"match_state": "tech", "lob": "gameplay",
                               "backyard": "level"}
        assert set(got["results"]) == {"tech", "gameplay", "level"}

    def test_one_at_a_time_still_refuses_the_same_batch(self, root):
        with pytest.raises(ValueError, match="not a plan row"):
            domainplan.set_plan(root, "level", LEVEL)

    def test_a_bad_batch_writes_nothing(self, root):
        bad_level = {**LEVEL, "entries": [{**LEVEL["entries"][0],
                                           "depends_on": ["nonexistent"]}]}
        with pytest.raises(ValueError, match="level plan: 'backyard' depends on "
                                             "'nonexistent'"):
            domainplan.set_plans(root, {"tech": TECH, "gameplay": GAMEPLAY,
                                        "level": bad_level})
        assert _rows(root) == {}
        assert domainplan.get(root, "tech") is None

    def test_a_field_error_names_the_domain(self, root):
        with pytest.raises(ValueError, match="gameplay plan: .*feedback is required"):
            domainplan.set_plans(root, {
                "tech": TECH,
                "gameplay": {**GAMEPLAY, "entries": [{**GAMEPLAY["entries"][0],
                                                      "feedback": ""}]}})
        assert _rows(root) == {}

    def test_a_name_used_twice_across_the_batch_is_refused(self, root):
        clash = {**LEVEL, "entries": [{**LEVEL["entries"][0], "name": "lob",
                                       "depends_on": []}]}
        with pytest.raises(ValueError, match="already a gameplay plan row"):
            domainplan.set_plans(root, {"gameplay": GAMEPLAY, "tech": TECH,
                                        "level": clash})
        assert _rows(root) == {}

    def test_a_cross_domain_cycle_is_named(self, root):
        tech = {**TECH, "entries": [{**TECH["entries"][0], "depends_on": ["lob"]}]}
        with pytest.raises(ValueError, match="cycle: gameplay <-> tech"):
            domainplan.set_plans(root, {"tech": tech, "gameplay": GAMEPLAY})
        assert _rows(root) == {}

    def test_rows_already_on_the_board_satisfy_a_dependency(self, root):
        domainplan.set_plan(root, "tech", TECH)
        got = domainplan.set_plans(root, {"level": LEVEL, "gameplay": GAMEPLAY})
        assert got["written"] == ["gameplay", "level"]
        assert set(_rows(root)) == {"match_state", "lob", "backyard"}

    def test_merge_revises_in_place(self, root):
        domainplan.set_plans(root, {"tech": TECH, "gameplay": GAMEPLAY})
        got = domainplan.set_plans(root, {"gameplay": {"entries": [
            {**GAMEPLAY["entries"][0], "acceptance": "lands within 1px, test green"}]}})
        plan = domainplan.get(root, "gameplay")
        assert plan["revision"] == 2
        assert plan["entries"][0]["acceptance"] == "lands within 1px, test green"
        assert got["results"]["gameplay"]["revision"] == 2

    @pytest.mark.parametrize("plans", [None, {}, [], {"vibes": TECH}])
    def test_bad_shapes_are_refused(self, root, plans):
        with pytest.raises(ValueError):
            domainplan.set_plans(root, plans)


class TestTheTool:
    def test_plans_goes_through_domain_plan_set(self, root, monkeypatch):
        from bgate_mcp import tools_plan

        monkeypatch.setenv("BGATE_ROOT", str(root))
        got = tools_plan.domain_plan_set.__wrapped__(
            plans={"level": LEVEL, "gameplay": GAMEPLAY, "tech": TECH})
        assert got["ok"] and got["written"] == ["tech", "gameplay", "level"]
        assert set(_rows(root)) == {"match_state", "lob", "backyard"}

    def test_a_seat_may_only_batch_its_own_domains(self, root, monkeypatch):
        from bgate_mcp import tools_plan

        monkeypatch.setenv("BGATE_ROOT", str(root))
        monkeypatch.setenv("BGATE_SEAT", "gameplay")
        monkeypatch.setenv("BGATE_WORK_ITEM", "7")
        got = tools_plan.domain_plan_set.__wrapped__(
            plans={"gameplay": GAMEPLAY, "tech": TECH})
        assert got["ok"] is False and got["refused"] == "not_your_domain"
        assert "tech" in got["error"]
        assert _rows(root) == {}

    def test_a_bad_batch_is_an_error_payload_not_a_raise(self, root, monkeypatch):
        from bgate_mcp import tools_plan

        monkeypatch.setenv("BGATE_ROOT", str(root))
        got = tools_plan.domain_plan_set.__wrapped__(plans={"level": LEVEL})
        assert got["ok"] is False and "not a plan row" in got["error"]
        assert tools_plan.domain_plan_set.__wrapped__()["ok"] is False
