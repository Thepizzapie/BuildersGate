"""Godot adapter against the REAL engine (4.7.1 portable).

Skipped when Godot isn't present rather than mocked — the risk here is entirely
in the boundary (which binary, does stdout survive, are errors detectable).
"""
from __future__ import annotations

import pytest

from bgate_adapters import godot

pytestmark = pytest.mark.skipif(
    not godot.available()["available"], reason="Godot not installed"
)

HELLO = """
extends SceneTree

func _init():
    print("BGATE_OK from Godot")
    print("version: ", Engine.get_version_info().string)
    quit()
"""


class TestDiscovery:
    def test_finds_godot(self):
        assert godot.available()["available"] is True

    def test_reports_version(self):
        assert "4." in godot.version()["version"]

    def test_prefers_main_exe_not_the_console_launcher(self):
        """Both pipe stdout identically (measured); the main exe is one less
        process between us and the engine."""
        import sys
        if sys.platform != "win32":
            pytest.skip("windows-only binary split")
        assert "_console" not in godot.find_godot().lower()

    def test_both_windows_binaries_deliver_stdout(self):
        """Guards the correction: the 'plain exe loses stdout' claim is FALSE.

        If this ever fails, discovery must flip back to the console launcher.
        """
        import sys
        from pathlib import Path
        if sys.platform != "win32":
            pytest.skip("windows-only binary split")

        main = Path(godot.find_godot())
        console = main.with_name(main.stem + "_console" + main.suffix)
        if not console.exists():
            pytest.skip("no console launcher alongside this build")

        for exe in (main, console):
            got = godot.run_script(HELLO, timeout=90)
            assert "BGATE_OK from Godot" in got["stdout"], exe.name

    def test_zero_byte_exe_is_ignored(self, tmp_path, monkeypatch):
        """A failed unzip leaves a 0-byte .exe that looks installed. Seen live."""
        stub = tmp_path / "Godot_v4.7.1-stable_win64.exe"
        stub.write_bytes(b"")
        monkeypatch.setenv("BGATE_GODOT", str(stub))
        with pytest.raises(godot.GodotNotFound, match="missing or empty"):
            godot.find_godot()

    def test_missing_override_is_explicit(self, monkeypatch):
        monkeypatch.setenv("BGATE_GODOT", r"C:\nope\godot.exe")
        with pytest.raises(godot.GodotNotFound):
            godot.find_godot()


class TestRunScript:
    def test_runs_gdscript_and_captures_stdout(self):
        got = godot.run_script(HELLO, timeout=90)
        assert got["ok"] is True, got
        assert "BGATE_OK from Godot" in got["stdout"]

    def test_script_error_is_detected_not_swallowed(self):
        """Godot prints SCRIPT ERROR and still exits 0 — grep, don't trust rc."""
        broken = """
extends SceneTree

func _init():
    var x = undefined_function_that_does_not_exist()
    quit()
"""
        got = godot.run_script(broken, timeout=90)
        assert got["ok"] is False
        assert got["errors"]

    def test_timeout_explains_the_usual_cause(self, monkeypatch):
        def spy(cmd, **kwargs):
            raise godot.subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(godot.subprocess, "run", spy)
        got = godot.run_script("extends SceneTree", timeout=1)
        assert got["ok"] is False
        assert "quit()" in got["hint"]


class TestStdinIsolation:
    def test_spawn_detaches_stdin(self, monkeypatch):
        """Same MCP-server hazard as Blender: inheriting stdin hangs forever."""
        captured = {}

        def spy(cmd, **kwargs):
            captured.update(kwargs)
            return godot.subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(godot.subprocess, "run", spy)
        godot._spawn(["x"], timeout=1)
        assert captured["stdin"] is godot.subprocess.DEVNULL


class TestProject:
    def test_missing_project_is_reported(self, tmp_path):
        got = godot.check_project(str(tmp_path))
        assert got["ok"] is False
        assert "project.godot" in got["error"]

    def test_imports_a_minimal_project(self, tmp_path):
        (tmp_path / "project.godot").write_text(
            'config_version=5\n\n[application]\n\nconfig/name="Probe"\n'
            'config/features=PackedStringArray("4.7")\n',
            encoding="utf-8")
        got = godot.check_project(str(tmp_path), timeout=180)
        assert got["ok"] is True, got
