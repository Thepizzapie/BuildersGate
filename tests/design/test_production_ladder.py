"""R1-R8: slice stage, key risks, kind/severity, locks, cut, release board +
golden path, the level seat, iteration-scoped autopilot."""
from __future__ import annotations

import pytest

from bgate_core.board import iterations, queue, seats
from bgate_core.design import domainplan, gameplan, greenlight
from bgate_core.store import db, settings

MANIFEST = [
    {"kind": "scene", "name": "hub_room", "seat": "gameplay", "slice": True,
     "acceptance": "boots headless, player spawns"},
    {"kind": "asset", "name": "hero_sheet", "seat": "art", "slice": False,
     "acceptance": "idle+walk, consistency_check green"},
]


def _done(root, item_id, result="ok"):
    with db.tx(root) as tx:
        tx.execute("UPDATE work_item SET status = 'done', result = ? WHERE id = ?",
                   (result, int(item_id)))


def _stage(root, at):
    doc = greenlight._doc(root)
    doc["stage"] = at
    greenlight._save(root, doc)


class TestKindAndSeverity:
    def test_kind_is_derived_from_the_source_and_the_plan_row(self, root):
        gameplan.ingest(root, MANIFEST)
        assert queue.add(root, "qa", "repro", source="playtest")["kind"] == "fix"
        assert queue.add(root, "qa", "gate", source="qa-gate")["kind"] == ""
        assert queue.add(root, "art", "hero", source="game-plan",
                         source_ref="hero_sheet")["kind"] == "content"
        with pytest.raises(ValueError, match="unknown severity"):
            queue.add(root, "qa", "x", severity="meh")

    def test_only_a_minor_is_accepted_as_a_known_issue(self, root):
        major = queue.add(root, "gameplay", "crash on door", kind="fix",
                          severity="major")
        with pytest.raises(ValueError, match="only a minor"):
            queue.accept_known_issue(root, major["id"], "human", "it is fine really")
        minor = queue.add(root, "gameplay", "1px seam", kind="fix", severity="minor")
        got = queue.accept_known_issue(root, minor["id"], "human",
                                       "invisible at the game's camera")
        assert got["accepted_by"].startswith("human:")


class TestSliceStage:
    def test_slice_admits_only_slice_rows_fixes_and_gates(self, root):
        gameplan.ingest(root, MANIFEST)
        _stage(root, greenlight.SLICE)
        hub = gameplan.status(root)
        slice_item = next(r["item"] for r in hub["remaining"] if r["name"] == "hub_room")
        other = queue.add(root, "art", "concept a menu", brief="paint the menu")
        fix = queue.add(root, "gameplay", "door crash", brief="fix the door",
                        kind="fix", severity="major")
        ready = {r["id"] for r in queue.ready(root)}
        assert slice_item in ready and fix["id"] in ready
        assert other["id"] not in ready
        assert "slice" in queue.slice_hold_reason(root, queue.get(root, other["id"]))

    def test_production_needs_a_passed_slice_check_and_retired_risks(self, root):
        _stage(root, greenlight.SLICE)
        got = greenlight.blockers(root, greenlight.PRODUCTION)
        assert any("no vertical slice" in b for b in got)
        assert any("no key risks" in b for b in got)
        gameplan.ingest(root, MANIFEST)
        greenlight.set_thesis(root, {
            "sentence": "Each room the player chooses whether to spend the dash "
                        "on offence or keep it to escape",
            "options": ["strike", "hold"], "stakes": "a held dash wastes a window",
            "tension": "enemies alternate who punishes which choice",
            "dominant_strategy": "never dashing at all and kiting",
            "cadence": "every encounter, every few seconds"})
        greenlight.set_risks(root, [{"hypothesis": "players find the dash "
                                                   "strike without a tutorial"}])
        got = greenlight.blockers(root, greenlight.PRODUCTION)
        assert any("slice check has not passed" in b for b in got)
        assert any("key risk 0 is open" in b for b in got)
        check = queue.add(root, "qa", "SLICE CHECK", source=gameplan.SLICE_CHECK_SOURCE)
        _done(root, check["id"], "played it. VERDICT: PASS")
        greenlight.retire_risk(root, 0, "modified",
                               "3 of 5 found it; added a prompt on room one")
        assert greenlight._slice_blockers(root) == []


