"""ITEM #17: gate the EXPORT, not the editor.

Every test here follows the failure end: EXIT 67 shipped an empty export
while every gate that ran was against the loose project directory. So the
gate must FIRE when no verification has ever run, FIRE when the source has
moved on since the last one, and clear only when a fresh, passing
verification postdates every .gd/.tscn on disk.
"""
from __future__ import annotations

import os
import time

from bgate_core.qa import exportgate


def _touch(path, contents="extends Node\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


def _game(tmp_path, name="game"):
    game = tmp_path / name
    _touch(game / "project.godot", "config_version=5\n")
    return game


class TestUnmet:
    def test_never_verified_is_blocking(self, root, tmp_path):
        game = _game(tmp_path)
        _touch(game / "player.gd")
        rows = exportgate.unmet(root, str(game))
        assert rows and rows[0]["kind"] == "blocking"
        assert "godot_export_verify" in rows[0]["clears_by"]

    def test_a_fresh_pass_clears_it(self, root, tmp_path):
        game = _game(tmp_path)
        _touch(game / "player.gd")
        exportgate.record(root, godot_project=str(game), scene="res://main.tscn",
                          pck="out.pck", ok=True, diffs=0)
        assert exportgate.unmet(root, str(game)) == []

    def test_a_source_edit_after_verification_reopens_it(self, root, tmp_path):
        game = _game(tmp_path)
        script = _touch(game / "player.gd")
        exportgate.record(root, godot_project=str(game), scene="res://main.tscn",
                          pck="out.pck", ok=True, diffs=0)
        assert exportgate.unmet(root, str(game)) == []

        # Touch the script AFTER verification with a later mtime.
        future = time.time() + 5
        script.write_text("extends Node\n# edited\n", encoding="utf-8")
        os.utime(script, (future, future))

        rows = exportgate.unmet(root, str(game))
        assert rows and any("changed after" in r["claim"] for r in rows)

    def test_a_failing_verification_stays_blocking_even_when_fresh(self, root, tmp_path):
        game = _game(tmp_path)
        _touch(game / "player.gd")
        exportgate.record(root, godot_project=str(game), scene="res://main.tscn",
                          pck="out.pck", ok=False, diffs=3)
        rows = exportgate.unmet(root, str(game))
        assert rows and any("diff" in r["claim"] for r in rows)

    def test_the_dot_godot_cache_does_not_count_as_source(self, root, tmp_path):
        game = _game(tmp_path)
        _touch(game / "player.gd")
        exportgate.record(root, godot_project=str(game), scene="res://main.tscn",
                          pck="out.pck", ok=True, diffs=0)
        future = time.time() + 5
        cache = _touch(game / ".godot" / "generated" / "stub.gd")
        os.utime(cache, (future, future))
        assert exportgate.unmet(root, str(game)) == []

    def test_no_godot_project_is_not_this_sections_question(self, root, tmp_path):
        not_a_project = tmp_path / "not_a_game"
        not_a_project.mkdir()
        assert exportgate.unmet(root, str(not_a_project)) == []


class TestRecordAndLast:
    def test_record_is_keyed_per_project(self, root, tmp_path):
        game_a = tmp_path / "a"
        game_b = tmp_path / "b"
        game_a.mkdir()
        game_b.mkdir()
        exportgate.record(root, godot_project=str(game_a), scene="", pck="a.pck", ok=True)
        assert exportgate.last(root, str(game_a)) is not None
        assert exportgate.last(root, str(game_b)) is None
