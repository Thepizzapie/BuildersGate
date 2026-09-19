"""Canon: which world is current, which files are retired, and the teeth.

Written after a day on Meridian in which forty items were dispatched into a
tree carrying the first street beside the island that replaced it, and the
agents read the old one as the pattern to copy (58 references to main.tscn in
one item's log, 88 to a retired module in another). Nothing said which was
which. This is the list, the audit that reads the tree against it, and the
hook refusal that makes it more than a note.
"""
from __future__ import annotations

import os

import pytest

from bgate_cli import hook
from bgate_core.board import canon


def _game(root):
    game = root / "game"
    (game / "scenes" / "old").mkdir(parents=True, exist_ok=True)
    (game / "scenes" / "island").mkdir(parents=True, exist_ok=True)
    (game / "scripts").mkdir(exist_ok=True)
    (game / "tools").mkdir(exist_ok=True)
    (game / "project.godot").write_text(
        'config_version=5\n[application]\nrun/main_scene="res://scenes/island/world.tscn"\n',
        encoding="utf-8")
    (game / "scenes" / "island" / "world.tscn").write_text(
        '[gd_scene format=3]\n[ext_resource type="PackedScene" path="res://scenes/island/farm.tscn" id="1"]\n'
        '[ext_resource type="Script" path="res://scripts/story.gd" id="2"]\n',
        encoding="utf-8")
    (game / "scenes" / "island" / "farm.tscn").write_text("[gd_scene format=3]\n", encoding="utf-8")
    (game / "scenes" / "old" / "main.tscn").write_text(
        '[gd_scene format=3]\n[ext_resource type="Script" path="res://scripts/old_streets.gd" id="1"]\n',
        encoding="utf-8")
    (game / "scenes" / "old" / "farm.tscn").write_text("[gd_scene format=3]\n", encoding="utf-8")
    (game / "scenes" / "orphan.tscn").write_text("[gd_scene format=3]\n", encoding="utf-8")
    (game / "scripts" / "story.gd").write_text(
        'var cars := "res://scenes/vehicles/"\nvar old := preload("res://scripts/old_streets.gd")\n',
        encoding="utf-8")
    (game / "scripts" / "old_streets.gd").write_text("extends Node\n", encoding="utf-8")
    (game / "scenes" / "vehicles").mkdir(exist_ok=True)
    (game / "scenes" / "vehicles" / "sedan.tscn").write_text("[gd_scene format=3]\n", encoding="utf-8")
    (game / "tools" / "old_probe.gd").write_text(
        'var s = load("res://scenes/old/main.tscn")\n', encoding="utf-8")
    return game


class TestTheList:
    def test_world_defaults_to_the_main_scene(self, root):
        game = _game(root)
        assert canon.world(root, game) == "scenes/island/world.tscn"
        canon.set_world(root, "res://scenes/island/other.tscn")
        assert canon.world(root, game) == "scenes/island/other.tscn"

    def test_retired_matches_paths_dirs_and_globs(self, root):
        canon.retire(root, "scenes/old/main.tscn", successor="scenes/island/world.tscn",
                     reason="first street")
        canon.retire(root, "scripts/districts/**")
        canon.retire(root, "tools/build_*")
        assert canon.retired_match(root, "res://scenes/old/main.tscn")["successor"] == "scenes/island/world.tscn"
        assert canon.retired_match(root, "scripts\\districts\\north.gd")
        assert canon.retired_match(root, "tools/build_study.gd")
        assert canon.retired_match(root, "scenes/island/world.tscn") is None
        canon.unretire(root, "tools/build_*")
        assert canon.retired_match(root, "tools/build_study.gd") is None

    def test_the_block_names_world_entries_and_retired(self, root):
        game = _game(root)
        canon.set_entry(root, "reporter", "res://scenes/island/reporter.tscn", kind="scene")
        canon.retire(root, "scenes/old/main.tscn", successor="scenes/island/world.tscn",
                     reason="the first street")
        text = canon.block(root, game)
        assert "WORLD    scenes/island/world.tscn" in text
        assert "reporter   scenes/island/reporter.tscn [scene]" in text
        assert "    scenes/old/main.tscn\n      ->  use scenes/island/world.tscn  (the first street)" in text
        # Without an engine project the world line is absent but the list
        # still prints - retired files are retired wherever the game lives.
        no_game = canon.block(root, "")
        assert "WORLD" not in no_game and "RETIRED" in no_game

    def test_the_block_groups_retired_files_by_reason(self, root):
        """A hundred probes of the old street are one fact, not a hundred
        lines: grouped by successor+reason, the first few named, the rest
        counted."""
        for i in range(12):
            canon.retire(root, f"tools/old_probe_{i:02d}.gd", "tools/new_probe.gd", "old street")
        canon.retire(root, "scenes/main.tscn", "scenes/island.tscn", "replaced")
        text = canon.block(root, "")
        assert "+4 more" in text
        assert text.count("->  use tools/new_probe.gd") == 1
        assert "scenes/main.tscn\n      ->  use scenes/island.tscn  (replaced)" in text


