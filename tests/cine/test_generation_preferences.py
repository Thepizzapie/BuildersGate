"""The stored provider/model preference, honoured where choices are made.

There was no stored preference anywhere: every choice was key-presence probing
plus per-tool hardcoded defaults, so a person with a paid, preferred service
watched the harness route work to whichever key probed first. These pin the
new precedence everywhere it was added: explicit ask > stored preference >
routing/defaults — and that a preference is honoured the way an ask is (its
provider's own error, never a silent substitution).
"""
from __future__ import annotations

import pytest

from bgate_core.runtime import providers
from bgate_core.store import settings
from bgate_core.board import tiers


@pytest.fixture()
def no_keys(monkeypatch):
    for var in ("OPENAI_API_KEY", "KREA_API_KEY", "KIE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


class TestProviderPreference:
    def test_auto_keeps_the_probe_order(self, root, no_keys, monkeypatch):
        monkeypatch.setenv("KREA_API_KEY", "k")
        assert providers.provider_for("concept", root=root) == "krea"

    def test_a_named_preference_wins_over_the_probe(self, root, no_keys,
                                                    monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "o")
        settings.set(root, "art.provider", "krea")
        # Honoured even though its key is missing — the caller gets krea's
        # own error naming KREA_API_KEY, never a silent openai substitution.
        assert providers.provider_for("concept", root=root) == "krea"

    def test_an_explicit_ask_beats_the_preference(self, root, no_keys):
        settings.set(root, "art.provider", "krea")
        assert providers.provider_for("concept", asked="openai",
                                      root=root) == "openai"

    def test_no_root_is_the_old_behaviour(self, no_keys, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "o")
        assert providers.provider_for("concept") == "openai"


class TestTierSubstitution:
    def test_a_configured_rung_is_never_overridden(self, root, no_keys,
                                                   monkeypatch):
        monkeypatch.setenv("KREA_API_KEY", "k")
        monkeypatch.setenv("OPENAI_API_KEY", "o")
        settings.set(root, "art.provider", "openai")
        got = tiers.resolve("animation", "standard", root=root)
        # The ladder is measured; the preference decides fallbacks only.
        assert (got["provider"], got["model"]) == ("krea", "krea-2-large")

    def test_a_keyless_rung_substitutes_the_configured_provider(
            self, root, no_keys, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "o")
        got = tiers.resolve("concept", "draft", root=root)
        assert got["provider"] == "openai"
        assert got["model"] == "gpt-image-1"
        assert "substituted" in got["note"]

    def test_no_keys_at_all_keeps_the_ladder_rung(self, root, no_keys):
        got = tiers.resolve("concept", "draft", root=root)
        assert got["provider"] == "krea"       # fails later with krea's error

    def test_no_root_keeps_the_ladder_rung(self, no_keys, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "o")
        got = tiers.resolve("concept", "draft")
        assert got["provider"] == "krea"


class TestCinematicModelPreference:
    def test_the_preference_fills_an_unnamed_plan(self, root):
        from bgate_adapters import kie
        from bgate_core.cine import cinematic

        others = sorted(m for m in kie.VIDEO_MODELS
                        if m != kie.DEFAULT_VIDEO_MODEL)
        if not others:
            pytest.skip("only one video model is registered in this build")
        other = others[0]
        settings.set(root, "cinematic.model", other)
        assert cinematic._resolve_model("", root=root) == other
        # An explicit model still wins.
        assert cinematic._resolve_model(kie.DEFAULT_VIDEO_MODEL,
                                        root=root) == kie.DEFAULT_VIDEO_MODEL

    def test_a_stale_preference_fails_loudly_like_an_ask(self, root):
        from bgate_core.cine import cinematic

        settings.set(root, "cinematic.model", "gone-model-9")
        with pytest.raises(cinematic.CinematicError):
            cinematic._resolve_model("", root=root)

    def test_blank_preference_is_the_adapter_default(self, root):
        from bgate_adapters import kie
        from bgate_core.cine import cinematic

        assert cinematic._resolve_model("", root=root) == \
            kie.DEFAULT_VIDEO_MODEL
