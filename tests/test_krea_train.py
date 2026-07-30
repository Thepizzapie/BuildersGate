"""Trained styles (LoRA), offline.

WHY THIS IS WORTH THE MODULE. The art seat's own rule: "A style reference and an
identity reference cannot share a weight. At equal strength the style ref
transfers the SUBJECT and the whole cast comes back as one person." One slot, two
jobs — that is the ceiling on reference-conditioned work. Training the style into
a model empties the slot.

Nothing here touches the network. What is pinned is the part that costs money to
get wrong: a training run is 5-15 minutes at an unpublished price, so the dataset
is judged BEFORE anything is uploaded, and the two shapes that would silently
fail — a data URI where a hosted URL belongs, an image under the resolution floor
— are refused on this side of the wire.
"""
from __future__ import annotations

import pytest

from bgate_adapters import krea

PIL = pytest.importorskip("PIL.Image", reason="resolution checks need Pillow")


def _png(path, w=1024, h=1024):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), (12, 12, 16)).save(path)
    return str(path)


@pytest.fixture()
def captured(monkeypatch):
    seen: dict = {}

    def fake(path, key, *, payload=None, method="GET", timeout=60.0):
        seen["path"], seen["payload"], seen["method"] = path, payload, method
        return {"job_id": "train-1", "status": "queued"}

    monkeypatch.setattr(krea, "_request", fake)
    monkeypatch.setenv("KREA_API_KEY", "test-key")
    return seen


