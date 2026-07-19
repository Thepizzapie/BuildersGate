"""Templates, verified by making REAL Godot import and run them.

A template that doesn't import is worse than no template — it hands someone a
broken project and a debugging session. So the tests here don't check that files
exist; they check that the engine accepts them and that telemetry actually lands
on disk.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from bgate_adapters import godot
from bgate_core import scaffold

needs_godot = pytest.mark.skipif(
    not godot.available()["available"], reason="Godot not installed")


class TestScaffold:
    def test_lists_both_templates(self):
        kinds = {t["kind"]: t for t in scaffold.list_templates()}
        assert kinds["2d"]["available"] and kinds["3d"]["available"]

    @pytest.mark.parametrize("kind", ["2d", "3d"])
    def test_creates_expected_layout(self, tmp_path, kind):
        got = scaffold.new_project(tmp_path / kind, "Emberfall", kind=kind)
        assert got["ok"] is True
        files = set(got["files"])
        assert "project.godot" in files
        assert "scenes/main.tscn" in files
        assert "scripts/player.gd" in files
        # The autoload comes from shared/ — one copy, overlaid onto every template.
        assert "addons/bgate/bgate_telemetry.gd" in files

    def test_substitutes_project_name(self, tmp_path):
        scaffold.new_project(tmp_path, "Emberfall", kind="2d")
        text = (tmp_path / "project.godot").read_text(encoding="utf-8")
        assert 'config/name="Emberfall"' in text
        assert "__PROJECT_NAME__" not in text

    def test_registers_the_autoload(self, tmp_path):
        scaffold.new_project(tmp_path, "Emberfall", kind="2d")
        text = (tmp_path / "project.godot").read_text(encoding="utf-8")
        assert 'BGateTelemetry="*res://addons/bgate/bgate_telemetry.gd"' in text

    def test_refuses_to_clobber_a_non_empty_dir(self, tmp_path):
        (tmp_path / "my_work.gd").write_text("precious", encoding="utf-8")
        with pytest.raises(FileExistsError, match="not empty"):
            scaffold.new_project(tmp_path, "Emberfall")
        assert (tmp_path / "my_work.gd").read_text(encoding="utf-8") == "precious"

    def test_force_overrides(self, tmp_path):
        (tmp_path / "junk.txt").write_text("x", encoding="utf-8")
        assert scaffold.new_project(tmp_path, "Emberfall", force=True)["ok"]

    def test_rejects_unknown_kind(self, tmp_path):
        with pytest.raises(ValueError, match="kind"):
            scaffold.new_project(tmp_path, "X", kind="4d")


@needs_godot
class TestTemplatesActuallyImport:
    @pytest.mark.slow
    @pytest.mark.parametrize("kind", ["2d", "3d"])
    def test_godot_imports_the_template(self, tmp_path, kind):
        scaffold.new_project(tmp_path, "Emberfall", kind=kind)
        got = godot.check_project(str(tmp_path), timeout=240)
        assert got["ok"] is True, got.get("errors") or got.get("output")


@needs_godot
class TestTelemetryEndToEnd:
    """The autoload is the whole point — prove it writes real events."""

    @pytest.mark.slow
    @pytest.mark.parametrize("kind", ["2d", "3d"])
    def test_running_the_game_emits_telemetry(self, tmp_path, kind):
        project = tmp_path / "game"
        scaffold.new_project(project, "Emberfall", kind=kind)
        godot.check_project(str(project), timeout=240)

        telemetry = tmp_path / "telemetry.jsonl"
        env = {
            **os.environ,
            "BGATE_TELEMETRY": str(telemetry),
            "BGATE_AUTOQUIT": "2",  # no human to close the window
        }
        subprocess.run(
            [godot.find_godot(), "--headless", "--path", str(project)],
            capture_output=True, text=True, timeout=180, env=env,
            stdin=subprocess.DEVNULL, creationflags=godot._NO_WINDOW)

        assert telemetry.exists(), "autoload wrote no telemetry file"
        events = [json.loads(line) for line in
                  telemetry.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert events, "telemetry file is empty"

        kinds = {e["kind"] for e in events}
        assert "session_open" in kinds
        assert "autoquit" in kinds

        for event in events:
            # ts is the load-bearing field: the game's clock and the recorder's
            # are unrelated, so wall clock is the only shared axis.
            assert "ts" in event and event["ts"] > 1_600_000_000
            assert "kind" in event and "data" in event

    @pytest.mark.slow
    @pytest.mark.parametrize("kind,scale", [("2d", 400.0), ("3d", 5.0)])
    def test_spawn_drop_reports_sane_numbers(self, tmp_path, kind, scale):
        """The spawn-fall must not fabricate a jump-shaped 'land'.

        Regression: _peak_y defaulted to 0.0 and only initialized on JUMP, so the
        opening drop reported peak_height 302 for a 24px player — a plausible
        number that would send an agent chasing physics that never happened.
        """
        project = tmp_path / "game"
        scaffold.new_project(project, "Emberfall", kind=kind)
        godot.check_project(str(project), timeout=240)

        telemetry = tmp_path / "telemetry.jsonl"
        env = {**os.environ, "BGATE_TELEMETRY": str(telemetry), "BGATE_AUTOQUIT": "2"}
        subprocess.run([godot.find_godot(), "--headless", "--path", str(project)],
                       capture_output=True, timeout=180, env=env,
                       stdin=subprocess.DEVNULL, creationflags=godot._NO_WINDOW)

        events = [json.loads(line) for line in
                  telemetry.read_text(encoding="utf-8").splitlines() if line.strip()]
        lands = [e for e in events if e["kind"] == "land"]
        assert lands, "the spawn drop should still report a landing"

        first = lands[0]["data"]
        assert first["cause"] == "spawn", "spawn drop must not masquerade as a jump"
        assert 0.0 <= first["peak_height"] < scale, first
        assert 0.0 <= first["air_time"] < 5.0, first
        # No jump happened, so nothing should claim one did.
        assert not [e for e in events if e["kind"] == "jump"]

    @pytest.mark.slow
    def test_autoload_is_inert_without_the_env_var(self, tmp_path):
        """Opening the game normally must not write files or error."""
        project = tmp_path / "game"
        scaffold.new_project(project, "Emberfall", kind="2d")
        godot.check_project(str(project), timeout=240)

        env = {k: v for k, v in os.environ.items() if k != "BGATE_TELEMETRY"}
        env["BGATE_AUTOQUIT"] = "2"
        proc = subprocess.run(
            [godot.find_godot(), "--headless", "--path", str(project)],
            capture_output=True, text=True, timeout=180, env=env,
            stdin=subprocess.DEVNULL, creationflags=godot._NO_WINDOW)

        output = (proc.stdout or "") + (proc.stderr or "")
        assert "BGateTelemetry" not in output or "cannot open" not in output
        assert not list(tmp_path.glob("*.jsonl"))


class TestWebTelemetryContract:
    def test_web_build_posts_to_the_active_app_session(self):
        telemetry = (
            Path(__file__).parents[1]
            / "templates" / "shared" / "addons" / "bgate"
            / "bgate_telemetry.gd"
        ).read_text(encoding="utf-8")
        assert "bgate_session" in telemetry
        assert "/api/playtest/%s/events" in telemetry
        assert 'OS.has_feature("web")' in telemetry
        assert "SCHEMA_VERSION := 1" in telemetry
        assert '"schema": SCHEMA_VERSION' in telemetry


@needs_godot
class TestSessionClockAlignment:
    """Telemetry must land on the SESSION clock, not the game's."""

    @pytest.mark.slow
    def test_events_align_to_session_start_not_game_start(self, root, tmp_path):
        import time

        from bgate_core import db, playtest

        project = tmp_path / "game"
        scaffold.new_project(project, "Emberfall", kind="2d")
        godot.check_project(str(project), timeout=240)

        # Session starts 5s "ago" — as if the game were launched later.
        session_start = time.time() - 5.0
        telemetry = tmp_path / "telemetry.jsonl"
        with db.tx(root) as conn:
            conn.execute(
                "INSERT INTO playtest_session (id, name, slug, status, "
                "telemetry_path, started_epoch) VALUES (1,'R','r','processing',?,?)",
                (str(telemetry), session_start))

        env = {**os.environ, "BGATE_TELEMETRY": str(telemetry), "BGATE_AUTOQUIT": "2"}
        subprocess.run([godot.find_godot(), "--headless", "--path", str(project)],
                       capture_output=True, timeout=180, env=env,
                       stdin=subprocess.DEVNULL, creationflags=godot._NO_WINDOW)

        got = playtest.ingest_telemetry(root, 1)
        assert got["ingested"] > 0
        assert "warning" not in got, got.get("warning")

        conn = db.connect(root)
        first = conn.execute(
            "SELECT t FROM playtest_event WHERE session_id = 1 ORDER BY t LIMIT 1"
        ).fetchone()[0]
        # The game booted ~5s after the session did, so its first event must land
        # near 5s — NOT near 0, which is what a naive game-clock read would give.
        assert 4.0 < first < 12.0, f"first event at t={first}: clocks misaligned"
