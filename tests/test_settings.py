"""The settings registry — one validator, one precedence rule.

Before this, tonight's switches lived in four mechanisms: a SQL row, workspace
docs, env vars, and module constants. Nothing listed them and nothing said which
the environment was overriding, so a panel could show "agent" while
BGATE_QA_GATE=0 forced "none" — the most expensive lie a settings screen can
tell. These tests pin the two properties that make the registry worth having:
every writer validates in the same place, and `source` never lies about who won.
"""
from __future__ import annotations

import pytest

from bgate_core import settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in set(sum((list(s.env_vars()) for s in settings.SETTINGS), [])):
        monkeypatch.delenv(name, raising=False)


class TestRegistryShape:
    def test_every_setting_declares_a_reachable_store_and_a_legal_default(self):
        """A registry entry whose default its own validator rejects is a field
        that cannot round-trip: the panel renders it, PATCHes it back and gets a
        400 for a value it was shown."""
        assert settings.SETTINGS, "the registry is empty"
        for s in settings.SETTINGS:
            assert s.kind in settings.KINDS, f"{s.key} has kind {s.kind!r}"
            assert s.group in settings.GROUPS, f"{s.key} is in group {s.group!r}"
            assert s.help.strip(), f"{s.key} has no help text"
            settings.coerce(s.key, s.default)      # raises SettingError if not

    def test_enum_settings_declare_their_choices(self):
        for s in settings.SETTINGS:
            if s.kind == settings.ENUM:
                assert s.choices, f"{s.key} is an enum with no choices"
                assert s.default in s.choices

    def test_keys_are_unique(self):
        keys = [s.key for s in settings.SETTINGS]
        assert len(keys) == len(set(keys))


class TestValidation:
    def test_an_unknown_key_is_refused_at_the_writer(self, root):
        with pytest.raises(settings.SettingError):
            settings.coerce("gate.mod", "none")          # the typo
        with pytest.raises(settings.SettingError):
            settings.set(root, "nope.at.all", 1)

    def test_an_enum_rejects_a_value_outside_its_choices(self):
        with pytest.raises(settings.SettingError):
            settings.coerce("gate.mode", "whenever")
        assert settings.coerce("gate.mode", "builders") == "builders"

    def test_bools_take_the_shapes_a_browser_actually_sends(self):
        for truthy in (True, 1, "1", "true", "on", "yes"):
            assert settings.coerce("autopilot.on", truthy) is True
        for falsey in (False, 0, "0", "false", "off", "no"):
            assert settings.coerce("autopilot.on", falsey) is False

    def test_a_number_outside_its_range_is_refused_not_clamped(self):
        """Clamping would silently give a studio a ceiling it did not choose."""
        with pytest.raises(settings.SettingError):
            settings.coerce("qa.max_rounds", 0)
        with pytest.raises(settings.SettingError):
            settings.coerce("qa.max_rounds", "not a number")

    def test_a_list_takes_a_string_or_a_list(self):
        got = settings.coerce("notify.kinds", "item.done, item.failed")
        assert got == ["item.done", "item.failed"]
        assert settings.coerce("notify.kinds", ["item.done"]) == ["item.done"]

    def test_a_list_refuses_a_member_outside_its_vocabulary(self):
        """`chain.filed` was missing from EVENT_KINDS while queue.add_chain was
        emitting it — the kind was unselectable AND refused by this validator."""
        with pytest.raises(settings.SettingError):
            settings.coerce("notify.kinds", ["item.invented"])