class TestLocksAndCut:
    def test_feature_lock_needs_features_built_or_cut_then_refuses_features(self, root):
        gameplan.ingest(root, MANIFEST, file_slice=False)
        _stage(root, greenlight.PRODUCTION)
        with pytest.raises(greenlight.StageRefused, match="hub_room"):
            greenlight.lock(root, "feature", "every system is in for alpha")
        gameplan.cut(root, "hub_room", "the hub moved into the first level itself")
        assert gameplan.milestones(root)["feature_complete"]
        greenlight.lock(root, "feature", "every system is in for alpha")
        with pytest.raises(greenlight.StageRefused, match="feature lock"):
            queue.add(root, "gameplay", "grappling hook", kind="feature")
        assert queue.add(root, "gameplay", "tune dash", kind="polish")["kind"] == "polish"
        with pytest.raises(greenlight.StageRefused, match="every row built or cut"):
            greenlight.lock(root, "content", "all content is in for beta")

    def test_cut_is_refused_while_an_item_builds_the_row(self, root):
        gameplan.ingest(root, MANIFEST)
        with pytest.raises(ValueError, match="queue_cancel"):
            gameplan.cut(root, "hub_room", "the hub moved into the first level")

    def test_content_lock_runs_fixes_first(self, root):
        gameplan.ingest(root, MANIFEST, file_slice=False)
        for name in ("hub_room", "hero_sheet"):
            gameplan.cut(root, name, "out of scope for this release candidate")
        _stage(root, greenlight.PRODUCTION)
        greenlight.lock(root, "feature", "every system is in for alpha")
        greenlight.lock(root, "content", "all content is in for beta")
        polish = queue.add(root, "gameplay", "juice", kind="polish", priority=9)
        fix = queue.add(root, "gameplay", "crash", kind="fix", severity="major")
        order = [r["id"] for r in queue.ready(root)]
        assert order.index(fix["id"]) < order.index(polish["id"])


class TestReleaseSections:
    def test_board_blocks_on_majors_and_unaccepted_minors(self, root):
        queue.add(root, "gameplay", "crash", kind="fix", severity="showstopper")
        queue.add(root, "gameplay", "seam", kind="fix", severity="minor")
        queue.add(root, "gameplay", "something", kind="fix")
        claims = [r["claim"] for r in greenlight._board_unmet(root)]
        assert any("open showstopper" in c for c in claims)
        assert any("nobody accepted" in c for c in claims)
        assert any("no severity" in c for c in claims)

    def test_golden_path_is_the_qa_plans_checks(self, root):
        assert "no QA plan" in greenlight._golden_path_unmet(root)[0]["claim"]
        domainplan.set_plan(root, "qa", {
            "goal": "The golden path from boot to the end of the slice plays "
                    "without a stop, every time.",
            "done_when": ["boot to slice end, no softlock, three runs"],
            "entries": [{"name": "golden_path", "steps": ["boot", "play"],
                         "covers": ["gameplay"],
                         "acceptance": "three clean runs recorded"}]})
        assert greenlight._golden_path_unmet(root)
        domainplan.record_check(root, "qa", 0, "pass",
                                "three runs, boot to end, captures in .bgate")
        assert greenlight._golden_path_unmet(root) == []


class TestLevelSeat:
    def test_level_is_a_seat_with_its_own_lanes_and_plan(self, root):
        assert "level" in seats.roles_for(root)
        assert domainplan.DOMAINS["level"]["seat"] == "level"
        from bgate_core import seattools
        assert seattools.seat_registers("room_build", "level")
        assert not seattools.seat_registers("room_build", "gameplay")


class TestIterationScope:
    def test_iteration_scope_dispatches_only_committed_work(self, root):
        from bgate_ui.agents import autodeploy
        a = queue.add(root, "gameplay", "dash", brief="build the dash")
        b = queue.add(root, "gameplay", "parry", brief="build the parry")
        settings.set(root, "autopilot.scope", "iteration")
        assert {c["id"] for c in autodeploy._candidates(str(root))} == set()
        iterations.open_next(root, "prove the dash feels right", [a["id"]])
        got = {c["id"] for c in autodeploy._candidates(str(root))}
        assert a["id"] in got and b["id"] not in got

    def test_close_needs_checks_and_the_next_needs_a_takeaway(self, root):
        iterations.open_next(root, "prove the dash feels right", [])
        with pytest.raises(ValueError, match="iteration_record_checks"):
            iterations.close(root, "the dash felt floaty in every room")
        iterations.record_checks(root, {"status": "pass", "summary": "14/14"})
        got = iterations.close(root, "the dash felt floaty in every room")
        assert queue.get(root, got["debrief"])["seat"] == "director"
        with pytest.raises(ValueError, match="previous_takeaway"):
            iterations.open_next(root, "tighten the dash arc", [])
        iterations.open_next(root, "tighten the dash arc", [],
                             previous_takeaway="the arc is too long; halve the hang")
