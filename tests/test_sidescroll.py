"""A side-scrolling level, and whether it can be played.

The gate is the point. A top-down level is playable when its floor is one
region; under gravity that question is meaningless, because you cannot walk
upward. These tests pin that the generator builds INSIDE the character's jump
and that the checks refuse a level the character cannot finish.
"""
from __future__ import annotations

import pytest

from bgate_core import jump
from bgate_core import sidescroll as ss

MARIO = dict(run=9.0, jump_speed=18.0, gravity=40.0)


def _spec(**kw):
    return jump.JumpSpec(**{**MARIO, **kw})


class TestLimitsComeFromTheJump:
    def test_they_are_read_off_the_kernel_not_guessed(self):
        lim = ss.Limits(_spec())
        assert lim.gap >= 2 and lim.rise >= 1
        assert lim.rise <= _spec().peak + 1

    def test_a_bigger_jump_clears_a_wider_pit(self):
        assert ss.Limits(_spec(jump_speed=22, run=12)).gap > \
            ss.Limits(_spec(jump_speed=12, run=6)).gap

    def test_a_character_with_no_platformer_in_it_is_refused(self):
        with pytest.raises(ss.LevelError, match="no platformer in that"):
            ss.Limits(jump.JumpSpec(run=1.0, jump_speed=2.0, gravity=60.0))


class TestTheGeneratorBuildsInsideTheJump:
    def test_blocks_a_weak_jump_cannot_use_are_not_generated(self):
        """Found by the engine acceptance run: a real player's fall
        multiplier converts to a jump that rises 1 cell, and blocks kept
        their 3-cell head clearance — standable cells nothing could reach,
        refused as `stranded` on every seed. The segment must degrade to
        flat, not generate its own refusal."""
        weak = jump.JumpSpec(run=6.875, jump_speed=11.875, gravity=49.0)
        assert ss.Limits(weak).rise < 3
        for seed in range(6):
            level = ss.plan(120, 16, seed=seed, spec=weak,
                            kinds=("blocks", "flat"))
            verdict = ss.check(level, weak)
            assert verdict["ok"], (seed, verdict["findings"])
        assert all(s["kind"] != "blocks"
                   for s in level["segments"])

    def test_blocks_still_appear_for_a_jump_that_can_use_them(self):
        lim = ss.Limits(_spec())
        assert lim.rise >= 3
        level = ss.plan(160, 16, seed=1, spec=_spec(), kinds=("blocks",))
        assert any(s["kind"] == "blocks" for s in level["segments"])
        assert ss.check(level, _spec())["ok"]


    def test_a_level_too_small_to_run_in_is_refused(self):
        with pytest.raises(ss.LevelError, match="too small to hold a run"):
            ss.plan(20, 16)

    def test_no_pit_is_wider_than_the_character_can_clear(self):
        """UNREPRESENTABLE, not caught afterwards: the segment sizes itself
        from the kernel."""
        lim = ss.Limits(_spec())
        for seed in range(6):
            lvl = ss.plan(160, 16, seed=seed, spec=_spec(), difficulty=1.0)
            for seg in lvl["segments"]:
                if seg["kind"] == "pit":
                    assert seg["width"] <= lim.gap, seg

    def test_no_pipe_is_taller_than_a_jump(self):
        lim = ss.Limits(_spec())
        for seed in range(6):
            lvl = ss.plan(160, 16, seed=seed, spec=_spec())
            for seg in lvl["segments"]:
                if seg["kind"] == "pipe":
                    assert seg["height"] <= lim.rise, seg

    def test_the_spawn_is_on_the_ground_it_starts_at(self):
        """`ground` is reassigned by every segment that changes level, so
        reading it after the loop put the spawn five cells in the air in a
        level whose every other number was correct."""
        lvl = ss.plan(160, 16, seed=1, spec=_spec())
        solid = {tuple(c) for c in lvl["solid"]}
        sp = tuple(lvl["spawn"])
        assert sp not in solid and (sp[0], sp[1] + 1) in solid

    def test_the_same_seed_builds_the_same_level(self):
        a = ss.plan(120, 16, seed=3, spec=_spec())
        b = ss.plan(120, 16, seed=3, spec=_spec())
        assert a["solid"] == b["solid"] and a["segments"] == b["segments"]

    def test_difficulty_tightens_rather_than_breaks(self):
        lim = ss.Limits(_spec())
        easy = ss.plan(200, 16, seed=5, spec=_spec(), difficulty=0.0)
        hard = ss.plan(200, 16, seed=5, spec=_spec(), difficulty=1.0)
        for lvl in (easy, hard):
            for seg in lvl["segments"]:
                if seg["kind"] == "pit":
                    assert seg["width"] <= lim.gap


class TestTheGate:
    def _level(self, seed=1):
        return ss.plan(160, 16, seed=seed, spec=_spec())

    def test_a_generated_level_is_playable(self):
        for seed in (1, 2, 3):
            got = ss.check(self._level(seed), _spec())
            assert got["ok"], got["findings"]
            assert got["goal_reachable"]

    def test_a_weaker_character_cannot_finish_and_it_says_so(self):
        """The gate is about a level AND a character together — neither is
        playable or unplayable alone."""
        got = ss.check(self._level(), jump.JumpSpec(run=4.0, jump_speed=9.0,
                                                    gravity=40.0))
        kinds = {f["kind"] for f in got["findings"]}
        assert "goal_unreachable" in kinds
        assert not got["goal_reachable"]

    def test_a_platform_outside_the_jump_is_reported_not_counted(self):
        """In a HAND-MADE level an unreachable ledge is background; in a
        generated one it is a set piece nobody can play."""
        lvl = self._level()
        lvl = {**lvl, "solid": sorted({tuple(c) for c in lvl["solid"]}
                                      | {(60, 1), (61, 1), (62, 1)})}
        got = ss.check(lvl, _spec())
        assert any(f["kind"] == "stranded" for f in got["findings"])

    def test_a_spawn_in_mid_air_is_refused_with_a_reason(self):
        lvl = {**self._level(), "spawn": [2, 0]}
        got = ss.check(lvl, _spec())
        assert not got["ok"]
        assert got["findings"][0]["kind"] == "no_spawn"

    def test_the_check_reads_the_spec_off_the_level_when_not_given_one(self):
        lvl = self._level()
        assert ss.check(lvl)["ok"]


class TestAsciiMap:
    def test_it_shows_the_shape_before_any_art_is_bought(self):
        lvl = ss.plan(120, 16, seed=4, spec=_spec())
        art = ss.ascii_map(lvl, width=60)
        rows = art.splitlines()
        assert len(rows) == 16 and all(len(r) == 60 for r in rows)
        assert "S" in art and "#" in art and "." in art