class TestPrecedence:
    def test_a_fresh_project_reports_the_default_as_the_default(self, root):
        assert settings.get(root, "gate.mode") == settings.setting("gate.mode").default
        assert settings.source(root, "gate.mode") == settings.SOURCE_DEFAULT

    def test_a_stored_value_wins_over_the_default(self, root):
        settings.set(root, "gate.mode", "builders")
        assert settings.get(root, "gate.mode") == "builders"
        assert settings.source(root, "gate.mode") == settings.SOURCE_STORED

    def test_a_supplying_env_var_wins_over_a_stored_value(self, root, monkeypatch):
        supplying = [s for s in settings.SETTINGS if s.env]
        assert supplying, "no setting declares a supplying env var"
        s = supplying[0]
        stored = next((c for c in (s.choices or ()) if c != s.default), None)
        if stored is not None:
            settings.set(root, s.key, stored)
        monkeypatch.setenv(s.env, str(s.default))
        assert settings.source(root, s.key) == settings.SOURCE_ENV

    def test_a_coercing_env_var_forces_the_value_it_names(self, root, monkeypatch):
        """BGATE_QA_GATE is a BOOLEAN kill switch, so it cannot SUPPLY one of
        three modes — it forces 'none'. Modelling that as env=NAME on an enum
        was a type mismatch, and this is the test that keeps it modelled right."""
        settings.set(root, "gate.mode", "builders")
        monkeypatch.setenv("BGATE_QA_GATE", "0")
        assert settings.get(root, "gate.mode") == "none"
        assert settings.source(root, "gate.mode") == settings.SOURCE_ENV
        monkeypatch.setenv("BGATE_QA_GATE", "1")
        assert settings.get(root, "gate.mode") == "builders"   # stored, unharmed

    def test_effective_names_the_env_var_that_won(self, root, monkeypatch):
        monkeypatch.setenv("BGATE_QA_GATE", "0")
        row = settings.effective(root)["gate.mode"]
        assert row["source"] == settings.SOURCE_ENV
        assert "BGATE_QA_GATE" in row.get("env", "")

    def test_effective_covers_every_declared_key(self, root):
        got = settings.effective(root)
        assert set(got) == set(settings.keys())


class TestStoresItDescribes:
    def test_the_gate_setting_reads_the_doc_gates_py_already_wrote(self, root):
        """Storage was NOT moved — the registry describes where a value already
        lives. If this drifts, the console's inline control and the panel are
        writing to two different places while showing one value."""
        from bgate_core import gates
        gates.set_mode(root, "builders")
        assert settings.get(root, "gate.mode") == "builders"
        settings.set(root, "gate.mode", "none")
        assert gates.mode(root) == "none"

    def test_the_autopilot_setting_and_autodeploy_agree(self, root):
        from bgate_ui import autodeploy
        settings.set(root, "autopilot.on", True)
        assert autodeploy.enabled(root) is True
        autodeploy.set_enabled(root, False)
        assert settings.get(root, "autopilot.on") is False

    def test_budget_keys_read_and_write_the_spend_row(self, root):
        from bgate_core import spend
        budget_keys = [s.key for s in settings.SETTINGS
                       if s.store and s.store[0] == "budget"]
        assert budget_keys, "no setting points at the budget row"
        settings.set(root, "dispatch.max_concurrent", 7)
        assert int(spend.budget(root)["max_concurrent"]) == 7

    def test_client_settings_are_all_declared(self, root):
        got = settings.client(root)
        assert got and all(isinstance(v, (int, float, bool)) for v in got.values())


class TestGuardSwitches:
    """A switch that WIDENS a safety guard, rather than tuning behaviour.

    `dispatch.allow_dirty` is the case that forced this: it used to need an
    environment variable, and describing it in the registry turned it into one
    click. An agent still cannot flip it — the PATCH demands a human actor — but
    a human can now do it by accident, and "the agent's edits are
    indistinguishable from mine in the diff" is a thing to find out before, not
    after.
    """

    def test_allow_dirty_is_marked_as_a_guard(self):
        assert settings.setting("dispatch.allow_dirty").guard is True
        assert settings.setting("gate.mode").guard is False   # tuning, not a guard

    def test_the_description_carries_the_flag_so_the_panel_can_confirm(self, root):
        row = [f for g in settings.describe(root)["groups"] for f in g["fields"]
               if f["key"] == "dispatch.allow_dirty"][0]
        assert row["guard"] is True

    def test_opening_a_guard_is_recorded_on_the_bus(self, root):
        from bgate_core import events
        settings.set(root, "dispatch.allow_dirty", True)
        kinds = [e for e in events.since(root, 0)["events"]
                 if e["kind"] == "settings.guard"]
        assert kinds, "turning a guard off left no trace"
        assert kinds[-1]["payload"]["opened"] is True
        assert kinds[-1]["ref"] == "dispatch.allow_dirty"

    def test_closing_it_again_is_recorded_but_not_as_an_opening(self, root):
        from bgate_core import events
        settings.set(root, "dispatch.allow_dirty", True)
        settings.set(root, "dispatch.allow_dirty", False)
        last = [e for e in events.since(root, 0)["events"]
                if e["kind"] == "settings.guard"][-1]
        assert last["payload"]["opened"] is False

    def test_a_no_op_write_says_nothing(self, root):
        from bgate_core import events
        settings.set(root, "dispatch.allow_dirty", False)   # already the default
        assert not [e for e in events.since(root, 0)["events"]
                    if e["kind"] == "settings.guard"]
