"""The Unity adapter, driven against a fake editor.

No editor is installed here and none will be in CI, so the binary is a Python
script that behaves the way the real one does at the seams this adapter
reads: it writes the log file `-logFile` names, writes the NUnit XML
`-testResults` names, and exits with the code the real editor would. What is
tested is everything on our side of those seams: discovery, the lockfile
refusal, log parsing, XML scoring, script installation, and the engine axis
around it (lanes, telemetry, playtest, the test history).
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from bgate_adapters import unity
from bgate_core.runtime import engines

FAKE = textwrap.dedent('''
    import os, sys, time
    args = sys.argv[1:]
    def opt(name):
        return args[args.index(name) + 1] if name in args else ""
    log = opt("-logFile")
    mode = os.environ.get("FAKE_UNITY_MODE", "ok")
    lines = ["Unity Editor fake " + os.environ.get("FAKE_UNITY_VERSION", "2022.3.10f1"),
             "-batchmode " + " ".join(args)]
    code = 0
    if mode == "compile_error":
        lines += ["Assets/Scripts/Player.cs(12,9): error CS0103: The name 'jmup' does not exist in the current context",
                  "Assets/Scripts/Player.cs(12,9): error CS0103: The name 'jmup' does not exist in the current context",
                  "Scripts have compiler errors."]
        code = 1
    if mode == "hang":
        time.sleep(30)
    results = opt("-testResults")
    if results and mode in ("ok", "test_fail"):
        failed = 1 if mode == "test_fail" else 0
        cases = ['<test-case id="1" name="JumpClearsLedge" fullname="Tests.Feel.JumpClearsLedge" result="Passed" duration="0.012"/>',
                 '<test-case id="2" name="CoyoteTime" fullname="Tests.Feel.CoyoteTime" result="%s" duration="0.030"/>' % ("Failed" if failed else "Passed")]
        xml = '<?xml version="1.0"?><test-run id="2" testcasecount="2" result="%s" total="2" passed="%d" failed="%d" inconclusive="0" skipped="0">%s</test-run>' % (
            "Failed" if failed else "Passed", 2 - failed, failed, "".join(cases))
        open(results, "w", encoding="utf-8").write(xml)
        code = 2 if failed else 0
    if "-executeMethod" in args and os.environ.get("BGATE_SHOT_OUT"):
        out = os.environ["BGATE_SHOT_OUT"]
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "wb").write(b"\\x89PNG fake")
        lines.append("BGateCapture: wrote " + out)
    if log:
        open(log, "w", encoding="utf-8").write("\\n".join(lines) + "\\n")
    sys.exit(code)
''')


@pytest.fixture
def fake_editor(tmp_path, monkeypatch):
    """A fake Unity on disk, pointed at by BGATE_UNITY, run through python."""
    script = tmp_path / "fake_unity.py"
    script.write_text(FAKE, encoding="utf-8")
    # BGATE_UNITY must be an executable; a .py is not, so a launcher is what
    # the variable names. On Windows that is a .cmd; elsewhere a shell script.
    if os.name == "nt":
        launcher = tmp_path / "Unity.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        launcher = tmp_path / "Unity"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                            encoding="utf-8")
        launcher.chmod(0o755)
    monkeypatch.setenv("BGATE_UNITY", str(launcher))
    monkeypatch.delenv("FAKE_UNITY_MODE", raising=False)
    return launcher


def _unity_project(base: Path, version: str = "2022.3.10f1") -> Path:
    (base / "ProjectSettings").mkdir(parents=True, exist_ok=True)
    (base / "ProjectSettings" / "ProjectVersion.txt").write_text(
        f"m_EditorVersion: {version}\nm_EditorVersionWithRevision: {version} (abc)\n",
        encoding="utf-8")
    (base / "ProjectSettings" / "ProjectSettings.asset").write_text(
        "PlayerSettings:\n  productName: Neon Drift\n", encoding="utf-8")
    (base / "Assets" / "Scripts").mkdir(parents=True, exist_ok=True)
    (base / "Packages").mkdir(exist_ok=True)
    (base / "Packages" / "manifest.json").write_text(json.dumps(
        {"dependencies": {"com.unity.test-framework": "1.1.33"}}), encoding="utf-8")
    return base


class TestDiscovery:
    def test_the_registry_drives_unity_now(self):
        assert engines.supported("unity")
        assert engines.adapter("unity") is unity
        assert engines.template_dir_name("unity") == ""
        assert engines.shared_dir_name("unity") == "unity"
        assert "unity" in engines.doctor_rows("unity")

    def test_bgate_unity_overrides_everything(self, fake_editor):
        assert unity.find_unity() == str(fake_editor)
        assert unity.available()["available"] is True

    def test_a_missing_override_is_named(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BGATE_UNITY", str(tmp_path / "nope.exe"))
        with pytest.raises(unity.UnityNotFound, match="BGATE_UNITY"):
            unity.find_unity()

    def test_hub_installs_are_listed_newest_first_and_the_project_picks_its_own(
            self, tmp_path, monkeypatch):
        monkeypatch.delenv("BGATE_UNITY", raising=False)
        hub = tmp_path / "Hub" / "Editor"
        for ver in ("2021.3.5f1", "2022.3.10f1", "6000.0.2f1"):
            exe = hub / ver / "Editor" / ("Unity.exe" if os.name == "nt" else "Unity")
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"x")
        monkeypatch.setattr(unity, "_hub_roots", lambda: [hub])
        got = unity.installed_editors()
        assert [e["version"] for e in got] == ["6000.0.2f1", "2022.3.10f1", "2021.3.5f1"]
        project = _unity_project(tmp_path / "game", "2022.3.10f1")
        # The project's own version wins over the newest: a newer editor
        # upgrades the project the moment it opens it.
        assert "2022.3.10f1" in unity.find_unity(project)
        assert "6000.0.2f1" in unity.find_unity()
        assert unity.version(unity.find_unity(project))["version"] == "2022.3.10f1"

    def test_project_facts_are_read_off_the_files(self, tmp_path):
        project = _unity_project(tmp_path)
        assert unity.is_project(project)
        assert unity.project_version(project) == "2022.3.10f1"
        assert unity.product_name(project) == "Neon Drift"
        assert unity.editor_open(project) is False
        (project / "Temp").mkdir()
        (project / "Temp" / "UnityLockfile").write_bytes(b"")
        assert unity.editor_open(project) is True


class TestBatchmode:
    def test_a_clean_compile_is_ok(self, fake_editor, tmp_path):
        project = _unity_project(tmp_path)
        got = unity.check_project(str(project), timeout=60)
        assert got["ok"] is True, got
        assert got["exit_code"] == 0 and got["errors"] == []
        assert "-batchmode" in got["command"] and "-nographics" in got["command"]
        assert Path(got["log_path"]).is_file()

    def test_compile_errors_come_out_of_the_log_deduplicated(self, fake_editor, tmp_path,
                                                             monkeypatch):
        monkeypatch.setenv("FAKE_UNITY_MODE", "compile_error")
        project = _unity_project(tmp_path)
        got = unity.check_project(str(project), timeout=60)
        assert got["ok"] is False
        assert got["errors"][0].startswith("Assets/Scripts/Player.cs(12,9): CS0103")
        assert got["errors"].count(got["errors"][0]) == 1
        assert "Scripts have compiler errors" in got["errors"]
        assert "1 compile error" in got["error"] or "compile error(s)" in got["error"]

    def test_an_open_editor_is_refused_not_run(self, fake_editor, tmp_path):
        project = _unity_project(tmp_path)
        (project / "Temp").mkdir()
        (project / "Temp" / "UnityLockfile").write_bytes(b"")
        got = unity.check_project(str(project), timeout=60)
        assert got["ok"] is False and got["editor_open"] is True
        assert "UnityLockfile" in got["error"]

    def test_a_hang_is_a_timeout_with_advice(self, fake_editor, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_UNITY_MODE", "hang")
        project = _unity_project(tmp_path)
        got = unity.check_project(str(project), timeout=2)
        assert got["ok"] is False and got["timeout"] is True
        assert "longer timeout" in got["error"]

    def test_execute_method_refuses_a_name_that_is_not_one(self, fake_editor, tmp_path):
        project = _unity_project(tmp_path)
        got = unity.execute_method(str(project), "not a method")
        assert got["ok"] is False and "fully qualified" in got["error"]
        got = unity.execute_method(str(project), "BGate.Tools.Rebuild", timeout=60)
        assert got["ok"] is True
        assert "-executeMethod BGate.Tools.Rebuild" in got["command"]


class TestTests:
    def test_nunit_xml_is_scored(self):
        xml = ('<test-run total="3" passed="2" failed="1" skipped="0" result="Failed">'
               '<test-case name="A" fullname="T.A" result="Passed" duration="0.1"/>'
               '<test-case name="B" fullname="T.B" result="Failed" duration="0.2">'
               '<failure/></test-case>'
               '<test-case name="C" fullname="T.C" result="Passed"/></test-run>')
        got = unity.parse_nunit(xml)
        assert (got["total"], got["passed"], got["failed"]) == (3, 2, 1)
        assert [c["ok"] for c in got["cases"]] == [True, False, True]

    def test_a_truncated_results_file_still_scores_what_ran(self):
        xml = ('<test-run total="12" passed="0" failed="0"><test-case fullname="T.A" '
               'result="Passed"/><test-case fullname="T.B" result="Pa')
        got = unity.parse_nunit(xml)
        assert got["total"] == 12 and len(got["cases"]) == 1

    def test_a_green_run_is_ok_and_a_red_one_names_the_count(self, fake_editor, tmp_path,
                                                            monkeypatch):
        project = _unity_project(tmp_path)
        got = unity.test_run(str(project), timeout=60)
        assert got["ok"] is True and got["total"] == 2 and got["platform"] == "EditMode"
        assert "-runTests" in got["command"] and "-testPlatform EditMode" in got["command"]
        monkeypatch.setenv("FAKE_UNITY_MODE", "test_fail")
        got = unity.test_run(str(project), timeout=60, filter="Tests.Feel")
        assert got["ok"] is False and got["failed"] == 1
        assert "1 of 2" in got["error"]
        assert "-testFilter Tests.Feel" in got["command"]

    def test_playmode_keeps_graphics(self, fake_editor, tmp_path):
        project = _unity_project(tmp_path)
        got = unity.test_run(str(project), platform="PlayMode", timeout=60)
        assert "-nographics" not in got["command"]
        assert unity.test_run(str(project), platform="Nope")["ok"] is False

    def test_no_results_file_is_no_tests_not_a_pass(self, fake_editor, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_UNITY_MODE", "silent")
        project = _unity_project(tmp_path)
        got = unity.test_run(str(project), timeout=60)
        assert got["ok"] is False and got["no_tests"] is True

    def test_test_scripts_are_found_by_attribute_not_by_name_alone(self, tmp_path):
        project = _unity_project(tmp_path)
        tests = project / "Assets" / "Tests"
        tests.mkdir()
        (tests / "FeelTests.cs").write_text("[Test] public void A() {}", encoding="utf-8")
        (tests / "Helper.cs").write_text("static class H {}", encoding="utf-8")
        (project / "Assets" / "Scripts" / "PlayerTests.cs").write_text(
            "[UnityTest] IEnumerator B() { yield break; }", encoding="utf-8")
        assert unity.test_scripts(project) == ["Assets/Scripts/PlayerTests.cs",
                                               "Assets/Tests/FeelTests.cs"]


class TestScripts:
    def test_install_puts_both_files_under_assets_bgate_once(self, tmp_path):
        project = _unity_project(tmp_path)
        got = unity.install_scripts(project)
        assert got["action"] == "installed" and len(got["written"]) == 2
        have = unity.scripts_installed(project)
        assert have["telemetry"] and have["capture"]
        assert (project / "Assets" / "BGate" / "Editor" / "BGateCapture.cs").is_file()
        assert not list((project / "Assets" / "BGate").glob("*.meta"))
        again = unity.install_scripts(project)
        assert again["action"] == "unchanged" and again["written"] == []

    def test_the_shipped_sources_say_what_they_are(self):
        telemetry = (unity.TEMPLATE_DIR / unity.TELEMETRY_FILE).read_text(encoding="utf-8")
        capture = (unity.TEMPLATE_DIR / unity.CAPTURE_FILE).read_text(encoding="utf-8")
        assert "BGATE_TELEMETRY" in telemetry and "RuntimeInitializeOnLoadMethod" in telemetry
        assert 'schema' in telemetry and 'ts' in telemetry and 'kind' in telemetry
        assert "BGATE_SHOT_OUT" in capture
        ns, cls, method = unity.CAPTURE_METHOD.rsplit(".", 2)
        assert f"namespace {ns}" in capture and f"class {cls}" in capture
        assert f"public static void {method}()" in capture

    def test_screenshot_needs_the_capture_script_then_renders(self, fake_editor, tmp_path):
        project = _unity_project(tmp_path)
        out = tmp_path / "shots" / "a.png"
        got = unity.screenshot(str(project), str(out))
        assert got["ok"] is False and "unity_install_scripts" in got["error"]
        unity.install_scripts(project)
        got = unity.screenshot(str(project), str(out), scene="Assets/Scenes/Main.unity",
                               timeout=60)
        assert got["ok"] is True, got
        assert out.is_file() and "-nographics" not in got["command"]
        assert "still" in got["note"]


class TestEngineAxis:
    """The rest of the pipeline sees a Unity project as one."""

    def test_adopt_records_unity_and_stamps_its_own_briefing(self, tmp_path):
        from bgate_core.store import adopt, project
        base = _unity_project(tmp_path)
        found = adopt.detect(base)
        assert found["engine"] == "unity" and found["engine_supported"] is True
        got = adopt.adopt(base, name="Neon Drift")
        assert project.get(base)["engine"] == "unity"
        claude_md = (base / "CLAUDE.md").read_text(encoding="utf-8")
        assert "unity_test_run" in claude_md and "project.godot" not in claude_md
        ignore = (base / ".gitignore").read_text(encoding="utf-8")
        assert "[Ll]ibrary/" in ignore
        assert got["lanes"]["engine"] == "unity"

    def test_lanes_land_on_assets(self, tmp_path):
        from bgate_core.board import seats
        from bgate_core.store import project
        base = _unity_project(tmp_path)
        project.init(base, "x", engine="unity")
        seats.apply_layout(base)
        assert "gameplay" in seats.lane_owners(base, "Assets/Scripts/Player.cs")
        assert "art" in seats.lane_owners(base, "Assets/Textures/hero.png")
        assert "tech" in seats.lane_owners(base, "ProjectSettings/ProjectSettings.asset")
        assert "qa" in seats.lane_owners(base, "Assets/Tests/FeelTests.cs")
        assert "**" not in seats.lanes_for_layout("", "unity")["tech"]

    def test_telemetry_status_and_install(self, tmp_path):
        from bgate_core.store import adopt
        base = _unity_project(tmp_path)
        got = adopt.telemetry_status(base)
        assert got["ok"] is False and got["installable"] is True
        put = adopt.install_telemetry(base)
        assert put["action"] == "installed"
        assert adopt.telemetry_status(base)["ok"] is True

    def test_playtest_hints_preflight_and_launch(self, fake_editor, tmp_path, monkeypatch):
        from bgate_core.qa import playtest
        base = _unity_project(tmp_path)
        assert playtest.game_window_hints(base) == ["Neon Drift", "Unity"]
        check = playtest.preflight(root=str(base), native=True)["checks"]["native_game"]
        assert check["engine"] == "unity" and check["ok"] is True
        (base / "Temp").mkdir()
        (base / "Temp" / "UnityLockfile").write_bytes(b"")
        check = playtest.preflight(root=str(base), native=True)["checks"]["native_game"]
        assert check["ok"] is False and "already has this project open" in check["reason"]

    def test_the_test_history_is_shared(self, fake_editor, tmp_path):
        from bgate_core.runtime import enginetests
        from bgate_core.store import project
        base = _unity_project(tmp_path)
        project.init(base, "x", engine="unity")
        (base / "Assets" / "Tests").mkdir()
        (base / "Assets" / "Tests" / "FeelTests.cs").write_text("[Test]", encoding="utf-8")
        assert enginetests.discover(base)["engine"] == "unity"
        got = enginetests.run(base, timeout=60)
        assert got["engine"] == "unity" and got["ok"] is True
        assert got["assertions_passed"] == 2 and got["scripts_run"] == 2
        assert enginetests.history(base)[0]["passed"] == 2

    def test_the_verify_rule_and_toolchain_know_unity(self, fake_editor, tmp_path, monkeypatch):
        from bgate_core.store import project
        from bgate_ui.agents import dispatch
        base = _unity_project(tmp_path)
        project.init(base, "x", engine="unity")
        rule = dispatch._verify_rule(str(base))
        assert "unity_test_run" in rule and "engine_check" in rule and "godot_" not in rule
        monkeypatch.setattr(dispatch, "_TOOLCHAIN", {})
        for var in ("BGATE_GODOT", "BGATE_NODE", "BGATE_BLENDER", "BGATE_FFMPEG"):
            monkeypatch.delenv(var, raising=False)
        assert dispatch._toolchain_env().get("BGATE_UNITY") == str(fake_editor)

    def test_assets_are_wired_by_guid(self, tmp_path):
        from bgate_core.store import assets
        base = _unity_project(tmp_path)
        tex = base / "Assets" / "Textures"
        tex.mkdir()
        (tex / "hero.png").write_bytes(bytes([0x89]) + b"PNG")
        (tex / "hero.png.meta").write_text(
            "fileFormatVersion: 2\nguid: 0123456789abcdef0123456789abcdef\n", encoding="utf-8")
        (tex / "unused.png").write_bytes(bytes([0x89]) + b"PNG")
        (tex / "unused.png.meta").write_text(
            "fileFormatVersion: 2\nguid: ffffffffffffffffffffffffffffffff\n", encoding="utf-8")
        scenes = base / "Assets" / "Scenes"
        scenes.mkdir()
        (scenes / "Main.unity").write_text(
            "m_Texture: {fileID: 2800000, guid: 0123456789abcdef0123456789abcdef, type: 3}\n",
            encoding="utf-8")
        got = assets.integration(base)
        assert got["unreferenced"] == ["Assets/Textures/unused.png"]

    def test_the_tools_are_owned_and_the_neutral_ones_reach_them(self):
        for name in ("unity_status", "unity_check", "unity_test_run",
                     "unity_execute", "unity_install_scripts", "unity_screenshot"):
            assert engines.tool_owner(name) == "unity"
            assert not engines.tool_enabled(name, "godot")
        assert engines.tool_enabled("engine_screenshot", "unity")


class TestDimension:
    def test_a_unity_scene_says_whether_it_is_3d(self, tmp_path):
        from bgate_core.store import adopt
        base = _unity_project(tmp_path)
        scenes = base / "Assets" / "Scenes"
        scenes.mkdir()
        (scenes / "Main.unity").write_text(
            "Camera:\n  orthographic: 0\nMeshRenderer:\n  m_Enabled: 1\n"
            "MeshFilter:\n  m_Mesh: {}\n", encoding="utf-8")
        found = adopt.detect(base)
        assert found["scenes"] == 1 and found["dimension"] == "3d"
        (scenes / "Main.unity").write_text(
            "Camera:\n  orthographic: 1\nSpriteRenderer:\n  m_Sprite: {}\n"
            "Rigidbody2D:\n  m_Mass: 1\n", encoding="utf-8")
        assert adopt.detect(base)["dimension"] == "2d"
