"""Optional feature modules: chosen at setup, enforced at every surface.

The contract under test: a disabled module's MCP tools are not registered,
its doctor rows stop being requirements, the choice is stored/delivered like
any setting, and the wizard/CLI can only disable REAL modules — a typo must
not silently disable the nearest feature.
"""
from __future__ import annotations

from bgate_core import modules, settings


class TestTheRegistry:
    def test_tool_gating_is_by_prefix(self):
        off = {"music", "cinematic"}
        assert not modules.tool_enabled("kie_music_generate", off)
        assert not modules.tool_enabled("cinematic_plan", off)
        assert not modules.tool_enabled("storyboard_auto", off)
        assert modules.tool_enabled("queue_add", off)
        assert modules.tool_enabled("image_generate", off)

    def test_everything_on_gates_nothing(self):
        assert modules.tool_enabled("kie_music_generate", set())

    def test_a_shared_doctor_row_survives_one_module_leaving(self):
        # ffmpeg is named by cinematic AND playtest: off one, still required.
        assert modules.doctor_row_enabled("ffmpeg", {"playtest"})
        assert not modules.doctor_row_enabled("ffmpeg",
                                              {"playtest", "cinematic"})
        # a core row is nobody's option
        assert modules.doctor_row_enabled("godot", set(modules.names()))

    def test_pip_hint_names_the_extras(self):
        assert "voice" in modules.pip_hint(("voice",))
        assert modules.pip_hint(("floor",)) == ""

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
        assert not server._module_registers("kie_music_generate")
        assert not server._module_registers("blender_rig")
        assert server._module_registers("queue_add")
        assert server._module_registers("godot_run")

    def test_an_unpinned_session_registers_everything(self, monkeypatch):
        from bgate_mcp import server

        monkeypatch.setattr(server, "_MODULES_OFF", None)
        monkeypatch.delenv("BGATE_ROOT", raising=False)
        # No project resolvable in the test cwd -> empty disabled set.
        assert server._module_registers("kie_music_generate")
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
