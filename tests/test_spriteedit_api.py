"""The sprite editor's API and the wiring API — the two surfaces that WRITE.

Everything else in the dashboard reads. These two put bytes into a PNG the
engine imports and lines into a .tscn the engine loads, from a browser, over
HTTP. So the tests are mostly about refusal: what must not be written, what
must not be reachable, and what must be recoverable afterwards.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from bgate_core import rigmap
from bgate_ui.app import app


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def game(root):
    """A Godot project at the root with one sheet and one scene in it."""
    (root / "project.godot").write_text(
        'config_version=5\n\n[application]\n\nconfig/name="Test"\n',
        encoding="utf-8")
    sprites = root / "assets" / "sprites"
    sprites.mkdir(parents=True)
    img = Image.new("RGBA", (128, 64), (0, 0, 0, 0))
    for x in range(4):
        for y in range(8):
            img.putpixel((x * 32 + 8, y * 8 + 4), (200, 40, 40, 255))
    img.save(sprites / "hero_sheet.png")
    scenes = root / "scenes"
    scenes.mkdir()
    (scenes / "main.tscn").write_text(
        '[gd_scene load_steps=1 format=3]\n\n'
        '[node name="Main" type="Node2D"]\n', encoding="utf-8")
    return root


def _png(size, colour=(10, 20, 30, 255)) -> str:
    buf = io.BytesIO()
    Image.new("RGBA", size, colour).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


SHEET = "assets/sprites/hero_sheet.png"


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------
def test_open_reports_size_res_path_and_an_empty_rig(client, game):
    r = client.get("/api/sprite/open", params={"rel": SHEET})
    assert r.status_code == 200
    d = r.json()
    assert (d["width"], d["height"]) == (128, 64)
    assert d["res_path"] == "res://assets/sprites/hero_sheet.png"
    assert d["rig"]["labels"] == []
    assert d["sidecar"].endswith("hero_sheet.rig.json")
    assert "main_hand" in d["known_slots"]


def test_open_refuses_a_path_that_escapes_the_project(client, game):
    r = client.get("/api/sprite/open", params={"rel": "../../etc/passwd"})
    assert r.status_code in (403, 415)


def test_only_png_and_webp_are_editable(client, game):
    (game / "note.jpg").write_bytes(b"x")
    r = client.get("/api/sprite/open", params={"rel": "note.jpg"})
    assert r.status_code == 415


def test_list_finds_the_sheet_and_flags_it_unrigged(client, game):
    d = client.get("/api/sprite/list").json()
    hit = next(s for s in d["sheets"] if s["rel"] == SHEET)
    assert hit["width"] == 128 and hit["rigged"] is False


# ---------------------------------------------------------------------------
# Saving pixels
# ---------------------------------------------------------------------------
def test_save_writes_the_pixels_and_keeps_the_old_ones(client, game):
    before = (game / SHEET).read_bytes()
    r = client.post("/api/sprite/save",
                    json={"rel": SHEET, "png": "data:image/png;base64," + _png((128, 64))})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert (game / SHEET).read_bytes() != before
    assert (game / data["backup"]).read_bytes() == before


def test_a_save_may_not_resize_the_sheet(client, game):
    """Every SpriteFrames region and gear anchor is in these exact dimensions."""
    r = client.post("/api/sprite/save",
                    json={"rel": SHEET, "png": _png((64, 64))})
    assert r.status_code == 409
    assert "resize" in r.text


def test_a_stale_mtime_is_refused_rather_than_clobbering(client, game):
    r = client.post("/api/sprite/save",
                    json={"rel": SHEET, "png": _png((128, 64)), "mtime": 1})
    assert r.status_code == 409
    assert "changed on disk" in r.text


def test_garbage_payloads_are_refused(client, game):
    assert client.post("/api/sprite/save",
                       json={"rel": SHEET, "png": ""}).status_code == 400
    assert client.post("/api/sprite/save",
                       json={"rel": SHEET, "png": "!!!not base64!!!"}).status_code == 400
    assert client.post("/api/sprite/save",
                       json={"rel": SHEET,
                             "png": base64.b64encode(b"nope").decode()}).status_code == 400


# ---------------------------------------------------------------------------
# The rig sidecar
# ---------------------------------------------------------------------------
def test_rig_saves_next_to_the_sheet_and_comes_back_on_open(client, game):
    rig = {"grid": {"cell_w": 32, "cell_h": 32, "cols": 4, "rows": 2},
           "animations": [{"name": "walk", "frames": [0, 1, 2, 3]}],
           "labels": [{"slot": "main_hand", "frame": 0, "x": 12, "y": 20}]}
    r = client.post("/api/sprite/rig", json={"rel": SHEET, "rig": rig})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["sidecar"] == "assets/sprites/hero_sheet.rig.json"
    assert (game / d["sidecar"]).is_file()
    assert d["coverage"]["missing"]["main_hand"] == [1, 2, 3]

    reopened = client.get("/api/sprite/open", params={"rel": SHEET}).json()
    assert reopened["rig"]["labels"][0]["slot"] == "main_hand"
    assert reopened["rig"]["labels"][0]["source"] == "authored"


def test_a_rig_that_does_not_tile_the_sheet_is_refused(client, game):
    r = client.post("/api/sprite/rig", json={
        "rel": SHEET,
        "rig": {"grid": {"cell_w": 30, "cell_h": 32, "cols": 4, "rows": 2}}})
    assert r.status_code == 400
    assert "does not tile" in r.text


def test_autogrid_validates_a_typed_cell_size(client, game):
    ok = client.post("/api/sprite/autogrid",
                     json={"rel": SHEET, "cell_w": 32, "cell_h": 32})
    assert ok.json()["data"]["grid"]["cols"] == 4
    assert ok.json()["data"]["frames"] == 8
    bad = client.post("/api/sprite/autogrid",
                      json={"rel": SHEET, "cell_w": 30, "cell_h": 32})
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# SpriteFrames export
# ---------------------------------------------------------------------------
def test_export_writes_a_loadable_spriteframes(client, game):
    client.post("/api/sprite/rig", json={
        "rel": SHEET,
        "rig": {"grid": {"cell_w": 32, "cell_h": 32, "cols": 4, "rows": 2},
                "animations": [{"name": "walk", "frames": [4, 5]}]}})
    r = client.post("/api/sprite/spriteframes", json={"rel": SHEET})
    assert r.status_code == 200, r.text
    out = game / r.json()["data"]["written"]
    text = out.read_text(encoding="utf-8")
    assert out.name == "hero_sheet_frames.tres"
    assert 'path="res://assets/sprites/hero_sheet.png"' in text
    assert "region = Rect2(0, 32, 32, 32)" in text
    assert '&"walk"' in text


def test_export_refuses_a_rig_with_nothing_to_export(client, game):
    r = client.post("/api/sprite/spriteframes", json={"rel": SHEET})
    assert r.status_code == 400
    assert "animation" in r.text


def test_export_dry_run_writes_nothing(client, game):
    client.post("/api/sprite/rig", json={
        "rel": SHEET,
        "rig": {"grid": {"cell_w": 32, "cell_h": 32, "cols": 4, "rows": 2},
                "animations": [{"name": "idle", "frames": [0]}]}})
    r = client.post("/api/sprite/spriteframes", json={"rel": SHEET, "dry_run": True})
    assert r.json()["data"]["dry_run"] is True
    assert not (game / "assets/sprites/hero_sheet_frames.tres").exists()


# ---------------------------------------------------------------------------
# Frame regeneration
#
# The adapter is stubbed everywhere below. A suite that calls a paid image API
# is a suite nobody runs, and the interesting behaviour here is not the model —
# it is the GEOMETRY (one cell in, that same cell back, nothing else touched)
# and the REFUSALS (which must all fire before a call is made).
# ---------------------------------------------------------------------------
def _stub_edit(monkeypatch, colour=(0, 255, 0, 255), calls=None):
    from bgate_adapters import imagegen

    def fake_edit(prompt, ref_paths, out_path, **kw):
        if calls is not None:
            calls.append({"prompt": prompt, "refs": list(ref_paths), **kw})
        Image.new("RGBA", (1024, 1024), colour).save(out_path)
        return {"ok": True, "path": out_path, "seconds": 1.0,
                "estimated_usd": 0.042, "model": "stub"}

    monkeypatch.setattr(imagegen, "available", lambda: {"available": True})
    monkeypatch.setattr(imagegen, "edit", fake_edit)


GRID = {"cell_w": 32, "cell_h": 32, "cols": 4, "rows": 2}


def test_status_reports_availability_and_the_real_price_table(client, game):
    from bgate_adapters import imagegen

    d = client.get("/api/sprite/regen/status").json()
    assert d["price_usd"] == imagegen.IMAGE_PRICE_USD, \
        "a price typed into the UI drifts from the price actually charged"
    assert d["max_frames"] >= 1


def test_regen_returns_pixels_for_exactly_one_cell_and_writes_nothing(
        client, game, monkeypatch):
    calls = []
    _stub_edit(monkeypatch, calls=calls)
    before = (game / SHEET).read_bytes()
    r = client.post("/api/sprite/regen", json={
        "rel": SHEET, "frame": 5, "prompt": "add a torch", "grid": GRID})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["written"] is False
    assert (game / SHEET).read_bytes() == before, "the sheet must not be touched"
    assert d["size"] == [32, 32]
    out = Image.open(io.BytesIO(base64.b64decode(d["png"])))
    assert out.size == (32, 32)
    # The whole sheet rides along as the second reference — that is what keeps
    # the model painting the SAME character into frame 5.
    assert len(calls[0]["refs"]) == 2
    assert calls[0]["transparent"] is True


def test_the_prompt_reaches_the_model_with_single_frame_framing(
        client, game, monkeypatch):
    calls = []
    _stub_edit(monkeypatch, calls=calls)
    client.post("/api/sprite/regen", json={
        "rel": SHEET, "frame": 0, "prompt": "give it a red hood", "grid": GRID})
    sent = calls[0]["prompt"]
    assert "give it a red hood" in sent
    assert "ONE frame" in sent and "Transparent background" in sent


def test_a_prompt_naming_frames_is_not_refused_by_the_multi_pose_guard(
        client, game, monkeypatch):
    """The guard stops one image being asked to BE a sheet. Here the input is a
    single cell, so 'match the other animation frames' is a fine instruction."""
    calls = []
    _stub_edit(monkeypatch, calls=calls)
    r = client.post("/api/sprite/regen", json={
        "rel": SHEET, "frame": 0, "grid": GRID,
        "prompt": "match the other animation frames"})
    assert r.status_code == 200, r.text
    assert calls[0]["allow_multi"] is True


def test_the_returned_alpha_is_hard_not_a_soft_halo(client, game, monkeypatch):
    """Area-downscaling a 1024px render leaves a soft ramp round the silhouette
    — exactly the halo the de-halo tool exists to remove. Cut at the source."""
    _stub_edit(monkeypatch, colour=(255, 0, 0, 140))
    d = client.post("/api/sprite/regen", json={
        "rel": SHEET, "frame": 0, "prompt": "x", "grid": GRID}).json()["data"]
    out = Image.open(io.BytesIO(base64.b64decode(d["png"]))).convert("RGBA")
    assert set(out.getchannel("A").getdata()) <= {0, 255}


def test_regen_falls_back_to_the_saved_rig_grid(client, game, monkeypatch):
    _stub_edit(monkeypatch)
    client.post("/api/sprite/rig", json={"rel": SHEET, "rig": {"grid": GRID}})
    d = client.post("/api/sprite/regen", json={
        "rel": SHEET, "frame": 7, "prompt": "x"}).json()["data"]
    assert d["size"] == [32, 32]


def test_regen_refuses_before_it_spends(client, game, monkeypatch):
    calls = []
    _stub_edit(monkeypatch, calls=calls)
    bad = [
        {"rel": SHEET, "frame": 0, "grid": GRID},                    # no prompt
        {"rel": SHEET, "frame": 99, "prompt": "x", "grid": GRID},    # past the end
        {"rel": SHEET, "frame": 0, "prompt": "x",                    # grid lies
         "grid": {"cell_w": 30, "cell_h": 32, "cols": 4, "rows": 2}},
        {"rel": SHEET, "frame": 0, "prompt": "x", "grid": GRID,
         "quality": "ultra"},                                        # no such tier
        {"rel": SHEET, "frame": "nope", "prompt": "x", "grid": GRID},
    ]
    for payload in bad:
        assert client.post("/api/sprite/regen", json=payload).status_code == 400
    assert calls == [], "a refusal must never reach the paid API"


def test_regen_says_why_when_the_provider_is_off(client, game, monkeypatch):
    from bgate_adapters import imagegen
    monkeypatch.setattr(imagegen, "available",
                        lambda: {"available": False, "reason": "OPENAI_API_KEY not set"})
    r = client.post("/api/sprite/regen", json={
        "rel": SHEET, "frame": 0, "prompt": "x", "grid": GRID})
    assert r.status_code == 503
    assert "OPENAI_API_KEY" in r.text


def test_an_adapter_failure_is_reported_not_swallowed(client, game, monkeypatch):
    from bgate_adapters import imagegen
    monkeypatch.setattr(imagegen, "available", lambda: {"available": True})
    monkeypatch.setattr(imagegen, "edit",
                        lambda *a, **k: {"ok": False, "error": "rate limited"})
    r = client.post("/api/sprite/regen", json={
        "rel": SHEET, "frame": 0, "prompt": "x", "grid": GRID})
    assert r.status_code == 503 and "rate limited" in r.text


# ---------------------------------------------------------------------------
# Anatomical hands
# ---------------------------------------------------------------------------
def test_left_and_right_hands_are_first_class_slots(client, game):
    rig = {"grid": GRID, "labels": [
        {"slot": "left_hand", "frame": 3, "x": 4, "y": 9},
        {"slot": "right_hand", "frame": 3, "x": 20, "y": 9},
        {"slot": "main_hand", "frame": 3, "x": 20, "y": 9},
    ]}
    r = client.post("/api/sprite/rig", json={"rel": SHEET, "rig": rig})
    assert r.status_code == 200, r.text
    d = client.get("/api/sprite/open", params={"rel": SHEET}).json()
    assert d["hands"] == {"3": ["left_hand", "right_hand"]}
    assert d["hand_slots"] == ["left_hand", "right_hand"]
    # Anatomical and logical coexist on one frame — a right hand that HOLDS the
    # weapon is two facts, not one.
    assert len(d["rig"]["labels"]) == 3


def test_swap_hands_exchanges_only_the_anatomical_pair():

    data = rigmap.normalise({"grid": GRID, "labels": [
        {"slot": "left_hand", "frame": 0, "x": 1, "y": 1},
        {"slot": "right_hand", "frame": 0, "x": 20, "y": 1},
        {"slot": "main_hand", "frame": 0, "x": 20, "y": 1},
        {"slot": "left_hand", "frame": 1, "x": 5, "y": 5},
    ]})
    rigmap.swap_hands(data, frame=0)
    at = {(l["slot"], l["frame"]): l["x"] for l in data["labels"]}
    assert at[("left_hand", 0)] == 20 and at[("right_hand", 0)] == 1
    assert at[("main_hand", 0)] == 20, "the logical hand does not move"
    assert at[("left_hand", 1)] == 5, "another frame is untouched"


# ---------------------------------------------------------------------------
# Scene wiring over HTTP
# ---------------------------------------------------------------------------
def test_tree_lists_the_nodes_of_a_scene(client, game):
    d = client.get("/api/scene/tree",
                   params={"scene": "res://scenes/main.tscn"}).json()
    assert d["root"] == "Main"
    assert [n["name"] for n in d["nodes"]] == ["Main"]


def test_dry_run_wiring_returns_the_text_and_writes_nothing(client, game):
    before = (game / "scenes/main.tscn").read_text(encoding="utf-8")
    r = client.post("/api/scene/wire", json={
        "scene": "res://scenes/main.tscn", "asset": SHEET, "dry_run": True})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["written"] is False
    assert 'type="Sprite2D"' in d["text"]
    assert (game / "scenes/main.tscn").read_text(encoding="utf-8") == before


def test_wiring_writes_the_scene_and_atlas_then_sees_the_edge(client, game):
    r = client.post("/api/scene/wire", json={
        "scene": "res://scenes/main.tscn", "asset": SHEET})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["written"] is True and d["backup"]
    text = (game / "scenes/main.tscn").read_text(encoding="utf-8")
    assert 'path="res://assets/sprites/hero_sheet.png"' in text

    # The whole point: the derived map now contains the edge that was drawn.
    smap = client.get("/api/screenmap").json()
    assert any(e["from"] == "res://scenes/main.tscn"
               and e["to"] == "res://assets/sprites/hero_sheet.png"
               for e in smap["edges"])
    assert "res://assets/sprites/hero_sheet.png" not in smap["orphans"]


def test_unwire_removes_the_node_and_the_resource(client, game):
    w = client.post("/api/scene/wire", json={
        "scene": "res://scenes/main.tscn", "asset": SHEET}).json()["data"]
    u = client.post("/api/scene/unwire", json={
        "scene": "res://scenes/main.tscn", "node": w["node"]})
    assert u.status_code == 200, u.text
    text = (game / "scenes/main.tscn").read_text(encoding="utf-8")
    assert "hero_sheet.png" not in text


def test_wirable_flags_the_scenes_that_already_have_it(client, game):
    first = client.get("/api/scene/wirable", params={"asset": SHEET}).json()
    assert first["scenes"][0]["has_asset"] is False
    client.post("/api/scene/wire", json={
        "scene": "res://scenes/main.tscn", "asset": SHEET})
    second = client.get("/api/scene/wirable", params={"asset": SHEET}).json()
    assert second["scenes"][0]["has_asset"] is True


def test_wiring_refuses_an_asset_outside_the_project(client, game, tmp_path):
    outside = tmp_path.parent / "elsewhere.png"
    r = client.post("/api/scene/wire", json={
        "scene": "res://scenes/main.tscn", "asset": str(outside)})
    assert r.status_code in (403, 404)


def test_wiring_refuses_an_unknown_parent(client, game):
    r = client.post("/api/scene/wire", json={
        "scene": "res://scenes/main.tscn", "asset": SHEET, "parent": "Ghost"})
    assert r.status_code == 400
    assert "no node at" in r.text


# ---------------------------------------------------------------------------
# The scene builder over HTTP
# ---------------------------------------------------------------------------
def test_the_outline_is_everything_a_canvas_needs_in_one_request(client, game):
    """Per-node requests would be N round trips per repaint — the mistake
    /api/node/media already exists elsewhere to avoid."""
    client.post("/api/scene/wire", json={
        "scene": "res://scenes/main.tscn", "asset": SHEET})
    d = client.get("/api/scene/outline",
                   params={"scene": "res://scenes/main.tscn"}).json()
    assert d["root"] == "Main"
    node = next(n for n in d["nodes"] if n["name"] == "HeroSheet")
    assert node["role"] == "visual"
    res = node["resources"][0]
    assert res["property"] == "texture" and res["exists"] is True
    assert res["preview"] == SHEET, "the card needs a path /api/preview accepts"
    assert d["roles"]["visual"] >= 1


def test_the_outline_flags_a_resource_that_is_not_on_disk(client, game):
    scene = game / "scenes/main.tscn"
    scene.write_text(scene.read_text(encoding="utf-8").replace(
        "[node", '[ext_resource type="Texture2D" path="res://assets/gone.png" '
                 'id="9_gone"]\n\n[node', 1), encoding="utf-8")
    d = client.get("/api/scene/outline",
                   params={"scene": "res://scenes/main.tscn"}).json()
    assert any(r["exists"] is False for r in d["resources"])


def test_every_scene_mutation_writes_with_a_backup(client, game):
    scene_file = game / "scenes/main.tscn"
    before = scene_file.read_text(encoding="utf-8")
    r = client.post("/api/scene/node/add", json={
        "scene": "res://scenes/main.tscn", "node_type": "CanvasLayer",
        "name": "HUD"})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["written"] is True
    assert (game / d["backup"]).read_text(encoding="utf-8") == before
    assert 'type="CanvasLayer"' in scene_file.read_text(encoding="utf-8")
    assert any(n["name"] == "HUD" for n in d["nodes"])


def test_a_dry_run_of_every_mutation_writes_nothing(client, game):
    scene_file = game / "scenes/main.tscn"
    client.post("/api/scene/wire", json={
        "scene": "res://scenes/main.tscn", "asset": SHEET})
    before = scene_file.read_text(encoding="utf-8")
    calls = [
        ("/api/scene/node/add", {"node_type": "Timer", "name": "T"}),
        ("/api/scene/node/property", {"node": "HeroSheet", "key": "z_index",
                                      "value": 3}),
        ("/api/scene/node/rename", {"node": "HeroSheet", "name": "Body"}),
        ("/api/scene/node/reparent", {"node": "HeroSheet", "parent": "."}),
        ("/api/scene/unwire", {"node": "HeroSheet"}),
    ]
    for path, body in calls:
        r = client.post(path, json={"scene": "res://scenes/main.tscn",
                                    "dry_run": True, **body})
        assert r.status_code == 200, f"{path}: {r.text}"
        assert r.json()["data"]["written"] is False
        assert "text" in r.json()["data"], "a dry run must return the result"
    assert scene_file.read_text(encoding="utf-8") == before


def test_swapping_a_resource_over_http(client, game):
    other = game / "assets/sprites/villain_sheet.png"
    Image.new("RGBA", (128, 64)).save(other)
    client.post("/api/scene/wire", json={
        "scene": "res://scenes/main.tscn", "asset": SHEET})
    r = client.post("/api/scene/node/swap", json={
        "scene": "res://scenes/main.tscn", "node": "HeroSheet",
        "asset": "assets/sprites/villain_sheet.png"})
    assert r.status_code == 200, r.text
    text = (game / "scenes/main.tscn").read_text(encoding="utf-8")
    assert "villain_sheet.png" in text
    assert "hero_sheet.png" not in text, "the replaced resource must be swept"


def test_a_tres_is_wired_with_the_type_it_declares(client, game):
    """Suffix-guessing calls every .tres SpriteFrames; a TileSet wired that way
    loads as null and the node silently draws nothing."""
    tres = game / "assets/office.tres"
    tres.write_text('[gd_resource type="TileSet" load_steps=1 format=3]\n\n'
                    '[resource]\n', encoding="utf-8")
    r = client.post("/api/scene/wire", json={
        "scene": "res://scenes/main.tscn", "asset": "assets/office.tres",
        "dry_run": True})
    assert 'type="TileSet"' in r.json()["data"]["text"]


def test_the_node_palette_is_curated_not_the_whole_class_db(client):
    d = client.get("/api/scene/node/types").json()
    types = [t for g in d["groups"] for t in g["types"]]
    assert "CanvasLayer" in types and "Camera2D" in types and "Timer" in types
    assert len(types) < 80, "a palette with two thousand entries is a search box"


def test_scene_mutations_refuse_a_scene_outside_the_project(client):
    r = client.post("/api/scene/node/add", json={
        "scene": "../../elsewhere.tscn", "node_type": "Node2D"})
    assert r.status_code in (403, 404)


# ---------------------------------------------------------------------------
# The viewport
# ---------------------------------------------------------------------------
def test_render_resolves_textures_to_paths_the_browser_can_fetch(client, game):
    client.post("/api/scene/wire", json={
        "scene": "res://scenes/main.tscn", "asset": SHEET})
    d = client.get("/api/scene/render",
                   params={"scene": "res://scenes/main.tscn"}).json()
    sprite = next(i for i in d["items"] if i["name"] == "HeroSheet")
    assert sprite["draw"]["kind"] == "image"
    assert sprite["draw"]["rel"] == SHEET, "/api/preview takes root-relative paths"
    assert sprite["draw"]["size"] == [128, 64]
    assert sprite["draw"]["centered"] is True


def test_render_reads_the_projects_own_viewport(client, game):
    (game / "project.godot").write_text(
        "config_version=5\n\n[display]\n\nwindow/size/viewport_width=640\n"
        "window/size/viewport_height=360\n", encoding="utf-8")
    d = client.get("/api/scene/render",
                   params={"scene": "res://scenes/main.tscn"}).json()
    assert d["viewport"] == [640, 360]


def test_dragging_a_node_writes_its_position_and_keeps_a_backup(client, game):
    """The viewport's whole write path, end to end — this is what a drag does."""
    client.post("/api/scene/wire", json={
        "scene": "res://scenes/main.tscn", "asset": SHEET})
    before = (game / "scenes/main.tscn").read_text(encoding="utf-8")
    r = client.post("/api/scene/node/property", json={
        "scene": "res://scenes/main.tscn", "node": "HeroSheet",
        "key": "position", "value": "Vector2(128, 64)"})
    assert r.status_code == 200, r.text
    assert (game / r.json()["data"]["backup"]).read_text(encoding="utf-8") == before

    d = client.get("/api/scene/render",
                   params={"scene": "res://scenes/main.tscn"}).json()
    moved = next(i for i in d["items"] if i["name"] == "HeroSheet")
    assert (moved["x"], moved["y"]) == (128, 64), "the render reflects the write"


