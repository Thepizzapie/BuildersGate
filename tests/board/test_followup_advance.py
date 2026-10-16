"""The stage advance: ONE director item when a gate clears, and only when asked.

Nothing re-entered the director after the slice check passed; this hook
does, behind a human-only setting that is off by default.
"""
from __future__ import annotations

import json

import pytest

from bgate_core.board import followup_advance, queue
from bgate_core.design import gameplan, greenlight
from bgate_core.qa import feel
from bgate_core.store import settings, workspace


def _at(root, stage):
    workspace.set(root, greenlight.SEAT, greenlight.DOC_KEY, {"stage": stage})


def _slice_check(root, verdict="VERDICT: PASS") -> int:
    item = queue.add(root, "qa", "SLICE CHECK: does the game actually play?",
                     brief="boot it, screenshot it, judge it" * 2,
                     source=gameplan.SLICE_CHECK_SOURCE, source_ref="3/3/3/0")
    queue.set_status(root, item["id"], "done", result=f"boots and plays. {verdict}")
    return int(item["id"])


def _advance_items(root):
    return [i for i in queue.list_items(root)
            if i["source"] == followup_advance.SOURCE]


class TestTheSliceGate:
    def test_off_by_default_files_nothing(self, root):
        _at(root, greenlight.SLICE)
        _slice_check(root)
        assert settings.get(root, "autopilot.stage_advance") is False
        assert followup_advance.sweep(root) is None
        assert followup_advance.due(root)["why"].endswith("is off")
        assert _advance_items(root) == []

    def test_a_passed_slice_check_files_exactly_one_director_item(self, root):
        _at(root, greenlight.SLICE)
        settings.set(root, "autopilot.stage_advance", True)
        greenlight.set_thesis(root, {
            "sentence": "Each turn the player decides whether to shoot from "
                        "cover or step out for the big hit.",
            "options": ["cover", "step out"], "stakes": "a gnome lost next turn",
            "tension": "wind moves the safe spots", "dominant_strategy": "always cover",
            "cadence": "every turn"})
        greenlight.set_risks(root, [{"hypothesis": "carved terrain holds 60 fps "
                                                   "with four teams", "retire_by": "a test"}])
        check = _slice_check(root)

        filed = followup_advance.sweep(root)
        assert filed is not None
        items = _advance_items(root)
        assert [i["id"] for i in items] == [filed["id"]]
        item = items[0]
        assert item["seat"] == "director"
        assert item["title"] == "ADVANCE: slice -> production"
        assert item["source_ref"] == f"slice->production#{check}"
        assert "greenlight_risk_retire" in item["brief"]
        assert "never" in item["brief"].lower() and "accepted" in item["brief"]
        assert "greenlight_advance('production')" in item["brief"]
        assert "plan_promote" in item["brief"]
        assert "carved terrain holds 60 fps" in item["brief"]
        # idempotent: a second sweep, and a tenth, file nothing more
        assert followup_advance.sweep(root) is None
        assert followup_advance.due(root)["why"] == "a stage-advance item is already open"
        assert len(_advance_items(root)) == 1

    def test_the_item_is_held_from_every_auto_dispatcher(self, root):
        _at(root, greenlight.SLICE)
        settings.set(root, "autopilot.stage_advance", True)
        _slice_check(root)
        filed = followup_advance.sweep(root)
        assert filed["source"] in queue.HELD_SOURCES
        assert all(i["id"] != filed["id"] for i in queue.ready(root))

    def test_a_failed_slice_check_files_nothing(self, root):
        _at(root, greenlight.SLICE)
        settings.set(root, "autopilot.stage_advance", True)
        _slice_check(root, verdict="VERDICT: FAIL - the yard does not boot")
        assert followup_advance.sweep(root) is None
        assert followup_advance.due(root)["why"] == "no slice check has passed"

    def test_the_feel_check_must_have_run(self, root, monkeypatch):
        _at(root, greenlight.SLICE)
        settings.set(root, "autopilot.stage_advance", True)
        _slice_check(root)
        monkeypatch.setattr(feel, "blockers",
                            lambda r: ["no scene has passed a feel check - ..."])
        assert followup_advance.sweep(root) is None
        assert followup_advance.due(root)["why"].startswith("the feel check holds")
        monkeypatch.setattr(feel, "blockers", lambda r: [])
        assert followup_advance.sweep(root) is not None

    def test_a_finished_advance_is_not_refiled_for_the_same_evidence(self, root):
        _at(root, greenlight.SLICE)
        settings.set(root, "autopilot.stage_advance", True)
        _slice_check(root)
        filed = followup_advance.sweep(root)
        queue.set_status(root, filed["id"], "done", result="risks retired, stage=production")
        # the director did its turn but (say) the stage did not move: the
        # same slice check does not buy a second director run
        assert followup_advance.sweep(root) is None
        assert "already been filed" in followup_advance.due(root)["why"] or \
            "already filed" in followup_advance.due(root)["why"]
        # a NEW passed slice check is new evidence
        _slice_check(root)
        assert followup_advance.sweep(root) is not None

    def test_nothing_at_thesis(self, root):
        _at(root, greenlight.THESIS)
        settings.set(root, "autopilot.stage_advance", True)
        _slice_check(root)
        assert followup_advance.sweep(root) is None


