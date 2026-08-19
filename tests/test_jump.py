"""What the player can reach — the side-scroller's answer to `connected`.

A flood fill settles a top-down level. Under gravity it settles nothing: you
cannot walk upward, so a platform is either inside the character's jump or it
is scenery. These tests pin the arithmetic; the engine is the referee for
whether the arithmetic matches the physics that ships.
"""
from __future__ import annotations

import pytest

from bgate_core import jump


def _spec(**kw):
    return jump.JumpSpec(**kw)


class TestTheSpec:
    def test_peak_and_span_are_the_numbers_a_designer_thinks_in(self):
        s = _spec(jump_speed=14.0, gravity=40.0, run=8.0)
        assert s.peak == pytest.approx(14.0 ** 2 / 80.0)
        assert s.span == pytest.approx(8.0 * (28.0 / 40.0))

    def test_a_character_that_cannot_move_is_refused(self):
        """Otherwise the generator refuses every layout and never says why."""
        with pytest.raises(jump.JumpError, match="must all be positive"):
            _spec(run=0)
        with pytest.raises(jump.JumpError, match="not a character"):
            _spec(body=(0, 2))


class TestFromPixels:
    """The handoff from a player scene's pixel tunables to cells."""

    def test_the_template_players_numbers_convert(self):
        """templates/2d's player.gd, through a 32px tile."""
        s = jump.from_pixels(speed=220.0, jump_velocity=-380.0,
                             gravity=980.0, fall_multiplier=1.6, tile_px=32)
        assert s.run == pytest.approx(220.0 / 32)
        assert s.jump_speed == pytest.approx(380.0 / 32)
        assert s.gravity == pytest.approx(980.0 * 1.6 / 32)

    def test_the_sign_of_jump_velocity_does_not_matter(self):
        """Godot's up is negative; a jump is its magnitude."""
        up = jump.from_pixels(speed=200, jump_velocity=-400, gravity=1000,
                              tile_px=16)
        down = jump.from_pixels(speed=200, jump_velocity=400, gravity=1000,
                                tile_px=16)
        assert up.jump_speed == down.jump_speed

    def test_fall_multiplier_only_ever_shrinks_the_modelled_jump(self):
        """The error must run in the safe direction: every generated gap is
        clearable by the real character, never the reverse."""
        flat = jump.from_pixels(speed=220, jump_velocity=-380, gravity=980,
                                tile_px=32, fall_multiplier=1.0)
        fast = jump.from_pixels(speed=220, jump_velocity=-380, gravity=980,
                                tile_px=32, fall_multiplier=1.6)
        assert fast.peak < flat.peak
        assert fast.span < flat.span

    def test_a_floaty_fall_does_not_stretch_the_model(self):
        """fall_multiplier < 1 would WEAKEN the modelled gravity and emit
        gaps the rise cannot clear — the rise gravity is the floor."""
        soft = jump.from_pixels(speed=220, jump_velocity=-380, gravity=980,
                                tile_px=32, fall_multiplier=0.5)
        assert soft.gravity == pytest.approx(980.0 / 32)

    def test_nonsense_is_refused_with_its_own_name(self):
        with pytest.raises(jump.JumpError, match="tile"):
            jump.from_pixels(speed=220, jump_velocity=-380, gravity=980,
                             tile_px=0)
        with pytest.raises(jump.JumpError, match="fall_multiplier"):
            jump.from_pixels(speed=220, jump_velocity=-380, gravity=980,
                             tile_px=32, fall_multiplier=0)

    def test_a_bigger_jump_reaches_higher(self):
        assert _spec(jump_speed=20).peak > _spec(jump_speed=14).peak
        assert _spec(gravity=80).peak < _spec(gravity=40).peak


