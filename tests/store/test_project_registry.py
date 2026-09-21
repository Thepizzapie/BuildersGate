"""register() must never lose the projects it did not touch."""
import json
import os
from pathlib import Path

from bgate_core.store import project


def _registry(monkeypatch, tmp_path):
    monkeypatch.setenv("BGATE_HOME", str(tmp_path / "home"))
    return project._registry_path()


def test_register_keeps_rows_whose_folder_is_unavailable(tmp_path, monkeypatch):
    reg = _registry(monkeypatch, tmp_path)
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"unplugged": str(tmp_path / "gone")}), encoding="utf-8")
    game = tmp_path / "game"
    (game / ".bgate").mkdir(parents=True)
    (game / ".bgate" / "game.db").write_bytes(b"")
    project.register(game, "game")
    on_disk = json.loads(reg.read_text(encoding="utf-8"))
    assert set(on_disk) == {"unplugged", "game"}
    # the filtered reader still hides the dead row
    assert set(project.known_projects()) == {"game"}


def test_register_does_not_replace_an_unparseable_registry(tmp_path, monkeypatch):
    reg = _registry(monkeypatch, tmp_path)
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text("{not json", encoding="utf-8")
    game = tmp_path / "game"
    (game / ".bgate").mkdir(parents=True)
    (game / ".bgate" / "game.db").write_bytes(b"")
    project.register(game, "game")   # best-effort: swallows, must not clobber
    assert reg.read_text(encoding="utf-8") == "{not json"


def _make_project(root):
    (root / ".bgate").mkdir(parents=True, exist_ok=True)
    (root / ".bgate" / "game.db").write_bytes(b"")


class TestItem33TempRegistrationNeverPreferred:
    """MEASURED (EXIT 67): `bgate init` ran inside a Temp kick_* scaffold dir
    and its active pointer beat the real project's — every tool answered
    about the wrong root until a human deleted the entry by hand."""

    def test_a_temp_dir_active_pointer_defers_to_a_known_real_project(
        self, tmp_path, monkeypatch
    ):
        # tmp_path is ITSELF under the OS temp dir (pytest's own tmp factory),
        # so the "real" project has to live outside whatever gettempdir()
        # reports here, or it would (correctly, for a test set up that way)
        # also read as temp. Redirecting gettempdir() to a dedicated
        # sub-directory keeps "real" and "temp" distinguishable.
        fake_tmp = tmp_path / "systemp"
        fake_tmp.mkdir()
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(fake_tmp))
        _registry(monkeypatch, tmp_path)
        real = tmp_path / "real_game"
        _make_project(real)
        project.register(real, "real_game")

        temp_dir = fake_tmp / "kick_bgatetest"
        _make_project(temp_dir)
        project.set_active(temp_dir)
        assert project.active_root() == real.resolve()

    def test_a_temp_dir_pointer_still_wins_when_it_is_the_only_project(
        self, tmp_path, monkeypatch
    ):
        fake_tmp = tmp_path / "systemp"
        fake_tmp.mkdir()
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(fake_tmp))
        _registry(monkeypatch, tmp_path)
        temp_dir = fake_tmp / "kick_bgatetest_only"
        _make_project(temp_dir)
        project.set_active(temp_dir)
        assert project.active_root() == temp_dir.resolve()

    def test_a_non_temp_active_pointer_is_returned_unchanged(
        self, tmp_path, monkeypatch
    ):
        fake_tmp = tmp_path / "systemp"
        fake_tmp.mkdir()
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(fake_tmp))
        _registry(monkeypatch, tmp_path)
        real = tmp_path / "real_game2"
        _make_project(real)
        project.set_active(real)
        assert project.active_root() == real.resolve()


class TestItem33MissingRegistered:
    def test_missing_registered_lists_rows_that_do_not_resolve(
        self, tmp_path, monkeypatch
    ):
        reg = _registry(monkeypatch, tmp_path)
        reg.parent.mkdir(parents=True, exist_ok=True)
        gone = tmp_path / "deleted_project"
        reg.write_text(json.dumps({"deleted": str(gone)}), encoding="utf-8")
        missing = project.missing_registered()
        assert missing == {"deleted": str(gone)}
        assert "deleted" not in project.known_projects()

    def test_missing_registered_is_empty_once_the_folder_is_real(
        self, tmp_path, monkeypatch
    ):
        reg = _registry(monkeypatch, tmp_path)
        game = tmp_path / "game3"
        _make_project(game)
        project.register(game, "game3")
        assert project.missing_registered() == {}
