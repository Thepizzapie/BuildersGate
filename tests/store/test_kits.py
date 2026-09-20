"""Kits: manifests that resolve, installs that never overwrite, a ledger
that tells installed from modified, and the engine's own word that what
landed compiles.
"""
from __future__ import annotations

import json

import pytest

from bgate_adapters import godot
from bgate_core.store import kits, scaffold

needs_godot = pytest.mark.skipif(
    not godot.available()["available"], reason="Godot not installed")

EXPECTED = {"topdown_controller", "platformer_controller",
            "third_person_controller", "vehicle_controller",
            "interaction_2d", "interaction_3d", "inventory", "health"}


@pytest.fixture()
def game2d(tmp_path):
    scaffold.new_project(tmp_path, "Emberfall", kind="2d")
    return tmp_path


@pytest.fixture()
def game3d(tmp_path):
    scaffold.new_project(tmp_path, "Emberfall", kind="3d")
    return tmp_path


class TestManifests:
    def test_every_shipped_kit_loads(self):
        kits_by_name = {k["name"]: k for k in kits.list_kits()}
        assert EXPECTED <= set(kits_by_name)
        broken = {n: k["error"] for n, k in kits_by_name.items() if k.get("error")}
        assert not broken, broken

    def test_every_source_file_exists_and_is_hashed(self):
        for kit in kits.list_kits():
            for f in kit["files"]:
                assert len(f["hash"]) == 64, (kit["name"], f)
                assert f["bytes"] > 0

    def test_template_refs_point_at_the_scaffold_scripts(self):
        """A kit that reuses a template script must name the SAME bytes the
        scaffold stamps, or the two drift."""
        kit = kits.load("interaction_3d")
        src = next(f for f in kit["files"] if f["dest"] == "scripts/interactable.gd")
        assert src["source"].endswith("interactable.gd")
        assert "templates" in src["source"] and "3d" in src["source"]

    def test_dimension_filter(self):
        two = {k["name"] for k in kits.list_kits(dimension="2d")}
        assert "topdown_controller" in two and "inventory" in two
        assert "vehicle_controller" not in two

    def test_unknown_kit_is_named(self):
        with pytest.raises(kits.KitError, match="no kit named"):
            kits.load("teleporter")

    def test_broken_manifest_is_reported_not_dropped(self, tmp_path, monkeypatch):
        bad = tmp_path / "godot" / "broken"
        bad.mkdir(parents=True)
        (bad / "kit.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(kits, "KITS_DIR", tmp_path)
        rows = kits.list_kits()
        assert rows and rows[0]["name"] == "broken" and rows[0]["error"]

    def test_manifest_cannot_escape_the_project(self, tmp_path, monkeypatch):
        bad = tmp_path / "godot" / "escape"
        bad.mkdir(parents=True)
        (bad / "x.gd").write_text("extends Node\n", encoding="utf-8")
        (bad / "kit.json").write_text(json.dumps({
            "name": "escape", "title": "t", "engine": "godot", "dimension": "any",
            "summary": "s", "files": {"../x.gd": "x.gd"}}), encoding="utf-8")
        monkeypatch.setattr(kits, "KITS_DIR", tmp_path)
        with pytest.raises(kits.KitError, match="relative path"):
            kits.load("escape")

    def test_every_action_key_is_a_known_keycode(self):
        for kit in kits.list_kits():
            for action, keys in kit["requires"]["actions"].items():
                for key in keys:
                    assert key in kits.KEYCODES, (kit["name"], action, key)


class TestInstall:
    def test_writes_files_and_binds_missing_actions(self, game2d):
        got = kits.install(game2d, game2d, "topdown_controller", dimension="2d")
        assert got["ok"]
        assert got["written"] == ["scripts/topdown_controller.gd"]
        # The 2D scaffold already declares move_left/right and jump.
        assert got["actions_added"] == ["move_down", "move_up"]
        assert got["actions_missing"] == []
        assert got["autoloads_missing"] == []
        actions = kits.declared_actions(game2d / "project.godot")
        assert {"move_left", "move_right", "move_up", "move_down"} <= actions
        # The additions land INSIDE [input], before [rendering].
        text = (game2d / "project.godot").read_text(encoding="utf-8")
        assert text.index("[input]") < text.index("move_up={") < text.index("[rendering]")

    def test_bound_action_carries_the_manifest_keys(self, game2d):
        kits.install(game2d, game2d, "topdown_controller")
        text = (game2d / "project.godot").read_text(encoding="utf-8")
        block = text.split("move_up={", 1)[1].split("}", 1)[0]
        assert f'"physical_keycode":{kits.KEYCODES["W"]}' in block
        assert f'"physical_keycode":{kits.KEYCODES["Up"]}' in block

    def test_existing_action_is_never_rebound(self, game2d):
        before = (game2d / "project.godot").read_text(encoding="utf-8")
        jump = before.split("jump={", 1)[1].split("}", 1)[0]
        kits.install(game2d, game2d, "platformer_controller")
        after = (game2d / "project.godot").read_text(encoding="utf-8")
        assert after.split("jump={", 1)[1].split("}", 1)[0] == jump

    def test_no_bind_reports_what_is_missing(self, game2d):
        got = kits.install(game2d, game2d, "topdown_controller", bind=False)
        assert got["actions_missing"] == ["move_down", "move_up"]
        assert got["actions_added"] == []
        assert "move_up" not in kits.declared_actions(game2d / "project.godot")

    def test_second_install_is_a_no_op(self, game2d):
        kits.install(game2d, game2d, "inventory")
        got = kits.install(game2d, game2d, "inventory")
        assert got["ok"] and got["written"] == []
        assert got["kept"][0]["reason"].startswith("already present, identical")
        assert got["note"]

    def test_never_overwrites_a_different_file(self, game2d):
        target = game2d / "scripts" / "health.gd"
        target.write_text("extends Node\n# mine\n", encoding="utf-8")
        got = kits.install(game2d, game2d, "health")
        assert got["written"] == []
        assert "DIFFERENT" in got["kept"][0]["reason"]
        assert target.read_text(encoding="utf-8") == "extends Node\n# mine\n"
        # Present, so the kit counts as in place, and status says modified.
        assert got["ok"]
        state = {k["name"]: k["state"] for k in kits.status(game2d, game2d)["kits"]}
        assert state["health"] == "modified"

    def test_force_replaces_and_keeps_a_backup(self, game2d):
        target = game2d / "scripts" / "health.gd"
        target.write_text("extends Node\n# mine\n", encoding="utf-8")
        got = kits.install(game2d, game2d, "health", force=True)
        assert got["written"] == ["scripts/health.gd"]
        assert got["replaced"][0]["dest"] == "scripts/health.gd"
        backups = list((game2d / "scripts").glob("health.gd.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "extends Node\n# mine\n"

    def test_dimension_mismatch_is_refused_and_named(self, game2d):
        with pytest.raises(kits.KitError, match="3d kit; this project is 2d"):
            kits.install(game2d, game2d, "vehicle_controller", dimension="2d")
        # The escape hatch.
        got = kits.install(game2d, game2d, "vehicle_controller", dimension="")
        assert got["ok"]

    def test_any_dimension_kit_installs_into_both(self, game2d):
        assert kits.install(game2d, game2d, "health", dimension="2d")["ok"]

    def test_missing_autoload_is_reported(self, tmp_path):
        (tmp_path / "project.godot").write_text(
            'config_version=5\n\n[application]\n\nconfig/name="Bare"\n',
            encoding="utf-8")
        got = kits.install(tmp_path, tmp_path, "health")
        assert got["ok"]
        assert got["autoloads_missing"] == ["BGateTelemetry"]

    def test_binds_into_a_project_with_no_input_section(self, tmp_path):
        (tmp_path / "project.godot").write_text(
            'config_version=5\n\n[application]\n\nconfig/name="Bare"\n',
            encoding="utf-8")
        got = kits.install(tmp_path, tmp_path, "topdown_controller")
        assert set(got["actions_added"]) == {"move_left", "move_right", "move_up", "move_down"}
        text = (tmp_path / "project.godot").read_text(encoding="utf-8")
        assert "[input]" in text
        assert kits.declared_actions(tmp_path / "project.godot") == set(got["actions_added"])

    def test_no_engine_project_is_refused(self, tmp_path):
        with pytest.raises(kits.KitError, match="no engine project"):
            kits.install(tmp_path, tmp_path / "nowhere", "health")


class TestLedgerAndStatus:
    def test_ledger_lands_under_bgate(self, game2d):
        kits.install(game2d, game2d, "inventory")
        ledger = json.loads((game2d / ".bgate" / "kits.json").read_text(encoding="utf-8"))
        assert "inventory" in ledger
        assert "scripts/inventory.gd" in ledger["inventory"]["files"]

    def test_status_reads_installed_modified_absent(self, game2d):
        kits.install(game2d, game2d, "inventory")
        kits.install(game2d, game2d, "health")
        (game2d / "scripts" / "health.gd").write_text("edited", encoding="utf-8")
        state = {k["name"]: k["state"] for k in kits.status(game2d, game2d)["kits"]}
        assert state["inventory"] == "installed"
        assert state["health"] == "modified"
        assert state["topdown_controller"] == "absent"

    def test_partial_when_one_file_is_gone(self, game2d):
        kits.install(game2d, game2d, "interaction_2d")
        (game2d / "scripts" / "pickup_2d.gd").unlink()
        state = {k["name"]: k for k in kits.status(game2d, game2d)["kits"]}
        assert state["interaction_2d"]["state"] == "partial"

    def test_stale_when_the_kit_changed_upstream(self, game2d):
        """Disk matches what install wrote, but the kit has since changed:
        installed, and stale, and NOT modified (nobody here touched it)."""
        kits.install(game2d, game2d, "health")
        target = game2d / "scripts" / "health.gd"
        target.write_text("extends Node\n# an older revision\n", encoding="utf-8")
        ledger_path = game2d / ".bgate" / "kits.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["health"]["files"]["scripts/health.gd"] = kits._sha(target)
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        row = next(k for k in kits.status(game2d, game2d)["kits"] if k["name"] == "health")
        assert row["state"] == "installed"
        assert row["stale"] is True


class TestRemove:
    def test_removes_unmodified_files_and_clears_the_ledger(self, game2d):
        kits.install(game2d, game2d, "interaction_2d")
        got = kits.remove(game2d, game2d, "interaction_2d")
        assert got["ok"] and len(got["removed"]) == 3
        assert not (game2d / "scripts" / "interactable_2d.gd").exists()
        assert "interaction_2d" not in kits.read_ledger(game2d)
        # Actions stay: another script may read them by now.
        assert "interact" in kits.declared_actions(game2d / "project.godot")

    def test_refuses_an_edited_file(self, game2d):
        kits.install(game2d, game2d, "health")
        (game2d / "scripts" / "health.gd").write_text("edited", encoding="utf-8")
        got = kits.remove(game2d, game2d, "health")
        assert not got["ok"]
        assert got["refused"][0]["dest"] == "scripts/health.gd"
        assert (game2d / "scripts" / "health.gd").exists()
        assert "health" in kits.read_ledger(game2d)

    def test_force_deletes_anyway(self, game2d):
        kits.install(game2d, game2d, "health")
        (game2d / "scripts" / "health.gd").write_text("edited", encoding="utf-8")
        got = kits.remove(game2d, game2d, "health", force=True)
        assert got["ok"] and got["removed"] == ["scripts/health.gd"]

    def test_not_installed_is_an_error(self, game2d):
        with pytest.raises(kits.KitError, match="not in the ledger"):
            kits.remove(game2d, game2d, "health")


@needs_godot
class TestEngineProof:
    """The scripts are REAL. --check-only cannot see autoloads and fails every
    controller on BGateTelemetry, so the proof loads each script inside the
    running project, the way the game would."""

    @pytest.mark.parametrize("kind,names", [
        ("2d", ["topdown_controller", "platformer_controller", "interaction_2d",
                "inventory", "health"]),
        ("3d", ["third_person_controller", "vehicle_controller", "interaction_3d",
                "inventory", "health"]),
    ])
    def test_every_kit_compiles_in_its_dimension(self, tmp_path, kind, names):
        scaffold.new_project(tmp_path, "Emberfall", kind=kind)
        rels = []
        for name in names:
            got = kits.install(tmp_path, tmp_path, name)
            assert got["ok"], got
            rels += [f["dest"] for f in kits.load(name)["files"]]
        got = kits.prove(tmp_path, rels, timeout=300)
        assert got["measured"], got
        failed = [s for s in got["scripts"] if not s["compiled"]]
        assert not failed, (failed, got["engine_errors"])
        assert got["ok"]

    def test_a_broken_script_is_caught(self, tmp_path):
        scaffold.new_project(tmp_path, "Emberfall", kind="2d")
        (tmp_path / "scripts" / "broken.gd").write_text(
            "extends Node\nfunc _ready() -> void:\n\tvar x := \n", encoding="utf-8")
        got = kits.prove(tmp_path, ["scripts/broken.gd"], timeout=300)
        assert got["measured"] and not got["ok"]
        assert got["scripts"][0]["compiled"] is False
