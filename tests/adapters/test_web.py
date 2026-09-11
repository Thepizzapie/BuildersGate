"""The web engine adapter, phase 3.

The payload budget is the assertion that matters most here. A finished Godot 3D
game exported to the web at 661 MB is what it exists to catch, and nothing in
the pipeline had ever asked what a build weighed.
"""
from __future__ import annotations

import json
import os

import pytest

from bgate_adapters import web
from bgate_core.runtime import engines
from bgate_core.store import project, scaffold


class TestBinary:
    def test_node_is_found_or_the_reason_is_named(self):
        got = web.available()
        assert "available" in got
        if not got["available"]:
            assert got["reason"]

    def test_bgate_node_overrides_and_refuses_a_missing_file(self, monkeypatch,
                                                             tmp_path):
        monkeypatch.setenv("BGATE_NODE", str(tmp_path / "not-here.exe"))
        with pytest.raises(web.NodeNotFound) as exc:
            web.find_node()
        assert "BGATE_NODE" in str(exc.value)

    def test_the_registry_reaches_it_through_find_binary(self):
        assert callable(web.find_binary)
        assert engines.supported("web") is True


class TestManifest:
    def test_a_missing_manifest_is_not_a_web_project(self, tmp_path):
        with pytest.raises(web.WebProjectError):
            web.manifest(tmp_path)
        assert web.scripts(tmp_path) == {}

    def test_a_broken_manifest_is_a_harder_failure_than_a_missing_one(self,
                                                                     tmp_path):
        (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(web.WebProjectError) as exc:
            web.manifest(tmp_path)
        assert "readable JSON" in str(exc.value)

    def test_a_project_without_node_modules_says_so_before_npm_does(self,
                                                                   tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps({"name": "x", "scripts": {"build": "vite build"}}),
            encoding="utf-8")
        got = web.check_project(str(tmp_path))
        assert got["ok"] is False
        assert got["installed"] is False
        assert "npm install" in got["error"]
        # The point of the branch: npm's own error names a missing binary, not
        # the missing install that caused it.
        assert "does not install dependencies" in got["error"]

    def test_a_project_with_nothing_to_check_says_that(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
        (tmp_path / "node_modules").mkdir()
        got = web.check_project(str(tmp_path))
        assert got["ok"] is False
        assert "neither a `build` nor a `typecheck`" in got["error"]


class TestPayloadBudget:
    """The check the 661 MB export did not have."""

    @staticmethod
    def _dist(tmp_path):
        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
        return dist

    def test_it_measures_the_wire_not_the_disk(self, tmp_path):
        dist = self._dist(tmp_path)
        # 8 MB of zeroes on disk is a few KB over the wire. A budget measured
        # against disk bytes is one people learn to ignore.
        (dist / "assets" / "level.glb").write_bytes(b"\0" * 8_000_000)
        got = web.payload(dist)
        big = got["biggest"][0]
        assert big["disk_bytes"] == 8_000_000
        assert big["bytes"] < 100_000
        assert got["within_budget"] is True

    def test_the_bundlers_own_bookkeeping_is_not_a_download(self, tmp_path):
        dist = self._dist(tmp_path)
        (dist / ".vite").mkdir()
        (dist / ".vite" / "manifest.json").write_text(
            '{"x": "' + "y" * 500_000 + '"}', encoding="utf-8")
        got = web.payload(dist)
        assert not any(".vite" in f["file"] for f in got["biggest"])

    def test_already_compressed_files_are_not_double_counted(self, tmp_path):
        dist = self._dist(tmp_path)
        blob = os.urandom(2_000_000)
        (dist / "assets" / "hero.png").write_bytes(blob)
        got = web.payload(dist)
        big = next(f for f in got["biggest"] if f["file"].endswith("hero.png"))
        # gzip of random data is LARGER than the input; reporting that would
        # make the budget lie in the expensive direction.
        assert big["bytes"] == len(blob)

    def test_sourcemaps_are_shipped_but_never_downloaded(self, tmp_path):
        dist = self._dist(tmp_path)
        (dist / "assets" / "app.js").write_text("x=1;\n" * 100, encoding="utf-8")
        (dist / "assets" / "app.js.map").write_text("y" * 5_000_000,
                                                    encoding="utf-8")
        got = web.payload(dist)
        assert not any(f["file"].endswith(".map") for f in got["biggest"])

    def test_over_budget_fails_and_names_the_biggest_contributors(self, tmp_path):
        dist = self._dist(tmp_path)
        (dist / "assets" / "huge.png").write_bytes(os.urandom(3_000_000))
        (dist / "assets" / "small.png").write_bytes(os.urandom(1000))
        got = web.payload(dist, budget_bytes=1_000_000)
        assert got["ok"] is False
        assert got["within_budget"] is False
        assert got["share_of_budget"] > 1
        assert got["biggest"][0]["file"].endswith("huge.png")
        assert "661 MB" in got["error"]

    def test_measuring_a_build_that_is_not_there_says_so(self, tmp_path):
        got = web.payload(tmp_path / "dist")
        assert got["ok"] is False
        assert "build before measuring" in got["error"]

    def test_the_default_budget_is_declared_not_inline(self):
        assert web.DEFAULT_BUDGET_BYTES == 25 * 1024 * 1024


class TestBrowser:
    def test_the_two_installs_are_reported_apart(self):
        """Package present and browser absent is a REAL state on a fresh box.

        Reporting "playwright: yes" off the import alone promises screenshots on
        a machine that cannot take one, the same mistake the art_key doctor row
        had to unlearn.
        """
        got = web.browser_available()
        assert set(got) >= {"available", "package", "browser"}
        if got["package"] and not got["browser"]:
            assert "playwright install" in got["reason"]
        if not got["available"]:
            assert got["reason"]

    def test_a_screenshot_without_a_browser_refuses_rather_than_raising(self,
                                                                       tmp_path):
        if web.browser_available()["available"]:
            pytest.skip("a browser is installed; the refusal path needs it gone")
        got = web.screenshot("http://127.0.0.1:1/", str(tmp_path / "s.png"))
        assert got["ok"] is False
        assert got["error"]


class TestDevServerBookkeeping:
    def test_no_pidfile_means_not_running(self, tmp_path):
        assert web.dev_status(tmp_path) == {"running": False}

    def test_a_stale_pidfile_is_cleaned_rather_than_obeyed(self, tmp_path):
        # A killed server that leaves its file behind would otherwise make
        # dev_start refuse forever, the bug the engine lock's TTL prevents.
        (tmp_path / web._PIDFILE).write_text(
            json.dumps({"pid": 999_999_999, "port": 5173}), encoding="utf-8")
        got = web.dev_status(tmp_path)
        assert got["running"] is False
        assert got["stale"] is True
        assert not (tmp_path / web._PIDFILE).exists()

    def test_a_live_pid_that_is_not_ours_is_stale(self, tmp_path, monkeypatch):
        # Pids are recycled: a pidfile from a dead server can name whatever the
        # OS handed that number to next, and dev_stop kills the tree under it.
        monkeypatch.setattr(web, "_image_name", lambda pid: "explorer.exe")
        (tmp_path / web._PIDFILE).write_text(
            json.dumps({"pid": 4242, "port": 5173}), encoding="utf-8")
        assert web.dev_status(tmp_path)["running"] is False
        assert not (tmp_path / web._PIDFILE).exists()

    def test_our_own_process_reads_as_ours_by_image_name(self):
        # Asked of a real pid so the tasklist/ps parsing is exercised, not
        # stubbed: this interpreter is python, which is deliberately NOT in the
        # allowed set, and its name must still come back.
        import os

        name = web._image_name(os.getpid())
        assert name.startswith("python"), name
        assert web._alive(os.getpid()) is False
        assert web._image_name(999_999_999) == ""

    def test_starting_without_a_dev_script_refuses(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
        (tmp_path / "node_modules").mkdir()
        got = web.dev_start(str(tmp_path))
        assert got["ok"] is False
        assert "no `dev` script" in got["error"]


class TestRunScript:
    def test_it_takes_a_path_and_says_so_when_given_source(self, tmp_path):
        got = web.run_script("console.log('hi')", project_dir=str(tmp_path))
        assert got["ok"] is False
        assert "takes a path, not source" in got["error"]

    def test_a_file_outside_the_project_is_refused(self, tmp_path):
        outside = tmp_path / "elsewhere" / "x.js"
        outside.parent.mkdir()
        outside.write_text("console.log(1)", encoding="utf-8")
        project = tmp_path / "game"
        project.mkdir()
        for given in (str(outside), "../elsewhere/x.js"):
            got = web.run_script(given, project_dir=str(project))
            assert got["ok"] is False
            assert "outside" in got["error"]

    def test_typescript_is_refused_before_node_sees_it(self, tmp_path):
        (tmp_path / "a.ts").write_text("const x: number = 1;", encoding="utf-8")
        got = web.run_script("a.ts", project_dir=str(tmp_path))
        assert got["ok"] is False
        assert "TypeScript" in got["error"]

    @pytest.mark.skipif(not web.available()["available"], reason="needs node")
    def test_it_runs_a_real_file(self, tmp_path):
        (tmp_path / "hello.js").write_text(
            "console.log('from node');", encoding="utf-8")
        got = web.run_script("hello.js", project_dir=str(tmp_path))
        assert got["ok"] is True, got
        assert "from node" in got["stdout"]


class TestScaffold:
    """The template on the engine axis, stamped for real."""

    def test_web_templates_exist_for_both_kinds(self):
        got = {t["kind"]: t for t in scaffold.list_templates("web")}
        assert set(got) == {"2d", "3d"}
        assert all(t["available"] for t in got.values())
        assert all(t["engine"] == "web" for t in got.values())

    def test_an_engine_with_no_template_lists_nothing(self):
        assert scaffold.list_templates("unity") == []
        with pytest.raises(ValueError):
            scaffold.engine_dir("unity")

    def test_it_stamps_a_runnable_shape(self, tmp_path):
        made = scaffold.new_project(tmp_path / "g", "Neon Drift", kind="2d",
                                    engine="web")
        assert made["engine"] == "web"
        files = set(made["files"])
        for wanted in ("package.json", "index.html", "tsconfig.json",
                       "vite.config.ts", "src/main.ts", "src/game.ts",
                       "src/game.test.ts", "src/tunables.ts",
                       "src/bgate/telemetry.ts", "CLAUDE.md"):
            assert wanted in files, wanted

    def test_the_manifest_name_is_the_slug_because_npm_refuses_the_other(
            self, tmp_path):
        """`npm install` dies on "Invalid name" before fetching anything.

        A display name with a space or a capital in package.json breaks the very
        first command the scaffold tells you to run.
        """
        scaffold.new_project(tmp_path / "g", "Neon Drift", kind="2d",
                             engine="web")
        got = json.loads((tmp_path / "g" / "package.json").read_text(
            encoding="utf-8"))
        assert got["name"] == "neon-drift"
        # ...and the DISPLAY name still reaches the places a human reads.
        assert "Neon Drift" in (tmp_path / "g" / "index.html").read_text(
            encoding="utf-8")
        assert (tmp_path / "g" / "CLAUDE.md").read_text(
            encoding="utf-8").startswith("# Neon Drift")

    def test_no_token_survives_into_the_stamped_project(self, tmp_path):
        scaffold.new_project(tmp_path / "g", "Neon Drift", kind="3d",
                             engine="web")
        for path in (tmp_path / "g").rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore")
                assert "__PROJECT_NAME__" not in text, path
                assert "__PROJECT_SLUG__" not in text, path

    def test_the_telemetry_module_keeps_the_godot_wire_contract(self):
        """Same transport as the Godot web export, deliberately.

        Matching it means the recorder, the aligner and every downstream
        analysis work on a web game with no changes at all.
        """
        source = (scaffold.TEMPLATES_DIR / "web" / "shared" / "src" / "bgate"
                  / "telemetry.ts").read_text(encoding="utf-8")
        assert "/api/playtest/status" in source
        assert "/api/playtest/${this.session}/events" in source
        assert "SCHEMA_VERSION = 1" in source
        assert "schema: SCHEMA_VERSION" in source


class TestProjectWiring:
    def test_a_scaffolded_web_project_records_its_engine(self, tmp_path):
        root = tmp_path / "g"
        scaffold.new_project(root, "Neon Drift", kind="2d", engine="web")
        project.init(root, "Neon Drift", engine="web", dimension="2d")
        assert project.engine_of(root) == "web"
        assert project.engine_dir(root) == (root, "web")
        # game_dir stays Godot-scoped; the caller has to name the engine.
        assert project.game_dir(root) is None
        assert project.game_dir(root, engine="web") == root

    def test_adopt_detects_a_web_project_instead_of_calling_it_nothing(
            self, tmp_path):
        from bgate_core.store import adopt

        root = tmp_path / "g"
        scaffold.new_project(root, "Neon Drift", kind="2d", engine="web")
        got = adopt.detect(root)
        assert got["engine"] == "web"
        assert got["engine_label"] == "Web"
        assert got["engine_supported"] is True
        assert got["godot"] is False


class TestDevServerBinding:
    """The two bugs a real run found, and neither would fail a unit test.

    MEASURED, on this machine: vite's default host is the string "localhost",
    which on Windows binds ::1 ONLY. The server was up and serving, and every
    probe of http://127.0.0.1:<port>/ failed for the full 90-second timeout -
    and the timeout path then killed the npm shim, orphaning the vite child that
    held the port, so every later start failed on --strictPort for a server
    nothing was tracking and nobody could stop.
    """

    def test_the_url_it_returns_is_the_address_it_binds(self):
        import inspect

        source = inspect.getsource(web.dev_start)
        assert '"--host", _HOST' in source
        assert 'f"http://{_HOST}:{port}/"' in source

    def test_a_timed_out_start_kills_the_tree_not_the_shim(self):
        import inspect

        source = inspect.getsource(web.dev_start)
        assert "_kill_tree(proc.pid)" in source
        assert "proc.terminate()" not in source

    def test_stop_and_the_timeout_path_share_one_killer(self):
        import inspect

        assert "_kill_tree(pid)" in inspect.getsource(web.dev_stop)


class TestWebTelemetryStatus:
    """playtest_check on a web project asks about telemetry.ts, not an autoload."""

    def _web(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
        (tmp_path / "src").mkdir()
        return tmp_path

    def test_a_missing_module_is_installable(self, tmp_path):
        from bgate_core.store import adopt
        root = self._web(tmp_path)
        got = adopt.telemetry_status(root)
        assert got["ok"] is False and got["installable"] is True
        put = adopt.install_telemetry(root)
        assert put["action"] == "installed"
        assert (root / "src" / "bgate" / "telemetry.ts").is_file()
        # Present but unimported is still not ok, and says so.
        again = adopt.telemetry_status(root)
        assert again["ok"] is False and "imports" in again["reason"]
        assert adopt.install_telemetry(root)["action"] == "unchanged"

    def test_an_imported_module_is_ok(self, tmp_path):
        from bgate_core.store import adopt
        root = self._web(tmp_path)
        adopt.install_telemetry(root)
        (root / "src" / "main.ts").write_text(
            'import { telemetry } from "./bgate/telemetry";\n', encoding="utf-8")
        assert adopt.telemetry_status(root)["ok"] is True

    def test_playtest_hints_and_launch_target_come_from_index_html(self, tmp_path):
        from bgate_core.qa import playtest
        root = self._web(tmp_path)
        (root / "index.html").write_text(
            "<html><head><title>Neon Drift</title></head></html>", encoding="utf-8")
        assert playtest.game_window_hints(root) == ["Neon Drift"]
        assert playtest._web_project_dir(root) == root
        # A Godot project with a tooling package.json is still Godot.
        (root / "project.godot").write_text("\n", encoding="utf-8")
        assert playtest._web_project_dir(root) is None

    def test_preflight_names_the_web_toolchain(self, tmp_path, monkeypatch):
        from bgate_core.qa import playtest
        root = self._web(tmp_path)
        got = playtest.preflight(root=str(root), native=True)["checks"]["native_game"]
        assert got["engine"] == "web"
        assert got["ok"] is False and "npm install" in got["reason"]
