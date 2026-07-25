"""The Krea adapter, offline.

Everything here runs without touching the network — the transport is stubbed and
what is under test is the part that actually bit during integration: each model
has its OWN request schema, and sending the wrong one is a 422, not a default.
The first live call failed exactly that way (aspect_ratio sent to flux), so
these tests pin the payload per model rather than trusting one shared shape.

Prices come from each model's API reference, NOT krea.ai/features/api — that
page lists flux at $0.04 while the reference says $0.007. Since Phase 2 puts a
live cost estimate on every node, the wrong table mis-quotes every generation.
"""
from __future__ import annotations

import base64
import json

import pytest

from bgate_adapters import krea


@pytest.fixture()
def captured(monkeypatch):
    """Swallow the HTTP call and hand back what would have been sent."""
    seen: dict = {}

    def fake(path, key, *, payload=None, method="GET", timeout=60.0):
        seen["path"], seen["payload"], seen["method"] = path, payload, method
        return {"job_id": "job-1", "status": "queued"}

    monkeypatch.setattr(krea, "_request", fake)
    monkeypatch.setenv("KREA_API_KEY", "test-key")
    return seen


class TestPerModelSchema:
    def test_krea_2_sends_aspect_and_resolution(self, captured):
        krea.submit("x", model="krea-2-large", size="1024x1024")
        assert captured["payload"]["aspect_ratio"] == "1:1"
        assert captured["payload"]["resolution"] == "1K"
        assert "width" not in captured["payload"]

    def test_flux_sends_pixels_not_aspect(self, captured):
        """The exact 422 the first live call hit: 'Unrecognized keys:
        aspect_ratio, resolution'."""
        krea.submit("x", model="flux-1-dev", size="1024x576")
        assert captured["payload"]["width"] == 1024
        assert captured["payload"]["height"] == 576
        assert "aspect_ratio" not in captured["payload"]
        assert "resolution" not in captured["payload"]

    def test_imagen_sends_pixels(self, captured):
        krea.submit("x", model="imagen-4", size="2048x2048")
        assert "width" in captured["payload"] and "aspect_ratio" not in captured["payload"]

    def test_each_model_posts_to_its_own_path(self, captured):
        for model, path in [("krea-2-large", "/generate/image/krea/krea-2/large"),
                            ("flux-1-dev", "/generate/image/bfl/flux-1-dev"),
                            ("imagen-4", "/generate/image/google/imagen-4")]:
            krea.submit("x", model=model)
            assert captured["path"] == path

    def test_an_unknown_model_is_refused_before_any_call(self, captured):
        with pytest.raises(krea.KreaError, match="unknown model"):
            krea.submit("x", model="not-a-model")
        assert not captured


class TestSizing:
    @pytest.mark.parametrize("size,aspect", [
        ("1024x1024", "1:1"), ("1536x1024", "3:2"), ("1024x1536", "2:3"),
        ("1920x1080", "16:9"), ("garbage", "1:1"),
    ])
    def test_aspect_mapping(self, size, aspect):
        assert krea.aspect_for(size) == aspect

    def test_pixels_clamp_to_the_accepted_range(self):
        assert krea.pixels_for("100x100") == (512, 512)
        assert krea.pixels_for("9000x9000") == (2368, 2368)
        assert krea.pixels_for("nonsense") == (1024, 1024)


class TestPricing:
    def test_prices_match_each_models_reference(self):
        assert krea.MODELS["flux-1-dev"]["usd"] == 0.007
        assert krea.MODELS["imagen-4"]["usd"] == 0.042
        assert krea.MODELS["krea-2-large"]["usd"] == 0.06

    def test_the_price_follows_the_request_not_the_model(self):
        """Attaching anchors costs more, and the art seat anchors nearly
        everything — so the common case is the dearer one."""
        assert krea.price_for("krea-2-large") == 0.06
        assert krea.price_for("krea-2-large", style_refs=2) == 0.065
        assert krea.price_for("krea-2-large", moodboard=True) == 0.07

    def test_a_model_with_no_surcharge_stays_flat(self):
        assert krea.price_for("flux-1-dev", style_refs=3) == 0.007

    def test_an_unknown_model_prices_as_the_default_rather_than_raising(self):
        assert krea.price_for("who") == krea.price_for(krea.DEFAULT_MODEL)


