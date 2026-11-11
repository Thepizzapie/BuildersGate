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
    made = opt("-createProject")
    if made and mode != "create_fail":
        os.makedirs(os.path.join(made, "ProjectSettings"), exist_ok=True)
        os.makedirs(os.path.join(made, "Packages"), exist_ok=True)
        open(os.path.join(made, "ProjectSettings", "ProjectVersion.txt"), "w").write(
            "m_EditorVersion: 6000.1.7f1\\n")
        open(os.path.join(made, "Packages", "manifest.json"), "w").write('{"dependencies": {}}')
    if opt("-executeMethod") == "BGate.Editor.BGateBuild.Player" and mode != "build_fail":
        exe = os.environ["BGATE_BUILD_OUT"]
        os.makedirs(os.path.dirname(exe), exist_ok=True)
        open(exe, "w").write("fake player")
        lines.append("BGateBuild: Succeeded " + exe)
    if opt("-executeMethod") == "BGate.Editor.BGateScaffold.Build":
        scenes = os.path.join(opt("-projectPath"), "Assets", "Scenes")
        os.makedirs(scenes, exist_ok=True)
        open(os.path.join(scenes, "Main.unity"), "w").write("kind " + os.environ.get("BGATE_KIND", ""))
    method = opt("-executeMethod")
    if method.startswith("BGate.Editor.BGateEdit."):
        import json
        verb = method.rsplit(".", 1)[-1]
        env = {k: v for k, v in os.environ.items() if k.startswith("BGATE_")}
        rep = {"ok": mode != "edit_fail", "verb": verb, "env": env}
        if mode == "edit_fail":
            rep["error"] = "Player has no Rigidbody (pass add=true to add it)"
            code = 2
        elif verb == "Import":
            rel = env["BGATE_IMPORT_PATH"]
            open(os.path.join(opt("-projectPath"), rel + ".meta"), "w").write("guid: abc\\n")
            rep.update({"path": rel, "guid": "abc", "main_type": "Texture2D", "placeable": False})
        elif verb == "Place":
            rep.update({"scene": env.get("BGATE_SCENE") or "Assets/Scenes/Main.unity",
                        "object": env.get("BGATE_NAME") or "Crate"})
        elif verb == "SetField":
            rep.update({"asset": env.get("BGATE_PREFAB") or env.get("BGATE_SCENE") or "Assets/Scenes/Main.unity",
                        "before": "5", "after": env["BGATE_VALUE"]})
        if mode != "edit_silent":
            open(env["BGATE_EDIT_RESULT"], "w", encoding="utf-8").write(json.dumps(rep))
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


def _hold_lock(monkeypatch, project):
    """An editor holding the project: the lockfile exists AND is held."""
    temp = Path(project) / "Temp"
    temp.mkdir(exist_ok=True)
    (temp / "UnityLockfile").write_bytes(b"")
    monkeypatch.setattr(unity, "_lock_held", lambda lock: True)


