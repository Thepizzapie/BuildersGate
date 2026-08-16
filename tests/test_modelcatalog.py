"""The model catalog: configured providers only, adapter tables as the source.

"Only show those for which a key is set" is the contract — a picker built
from this must not offer a model that fails its first call with a
missing-key error, and every id offered must be one the adapters can
actually route (their own tables, not a docs scrape).
"""
from __future__ import annotations

import pytest

from bgate_core import modelcatalog, settings


@pytest.fixture()
def no_keys(monkeypatch):
    for var in ("OPENAI_API_KEY", "KREA_API_KEY", "KIE_API_KEY",
                "DEEPGRAM_API_KEY"):
        monkeypatch.delenv(var, raising=False)


class TestCatalogFollowsTheKeys:
    def test_an_unconfigured_provider_contributes_nothing(self, no_keys,
                                                          monkeypatch):
        monkeypatch.setenv("KREA_API_KEY", "k")
        got = modelcatalog.catalog()
        assert "krea" in got
        assert "openai" not in got and "kie" not in got

    def test_the_ids_are_the_adapters_own(self, no_keys, monkeypatch):
        from bgate_adapters import kie, krea

        monkeypatch.setenv("KREA_API_KEY", "k")
        monkeypatch.setenv("KIE_API_KEY", "x")
        got = modelcatalog.catalog()
        assert set(got["krea"]["image"]) == set(krea.MODELS)
        assert set(got["kie"]["music"]) == set(kie.SUNO_MODELS)

    def test_provider_options_lead_with_auto(self, no_keys, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "o")
        got = modelcatalog.options(None, "image-providers")
        assert got[0] == "auto"
        assert "openai" in got and "krea" not in got

    def test_agent_models_are_the_cli_aliases(self):
        assert modelcatalog.options(None, "agent-models") == \
            list(modelcatalog.AGENT_MODELS)


class TestSettingsPickersReadTheCatalog:
    def test_a_model_field_carries_live_choices(self, root, no_keys,
                                                monkeypatch):
        monkeypatch.setenv("KREA_API_KEY", "k")
        fields = {f["key"]: f for g in settings.describe(root)["groups"]
                  for f in g["fields"]}
        assert "krea-2-large" in fields["art.model"]["choices"]
        assert fields["art.provider"]["choices"][0] == "auto"
        # ENUM legality survives the filter: every declared value reachable.
        assert set(fields["art.provider"]["choices"]) >= {
            "auto", "openai", "krea", "kie", "local"}

    def test_a_stored_value_survives_losing_its_key(self, root, no_keys):
        settings.set(root, "art.model", "krea-2-large")
        fields = {f["key"]: f for g in settings.describe(root)["groups"]
                  for f in g["fields"]}
        assert "krea-2-large" in fields["art.model"]["choices"]

    def test_agent_model_settings_offer_the_aliases(self, root):
        fields = {f["key"]: f for g in settings.describe(root)["groups"]
                  for f in g["fields"]}
        for key in ("dispatch.model", "console.model", "brainstorm.model"):
            assert "fable" in fields[key]["choices"]
