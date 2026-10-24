"""The node shows REAL media and REAL money - both halves, pinned.

A workflow node used to describe its output and say nothing about the picture it
made or the dollars it burned. Phase 2 puts both on the card. Two things can
silently rot:

  1. the batch read (``GET /api/node/media``) - one call per canvas load, because
     ``renderBody`` runs on every paint and must never do I/O. Its payload shape
     and its path normalisation (``/api/preview`` REFUSES absolute paths, so an
     absolute path that escapes the project must come back refused, never
     leaked);
  2. the price table - the estimate on a node has to be
     ``count x variants x IMAGE_PRICE_USD[quality]`` from the ADAPTER. A price
     constant typed into JavaScript is an estimate that drifts away from the
     charge the moment either side moves, so the static half asserts no step file
     writes one.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bgate_adapters import imagegen
from bgate_core.store import artifacts
from bgate_ui import api
from bgate_ui.app import app
from bgate_ui.routes import node_media

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "public"
STEP_FILES = [STATIC / "wf_steps_model.js", STATIC / "wf_steps_tools.js",
              STATIC / "wf_steps_templates.js"]
WF_JS = STATIC / "wf.js"


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app, headers={"x-bgate-token": api.ensure_token(root)})


def _png(root, rel: str) -> Path:
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + rel.encode())
    return path


def _payload(client, **params) -> dict:
    res = client.get("/api/node/media", params=params)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True, body
    return body["data"]


# --------------------------------------------------------------------------- #
# the batch payload
# --------------------------------------------------------------------------- #
class TestBatchPayload:
    def test_empty_project_still_answers_with_prices(self, client):
        data = _payload(client)
        assert data["prices"] == imagegen.IMAGE_PRICE_USD
        assert data["default_quality"] in imagegen.IMAGE_PRICE_USD
        assert data["assets"] == {} and data["names"] == []

    def test_unknown_name_is_an_empty_state_not_an_error(self, client):
        """A node naming an asset nothing has produced must degrade to the empty
        plate - a 404 would blank the whole canvas."""
        data = _payload(client, names="nobody-has-made-this")
        entry = data["assets"]["nobody-has-made-this"]
        assert entry["latest"] is None
        assert entry["candidates"] == []
        assert entry["revisions"] == 0

    def test_latest_and_candidate_strip(self, client, root):
        for rev in range(1, 6):
            _png(root, f"art/scoville_{rev}.png")
            artifacts.register(root, "scoville", f"art/scoville_{rev}.png",
                               producer="art")
        data = _payload(client, names="scoville", candidates=4)
        entry = data["assets"]["scoville"]
        assert entry["latest"]["revision"] == 5, entry
        assert entry["latest"]["rel"] == "art/scoville_5.png"
        # newest first, capped at what was asked for
        assert [c["revision"] for c in entry["candidates"]] == [5, 4, 3, 2]
        assert entry["revisions"] == 5
        assert "scoville" in data["names"]

    def test_candidate_cap_is_bounded(self, client, root):
        _png(root, "art/one.png")
        artifacts.register(root, "one", "art/one.png")
        entry = _payload(client, names="one", candidates=999)["assets"]["one"]
        assert len(entry["candidates"]) <= node_media.MAX_CANDIDATES

    def test_unpreviewable_revision_yields_no_rel(self, client, root):
        """A .glb has no thumbnail. It must come back with rel="" (the node
        renders the empty plate) rather than a path that 415s the preview."""
        _png(root, "models/beetle.glb")
        artifacts.register(root, "beetle", "models/beetle.glb")
        entry = _payload(client, names="beetle")["assets"]["beetle"]
        assert entry["revisions"] == 1
        assert entry["latest"] is None and entry["candidates"] == []

    def test_every_rel_is_servable_by_preview(self, client, root):
        _png(root, "art/hero.png")
        artifacts.register(root, "hero", "art/hero.png")
        rel = _payload(client, names="hero")["assets"]["hero"]["latest"]["rel"]
        assert client.get("/api/preview", params={"rel": rel}).status_code == 200

    def test_names_omitted_returns_everything_known(self, client, root):
        for name in ("alpha", "beta"):
            _png(root, f"art/{name}.png")
            artifacts.register(root, name, f"art/{name}.png")
        data = _payload(client)
        assert set(data["assets"]) == {"alpha", "beta"}
        assert sorted(data["names"]) == ["alpha", "beta"]


# --------------------------------------------------------------------------- #
# prices - the FORWARD estimate, and nothing that sums what was spent
# --------------------------------------------------------------------------- #
class TestPrices:
    def test_prices_are_the_adapters_own_table(self, client):
        prices = _payload(client)["prices"]
        assert prices == imagegen.IMAGE_PRICE_USD
        # the estimate a node shows is count x price[quality] - pin the arithmetic
        assert 6 * 2 * prices["medium"] == pytest.approx(
            12 * imagegen.price_per_image("medium"))


# --------------------------------------------------------------------------- #
# path normalisation
# --------------------------------------------------------------------------- #
class TestPathNormalisation:
    def test_relative_path_passes_through(self, root):
        assert node_media.rel_for_preview(root, "art/hero.png") == "art/hero.png"

    def test_backslashes_become_forward_slashes(self, root):
        assert node_media.rel_for_preview(root, r"art\hero.png") == "art/hero.png"

    def test_absolute_path_inside_the_project_is_made_relative(self, root):
        absolute = str(Path(root) / "art" / "hero.png")
        got = node_media.rel_for_preview(root, absolute)
        assert got == "art/hero.png"
        assert not Path(got).is_absolute()

    def test_absolute_path_outside_the_project_is_refused(self, root, tmp_path):
        outside = tmp_path.parent / "elsewhere" / "secret.png"
        assert node_media.rel_for_preview(root, str(outside)) == ""

    def test_traversal_is_refused(self, root):
        assert node_media.rel_for_preview(root, "../../etc/passwd.png") == ""

    def test_blank_is_refused(self, root):
        assert node_media.rel_for_preview(root, "") == ""
        assert node_media.rel_for_preview(root, None) == ""

    def test_no_rel_the_endpoint_returns_is_ever_absolute(self, client, root):
        _png(root, "art/hero.png")
        artifacts.register(root, "hero", "art/hero.png")
        for entry in _payload(client)["assets"].values():
            for media in ([entry["latest"]] if entry["latest"] else []) + entry["candidates"]:
                assert media["rel"]
                assert not Path(media["rel"]).is_absolute()
                assert "\\" not in media["rel"]


# --------------------------------------------------------------------------- #
# the static half: the browser must not invent prices or do I/O in a body
# --------------------------------------------------------------------------- #
def _sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in STEP_FILES + [WF_JS]}


# a bare dollar-ish float literal, e.g. 0.042 - the shape of a smuggled price
PRICE_LITERAL = re.compile(r"\b0\.\d{2,}\b")
KNOWN_PRICES = {str(v) for v in imagegen.IMAGE_PRICE_USD.values()}


@pytest.mark.parametrize("name", sorted(_sources()))
def test_no_step_file_writes_a_price_constant(name):
    """Prices come from the adapter over the wire, never from JavaScript.

    A literal 0.042 in a step file is an estimate that silently stops matching
    the charge the moment the adapter's table moves.
    """
    src = _sources()[name]
    for literal in PRICE_LITERAL.findall(src):
        assert literal not in KNOWN_PRICES, (
            f"{name} hard-codes the image price {literal} — read it from "
            "/api/node/media (imagegen.IMAGE_PRICE_USD) instead")


def test_wf_reads_prices_from_the_batch_endpoint():
    src = WF_JS.read_text(encoding="utf-8")
    assert "/api/node/media" in src, "wf.js never calls the batch endpoint"
    assert "prices" in src and "estimate(node)" in src
    # no fallback unit price: an invented number is worse than no number
    assert re.search(r"unit\s*==\s*null[^\n]*\n\s*return null", src) or \
        "if (unit == null) return null;" in src


def test_empty_state_never_becomes_a_broken_image():
    """A missing picture degrades to the engine's empty plate."""
    src = WF_JS.read_text(encoding="utf-8")
    assert "W.image(null, { empty:" in src
    for path in STEP_FILES:
        text = path.read_text(encoding="utf-8")
        assert "placeholder.png" not in text and "via.placeholder" not in text
