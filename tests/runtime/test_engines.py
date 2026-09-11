"""The engine registry, phase 1 of giving project.engine teeth.

The column existed since migration 0001 and changed nothing; these are the
assertions that say what it now decides.
"""
from __future__ import annotations

import pytest

from bgate_core.runtime import doctor, engines
from bgate_core.store import project


class TestRegistry:
    def test_godot_is_the_default_and_every_engine_is_legal(self):
        assert engines.DEFAULT == "godot"
        assert set(engines.names()) == {"godot", "web", "unity", "none"}
        assert project.ENGINES == engines.names()

    def test_an_unknown_engine_is_refused_by_name(self):
        with pytest.raises(ValueError) as exc:
            engines.spec("unreal")
        assert "unreal" in str(exc.value)

    def test_declared_but_undriven_engines_say_so_instead_of_importing(self):
        # "none" is in the table so a directory with no game is a legal row.
        # Nothing must read that as "there is an adapter".
        assert engines.supported("godot") is True
        assert engines.supported("web") is True         # since phase 3
        assert engines.supported("unity") is True       # since the adapter
        assert engines.supported("none") is False
        with pytest.raises(engines.EngineUnsupported):
            engines.adapter("none")

    def test_binary_reaches_the_adapter_through_one_name(self):
        # The 13 scattered find_godot() calls collapse into this. The adapter
        # must expose find_binary() with no arguments for the registry to work.
        from bgate_adapters import godot

        assert callable(godot.find_binary)

    def test_label_of_a_stored_value_we_no_longer_know_reads_as_itself(self):
        # Called to render a row that already exists; it must not take a page
        # down over a value from an older install.
        assert engines.label("godot") == "Godot"
        assert engines.label("unreal") == "unreal"


class TestDetection:
    def test_each_engine_is_found_by_its_own_marker(self, tmp_path):
        (tmp_path / "project.godot").write_text("[application]", encoding="utf-8")
        assert engines.detect_engine(tmp_path) == "godot"

        web = tmp_path / "w"
        web.mkdir()
        (web / "package.json").write_text("{}", encoding="utf-8")
        assert engines.detect_engine(web) == "web"

        unity = tmp_path / "u"
        (unity / "ProjectSettings").mkdir(parents=True)
        (unity / "ProjectSettings" / "ProjectVersion.txt").write_text(
            "m_EditorVersion: 2022.3.0f1", encoding="utf-8")
        assert engines.detect_engine(unity) == "unity"

    def test_a_godot_project_with_a_package_json_is_still_godot(self):
        """The reason DETECT_ORDER is not alphabetical.

        package.json is written by anything with a build step, a Godot game
        with a tooling dependency has one. Checking the weak marker first would
        relabel that game as a web project on a file that has nothing to do with
        its engine.
        """
        assert engines.DETECT_ORDER.index("godot") < engines.DETECT_ORDER.index("web")

    def test_a_directory_with_no_marker_is_not_an_engine_project(self, tmp_path):
        (tmp_path / "notes.md").write_text("hi", encoding="utf-8")
        assert engines.detect_engine(tmp_path) == ""


