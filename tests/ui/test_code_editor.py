"""The code editor's two surfaces: what a scene reaches, and writing to it.

Everything the dashboard did to a game project before this was either a read or
a structured mutation with its own parser in front of it. This endpoint takes a
string from a browser and puts it in a file the engine loads, which makes the
interesting tests the refusals — the save that would have discarded an agent's
work, the save through a held lock, the path that was never in the project.

The other half is /api/scene/files, whose whole value is the SECOND hop: a
scene file lists the script it attaches and says nothing about what that script
preloads, and those resources are exactly the ones you go looking for next.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import assets
from bgate_ui.app import app


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    with TestClient(app) as c:
        yield c


PLAYER_GD = """extends CharacterBody2D

const HURT = preload("res://assets/sfx/hurt.ogg")
@onready var sprite := $Sprite2D

func _physics_process(delta: float) -> void:
\tvar frames = load("res://assets/sprites/hero_frames.tres")
\tvelocity.y += delta
"""

MAIN_TSCN = """[gd_scene load_steps=3 format=3]

[ext_resource type="Script" path="res://scripts/player.gd" id="1_p"]
[ext_resource type="Texture2D" path="res://assets/sprites/hero.png" id="2_t"]

[node name="Main" type="Node2D"]

[node name="Player" type="CharacterBody2D" parent="."]
script = ExtResource("1_p")
"""


def _write(path: Path, text: str) -> None:
    """Godot writes \\n on every platform. write_text() without `newline` gives
    CRLF on Windows, which would make these fixtures disagree with the engine
    and quietly turn the line-ending test into a test of itself."""
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def game(root):
    """A Godot project at <root>/game — where /api/godot/* looks by default."""
    g = root / "game"
    (g / "scripts").mkdir(parents=True)
    (g / "scenes").mkdir(parents=True)
    (g / "assets" / "sprites").mkdir(parents=True)
    (g / "assets" / "sfx").mkdir(parents=True)
    _write(g / "project.godot",
           'config_version=5\n\n[application]\n\nconfig/name="Test"\n')
    _write(g / "scripts" / "player.gd", PLAYER_GD)
    _write(g / "scenes" / "main.tscn", MAIN_TSCN)
    (g / "assets" / "sprites" / "hero.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    _write(g / "assets" / "sprites" / "hero_frames.tres",
           '[gd_resource type="SpriteFrames" format=3]\n')
    (g / "assets" / "sfx" / "hurt.ogg").write_bytes(b"OggS")
    return g


def _read(client, rel):
    r = client.get("/api/godot/file", params={"rel": rel})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Which directory res:// means
# ---------------------------------------------------------------------------
def test_the_default_project_is_found_where_adopt_leaves_it(client, root):
    """`bgate adopt` leaves project.godot at the ROOT. This defaulted to
    <root>/game, so every endpoint here 404'd on an adopted project."""
    _write(root / "project.godot", 'config_version=5\n')
    (root / "scripts").mkdir()
    _write(root / "scripts" / "thing.gd", "extends Node\n")
    assert _read(client, "scripts/thing.gd")["text"] == "extends Node\n"


def test_a_scaffolded_project_still_resolves_under_game(client, game):
    assert _read(client, "scripts/player.gd")["text"] == PLAYER_GD


def test_scene_files_and_the_editor_agree_on_the_directory(client, game):
    """The two modules resolved res:// independently and could disagree; an
    edit_rel from one had to open through the other."""
    d = client.get("/api/scene/files",
                   params={"scene": "res://scenes/main.tscn"}).json()
    for f in d["files"]:
        if f["editable"] and f["exists"]:
            assert client.get("/api/godot/file",
                              params={"rel": f["edit_rel"]}).status_code == 200


# ---------------------------------------------------------------------------
# What the scene reaches
# ---------------------------------------------------------------------------
def test_scene_files_lists_what_the_scene_declares(client, game):
    d = client.get("/api/scene/files",
                   params={"scene": "res://scenes/main.tscn"}).json()
    by_res = {f["res"]: f for f in d["files"]}
    assert "res://scenes/main.tscn" in by_res, "the scene must list itself"
    assert by_res["res://scripts/player.gd"]["via"] == "scene"
    assert by_res["res://assets/sprites/hero.png"]["via"] == "scene"


def test_scene_files_follows_one_hop_through_the_script(client, game):
    """The whole reason this endpoint exists rather than reading the .tscn."""
    d = client.get("/api/scene/files",
                   params={"scene": "res://scenes/main.tscn"}).json()
    by_res = {f["res"]: f for f in d["files"]}
    # Neither of these appears anywhere in main.tscn — only in player.gd.
    assert by_res["res://assets/sfx/hurt.ogg"]["via"] == "script:res://scripts/player.gd"
    assert by_res["res://assets/sprites/hero_frames.tres"]["via"] \
        == "script:res://scripts/player.gd"


def test_scene_files_marks_what_the_editor_can_open(client, game):
    d = client.get("/api/scene/files",
                   params={"scene": "res://scenes/main.tscn"}).json()
    by_res = {f["res"]: f for f in d["files"]}
    assert by_res["res://scripts/player.gd"]["editable"] is True
    # Offering a code pane on a PNG is an offer to corrupt it.
    assert by_res["res://assets/sprites/hero.png"]["editable"] is False


def test_scene_files_edit_rel_is_what_the_write_endpoint_wants(client, game):
    """The two APIs address from different roots; the payload bridges them."""
    d = client.get("/api/scene/files",
                   params={"scene": "res://scenes/main.tscn"}).json()
    script = next(f for f in d["files"] if f["res"] == "res://scripts/player.gd")
    assert script["rel"] == "game/scripts/player.gd"      # from the bg root
    assert script["edit_rel"] == "scripts/player.gd"      # from the godot dir
    assert _read(client, script["edit_rel"])["text"] == PLAYER_GD


def test_scene_files_reports_a_reference_with_nothing_behind_it(client, game):
    (game / "assets" / "sfx" / "hurt.ogg").unlink()
    d = client.get("/api/scene/files",
                   params={"scene": "res://scenes/main.tscn"}).json()
    assert "res://assets/sfx/hurt.ogg" in d["missing"]


# ---------------------------------------------------------------------------
# Reading, now that a read has to support a write
# ---------------------------------------------------------------------------
def test_read_hands_back_a_hash_to_save_against(client, game):
    d = _read(client, "scripts/player.gd")
    assert d["sha"] and d["writable"] is True and d["lock"] is None


def test_read_refuses_to_call_a_generated_file_writable(client, game):
    (game / "assets" / "sprites" / "hero.png.import").write_text(
        "[remap]\n", encoding="utf-8")
    d = _read(client, "assets/sprites/hero.png.import")
    assert d["writable"] is False, ".import is regenerated by the engine"


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def test_save_writes_and_keeps_the_previous_bytes(client, game, root):
    before = _read(client, "scripts/player.gd")
    new = PLAYER_GD.replace("velocity.y += delta", "velocity.y += delta * 2.0")
    r = client.post("/api/godot/file", json={
        "rel": "scripts/player.gd", "text": new, "base_sha": before["sha"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["written"] is True
    assert (game / "scripts" / "player.gd").read_text(encoding="utf-8") == new
    # "undo" is not something a web UI has over a file the engine also owns.
    assert (root / body["backup"]).read_text(encoding="utf-8") == PLAYER_GD


def test_save_normalises_the_browsers_line_endings(client, game):
    """A textarea hands back \\r\\n on a platform whose engine writes \\n. Left
    alone, the first save rewrites every line and buries the one-line change."""
    before = _read(client, "scripts/player.gd")
    edited = PLAYER_GD.replace("velocity.y += delta", "velocity.y += delta * 2.0")
    r = client.post("/api/godot/file", json={
        "rel": "scripts/player.gd", "text": edited.replace("\n", "\r\n"),
        "base_sha": before["sha"]})
    assert r.status_code == 200 and r.json()["written"] is True
    on_disk = (game / "scripts" / "player.gd").read_bytes()
    assert b"\r\n" not in on_disk
    assert on_disk.decode() == edited


def test_save_leaves_a_files_existing_line_endings_alone(client, game):
    """Normalising on every save would rewrite a CRLF file nobody touched."""
    (game / "scripts" / "player.gd").write_bytes(PLAYER_GD.replace("\n", "\r\n").encode())
    body = client.post("/api/godot/file", json={
        "rel": "scripts/player.gd", "text": PLAYER_GD}).json()
    assert body["written"] is False, "same content is not a reason to rewrite"
    assert b"\r\n" in (game / "scripts" / "player.gd").read_bytes()


def test_save_of_identical_text_is_not_a_write(client, game, root):
    before = _read(client, "scripts/player.gd")
    body = client.post("/api/godot/file", json={
        "rel": "scripts/player.gd", "text": PLAYER_GD,
        "base_sha": before["sha"]}).json()
    assert body["written"] is False and body["unchanged"] is True
    assert not (root / ".bgate_out" / "edits").exists(), "no backup for a no-op"


def test_save_refuses_when_the_file_moved_under_the_tab(client, game):
    """An agent writing the same script is not a merge this editor performs."""
    before = _read(client, "scripts/player.gd")
    (game / "scripts" / "player.gd").write_text(
        PLAYER_GD + "\nfunc _ready() -> void:\n\tpass\n", encoding="utf-8")
    r = client.post("/api/godot/file", json={
        "rel": "scripts/player.gd", "text": "extends Node\n",
        "base_sha": before["sha"]})
    assert r.status_code == 409
    # The agent's line is still there.
    assert "_ready" in (game / "scripts" / "player.gd").read_text(encoding="utf-8")


def test_save_without_a_base_sha_is_allowed(client, game):
    """Scripted callers that never opened a tab have nothing to compare."""
    r = client.post("/api/godot/file", json={
        "rel": "scripts/player.gd", "text": "extends Node\n"})
    assert r.status_code == 200 and r.json()["written"] is True


def test_save_refuses_a_held_lock_until_told_twice(client, game, root):
    assets.track(root, "game/scripts/player.gd")
    assets.lock(root, "game/scripts/player.gd", "gameplay", owner="item-7")
    payload = {"rel": "scripts/player.gd", "text": "extends Node\n"}

    blocked = client.post("/api/godot/file", json=payload)
    assert blocked.status_code == 423
    assert "gameplay" in json.dumps(blocked.json())
    assert (game / "scripts" / "player.gd").read_text(encoding="utf-8") == PLAYER_GD

    forced = client.post("/api/godot/file", json={**payload, "force": True})
    assert forced.status_code == 200 and forced.json()["written"] is True


def test_read_names_the_seat_holding_the_file(client, game, root):
    assets.track(root, "game/scripts/player.gd")
    assets.lock(root, "game/scripts/player.gd", "gameplay", owner="item-7")
    assert _read(client, "scripts/player.gd")["lock"]["seat"] == "gameplay"


# ---------------------------------------------------------------------------
# Refusals that keep the endpoint from being a general file writer
# ---------------------------------------------------------------------------
def test_backup_lands_under_bgate_out_even_for_an_absolute_rel(client, game, root):
    """An absolute `rel` still inside the project passes containment; joining
    that raw string onto the backup dir would put the copy somewhere else."""
    r = client.post("/api/godot/file", json={
        "rel": str(game / "scripts" / "player.gd"), "text": "extends Node\n"})
    assert r.status_code == 200, r.text
    backup = root / r.json()["backup"]
    assert backup.is_file()
    assert (root / ".bgate_out") in backup.parents
    assert backup.read_text(encoding="utf-8") == PLAYER_GD


def test_save_refuses_a_path_that_escapes_the_project(client, game, root):
    outside = root / "secrets.gd"
    outside.write_text("keep me\n", encoding="utf-8")
    r = client.post("/api/godot/file", json={
        "rel": "../secrets.gd", "text": "clobbered\n"})
    assert r.status_code == 403
    assert outside.read_text(encoding="utf-8") == "keep me\n"


def test_save_refuses_a_suffix_the_engine_regenerates(client, game):
    imp = game / "assets" / "sprites" / "hero.png.import"
    imp.write_text("[remap]\n", encoding="utf-8")
    r = client.post("/api/godot/file", json={
        "rel": "assets/sprites/hero.png.import", "text": "[remap]\nx=1\n"})
    assert r.status_code == 415
    assert imp.read_text(encoding="utf-8") == "[remap]\n"


def test_save_refuses_a_binary_suffix_outright(client, game):
    r = client.post("/api/godot/file", json={
        "rel": "assets/sprites/hero.png", "text": "not a png"})
    assert r.status_code == 415


def test_save_will_not_create_a_file(client, game):
    """An empty .gd attached to nothing is not something this editor offers."""
    r = client.post("/api/godot/file", json={
        "rel": "scripts/brand_new.gd", "text": "extends Node\n"})
    assert r.status_code == 404
    assert not (game / "scripts" / "brand_new.gd").exists()


def test_save_refuses_a_body_that_is_not_text(client, game):
    r = client.post("/api/godot/file", json={
        "rel": "scripts/player.gd", "text": {"nice": "try"}})
    assert r.status_code == 400


def test_save_refuses_more_than_the_cap(client, game):
    from bgate_ui.routes import godot_ws
    r = client.post("/api/godot/file", json={
        "rel": "scripts/player.gd", "text": "#" * (godot_ws._MAX_WRITE + 1)})
    assert r.status_code == 413
