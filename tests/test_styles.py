"""Trained styles: the record, the toggle, and the door they reach generation by.

The tax this lifts is in the art seat's own rules — "a style reference and an
identity reference cannot share a weight" — so what matters is not that a LoRA
can be trained but that ONE place decides whether a given image uses it, and
that the answer degrades to today's behaviour whenever anything is missing. A
LoRA's failure mode is that its drift is baked into the model instead of visible
in the payload, so a project that silently started using one it did not ask for
is the worst outcome available here.

No network: `krea.train` is stubbed everywhere below.
"""
from __future__ import annotations

import pytest

from bgate_core import chroma, events, refs, settings, styles


def _png(path, w=1024, h=1024):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), (12, 12, 16)).save(path)
    return str(path)


@pytest.fixture()
def anchors(root, tmp_path):
    """Six pinned references that clear the 1024 floor, and one that does not."""
    pytest.importorskip("PIL.Image")
    for i in range(6):
        refs.pin(root, f"concept-{i}", _png(tmp_path / f"c{i}.png"))
    refs.pin(root, "tiny-sprite", _png(tmp_path / "tiny.png", 64, 32))
    return root


@pytest.fixture()
def trained(root):
    return styles.record(root, {
        "style_id": "w29t6pvy0", "name": "Dark Neon Office",
        "images": 12, "sources": ["concept-0", "concept-1"]})


class TestTheRecord:
    def test_a_trained_style_is_remembered_with_what_it_was_trained_on(self, root,
                                                                       trained):
        got = styles.active(root)
        assert got["style_id"] == "w29t6pvy0" and got["name"] == "Dark Neon Office"
        # WITHOUT THE SOURCES a LoRA is unfalsifiable six months later: you
        # cannot tell whether the thing making your art saw the anchors you are
        # looking at.
        assert got["sources"] == ["concept-0", "concept-1"]
        assert got["trained_at"] and got["strength"] == 0.85

    def test_a_style_with_no_id_is_refused(self, root):
        with pytest.raises(ValueError):
            styles.record(root, {"name": "nameless"})

    def test_training_a_second_style_replaces_the_active_one(self, root, trained):
        styles.record(root, {"style_id": "second", "name": "Brighter"})
        assert styles.active(root)["style_id"] == "second"
        assert len(styles.trained(root)) == 2

    def test_the_active_style_can_be_switched_and_cleared(self, root, trained):
        styles.record(root, {"style_id": "second", "name": "Brighter"},
                      make_active=False)
        assert styles.set_active(root, "w29t6pvy0")["style_id"] == "w29t6pvy0"
        assert styles.set_active(root, "") is None
        with pytest.raises(LookupError):
            styles.set_active(root, "never-trained")

    def test_forgetting_clears_the_pointer_too(self, root, trained):
        assert styles.forget(root, "w29t6pvy0") is True
        assert styles.active(root) is None
        assert styles.forget(root, "w29t6pvy0") is False

    def test_a_trained_style_lands_on_the_bus(self, root, trained):
        kinds = [e for e in events.since(root, 0)["events"]
                 if e["kind"] == "style.trained"]
        assert kinds and kinds[-1]["payload"]["style_id"] == "w29t6pvy0"


class TestTheDataset:
    def test_it_is_the_pinned_anchors_and_it_names_what_it_dropped(self, anchors):
        got = styles.dataset(anchors)
        assert got["ok"] is True
        assert set(got["usable_names"]) == {f"concept-{i}" for i in range(6)}
        # Named, not pathed: the human pinned "tiny-sprite", and telling them
        # ".bgate/refs/tiny-sprite.png is 64x32" makes them do the lookup.
        dropped = {r["name"]: r["why"] for r in got["rejected"]}
        assert "tiny-sprite" in dropped and "64x32" in dropped["tiny-sprite"]

    def test_a_named_subset_narrows_it(self, anchors):
        got = styles.dataset(anchors, ["concept-0", "concept-1"])
        assert got["usable_names"] == ["concept-0", "concept-1"]
        assert got["ok"] is False        # two is below Krea's minimum of five
        assert "at least 5" in got["reason"]

    def test_a_project_with_no_pins_says_so_rather_than_training_nothing(self, root):
        got = styles.dataset(root)
        assert got["ok"] is False and got["candidates"] == 0