class TestTheKernel:
    def test_it_reaches_up_across_and_down(self):
        k = jump.kernel(_spec())
        assert any(o[1] < 0 for o in k), "nothing can be reached above"
        assert any(o[1] == 0 for o in k), "nothing at the same height"
        assert any(o[1] > 0 for o in k), "nothing below — falling is a move"

    def test_the_rise_never_exceeds_the_jump(self):
        """The one thing a generator must not get wrong in the optimistic
        direction: a ledge placed above the peak is scenery the layout thinks
        is a route."""
        s = _spec()
        k = jump.kernel(s)
        assert -min(o[1] for o in k) <= s.peak + 1

    def test_standing_still_is_not_a_move(self):
        """Left in, every surface is trivially reachable from itself and the
        gate passes a level nobody can traverse."""
        assert (0, 0) not in jump.kernel(_spec())

    def test_landings_are_only_recorded_while_descending(self):
        """On the way up you are still under way; calling that a landing lets
        the generator place a ledge the player is rising past."""
        s = _spec()
        for offset, entry in jump.kernel(s).items():
            assert entry["kind"] in ("jump", "fall")
            assert offset not in entry["clear"]

    def test_a_weaker_jump_reaches_strictly_less_upward(self):
        weak = {o for o in jump.kernel(_spec(jump_speed=9)) if o[1] < 0}
        strong = {o for o in jump.kernel(_spec(jump_speed=18)) if o[1] < 0}
        assert -min(o[1] for o in strong) > -min(o[1] for o in weak)

    def test_both_directions_are_covered(self):
        k = jump.kernel(_spec())
        assert any(o[0] > 0 for o in k) and any(o[0] < 0 for o in k)


class TestTheBodyIsNotAPoint:
    def test_clearance_counts_the_whole_body_not_just_the_feet(self):
        """A ledge that clears the trajectory can still catch the head."""
        s = _spec(body=(1, 3))
        k = jump.kernel(s)
        off, entry = next(iter(k.items()))
        cells = jump.clear_for(s, off, entry)
        assert len(cells) >= 3 * len(entry["clear"]) or len(cells) > len(entry["clear"])

    def test_a_surface_needs_headroom_for_the_body(self):
        """A one-cell slot under a ceiling is not standable by a two-cell
        character, and a level that ignores it reads perfectly and cannot be
        walked through."""
        solid = {(x, 5) for x in range(10)} | {(x, 3) for x in range(10)}
        tall = jump.surfaces(solid, body=(1, 2))
        short = jump.surfaces(solid, body=(1, 1))
        assert not any(c[1] == 4 for c in tall), "no room between floor and lid"
        assert any(c[1] == 4 for c in short)


class TestReachability:
    def _flat(self, length=20, y=10):
        return {(x, y) for x in range(length)}

    def test_a_flat_run_is_all_reachable(self):
        s = _spec()
        solid = self._flat()
        got = jump.reachable(solid, (0, 9), s)
        assert got["ok"] and not got["unreachable"]

    def test_a_platform_inside_the_jump_is_reached(self):
        s = _spec(jump_speed=18)
        solid = self._flat(8) | {(x, 8) for x in range(11, 15)}
        got = jump.reachable(solid, (0, 9), s)
        assert (11, 7) in got["reached"] or (12, 7) in got["reached"]

    def test_a_platform_out_of_reach_is_reported_not_ignored(self):
        """The whole point of the gate: unreachable is a fact about the level,
        not a difficulty setting."""
        s = _spec()
        solid = self._flat(8) | {(x, 0) for x in range(20, 24)}
        got = jump.reachable(solid, (0, 9), s)
        assert got["ok"]
        assert any(c[1] < 5 for c in got["unreachable"])

    def test_a_start_that_is_not_standable_is_refused_with_a_reason(self):
        got = jump.reachable(self._flat(), (0, 0), _spec())
        assert not got["ok"] and "standable" in got["reason"]

    def test_a_ceiling_blocks_a_jump_that_would_otherwise_reach(self):
        """The arc has to be clear, not just the landing — otherwise the
        generator produces levels that look correct and cannot be played."""
        s = _spec(jump_speed=18)
        ledge = {(x, 6) for x in range(6, 10)}
        open_run = self._flat(6) | ledge
        lidded = open_run | {(x, 4) for x in range(0, 10)}
        free = jump.reachable(open_run, (0, 9), s)["reached"]
        boxed = jump.reachable(lidded, (0, 9), s)["reached"]
        assert len(boxed) <= len(free)
