"""The production stage: thesis, slice, seat holds, and the release gate."""
from __future__ import annotations

import pytest

from bgate_core.level import encounter
from bgate_core.design import greenlight
from bgate_core.board import queue


def _thesis(**over):
    got = {
        "sentence": "Every room the player chooses whether to spend battery "
                    "light to see the threat or move blind and keep the "
                    "charge for the exit.",
        "options": ["burn battery to see", "move blind and bank the charge"],
        "stakes": "a wrong read costs the shift, and the battery does not "
                  "come back",
        "tension": "the exit needs charge, so the safest moment to look is "
                   "also the most expensive one",
        "dominant_strategy": "if corridors were short enough to cross blind "
                             "by memory, nobody would ever light one",
        "cadence": "once per room, about every forty seconds",
    }
    got.update(over)
    return got


class TestThesis:
    def test_a_premise_is_not_a_thesis(self, fresh_root):
        with pytest.raises(ValueError, match="describes the game, not a decision"):
            greenlight.set_thesis(
                fresh_root, _thesis(sentence="A tense horror game about surviving "
                                       "the night shift in an office tower."))

    def test_one_option_is_a_button(self, fresh_root):
        with pytest.raises(ValueError, match="at least two options"):
            greenlight.set_thesis(fresh_root, _thesis(options=["press the button"]))

    def test_the_same_option_twice_is_one_option(self, fresh_root):
        with pytest.raises(ValueError, match="same option written twice"):
            greenlight.set_thesis(
                fresh_root, _thesis(options=["burn battery", "Burn Battery"]))

    @pytest.mark.parametrize(
        "field", ["stakes", "tension", "dominant_strategy", "cadence"])
    def test_every_field_is_owed(self, fresh_root, field):
        with pytest.raises(ValueError, match=f"no {field}"):
            greenlight.set_thesis(fresh_root, _thesis(**{field: ""}))

    def test_a_settled_thesis_does_not_open_the_gate(self, fresh_root):
        greenlight.set_thesis(fresh_root, _thesis())
        assert greenlight.stage(fresh_root) == greenlight.THESIS


class TestSeatHolds:
    def test_a_new_project_starts_at_thesis(self, fresh_root):
        assert greenlight.stage(fresh_root) == greenlight.THESIS

    def test_art_is_held_before_a_thesis_exists(self, fresh_root):
        ok, why = greenlight.allows(fresh_root, "art")
        assert not ok
        assert "no mechanical thesis" in why

    def test_qa_runs_at_every_stage(self, fresh_root):
        # Holding qa would deadlock the gate qa exists to clear.
        assert greenlight.allows(fresh_root, "qa")[0]

    def test_every_seat_runs_at_slice(self, fresh_root):
        greenlight.set_thesis(fresh_root, _thesis())
        greenlight.advance(fresh_root, greenlight.SLICE)
        from bgate_core.board import seats
        for seat in seats.DEFAULT_SEATS:
            assert greenlight.allows(fresh_root, seat)[0], seat
        assert greenlight.held_seats(fresh_root) == ()

    def test_ready_does_not_dispatch_a_held_seat(self, fresh_root):
        art = queue.add(fresh_root, "art", "paint the lobby")
        narrative = queue.add(fresh_root, "narrative", "write the pitch")
        ready = {int(r["id"]) for r in queue.ready(fresh_root)}
        assert int(narrative["id"]) in ready
        assert int(art["id"]) not in ready

    def test_a_waiver_releases_one_seat_and_only_that_seat(self, fresh_root):
        greenlight.waive(fresh_root, "art", "the test room needs its placeholder "
                                      "blocks and nobody else can make them")
        assert greenlight.allows(fresh_root, "art")[0]
        assert not greenlight.allows(fresh_root, "audio")[0]

    def test_a_waiver_costs_a_sentence(self, fresh_root):
        with pytest.raises(ValueError, match="costs a sentence"):
            greenlight.waive(fresh_root, "art", "later")

    def test_a_waiver_can_be_withdrawn(self, fresh_root):
        greenlight.waive(fresh_root, "art", "the test room needs placeholder blocks "
                                      "and art is the only seat that makes them")
        greenlight.unwaive(fresh_root, "art")
        assert not greenlight.allows(fresh_root, "art")[0]

    def test_production_holds_nobody(self, fresh_root):
        _reach_production(fresh_root)
        assert greenlight.held_seats(fresh_root) == ()


