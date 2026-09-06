import pytest
from fastapi.testclient import TestClient

from bgate_core.board import seats
from bgate_core.store import db, settings
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


def test_settings_api_exposes_and_saves_the_route(api_client, root):
    response = api_client.get("/api/settings/art.mesh_route")
    assert response.status_code == 200
    field = response.json().get("data", response.json())
    assert field["choices"] == ["smart", "api", "blender"]

    saved = api_client.patch("/api/settings", json={"art.mesh_route": "api"})
    assert saved.status_code == 200
    assert settings.get(root, "art.mesh_route") == "api"
