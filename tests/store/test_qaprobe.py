"""A thin or wrong probe contract must be VISIBLY wrong.

The old hardcoded probe's failure was silence: every game that was not the
fighter got a bot that could not fail because it sampled nothing. Derivation
replaced the hardcoding, but derivation has its own quiet failure modes, and
these tests pin the rule that each of them says so out loud:

* actors whose state is built at runtime derive a contract with nothing to
  sample — ``derived_thin: true`` plus a reason, never empty-but-confident;
* a scene that merely names two nodes Player and Opponent still gets the
  pinned fighter contract (baselines address those six keys), but when the
  scripts declare none of the fighter's properties the pin is flagged, not
  silent;
* the flag survives persistence: a stored machine guess is re-checked on
  every load, not just the first.
"""
from __future__ import annotations

import pytest

from bgate_core.store import project
from bgate_core.runtime import qaprobe

_FIGHT_SCENE = '''[gd_scene load_steps=3 format=3]

[ext_resource type="Script" path="res://scripts/fight.gd" id="1_fight"]
[ext_resource type="Script" path="res://scripts/boxer.gd" id="2_boxer"]

[node name="Main" type="Node2D"]
script = ExtResource("1_fight")

[node name="Player" type="Node2D" parent="."]
script = ExtResource("2_boxer")

[node name="Opponent" type="Node2D" parent="."]
script = ExtResource("2_boxer")
'''

# Two nodes NAMED Player and Opponent, in a game that is nothing like the
# fighter: no hp, no stamina, anywhere.
_CHESS_SCENE = '''[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://scripts/chess.gd" id="1_chess"]

[node name="Board" type="Node2D"]
script = ExtResource("1_chess")

[node name="Player" type="Node2D" parent="."]
script = ExtResource("1_chess")

[node name="Opponent" type="Node2D" parent="."]
script = ExtResource("1_chess")
'''

# An actor node (role: enemy, via its script name) that is a plain Node — no
# position to sample — whose script declares no numeric state. The shape of a
# game that spawns its real actors at runtime.
_SPAWNER_SCENE = '''[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://scripts/enemy_brain.gd" id="1_brain"]

[node name="World" type="Node"]

[node name="EnemyBrain" type="Node" parent="."]
script = ExtResource("1_brain")
'''


def _write_game(tmp_path, *, main_scene: str, scenes: dict, scripts: dict):
    game = tmp_path / "game"
    (game / "scenes").mkdir(parents=True, exist_ok=True)
    (game / "scripts").mkdir(parents=True, exist_ok=True)
    (game / "project.godot").write_text(
        f'[application]\nrun/main_scene="{main_scene}"\n', encoding="utf-8")
    for name, text in scenes.items():
        (game / "scenes" / name).write_text(text, encoding="utf-8")
    for name, text in scripts.items():
        (game / "scripts" / name).write_text(text, encoding="utf-8")
    return game


class TestThinDerivationIsVisible:
    def test_a_healthy_derivation_is_not_thin(self, tmp_path):
        game = _write_game(
            tmp_path, main_scene="res://scenes/main.tscn",
            scenes={"main.tscn": _FIGHT_SCENE},
            scripts={"fight.gd": "func sim_tick() -> void:\n\tpass\n",
                     "boxer.gd": "var hp: float\nvar stamina: float\n"})
        c = qaprobe.derive(game)
        assert c["derived_thin"] is False
        assert c["issues"] == []

    def test_runtime_spawned_actors_derive_thin_and_say_so(self, tmp_path):
        """Actors with nothing to sample used to come back empty-but-confident:
        actors declared, samples [], no complaint anywhere."""
        game = _write_game(
            tmp_path, main_scene="res://scenes/world.tscn",
            scenes={"world.tscn": _SPAWNER_SCENE},
            scripts={"enemy_brain.gd": "var targets := []\n"})
        c = qaprobe.derive(game)
        assert c["actors"] and not c["samples"]
        assert c["derived_thin"] is True
        joined = " ".join(c["issues"])
        assert "numeric property" in joined
        assert "runtime" in joined

    def test_no_scene_at_all_is_thin_and_mentions_runtime_spawning(self, tmp_path):
        game = _write_game(tmp_path, main_scene="", scenes={}, scripts={})
        c = qaprobe.derive(game)
        assert c["source"] == "none"
        assert c["derived_thin"] is True
        joined = " ".join(c["issues"])
        assert "spawns its actors at runtime" in joined
        assert "Declare the probe contract by hand" in joined


class TestThePinnedFighterShapeIsNeverSilent:
    def test_a_real_fighter_keeps_the_pin_with_no_complaint(self, tmp_path):
        game = _write_game(
            tmp_path, main_scene="res://scenes/main.tscn",
            scenes={"main.tscn": _FIGHT_SCENE},
            scripts={"fight.gd": "func sim_tick() -> void:\n\tpass\n",
                     "boxer.gd": "var hp := 100.0\nvar stamina := 50.0\n"})
        c = qaprobe.derive(game)
        assert c["shape"] == "fight"
        assert c["derived_thin"] is False
        assert c["issues"] == []

    def test_player_and_opponent_without_fighter_state_is_flagged(self, tmp_path):
        """The pin is kept — six keys of baseline history address it — but a
        chess game must not get a silently fighter-shaped contract."""
        game = _write_game(
            tmp_path, main_scene="res://scenes/board.tscn",
            scenes={"board.tscn": _CHESS_SCENE},
            scripts={"chess.gd": "var selected_square := 0\n"})
        c = qaprobe.derive(game)
        assert c["shape"] == "fight"
        assert qaprobe.sample_keys(c) == [
            "player_x", "opponent_x", "player_hp", "opponent_hp",
            "player_stamina", "distance"]     # the pin itself is untouched
        assert c["derived_thin"] is True
        joined = " ".join(c["issues"])
        assert "Player.hp" in joined and "Player.stamina" in joined
        assert "null" in joined


class TestTheFlagSurvivesPersistence:
    def test_a_stored_machine_guess_is_rechecked_on_every_load(self, tmp_path):
        """load() persists a derivation on first read. Without the re-check the
        second read would report the same thin guess with the flag and the
        reason both gone — a machine guess dressed as a human's declaration."""
        root = tmp_path / "proj"
        root.mkdir()
        project.init(str(root), "ThinProbe", dimension="2d")
        game = _write_game(
            root, main_scene="res://scenes/board.tscn",
            scenes={"board.tscn": _CHESS_SCENE},
            scripts={"chess.gd": "var selected_square := 0\n"})

        first = qaprobe.load(root, game)
        assert first["derived_thin"] is True

        second = qaprobe.load(root, game)   # now served from the store
        assert second["source"] == "derived"
        assert second["derived_thin"] is True
        assert any("null" in i for i in second["issues"])


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BGATE_HOME", str(tmp_path / "bgate_home"))
