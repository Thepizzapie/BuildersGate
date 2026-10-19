"""The studio: one folder for every game and what they share.

BGATE_HOME is redirected and BGATE_STUDIO cleared per test by conftest, so
nothing here reads or writes the developer's real studio pointer.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bgate_cli import main as cli
from bgate_cli import session
from bgate_core.store import db, project, studio


def _game(where: Path, name: str = "Ember Run") -> Path:
    project.init(where, name)
    (where / "scenes").mkdir(exist_ok=True)
    (where / "scenes" / "main.tscn").write_text("[gd_scene]\n", encoding="utf-8")
    db.close_all()
    return where


@pytest.fixture()
def made(tmp_path) -> Path:
    return Path(studio.init(tmp_path / "studio")["root"])


class TestNoStudio:
    def test_unset_is_none_and_says_so(self):
        assert studio.root() is None
        assert studio.status() == {"set": False, "ok": False, "root": None,
                                   "source": ""}
        with pytest.raises(studio.StudioError, match="no studio is set"):
            studio.games_dir()

    def test_init_without_a_studio_still_lands_in_the_cwd(self, tmp_path,
                                                          monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        assert cli.init_project("Flat Earth", kind="2d", engine="web") == 0
        assert (tmp_path / "flat-earth" / ".bgate" / "game.db").exists()
        db.close_all()

    def test_a_folder_without_an_index_is_not_a_studio(self, tmp_path,
                                                       monkeypatch):
        """A typo in BGATE_STUDIO must not make some directory the place new
        games are written."""
        monkeypatch.setenv(studio.ENV, str(tmp_path))
        assert studio.root() is None
        st = studio.status()
        assert st["set"] and not st["ok"] and st["source"] == "env"


class TestInit:
    def test_lays_out_the_folder_and_remembers_it(self, made):
        for sub in ("games", "shared/modules", "shared/assets"):
            assert (made / sub).is_dir()
        assert json.loads((made / "studio.json").read_text()) == {
            "games": {}, "modules": {}, "version": 1}
        assert studio.root() == made
        assert studio.configured()[1] == "pointer"
        assert studio.modules_dir("web") == made / "shared" / "modules" / "web"
        assert studio.assets_dir() == made / "shared" / "assets"

    def test_rerunning_keeps_the_index(self, made):
        data = studio.index()
        data["modules"]["web/relay"] = {"versions": ["1.0.0"]}
        studio.save_index(data)
        again = studio.init(made)
        assert again["created"] is False
        assert "web/relay" in studio.index()["modules"]

    def test_the_env_wins_over_the_pointer(self, made, tmp_path, monkeypatch):
        other = Path(studio.init(tmp_path / "other")["root"])
        studio.init(made)
        monkeypatch.setenv(studio.ENV, str(other))
        assert studio.root() == other
        assert "wins over the pointer" in studio.init(made)["note"]

    def test_refuses_a_folder_inside_a_project(self, tmp_path):
        game = _game(tmp_path / "game")
        with pytest.raises(studio.StudioError, match="inside the project"):
            studio.init(game / "studio")

    def test_refuses_a_bad_engine_name(self, made):
        with pytest.raises(studio.StudioError):
            studio.modules_dir("../web")


class TestCliInit:
    def test_new_games_land_in_games_and_the_index(self, made, tmp_path,
                                                   monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert cli.init_project("Neon Drift", kind="2d", engine="web") == 0
        root = made / "games" / "neon-drift"
        assert (root / ".bgate" / "game.db").exists()
        assert not (tmp_path / "neon-drift").exists()
        assert studio.index()["games"]["neon-drift"]["path"] == "games/neon-drift"
        assert project.known_projects()["neon-drift"] == str(root)
        db.close_all()

    def test_dir_still_wins(self, made, tmp_path):
        dest = tmp_path / "elsewhere"
        assert cli.init_project("Off Grid", kind="2d", engine="web",
                                dest=str(dest)) == 0
        assert (dest / ".bgate" / "game.db").exists()
        assert "off-grid" not in studio.index()["games"]
        db.close_all()


class TestAdopt:
    def test_in_place_indexes_with_an_absolute_path(self, made, tmp_path):
        game = _game(tmp_path / "loose")
        got = studio.adopt(game)
        assert got["moved"] is False
        entry = studio.index()["games"]["ember-run"]
        assert entry["path"] == str(game.resolve())
        assert studio.status()["games"][0]["exists"] is True

    def test_move_copies_checks_and_repoints_but_never_deletes(self, made,
                                                               tmp_path):
        game = _game(tmp_path / "loose")
        project.set_active(game)
        got = studio.adopt(game, move=True)
        dest = made / "games" / "ember-run"
        assert got["moved"] and got["root"] == str(dest)
        assert got["original"] == str(game.resolve())
        assert (game / ".bgate" / "game.db").exists(), "original kept"
        assert (dest / "scenes" / "main.tscn").read_text() == "[gd_scene]\n"
        assert project.get(dest)["slug"] == "ember-run"
        assert project.known_projects()["ember-run"] == str(dest)
        assert project.active_root() == dest and got["active_repointed"]
        assert studio.index()["games"]["ember-run"]["path"] == "games/ember-run"
        db.close_all()

    def test_move_refuses_an_occupied_destination(self, made, tmp_path):
        game = _game(tmp_path / "loose")
        (made / "games" / "ember-run").mkdir()
        with pytest.raises(studio.StudioError, match="already exists"):
            studio.adopt(game, move=True)
        assert project.known_projects()["ember-run"] == str(game.resolve())

    def test_a_bad_copy_leaves_the_registry_on_the_original(self, made, tmp_path,
                                                           monkeypatch):
        game = _game(tmp_path / "loose")
        real = studio._inventory
        monkeypatch.setattr(studio, "_inventory", lambda base: (
            {} if Path(base) == made / "games" / "ember-run" else real(base)))
        with pytest.raises(studio.StudioError, match="does not match"):
            studio.adopt(game, move=True)
        assert project.known_projects()["ember-run"] == str(game.resolve())
        assert "ember-run" not in studio.index()["games"]

    @pytest.mark.parametrize("slug", ["..\\x", "../x", "Ember", "", "-x", "a b"])
    def test_a_hand_edited_slug_cannot_leave_games(self, made, tmp_path, slug):
        game = _game(tmp_path / "loose")
        with db.tx(game) as conn:
            conn.execute("UPDATE project SET slug = ?", (slug,))
        db.close_all()
        with pytest.raises(studio.StudioError, match="refusing game name"):
            studio.adopt(game, move=True)
        with pytest.raises(studio.StudioError, match="refusing game name"):
            studio.adopt(game)
        assert sorted(p.name for p in (made / "games").iterdir()) == []
        assert not (made / "x").exists() and not (tmp_path / "x").exists()
        assert studio.index()["games"] == {}

    def test_refuses_what_is_not_a_project(self, made, tmp_path):
        (tmp_path / "plain").mkdir()
        with pytest.raises(studio.StudioError, match="not a Builders Gate project"):
            studio.adopt(tmp_path / "plain")

    def test_cli_move_prints_the_original_for_the_person(self, made, tmp_path,
                                                         capsys):
        game = _game(tmp_path / "loose")
        assert cli._cmd_studio(["adopt", str(game), "--move"]) == 0
        out = capsys.readouterr().out
        assert "untouched" in out and str(game.resolve()) in out
        db.close_all()


class TestSurfaces:
    def test_status_cli_without_a_studio(self, capsys):
        assert cli._cmd_studio(["status"]) == 0
        assert "no studio set" in capsys.readouterr().out

    def test_session_block_names_the_studio(self, made, tmp_path):
        game = _game(tmp_path / "loose")
        studio.adopt(game)
        text = session.build_context(str(game))
        assert f"STUDIO   {made}" in text and "1 game(s)" in text

    def test_session_block_is_silent_about_no_studio(self, tmp_path):
        game = _game(tmp_path / "loose")
        assert "STUDIO" not in session.build_context(str(game))

    @pytest.mark.anyio
    async def test_project_status_carries_it(self, made, tmp_path):
        from bgate_mcp import server
        game = _game(tmp_path / "loose")
        result = await server.mcp.call_tool("project_status",
                                            {"project_dir": str(game)})
        content = result[0] if isinstance(result, tuple) else result
        got = json.loads(content[0].text)
        assert got["studio"]["root"] == str(made)
        db.close_all()
