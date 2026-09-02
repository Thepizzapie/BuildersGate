"""Adoption is defined by what it refuses to do.

The user this exists for has months of work in the directory. Every test here
is really the same assertion from a different angle: nothing they wrote changed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bgate_cli import main as cli
from bgate_core.store import adopt, db, project

PROJECT_GODOT_3D = """\
config_version=5

[application]

config/name="Wanderer"
run/main_scene="res://scenes/world.tscn"
config/features=PackedStringArray("4.3", "Forward Plus")
"""

SCENE_3D = """\
[gd_scene load_steps=2 format=3]

[node name="World" type="Node3D"]
[node name="Camera" type="Camera3D" parent="."]
[node name="Player" type="CharacterBody3D" parent="."]
[node name="Ground" type="MeshInstance3D" parent="."]
[node name="HUD" type="CanvasLayer" parent="."]
"""

SCENE_2D = """\
[gd_scene load_steps=2 format=3]

[node name="Menu" type="Node2D"]
[node name="Logo" type="Sprite2D" parent="."]
"""


@pytest.fixture()
def game(tmp_path) -> Path:
    """A directory that looks like somebody's half-finished 3D Godot game."""
    base = tmp_path / "wanderer"
    (base / "scenes").mkdir(parents=True)
    (base / "scripts").mkdir()
    (base / "assets").mkdir()
    (base / "project.godot").write_text(PROJECT_GODOT_3D, encoding="utf-8")
    (base / "scenes" / "world.tscn").write_text(SCENE_3D, encoding="utf-8")
    (base / "scenes" / "menu.tscn").write_text(SCENE_2D, encoding="utf-8")
    (base / "scripts" / "player.gd").write_text(
        "extends CharacterBody3D\n", encoding="utf-8")
    (base / "assets" / "hero.png").write_bytes(b"\x89PNG not really")
    yield base
    db.close_all()


class TestDetect:
    def test_finds_the_godot_project_and_reads_it(self, game):
        found = adopt.detect(game)
        assert found["godot"] is True
        assert found["godot_name"] == "Wanderer"
        assert found["godot_version"] == "4.3"
        assert found["main_scene"] == "res://scenes/world.tscn"

    def test_calls_a_3d_game_3d(self, game):
        """A 3D game with a 2D menu is still a 3D game — the answer people get
        wrong by counting scenes instead of nodes."""
        found = adopt.detect(game)
        assert found["dimension"] == "3d"
        assert found["dimension_evidence"]["3d_nodes"] > 0

    def test_counts_the_work(self, game):
        found = adopt.detect(game)
        assert found["scenes"] == 2
        assert found["scripts"] == 1
        assert found["images"] == 1
        assert found["bytes"] > 0
        assert "scenes" in found["top_dirs"]

    def test_a_directory_with_no_engine_is_not_a_lie(self, tmp_path):
        found = adopt.detect(tmp_path)
        assert found["godot"] is False
        assert found["godot_dir"] is None

    def test_import_cache_does_not_count_as_the_users_work(self, game):
        junk = game / ".godot" / "imported"
        junk.mkdir(parents=True)
        (junk / "blob.res").write_bytes(b"x" * 5000)
        assert ".godot" not in adopt.detect(game)["top_dirs"]
        assert adopt.detect(game)["bytes"] < 5000


