"""Studio modules: extract a version from one game, install it into another
by pin, move the pin forward without touching edited files. Godot and web.

The studio root comes from bgate_core.store.studio; these tests point
kits.studio_modules_dir at a temp directory so they exercise the shelf, not
the studio's own config.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bgate_core.store import kits


@pytest.fixture()
def studio(tmp_path, monkeypatch):
    base = tmp_path / "studio" / "shared" / "modules"
    monkeypatch.setattr(kits, "studio_modules_dir", lambda engine: base / engine)
    return base


def _game(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    game = tmp_path / name
    game.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (game / rel).parent.mkdir(parents=True, exist_ok=True)
        (game / rel).write_text(text, encoding="utf-8")
    (game / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    return game


def _publish(game, name, version, files, engine="godot", **kw):
    return kits.extract(game, name, version, files, engine=engine,
                        summary=kw.pop("summary", "A test module."),
                        confirm=True, **kw)


class TestExtract:
    def test_without_confirm_nothing_is_written(self, tmp_path, studio):
        game = _game(tmp_path, "a", {"scripts/hp.gd": "extends Node\n"})
        got = kits.extract(game, "hp", "1.0.0", ["scripts/hp.gd"],
                           provides={"class_names": ["HP"]})
        assert got["confirmed"] is False
        assert got["manifest"]["files"] == {"scripts/hp.gd": "files/scripts/hp.gd"}
        assert got["manifest"]["provides"] == {"class_names": ["HP"]}
        assert not (studio / "godot" / "hp").exists()

    def test_confirm_publishes_a_loadable_versioned_module(self, tmp_path, studio):
        game = _game(tmp_path, "a", {"scripts/hp.gd": "extends Node\n"})
        _publish(game, "hp", "1.0.0", ["scripts/hp.gd"])
        kit = kits.load("hp", "godot", "1.0.0")
        assert kit["origin"] == "studio" and kit["version"] == "1.0.0"
        assert kits.module_versions("hp") == ["1.0.0"]
        data = json.loads((studio / "godot" / "hp" / "1.0.0" / "kit.json").read_text())
        assert data["extracted_from"] == str(game.resolve())

    def test_a_published_version_is_immutable(self, tmp_path, studio):
        game = _game(tmp_path, "a", {"scripts/hp.gd": "extends Node\n"})
        _publish(game, "hp", "1.0.0", ["scripts/hp.gd"])
        (game / "scripts/hp.gd").write_text("extends Node # v2\n")
        with pytest.raises(kits.KitError, match="immutable"):
            _publish(game, "hp", "1.0.0", ["scripts/hp.gd"])
        assert "v2" not in (studio / "godot/hp/1.0.0/files/scripts/hp.gd").read_text()

    def test_an_older_version_than_the_newest_is_refused(self, tmp_path, studio):
        game = _game(tmp_path, "a", {"scripts/hp.gd": "x\n"})
        _publish(game, "hp", "1.2.0", ["scripts/hp.gd"])
        with pytest.raises(kits.KitError, match="older"):
            _publish(game, "hp", "1.1.0", ["scripts/hp.gd"])

    @pytest.mark.parametrize("files", [["../outside.gd"], ["missing.gd"]])
    def test_files_must_be_inside_the_game_and_exist(self, tmp_path, studio, files):
        game = _game(tmp_path, "a", {})
        (tmp_path / "outside.gd").write_text("x")
        with pytest.raises(kits.KitError):
            kits.extract(game, "hp", "1.0.0", files)

    def test_web_modules_live_under_src(self, tmp_path, studio):
        game = _game(tmp_path, "w", {"src/net/relay.ts": "export {}\n",
                                     "index.html": "<html></html>"})
        with pytest.raises(kits.KitError, match="src/"):
            kits.extract(game, "relay", "1.0.0", ["index.html"], engine="web")
        _publish(game, "relay", "1.0.0", ["src/net/relay.ts"], engine="web")
        assert kits.load("relay", "web")["version"] == "1.0.0"

    def test_no_studio_is_a_named_refusal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(kits, "studio_modules_dir", lambda engine: None)
        game = _game(tmp_path, "a", {"scripts/hp.gd": "x"})
        with pytest.raises(kits.KitError, match="studio init"):
            kits.extract(game, "hp", "1.0.0", ["scripts/hp.gd"])


class TestShelf:
    def test_a_studio_only_module_is_listed_with_its_versions(self, tmp_path, studio):
        game = _game(tmp_path, "a", {"scripts/hp2.gd": "x"})
        _publish(game, "my_hp", "1.0.0", ["scripts/hp2.gd"])
        _publish(game, "my_hp", "1.1.0", ["scripts/hp2.gd"])
        row = next(k for k in kits.list_kits() if k["name"] == "my_hp")
        assert row["version"] == "1.1.0" and row["versions"] == ["1.0.0", "1.1.0"]

    def test_a_studio_module_named_like_a_shipped_kit_needs_a_pin(self, tmp_path, studio):
        game = _game(tmp_path, "a", {"scripts/health.gd": "# mine\n"})
        _publish(game, "health", "1.0.0", ["scripts/health.gd"])
        row = next(k for k in kits.list_kits() if k["name"] == "health")
        assert row["origin"] == "shipped" and row["studio_versions"] == ["1.0.0"]

        root, target = tmp_path / "root-b", _game(tmp_path, "b", {})
        kits.install(root, target, "health")
        assert "# mine" not in (target / "scripts/health.gd").read_text()
        (target / "scripts/health.gd").unlink()
        got = kits.install(root, target, "health", version="1.0.0", force=True)
        assert got["origin"] == "studio"
        assert (target / "scripts/health.gd").read_text() == "# mine\n"
        assert kits.read_ledger(root)["health"]["version"] == "1.0.0"


class TestPinAndUpdate:
    def _two_versions(self, tmp_path):
        src = _game(tmp_path, "src", {"scripts/a.gd": "a1\n", "scripts/b.gd": "b1\n",
                                       "scripts/old.gd": "old\n"})
        _publish(src, "mod", "1.0.0", ["scripts/a.gd", "scripts/b.gd", "scripts/old.gd"])
        (src / "scripts/a.gd").write_text("a2\n")
        (src / "scripts/b.gd").write_text("b2\n")
        (src / "scripts/new.gd").write_text("new\n")
        _publish(src, "mod", "2.0.0", ["scripts/a.gd", "scripts/b.gd", "scripts/new.gd"])

    def test_install_pins_and_reinstall_keeps_the_pin(self, tmp_path, studio):
        self._two_versions(tmp_path)
        root, game = tmp_path / "root", _game(tmp_path, "g", {})
        got = kits.install(root, game, "mod", version="1.0.0")
        assert got["version"] == "1.0.0"
        assert kits.install(root, game, "mod")["version"] == "1.0.0"
        row = next(k for k in kits.status(root, game)["kits"] if k["name"] == "mod")
        assert row["version"] == "1.0.0" and row["newer"] == "2.0.0"
        assert row["state"] == "installed"

    def test_update_replaces_untouched_files_and_keeps_edited_ones(self, tmp_path, studio):
        self._two_versions(tmp_path)
        root, game = tmp_path / "root", _game(tmp_path, "g", {})
        kits.install(root, game, "mod", version="1.0.0")
        (game / "scripts/b.gd").write_text("b1 edited\n")
        got = kits.update(root, game, "mod")
        assert (got["from"], got["to"]) == ("1.0.0", "2.0.0")
        assert set(got["written"]) == {"scripts/a.gd", "scripts/new.gd"}
        assert got["kept"] == [{"dest": "scripts/b.gd",
                                "reason": "edited since install; left alone"}]
        assert got["dropped"] == ["scripts/old.gd"]
        assert (game / "scripts/a.gd").read_text() == "a2\n"
        assert (game / "scripts/b.gd").read_text() == "b1 edited\n"
        assert (game / "scripts/old.gd").is_file()
        assert kits.read_ledger(root)["mod"]["version"] == "2.0.0"
        row = next(k for k in kits.status(root, game)["kits"] if k["name"] == "mod")
        assert row["state"] == "modified" and row["newer"] == ""

    def test_update_never_moves_backwards_or_moves_a_shipped_kit(self, tmp_path, studio):
        self._two_versions(tmp_path)
        root, game = tmp_path / "root", _game(tmp_path, "g", {})
        kits.install(root, game, "mod", version="2.0.0")
        with pytest.raises(kits.KitError, match="older"):
            kits.update(root, game, "mod", version="1.0.0")
        kits.install(root, game, "inventory")
        with pytest.raises(kits.KitError, match="shipped"):
            kits.update(root, game, "inventory")

    def test_remove_uses_the_pinned_version(self, tmp_path, studio):
        self._two_versions(tmp_path)
        root, game = tmp_path / "root", _game(tmp_path, "g", {})
        kits.install(root, game, "mod", version="1.0.0")
        got = kits.remove(root, game, "mod")
        assert set(got["removed"]) == {"scripts/a.gd", "scripts/b.gd", "scripts/old.gd"}

    def test_the_cli_extracts_installs_and_updates(self, tmp_path, studio, capsys):
        from bgate_cli import main as cli
        from bgate_core.store import db, project, scaffold

        game = tmp_path / "cli-game"
        scaffold.new_project(game, "Cli Game", kind="2d")
        project.init(game, "Cli Game")
        (game / "scripts" / "dash.gd").write_text("extends Node\n", encoding="utf-8")
        base = ["--project", str(game)]
        assert cli._cmd_kit(["extract", "dash", "1.0.0", "scripts/dash.gd",
                             "--summary", "A dash."] + base) == 0
        assert "dry run" in capsys.readouterr().out
        assert cli._cmd_kit(["extract", "dash", "1.0.0", "scripts/dash.gd",
                             "--summary", "A dash.", "--confirm"] + base) == 0
        (game / "scripts" / "dash.gd").write_text("extends Node # 2\n", encoding="utf-8")
        assert cli._cmd_kit(["extract", "dash", "1.1.0", "scripts/dash.gd",
                             "--summary", "A dash.", "--confirm"] + base) == 0
        (game / "scripts" / "dash.gd").unlink()
        assert cli._cmd_kit(["install", "dash", "--version", "1.0.0"] + base) == 0
        capsys.readouterr()
        assert cli._cmd_kit(["list"] + base) == 0
        assert "dash@1.0.0" in capsys.readouterr().out
        assert cli._cmd_kit(["update", "dash"] + base) == 0
        assert "1.0.0 -> 1.1.0" in capsys.readouterr().out
        db.close_all()

    def test_web_install_lists_needs_instead_of_binding(self, tmp_path, studio):
        src = _game(tmp_path, "w", {"src/input/dash.ts": "export {}\n"})
        _publish(src, "dash", "1.0.0", ["src/input/dash.ts"], engine="web",
                 requires={"actions": {"dash": ["Shift"]}})
        root, game = tmp_path / "root", tmp_path / "g"
        game.mkdir()
        got = kits.install(root, game, "dash", engine="web")
        assert got["written"] == ["src/input/dash.ts"]
        assert got["actions_missing"] == ["dash"] and got["actions_added"] == []
        assert not (game / "project.godot").exists()
