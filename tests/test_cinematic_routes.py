"""The cutscene endpoints — the half of the seat a browser can reach.

Thin on purpose: bgate_core.cinematic is tested directly in test_cinematic.py,
so what is worth pinning here is only what this layer decides — that the route
module loads at all (a workspace whose API silently failed to import is the
failure routes/__init__.py's FAILURES list exists for), that the two
availabilities are reported separately, and that a plan's warnings survive the
trip rather than being swallowed into a 200 with no body.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core import cinematic
from bgate_ui.app import app


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


class TestTheModuleIsWired:
    def test_the_route_module_actually_loaded(self):
        """A dashboard can look perfectly healthy with half its API missing."""
        from bgate_ui import routes

        assert "cinematic" in routes.REGISTERED
        assert not [f for f in routes.FAILURES if f["module"] == "cinematic"]

    def test_the_seat_panel_ships_and_is_loaded_by_the_shell(self):
        """A seat in the roster with no panel file draws an empty workspace."""
        from pathlib import Path

        static = Path(__file__).resolve().parents[1] / "bgate_ui" / "static"
        assert (static / "seats" / "cinematic.js").is_file()
        index = (static / "index.html").read_text(encoding="utf-8")
        assert "/static/seats/cinematic.js" in index


class TestOptionsAndStyles:
    def test_the_two_availabilities_are_reported_separately(self, client):
        """A key BUYS a shot; an ffmpeg with libtheora makes a bought shot
        playable. One disabled button with no reason sends a user to spend an
        afternoon on the wrong problem."""
        got = data(client.get("/api/cinematic/options"))
        assert "provider_available" in got
        assert "encoder" in got and "ok" in got["encoder"]

    def test_the_style_table_is_served_rather_than_retyped(self, client):
        got = data(client.get("/api/cinematic/styles"))
        assert set(got["styles"]) == set(cinematic.STYLES)
        assert got["fallback"] == cinematic.STYLE_FALLBACK

    def test_model_limits_are_served_in_intent_terms(self, client):
        """A form that retypes '4 to 15 seconds' lies the day kie changes it,
        and the field name behind it differs per model anyway."""
        got = data(client.get("/api/cinematic/options"))
        assert got["models"]["seedance-2"]["options"]["seconds"] == [4, 15]


class TestPlanning:
    def test_a_plan_round_trips_with_its_style_applied(self, client):
        got = data(client.post("/api/cinematic/plan", json={
            "name": "Prologue", "style": "noir",
            "shots": [{"action": "rain on the dock", "camera": "wide",
                       "duration": 6}]}))
        assert got["name"] == "prologue"
        assert got["style_resolved"]["label"] == "Film noir"
        assert "film noir" in got["shots"][0]["prompt"]

    def test_warnings_survive_the_trip(self, client):
        """An unanchored sequence is a choice a human may make; making it
        uninformed is what the warning prevents, and a warning swallowed by the
        transport is a warning that does not exist."""
        got = data(client.post("/api/cinematic/plan", json={
            "name": "drifty",
            "shots": [{"action": "someone walks in", "duration": 5}]}))
        assert any("NOT ONE SHOT IS ANCHORED" in w for w in got["warnings"])

    def test_a_plan_with_no_shots_is_a_400_not_a_500(self, client):
        assert client.post("/api/cinematic/plan",
                           json={"name": "x", "shots": []}).status_code == 400

    def test_an_unregistered_model_is_a_400_naming_the_real_ones(self, client):
        r = client.post("/api/cinematic/plan", json={
            "name": "x", "model": "veo-9",
            "shots": [{"action": "a", "duration": 5}]})
        assert r.status_code == 400
        assert "seedance-2" in r.text

    def test_sequences_lists_then_opens(self, client):
        client.post("/api/cinematic/plan", json={
            "name": "one", "style": "anime",
            "shots": [{"action": "a", "duration": 5}]})
        listing = data(client.get("/api/cinematic/sequences"))
        assert [s["name"] for s in listing["sequences"]] == ["one"]
        assert listing["sequences"][0]["style_label"] == "Anime / cel"
        one = data(client.get("/api/cinematic/sequences?name=one"))
        assert len(one["sequence"]["shots"]) == 1


class TestGuards:
    def test_generate_needs_a_shot_number(self, client):
        assert client.post("/api/cinematic/generate",
                           json={"name": "x"}).status_code == 400

    def test_keep_needs_an_artifact_id(self, client):
        assert client.post("/api/cinematic/keep", json={}).status_code == 400

    def test_recover_needs_a_sequence(self, client):
        assert client.post("/api/cinematic/recover",
                           json={"idx": 1}).status_code == 400


class TestPostProductionEndpoints:
    def test_transitions_are_served_with_their_costs(self, client):
        got = data(client.get("/api/cinematic/transitions"))
        assert got["default"] == "cut"
        assert set(got["transitions"]) >= {"cut", "fade", "dissolve"}
        assert all(t["note"] for t in got["transitions"].values())

    def test_a_plan_accepts_the_sound_and_transition_fields(self, client):
        got = data(client.post("/api/cinematic/plan", json={
            "name": "scored", "audio_track": "game/assets/audio/music/x.mp3",
            "audio_gain_db": -6, "fade_in": 0.5, "fade_out": 1.0,
            "shots": [{"action": "a", "duration": 5},
                      {"action": "b", "duration": 5,
                       "transition": "dissolve", "transition_s": 1.0,
                       "dialogue": "a line"}]}))
        assert got["audio_track"].endswith("x.mp3")
        assert got["fade_in"] == 0.5
        assert [s["transition"] for s in got["shots"]] == ["cut", "dissolve"]

    def test_an_impossible_transition_is_a_400(self, client):
        r = client.post("/api/cinematic/plan", json={
            "name": "bad", "shots": [{"action": "a", "duration": 5},
                                     {"action": "b", "duration": 5,
                                      "transition": "fade",
                                      "transition_s": 5}]})
        assert r.status_code == 400
        assert "as long as the shot" in r.text

    def test_deliver_needs_a_sequence(self, client):
        assert client.post("/api/cinematic/deliver",
                           json={}).status_code == 400

    def test_delivering_an_unassembled_sequence_is_a_400_that_explains(
            self, client):
        client.post("/api/cinematic/plan", json={
            "name": "raw", "shots": [{"action": "a", "duration": 5}]})
        r = client.post("/api/cinematic/deliver", json={"name": "raw"})
        assert r.status_code == 400 and "assembled" in r.text

    def test_continuity_needs_a_sequence(self, client):
        assert client.post("/api/cinematic/continuity",
                           json={}).status_code == 400


class TestMachineWideKeys:
    """A key set with `bgate key set <provider> --global` and nowhere else was
    invisible to every panel that asks an adapter whether it is configured.

    The adapters call envfile.load_project_env, which reads a PROJECT's .env
    only; the machine-wide store is loaded by envfile.load_env. The MCP server's
    _root() already did that and the dashboard's did not, so `bgate key` listed
    a key as in force while the panel said no provider was configured — the most
    expensive kind of wrong this product can be about credentials.
    """

    def test_a_machine_wide_key_reaches_the_panels(self, root, monkeypatch,
                                                   tmp_path):
        from bgate_core import envfile

        monkeypatch.setenv("BGATE_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("KIE_API_KEY", raising=False)
        gdir = envfile.global_dir()
        gdir.mkdir(parents=True, exist_ok=True)
        (gdir / ".env").write_text("KIE_API_KEY=machine-wide\n",
                                   encoding="utf-8")
        monkeypatch.setenv("BGATE_ROOT", str(root))

        client = TestClient(app)
        got = data(client.get("/api/cinematic/options"))
        assert got["provider_available"] is True

    def test_the_project_still_beats_the_machine_wide_store(self, root,
                                                            monkeypatch,
                                                            tmp_path):
        """Precedence is shell > project > machine-wide, and a panel that
        reported the wrong layer is how a stale key survives a debugging
        session."""
        from bgate_core import envfile

        monkeypatch.setenv("BGATE_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("KIE_API_KEY", raising=False)
        gdir = envfile.global_dir()
        gdir.mkdir(parents=True, exist_ok=True)
        (gdir / ".env").write_text("KIE_API_KEY=machine-wide\n",
                                   encoding="utf-8")
        (root / ".env").write_text("KIE_API_KEY=project-key\n", encoding="utf-8")
        monkeypatch.setenv("BGATE_ROOT", str(root))

        TestClient(app).get("/api/cinematic/options")
        import os
        assert os.environ.get("KIE_API_KEY") == "project-key"
