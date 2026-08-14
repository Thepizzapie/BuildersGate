"""The tech seat's static reads.

What is pinned here is the two ways this panel could lie. A convention audit
that scores the tool's own scene backups reports breaches nobody can fix, and a
generator inventory that scores an unreadable script as compliant hides exactly
the tool the rule was written for.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bgate_core import plumbing
from bgate_ui.app import app

SCENE = """[gd_scene load_steps=2 format=3]

[node name="Floor" type="Node2D"]

[node name="Terrain" type="TileMapLayer" parent="."]

[node name="Crate" type="Node2D" parent="Terrain"]

[node name="Sprite2D" type="Sprite2D" parent="."]

[node name="Cabinet" type="Node2D" parent="."]

[node name="Cabinet2" type="Node2D" parent="."]

[node name="Cabinet3" type="Node2D" parent="."]

[node name="Spawns" type="Node" parent="."]
"""


def _rule(out: dict, needle: str) -> dict:
    return next(r for r in out["rules"] if needle in r["rule"])


def test_every_rule_counts_a_real_node(tmp_path):
    (tmp_path / "game" / "scenes").mkdir(parents=True)
    (tmp_path / "game" / "scenes" / "floor.tscn").write_text(SCENE, encoding="utf-8")
    out = plumbing.scene_convention(tmp_path)

    assert out["scenes"] == 1 and out["nodes"] == 8
    # Sprite2D wears the name the engine gave it; Crate and Cabinet do not.
    named = _rule(out, "named node")
    assert named["count"] == 1 and named["tone"] == "bad"
    assert "Sprite2D" in named["examples"][0]
    # A terrain layer with an object parented into it.
    assert _rule(out, "TileMapLayer")["count"] == 1
    # Cabinet ×3 off one stem, none instanced.
    pasted = _rule(out, "Instance")
    assert pasted["count"] == 1 and "Cabinet ×3" in pasted["examples"][0]
    # Every childless plain container: Crate, the three Cabinets and Spawns.
    # The TileMapLayer has a child and the root is never counted, so a scene
    # with one marker in it does not read as five breaches.
    empty = _rule(out, "empty container")
    assert empty["count"] == 5 and empty["tone"] == "warn"
    assert not any("Floor" == e.split("· ")[-1] for e in empty["examples"])


def test_the_tools_own_scene_backups_are_not_the_game(tmp_path):
    """.bgate_out holds every historical copy of a scene this tool has edited.

    Auditing it reported the same breach once per undo step and made the count
    grow every time somebody used the editor.
    """
    for where in ("game/scenes", ".bgate_out/scene_backups", ".godot/imported"):
        d = tmp_path / where
        d.mkdir(parents=True)
        (d / "floor.tscn").write_text(SCENE, encoding="utf-8")
    assert plumbing.scene_convention(tmp_path)["scenes"] == 1


def test_a_flag_named_only_in_a_comment_does_not_count(tmp_path):
    """The flags are read out of the AST because a docstring is not a gate."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "liar.py").write_text(
        '"""Run with --check first, and it defaults to dry."""\n'
        'import argparse\n'
        'def main():\n'
        '    ap = argparse.ArgumentParser()\n'
        '    ap.add_argument("--scene")\n'
        '    open("game/scenes/floor.tscn", "w").write("x")\n',
        encoding="utf-8")
    row = plumbing.generator_inventory(tmp_path)["rows"][0]
    assert row["check"] == "no --check" and row["check_ok"] is False
    assert row["dry"] == "writes" and row["dry_ok"] is False


def test_apply_and_dry_run_are_opposite_defaults(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "bake.py").write_text(
        'import argparse\n'
        'ap = argparse.ArgumentParser()\n'
        'ap.add_argument("--check", action="store_true")\n'
        'ap.add_argument("--apply", action="store_true")\n'
        'p.write_text("[gd_scene]")  # game/scenes/floor.tscn\n',
        encoding="utf-8")
    (scripts / "fix.py").write_text(
        'import argparse\n'
        'ap = argparse.ArgumentParser()\n'
        'ap.add_argument("--dry-run", action="store_true")\n'
        'p.write_text("x")  # writes a .tscn\n',
        encoding="utf-8")
    rows = {r["path"]: r for r in plumbing.generator_inventory(tmp_path)["rows"]}
    assert rows["scripts/bake.py"]["dry_ok"] is True
    assert rows["scripts/fix.py"]["dry_ok"] is False


def test_a_script_that_writes_no_project_data_is_not_a_generator(tmp_path):
    """A measurement script that saves a .png owes nobody a --check."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "measure.py").write_text(
        'from pathlib import Path\n'
        'Path("out.png").write_bytes(b"")\n', encoding="utf-8")
    out = plumbing.generator_inventory(tmp_path)
    assert out["scanned"] == 1 and out["rows"] == []


def test_an_unparseable_script_is_unknown_not_compliant(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "broken.py").write_text(
        'def main(:\n    p.write_text("x")  # game/scenes/floor.tscn\n',
        encoding="utf-8")
    row = plumbing.generator_inventory(tmp_path)["rows"][0]
    assert row["check_ok"] is False and row["dry_ok"] is False
    assert "cannot be read" in row["note"]


def test_the_endpoint_answers_in_the_envelope(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    (root / "game" / "scenes").mkdir(parents=True, exist_ok=True)
    (root / "game" / "scenes" / "floor.tscn").write_text(SCENE, encoding="utf-8")
    body = TestClient(app).get("/api/tech/plumbing").json()
    assert body["ok"] is True
    data = body["data"]
    assert data["scenes"]["scenes"] == 1
    assert len(data["scenes"]["rules"]) == 4
    assert "dirty" in data["git"]
