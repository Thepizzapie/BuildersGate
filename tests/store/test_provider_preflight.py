"""ITEM 13: dispatch-time provider preflight for the art/audio/cinematic seats.

MEASURED: Retro Diffusion ran dry ($0.11) on EXIT 67's night one; two Codex
art runs stalled on it silently instead of being refused before they spawned.
"""
from __future__ import annotations

from bgate_core.board import seats
from bgate_core.runtime import gateway, preflight


class TestPreflightCheck:
    def setup_method(self):
        preflight.clear_cache()

    def test_a_seat_with_no_paid_capability_is_never_checked(self, root):
        assert preflight.check(str(root), "tech") is None
        assert preflight.check(str(root), "director") is None

    def test_routable_provider_passes(self, root, monkeypatch):
        monkeypatch.setattr(
            gateway, "pick",
            lambda root, cap: {"provider": "kie", "alternatives": [], "why": "keyed"})
        assert preflight.check(str(root), "art") is None

    def test_a_keyed_but_drained_provider_refuses_by_name(self, root, monkeypatch):
        # kie is the ONLY provider for `image`; keyed and at balance 0.
        monkeypatch.setattr(
            gateway, "status",
            lambda root: [{"id": "kie", "keyed": True, "balance": 0},
                          {"id": "krea", "keyed": False, "balance": None},
                          {"id": "openai", "keyed": False, "balance": None}])
        monkeypatch.setattr(
            gateway, "pick",
            lambda root, cap: {"provider": None, "alternatives": [],
                                "why": f"{cap}: drained (balance 0)"})
        result = preflight.check(str(root), "art")
        assert result is not None
        assert result["code"] == "provider_drained"
        assert "kie" in result["error"]

    def test_nothing_keyed_at_all_is_not_a_dispatch_blocker(self, root, monkeypatch):
        # No key configured yet - normal, and not what this item targets.
        monkeypatch.setattr(gateway, "status", lambda root: [])
        monkeypatch.setattr(
            gateway, "pick",
            lambda root, cap: {"provider": None, "alternatives": [],
                                "why": f"{cap}: no key"})
        assert preflight.check(str(root), "art") is None

    def test_result_is_cached_for_60_seconds(self, root, monkeypatch):
        calls = {"n": 0}

        def counting_pick(root, cap):
            calls["n"] += 1
            return {"provider": None, "alternatives": [], "why": "drained"}

        monkeypatch.setattr(gateway, "pick", counting_pick)
        preflight.check(str(root), "art")
        first = calls["n"]
        preflight.check(str(root), "art")
        assert calls["n"] == first  # second call served from cache


class TestProvidersBrief:
    def setup_method(self):
        preflight.clear_cache()

    def test_empty_for_a_non_paid_seat(self, root):
        assert preflight.providers_brief(str(root), "tech") == []

    def test_bounded_rows_for_a_paid_seat(self, root, monkeypatch):
        monkeypatch.setattr(
            gateway, "status",
            lambda root: [{"id": "kie", "keyed": True, "balance": 12.5}])
        rows = preflight.providers_brief(str(root), "art")
        assert rows
        assert all({"name", "capability", "keyed", "balance", "usable"} <= set(r) for r in rows)


class TestSeatBriefCarriesProviders:
    def test_providers_field_present_on_a_paid_seat(self, root):
        out = seats.brief(root, "art")
        assert "providers" in out
        assert isinstance(out["providers"], list)

    def test_providers_empty_for_a_non_paid_seat(self, root):
        out = seats.brief(root, "tech")
        assert out["providers"] == []