def test_scale_rotation_and_z_all_round_trip_through_the_render(client, game):
    client.post("/api/scene/wire", json={
        "scene": "res://scenes/main.tscn", "asset": SHEET})
    for key, value in (("scale", "Vector2(2, 3)"), ("rotation", 1.5708),
                       ("z_index", 4)):
        r = client.post("/api/scene/node/property", json={
            "scene": "res://scenes/main.tscn", "node": "HeroSheet",
            "key": key, "value": value})
        assert r.status_code == 200, f"{key}: {r.text}"
    d = client.get("/api/scene/render",
                   params={"scene": "res://scenes/main.tscn"}).json()
    it = next(i for i in d["items"] if i["name"] == "HeroSheet")
    assert (it["sx"], it["sy"]) == (2, 3)
    assert it["rot"] == pytest.approx(1.5708, abs=1e-4)
    assert it["z"] == 4
    assert d["items"][-1]["name"] == "HeroSheet", "z=4 paints last"


def test_a_child_of_a_moved_parent_moves_with_it_in_the_render(client, game):
    client.post("/api/scene/node/add", json={
        "scene": "res://scenes/main.tscn", "node_type": "Node2D", "name": "Group"})
    client.post("/api/scene/wire", json={
        "scene": "res://scenes/main.tscn", "asset": SHEET, "parent": "Group"})
    client.post("/api/scene/node/property", json={
        "scene": "res://scenes/main.tscn", "node": "Group",
        "key": "position", "value": "Vector2(100, 100)"})
    client.post("/api/scene/node/property", json={
        "scene": "res://scenes/main.tscn", "node": "Group/HeroSheet",
        "key": "position", "value": "Vector2(10, 5)"})
    d = client.get("/api/scene/render",
                   params={"scene": "res://scenes/main.tscn"}).json()
    child = next(i for i in d["items"] if i["name"] == "HeroSheet")
    assert (child["x"], child["y"]) == (110, 105), "world = parent + local"