class TestGameDirStaysNarrow:
    """game_dir must NOT widen to every engine, see its docstring.

    Thirty call sites take its answer and do Godot things with it. A web game
    adopted as a Godot game with an empty config is worse than one that is not
    recognised.
    """

    def test_the_two_candidate_search_is_unchanged(self, tmp_path):
        game = tmp_path / "game"
        game.mkdir()
        (game / "project.godot").write_text("[application]", encoding="utf-8")
        assert project.game_dir(tmp_path) == game

        flat = tmp_path / "flat"
        flat.mkdir()
        (flat / "project.godot").write_text("[application]", encoding="utf-8")
        assert project.game_dir(flat) == flat

    def test_a_web_directory_is_not_a_game_dir(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert project.game_dir(tmp_path) is None
        # ...but the general resolver names it correctly.
        where, engine = project.engine_dir(tmp_path)
        assert where == tmp_path and engine == "web"

    def test_game_dir_answers_for_an_engine_a_caller_names(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert project.game_dir(tmp_path, engine="web") == tmp_path

    def test_engine_dir_prefers_the_game_subdirectory(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        game = tmp_path / "game"
        game.mkdir()
        (game / "project.godot").write_text("[application]", encoding="utf-8")
        assert project.engine_dir(tmp_path) == (game, "godot")


class TestProjectRow:
    def test_a_new_project_still_records_godot(self, tmp_path):
        project.init(tmp_path, "Engine Test")
        assert project.get(tmp_path)["engine"] == "godot"
        assert project.engine_of(tmp_path) == "godot"

    def test_set_engine_changes_it_and_reports_the_previous_value(self, tmp_path):
        project.init(tmp_path, "Engine Test")
        after = project.set_engine(tmp_path, "web")
        assert after["engine"] == "web"
        assert project.engine_of(tmp_path) == "web"

    def test_set_engine_refuses_a_value_the_registry_does_not_know(self, tmp_path):
        project.init(tmp_path, "Engine Test")
        with pytest.raises(ValueError):
            project.set_engine(tmp_path, "unreal")
        assert project.engine_of(tmp_path) == "godot"

    def test_init_accepts_every_registered_engine(self, tmp_path):
        for name in engines.names():
            root = tmp_path / name
            project.init(root, f"Game {name}", engine=name)
            assert project.get(root)["engine"] == name

    def test_engine_of_defaults_rather_than_raising(self, tmp_path):
        """A missing capability must only come from a stored decision.

        engine_of runs from tool registration and from doctor, both of which
        fire before anyone has checked a project exists.
        """
        assert project.engine_of(tmp_path / "nothing-here") == "godot"


class TestDoctorRows:
    def test_a_row_no_engine_claims_is_graded_for_everyone(self):
        for engine in engines.names():
            assert engines.doctor_row_enabled("python", engine)
            assert engines.doctor_row_enabled("art_key", engine)

    def test_an_engine_only_row_is_graded_for_that_engine_alone(self):
        assert engines.doctor_row_enabled("godot", "godot")
        assert engines.doctor_row_enabled("godot_web_templates", "godot")
        assert not engines.doctor_row_enabled("godot", "web")
        assert not engines.doctor_row_enabled("godot_web_templates", "unity")

    def test_every_engine_row_is_a_real_doctor_row(self):
        # Ghost guard, the same one SPINE has: a renamed doctor row left behind
        # in the registry silently un-grades a real dependency. Phase 1 declared
        # node and playwright ahead of the adapter that needed them; phase 3
        # built the probes, so nothing may be unmatched now.
        unknown = engines.engine_rows() - set(doctor.CHECKS)
        assert not unknown, sorted(unknown)

    def test_a_web_project_is_graded_on_node_and_not_on_godot(self):
        assert engines.doctor_row_enabled("node", "web")
        assert engines.doctor_row_enabled("playwright", "web")
        assert not engines.doctor_row_enabled("node", "godot")
        assert not engines.doctor_row_enabled("playwright", "godot")

    def test_a_godot_project_is_still_graded_on_godot(self, tmp_path):
        project.init(tmp_path, "Engine Test")
        report = doctor.check(str(tmp_path), refresh=True)
        assert not report["godot"].get("engine_disabled")

    def test_a_web_project_is_not_graded_on_godot(self, tmp_path):
        project.init(tmp_path, "Engine Test", engine="web")
        report = doctor.check(str(tmp_path), refresh=True)
        # Marked, not removed: "why isn't Godot listed" must have an answer on
        # the row itself.
        assert report["godot"]["engine_disabled"] is True
        assert "Web project" in report["godot"]["reason"]
        assert report["godot_web_templates"]["engine_disabled"] is True
        assert not report["python"].get("engine_disabled")


class TestToolOwnership:
    """Which tools an engine claims, the table the registration gate reads."""

    @staticmethod
    def _declared() -> list[str]:
        """Every @_tool in the MCP package, read from source.

        The same AST walk tests/cli/test_modules.py uses, and for the same
        reason: importing the server registers only what THIS process's gates
        allow, which is the thing under test.
        """
        import ast
        import pathlib

        names: list[str] = []
        for path in sorted(pathlib.Path("src/bgate_mcp").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in node.decorator_list:
                    got = (dec.id if isinstance(dec, ast.Name) else
                           dec.func.id if isinstance(dec, ast.Call)
                           and isinstance(dec.func, ast.Name) else None)
                    if got == "_tool":
                        names.append(node.name)
        return names

    def test_the_source_of_truth_is_findable(self):
        assert len(self._declared()) > 200

    def test_the_table_names_no_tool_that_does_not_exist(self):
        """Ghost guard, the same one SPINE has.

        A renamed or deleted tool left behind in ENGINE_TOOLS is a lie about
        what an engine owns, and nothing else would catch it.
        """
        ghosts = sorted(engines.owned_tools() - set(self._declared()))
        assert not ghosts, ("ENGINE_TOOLS names tools that no longer exist: "
                            + ", ".join(ghosts))

    def test_no_tool_is_owned_by_two_engines(self):
        seen: dict[str, str] = {}
        for engine, owned in engines.ENGINE_TOOLS.items():
            for name in owned:
                assert name not in seen, (
                    f"{name} is claimed by both {seen[name]} and {engine}")
                seen[name] = engine

    def test_every_godot_prefixed_tool_is_owned_by_godot(self):
        """The prefix is not the rule, but it must not disagree with the rule.

        A tool literally named godot_* that no engine claims would register on a
        web project and open a project.godot that is not there.
        """
        stray = sorted(n for n in self._declared()
                       if n.startswith(("godot_", "scene_"))
                       and engines.tool_owner(n) != "godot")
        assert not stray, stray

    def test_the_neutral_dispatchers_belong_to_no_engine(self):
        # They are the layer that ASKS the engine; owning them would delete the
        # thing that answers "what is this project even built in".
        for name in ("engine_status", "engine_check", "engine_scaffold",
                     "engine_templates", "engine_screenshot"):
            assert engines.tool_owner(name) == ""
            for engine in engines.names():
                assert engines.tool_enabled(name, engine)

    def test_a_tool_no_engine_claims_runs_everywhere(self):
        for name in ("queue_add", "bible_read", "blender_rig", "image_sprites",
                     "brainstorm_new", "playtest_list", "level_plan",
                     "ui_concept", "evidence_assert", "iteration_status"):
            assert engines.tool_owner(name) == "", name
            assert engines.tool_enabled(name, "web"), name

    def test_scene_surgery_is_godot_only(self):
        assert engines.tool_enabled("scene_wire", "godot")
        assert not engines.tool_enabled("scene_wire", "web")
        assert not engines.tool_enabled("level_generate", "unity")
        assert not engines.tool_enabled("tileset_generate", "none")


class TestRegistrationGate:
    """The gate itself, exercised the way the module gate's tests are: by
    asking the predicate, not by re-importing the server per engine."""

    def test_a_godot_project_keeps_every_engine_tool(self):
        for name in sorted(engines.ENGINE_TOOLS["godot"]):
            assert engines.tool_enabled(name, "godot"), name

    def test_a_web_project_loses_exactly_the_other_engines_surfaces(self):
        lost = sorted(n for n in engines.owned_tools()
                      if not engines.tool_enabled(n, "web"))
        assert lost == sorted(engines.ENGINE_TOOLS["godot"]
                              | engines.ENGINE_TOOLS["unity"])
        assert len(lost) > 35
        kept = sorted(n for n in engines.owned_tools()
                      if engines.tool_enabled(n, "unity"))
        assert kept == sorted(engines.ENGINE_TOOLS["unity"])

    def test_an_unreadable_project_registers_everything(self, tmp_path,
                                                        monkeypatch):
        """A missing tool must only come from a stored decision.

        The gate resolves the engine once at import; a directory with no
        game.db must fall open rather than hide the whole engine surface.
        """
        from bgate_mcp import server

        monkeypatch.setattr(server, "_ENGINE", None)
        monkeypatch.setenv("BGATE_ROOT", str(tmp_path / "not-a-project"))
        assert server._engine_registers("scene_wire") is True

    def test_the_gate_reads_the_recorded_engine(self, tmp_path, monkeypatch):
        from bgate_mcp import server

        project.init(tmp_path, "Gate Test", engine="web")
        monkeypatch.setattr(server, "_ENGINE", None)
        monkeypatch.setenv("BGATE_ROOT", str(tmp_path))
        assert server._engine_registers("scene_wire") is False
        assert server._engine_registers("queue_add") is True
        assert server._engine_registers("engine_status") is True

    def test_engine_tools_are_not_parked_for_later_unlock(self):
        """tool_unlock is a seat's move, not an engine's.

        A seat can take on work outside its craft mid-session; there is no
        corresponding move for an engine, because the engine is what the game
        is written in rather than who is working on it.
        """
        from bgate_mcp import server

        assert not (set(server._PARKED) & engines.owned_tools())