class TestLegacyGrayboxStage:
    def test_a_project_stored_at_graybox_reads_as_slice(self, fresh_root):
        from bgate_core.store import workspace
        workspace.set(fresh_root, greenlight.SEAT, greenlight.DOC_KEY,
                      {"stage": "graybox"})
        assert greenlight.stage(fresh_root) == greenlight.SLICE
        assert greenlight.state(fresh_root)["stage"] == "slice"
        assert greenlight.held_seats(fresh_root) == ()

    def test_graybox_is_not_a_stage(self, fresh_root):
        assert "graybox" not in greenlight.STAGES
        with pytest.raises(ValueError):
            greenlight.advance(fresh_root, "graybox")

    def test_state_has_no_graybox_keys(self, fresh_root):
        got = greenlight.state(fresh_root)
        assert "graybox" not in got and "fast_track" not in got


class TestAdvance:
    def test_slice_needs_a_thesis(self, fresh_root):
        with pytest.raises(greenlight.StageRefused,
                           match="no mechanical thesis"):
            greenlight.advance(fresh_root, greenlight.SLICE)

    def test_thesis_straight_to_slice_needs_only_a_thesis(self, fresh_root):
        greenlight.set_thesis(fresh_root, _thesis())
        assert greenlight.blockers(fresh_root, greenlight.SLICE) == []
        greenlight.advance(fresh_root, greenlight.SLICE)
        assert greenlight.stage(fresh_root) == greenlight.SLICE

    def test_going_backward_is_allowed(self, fresh_root):
        _reach_production(fresh_root)
        greenlight.advance(fresh_root, greenlight.SLICE)
        assert greenlight.stage(fresh_root) == greenlight.SLICE

    def test_an_isolated_enemy_roster_blocks_the_slice(self, fresh_root):
        greenlight.set_thesis(fresh_root, _thesis())
        encounter.set_roster(fresh_root, [
            {"name": "Shambler", "role": "melee",
             "pressure": "closes the distance and forces you to move"},
            {"name": "Watcher", "role": "ranged",
             "pressure": "punishes you for standing still in the open"},
        ])
        with pytest.raises(greenlight.StageRefused, match="enemy design"):
            greenlight.advance(fresh_root, greenlight.SLICE)


class TestReleaseGate:
    def test_the_guard_is_silent_below_release(self, fresh_root):
        _reach_production(fresh_root)
        greenlight.release_guard(fresh_root)          # does not raise

    def test_a_check_that_cannot_run_is_a_fail_not_a_skip(self, fresh_root, monkeypatch):
        monkeypatch.setattr(greenlight, "_rooms_unmet",
                            lambda _root: (_ for _ in ()).throw(
                                RuntimeError("the reviewer is offline")))
        got = greenlight.presentation_check(fresh_root)
        assert not got["ok"]
        assert any("could not run" in row for row in got["unmet"])

    def test_release_cannot_be_waived(self, fresh_root, monkeypatch):
        _reach_production(fresh_root)
        monkeypatch.setattr(greenlight, "_rooms_unmet",
                            lambda _root: ["the lobby has never been reviewed"])
        # A waiver on every seat changes nothing about the release gate.
        for seat in ("art", "audio", "gameplay"):
            greenlight.waive(fresh_root, seat, "the human said ship it tonight and "
                                         "took responsibility for the call")
        with pytest.raises(greenlight.StageRefused, match="no waiver"):
            greenlight.advance(fresh_root, greenlight.RELEASE)


def _reach_production(fresh_root):
    greenlight.set_thesis(fresh_root, _thesis())
    greenlight.advance(fresh_root, greenlight.SLICE)
    clear_slice(fresh_root)
    greenlight.advance(fresh_root, greenlight.PRODUCTION)


def clear_slice(root):
    """What the slice stage owes: a slice row, a passed slice check, and every
    key risk retired."""
    from bgate_core.board import queue
    from bgate_core.design import gameplan
    from bgate_core.store import db

    gameplan.ingest(root, [{"kind": "scene", "name": "hub_room",
                            "seat": "gameplay", "slice": True,
                            "acceptance": "boots headless"}])
    check = queue.add(root, "qa", "SLICE CHECK",
                      source=gameplan.SLICE_CHECK_SOURCE)
    with db.tx(root) as tx:
        tx.execute("UPDATE work_item SET status = 'done', result = ? WHERE id = ?",
                   ("booted, played the slice end to end. VERDICT: PASS",
                    check["id"]))
    greenlight.set_risks(root, [{"hypothesis": "the dash reads clearly at "
                                               "sixty frames per second"}])
    greenlight.retire_risk(root, 0, "confirmed",
                           "measured 60 fps in the slice playtest capture")
