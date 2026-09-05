"""One enforcement setting composed from four ladders, and the precedence
that keeps an explicit ladder setting ahead of it."""
from __future__ import annotations

import pytest

from bgate_cli import hook
from bgate_core.board import aegis, enforcement, gates, seats
from bgate_core.store import settings

LADDER_VARS = ("BGATE_ENFORCEMENT", "BGATE_DIRECTOR_MODE", "BGATE_LANES",
               "BGATE_AEGIS", "BGATE_QA_GATE", "BGATE_ROOT")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in LADDER_VARS:
        monkeypatch.delenv(var, raising=False)


def _ladders(root) -> dict[str, str]:
    return {"director": hook.director_mode(str(root)),
            "lanes": seats.lane_mode(str(root)),
            "aegis": aegis.mode(str(root)),
            "gate": gates.mode(root)}


class TestProfileSelection:
    def test_standard_is_the_default_and_is_todays_defaults(self, root):
        assert enforcement.profile(str(root)) == "standard"
        assert _ladders(root) == {"director": "collide", "lanes": "warn",
                                  "aegis": "block", "gate": "agent"}

    def test_the_env_selects_a_profile_without_a_root(self, monkeypatch):
        monkeypatch.setenv("BGATE_ENFORCEMENT", "STRICT")
        assert enforcement.profile(None) == "strict"
        assert hook.director_mode() == "block"
        assert seats.lane_mode() == "block"
        assert aegis.mode() == "block"

    def test_the_stored_setting_selects_a_profile(self, root):
        settings.set(root, "enforcement.profile", "relaxed", actor="human")
        assert _ladders(root) == {"director": "off", "lanes": "collide",
                                  "aegis": "warn", "gate": "none"}

    def test_a_pinned_root_reaches_rootless_callers(self, root, monkeypatch):
        """The MCP server asks lane_mode()/aegis.mode() with no root; a
        dispatched worker carries BGATE_ROOT, which is the same project."""
        settings.set(root, "enforcement.profile", "strict", actor="human")
        monkeypatch.setenv("BGATE_ROOT", str(root))
        assert seats.lane_mode() == "block"
        assert hook.director_mode() == "block"

    def test_an_unknown_profile_falls_back_to_standard(self, root, monkeypatch):
        monkeypatch.setenv("BGATE_ENFORCEMENT", "nonsense")
        assert enforcement.profile(str(root)) == "standard"


class TestOverridePrecedence:
    def test_an_explicit_ladder_env_beats_the_profile(self, root, monkeypatch):
        monkeypatch.setenv("BGATE_ENFORCEMENT", "relaxed")
        monkeypatch.setenv("BGATE_LANES", "block")
        monkeypatch.setenv("BGATE_AEGIS", "block")
        monkeypatch.setenv("BGATE_DIRECTOR_MODE", "warn")
        got = _ladders(root)
        assert got["lanes"] == "block"
        assert got["aegis"] == "block"
        assert got["director"] == "warn"
        assert got["gate"] == "none", "the untouched ladder still follows the profile"

    def test_a_stored_gate_mode_beats_the_profile(self, root, monkeypatch):
        monkeypatch.setenv("BGATE_ENFORCEMENT", "strict")
        gates.set_mode(root, "none", by="human")
        assert gates.mode(root) == "none"

    def test_the_legacy_kill_switch_beats_the_profile(self, root, monkeypatch):
        monkeypatch.setenv("BGATE_ENFORCEMENT", "strict")
        monkeypatch.setenv("BGATE_QA_GATE", "0")
        assert gates.mode(root) == "none"

    def test_a_typo_in_a_ladder_var_falls_through_to_the_profile(
            self, root, monkeypatch):
        monkeypatch.setenv("BGATE_ENFORCEMENT", "strict")
        monkeypatch.setenv("BGATE_LANES", "nonsense")
        assert seats.lane_mode(str(root)) == "block"


class TestDescribe:
    def test_one_sentence_per_ladder_and_the_profile(self, root):
        text = enforcement.describe(str(root))
        lines = text.splitlines()
        assert lines[0].startswith("Enforcement profile 'standard'")
        assert [ln.split(" = ")[0] for ln in lines[1:]] == [
            "director", "lanes", "aegis", "gate"]
        assert all(ln.endswith(".") for ln in lines)