# ---------------------------------------------------------------------------
# The UI actually ships these
# ---------------------------------------------------------------------------
STATIC = Path(__file__).resolve().parents[1] / "bgate_ui" / "static"


def test_the_editor_and_graph_are_loaded_by_the_shell():
    """A module nobody <script src=>s is a module that silently does not exist."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for js in ("spriteedit.js", "atlas_graph.js"):
        assert f'src="/static/{js}"' in html, f"{js} is never loaded"
        assert (STATIC / js).is_file()
    assert 'id="atlas-graph"' in html


def test_the_editor_uses_the_shared_endpoints_not_invented_ones():
    js = (STATIC / "spriteedit.js").read_text(encoding="utf-8")
    for path in ("/api/sprite/open", "/api/sprite/save", "/api/sprite/rig",
                 "/api/sprite/spriteframes", "/api/sprite/autogrid"):
        assert path in js
    graph = (STATIC / "atlas_graph.js").read_text(encoding="utf-8")
    for path in ("/api/screenmap", "/api/scene/wire", "/api/scene/unwire",
                 "/api/scene/tree"):
        assert path in graph


def test_the_scene_builder_is_loaded_and_previews_before_it_writes():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'src="/static/scenebuild.js"' in html
    assert 'id="atlas-scene"' in html
    assert 'data-m="scene"' in html, "the mode needs a way in"
    js = (STATIC / "scenebuild.js").read_text(encoding="utf-8")
    for path in ("/api/scene/outline", "/api/scene/node/add",
                 "/api/scene/node/property", "/api/scene/node/swap",
                 "/api/scene/node/rename", "/api/scene/node/reparent",
                 "/api/scene/node/types", "/api/scene/unwire"):
        assert path in js
    # Every mutation is routed through the one function that shows the diff,
    # so no edit can be added later that skips the preview.
    assert js.count("confirmDiff(") >= 2
    assert "dry_run: true" in js
    # And the editors it hands off to are the ones that already own those files.
    assert "SpriteEdit.open" in js and "AudioLab.open" in js


def test_the_viewport_is_loaded_and_is_the_primary_surface():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'src="/static/sceneview.js"' in html
    build = (STATIC / "scenebuild.js").read_text(encoding="utf-8")
    assert "SceneView.mount" in build
    assert '"viewport"' in build, "the viewport is the default surface"
    view = (STATIC / "sceneview.js").read_text(encoding="utf-8")
    assert "/api/scene/render" in view
    assert "/api/scene/node/property" in view, "drags must write back"


def test_the_viewport_stages_edits_and_never_writes_on_a_drag():
    """This is a regression guard with a cost attached.

    The first version committed each drag on pointer-release. That produced
    twenty-two unrequested writes to a live scene in one sitting, because
    moving something to look at it is not a decision to change the game. A
    backup made those recoverable, not acceptable. Nothing may write without an
    explicit apply.
    """
    js = (STATIC / "sceneview.js").read_text(encoding="utf-8")

    # The pointer-release path stages; it must not call the write endpoint.
    done = js.split("const done = ()", 1)[1].split("cv.addEventListener", 1)[0]
    assert "stage(" in done
    assert "mutate(" not in done, "pointer-release must not write"

    # Exactly one function writes properties, and it is the confirmed one.
    assert js.count('"/api/scene/node/property"') == 1
    apply_fn = js.split("async function applyPending()", 1)[1].split(
        "async function discardPending", 1)[0]
    assert "confirm(" in apply_fn, "apply must ask before it writes"
    assert '"/api/scene/node/property"' in apply_fn

    # Leaving or switching with work outstanding asks first.
    for fn in ("function unmount()", "function setScene("):
        body = js.split(fn, 1)[1][:400]
        assert "hasPending()" in body and "confirm(" in body, f"{fn} must guard"

    # And a structural edit refuses rather than silently discarding them.
    build = (STATIC / "scenebuild.js").read_text(encoding="utf-8")
    step = build.split("async function step(", 1)[1].split("function setProp", 1)[0]
    assert "hasPending" in step


def test_no_canvas_latches_its_repaint_guard():
    """requestAnimationFrame never fires while a pane is not compositing. A
    plain `if (pending) return` guard then stays true forever and the canvas is
    frozen for the rest of the session — a dead panel, not a throttled one."""
    for name in ("sceneview.js", "spriteedit.js", "audiolab.js"):
        js = (STATIC / name).read_text(encoding="utf-8")
        assert "requestAnimationFrame(run)" in js, f"{name}: no coalesced paint"
        assert "setTimeout(run, 120)" in js, f"{name}: no escape hatch"
