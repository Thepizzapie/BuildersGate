"""The local-runtime registry: what it declares, how it stages, where it writes.

THE THING MOST WORTH PINNING is not any single row — it is the pair of choices
the module makes that a later refactor would quietly undo:

  * a local runtime is NOT a Provider. Its status returns the VALUE, where a
    provider's returns a four-character fingerprint precisely because it must
    not. Somebody unifying the two "for symmetry" turns a path panel into a
    panel you cannot read, or a key panel into one you can.
  * the dashboard does not start anything. There is no spawn here and there
    must not be one; the test asserts the module imports no process API at all,
    which is the only way to catch a start button arriving by accident.

Everything else is the four stages, which are the whole user-facing contract:
not set up / set up-not running / running-but-wrong / ready.
"""
from __future__ import annotations

import ast
import inspect
import os

import pytest

from bgate_core import envfile, localruntimes as L, providers


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every variable the registry reads, unset. Without this the suite passes
    or fails depending on whether the developer has a ComfyUI configured."""
    for one in L.runtimes():
        for field in one.fields:
            monkeypatch.delenv(field.env, raising=False)
    envfile.reset_cache()


class TestRegistry:
    def test_the_2d_and_3d_runtimes_are_both_there(self):
        ids = L.ids()
        assert "comfy-image" in ids
        # Generated from imageto3d.BACKENDS' own local rows — a backend added
        # there must appear here with no edit to localruntimes.
        from bgate_adapters import imageto3d
        for backend in imageto3d.LOCAL:
            assert backend in ids

    def test_every_runtime_names_a_capability_from_the_shared_vocabulary(self):
        """The one thing genuinely shared with providers.py. If a runtime coins
        its own word for '2D images', the dashboard's grouping silently splits
        into two sections that mean the same thing."""
        for one in L.runtimes():
            assert one.powers
            for power in one.powers:
                assert power in providers.CAPABILITIES

    def test_every_field_explains_itself(self):
        """The ask this surface exists for was legibility. A field whose help is
        empty, or which only restates its own label, is the papercut back."""
        for one in L.runtimes():
            for field in one.fields:
                assert len(field.help) > 40, f"{one.id}.{field.env}"
                assert field.help.lower() != field.label.lower()

    def test_unknown_runtime_names_the_legal_ids(self):
        with pytest.raises(L.LocalConfigError) as got:
            L.by_id("nope")
        assert "comfy-image" in str(got.value)


class TestStages:
    def test_missing_a_required_field_is_unconfigured(self, root):
        row = L.status_for(root, "comfy-image", probe=False)
        assert row["stage"] == "unconfigured"
        assert "workflow" in row["reason"].lower()
        assert row["available"] is False

    def test_a_configured_runtime_with_nothing_listening_is_unreachable(
            self, root, tmp_path, monkeypatch):
        wf = tmp_path / "wf.json"
        wf.write_text('{"1": {"class_type": "CLIPTextEncode", "inputs": '
                      '{"text": "__BGATE_PROMPT__"}}}', encoding="utf-8")
        monkeypatch.setenv("BGATE_COMFY_T2I_WORKFLOW", str(wf))
        # A port nothing is on. Not 8188: a developer running ComfyUI would
        # otherwise see this test go green for the wrong reason.
        monkeypatch.setenv("BGATE_COMFY_URL", "http://127.0.0.1:9")
        row = L.status_for(root, "comfy-image", probe=True)
        assert row["stage"] == "unreachable"
        # VERBATIM, not collapsed to a lamp: the address is the actionable half.
        assert "127.0.0.1:9" in row["reason"]

    def test_an_unprobed_read_says_it_did_not_check(self, root, tmp_path,
                                                    monkeypatch):
        """`configured` exists so an unprobed read cannot claim 'not running'."""
        wf = tmp_path / "wf.json"
        wf.write_text('{"1": {"class_type": "X", "inputs": '
                      '{"text": "__BGATE_PROMPT__"}}}', encoding="utf-8")
        monkeypatch.setenv("BGATE_COMFY_T2I_WORKFLOW", str(wf))
        row = L.status_for(root, "comfy-image", probe=False)
        assert row["stage"] == "configured"
        assert "not asked" in row.get("checked", "")

    def test_a_workflow_pointing_at_nothing_is_reported_as_such(
            self, root, monkeypatch):
        monkeypatch.setenv("BGATE_COMFY_T2I_WORKFLOW", r"C:\nope\gone.json")
        row = L.status_for(root, "comfy-image", probe=False)
        assert row["stage"] == "unhealthy"
        assert "gone.json" in row["reason"]

    def test_an_unwired_backend_is_unavailable_not_unhealthy(self, root):
        """A backend this build cannot talk to is not the user's setup being
        broken, and offering them fields to fix it would waste their time."""
        rows = {r["id"]: r for r in L.status(root, probe=False)}
        assert rows["gradio-local"]["stage"] == "unavailable"

    def test_every_stage_has_a_label_and_a_tone(self):
        for row in L.status(None, probe=False):
            assert row["stage"] in L.STAGES
            assert row["stage_label"] == L.STAGES[row["stage"]]
            assert row["tone"] in ("good", "warn", "off")


class TestConfigWriting:
    def test_a_value_round_trips_through_the_env_file_and_the_process(
            self, root, tmp_path):
        wf = tmp_path / "graph.json"
        wf.write_text("{}", encoding="utf-8")
        L.set_field(root, "comfy-image", "BGATE_COMFY_T2I_WORKFLOW", str(wf))
        # BOTH halves. The file alone is not enough: load_project_env refuses to
        # overwrite a name already in os.environ, so without the in-process
        # assignment the user saves a path and nothing starts working.
        assert envfile.file_vars(root)["BGATE_COMFY_T2I_WORKFLOW"] == str(wf)
        assert os.environ["BGATE_COMFY_T2I_WORKFLOW"] == str(wf)

    def test_a_path_with_spaces_survives(self, root, tmp_path):
        """On the supported platform paths have spaces in them constantly. The
        key writer refuses whitespace on purpose; this one must not."""
        folder = tmp_path / "My Workflows"
        folder.mkdir()
        wf = folder / "t2i.json"
        wf.write_text("{}", encoding="utf-8")
        L.set_field(root, "comfy-image", "BGATE_COMFY_T2I_WORKFLOW", str(wf))
        assert envfile.file_vars(root)["BGATE_COMFY_T2I_WORKFLOW"] == str(wf)
        raw = (root / ".env").read_text(encoding="utf-8")
        assert f'"{wf}"' in raw       # quoted in the file, unquoted on the way out

    def test_clearing_removes_it_from_the_file_and_the_process(self, root):
        L.set_field(root, "comfy-image", "BGATE_COMFY_URL", "http://127.0.0.1:9")
        L.clear_field(root, "comfy-image", "BGATE_COMFY_URL")
        assert "BGATE_COMFY_URL" not in envfile.file_vars(root)
        assert "BGATE_COMFY_URL" not in os.environ

    def test_saving_an_empty_value_clears_rather_than_storing_a_blank(self, root):
        L.set_field(root, "comfy-image", "BGATE_COMFY_URL", "http://127.0.0.1:9")
        L.set_field(root, "comfy-image", "BGATE_COMFY_URL", "   ")
        assert "BGATE_COMFY_URL" not in envfile.file_vars(root)

    def test_an_address_without_a_scheme_is_refused_with_the_fix(self, root):
        with pytest.raises(L.LocalConfigError) as got:
            L.set_field(root, "comfy-image", "BGATE_COMFY_URL", "127.0.0.1:8188")
        assert "http://" in str(got.value)

    def test_a_declared_model_outside_the_licence_table_is_refused(self, root):
        with pytest.raises(L.LocalConfigError):
            L.set_field(root, "comfy-image", "BGATE_LOCAL_IMAGE_MODEL", "wat")

    def test_a_field_that_is_not_this_runtime_s_is_refused(self, root):
        with pytest.raises(L.LocalConfigError) as got:
            L.set_field(root, "comfy-image", "BGATE_TRELLIS_CPP_URL", "http://x")
        assert "BGATE_COMFY_URL" in str(got.value)

    def test_other_lines_in_the_env_survive_a_write(self, root):
        (root / ".env").write_text(
            "# a note the user left\nOPENAI_API_KEY=sk-keep-me\n", encoding="utf-8")
        L.set_field(root, "comfy-image", "BGATE_COMFY_URL", "http://127.0.0.1:9")
        raw = (root / ".env").read_text(encoding="utf-8")
        assert "# a note the user left" in raw
        assert "OPENAI_API_KEY=sk-keep-me" in raw


class TestValueVisibility:
    def test_the_value_is_returned_in_full(self, root, monkeypatch):
        """THE DELIBERATE INVERSION OF providers.status. A key is fingerprinted
        because it must never be shown; a path is shown because a path you
        cannot read is a path you cannot check for the typo. If somebody
        unifies these two modules, this is the test that should stop them."""
        monkeypatch.setenv("BGATE_COMFY_URL", "http://192.168.1.40:8188")
        row = L.status_for(root, "comfy-image", probe=False)
        field = [f for f in row["fields"] if f["env"] == "BGATE_COMFY_URL"][0]
        assert field["value"] == "http://192.168.1.40:8188"
        assert "last4" not in field

    def test_a_shell_variable_shadowing_the_file_is_named(self, root,
                                                          monkeypatch):
        """load_project_env lets the shell win, so a panel reading os.environ
        alone would show the saved value as in force while an empty export is
        what the adapter actually sees."""
        (root / ".env").write_text("BGATE_COMFY_URL=http://from-file:8188\n",
                                   encoding="utf-8")
        monkeypatch.setenv("BGATE_COMFY_URL", "")
        envfile.reset_cache()
        row = L.status_for(root, "comfy-image", probe=False)
        field = [f for f in row["fields"] if f["env"] == "BGATE_COMFY_URL"][0]
        assert field["source"] == "shadowed"


class TestNoProcessManagement:
    def test_the_module_cannot_start_anything(self):
        """No spawn, and the assertion is structural rather than behavioural.

        A start button is the kind of feature that arrives by accident, one
        helper at a time, and by the time it can orphan a process holding 8 GB
        of VRAM nobody remembers it was decided against. Importing subprocess
        here is the first step of that, so the import is what is banned.
        """
        tree = ast.parse(inspect.getsource(L))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "subprocess" not in imported
        assert "multiprocessing" not in imported
        source = inspect.getsource(L)
        for banned in ("Popen(", "os.spawn", "os.system", "taskkill"):
            assert banned not in source

    def test_the_start_instructions_are_prose_for_a_human(self):
        """Every runtime tells the user how to start it. This is the whole
        replacement for a start button and an empty one is a dead end."""
        for one in L.runtimes():
            if not one.implemented:
                continue
            assert one.start, one.id
            assert all(len(step) > 20 for step in one.start), one.id


class TestDoctor:
    def test_the_row_is_optional_and_says_what_is_missing(self, root):
        row = L.doctor_row(root)
        assert row["name"] == "local_runtimes"
        assert row["optional"] is True
        assert row["available"] is False
        # The count, not a bare "unavailable": "three not set up" and "three set
        # up but not running" are different problems.
        assert "not set up" in row["detail"] or "not running" in row["detail"]
        assert "hosted" in row["detail"]

    def test_doctor_registers_the_check(self):
        from bgate_core import doctor
        assert "local_runtimes" in doctor.CHECKS
        assert "local_runtimes" in doctor._PROBES
        assert doctor.MIN_REQUIRED["local_runtimes"] == ""
