"""register() must never lose the projects it did not touch."""
import json

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
