"""The F1 live-tuning overlay — the part of it Python can hold accountable.

The dashboard tells the user, in four separate places, that F1 opens a live
tuning panel over the running game. That claim is only true if three things hold,
and each of them is checkable from here:

  1. the addon SHIPS — it lands in a scaffolded project like the telemetry one,
  2. it is REGISTERED — project.godot autoloads it, or it never runs,
  3. the file it persists to ROUND-TRIPS — the shape the addon writes is the
     shape the addon (and bgate_core.iterations) reads back.

The fourth thing — that dragging a slider moves the game — needs the engine, so
the end-to-end test below is gated on a real Godot and boots the scaffolded
project with a pre-written override file.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from bgate_adapters import godot
from bgate_core import iterations, scaffold

needs_godot = pytest.mark.skipif(
    not godot.available()["available"], reason="Godot not installed")

ADDON_REL = "addons/bgate/bgate_tuner.gd"
ADDON_SOURCE = (Path(__file__).parents[1] / "templates" / "shared" / ADDON_REL)


def tuner_source() -> str:
    return ADDON_SOURCE.read_text(encoding="utf-8")


class TestTheAddonShips:
    @pytest.mark.parametrize("kind", ["2d", "3d"])
    def test_lands_in_a_scaffolded_project(self, tmp_path, kind):
        got = scaffold.new_project(tmp_path / kind, "Emberfall", kind=kind)
        assert ADDON_REL in set(got["files"])
        assert (tmp_path / kind / ADDON_REL).is_file()

    @pytest.mark.parametrize("kind", ["2d", "3d"])
    def test_autoload_is_registered(self, tmp_path, kind):
        scaffold.new_project(tmp_path / kind, "Emberfall", kind=kind)
        text = (tmp_path / kind / "project.godot").read_text(encoding="utf-8")
        assert f'BGateTuner="*res://{ADDON_REL}"' in text
        # Registered the same way the telemetry autoload is — same section, same
        # "*" (always-on) prefix. If that convention ever changes, both move.
        assert 'BGateTelemetry="*res://addons/bgate/bgate_telemetry.gd"' in text
        autoload = text.split("[autoload]", 1)[1]
        assert autoload.split("[", 1)[0].count("BGateTuner=") == 1


class TestOverlayContract:
    """Guard the promises the dashboard copy makes on the addon's behalf."""

    def test_f1_is_the_key(self):
        src = tuner_source()
        # No input-map action: F1 has to work in a project that never declared
        # one, which is every project the scaffold has ever produced.
        assert "KEY_F1" in src
        assert "func _input(" in src

    def test_persists_to_the_bgate_tunables_file(self):
        src = tuner_source()
        assert 'TUNABLES_DIR := ".bgate"' in src
        assert 'TUNABLES_FILE := "tunables.json"' in src
        assert 'SCHEMA_VERSION := 1' in src
        assert '"values": _values' in src

    def test_never_walks_into_the_global_registry(self):
        # ~/.bgate holds the project REGISTRY. A walk-up that accepts any .bgate
        # would tune every project on the machine at once.
        assert 'ROOT_MARKER := "game.db"' in tuner_source()

    def test_a_shipped_build_is_inert(self):
        src = tuner_source()
        assert "OS.is_debug_build()" in src
        assert 'FEATURE_TAG := "bgate_tuning"' in src
        # Nothing is armed until _arm(), and _arm is only reachable through the
        # debug/feature-tag/dashboard gate.
        assert "set_process_input(false)" in src
        assert "func _arm() -> void:" in src

    def test_only_exported_variables_are_offered(self):
        src = tuner_source()
        assert "PROPERTY_USAGE_SCRIPT_VARIABLE" in src
        assert "PROPERTY_USAGE_EDITOR" in src
        assert "PROPERTY_HINT_RANGE" in src


def write_tunables(root: Path, values: dict) -> Path:
    """Write the file exactly as the addon writes it."""
    path = root / ".bgate" / "tunables.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"schema": 1, "updated": 1_700_000_000.0, "values": values},
        indent="\t"), encoding="utf-8")
    return path


