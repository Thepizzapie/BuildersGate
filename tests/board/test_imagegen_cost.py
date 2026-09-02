"""Cost and latency are FACTS the art UI shows — so they must be produced.

The price table in imagegen has always existed and nothing ever put a number in
front of a human: no elapsed time, no dollars, no ledger row. These tests pin
the three things that make the art lab's "$0.42 spent · 18.4s" honest:

  1. every generate/edit result carries ``seconds`` and ``estimated_usd``;
  2. ``estimated_usd`` comes from IMAGE_PRICE_USD — never a number invented
     somewhere else, and never drifting between quality tiers;
  3. passing ``root`` appends a spend_event so ``spend.for_logical`` — the exact
     call the lab header makes — can answer what an asset cost.

The OpenAI client is faked the same way tests/store/test_imagegen.py fakes it.
"""
from __future__ import annotations

import base64
import types

import pytest

from bgate_adapters import imagegen
from bgate_core.board import spend


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
        assert got["estimated_usd"] == imagegen.IMAGE_PRICE_USD["medium"]

    def test_edit_returns_seconds_and_usd(self, stub):
        got = imagegen.edit("same character, jab", [str(stub["ref"])],
                            str(stub["tmp"] / "e.png"), quality="high")
        assert got["ok"] is True, got
        assert isinstance(got["seconds"], float)
        assert got["estimated_usd"] == imagegen.IMAGE_PRICE_USD["high"]

    @pytest.mark.parametrize("quality", ["low", "medium", "high", "auto"])
    def test_price_comes_from_the_one_table(self, stub, quality):
        got = imagegen.generate("x", str(stub["tmp"] / f"{quality}.png"),
                                quality=quality)
        assert got["estimated_usd"] == imagegen.price_per_image(quality)

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
        assert got["estimated_usd"] == 0.0

    def test_cost_meta_is_what_lands_in_artifact_metadata(self, stub):
        got = imagegen.generate("x", str(stub["tmp"] / "m.png"), quality="low")
        meta = imagegen.cost_meta(got)
        assert set(meta) == {"seconds", "estimated_usd"}
        assert meta["estimated_usd"] == imagegen.IMAGE_PRICE_USD["low"]
        assert meta["seconds"] == got["seconds"]
        # A caller that hands over nothing gets nulls, not an exception —
        # register-time metadata must never be the thing that fails a render.
        assert imagegen.cost_meta({}) == {"seconds": None, "estimated_usd": None}


class TestTheLedgerIsWritten:
    def test_generate_records_spend_for_the_logical_asset(self, stub, root):
        got = imagegen.generate("x", str(stub["tmp"] / "t.png"), quality="high",
                                root=root, logical_name="tommy")
        assert got["ok"] is True
        assert spend.for_logical(root, "tommy") == pytest.approx(
            imagegen.IMAGE_PRICE_USD["high"])
        totals = spend.totals(root)
        assert totals["by_kind"]["image"] == pytest.approx(
            imagegen.IMAGE_PRICE_USD["high"])

    def test_edit_records_spend_too(self, stub, root):
        imagegen.edit("x", [str(stub["ref"])], str(stub["tmp"] / "e.png"),
                      quality="medium", root=root, logical_name="tommy")
        assert spend.for_logical(root, "tommy") == pytest.approx(
            imagegen.IMAGE_PRICE_USD["medium"])

    def test_a_sprite_set_accumulates_on_one_logical_name(self, stub, root):
        """Twelve poses are twelve charges against ONE asset — that sum is the
        number the iteration-lab header shows."""
        for i in range(3):
            imagegen.edit("pose", [str(stub["ref"])],
                          str(stub["tmp"] / f"p{i}.png"), quality="low",
                          root=root, logical_name="tommy")
        assert spend.for_logical(root, "tommy") == pytest.approx(
            3 * imagegen.IMAGE_PRICE_USD["low"])

    def test_spend_is_charged_to_the_work_item(self, stub, root):
        from bgate_core.board import queue

        item = queue.add(root, "art", "paint tommy")
        imagegen.generate("x", str(stub["tmp"] / "w.png"), quality="medium",
                          root=root, logical_name="tommy",
                          work_item_id=item["id"])
        from bgate_core.store import db

        row = db.connect(root).execute(
            "SELECT total_cost_usd FROM work_item WHERE id = ?",
            (item["id"],)).fetchone()
        assert row["total_cost_usd"] == pytest.approx(
            imagegen.IMAGE_PRICE_USD["medium"])

    def test_no_root_means_no_ledger_write_and_no_crash(self, stub, root):
        got = imagegen.generate("x", str(stub["tmp"] / "n.png"))
        assert got["ok"] is True
        assert spend.totals(root)["events"] == 0

    def test_a_failed_generation_is_never_charged(self, tmp_path, monkeypatch,
                                                  root):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")

        class _Images:
            def generate(self, **kw):
                raise RuntimeError("content policy")

        class _Client:
            def __init__(self, timeout=None):
                self.images = _Images()

        monkeypatch.setattr("openai.OpenAI", _Client)
        imagegen.generate("x", str(tmp_path / "no.png"), root=root,
                          logical_name="tommy")
        assert spend.for_logical(root, "tommy") == 0.0

    def test_a_broken_ledger_never_loses_the_image(self, stub, monkeypatch):
        """The image is the product; the ledger is bookkeeping. If recording
        raises, the render still comes back ok."""
        def _boom(*a, **kw):
            raise RuntimeError("db is gone")

        monkeypatch.setattr(spend, "record", _boom)
        got = imagegen.generate("x", str(stub["tmp"] / "s.png"),
                                root=stub["tmp"], logical_name="tommy")
        assert got["ok"] is True
        assert (stub["tmp"] / "s.png").exists()


class TestTheCostEndpoint:
    """/api/art/cost is what art.js reads for the header total and the live
    '~$X.XX' batch estimate — it must serve the adapter's real price table."""

    def _client(self, root, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("BGATE_ROOT", str(root))
        from bgate_ui import app as app_mod

        return TestClient(app_mod.app)

    def test_reports_prices_and_per_asset_totals(self, root, monkeypatch):
        spend.record(root, 0.042, kind="image", logical_name="tommy")
        spend.record(root, 0.011, kind="image", logical_name="tommy")
        spend.record(root, 0.167, kind="image", logical_name="colosseum")
        client = self._client(root, monkeypatch)

        body = client.get("/api/art/cost").json()
        assert body["ok"] is True
        data = body["data"]
        assert data["prices"] == imagegen.IMAGE_PRICE_USD
        assert data["by_logical"]["tommy"] == pytest.approx(0.053)
        assert data["by_logical"]["colosseum"] == pytest.approx(0.167)

        one = client.get("/api/art/cost", params={"logical_name": "tommy"}).json()
        assert one["data"]["usd"] == pytest.approx(0.053)

    def test_empty_project_answers_zeroes_not_an_error(self, root, monkeypatch):
        client = self._client(root, monkeypatch)
        body = client.get("/api/art/cost").json()
        assert body["ok"] is True
        assert body["data"]["by_logical"] == {}
