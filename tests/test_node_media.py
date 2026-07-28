"""The node shows REAL media and REAL money — both halves, pinned.

A workflow node used to describe its output and say nothing about the picture it
made or the dollars it burned. Phase 2 puts both on the card. Two things can
silently rot:

  1. the batch read (``GET /api/node/media``) — one call per canvas load, because
     ``renderBody`` runs on every paint and must never do I/O. Its payload shape,
     its spend figures (which must equal ``spend.for_logical``), and its path
     normalisation (``/api/preview`` REFUSES absolute paths, so an absolute path
     that escapes the project must come back refused, never leaked);
  2. the price table — the estimate on a node has to be
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
from bgate_core import artifacts, spend
from bgate_ui import api
from bgate_ui.app import app
from bgate_ui.routes import node_media

STATIC = Path(__file__).resolve().parents[1] / "bgate_ui" / "static"
STEP_FILES = [STATIC / "wf_steps_asset.js", STATIC / "wf_steps_agent.js",
              STATIC / "wf_steps_world.js", STATIC / "wf_steps_3d.js"]
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
        assert "project_usd" in data["spend"]

    def test_unknown_name_is_an_empty_state_not_an_error(self, client):
        """A node naming an asset nothing has produced must degrade to the empty
        plate — a 404 would blank the whole canvas."""
        data = _payload(client, names="nobody-has-made-this")
        entry = data["assets"]["nobody-has-made-this"]
        assert entry["latest"] is None
        assert entry["candidates"] == []
        assert entry["revisions"] == 0
        assert entry["usd"] == 0.0

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
# money
# --------------------------------------------------------------------------- #
class TestSpendFigures:
    def test_usd_matches_spend_for_logical(self, client, root):
        _png(root, "art/scoville.png")
        artifacts.register(root, "scoville", "art/scoville.png")
        spend.record(root, 0.042, kind="image", logical_name="scoville")
        spend.record(root, 0.098, kind="image", logical_name="scoville")
        spend.record(root, 5.0, kind="agent", logical_name="someone-else")

        entry = _payload(client, names="scoville")["assets"]["scoville"]
        assert entry["usd"] == pytest.approx(spend.for_logical(root, "scoville"))
        assert entry["usd"] == pytest.approx(0.14)

    def test_totals_ride_along_so_the_chrome_has_a_number(self, client, root):
        spend.record(root, 1.25, kind="image", logical_name="scoville")
        data = _payload(client)
        assert data["spend"]["project_usd"] == pytest.approx(1.25)
        assert "budget" in data["spend"]

    def test_prices_are_the_adapters_own_table(self, client):
        prices = _payload(client)["prices"]
        assert prices == imagegen.IMAGE_PRICE_USD
        # the estimate a node shows is count x price[quality] — pin the arithmetic
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


# a bare dollar-ish float literal, e.g. 0.042 — the shape of a smuggled price
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


def test_estimate_multiplies_images_by_the_fetched_unit_price():
    src = WF_JS.read_text(encoding="utf-8")
    assert "images * unit" in src, "the estimate is not count x unit price"


@pytest.mark.parametrize("name", sorted(p.name for p in STEP_FILES))
def test_node_bodies_do_no_io(name):
    """A body runs on every paint. fetch() in one is a request per repaint."""
    src = (STATIC / name).read_text(encoding="utf-8")
    for token in ("fetch(", "XMLHttpRequest"):
        assert token not in src, f"{name} does I/O — bodies must read WF's cache"


def _step_blocks() -> dict[str, str]:
    """Every ``WF.registerStep({...})`` body, brace-matched, by step type.

    Templates repeat the same ``type:"art.concept"`` strings, so a naive
    last-match scan reads a template where it means a step.
    """
    out: dict[str, str] = {}
    for path in STEP_FILES:
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"WF\.registerStep\(\s*\{", src):
            i = src.index("{", m.end() - 1)
            depth, j = 0, i
            while j < len(src):
                if src[j] == "{":
                    depth += 1
                elif src[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            block = src[i:j + 1]
            t = re.search(r'type:\s*"([\w.]+)"', block)
            if t:
                out[t.group(1)] = block
    return out


def test_image_steps_declare_what_they_cost():
    """Every step that spends money through the image adapter declares
    imageCost(); steps that spend nothing must NOT (a fake estimate on a free
    step is a lie in the title bar)."""
    costed = {"art.concept": "wf_steps_asset.js", "art.anchor": "wf_steps_asset.js",
              "art.animation": "wf_steps_asset.js", "art.edit": "wf_steps_asset.js",
              "agent.art": "wf_steps_agent.js", "world.background": "wf_steps_world.js",
              "world.parallax": "wf_steps_world.js", "world.tileset": "wf_steps_world.js",
              "world.props": "wf_steps_world.js", "3d.concept": "wf_steps_3d.js"}
    free = ("output.sheet", "output.rig", "control.select", "control.gate",
            "3d.sprites", "3d.model", "3d.import")
    blocks = _step_blocks()
    for step_type in costed:
        assert step_type in blocks, f"{step_type} is no longer registered"
        assert "imageCost" in blocks[step_type], (
            f"{step_type} spends on images but declares no imageCost() — its "
            "node can never show an estimate")
    for step_type in free:
        block = blocks.get(step_type, "")
        assert "imageCost" not in block, (
            f"{step_type} spends no API money but declares imageCost()")


def test_empty_state_never_becomes_a_broken_image():
    """A missing picture degrades to the engine's empty plate."""
    src = WF_JS.read_text(encoding="utf-8")
    assert "W.image(null, { empty:" in src
    for path in STEP_FILES:
        text = path.read_text(encoding="utf-8")
        assert "placeholder.png" not in text and "via.placeholder" not in text