class TestTheReleaseGate:
    def test_a_clean_presentation_check_files_the_release_item(self, root, monkeypatch):
        # the root fixture is at production
        settings.set(root, "autopilot.stage_advance", True)
        monkeypatch.setattr(greenlight, "presentation_check",
                            lambda r: {"unmet": ["export: no verified pck"]})
        assert followup_advance.sweep(root) is None
        monkeypatch.setattr(greenlight, "presentation_check", lambda r: {"unmet": []})
        filed = followup_advance.sweep(root)
        assert filed["title"] == "ADVANCE: production -> release"
        assert "greenlight_advance('release')" in filed["brief"]
        assert followup_advance.sweep(root) is None


class TestThroughTheRouter:
    def test_the_slice_check_landing_files_the_item_on_the_same_tick(self, root):
        from bgate_ui.agents import followup

        _at(root, greenlight.SLICE)
        settings.set(root, "autopilot.stage_advance", True)
        # The router starts at the HEAD of the event log, never replaying
        # history - so there has to be a history for it to start after.
        earlier = queue.add(root, "tech", "an earlier item", brief="x" * 40)
        queue.set_status(root, earlier["id"], "dispatched")
        queue.complete(root, earlier["id"], result="landed")
        followup.tick(root)                       # bootstrap the cursor
        item = queue.add(root, "qa", "SLICE CHECK: does the game actually play?",
                         brief="boot it and judge it" * 3,
                         source=gameplan.SLICE_CHECK_SOURCE, source_ref="3/3/3/0")
        queue.set_status(root, item["id"], "dispatched")
        queue.complete(root, item["id"], result="boots. VERDICT: PASS")
        followup.tick(root)
        items = _advance_items(root)
        assert len(items) == 1
        # no director session is known to this project: it stays held for
        # the dashboard rather than being dispatched to a seat agent
        assert items[0]["status"] == "queued"
        followup.tick(root)
        assert len(_advance_items(root)) == 1

    def test_the_router_is_quiet_when_the_switch_is_off(self, root):
        from bgate_ui.agents import followup

        _at(root, greenlight.SLICE)
        earlier = queue.add(root, "tech", "an earlier item", brief="x" * 40)
        queue.set_status(root, earlier["id"], "dispatched")
        queue.complete(root, earlier["id"], result="landed")
        followup.tick(root)
        item = queue.add(root, "qa", "SLICE CHECK: does the game actually play?",
                         brief="boot it and judge it" * 3,
                         source=gameplan.SLICE_CHECK_SOURCE)
        queue.set_status(root, item["id"], "dispatched")
        queue.complete(root, item["id"], result="boots. VERDICT: PASS")
        followup.tick(root)
        assert _advance_items(root) == []


class TestTheSetting:
    def test_it_is_human_only_and_off(self, root, monkeypatch):
        s = settings.setting("autopilot.stage_advance")
        assert s.default is False and s.human_only
        monkeypatch.setenv("BGATE_ACTOR", "agent:item-7")
        with pytest.raises(settings.SettingError):
            settings.set(root, "autopilot.stage_advance", True)
