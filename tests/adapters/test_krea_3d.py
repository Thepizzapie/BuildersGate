"""Krea's 3D models, offline.

Same discipline as test_krea.py — the transport is stubbed and what is pinned
is the per-model request schema, because each 3D model takes a different set of
knobs and sending the wrong one is a 422 rather than a default.

Two things here are not like the image path and both have their own class
below. Krea publishes NO per-generation price for any 3D model, so a call
cannot be quoted before it runs; that has to surface as None and a refusal to
spend, never as 0.0, which every budget check in the product would read as
free. And an unsupported parameter must be REFUSED rather than dropped: a
caller who asks for a decimation target on a model that has none would
otherwise pay for a generation and get back the fragmented mesh they were
trying to avoid, with nothing saying why.

Endpoint paths are pinned verbatim. They were read off the API reference one
page at a time after two rounds of guessing wrong about what Krea's 3D API
even was, and a typo in a vendor prefix is a 404 nobody would attribute to
this file.
"""
from __future__ import annotations

import pytest

from bgate_adapters import krea


@pytest.fixture()
def captured(monkeypatch):
    """Swallow the HTTP call and hand back what would have been sent."""
    seen: dict = {}

    def fake(path, key, *, payload=None, method="GET", timeout=60.0):
        seen["path"], seen["payload"], seen["method"] = path, payload, method
        return {"job_id": "job-3d", "status": "queued"}

    monkeypatch.setattr(krea, "_request", fake)
    monkeypatch.setenv("KREA_API_KEY", "test-key")
    return seen


class TestTheCatalogue:
    @pytest.mark.parametrize("model,path", [
        ("trellis-2", "/generate/3d/microsoft/trellis-2"),
        ("trellis", "/generate/3d/microsoft/trellis"),
        ("tripo", "/generate/3d/tripo/tripo"),
        ("hunyuan3d-2.1", "/generate/3d/tencent/hunyuan3d-2.1"),
        ("hunyuan3d-3.1-pro", "/generate/3d/tencent/hunyuan3d-3.1-pro"),
    ])
    def test_the_endpoint_paths_are_the_documented_ones(self, model, path):
        """Vendor prefixes differ per model and a wrong one is a 404 that reads
        like an outage. microsoft/, tripo/ and tencent/ are not guessable."""
        assert krea.MODELS_3D[model]["path"] == path

    def test_models_3d_hides_the_supports_set(self):
        """It is a catalogue for a caller choosing a model, not the validator's
        working state."""
        listed = krea.models_3d()
        assert set(listed) == set(krea.MODELS_3D)
        assert all("supports" not in spec for spec in listed.values())
        assert all(spec.get("note") for spec in listed.values())

    def test_the_default_is_the_one_with_a_decimation_target(self):
        spec = krea.MODELS_3D[krea.DEFAULT_MODEL_3D]
        assert "decimation_target" in spec["supports"]


class TestPriceIsUnknownNotFree:
    def test_a_price_is_measured_or_it_is_none_but_never_zero(self):
        """0.0 would read as free in every budget check in the product. Krea
        publishes no 3D price, so a number here comes from a real invoice or it
        does not exist."""
        for model in krea.MODELS_3D:
            got = krea.price_for_3d(model)
            assert got is None or got > 0, (model, got)

    def test_trellis_2_carries_the_price_that_was_actually_invoiced(self):
        """Measured 2026-07-31: two text-to-3D jobs at default parameters
        billed $0.30 each on Krea's usage page. Pinned so it cannot drift back
        to a guess, and marked with what was measured — nothing is known about
        how resolution or decimation move it."""
        assert krea.price_for_3d("trellis-2") == 0.30
        assert krea.MODELS_3D["trellis-2"]["usd_measured"]

    def test_an_unknown_model_is_named_rather_than_priced(self):
        with pytest.raises(krea.KreaError) as err:
            krea.price_for_3d("trellis-3")
        assert "trellis-3" in str(err.value)

    def test_an_unpriced_model_will_not_spend_without_being_told_to(self, captured):
        """The gate fires before the request, so nothing is charged."""
        got = krea.generate_3d("out.glb", images=["plate.png"], model="tripo")
        assert got["ok"] is False
        assert got["usd"] is None
        assert "confirm_unpriced" in got["error"]
        assert not captured, "the gate let a request through"

    def test_a_measured_model_quotes_itself_and_needs_no_ceremony(self, captured):
        """The gate is for an unknown price, not for spending. trellis-2 has
        been invoiced, so it behaves like every other paid call."""
        krea.submit_3d("a crate", model="trellis-2")
        assert captured, "a priced model should not be gated"

    def test_the_refusal_says_compute_tokens_are_the_wrong_meter(self, captured):
        """Krea's user guide quotes compute tokens; those are the web app's
        subscription currency, not the API's USD balance. Somebody reading the
        wrong page is exactly how this got mis-stated in the first place."""
        got = krea.generate_3d("out.glb", images=["plate.png"], model="tripo")
        assert "web app" in got["price_note"]

    def test_a_failed_generation_still_reports_unknown_not_zero(self, monkeypatch):
        monkeypatch.setenv("KREA_API_KEY", "test-key")

        def boom(*a, **k):
            raise krea.KreaError("Krea job failed: out of capacity")

        monkeypatch.setattr(krea, "submit_3d", boom)
        got = krea.generate_3d("out.glb", images=["p.png"], model="tripo",
                               confirm_unpriced=True)
        assert got["ok"] is False
        assert got["usd"] is None