class TestFileRoundTrip:
    def test_values_survive_a_write_read_cycle(self, tmp_path):
        values = {
            "/root/Main/Player": {
                "gravity": 1440.0,
                "fall_multiplier": 2.5,
                "coyote_time": 0.42,
                "double_jump": True,
                "label": "brisk",
                "offset": [9.0, 11.0],
                "tint": [1.0, 0.0, 0.0, 1.0],
            },
        }
        path = write_tunables(tmp_path, values)
        back = json.loads(path.read_text(encoding="utf-8"))
        assert back["schema"] == 1
        assert back["values"] == values
        # Keys are absolute node paths; each bucket is property -> JSON scalar,
        # bool, string, or float array. Nothing else is representable, because
        # nothing else survives JSON without a custom decoder.
        for node_path, bucket in back["values"].items():
            assert node_path.startswith("/root/")
            for value in bucket.values():
                assert isinstance(value, (bool, int, float, str, list))

    def test_the_iteration_snapshot_sees_the_overrides(self, tmp_path):
        """A tuned build must not be invisible drift.

        bgate_core.iterations already reads .bgate/tunables.json as `overrides`;
        writing there is what makes a tuning session show up in the snapshot
        rather than silently changing how the game plays.
        """
        (tmp_path / "game").mkdir()
        write_tunables(tmp_path, {"/root/Main/Player": {"gravity": 1440.0}})
        captured = iterations._tunables(tmp_path)
        assert captured["overrides"]["values"]["/root/Main/Player"]["gravity"] == 1440.0

    def test_a_corrupt_file_is_reported_not_raised(self, tmp_path):
        (tmp_path / "game").mkdir()
        path = tmp_path / ".bgate" / "tunables.json"
        path.parent.mkdir(parents=True)
        path.write_text("not json at all {{{", encoding="utf-8")
        captured = iterations._tunables(tmp_path)
        assert captured["overrides"] == {"error": "invalid .bgate/tunables.json"}


@needs_godot
class TestBootsWithTheSavedValues:
    """The claim that makes this useful: a tuning session survives a restart."""

    @pytest.mark.slow
    @pytest.mark.parametrize("kind", ["2d", "3d"])
    def test_stored_overrides_are_applied_at_boot(self, tmp_path, kind):
        # Mirror the real layout: <root>/.bgate next to <root>/game.
        project = tmp_path / "game"
        scaffold.new_project(project, "Emberfall", kind=kind)
        godot.check_project(str(project), timeout=240)
        (tmp_path / ".bgate").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".bgate" / "game.db").write_bytes(b"")  # marks the root
        write_tunables(tmp_path, {"/root/Main/Player": {
            "gravity": 1440.0, "fall_multiplier": 2.5}})

        telemetry = tmp_path / "telemetry.jsonl"
        env = {**os.environ,
               "BGATE_TELEMETRY": str(telemetry),
               "BGATE_AUTOQUIT": "2"}
        subprocess.run([godot.find_godot(), "--headless", "--path", str(project)],
                       capture_output=True, timeout=180, env=env,
                       stdin=subprocess.DEVNULL, creationflags=godot._NO_WINDOW)

        events = [json.loads(line) for line in
                  telemetry.read_text(encoding="utf-8").splitlines() if line.strip()]
        applied = [e for e in events if e["kind"] == "tunables_applied"]
        assert applied, "the tuner did not apply the saved values at boot"
        player = applied[0]["data"]["values"]["/root/Main/Player"]
        assert player["gravity"] == 1440.0
        assert player["fall_multiplier"] == 2.5
        assert applied[0]["data"]["source"].endswith(".bgate/tunables.json")

    @pytest.mark.slow
    def test_a_missing_bgate_dir_is_not_an_error(self, tmp_path):
        """No .bgate above the project: boot clean, say nothing, tune nothing."""
        project = tmp_path / "game"
        scaffold.new_project(project, "Emberfall", kind="2d")
        godot.check_project(str(project), timeout=240)

        env = {**os.environ, "BGATE_AUTOQUIT": "2"}
        env.pop("BGATE_TELEMETRY", None)
        proc = subprocess.run(
            [godot.find_godot(), "--headless", "--path", str(project)],
            capture_output=True, text=True, timeout=180, env=env,
            stdin=subprocess.DEVNULL, creationflags=godot._NO_WINDOW)

        output = (proc.stdout or "") + (proc.stderr or "")
        assert "BGateTuner" not in output, output[-800:]
        assert not (tmp_path / ".bgate").exists(), "the tuner invented a .bgate dir"

    @pytest.mark.slow
    def test_a_corrupt_file_does_not_stop_the_boot(self, tmp_path):
        project = tmp_path / "game"
        scaffold.new_project(project, "Emberfall", kind="2d")
        godot.check_project(str(project), timeout=240)
        (tmp_path / ".bgate").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".bgate" / "game.db").write_bytes(b"")
        (tmp_path / ".bgate" / "tunables.json").write_text(
            "{ this is not json", encoding="utf-8")

        telemetry = tmp_path / "telemetry.jsonl"
        env = {**os.environ,
               "BGATE_TELEMETRY": str(telemetry),
               "BGATE_AUTOQUIT": "2"}
        subprocess.run([godot.find_godot(), "--headless", "--path", str(project)],
                       capture_output=True, timeout=180, env=env,
                       stdin=subprocess.DEVNULL, creationflags=godot._NO_WINDOW)

        # The game still ran and still reported — a bad override file costs you
        # the overrides, not the session.
        events = [json.loads(line) for line in
                  telemetry.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert {"session_open", "autoquit"} <= {e["kind"] for e in events}
        assert not [e for e in events if e["kind"] == "tunables_applied"]
