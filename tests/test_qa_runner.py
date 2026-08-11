"""godot_test_run — the QA seat's missing test runner.

The seat's mission is "Own tests, repro, regression" and there was no MCP tool
that ran a test: the only path was hand-rolling a godot_run per script and
counting PASS lines by eye, which is not a thing a fail/reopen gate can be built
on. The brief demands "tests at the known baseline, no new failures", so the
result has to be structured enough to answer that in one call.

The engine is faked here on purpose. What is under test is discovery (both
project layouts), scoring (exit code is NOT enough — Godot prints SCRIPT ERROR
and exits 0), and the no-tests case, none of which need a 170 MB binary. The
real subprocess path is bgate_adapters/godot.py's, tested there.
"""
from __future__ import annotations

import json

import pytest

from bgate_adapters import godot as _godot
from bgate_mcp import server


async def call(tool: str, /, **kwargs) -> dict:
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


@pytest.fixture()
def wired(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return root


def _godot_project(root, layout: str = "game"):
    """A project.godot in one of the two layouts the repo actually ships."""
    base = (root / "game") if layout == "game" else root
    base.mkdir(parents=True, exist_ok=True)
    (base / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    return base


def _fake_engine(monkeypatch, outputs: dict, seen: list | None = None):
    """Stand in for a headless Godot: keyed on a marker in the script source."""
    def run_script(script, project_dir=None, timeout=120):
        if seen is not None:
            seen.append({"script": script, "project_dir": project_dir,
                         "timeout": timeout})
        for marker, result in outputs.items():
            if marker in script:
                return {"stdout": "", "stderr": "", "exit_code": 0,
                        "seconds": 0.1, "errors": [], "ok": True, **result}
        return {"ok": True, "stdout": "", "stderr": "", "exit_code": 0,
                "seconds": 0.1, "errors": []}

    monkeypatch.setattr(_godot, "run_script", run_script)


@pytest.mark.anyio
class TestNoTests:
    """"0 failures" out of nothing run is the most misleading answer available:
    a regression gate that silently tests nothing looks exactly like a green one."""

    async def test_a_project_with_no_godot_project_says_so(self, wired):
        got = await call("godot_test_run")
        assert got["ok"] is False and got["no_tests"] is True
        assert "project.godot" in got["error"]

    async def test_a_project_with_no_test_scripts_is_not_a_pass(self, wired):
        _godot_project(wired)
        got = await call("godot_test_run")
        assert got["ok"] is False and got["no_tests"] is True
        assert got["scripts_run"] == 0
        # Where it looked, so the answer is actionable rather than a shrug.
        assert "tests" in got["tests_dir"]
        assert "no regression baseline" in got["error"]

    async def test_a_named_script_that_does_not_exist_is_named_back(self, wired):
        _godot_project(wired)
        got = await call("godot_test_run", paths=["tests/ghost.gd"])
        assert got["no_tests"] is True and got["missing"] == ["tests/ghost.gd"]


@pytest.mark.anyio
class TestDiscovery:
    async def test_it_finds_tests_in_the_scaffold_layout(self, wired, monkeypatch):
        base = _godot_project(wired, "game")
        (base / "tests").mkdir()
        (base / "tests" / "fight_test.gd").write_text("# MARK\nPASS\n",
                                                      encoding="utf-8")
        _fake_engine(monkeypatch, {"MARK": {"stdout": "PASS one\nPASS two\n"}})
        got = await call("godot_test_run")
        assert got["ok"] is True
        assert [s["script"] for s in got["scripts"]] == ["tests/fight_test.gd"]

    async def test_it_finds_tests_in_the_cli_layout_too(self, wired, monkeypatch):
        # `bgate init` puts project.godot at the ROOT. Everything that hardcoded
        # <root>/game silently did nothing for every CLI-created project.
        base = _godot_project(wired, "root")
        (base / "tests").mkdir()
        (base / "tests" / "smoke_test.gd").write_text("# MARK\n", encoding="utf-8")
        _fake_engine(monkeypatch, {"MARK": {"stdout": "PASS\n"}})
        got = await call("godot_test_run")
        assert got["scripts_run"] == 1
        assert got["godot_project"] == str(base)

    async def test_the_scripts_run_against_the_godot_project(self, wired,
                                                             monkeypatch):
        base = _godot_project(wired)
        (base / "tests").mkdir()
        (base / "tests" / "a_test.gd").write_text("# MARK\nbody\n",
                                                  encoding="utf-8")
        seen: list = []
        _fake_engine(monkeypatch, {"MARK": {"stdout": "PASS\n"}}, seen)
        await call("godot_test_run", timeout=42)
        assert seen[0]["project_dir"] == str(base)
        assert seen[0]["timeout"] == 42
        # The script's OWN source is what runs — res:// loads inside it resolve
        # because --path points at the project.
        assert "body" in seen[0]["script"]


@pytest.mark.anyio
class TestScoring:
    def _suite(self, root):
        base = _godot_project(root)
        (base / "tests").mkdir()
        (base / "tests" / "green_test.gd").write_text("# GREEN\n", encoding="utf-8")
        (base / "tests" / "red_test.gd").write_text("# RED\n", encoding="utf-8")
        return base

    async def test_per_script_verdicts_and_the_one_number_the_brief_wants(
            self, wired, monkeypatch):
        self._suite(wired)
        _fake_engine(monkeypatch, {
            "GREEN": {"stdout": "PASS door opens\nPASS door shuts\n"},
            "RED": {"stdout": "PASS door opens\nFAIL door eats the player\n"},
        })
        got = await call("godot_test_run")

        assert got["ok"] is False
        assert got["scripts_run"] == 2 and got["scripts_failed"] == 1
        assert got["failures"] == ["tests/red_test.gd"]
        assert got["assertions_passed"] == 3 and got["assertions_failed"] == 1

        by_name = {s["script"]: s for s in got["scripts"]}
        assert by_name["tests/green_test.gd"]["ok"] is True
        assert by_name["tests/red_test.gd"]["ok"] is False
        # The failure carries its output; the pass does not — a green suite's
        # stdout is engine boot chatter and returning it for every script is how
        # a passing run stops fitting in a tool result.
        assert "eats the player" in by_name["tests/red_test.gd"]["output"]
        assert "output" not in by_name["tests/green_test.gd"]

    async def test_a_clean_suite_reports_ok_with_no_error_string(self, wired,
                                                                monkeypatch):
        self._suite(wired)
        _fake_engine(monkeypatch, {"GREEN": {"stdout": "PASS\n"},
                                   "RED": {"stdout": "PASS\n"}})
        got = await call("godot_test_run")
        assert got["ok"] is True and not got["error"]
        assert got["scripts_failed"] == 0

    async def test_a_script_error_fails_the_script_even_at_exit_zero(
            self, wired, monkeypatch):
        # Godot prints SCRIPT ERROR and still exits 0. Scoring on the exit code
        # alone would call a suite that never ran an assertion green.
        self._suite(wired)
        _fake_engine(monkeypatch, {
            "GREEN": {"stdout": "PASS\n"},
            "RED": {"stdout": "PASS\n", "exit_code": 0, "ok": False,
                    "errors": ["SCRIPT ERROR: Invalid call on null instance"]},
        })
        got = await call("godot_test_run")
        assert got["failures"] == ["tests/red_test.gd"]
        red = [s for s in got["scripts"] if s["script"].endswith("red_test.gd")][0]
        assert red["errors"] and red["ok"] is False

    async def test_a_timeout_is_a_failure_that_names_itself(self, wired,
                                                            monkeypatch):
        self._suite(wired)
        _fake_engine(monkeypatch, {
            "GREEN": {"stdout": "PASS\n"},
            "RED": {"ok": False, "error": "Godot timed out after 180s",
                    "seconds": 180},
        })
        got = await call("godot_test_run")
        red = [s for s in got["scripts"] if s["script"].endswith("red_test.gd")][0]
        assert "timed out" in red["error"]

    async def test_an_explicit_subset_runs_only_that(self, wired, monkeypatch):
        self._suite(wired)
        _fake_engine(monkeypatch, {"GREEN": {"stdout": "PASS\n"},
                                   "RED": {"stdout": "FAIL\n"}})
        got = await call("godot_test_run", paths=["tests/green_test.gd"])
        assert got["ok"] is True and got["scripts_run"] == 1

    async def test_the_engine_being_absent_comes_back_as_a_payload(self, wired,
                                                                   monkeypatch):
        self._suite(wired)

        def boom(*_args, **_kwargs):
            raise _godot.GodotNotFound("Godot not found.")

        monkeypatch.setattr(_godot, "run_script", boom)
        got = await call("godot_test_run")
        assert got.get("ok") is False and "Godot not found" in got["error"]
