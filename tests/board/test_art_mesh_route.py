import pytest
from fastapi.testclient import TestClient

from bgate_core.board import seats
from bgate_core.board import queue
from bgate_core.store import db, project, settings
from bgate_ui.agents import dispatch
from bgate_ui.app import app


@pytest.fixture()
def api_client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


def test_mesh_route_defaults_to_artifact_aware_routing(tmp_path):
    db.connect(tmp_path)
    assert settings.get(tmp_path, "art.mesh_route") == "smart"
    rule = seats.dispatch_rules(tmp_path, "art")
    assert "3D CREATION ROUTE — SMART" in rule
    assert "character_generate" in rule
    assert "blender_run" in rule
    assert "an apple" in rule
    assert "finished car" in rule


def test_api_route_forbids_hand_authored_replacement_geometry(tmp_path):
    db.connect(tmp_path)
    settings.set(tmp_path, "art.mesh_route", "api")
    rule = seats.dispatch_rules(tmp_path, "art")
    assert "3D CREATION ROUTE — API GENERATORS" in rule
    assert "Do not hand-author replacement geometry" in rule


def test_blender_route_keeps_image_apis_for_textures(tmp_path):
    db.connect(tmp_path)
    settings.set(tmp_path, "art.mesh_route", "blender")
    rule = seats.dispatch_rules(tmp_path, "art")
    assert "3D CREATION ROUTE — BLENDER" in rule
    assert "Do not call character_generate or blender_generate" in rule
    assert "texture maps" in rule


def test_mesh_route_is_only_added_to_the_art_seat(tmp_path):
    db.connect(tmp_path)
    settings.set(tmp_path, "art.mesh_route", "blender")
    assert "3D CREATION ROUTE" not in seats.dispatch_rules(tmp_path, "gameplay")


def test_settings_api_exposes_and_saves_the_route(api_client, root):
    response = api_client.get("/api/settings/art.mesh_route")
    assert response.status_code == 200
    field = response.json().get("data", response.json())
    assert field["choices"] == ["smart", "api", "blender"]

    saved = api_client.patch("/api/settings", json={"art.mesh_route": "api"})
    assert saved.status_code == 200
    assert settings.get(root, "art.mesh_route") == "api"


def test_saved_route_reaches_the_actual_dispatch_prompt(root):
    settings.set(root, "art.mesh_route", "blender")
    item = queue.add(root, "art", "Model a hero prop", "Build the final mesh")
    prompt = dispatch._prompt_for(str(root), item)
    assert "3D CREATION ROUTE — BLENDER" in prompt
    assert "Do not call character_generate or blender_generate" in prompt


def test_saved_route_reaches_the_three_d_seat_brief(root):
    project.set_dimension(root, "3d")
    settings.set(root, "art.mesh_route", "api")
    payload = seats.brief(root, "art")
    assert "3D CREATION ROUTE — API GENERATORS" in payload["workflow"]