class TestDiscovery:
    def test_the_registry_drives_unity_now(self):
        assert engines.supported("unity")
        assert engines.adapter("unity") is unity
        assert engines.template_dir_name("unity") == "unity"
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

    def test_a_hub_install_without_its_binary_is_named_not_hidden(
            self, tmp_path, monkeypatch):
        monkeypatch.delenv("BGATE_UNITY", raising=False)
        monkeypatch.setattr(unity.shutil, "which", lambda name: None)
        hub = tmp_path / "Hub" / "Editor"
        (hub / "6000.1.7f1" / "Editor" / "Data").mkdir(parents=True)
        monkeypatch.setattr(unity, "_hub_roots", lambda: [hub])
        assert unity.installed_editors() == []
        assert unity.broken_installs() == [str(hub / "6000.1.7f1")]
        with pytest.raises(unity.UnityNotFound, match="install is incomplete"):
            unity.find_unity()

    def test_project_facts_are_read_off_the_files(self, tmp_path, monkeypatch):
        project = _unity_project(tmp_path)
        assert unity.is_project(project)
        assert unity.project_version(project) == "2022.3.10f1"
        assert unity.product_name(project) == "Neon Drift"
        assert unity.editor_open(project) is False
        (project / "Temp").mkdir()
        (project / "Temp" / "UnityLockfile").write_bytes(b"")
        # Unity 6 leaves the file behind after every clean batchmode exit, so
        # a lockfile nobody holds is NOT an open editor.
        assert unity.editor_open(project) is False
        _hold_lock(monkeypatch, project)
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

    def test_an_open_editor_is_refused_not_run(self, fake_editor, tmp_path, monkeypatch):
        project = _unity_project(tmp_path)
        _hold_lock(monkeypatch, project)
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
        # -quit would end the editor before the asynchronous test run began;
        # the runner exits on its own. The compile check keeps -quit.
        assert "-quit" not in got["command"]
        assert "-quit" in unity.check_project(str(project), timeout=60)["command"]
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
        _hold_lock(monkeypatch, base)
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
                     "unity_execute", "unity_install_scripts", "unity_screenshot",
                     "unity_build", "unity_play"):
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


