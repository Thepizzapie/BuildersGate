"""Domain plans: per-discipline end state -> plan_row -> brief, review, promote."""
from __future__ import annotations

import pytest

from bgate_core.board import queue, seats
from bgate_core.design import brainstorm, domainplan, gameplan

GAMEPLAY = {
    "goal": "Dashing through enemies is the only way to deal damage, and every "
            "dash is a risk the player chooses.",
    "done_when": ["godot_test_run: dash damages and grants i-frames"],
    "entries": [
        {"name": "dash", "acceptance": "dash moves 3.5m in 0.12s, test green",
         "verb": "dash through an enemy", "feedback": "flash, hitstop, thunk",
         "tunables": ["dash_distance=3.5m"], "slice": True},
        {"name": "parry", "acceptance": "parry window 120ms, test green",
         "verb": "parry a shot", "feedback": "ring sfx and slow-mo",
         "tunables": ["parry_window=0.12s"]},
    ],
}
LEVEL = {
    "goal": "Three rooms that teach the dash, test it under pressure, then "
            "reward mastery with a shortcut.",
    "done_when": ["traversal_prove passes on all three rooms"],
    "entries": [
        {"name": "room_teach", "acceptance": "room_audit clean, dash gap 3m",
         "purpose": "teach", "introduces": ["dash"], "slice": True,
         "depends_on": ["dash"]},
    ],
}


class TestValidate:
    def test_requires_domain_fields_and_a_real_goal(self):
        with pytest.raises(ValueError, match="goal"):
            domainplan.validate("gameplay", {**GAMEPLAY, "goal": "dash"})
        bad = {**GAMEPLAY, "entries": [{**GAMEPLAY["entries"][0], "feedback": ""}]}
        with pytest.raises(ValueError, match="feedback is required"):
            domainplan.validate("gameplay", bad)
        cue = {"goal": "x" * 50, "done_when": ["every mechanic has a cue at -14 LUFS"],
               "entries": [{"name": "c", "acceptance": "plays on dash in game",
                            "type": "kazoo", "trigger": "dash"}]}
        with pytest.raises(ValueError, match="type must be one of"):
            domainplan.validate("audio", cue)

    def test_unknown_domain_and_seat_scope(self):
        with pytest.raises(ValueError, match="unknown domain"):
            domainplan.template("vibes")
        assert domainplan.may_write("art", "animation")
        assert not domainplan.may_write("art", "audio")
        assert domainplan.may_write("director", "audio")
        assert domainplan.may_write("", "audio")


class TestSetAndStatus:
    def test_entries_compile_to_domain_tagged_plan_rows(self, root):
        domainplan.set_plan(root, "gameplay", GAMEPLAY)
        domainplan.set_plan(root, "level", LEVEL)
        state = gameplan.status(root)
        assert state["rows"] == 3
        assert state["by_domain"]["gameplay"]["rows"] == 2
        got = domainplan.status(root, "gameplay")["domains"]["gameplay"]
        assert got["entries"] == 2 and got["in_game"] == 0

    def test_merge_keeps_unmentioned_entries_and_replace_drops_spec_rows(self, root):
        domainplan.set_plan(root, "gameplay", GAMEPLAY)
        domainplan.set_plan(root, "gameplay", {"entries": [
            {**GAMEPLAY["entries"][1], "acceptance": "parry window 100ms, test green"}]})
        plan = domainplan.get(root, "gameplay")
        assert [e["name"] for e in plan["entries"]] == ["dash", "parry"]
        assert plan["revision"] == 2
        domainplan.set_plan(root, "gameplay",
                            {**GAMEPLAY, "entries": GAMEPLAY["entries"][:1]},
                            merge=False)
        assert {r["name"] for r in gameplan.row_states(root)} == {"dash"}

    def test_names_are_unique_across_domains_and_deps_must_exist(self, root):
        domainplan.set_plan(root, "gameplay", GAMEPLAY)
        clash = {**LEVEL, "entries": [{**LEVEL["entries"][0], "name": "dash",
                                       "depends_on": []}]}
        with pytest.raises(ValueError, match="already a gameplay plan row"):
            domainplan.set_plan(root, "level", clash)
        ghost = {**LEVEL, "entries": [{**LEVEL["entries"][0],
                                       "depends_on": ["ghost"]}]}
        with pytest.raises(ValueError, match="not a plan row"):
            domainplan.set_plan(root, "level", ghost)


