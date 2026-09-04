"""Cost and latency are FACTS the art UI shows — so they must be produced.

The price table in imagegen has always existed and nothing ever put a number in
front of a human: no elapsed time, no dollars. These tests pin the two things
that make "~$0.04 · 18.4s" on a candidate honest:

  1. every generate/edit result carries ``seconds`` and ``usd``;
  2. ``usd`` comes from IMAGE_PRICE_USD — never a number invented somewhere
     else, and never drifting between quality tiers.

Nothing sums those figures anywhere: this product keeps no ledger and holds no
budget, and what an account was actually charged is the provider's to report.

The OpenAI client is faked the same way tests/store/test_imagegen.py fakes it.
"""
from __future__ import annotations

import base64
import types

import pytest

from bgate_adapters import imagegen


@pytest.fixture()
def stub(tmp_path, monkeypatch):
    """A fake OpenAI client that costs nothing and takes a measurable moment."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.delenv("BGATE_IMAGE_MODEL", raising=False)
    captured: dict = {}

    class _Images:
        def generate(self, **kw):
            captured["generate"] = kw
            return types.SimpleNamespace(data=[types.SimpleNamespace(
                b64_json=base64.b64encode(b"\x89PNG\r\n\x1a\nstub").decode(),
                revised_prompt=None)])

        def edit(self, **kw):
            captured["edit"] = kw
            return self.generate()

    class _Client:
        def __init__(self, timeout=None):
            self.images = _Images()

    monkeypatch.setattr("openai.OpenAI", _Client)
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\nref")
    return {"captured": captured, "ref": ref, "tmp": tmp_path}


class TestResultCarriesCostAndLatency:
    def test_generate_returns_seconds_and_usd(self, stub):
        got = imagegen.generate("a tomato boxer portrait",
                                str(stub["tmp"] / "g.png"), quality="medium")
        assert got["ok"] is True, got
        assert isinstance(got["seconds"], float)
        assert got["seconds"] >= 0
        assert got["usd"] == imagegen.IMAGE_PRICE_USD["medium"]

    def test_edit_returns_seconds_and_usd(self, stub):
        got = imagegen.edit("same character, jab", [str(stub["ref"])],
                            str(stub["tmp"] / "e.png"), quality="high")
        assert got["ok"] is True, got
        assert isinstance(got["seconds"], float)
        assert got["usd"] == imagegen.IMAGE_PRICE_USD["high"]

    @pytest.mark.parametrize("quality", ["low", "medium", "high", "auto"])
    def test_price_comes_from_the_one_table(self, stub, quality):
        got = imagegen.generate("x", str(stub["tmp"] / f"{quality}.png"),
                                quality=quality)
        assert got["usd"] == imagegen.price_per_image(quality)

    def test_failed_call_still_reports_elapsed_and_no_charge(self, tmp_path,
                                                             monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

        class _Images:
            def generate(self, **kw):
                raise RuntimeError("quota exhausted")

        class _Client:
            def __init__(self, timeout=None):
                self.images = _Images()

        monkeypatch.setattr("openai.OpenAI", _Client)
        got = imagegen.generate("x", str(tmp_path / "boom.png"))
        assert got["ok"] is False
        assert "quota exhausted" in got["error"]
        assert got["seconds"] >= 0
        assert got["usd"] == 0.0

    def test_cost_meta_is_what_lands_in_artifact_metadata(self, stub):
        got = imagegen.generate("x", str(stub["tmp"] / "m.png"), quality="low")
        meta = imagegen.cost_meta(got)
        assert set(meta) == {"seconds", "usd"}
        assert meta["usd"] == imagegen.IMAGE_PRICE_USD["low"]
        assert meta["seconds"] == got["seconds"]
        # A caller that hands over nothing gets nulls, not an exception —
        # register-time metadata must never be the thing that fails a render.
        assert imagegen.cost_meta({}) == {"seconds": None, "usd": None}