class TestTheAudit:
    def test_references_duplicates_and_orphans(self, root):
        game = _game(root)
        canon.retire(root, "scripts/old_streets.gd", successor="scripts/story.gd")
        report = canon.audit(root, game)
        assert report["world"] == "scenes/island/world.tscn"
        # story.gd (live) still preloads the retired module, and so does the
        # old street's scene, which nobody retired: both named.
        assert report["references_to_retired"] == {
            "scenes/old/main.tscn": ["scripts/old_streets.gd"],
            "scripts/story.gd": ["scripts/old_streets.gd"]}
        # farm.tscn lives twice.
        assert report["duplicate_scenes"] == {
            "farm.tscn": ["scenes/island/farm.tscn", "scenes/old/farm.tscn"]}
        # orphan.tscn is reached by nothing; old/main.tscn is loaded by a
        # tool; vehicles/ is reached through the directory prefix story.gd
        # builds paths from; old/farm.tscn is reached by nothing either.
        assert report["unreachable_scenes"] == ["scenes/old/farm.tscn", "scenes/orphan.tscn"]

    def test_retired_files_are_not_reported_as_orphans(self, root):
        game = _game(root)
        canon.retire(root, "scenes/old/**")
        report = canon.audit(root, game)
        assert report["unreachable_scenes"] == ["scenes/orphan.tscn"]


class TestTheHook:
    def _payload(self, root, tool, path):
        key = "file_path"
        return {"tool_name": tool, "tool_input": {key: str(path)},
                "cwd": str(root), "session_id": "s1"}

    def test_a_seat_may_not_read_or_write_a_retired_file(self, root, monkeypatch):
        game = _game(root)
        canon.retire(root, "scenes/old/main.tscn", successor="scenes/island/world.tscn",
                     reason="first street")
        monkeypatch.setenv("BGATE_SEAT", "art")
        monkeypatch.setenv("BGATE_ROOT", str(root))
        target = game / "scenes" / "old" / "main.tscn"
        code, message = hook.decide(self._payload(root, "Read", target), "art", "item-1", "warn")
        assert code == hook.BLOCK
        assert "RETIRED" in message and "scenes/island/world.tscn" in message
        code, _ = hook.decide(self._payload(root, "Write", target), "art", "item-1", "warn")
        assert code == hook.BLOCK
        # ...and the current world is untouched by the gate.
        live = game / "scenes" / "island" / "world.tscn"
        code, _ = hook.decide(self._payload(root, "Read", live), "art", "item-1", "warn")
        assert code != hook.BLOCK

    def test_a_relative_path_is_judged_in_the_engine_projects_frame(self, root, monkeypatch):
        game = _game(root)
        canon.retire(root, "scenes/old/main.tscn")
        monkeypatch.setenv("BGATE_SEAT", "art")
        monkeypatch.setenv("BGATE_ROOT", str(root))
        payload = {"tool_name": "Read", "tool_input": {"file_path": "game/scenes/old/main.tscn"},
                   "cwd": str(root), "session_id": "s1"}
        assert hook.decide(payload, "art", "item-1", "warn")[0] == hook.BLOCK

    def test_the_human_session_is_not_gated(self, root, monkeypatch):
        game = _game(root)
        canon.retire(root, "scenes/old/main.tscn")
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        monkeypatch.setenv("BGATE_ROOT", str(root))
        target = game / "scenes" / "old" / "main.tscn"
        code, _ = hook.decide(self._payload(root, "Read", target), hook.DIRECTOR_SEAT,
                              "session:abc", "collide")
        assert code != hook.BLOCK