class TestFirstRunAdopt:
    def test_the_card_offers_unity_to_create_or_adopt(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from bgate_ui.app import app
        monkeypatch.setenv("BGATE_ROOT", str(tmp_path / "nowhere"))
        client = TestClient(app)
        got = client.get("/api/project").json()
        body = got.get("data", got)
        engines = {e["name"]: e for e in body["engines"]}
        # Unity is scaffolded by the editor now, 2D and 3D.
        assert engines["unity"]["adopt_only"] is False
        assert [t["kind"] for t in engines["unity"]["templates"]
                if t["available"]] == ["2d", "3d"]
        assert engines["godot"]["adopt_only"] is False
        assert "none" not in engines

    def test_adopting_through_the_card_records_unity(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from bgate_ui import api as _api
        from bgate_ui.app import app
        from bgate_core.store import project
        base = _unity_project(tmp_path / "game")
        monkeypatch.setenv("BGATE_ROOT", str(tmp_path / "nowhere"))
        monkeypatch.setattr(_api, "current_actor", lambda request: "human")
        monkeypatch.setattr(_api, "require_human", lambda actor, what: None)
        client = TestClient(app)
        got = client.post("/api/project/adopt", json={"path": str(base), "name": "Hot Cargo"})
        assert got.status_code == 200, got.text
        body = got.json().get("data", got.json())
        assert body["engine"] == "unity" and body["engine_supported"] is True
        assert project.get(base)["engine"] == "unity"


class TestEngineScreenshotDispatch:
    def test_the_neutral_screenshot_calls_the_functions_not_the_async_wrappers(self, tmp_path, monkeypatch):
        """server._tool wraps every tool in an async function; a dispatcher
        that calls the registered name gets a coroutine, and `{**coro}` is a
        TypeError. Measured: every Godot and Unity engine_screenshot failed."""
        import inspect
        from bgate_mcp import server, tools_unity, tools_web
        base = _unity_project(tmp_path)
        from bgate_core.store import project
        project.init(base, "x", engine="unity")
        monkeypatch.setenv("BGATE_ROOT", str(base))
        monkeypatch.setattr(server, "_root", lambda: str(base))
        seen = {}
        def fake_still(**kw):
            seen.update(kw); return {"ok": True, "path": "x.png"}
        monkeypatch.setattr(tools_unity.unity_screenshot, "__wrapped__", fake_still, raising=False)
        got = tools_web.engine_screenshot.__wrapped__(scene="Assets/Scenes/Main.unity", label="t")
        assert not inspect.iscoroutine(got)
        assert got["engine"] == "unity" and got["ok"] is True and seen["scene"] == "Assets/Scenes/Main.unity"


class TestScaffold:
    """`bgate init --engine unity`: the editor makes the project, the template
    overlays C#, BGateScaffold.Build saves the scene."""

    def _bundle_test_framework(self, fake_editor, version="1.5.1"):
        pkg = (Path(fake_editor).parent / "Data" / "Resources" / "PackageManager"
               / "BuiltInPackages" / "com.unity.test-framework")
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")

    def test_init_creates_overlays_and_builds_the_scene(self, fake_editor, tmp_path):
        from bgate_core.store import scaffold
        self._bundle_test_framework(fake_editor)
        target = tmp_path / "game"
        got = scaffold.new_project(target, "Neon Drift", kind="3d", engine="unity")
        assert got["ok"] is True, got
        assert got["engine_setup"]["prepare"]["action"] == "created"
        assert (target / "Assets" / "Scripts" / "PlayerController.cs").is_file()
        assert (target / "Assets" / "BGate" / "Editor" / "BGateScaffold.cs").is_file()
        assert (target / "Assets" / "Tests" / "EditMode" / "StarterSceneTests.cs").is_file()
        assert (target / "Assets" / "Scenes" / "Main.unity").read_text() == "kind 3d"
        deps = json.loads((target / "Packages" / "manifest.json").read_text())["dependencies"]
        assert deps["com.unity.test-framework"] == "1.5.1"

    def test_a_failed_create_is_an_error_not_a_half_project(self, fake_editor, tmp_path,
                                                            monkeypatch):
        from bgate_core.store import scaffold
        monkeypatch.setenv("FAKE_UNITY_MODE", "create_fail")
        target = tmp_path / "game"
        with pytest.raises(scaffold.EngineScaffoldError):
            scaffold.new_project(target, "Neon Drift", kind="2d", engine="unity")
        assert not (target / "Assets").exists()

    def test_an_existing_project_is_never_recreated(self, fake_editor, tmp_path):
        base = _unity_project(tmp_path / "game")
        got = unity.scaffold_prepare(base)
        assert got == {"ok": True, "action": "exists", "path": str(base)}


WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="player builds are Windows-only (StandaloneWindows64)")


class TestPlayer:
    @WINDOWS_ONLY
    def test_build_installs_the_scripts_then_reuses_a_fresh_player(self, fake_editor, tmp_path):
        project = _unity_project(tmp_path / "game")
        got = unity.build_player(project, timeout=60)
        assert got["ok"] is True and got["cached"] is False, got
        exe = Path(got["exe"])
        assert exe.is_file() and exe.parent == project / "Builds" / "BGatePlayer"
        have = unity.scripts_installed(project)
        assert have["agent"] and have["build"] and have["telemetry"]
        os.utime(exe, (exe.stat().st_mtime + 10,) * 2)
        again = unity.build_player(project, timeout=60)
        assert again["ok"] is True and again["cached"] is True
        forced = unity.build_player(project, timeout=60, force=True)
        assert forced["cached"] is False

    @WINDOWS_ONLY
    def test_an_edited_script_makes_the_player_stale(self, fake_editor, tmp_path):
        project = _unity_project(tmp_path / "game")
        exe = Path(unity.build_player(project, timeout=60)["exe"])
        os.utime(exe, (1, 1))
        assert unity.build_player(project, timeout=60)["cached"] is False

    @WINDOWS_ONLY
    def test_a_build_that_writes_no_player_fails(self, fake_editor, tmp_path, monkeypatch):
        project = _unity_project(tmp_path / "game")
        monkeypatch.setenv("FAKE_UNITY_MODE", "build_fail")
        got = unity.build_player(project, timeout=60)
        assert got["ok"] is False and "no player" in got["error"]

    def test_player_args_headless_draws_nothing(self, tmp_path):
        args = unity.player_args("P.exe", log_path=tmp_path / "p.log", headless=True)
        assert "-batchmode" in args and "-nographics" in args
        windowed = unity.player_args("P.exe", log_path=tmp_path / "p.log")
        assert "-batchmode" not in windowed and "-screen-fullscreen" in windowed

    def test_player_log_errors_keep_the_game_and_drop_the_noise(self):
        log = ("NullReferenceException: Object reference not set\n"
               "loading scene\n"
               "d3d11: failed to create staging 2D texture\n"
               "NullReferenceException: Object reference not set\n")
        assert unity.player_log_errors(log) == [
            "NullReferenceException: Object reference not set"]

    def test_the_agent_source_keeps_the_wire_contract(self):
        agent = (unity.TEMPLATE_DIR / unity.AGENT_FILE).read_text(encoding="utf-8")
        for needle in ("BGATE_AGENT_URL", "BGATE_AGENT_EVERY", "BGATE_AGENT_LOCKSTEP",
                       "BGATE_AGENT_SECONDS", "BGATE_SCENE", "interface IBGateObserver",
                       "class BGateInput", "DefaultExecutionOrder", "\\\"progress\\\"",
                       "\\\"danger\\\"", "agent_decision"):
            assert needle in agent, needle
        build = (unity.TEMPLATE_DIR / unity.BUILD_FILE).read_text(encoding="utf-8")
        ns, cls, method = unity.BUILD_METHOD.rsplit(".", 2)
        assert f"namespace {ns}" in build and f"class {cls}" in build
        assert f"public static void {method}()" in build and "BGATE_BUILD_OUT" in build

    def test_the_template_players_read_the_bot(self):
        root = unity.TEMPLATE_DIR.parents[2]
        for rel in ("2d/Assets/Scripts/PlayerController2D.cs",
                    "3d/Assets/Scripts/PlayerController.cs"):
            text = (root / rel).read_text(encoding="utf-8")
            assert "BGateInput.Declare(" in text and "BGateInput.Held(" in text
            assert "BGateInput.Down(\"jump\")" in text

    def test_wired_means_the_games_own_code_reads_the_bot(self, tmp_path):
        from bgate_core.qa import agentplay
        project = _unity_project(tmp_path / "game")
        unity.install_scripts(project, which=("agent",))
        assert agentplay._unity_wired(project) is False
        (project / "Assets" / "Scripts" / "Hero.cs").write_text(
            'if (BGate.BGateInput.Held("move_right")) x += 1;', encoding="utf-8")
        assert agentplay._unity_wired(project) is True

class TestEdits:
    """Import, place and set a field: always the editor's API, never YAML."""

    def _png(self, tmp_path, name="hero.png"):
        src = tmp_path / "art" / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(bytes([0x89]) + b"PNG fake")
        return src

    def test_the_shipped_source_has_every_verb(self):
        edit = (unity.TEMPLATE_DIR / unity.EDIT_FILE).read_text(encoding="utf-8")
        ns, cls = unity.EDIT_CLASS.rsplit(".", 1)
        assert f"namespace {ns}" in edit and f"class {cls}" in edit
        for verb in ("Import", "Place", "SetField"):
            assert f"public static void {verb}()" in edit
        for api in ("AssetDatabase.ImportAsset", "PrefabUtility.InstantiatePrefab",
                    "PrefabUtility.SaveAsPrefabAsset", "EditorSceneManager.SaveScene",
                    "SerializedObject", "BGATE_EDIT_RESULT"):
            assert api in edit, api

    def test_import_copies_installs_the_script_and_reports_the_guid(self, fake_editor, tmp_path):
        project = _unity_project(tmp_path / "game")
        got = unity.import_asset(project, self._png(tmp_path), "Assets/Art/", sprite=True,
                                 pixels_per_unit=16, point_filter=True, timeout=60)
        assert got["ok"] is True, got
        assert got["path"] == "Assets/Art/hero.png" and got["guid"] == "abc"
        assert (project / "Assets" / "Art" / "hero.png").is_file()
        assert unity.scripts_installed(project)["edit"]
        env = got["env"]
        assert env["BGATE_IMPORT_SPRITE"] == "1" and env["BGATE_IMPORT_POINT"] == "1"
        assert env["BGATE_IMPORT_PPU"] == "16.0"
        assert "BGate.Editor.BGateEdit.Import" in got["command"]

    def test_import_defaults_to_assets_imported_and_will_not_clobber(self, fake_editor, tmp_path):
        project = _unity_project(tmp_path / "game")
        src = self._png(tmp_path)
        assert unity.import_asset(project, src, timeout=60)["path"] == "Assets/Imported/hero.png"
        again = unity.import_asset(project, src, timeout=60)
        assert again["ok"] is False and again["exists"] is True and "overwrite" in again["error"]
        assert unity.import_asset(project, src, overwrite=True, timeout=60)["ok"] is True

    @pytest.mark.parametrize("dest,needle", [
        ("../outside.png", "under Assets/"),
        ("Assets/../ProjectSettings/x.png", "under Assets/"),
        ("Packages/x.png", "under Assets/"),
        ("Assets/Art/hero.jpg", "extension"),
    ])
    def test_import_refuses_a_destination_outside_assets(self, fake_editor, tmp_path, dest, needle):
        project = _unity_project(tmp_path / "game")
        got = unity.import_asset(project, self._png(tmp_path), dest, timeout=60)
        assert got["ok"] is False and needle in got["error"]
        assert not (tmp_path / "outside.png").exists()

    @pytest.mark.parametrize("name", ["Main.unity", "Crate.prefab", "hero.png.meta"])
    def test_import_refuses_the_editors_own_serialized_files(self, fake_editor, tmp_path, name):
        project = _unity_project(tmp_path / "game")
        src = tmp_path / name
        src.write_text("%YAML 1.1", encoding="utf-8")
        got = unity.import_asset(project, src, timeout=60)
        assert got["ok"] is False and "never copied in" in got["error"]

    def test_import_is_refused_while_the_editor_holds_the_project(self, fake_editor, tmp_path,
                                                                  monkeypatch):
        project = _unity_project(tmp_path / "game")
        _hold_lock(monkeypatch, project)
        got = unity.import_asset(project, self._png(tmp_path), timeout=60)
        assert got["ok"] is False and got["editor_open"] is True
        assert not (project / "Assets" / "Imported").exists()

    def test_a_failed_import_of_a_new_file_leaves_nothing_behind(self, fake_editor, tmp_path,
                                                                 monkeypatch):
        project = _unity_project(tmp_path / "game")
        monkeypatch.setenv("FAKE_UNITY_MODE", "edit_fail")
        got = unity.import_asset(project, self._png(tmp_path), "Assets/Art/", timeout=60)
        assert got["ok"] is False
        assert not (project / "Assets" / "Art" / "hero.png").exists()
        assert not (project / "Assets" / "Art" / "hero.png.meta").exists()
        assert "copied" not in got and len(got["removed"]) == 1

    def test_a_failed_overwrite_keeps_the_file_that_was_there(self, fake_editor, tmp_path,
                                                              monkeypatch):
        project = _unity_project(tmp_path / "game")
        src = self._png(tmp_path)
        assert unity.import_asset(project, src, "Assets/Art/", timeout=60)["ok"] is True
        monkeypatch.setenv("FAKE_UNITY_MODE", "edit_fail")
        got = unity.import_asset(project, src, "Assets/Art/", overwrite=True, timeout=60)
        assert got["ok"] is False and "removed" not in got
        assert (project / "Assets" / "Art" / "hero.png").is_file()
        assert (project / "Assets" / "Art" / "hero.png.meta").is_file()

    def test_place_passes_the_transform_as_text(self, fake_editor, tmp_path):
        project = _unity_project(tmp_path / "game")
        got = unity.place_prefab(project, "Assets/Prefabs/Crate.prefab",
                                 scene="Assets/Scenes/Main.unity", name="Crate1",
                                 parent="Level", position=[1, 0.5, -2], rotation="0,90,0",
                                 timeout=60)
        assert got["ok"] is True, got
        env = got["env"]
        assert env["BGATE_PREFAB"] == "Assets/Prefabs/Crate.prefab"
        assert env["BGATE_POS"] == "1.0,0.5,-2.0" and env["BGATE_ROT"] == "0.0,90.0,0.0"
        assert env.get("BGATE_SCALE", "") == "" and env["BGATE_PARENT"] == "Level"
        assert got["object"] == "Crate1"

    def test_place_refuses_bad_input_without_starting_the_editor(self, fake_editor, tmp_path):
        project = _unity_project(tmp_path / "game")
        assert "inside" in unity.place_prefab(project, str(tmp_path / "x.prefab"))["error"]
        assert ".unity" in unity.place_prefab(project, "Assets/x.prefab",
                                              scene="Assets/Main.txt")["error"]
        assert "three numbers" in unity.place_prefab(project, "Assets/x.prefab",
                                                     position=[1, 2])["error"]

    def test_set_field_encodes_values_and_reports_before_and_after(self, fake_editor, tmp_path):
        project = _unity_project(tmp_path / "game")
        got = unity.set_field(project, "PlayerController", "speed", 7.5,
                              object_path="Player", timeout=60)
        assert got["ok"] is True, got
        assert got["before"] == "5" and got["after"] == "7.5"
        assert got["env"]["BGATE_OBJECT"] == "Player"
        assert unity._value_text([1, 2, 3]) == "1,2,3"
        assert unity._value_text(True) == "true" and unity._value_text(None) == "null"

    def test_set_field_on_a_prefab_needs_no_object_path(self, fake_editor, tmp_path):
        project = _unity_project(tmp_path / "game")
        got = unity.set_field(project, "Light", "m_Intensity", "2",
                              prefab="Assets/Prefabs/Lamp.prefab", add=True, timeout=60)
        assert got["ok"] is True and got["asset"] == "Assets/Prefabs/Lamp.prefab"
        assert got["env"]["BGATE_ADD"] == "1" and "BGATE_SCENE" not in got["env"]

    def test_set_field_refusals(self, fake_editor, tmp_path):
        project = _unity_project(tmp_path / "game")
        assert "object_path" in unity.set_field(project, "X", "y", 1)["error"]
        assert "not both" in unity.set_field(project, "X", "y", 1, scene="Assets/a.unity",
                                             prefab="Assets/a.prefab")["error"]
        assert ".prefab" in unity.set_field(project, "X", "y", 1,
                                            prefab="Assets/a.fbx")["error"]
        assert "required" in unity.set_field(project, "", "y", 1)["error"]

    def test_the_editors_error_is_the_result(self, fake_editor, tmp_path, monkeypatch):
        project = _unity_project(tmp_path / "game")
        monkeypatch.setenv("FAKE_UNITY_MODE", "edit_fail")
        got = unity.set_field(project, "Rigidbody", "mass", 2, object_path="Player",
                              timeout=60)
        assert got["ok"] is False and "no Rigidbody" in got["error"]

    def test_a_clean_exit_with_no_result_is_a_failure(self, fake_editor, tmp_path, monkeypatch):
        project = _unity_project(tmp_path / "game")
        monkeypatch.setenv("FAKE_UNITY_MODE", "edit_silent")
        got = unity.place_prefab(project, "Assets/x.prefab", timeout=60)
        assert got["ok"] is False and "wrote no result" in got["error"]

    def test_the_tools_are_unity_owned_and_seated(self):
        from bgate_core import seattools
        from bgate_core.store import modules
        for name in ("unity_import_asset", "unity_place_prefab", "unity_set_field"):
            assert engines.tool_owner(name) == "unity"
            assert not engines.tool_enabled(name, "godot")
            assert name in modules.SPINE_GROUPS["engine"]
        assert "unity_import_asset" in seattools.SEATS["art"]
        assert "unity_place_prefab" in seattools.SEATS["level"]