class TestAdoptDoesNotClobber:
    def test_scaffold_files_are_never_written(self, game):
        before = {p.name for p in game.rglob("*") if p.is_file()}
        adopt.adopt(game)
        after = {p.name for p in game.rglob("*") if p.is_file()}
        # Only additive, and only the three things adoption documents.
        assert after - before <= {"game.db", "game.db-wal", "game.db-shm",
                                  ".gitignore", "CLAUDE.md"}
        assert "player.tscn" not in after  # i.e. no template landed

    def test_existing_user_files_are_byte_identical_afterwards(self, game):
        originals = {p: p.read_bytes()
                     for p in game.rglob("*") if p.is_file()}
        adopt.adopt(game)
        for path, content in originals.items():
            assert path.read_bytes() == content, path

    def test_existing_gitignore_is_merged_not_replaced(self, game):
        (game / ".gitignore").write_text("# mine\n*.tmp\n", encoding="utf-8")
        adopt.adopt(game)
        text = (game / ".gitignore").read_text(encoding="utf-8")
        assert "*.tmp" in text        # theirs survived
        assert ".env" in text         # ours arrived
        assert adopt.MARK_START in text

    def test_existing_claude_md_is_merged_not_replaced(self, game):
        (game / "CLAUDE.md").write_text(
            "# House rules\nAlways use tabs.\n", encoding="utf-8")
        adopt.adopt(game)
        text = (game / "CLAUDE.md").read_text(encoding="utf-8")
        assert "Always use tabs." in text
        assert "queue_next" in text

    def test_refuses_when_a_managed_path_is_a_directory(self, game):
        (game / "CLAUDE.md").mkdir()
        with pytest.raises(FileExistsError, match="refusing to adopt"):
            adopt.adopt(game)

    def test_refuses_a_path_that_is_not_a_directory(self, tmp_path):
        target = tmp_path / "notadir.txt"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(NotADirectoryError):
            adopt.adopt(target)


class TestAdoptIsIdempotent:
    def test_second_run_changes_nothing_it_should_not(self, game):
        adopt.adopt(game)
        first_ignore = (game / ".gitignore").read_text(encoding="utf-8")
        first_md = (game / "CLAUDE.md").read_text(encoding="utf-8")

        second = adopt.adopt(game)
        assert second["already_adopted"] is True
        assert (game / ".gitignore").read_text(encoding="utf-8") == first_ignore
        assert (game / "CLAUDE.md").read_text(encoding="utf-8") == first_md
        # One block, not two.
        assert first_md.count(adopt.MD_MARK_START) == 1
        assert second["written"]["claude_md"]["action"] == "unchanged"

    def test_re_adopting_keeps_a_pitch_the_user_set_later(self, game):
        adopt.adopt(game, pitch="a lonely walk home")
        again = adopt.adopt(game)
        assert again["project"]["pitch"] == "a lonely walk home"


class TestAdoptRecords:
    def test_the_detected_dimension_seeds_the_project(self, game):
        record = adopt.adopt(game)["project"]
        assert record["dimension"] == "3d"
        assert record["name"] == "Wanderer"   # from config/name
        assert record["engine"] == "godot"

    def test_explicit_name_and_dimension_win(self, game):
        record = adopt.adopt(game, name="Other", dimension="2d")["project"]
        assert record["name"] == "Other"
        assert record["dimension"] == "2d"

    def test_no_godot_means_engine_none_rather_than_a_broken_godot_project(
            self, tmp_path):
        (tmp_path / "notes.md").write_text("someday", encoding="utf-8")
        record = adopt.adopt(tmp_path)["project"]
        assert record["engine"] == "none"
        db.close_all()

    def test_the_store_actually_opens(self, game):
        adopt.adopt(game)
        assert (game / ".bgate" / "game.db").is_file()
        assert project.get(game)["name"] == "Wanderer"


