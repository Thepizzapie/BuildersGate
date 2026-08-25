"""The production stage: thesis, graybox, seat holds, and the release gate."""
from __future__ import annotations

import pytest

from bgate_core import encounter, greenlight, queue


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

    def test_gameplay_runs_at_graybox(self, fresh_root):
        greenlight.set_thesis(fresh_root, _thesis())
        greenlight.advance(fresh_root, greenlight.GRAYBOX)
        assert greenlight.allows(fresh_root, "gameplay")[0]
        assert not greenlight.allows(fresh_root, "art")[0]

    def test_ready_does_not_dispatch_a_held_seat(self, fresh_root):
        art = queue.add(fresh_root, "art", "paint the lobby")
        gameplay = queue.add(fresh_root, "gameplay", "prove the loop")
        greenlight.set_thesis(fresh_root, _thesis())
        greenlight.advance(fresh_root, greenlight.GRAYBOX)
        ready = {int(r["id"]) for r in queue.ready(fresh_root)}
        assert int(gameplay["id"]) in ready
        assert int(art["id"]) not in ready

    def test_a_waiver_releases_one_seat_and_only_that_seat(self, fresh_root):
        greenlight.set_thesis(fresh_root, _thesis())
        greenlight.advance(fresh_root, greenlight.GRAYBOX)
        greenlight.waive(fresh_root, "art", "the graybox needs its placeholder "
                                      "blocks and nobody else can make them")
        assert greenlight.allows(fresh_root, "art")[0]
        assert not greenlight.allows(fresh_root, "audio")[0]

    def test_a_waiver_costs_a_sentence(self, fresh_root):
        with pytest.raises(ValueError, match="costs a sentence"):
            greenlight.waive(fresh_root, "art", "later")

    def test_a_waiver_can_be_withdrawn(self, fresh_root):
        greenlight.waive(fresh_root, "art", "the graybox needs placeholder blocks "
                                      "and art is the only seat that makes them")
        greenlight.unwaive(fresh_root, "art")
        assert not greenlight.allows(fresh_root, "art")[0]

    def test_production_holds_nobody(self, fresh_root):
        _reach_production(fresh_root)
        assert greenlight.held_seats(fresh_root) == ()


class TestGraybox:
    def test_a_scene_that_does_not_exist_is_a_claim(self, fresh_root):
        with pytest.raises(ValueError, match="not a file under this project"):
            greenlight.graybox_submit(fresh_root, scene="game/scenes/ghost.tscn",
                                      evidence=["shot.png"])

    def test_evidence_is_required(self, fresh_root, tmp_path):
        _write_scene(fresh_root)
        with pytest.raises(ValueError, match="needs evidence"):
            greenlight.graybox_submit(fresh_root, scene="game/scenes/graybox.tscn",
                                      evidence=[])

    def test_a_verdict_needs_a_reason_even_to_pass(self, fresh_root):
        _submit(fresh_root)
        with pytest.raises(ValueError, match="say why"):
            greenlight.graybox_verdict(fresh_root, verdict="pass", interesting=True,
                                       why="ok")

    def test_a_pass_cannot_say_it_is_not_interesting(self, fresh_root):
        _submit(fresh_root)
        with pytest.raises(ValueError, match="contradiction"):
            greenlight.graybox_verdict(
                fresh_root, verdict="pass", interesting=False,
                why="it is attack, dodge and hold interact, but ship it")

    def test_there_is_nothing_to_rule_on_before_a_submission(self, fresh_root):
        with pytest.raises(greenlight.StageRefused, match="no graybox"):
            greenlight.graybox_verdict(
                fresh_root, verdict="pass", interesting=True,
                why="the light decision holds up over four rooms")


class TestAdvance:
    def test_graybox_needs_a_thesis(self, fresh_root):
        with pytest.raises(greenlight.StageRefused,
                           match="no mechanical thesis"):
            greenlight.advance(fresh_root, greenlight.GRAYBOX)

    def test_production_needs_a_passed_graybox(self, fresh_root):
        greenlight.set_thesis(fresh_root, _thesis())
        greenlight.advance(fresh_root, greenlight.GRAYBOX)
        with pytest.raises(greenlight.StageRefused, match="no graybox"):
            greenlight.advance(fresh_root, greenlight.PRODUCTION)

    def test_a_failed_graybox_does_not_open_production(self, fresh_root):
        greenlight.set_thesis(fresh_root, _thesis())
        greenlight.advance(fresh_root, greenlight.GRAYBOX)
        _submit(fresh_root)
        greenlight.graybox_verdict(
            fresh_root, verdict="fail", interesting=False,
            why="it reduces to attack, dodge and hold interact — the light "
                "never actually changes what you do")
        with pytest.raises(greenlight.StageRefused, match="not 'pass'"):
            greenlight.advance(fresh_root, greenlight.PRODUCTION)

    def test_going_backward_is_allowed(self, fresh_root):
        _reach_production(fresh_root)
        greenlight.advance(fresh_root, greenlight.GRAYBOX)
        assert greenlight.stage(fresh_root) == greenlight.GRAYBOX

    def test_an_isolated_enemy_roster_blocks_production(self, fresh_root):
        greenlight.set_thesis(fresh_root, _thesis())
        greenlight.advance(fresh_root, greenlight.GRAYBOX)
        _submit(fresh_root)
        _pass(fresh_root)
        encounter.set_roster(fresh_root, [
            {"name": "Shambler", "role": "melee",
             "pressure": "closes the distance and forces you to move"},
            {"name": "Watcher", "role": "ranged",
             "pressure": "punishes you for standing still in the open"},
        ])
        with pytest.raises(greenlight.StageRefused, match="enemy design"):
            greenlight.advance(fresh_root, greenlight.PRODUCTION)


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


def _write_scene(fresh_root):
    scene = fresh_root / "game" / "scenes"
    scene.mkdir(parents=True, exist_ok=True)
    (scene / "graybox.tscn").write_text("[gd_scene format=3]\n", encoding="utf-8")
    (fresh_root / "shot.png").write_bytes(b"not really a png")


def _submit(fresh_root):
    _write_scene(fresh_root)
    return greenlight.graybox_submit(
        fresh_root, scene="game/scenes/graybox.tscn", evidence=["shot.png"])


def _pass(fresh_root):
    return greenlight.graybox_verdict(
        fresh_root, verdict="pass", interesting=True,
        why="over four rooms the light decision kept changing what I did — "
            "twice I crossed blind and regretted it")


def _reach_production(fresh_root):
    greenlight.set_thesis(fresh_root, _thesis())
    greenlight.advance(fresh_root, greenlight.GRAYBOX)
    _submit(fresh_root)
    _pass(fresh_root)
    greenlight.advance(fresh_root, greenlight.PRODUCTION)
