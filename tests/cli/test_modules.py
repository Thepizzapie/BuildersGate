"""Optional feature modules: chosen at setup, enforced at every surface.

The contract under test: a disabled module's MCP tools are not registered,
its doctor rows stop being requirements, the choice is stored/delivered like
any setting, and the wizard/CLI can only disable REAL modules — a typo must
not silently disable the nearest feature.
"""
from __future__ import annotations

from bgate_core.store import modules, settings


class TestTheRegistry:
    def test_tool_gating_is_by_prefix(self):
        off = {"music", "cinematic"}
        assert not modules.tool_enabled("music_generate", off)
        assert not modules.tool_enabled("cinematic_plan", off)
        assert not modules.tool_enabled("storyboard_auto", off)
        assert modules.tool_enabled("queue_add", off)
        assert modules.tool_enabled("image_generate", off)

    def test_everything_on_gates_nothing(self):
        assert modules.tool_enabled("music_generate", set())

    def test_a_shared_doctor_row_survives_one_module_leaving(self):
        # ffmpeg is named by cinematic AND playtest: off one, still required.
        assert modules.doctor_row_enabled("ffmpeg", {"playtest"})
        assert not modules.doctor_row_enabled("ffmpeg",
                                              {"playtest", "cinematic"})
        # a core row is nobody's option
        assert modules.doctor_row_enabled("godot", set(modules.names()))

    def test_pip_hint_names_the_extras(self):
        assert "voice" in modules.pip_hint(("voice",))
        assert "floor" in modules.pip_hint(("floor",))   # the assets pack
        assert modules.pip_hint(("brainstorm",)) == ""

    def test_unknown_stored_names_are_dropped(self, root):
        settings.set(root, "modules.disabled", ["music"])
        assert modules.disabled(root) == {"music"}
        # the setting's own choices refuse unknown names outright
        import pytest

        with pytest.raises(Exception):
            settings.set(root, "modules.disabled", ["muzak"])

    def test_the_choice_reaches_the_page_bootstrap(self, root):
        settings.set(root, "modules.disabled", ["floor"])
        assert settings.client(root)["modules.disabled"] == ["floor"]

    def test_the_catalog_carries_the_wizard_row(self):
        rows = {r["name"]: r for r in modules.catalog()}
        assert set(rows) == set(modules.names())
        assert rows["voice"]["pip"].startswith("pip install")
        assert rows["floor"]["blurb"]


class TestTheServerRegistry:
    def test_registration_respects_the_disabled_set(self, monkeypatch):
        from bgate_mcp import server

        monkeypatch.setattr(server, "_MODULES_OFF", {"music", "three_d"})
        assert not server._module_registers("music_generate")
        assert not server._module_registers("blender_rig")
        assert server._module_registers("queue_add")
        assert server._module_registers("godot_run")

    def test_an_unpinned_session_registers_everything(self, monkeypatch):
        from bgate_mcp import server

        monkeypatch.setattr(server, "_MODULES_OFF", None)
        monkeypatch.delenv("BGATE_ROOT", raising=False)
        # No project resolvable in the test cwd -> empty disabled set.
        assert server._module_registers("music_generate")
        monkeypatch.setattr(server, "_MODULES_OFF", None)  # drop the cache


class TestTheCliFlag:
    def test_without_stores_the_choice_and_warns_on_typos(self, root):
        from bgate_cli import main

        note = main._store_modules_off(root, "floor, muzak")
        assert "switched off: floor" in note
        assert "muzak" in note
        assert modules.disabled(root) == {"floor"}

    def test_no_flag_is_a_no_op(self, root):
        from bgate_cli import main

        assert main._store_modules_off(root, "") == ""
        assert modules.disabled(root) == set()


class TestSeatScopedTools:
    def test_a_seat_registers_its_craft_and_the_spine(self):
        assert modules.seat_tool_enabled("image_generate", "art")
        assert modules.seat_tool_enabled("blender_rig", "art")
        assert modules.seat_tool_enabled("queue_add", "art")       # spine
        assert modules.seat_tool_enabled("seat_brief", "gameplay")  # spine
        assert not modules.seat_tool_enabled("music_generate", "art")
        assert not modules.seat_tool_enabled("blender_rig", "gameplay")
        assert not modules.seat_tool_enabled("cinematic_plan", "audio")

    def test_narrative_holds_the_storyboard_half(self):
        assert modules.seat_tool_enabled("storyboard_write_script", "narrative")
        assert not modules.seat_tool_enabled("image_generate", "narrative")

    def test_unknown_and_absent_seats_are_unscoped(self):
        assert modules.seat_tool_enabled("music_generate", "")
        assert modules.seat_tool_enabled("music_generate", "director")
        assert modules.seat_tool_enabled("music_generate", "mystery-seat")

    def test_the_server_gate_composes_seat_and_modules(self, monkeypatch):
        from bgate_mcp import server

        monkeypatch.setattr(server, "_MODULES_OFF", set())
        monkeypatch.setenv("BGATE_SEAT", "gameplay")
        monkeypatch.delenv("BGATE_SEAT_TOOLS", raising=False)
        assert not server._module_registers("blender_rig")
        assert server._module_registers("godot_run")
        # the escape hatch: a session that genuinely needs everything
        monkeypatch.setenv("BGATE_SEAT_TOOLS", "all")
        assert server._module_registers("blender_rig")


