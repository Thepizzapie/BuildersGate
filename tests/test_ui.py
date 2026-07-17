"""Dashboard backend — the read-only contract and the path-escape guard."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core import activity, assets, bible, lore, seats
from bgate_ui.app import app


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


class TestState:
    def test_state_shape(self, client, root):
        bible.add(root, "pillar", "Tension over spectacle")
        lore.add_entity(root, "faction", "The Ashen Order", status="canon")
        assets.lock(root, "game/assets/shard.blend", "art")
        seats.post_note(root, "art", "starting the shard", topic="shard")

        got = client.get("/api/state").json()
        assert got["project"]["name"] == "Test Game"
        assert len(got["seats"]) == 7

        art = next(s for s in got["seats"] if s["role"] == "art")
        assert art["locks"][0]["path"] == "game/assets/shard.blend"
        assert art["last_activity"]["kind"] in ("lock", "note")
        assert got["bible"]["pillars"][0]["title"] == "Tension over spectacle"
        assert got["lore"]["canon"][0]["name"] == "The Ashen Order"
        assert got["verify"]["ok"] is True

    def test_drift_surfaces_in_state(self, client, root):
        blend = root / "b.blend"
        blend.write_bytes(b"v1")
        assets.track(root, blend)
        blend.write_bytes(b"stomped")
        got = client.get("/api/state").json()
        assert got["verify"]["ok"] is False
        assert got["verify"]["modified"][0]["path"] == "b.blend"


class TestActivity:
    def test_incremental_polling(self, client, root):
        activity.log(root, "lock", "locked a", seat="art")
        first = client.get("/api/activity").json()["events"]
        assert first[0]["summary"] == "locked a"

        activity.log(root, "release", "released a", seat="art")
        newer = client.get(f"/api/activity?after_id={first[0]['id']}").json()["events"]
        assert len(newer) == 1
        assert newer[0]["kind"] == "release"


class TestPreview:
    def test_serves_project_images(self, client, root):
        png = root / ".bgate" / "previews" / "x.png"
        png.parent.mkdir(parents=True, exist_ok=True)
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        got = client.get("/api/preview?rel=.bgate/previews/x.png")
        assert got.status_code == 200
        assert got.content.startswith(b"\x89PNG")

    def test_path_escape_is_refused(self, client):
        assert client.get("/api/preview?rel=../../secrets.png").status_code == 403

    def test_non_image_refused(self, client, root):
        (root / "game.db.png.py").write_text("nope")
        assert client.get("/api/preview?rel=game.db").status_code in (403, 404, 415)

    def test_missing_image_404s(self, client):
        assert client.get("/api/preview?rel=.bgate/previews/ghost.png").status_code == 404


class TestIndex:
    def test_serves_the_page(self, client):
        got = client.get("/")
        assert got.status_code == 200
        assert "Builders Gate" in got.text