class TestActiveProject:
    def test_use_survives_the_process(self, game):
        adopt.adopt(game)
        project.clear_active()
        assert project.active_root() is None
        project.set_active(game)
        assert project.active_root() == game.resolve()

    def test_use_refuses_a_directory_that_is_not_a_project(self, tmp_path):
        with pytest.raises(LookupError, match="not a Builders Gate project"):
            project.set_active(tmp_path)

    def test_a_pointer_at_a_deleted_project_degrades_to_no_pointer(
            self, game, tmp_path):
        adopt.adopt(game)
        project.set_active(game)
        db.close_all()
        (game / ".bgate" / "game.db").unlink()
        assert project.active_root() is None

    def test_require_root_falls_back_to_the_active_project(self, game, tmp_path):
        """The whole point of `bgate use`: work from a directory that is
        nowhere near the game, without exporting BGATE_ROOT."""
        adopt.adopt(game)
        elsewhere = tmp_path / "somewhere-else"
        elsewhere.mkdir()
        assert project.require_root(elsewhere) == game.resolve()

    def test_standing_inside_a_project_still_beats_the_pointer(
            self, game, tmp_path):
        """Precedence must not change: cwd walk-up outranks the preference."""
        other = tmp_path / "other"
        other.mkdir()
        project.init(other, "Other Game")
        adopt.adopt(game)                      # sets active to `game`
        assert project.active_root() == game.resolve()
        assert project.require_root(other) == other.resolve()

    def test_no_pointer_and_no_project_still_raises(self, tmp_path):
        with pytest.raises(LookupError, match="no .bgate project"):
            project.require_root(tmp_path)


class TestCli:
    def test_adopt_then_use_then_projects(self, game, capsys, monkeypatch):
        assert cli.main.__module__  # sanity: the module imported
        monkeypatch.setattr("sys.argv", ["bgate", "adopt", str(game)])
        assert cli.main() == 0
        out = capsys.readouterr().out
        assert "adopted Wanderer" in out
        assert "3d" in out
        assert "project.godot" not in out or "godot" in out

        monkeypatch.setattr("sys.argv", ["bgate", "projects", "--json"])
        assert cli.main() == 0
        listed = json.loads(capsys.readouterr().out)
        assert listed["active"] == str(game.resolve())
        assert any(row["active"] for row in listed["projects"])

        monkeypatch.setattr("sys.argv", ["bgate", "use", str(game), "--json"])
        assert cli.main() == 0
        assert json.loads(capsys.readouterr().out)["active"] == str(game.resolve())

    def test_use_accepts_a_registered_name(self, game, capsys, monkeypatch):
        adopt.adopt(game)
        slug = project.get(game)["slug"]
        monkeypatch.setattr("sys.argv", ["bgate", "use", slug, "--json"])
        assert cli.main() == 0
        assert json.loads(capsys.readouterr().out)["active"] == str(game.resolve())

    def test_use_on_a_plain_directory_fails_loudly(self, tmp_path, capsys,
                                                   monkeypatch):
        monkeypatch.setattr("sys.argv", ["bgate", "use", str(tmp_path)])
        assert cli.main() == 1
        assert "not a Builders Gate project" in capsys.readouterr().out

    def test_adopt_json_carries_the_detection_report(self, game, capsys,
                                                     monkeypatch):
        monkeypatch.setattr("sys.argv", ["bgate", "adopt", str(game), "--json"])
        assert cli.main() == 0
        report = json.loads(capsys.readouterr().out)
        assert report["detected"]["dimension"] == "3d"
        assert report["written"]["claude_md"]["action"] == "created"

    def test_projects_with_none_known_says_what_to_do(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["bgate", "projects"])
        assert cli.main() == 0
        out = capsys.readouterr().out
        assert "bgate adopt" in out


