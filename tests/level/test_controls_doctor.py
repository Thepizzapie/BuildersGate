"""Controls read from the project, and the dependency report over HTTP.

Both exist to kill a specific lie the audit found. The play panel hardcoded
"J/K punch · U/I kick · S block · L duck" — controls the shipped template has
never implemented — and the record button's preflight opened the microphone and
spawned a whisper probe every 15 seconds just to decide whether to grey itself
out. One now reads the project's own input map; the other answers from
`bgate doctor`, which touches no hardware.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core.level import controls
from bgate_core.store import scaffold


class TestKeyNames:
    @pytest.mark.parametrize("code,name", [
        (65, "A"), (68, "D"), (32, "Space"),
        (4194319, "←"), (4194321, "→"), (4194320, "↑"), (4194322, "↓"),
        (4194332, "F1"), (4194305, "Esc"),
    ])
    def test_known_keys_render_as_something_pressable(self, code, name):
        assert controls.key_name(code) == name

    def test_an_unknown_code_is_honest_rather_than_wrong(self):
        """Inventing a plausible name is how the hardcoded hint happened."""
        assert controls.key_name(9_999_999) == "key 9999999"


class TestParseInputMap:
    def test_the_2d_template_reports_only_what_it_binds(self):
        """The regression guard: if anyone re-adds a fake control, this fails."""
        text = (scaffold.TEMPLATES_DIR / "2d" / "project.godot").read_text(
            encoding="utf-8")
        got = controls.parse_input_map(text)
        assert [a["action"] for a in got] == ["move_left", "move_right", "jump"]
        assert got[0]["keys"] == ["A", "←"]
        assert got[2]["keys"] == ["Space"]

    def test_the_3d_template_reports_its_own_map(self):
        text = (scaffold.TEMPLATES_DIR / "3d" / "project.godot").read_text(
            encoding="utf-8")
        actions = [a["action"] for a in controls.parse_input_map(text)]
        assert "move_forward" in actions and "move_back" in actions

    def test_godots_own_ui_actions_are_not_shown_to_a_player(self):
        text = ('[input]\n\nui_accept={\n"deadzone": 0.5,\n"events": []\n}\n'
                'fire={\n"deadzone": 0.5,\n"events": [Object(InputEventKey,'
                '"physical_keycode":74,"keycode":0)\n]\n}\n')
        assert [a["action"] for a in controls.parse_input_map(text)] == ["fire"]
        assert controls.parse_input_map(text, include_builtin=True)[0][
            "action"] == "ui_accept"

    def test_declaration_order_is_preserved(self):
        """The author's order reads better than alphabetical."""
        text = ('[input]\n\nzeta={\n"events": [Object(InputEventKey,'
                '"physical_keycode":90)\n]\n}\nalpha={\n"events": '
                '[Object(InputEventKey,"physical_keycode":65)\n]\n}\n')
        assert [a["action"] for a in controls.parse_input_map(text)] == [
            "zeta", "alpha"]

    def test_only_the_input_section_is_read(self):
        text = ('[rendering]\nrenderer={\n"events": [Object(InputEventKey,'
                '"physical_keycode":88)\n]\n}\n\n[input]\n\njump={\n"events": '
                '[Object(InputEventKey,"physical_keycode":32)\n]\n}\n')
        assert [a["action"] for a in controls.parse_input_map(text)] == ["jump"]

    def test_a_gamepad_binding_is_reported_separately(self):
        text = ('[input]\n\nfire={\n"events": [Object(InputEventJoypadButton,'
                '"button_index":2)\n]\n}\n')
        assert controls.parse_input_map(text)[0]["buttons"] == ["pad 2"]

    def test_duplicate_events_collapse(self):
        text = ('[input]\n\njump={\n"events": [Object(InputEventKey,'
                '"physical_keycode":32), Object(InputEventKey,'
                '"physical_keycode":32)\n]\n}\n')
        assert controls.parse_input_map(text)[0]["keys"] == ["Space"]


class TestForProject:
    def test_a_scaffolded_project_reports_its_controls(self, root):
        scaffold.new_project(root, "Ctrl", kind="2d", force=True)
        assert [a["action"] for a in controls.for_project(root)] == [
            "move_left", "move_right", "jump"]

    def test_no_project_godot_means_no_claims(self, root):
        """Empty is a real answer the UI renders as one."""
        assert controls.for_project(root) == []

    def test_an_unreadable_project_file_does_not_raise(self, root, monkeypatch):
        monkeypatch.setattr(controls.Path, "read_text",
                            lambda *a, **k: (_ for _ in ()).throw(OSError()))
        assert controls.for_project(root) == []


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    from bgate_ui.app import app
    return TestClient(app)


class TestDoctorEndpoint:
    def test_it_reports_every_dependency(self, client):
        body = client.get("/api/doctor").json()
        assert body["ok"] is True
        assert set(body["data"]) >= {"python", "ffmpeg", "godot", "blender",
                                     "whisper", "art_key"}

    def test_each_row_says_what_to_do_about_it(self, client):
        for name, row in client.get("/api/doctor").json()["data"].items():
            assert set(row) >= {"available", "path", "version", "reason"}
            assert row["available"] or row["reason"], f"{name} is silent"

    def test_the_summary_names_what_is_missing(self, client):
        summary = client.get("/api/doctor").json()["summary"]
        assert summary["ok"] == (not summary["missing"])

    def test_it_never_opens_the_microphone(self, client, monkeypatch):
        """The whole point: the record button's cheap path must stay cheap."""
        from bgate_adapters import recorder

        def _boom(*a, **k):
            raise AssertionError("the dependency report probed the mic")

        monkeypatch.setattr(recorder, "probe_mic", _boom, raising=False)
        assert client.get("/api/doctor?refresh=1").status_code == 200

    def test_it_answers_even_with_no_project(self, client, monkeypatch, tmp_path):
        """A machine with no project still has a toolchain question."""
        monkeypatch.setenv("BGATE_ROOT", str(tmp_path / "nowhere"))
        assert client.get("/api/doctor").status_code == 200