class TestReview:
    def test_reads_plans_against_each_other(self, root):
        domainplan.set_plan(root, "gameplay", GAMEPLAY)
        domainplan.set_plan(root, "level", LEVEL)
        domainplan.set_plan(root, "audio", {
            "goal": "Every verb answers with a sound the player can feel in "
                    "the hands, mixed under the music bed.",
            "done_when": ["every gameplay mechanic has a cue in game"],
            "entries": [{"name": "dash_whoosh", "type": "sfx",
                         "trigger": "dash pressed",
                         "acceptance": "audible on dash, -14 LUFS"}]})
        texts = [f["text"] for f in domainplan.review(root)]
        assert any("'parry' is never introduced" in t for t in texts)
        assert any("'parry' has no cue" in t for t in texts)
        assert not any("'dash' has no cue" in t for t in texts)
        assert any(t.startswith("no art plan") for t in texts)


class TestBriefAndBoard:
    def test_seat_brief_carries_its_plan_or_the_order_to_write_one(self, root):
        domainplan.set_plan(root, "gameplay", GAMEPLAY)
        block = seats.brief(root, "gameplay")["domain_plan"]
        by = {p["domain"]: p for p in block["plans"]}
        assert by["gameplay"]["goal"].startswith("Dashing")
        assert by["gameplay"]["entries"][0]["state"] == "spec"
        assert "missing" in by["ui"]
        director = seats.brief(root, "director")["domain_plan"]
        assert any(d["domain"] == "gameplay" and d["planned"]
                   for d in director["domains"])

    def test_promote_files_in_order_with_links_and_the_domain_goal(self, root):
        domainplan.set_plan(root, "gameplay", GAMEPLAY)
        domainplan.set_plan(root, "level", LEVEL)
        with pytest.raises(ValueError, match="promote it in the same call"):
            domainplan.promote(root, ["room_teach"])
        got = domainplan.promote(root, ["room_teach", "dash"])
        dash, room = got["filed"]
        assert dash["name"] == "dash" and room["depends_on"] == [dash["id"]]
        item = queue.get(root, room["id"])
        assert "DOMAIN GOAL: Three rooms" in item["brief"]
        assert item["acceptance"].startswith("room_audit")
        with pytest.raises(ValueError, match="only spec or lost"):
            domainplan.promote(root, ["dash"])

    def test_link_points_a_row_at_a_hand_filed_item(self, root):
        domainplan.set_plan(root, "gameplay", GAMEPLAY)
        item = queue.add(root, "gameplay", "parry")
        gameplan.link(root, "parry", item["id"])
        assert {r["name"]: r["state"] for r in gameplan.row_states(root)}[
            "parry"] == "on_board"
        other = queue.add(root, "gameplay", "parry again")
        with pytest.raises(ValueError, match="already being built"):
            gameplan.link(root, "parry", other["id"])


def test_synthesis_preview_keeps_the_manifest():
    text = ('{"summary": "s", "items": [], "manifest": [{"kind": "scene", '
            '"name": "hub", "seat": "gameplay", "slice": true}]}')
    plan = brainstorm.parse_plan(text, "director")
    assert plan["manifest"][0]["name"] == "hub"