class TestTheToggle:
    """`for_generation` is the ONE place that answers "does this image use the
    trained look". Everything else asks it."""

    def test_off_by_default_even_with_a_style_trained(self, root, trained):
        assert settings.get(root, "art.style_source") == "refs"
        assert styles.for_generation(root) == []

    def test_on_sends_the_style_at_its_recorded_strength(self, root, trained):
        settings.set(root, "art.style_source", "lora")
        got = styles.for_generation(root)
        assert got == [{"id": "w29t6pvy0", "strength": 0.85}]

    def test_the_setting_supplies_the_strength_when_the_record_does_not(self, root):
        styles.record(root, {"style_id": "s1", "name": "s", "strength": None})
        settings.set(root, "art.style_source", "lora")
        settings.set(root, "art.lora_strength", 0.4)
        assert styles.for_generation(root)[0]["strength"] == 0.4

    def test_lora_mode_with_nothing_trained_falls_back_rather_than_failing(self, root):
        """The mode is a preference, not a promise. A project that flips the
        toggle before training must generate the way it always did, not refuse
        every image."""
        settings.set(root, "art.style_source", "lora")
        assert styles.for_generation(root) == []

    def test_an_unreadable_setting_never_costs_an_image(self, root, trained,
                                                        monkeypatch):
        monkeypatch.setattr(settings, "get",
                            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
        assert styles.for_generation(root) == []

    def test_no_project_means_no_style(self):
        assert styles.for_generation(None) == []


class TestTheDoorItReachesGenerationBy:
    """chroma.generate is the single door every image walks through — the same
    one that appends the bible. A look that applies to the art seat but not to a
    workflow node is not this project's look."""

    @pytest.fixture()
    def sent(self, monkeypatch):
        seen: dict = {}

        def fake(prompt, out_path, **kw):
            seen.update(kw)
            seen["prompt"] = prompt
            from pathlib import Path
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
            return {"ok": True, "path": str(out_path), "bytes": 48,
                    "provider": "krea", "model": "krea-2-medium",
                    "estimated_usd": 0.03, "seconds": 0.1}

        from bgate_adapters import krea
        monkeypatch.setattr(krea, "generate", fake)
        return seen

    def test_nothing_extra_is_sent_when_the_toggle_is_off(self, root, trained,
                                                          sent, tmp_path):
        chroma.generate("a hero", tmp_path / "out.png", provider="krea",
                        keyed=False, root=root)
        assert not sent.get("styles")

    def test_the_trained_style_rides_alongside_the_references(self, root, trained,
                                                              sent, tmp_path):
        """BOTH, which is the entire point of training: the LoRA carries the
        style so the reference slot is free to carry identity."""
        settings.set(root, "art.style_source", "lora")
        anchor = tmp_path / "anchor.png"
        pytest.importorskip("PIL.Image")
        _png(anchor)
        chroma.generate("a hero", tmp_path / "out.png", provider="krea",
                        keyed=False, root=root, ref_paths=[str(anchor)])
        assert sent["styles"] == [{"id": "w29t6pvy0", "strength": 0.85}]
        assert len(sent["style_refs"]) == 1

    def test_a_broken_style_module_does_not_stop_an_image(self, root, trained,
                                                          sent, tmp_path,
                                                          monkeypatch):
        settings.set(root, "art.style_source", "lora")
        monkeypatch.setattr(styles, "for_generation",
                            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
        got = chroma.generate("a hero", tmp_path / "out.png", provider="krea",
                              keyed=False, root=root)
        assert got["ok"] is True and not sent.get("styles")


class TestOverTheWire:
    @pytest.fixture()
    def client(self, root, monkeypatch):
        from fastapi.testclient import TestClient
        from bgate_ui.app import app
        monkeypatch.setenv("BGATE_ROOT", str(root))
        return TestClient(app)

    def test_the_panel_payload_carries_the_dataset_verdict(self, client, anchors):
        got = client.get("/api/art/style").json()
        data = got.get("data", got)
        assert data["mode"] == "refs" and data["active"] is None
        assert data["dataset"]["ok"] is True

    def test_training_without_a_name_is_refused_before_anything_uploads(self, client):
        assert client.post("/api/art/style/train", json={}).status_code == 400

    def test_a_dataset_that_cannot_train_names_the_anchors_it_dropped(self, client,
                                                                      anchors):
        got = client.post("/api/art/style/train",
                          json={"name": "Too Few", "names": ["tiny-sprite"]})
        assert got.status_code == 400
        detail = got.json()["error"]["detail"]
        assert any(r.get("name") == "tiny-sprite" for r in detail["rejected"])

    def test_activating_a_style_that_does_not_exist_is_a_404(self, client):
        assert client.post("/api/art/style/active",
                           json={"style_id": "nope"}).status_code == 404

    def test_forgetting_says_the_style_still_exists_upstream(self, client, root,
                                                             trained):
        got = client.delete("/api/art/style/w29t6pvy0")
        assert got.status_code == 200
        assert "still exists" in (got.json().get("data") or got.json())["note"]