class TestStampedBriefing:
    """The CLAUDE.md is the deliverable, not a nice-to-have — it is the only
    thing a first-time user reads before their session starts guessing."""

    SOURCE = Path(__file__).resolve().parents[2] / "src" / "templates" / "shared" / "CLAUDE.md"

    def test_the_template_exists(self):
        assert self.SOURCE.is_file()

    def test_it_stays_skimmable(self):
        lines = self.SOURCE.read_text(encoding="utf-8").splitlines()
        assert len(lines) < 250, "a briefing nobody finishes is a briefing nobody reads"

    def test_it_only_names_tools_that_exist(self):
        """Every backticked identifier that looks like an MCP tool must be one.
        A briefing that tells a session to call a tool that does not exist
        teaches it to distrust the whole document."""
        import re

        from bgate_mcp import server

        real = {name for name in dir(server) if not name.startswith("_")}
        text = self.SOURCE.read_text(encoding="utf-8")
        cited = set(re.findall(r"`([a-z][a-z0-9_]*)\(", text))
        cited |= {m for m in re.findall(r"`([a-z][a-z0-9_]+)`", text)
                  if "_" in m and not m.startswith("bgate ")}
        unknown = {name for name in cited if name not in real}
        assert not unknown, f"CLAUDE.md cites non-existent tools: {sorted(unknown)}"

    def test_it_covers_the_things_a_beginner_needs(self):
        text = self.SOURCE.read_text(encoding="utf-8")
        for topic in ("seat", "bible", "lore", "queue", "consistency_check",
                      "ref_pin", "canon_check", "What NOT to do"):
            assert topic in text, topic

    def test_scaffolded_projects_get_it_with_the_real_name(self, tmp_path):
        from bgate_core.store import scaffold

        dest = tmp_path / "fresh"
        scaffold.new_project(dest, "Neon Drift", kind="2d")
        text = (dest / "CLAUDE.md").read_text(encoding="utf-8")
        assert "Neon Drift" in text
        assert "__PROJECT_NAME__" not in text


class TestTheTelemetryAutoload:
    """An adopted game never got the addon, so it recorded nothing.

    scaffold overlays templates/shared onto every project it CREATES, so a
    scaffolded game has addons/bgate and its autoloads. Adoption did not, and
    nothing else installed them - a real project reached 28 sessions and 59
    pieces of feedback with zero rows in playtest_event, while its review screen
    said "NO TELEMETRY" every time without ever naming the missing addon.
    """

    def _game(self, tmp_path, body: str):
        game = tmp_path / "game"
        game.mkdir(parents=True)
        (game / "project.godot").write_text(body, encoding="utf-8")
        return tmp_path

    def test_it_installs_the_addon_and_registers_the_autoloads(self, tmp_path):
        root = self._game(tmp_path, '[application]\nconfig/name="G"\n')
        out = adopt.install_telemetry(root)
        assert out["action"] == "installed"
        assert "bgate_telemetry.gd" in out["scripts"]
        cfg = (root / "game" / "project.godot").read_text(encoding="utf-8")
        assert 'BGateTelemetry="*res://addons/bgate/bgate_telemetry.gd"' in cfg
        assert (root / "game" / "addons" / "bgate" / "bgate_telemetry.gd").is_file()

    def test_it_keeps_autoloads_the_project_already_had(self, tmp_path):
        """The one unrecoverable thing adopt must never do."""
        root = self._game(
            tmp_path,
            '[autoload]\n\nAudio="*res://scripts/audio.gd"\n'
            'PauseMenu="*res://scenes/pause_menu.tscn"\n')
        adopt.install_telemetry(root)
        cfg = (root / "game" / "project.godot").read_text(encoding="utf-8")
        assert 'Audio="*res://scripts/audio.gd"' in cfg
        assert 'PauseMenu="*res://scenes/pause_menu.tscn"' in cfg
        assert "BGateTelemetry" in cfg

    def test_a_project_with_no_autoload_section_gets_one(self, tmp_path):
        root = self._game(tmp_path, '[application]\nconfig/name="G"\n')
        adopt.install_telemetry(root)
        cfg = (root / "game" / "project.godot").read_text(encoding="utf-8")
        assert cfg.count("[autoload]") == 1
        assert "BGateTelemetry" in cfg

    def test_running_it_twice_changes_nothing(self, tmp_path):
        root = self._game(tmp_path, '[application]\nconfig/name="G"\n')
        adopt.install_telemetry(root)
        before = (root / "game" / "project.godot").read_text(encoding="utf-8")
        assert adopt.install_telemetry(root)["action"] == "unchanged"
        assert (root / "game" / "project.godot").read_text(encoding="utf-8") == before

    def test_a_project_with_no_godot_still_adopts(self, tmp_path):
        """Never fail an adopt over an addon."""
        out = adopt.install_telemetry(tmp_path)
        assert out["action"] == "skipped"
        assert "no Godot project" in out["why"]