class TestPlanFirst:
    def test_no_plans_means_no_hold(self, root):
        queue.add(root, "art", "hero sheet", brief="draw the hero sheet now")
        assert domainplan.planning_held(root) == set()
        assert domainplan.ensure_planning_items(root) == []

    def test_unplanned_seat_is_held_until_its_planning_item_lands(self, root):
        domainplan.set_plan(root, "gameplay", GAMEPLAY)
        art = queue.add(root, "art", "hero sheet", brief="draw the hero sheet now")
        assert "art" in domainplan.planning_held(root)
        ready = {r["id"] for r in queue.ready(root)}
        assert art["id"] not in ready
        filed = domainplan.ensure_planning_items(root)
        assert len(filed) == 1
        assert domainplan.ensure_planning_items(root) == []      # idempotent
        plan_item = queue.get(root, filed[0])
        assert plan_item["source"] == domainplan.PLANNING_SOURCE
        assert "hero sheet" in plan_item["brief"]
        assert filed[0] in {r["id"] for r in queue.ready(root)}
        domainplan.set_plan(root, "art", {
            "goal": "Every sprite reads at 1x on the darkest background, in "
                    "one palette, at the game's camera.",
            "done_when": ["screen_audit passes on the slice scene"],
            "entries": [{"name": "hero_sheet", "use": "the player, always on "
                         "screen", "view": "side, 64px tall",
                         "acceptance": "sprite_sheet_check green, 8 frames"}]})
        assert art["id"] in {r["id"] for r in queue.ready(root)}


class TestChecksAndReplan:
    def test_checks_pass_go_stale_and_fail_flags_replan(self, root):
        domainplan.set_plan(root, "gameplay", GAMEPLAY)
        with pytest.raises(ValueError, match="evidence"):
            domainplan.record_check(root, "gameplay", 0, "pass", "ok")
        domainplan.record_check(root, "gameplay", 0, "pass",
                                "godot_test_run: 14 passed, 0 failed")
        states = domainplan.check_state(root, "gameplay")
        assert states[0]["state"] == "pass"
        domainplan.record_check(root, "gameplay", 0, "fail",
                                "godot_test_run: dash i-frames test failed")
        plan = domainplan.get(root, "gameplay")
        assert len(domainplan.open_flags(plan)) == 1
        assert any(f["severity"] == "replan" for f in domainplan.review(root))
        domainplan.set_plan(root, "gameplay", {"goal": GAMEPLAY["goal"] + " Revised."})
        assert domainplan.open_flags(domainplan.get(root, "gameplay")) == []

    def test_flag_is_raised_once_per_source(self, root):
        domainplan.set_plan(root, "gameplay", GAMEPLAY)
        assert domainplan.flag_replan(root, "gameplay", "why", "src:1")
        assert not domainplan.flag_replan(root, "gameplay", "why", "src:1")


class TestDeployAndDraft:
    def test_deploy_writes_plans_and_files_their_slice(self, root):
        got = brainstorm.deploy_plans(root, {"domain_plans": {
            "gameplay": GAMEPLAY, "level": LEVEL,
            "audio": {"goal": "short"}}}, session_id=1)
        assert set(got["domain_plans"]["written"]) == {"gameplay", "level"}
        assert "audio" in got["domain_plans"]["refused"]
        names = {f["name"] for f in got["slice_filed"]}
        assert names == {"dash", "room_teach"}

    def test_synthesis_system_asks_for_domain_plans(self):
        text = brainstorm.synthesis_system("director")
        assert '"domain_plans"' in text and "mechanic {verb" in text
        assert '"domain_plans"' not in brainstorm.synthesis_system("narrative")

    def test_draft_writes_nothing_and_offers_the_template(self, root):
        got = domainplan.draft(root, "narrative")
        assert got["template"]["entry"] == "beat"
        assert domainplan.get(root, "narrative") is None


def test_an_explicit_empty_list_closes_open_questions(root):
    domainplan.set_plan(root, "gameplay", {**GAMEPLAY, "open_questions": ["death saves?"]})
    domainplan.set_plan(root, "gameplay", {"goal": GAMEPLAY["goal"]})
    assert domainplan.get(root, "gameplay")["open_questions"] == ["death saves?"]
    domainplan.set_plan(root, "gameplay", {"open_questions": []})
    assert domainplan.get(root, "gameplay")["open_questions"] == []