class TestTheTwoDDefault:
    def test_a_2d_init_switches_the_3d_pipeline_off(self, tmp_path):
        from bgate_cli import main

        assert main.init_project("Flatland", kind="2d",
                                 dest=str(tmp_path / "flatland")) == 0
        assert "three_d" in modules.disabled(tmp_path / "flatland")

    def test_a_3d_init_keeps_it(self, tmp_path):
        from bgate_cli import main

        assert main.init_project("Deepland", kind="3d",
                                 dest=str(tmp_path / "deepland")) == 0
        assert "three_d" not in modules.disabled(tmp_path / "deepland")


class TestTheFloorAssetsPack:
    def test_the_floor_module_names_its_extra(self):
        assert '[floor]' in modules.pip_hint(("floor",))

    def test_the_wheel_excludes_what_the_pack_carries(self):
        import tomllib
        from pathlib import Path

        cfg = tomllib.loads((Path(__file__).resolve().parents[2]
                             / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = cfg["tool"]["setuptools"]["exclude-package-data"]["bgate_ui"]
        assert any("img/floor" in p for p in patterns)
        assert any("audio/floor" in p for p in patterns)
        assert "builders-gate-floor-assets" in str(
            cfg["project"]["optional-dependencies"]["floor"])


class TestMachineDefaults:
    """The installer's component page writes ~/.bgate/modules.json; projects
    without a stored choice inherit it, and creation seeds from it."""

    def _write_machine(self, names):
        import json
        import os
        from pathlib import Path

        home = Path(os.environ["BGATE_HOME"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "modules.json").write_text(
            json.dumps({"disabled": names}), encoding="utf-8")

    def test_an_unstored_project_inherits_the_machine_file(self, root):
        self._write_machine(["music", "not-a-module"])
        assert modules.disabled(root) == {"music"}   # unknowns dropped

    def test_a_stored_choice_beats_the_machine_file(self, root):
        self._write_machine(["music"])
        settings.set(root, "modules.disabled", ["floor"])
        assert modules.disabled(root) == {"floor"}

    def test_creation_seeds_the_union(self, tmp_path):
        from bgate_cli import main

        self._write_machine(["music"])
        assert main.init_project("Seeded", kind="2d",
                                 dest=str(tmp_path / "seeded")) == 0
        assert modules.disabled(tmp_path / "seeded") == {"music", "three_d"}

    def test_no_file_means_everything_on(self):
        assert modules.machine_defaults() == set()


class TestEveryToolIsClassified:
    """The accident P0 exists to forbid.

    CRAFTS is a prefix table, so a tool whose name matched no prefix joined the
    shared spine SILENTLY — nobody decided it was universal, nobody noticed it
    was not. `sidescroll_generate` (27 params) and `godot_deliver_asset` (713
    docstring words) rode in the audio seat's context that way. The registry is
    read from source rather than from a live FastMCP instance on purpose:
    registration is itself gated by seat and module env, so an imported
    registry answers "what does THIS process serve", and the question here is
    "what does this repo declare".
    """

    @staticmethod
    def _declared() -> list[str]:
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "src" / "bgate_mcp"
        names = []
        for path in sorted(root.glob("*.py")):
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
        # A guard on the guard: an AST walk that silently found nothing would
        # make every assertion below vacuously true.
        assert len(self._declared()) > 200

    def test_no_tool_falls_through_both_tables(self):
        missing = modules.unclassified(self._declared())
        assert not missing, (
            "these MCP tools match no craft prefix and are not on the SPINE "
            "allowlist, so they joined the shared spine by accident and now "
            "cost every seat context on every turn — add each to "
            "modules.CRAFTS or modules.SPINE: " + ", ".join(missing))

    def test_the_spine_allowlist_has_no_ghosts(self):
        # The other direction: a renamed or deleted tool left behind in SPINE
        # is a lie about what is universal, and nothing else would catch it.
        declared = set(self._declared())
        ghosts = sorted(n for n in modules.SPINE if n not in declared)
        assert not ghosts, (
            "SPINE names tools that no longer exist: " + ", ".join(ghosts))

    def test_spine_and_crafts_do_not_both_claim_a_tool(self):
        both = sorted(n for n in modules.SPINE if modules.crafts_owning(n))
        assert not both, (
            "these are on SPINE but a craft prefix also claims them, so the "
            "allowlist disagrees with the scoping that actually runs: "
            + ", ".join(both))

    def test_the_tools_p0_reclassified_reach_the_right_seats(self):
        # The specific miscategorisations, asserted by seat rather than by
        # table, because the table is the mechanism and this is the point.
        assert modules.seat_tool_enabled("sidescroll_generate", "gameplay")
        assert not modules.seat_tool_enabled("sidescroll_generate", "audio")
        assert modules.seat_tool_enabled("sprite_sheet_check", "art")
        assert modules.seat_tool_enabled("sprite_sheet_check", "qa")  # verdict
        assert not modules.seat_tool_enabled("sprite_sheet_check", "audio")
        assert modules.seat_tool_enabled("godot_deliver_asset", "art")
        assert modules.seat_tool_enabled("godot_deliver_asset", "tech")
        assert not modules.seat_tool_enabled("godot_deliver_asset", "narrative")
        assert modules.seat_tool_enabled("causal_chains", "qa")
        assert not modules.seat_tool_enabled("causal_chains", "art")

    def test_kie_status_stays_universal(self):
        # Filing it under `image` would hide the music path's own key check
        # from the audio seat. Named here so a later tidy-up cannot quietly
        # "finish the job".
        for seat in ("audio", "art", "cinematic", "qa"):
            assert modules.seat_tool_enabled("kie_status", seat)


class TestTheDirectorOnlySplit:
    """The spine is not one thing.

    `SPINE` means "not a craft"; it never meant "everyone needs this". Nine of
    its tools are the top-level session's job by construction and rode in every
    dispatched seat's context anyway, because no other category existed.
    """

    def test_a_dispatched_seat_cannot_see_the_directors_own_tools(self):
        for seat in ("art", "gameplay", "tech", "audio", "narrative", "qa"):
            assert not modules.seat_tool_enabled("seat_configure", seat)
            assert not modules.seat_tool_enabled("project_init", seat)
            assert not modules.seat_tool_enabled("agent_steer", seat)
            assert not modules.seat_tool_enabled("decision_settle", seat)

    def test_the_human_session_keeps_all_of_them(self):
        for name in modules.DIRECTOR_ONLY:
            assert modules.seat_tool_enabled(name, "")

    def test_an_invented_seat_is_still_a_dispatched_one(self):
        # Craft scoping fails OPEN for an unknown seat because its surface is
        # unknowable. This gate does not: the question is "was this process
        # dispatched", and a seat env var is that answer whatever it says.
        assert not modules.seat_tool_enabled("seat_configure", "mystery-seat")
        assert modules.seat_tool_enabled("image_generate", "mystery-seat")

    def test_the_workers_lifelines_are_not_in_it(self):
        # Considered and rejected — hiding any of these breaks a workflow
        # silently, which costs more than the docstring words save.
        for name in ("ask_human", "queue_claim_next", "queue_add",
                     "queue_complete", "board_digest", "plan_status",
                     "bgate_doctor", "seat_brief", "handoff_note"):
            assert name not in modules.DIRECTOR_ONLY
            assert modules.seat_tool_enabled(name, "art")

    def test_director_only_tools_are_still_classified(self):
        # They are spine members, not a third table that dodges P0's rule.
        for name in modules.DIRECTOR_ONLY:
            assert name in modules.SPINE
        assert not modules.unclassified(modules.DIRECTOR_ONLY)

    def test_no_seat_brief_tells_a_worker_to_call_a_tool_it_cannot_see(self):
        """The silent breakage this whole split has to avoid.

        A brief that says "call project_init" while the tool is hidden is
        worse than either the tool being there or the sentence being absent.
        """
        import re
        from pathlib import Path

        from bgate_core.board import seats

        source = Path(seats.__file__).read_text(encoding="utf-8")
        # The director's own protocol may name them: it is only ever shown to
        # a seatless session, which has them all.
        director_text = seats.DIRECTOR_PROTOCOL
        for name in modules.DIRECTOR_ONLY:
            for match in re.finditer(re.escape(name), source):
                line_start = source.rfind("\n", 0, match.start()) + 1
                line = source[line_start:source.find("\n", match.start())]
                if line.lstrip().startswith("#"):
                    continue          # a comment is not something an agent reads
                assert name in director_text, (
                    f"{name} is named in seat-facing prose but a dispatched "
                    f"seat cannot call it: {line.strip()}")
