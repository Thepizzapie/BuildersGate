"""`GET /api/studio-index`: the Shared screen's one read.

Pins three things: no studio is an ordinary answer, modules come from the
version folders on disk, and a game's kit ledger is what says it pins a
version.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import studio
from bgate_ui.app import app


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def studio_root(tmp_path, monkeypatch):
    monkeypatch.setenv("BGATE_HOME", str(tmp_path / "home"))
    root = tmp_path / "studio"
    monkeypatch.setenv(studio.ENV, str(root))
    studio.init(root)
    return root


def _module(root, engine, name, version, title):
    d = root / "shared" / "modules" / engine / name / version
    d.mkdir(parents=True)
    (d / "kit.json").write_text(json.dumps({"name": name, "title": title,
                                            "summary": "s"}), encoding="utf-8")


def test_no_studio_is_not_an_error(client, tmp_path, monkeypatch):
    monkeypatch.setenv("BGATE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(studio.ENV, raising=False)
    got = client.get("/api/studio-index").json()["data"]
    assert got["set"] is False and got["modules"] == [] and got["assets"] is None


def test_lists_modules_pins_and_assets(client, studio_root, monkeypatch):
    _module(studio_root, "godot", "health", "1.0.0", "Health")
    _module(studio_root, "godot", "health", "1.1.0", "Health")
    _module(studio_root, "web", "relay", "0.1.0", "Relay netcode")
    (studio_root / "shared" / "modules" / "web" / "empty" / "0.1.0").mkdir(parents=True)
    game = studio_root / "games" / "ember"
    (game / ".bgate").mkdir(parents=True)
    (game / ".bgate" / "kits.json").write_text(
        json.dumps({"health": {"version": "1.0.0"}, "inventory": {}}), encoding="utf-8")
    monkeypatch.setattr(studio, "status", lambda: {
        "set": True, "ok": True, "root": str(studio_root), "source": "env",
        "games": [{"name": "ember", "path": str(game), "exists": True}],
        "modules": 2})

    got = client.get("/api/studio-index").json()["data"]
    mods = {m["name"]: m for m in got["modules"]}
    assert set(mods) == {"health", "relay"}
    assert mods["health"]["versions"] == ["1.0.0", "1.1.0"]
    assert mods["health"]["pinned_by"] == [{"game": "ember", "version": "1.0.0"}]
    assert mods["relay"]["engine"] == "web" and mods["relay"]["pinned_by"] == []
    assert got["assets"]["home"] == str((studio_root / "shared" / "assets").resolve())
    assert got["assets"]["count"] == 0
