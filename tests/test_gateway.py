"""The provider gateway - a dead key must read as a routing event.

The observed failure this pins: an agent's first call hit one provider,
read "no credit", and hand-rolled the asset while a second provider sat
keyed and funded. The gateway answers "who IS live" in one place, and the
MCP server's _fail appends that answer to every billing-shaped failure so
the redirect lands in the same tool result as the refusal.
"""
from __future__ import annotations

import pytest

from bgate_core import gateway


@pytest.fixture(autouse=True)
def _fresh_cache():
    gateway._cache.clear()
    yield
    gateway._cache.clear()


def _rows(monkeypatch, **rows):
    """Stub the per-provider probe: rows maps provider id -> row overrides."""
    def probe(provider, root):
        base = {"id": provider, "keyed": False, "reason": "no key",
                "balance": None, "balance_unit": ""}
        base.update(rows.get(provider, {}))
        return base
    monkeypatch.setattr(gateway, "_probe", probe)


class TestPick:
    def test_doctrine_order_holds(self, monkeypatch):
        """kie mints, RD animates - a funded openai must not outrank a funded
        kie for images, and nothing but RD may serve `animate`."""
        _rows(monkeypatch,
              kie={"keyed": True, "balance": 400.0, "balance_unit": "credits"},
              openai={"keyed": True, "reason": ""})
        got = gateway.pick(None, "image")
        assert got["provider"] == "kie"
        assert got["alternatives"] == ["openai"]
        assert gateway.pick(None, "animate")["provider"] is None

    def test_unknown_balance_is_routable_but_drained_is_not(self, monkeypatch):
        """None means UNKNOWN, never zero - krea never says; a provider whose
        balance reads 0 is skipped and named as drained."""
        _rows(monkeypatch,
              kie={"keyed": True, "balance": 0.0, "balance_unit": "credits"},
              krea={"keyed": True, "reason": ""})
        got = gateway.pick(None, "image")
        assert got["provider"] == "krea"
        assert "balance unknown" in got["why"]

    def test_nothing_routable_names_every_reason(self, monkeypatch):
        _rows(monkeypatch,
              kie={"keyed": True, "balance": 0.0})
        got = gateway.pick(None, "image")
        assert got["provider"] is None
        assert "kie: drained" in got["why"]
        assert "krea: no key" in got["why"]


class TestBillingNote:
    def test_a_live_provider_forbids_hand_rolling(self, monkeypatch):
        _rows(monkeypatch,
              kie={"keyed": True, "balance": 412.0, "balance_unit": "credits"})
        note = gateway.billing_note(None)
        assert "ROUTING EVENT" in note
        assert "412 credits" in note
        assert "Do NOT hand-roll" in note

    def test_nothing_funded_routes_to_the_human(self, monkeypatch):
        _rows(monkeypatch)
        note = gateway.billing_note(None)
        assert "Nothing is funded" in note
        assert "file the top-up as the blocker" in note

    def test_probes_are_cached_not_hammered(self, monkeypatch):
        """A burst of billing failures is exactly when this runs - it must
        not turn into a burst of network probes."""
        calls = []
        def probe(provider, root):
            calls.append(provider)
            return {"id": provider, "keyed": False, "reason": "no key",
                    "balance": None, "balance_unit": ""}
        monkeypatch.setattr(gateway, "_probe", probe)
        gateway.billing_note(None)
        gateway.billing_note(None)
        assert len(calls) == len(gateway.PROVIDERS)
        gateway.status(None, fresh=True)
        assert len(calls) == len(gateway.PROVIDERS) * 2


class TestBillingDetection:
    def test_the_adapters_own_sentences_are_recognised(self):
        # The exact phrasings kie.py / krea.py raise today.
        for text in ("kie has no credit left — top the account up at kie.ai",
                     "Krea has no API credit — the API balance is billed",
                     "HTTP 402 Payment Required",
                     "You exceeded your current quota"):
            assert gateway.is_billing_error(text), text

    def test_a_shape_error_is_not_a_money_problem(self):
        """A 422 that reads as billing trains agents to provider-hop around
        real bugs."""
        for text in ("Krea refused the request shape for this model",
                     "unknown seat 'artt'", "file not found", ""):
            assert not gateway.is_billing_error(text), text


class TestServerWiring:
    def test_fail_appends_the_route_to_billing_errors(self, monkeypatch):
        from bgate_mcp import server
        _rows(monkeypatch,
              kie={"keyed": True, "balance": 55.0, "balance_unit": "credits"})
        out = server._fail(RuntimeError(
            "kie has no credit left — top the account up at kie.ai"))
        assert out["ok"] is False
        assert "ROUTING EVENT" in out.get("route", "")

    def test_fail_stays_silent_on_ordinary_errors(self):
        from bgate_mcp import server
        out = server._fail(ValueError("wrong direction set"))
        assert "route" not in out

    def test_fail_never_raises_over_a_broken_gateway(self, monkeypatch):
        from bgate_mcp import server
        monkeypatch.setattr(gateway, "billing_note",
                            lambda root: 1 / 0)
        out = server._fail(RuntimeError("no credit"))
        assert out["ok"] is False   # the original error survives