class TestStyleReferences:
    def test_krea_2_clamps_strength_to_0_1(self, captured):
        krea.submit("x", model="krea-2-large",
                    style_refs=[{"url": "u", "strength": 5}])
        assert captured["payload"]["image_style_references"][0]["strength"] == 1.0

    def test_flux_allows_negative_strength(self, captured):
        """-2..2 on flux: negative pushes AWAY from the reference."""
        krea.submit("x", model="flux-1-dev",
                    style_refs=[{"url": "u", "strength": -1.5}])
        assert captured["payload"]["image_style_references"][0]["strength"] == -1.5

    def test_capped_at_ten(self, captured):
        krea.submit("x", model="krea-2-large",
                    style_refs=[{"url": f"u{i}"} for i in range(25)])
        assert len(captured["payload"]["image_style_references"]) == 10

    def test_a_model_without_reference_support_refuses_loudly(self, captured):
        with pytest.raises(krea.KreaError, match="does not take style references"):
            krea.submit("x", model="imagen-4", style_refs=[{"url": "u"}])

    def test_refs_without_a_url_are_dropped(self, captured):
        krea.submit("x", model="krea-2-large",
                    style_refs=[{"strength": 0.5}, {"url": "good"}])
        assert len(captured["payload"]["image_style_references"]) == 1


class TestDataUri:
    def test_a_local_png_becomes_an_inline_uri(self, tmp_path):
        """Krea has no upload endpoint — an anchor travels inline or not at all."""
        f = tmp_path / "anchor.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        uri = krea.data_uri(f)
        assert uri.startswith("data:image/png;base64,")
        assert base64.b64decode(uri.split(",", 1)[1]) == f.read_bytes()

    def test_a_missing_file_says_so(self, tmp_path):
        with pytest.raises(krea.KreaError, match="not found"):
            krea.data_uri(tmp_path / "nope.png")

    def test_an_unsupported_type_is_refused(self, tmp_path):
        f = tmp_path / "anchor.tga"
        f.write_bytes(b"x")
        with pytest.raises(krea.KreaError, match="png/jpg/webp"):
            krea.data_uri(f)


class TestErrors:
    def _http(self, monkeypatch, code, body=b"{}"):
        import urllib.error

        def boom(*a, **k):
            raise urllib.error.HTTPError("u", code, "err", {}, None)

        monkeypatch.setattr(krea.urllib.request, "urlopen", boom)
        monkeypatch.setenv("KREA_API_KEY", "k")

    def test_402_explains_the_separate_api_balance(self, monkeypatch):
        """The surprising one: a Krea SUBSCRIPTION does not pay for API calls."""
        self._http(monkeypatch, 402)
        with pytest.raises(krea.KreaError, match="billed separately"):
            krea.submit("x")

    def test_401_points_at_the_key(self, monkeypatch):
        self._http(monkeypatch, 401)
        with pytest.raises(krea.KreaError, match="KREA_API_KEY"):
            krea.submit("x")

    def test_422_says_schemas_are_per_model(self, monkeypatch):
        self._http(monkeypatch, 422)
        with pytest.raises(krea.KreaError, match="own parameter schema"):
            krea.submit("x")

    def test_no_key_is_an_actionable_reason_not_a_crash(self, monkeypatch):
        monkeypatch.delenv("KREA_API_KEY", raising=False)
        got = krea.available()
        assert got["available"] is False
        assert "krea.ai/settings/api-tokens" in got["reason"]

    def test_generate_returns_a_result_shaped_like_imagegen(self, monkeypatch):
        """The art pipeline must not care which provider made the file."""
        monkeypatch.setenv("KREA_API_KEY", "k")
        monkeypatch.setattr(krea, "submit", lambda *a, **k: (_ for _ in ()).throw(
            krea.KreaError("nope")))
        got = krea.generate("x", "out.png")
        assert got["ok"] is False
        assert set(got) >= {"ok", "error", "provider", "model", "seconds", "estimated_usd"}
        assert got["provider"] == "krea"


class TestJobLifecycle:
    def test_an_intermediate_preview_is_not_treated_as_finished(self, monkeypatch):
        """`intermediate-complete` means a preview exists while sampling
        continues — taking it ships a half-cooked image."""
        assert "intermediate-complete" in krea.RUNNING
        assert "intermediate-complete" != krea.DONE

    def test_a_failed_job_raises_with_the_reason(self, monkeypatch):
        monkeypatch.setenv("KREA_API_KEY", "k")
        monkeypatch.setattr(krea, "_request", lambda *a, **k: {
            "status": "failed", "error": {"message": "content policy"}})
        with pytest.raises(krea.KreaError, match="content policy"):
            krea.poll("job-1", timeout=5)

    def test_an_unknown_status_stops_rather_than_spinning(self, monkeypatch):
        monkeypatch.setenv("KREA_API_KEY", "k")
        monkeypatch.setattr(krea, "_request", lambda *a, **k: {"status": "wat"})
        with pytest.raises(krea.KreaError, match="unknown status"):
            krea.poll("job-1", timeout=5)