class TestUnsupportedParametersAreRefusedNotDropped:
    def test_a_knob_the_model_lacks_is_refused_and_names_who_has_it(self, captured):
        """Dropping it would send the request, charge for it, and return the
        default — the fragmented mesh the caller was trying to avoid."""
        with pytest.raises(krea.KreaError) as err:
            krea.submit_3d("a crate", model="tripo", decimation_target=500_000)
        message = str(err.value)
        assert "tripo does not take decimation_target" in message
        assert "trellis-2" in message, "should name a model that does"
        assert not captured

    def test_face_count_belongs_to_hunyuan_not_trellis(self, captured):
        with pytest.raises(krea.KreaError):
            krea.submit_3d("a crate", model="trellis-2", face_count=100_000)
        assert not captured

    def test_pbr_belongs_to_hunyuan_pro_only(self, captured):
        with pytest.raises(krea.KreaError):
            krea.submit_3d("a crate", model="tripo", enable_pbr=True)
        assert not captured

    @pytest.mark.parametrize("field,value", [
        ("resolution", "999"),
        ("texture_size", "8192"),
    ])
    def test_an_out_of_enum_value_is_refused_with_the_allowed_list(
            self, captured, field, value):
        with pytest.raises(krea.KreaError) as err:
            krea.submit_3d("a crate", model="trellis-2", **{field: value})
        assert "not one of" in str(err.value)
        assert not captured

    @pytest.mark.parametrize("model,field,value", [
        ("trellis-2", "decimation_target", 50),
        ("trellis-2", "decimation_target", 9_000_000),
        ("hunyuan3d-3.1-pro", "face_count", 10),
        ("hunyuan3d-3.1-pro", "face_count", 9_000_000),
    ])
    def test_a_range_is_a_range(self, captured, model, field, value):
        with pytest.raises(krea.KreaError) as err:
            krea.submit_3d("a crate", model=model, **{field: value})
        assert "must be" in str(err.value)
        assert not captured

    def test_the_valid_combination_actually_goes_through(self, captured):
        krea.submit_3d("a crate", model="hunyuan3d-3.1-pro", face_count=200_000,
                       enable_pbr=True)
        assert captured["payload"]["face_count"] == 200_000
        assert captured["payload"]["enable_pbr"] is True

    def test_shape_is_checked_before_the_key(self, monkeypatch):
        """A bad parameter must not be answered with "KREA_API_KEY not set".
        That names the wrong problem, and it would make every refusal above
        untestable without a live key."""
        monkeypatch.delenv("KREA_API_KEY", raising=False)
        monkeypatch.setattr(krea, "api_key", lambda root=None: "")
        with pytest.raises(krea.KreaError) as err:
            krea.submit_3d("a crate", model="tripo", enable_pbr=True)
        assert "does not take enable_pbr" in str(err.value)