class TestDatasetGate:
    def test_a_small_image_is_rejected_with_its_actual_size(self, tmp_path):
        """Measured on a real board: 6 of 27 pinned refs cleared the 1024 floor.
        The others must fail HERE, naming the size, not after an upload loop."""
        good = [_png(tmp_path / f"g{i}.png") for i in range(5)]
        small = _png(tmp_path / "small.png", 297, 472)
        got = krea.check_training_set(good + [small])
        assert got["ok"] is True
        assert got["usable"] == good
        why = [r["why"] for r in got["rejected"] if r["path"] == small][0]
        assert "297x472" in why and "1024" in why

    def test_too_few_usable_images_is_not_ok(self, tmp_path):
        got = krea.check_training_set([_png(tmp_path / f"g{i}.png") for i in range(4)])
        assert got["ok"] is False and "at least 5" in got["reason"]

    def test_a_thin_but_legal_set_trains_with_a_warning(self, tmp_path):
        got = krea.check_training_set([_png(tmp_path / f"g{i}.png") for i in range(6)])
        assert got["ok"] is True
        assert any("thin" in w for w in got["warnings"])

    def test_duplicates_and_junk_are_named_individually(self, tmp_path):
        one = _png(tmp_path / "a.png")
        got = krea.check_training_set([one, one, str(tmp_path / "nope.png"),
                                       _png(tmp_path / "b.jpg")])
        whys = {r["path"]: r["why"] for r in got["rejected"]}
        assert "the same image twice" in whys[one]
        assert "not a file" in whys[str(tmp_path / "nope.png")]

    def test_a_wrong_format_is_refused(self, tmp_path):
        bad = tmp_path / "sheet.tres"
        bad.write_text("not an image", encoding="utf-8")
        got = krea.check_training_set([str(bad)])
        assert "png/jpg/webp" in got["rejected"][0]["why"]

    def test_no_pillow_reports_that_sizes_were_not_checked(self, tmp_path,
                                                           monkeypatch):
        """Silence here would be the expensive kind: the set looks clean and Krea
        refuses it after the upload."""
        import builtins
        paths = [_png(tmp_path / f"g{i}.png") for i in range(5)]   # before the block
        real = builtins.__import__

        def blocked(name, *a, **kw):
            if name == "PIL":
                raise ImportError("no PIL")
            return real(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", blocked)
        got = krea.check_training_set(paths)
        assert any("Pillow is not installed" in w for w in got["warnings"])
        # And it does NOT quietly pass them as verified.
        assert got["usable"] == paths


class TestTrainRequest:
    def test_the_payload_matches_the_documented_shape(self, captured):
        krea.train_style("Dark Neon Office", [f"https://k/{i}.png" for i in range(8)],
                         kind="Style", trigger_word="dno_style",
                         max_train_steps=500, learning_rate=0.0003)
        assert captured["path"] == "/styles/train" and captured["method"] == "POST"
        p = captured["payload"]
        assert p["name"] == "Dark Neon Office" and len(p["urls"]) == 8
        assert p["model"] == "flux_dev" and p["type"] == "Style"
        assert p["trigger_word"] == "dno_style"
        assert p["max_train_steps"] == 500 and p["learning_rate"] == 0.0003

    def test_data_uris_are_refused_with_the_fix(self, captured):
        """THE TRAP THIS MODULE INVITES. Every other call in the adapter sends
        anchors as data URIs; /styles/train takes hosted URLs only."""
        with pytest.raises(krea.KreaError) as exc:
            krea.train_style("x", ["data:image/png;base64,AAA"] * 6)
        assert "upload" in str(exc.value) and "data URI" in str(exc.value)
        assert not captured                     # nothing was sent

    def test_optional_fields_are_omitted_rather_than_nulled(self, captured):
        krea.train_style("x", [f"https://k/{i}.png" for i in range(5)])
        assert "trigger_word" not in captured["payload"]
        assert "max_train_steps" not in captured["payload"]

    @pytest.mark.parametrize("kwargs, hint", [
        ({"max_train_steps": 5000}, "1 and 2000"),
        ({"learning_rate": 5.0}, "0.0001"),
        ({"kind": "Vibe"}, "type must be one of"),
        ({"model": "sdxl"}, "unknown training base"),
    ])
    def test_out_of_range_parameters_never_reach_the_api(self, captured, kwargs, hint):
        with pytest.raises(krea.KreaError) as exc:
            krea.train_style("x", [f"https://k/{i}.png" for i in range(5)], **kwargs)
        assert hint in str(exc.value)
        assert not captured

    def test_a_nameless_style_is_refused(self, captured):
        with pytest.raises(krea.KreaError):
            krea.train_style("   ", [f"https://k/{i}.png" for i in range(5)])


class TestTrainEndToEnd:
    def test_every_image_is_judged_before_any_is_uploaded(self, tmp_path,
                                                          monkeypatch):
        """The ordering that saves the money: one bad anchor must not cost five
        uploads and a half-formed dataset on Krea's side."""
        uploads: list = []
        monkeypatch.setattr(krea, "upload",
                            lambda p, **kw: uploads.append(p) or {"image_url": "u", "path": p})
        got = krea.train("x", [_png(tmp_path / "a.png"),
                               _png(tmp_path / "small.png", 100, 100)])
        assert got["ok"] is False
        assert uploads == []

    def test_a_finished_run_returns_the_style_id_and_its_sources(self, tmp_path,
                                                                 monkeypatch):
        paths = [_png(tmp_path / f"g{i}.png") for i in range(10)]
        monkeypatch.setattr(krea, "upload",
                            lambda p, **kw: {"image_url": f"https://k/{p}", "path": p})
        monkeypatch.setattr(krea, "train_style",
                            lambda *a, **kw: {"job_id": "train-9", "status": "queued"})
        monkeypatch.setattr(krea, "poll",
                            lambda job_id, **kw: {"status": "completed",
                                                  "result": {"style_id": "w29t6pvy0"}})
        got = krea.train("Dark Neon Office", paths, trigger_word="dno")
        assert got["ok"] and got["style_id"] == "w29t6pvy0"
        assert got["images"] == 10 and got["sources"] == paths
        # NOT a number: Krea publishes no training price, and an under-quote is
        # worse than a missing one because a spend ceiling would pass it.
        assert got["estimated_usd"] is None

    def test_wait_false_hands_back_the_job_without_blocking(self, tmp_path,
                                                            monkeypatch):
        monkeypatch.setattr(krea, "upload", lambda p, **kw: {"image_url": "u", "path": p})
        monkeypatch.setattr(krea, "train_style",
                            lambda *a, **kw: {"job_id": "train-3"})
        monkeypatch.setattr(krea, "poll", lambda *a, **kw: pytest.fail("must not poll"))
        got = krea.train("x", [_png(tmp_path / f"g{i}.png") for i in range(5)],
                         wait=False)
        assert got["ok"] and got["pending"] and got["job_id"] == "train-3"

    def test_completed_with_no_style_id_is_a_failure_not_a_success(self, tmp_path,
                                                                  monkeypatch):
        monkeypatch.setattr(krea, "upload", lambda p, **kw: {"image_url": "u", "path": p})
        monkeypatch.setattr(krea, "train_style", lambda *a, **kw: {"job_id": "t"})
        monkeypatch.setattr(krea, "poll", lambda *a, **kw: {"status": "completed",
                                                            "result": {}})
        got = krea.train("x", [_png(tmp_path / f"g{i}.png") for i in range(5)])
        assert got["ok"] is False and "style_id" in got["error"]


class TestUpload:
    def test_the_body_is_multipart_with_the_documented_field_names(self, tmp_path,
                                                                   monkeypatch):
        sent: dict = {}

        class Resp:
            def read(self): return b'{"id":"a1","image_url":"https://k/a1.png"}'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_open(req, timeout=None):
            sent["url"] = req.full_url
            sent["headers"] = dict(req.headers)
            sent["body"] = req.data
            return Resp()

        monkeypatch.setattr(krea.urllib.request, "urlopen", fake_open)
        monkeypatch.setenv("KREA_API_KEY", "test-key")
        got = krea.upload(_png(tmp_path / "anchor.png"), description="the anchor")
        assert got["image_url"] == "https://k/a1.png"
        assert sent["url"].endswith("/assets")
        assert "multipart/form-data; boundary=" in sent["headers"]["Content-type"]
        assert b'name="file"; filename="anchor.png"' in sent["body"]
        assert b'name="description"' in sent["body"] and b"the anchor" in sent["body"]
        assert b"image/png" in sent["body"]

    def test_a_response_without_a_url_is_an_error_not_an_empty_string(
            self, tmp_path, monkeypatch):
        class Resp:
            def read(self): return b'{"id":"a1"}'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(krea.urllib.request, "urlopen", lambda *a, **kw: Resp())
        monkeypatch.setenv("KREA_API_KEY", "test-key")
        with pytest.raises(krea.KreaError) as exc:
            krea.upload(_png(tmp_path / "a.png"))
        assert "no image_url" in str(exc.value)


class TestGenerateWithATrainedStyle:
    def test_the_styles_array_rides_alongside_the_references(self, captured,
                                                              tmp_path):
        """Both axes at once is the POINT, not a conflict: the LoRA carries the
        style so the reference slot is free to carry identity."""
        krea.submit("a hero", model="krea-2-medium",
                    styles=[krea.style("w29t6pvy0", 0.9)],
                    style_refs=[{"url": "data:image/png;base64,AAA"}])
        p = captured["payload"]
        assert p["styles"] == [{"id": "w29t6pvy0", "strength": 0.9}]
        assert len(p["image_style_references"]) == 1

    def test_strength_is_clamped_and_defaults_to_the_recommended_band(self, captured):
        krea.submit("x", model="krea-2-medium", styles=[{"id": "s"}, {"id": "t", "strength": 9}])
        assert captured["payload"]["styles"] == [
            {"id": "s", "strength": 0.85}, {"id": "t", "strength": 1.0}]

    def test_a_model_that_cannot_use_one_says_which_can(self, captured):
        with pytest.raises(krea.KreaError) as exc:
            krea.submit("x", model="imagen-4", styles=[{"id": "s"}])
        assert "krea-2-medium" in str(exc.value)
        assert not captured