class TestPayloadShape:
    def test_an_image_sets_image_mode(self, captured, tmp_path):
        plate = tmp_path / "plate.png"
        plate.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        krea.submit_3d("", model="tripo", images=[plate])
        assert captured["payload"]["input_mode"] == "image"
        assert captured["payload"]["image_urls"][0].startswith("data:")

    def test_no_image_means_text_to_3d(self, captured):
        krea.submit_3d("a wooden crate", model="tripo")
        assert captured["payload"]["input_mode"] == "text"
        assert "image_urls" not in captured["payload"]

    def test_a_url_is_passed_through_rather_than_read_from_disk(self, captured):
        krea.submit_3d("", model="tripo", images=["https://example.com/a.png"])
        assert captured["payload"]["image_urls"] == ["https://example.com/a.png"]

    def test_neither_an_image_nor_a_prompt_is_refused(self, captured):
        with pytest.raises(krea.KreaError) as err:
            krea.submit_3d("", model="tripo")
        assert "got neither" in str(err.value)
        assert not captured

    def test_extra_views_become_their_url_fields(self, captured):
        """Multi-view is how a back nobody photographed stops being invented,
        and the caller should not have to remember the _image_url suffix."""
        krea.submit_3d("", model="hunyuan3d-3.1-pro",
                       images=["https://example.com/front.png"],
                       views={"back": "https://example.com/back.png",
                              "left_image_url": "https://example.com/left.png"})
        assert captured["payload"]["back_image_url"].endswith("back.png")
        assert captured["payload"]["left_image_url"].endswith("left.png")

    def test_views_are_refused_on_a_single_view_model(self, captured):
        with pytest.raises(krea.KreaError):
            krea.submit_3d("", model="trellis-2",
                           images=["https://example.com/f.png"],
                           views={"back": "https://example.com/b.png"})
        assert not captured

    def test_texture_can_be_turned_off(self, captured):
        krea.submit_3d("a crate", model="tripo", generate_texture=False)
        assert captured["payload"]["generate_texture"] is False


class TestTheResultIsADraft:
    def _completed(self, monkeypatch, result):
        monkeypatch.setenv("KREA_API_KEY", "test-key")
        monkeypatch.setattr(krea, "submit_3d",
                            lambda *a, **k: {"job_id": "job-3d"})
        monkeypatch.setattr(krea, "poll",
                            lambda *a, **k: {"status": "completed",
                                             "result": result})
        monkeypatch.setattr(krea, "download", lambda url, out, **k: 4096)

    def test_a_finished_generation_says_it_is_not_an_asset_yet(
            self, monkeypatch, tmp_path):
        """Geometry and texture, no rig, no unit convention, no guaranteed
        pose. Anything that reads this as finished ships a statue."""
        self._completed(monkeypatch, {"urls": ["https://example.com/m.glb"]})
        got = krea.generate_3d(str(tmp_path / "m.glb"), images=["p.png"],
                               confirm_unpriced=True)
        assert got["ok"] is True
        assert got["draft"] is True
        assert got["usd"] == 0.30
        assert got["next_steps"]

    def test_an_undocumented_result_field_is_found_rather_than_crashed_on(
            self, monkeypatch, tmp_path):
        """The 3D job schema does not publish its completed body. urls[] is the
        documented shape on the image path; fall back by name instead of
        raising KeyError on a field nobody has seen."""
        self._completed(monkeypatch, {"model_url": "https://example.com/m.glb"})
        got = krea.generate_3d(str(tmp_path / "m.glb"), images=["p.png"],
                               confirm_unpriced=True)
        assert got["ok"] is True
        assert got["url"].endswith(".glb")

    def test_completed_with_no_model_url_quotes_the_body(
            self, monkeypatch, tmp_path):
        self._completed(monkeypatch, {"nothing_useful": 1})
        got = krea.generate_3d(str(tmp_path / "m.glb"), images=["p.png"],
                               confirm_unpriced=True)
        assert got["ok"] is False
        assert "nothing_useful" in got["error"]


class TestTheImagePathIsUntouched:
    def test_download_still_defaults_to_accepting_images(self):
        """`accept` was added for .glb. Every existing image caller passes
        nothing, so the default has to stay what it was."""
        import inspect
        assert inspect.signature(krea.download).parameters["accept"].default == "image/*"

    def test_the_2d_model_table_did_not_gain_3d_entries(self):
        assert not set(krea.MODELS) & set(krea.MODELS_3D)
        assert all("/generate/3d/" not in spec["path"]
                   for spec in krea.MODELS.values())
