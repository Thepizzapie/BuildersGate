"""A Unity project's WebGL build, as /api/play/status reports it.

The bug: the "a WebGL build in export/web counts as built" rule lived only in
bgate_site's collect, so on ff-b67-unity /play/ served the game with a 200 while
the phone's Play panel (webbuild.status) answered "no game project" and offered
a BUILD IT button that could do nothing for Unity.
"""
from __future__ import annotations

import pytest

from bgate_core.store import project
from bgate_ui import webbuild


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A root with no Godot game and no vite game: only the engine record decides."""
    monkeypatch.setattr(webbuild, "_game", lambda r: None)
    monkeypatch.setattr(webbuild, "_web_game", lambda r: None)
    return tmp_path


def _engine(monkeypatch, name: str) -> None:
    monkeypatch.setattr(project, "engine_of", lambda r: name)


def _webgl(root) -> None:
    out = root / "export" / "web"
    out.mkdir(parents=True)
    (out / "index.html").write_text("<!doctype html>", encoding="utf-8")


def test_a_unity_webgl_build_is_built_and_never_stale(root, monkeypatch):
    _engine(monkeypatch, "unity")
    _webgl(root)
    got = webbuild.status(root)
    assert got["built"] is True
    assert got["stale"] is False
    assert got["reason"] == ""


def test_a_unity_project_without_a_build_names_the_fix(root, monkeypatch):
    _engine(monkeypatch, "unity")
    got = webbuild.status(root)
    assert got["built"] is False
    assert got["reason"] == webbuild.UNITY_NO_BUILD


def test_control_the_same_build_under_another_engine_is_still_no_project(root, monkeypatch):
    # Without the Unity branch this is what every Unity project read as.
    _engine(monkeypatch, "godot")
    _webgl(root)
    assert webbuild.status(root)["reason"] == "no game project"


def test_rebuild_refuses_a_unity_project_with_the_way_forward(root, monkeypatch):
    _engine(monkeypatch, "unity")
    got = webbuild.rebuild(root)
    assert got["ok"] is False
    assert got["error"] == webbuild.UNITY_REBUILD_REFUSED


def test_control_rebuild_without_any_project_still_says_so(root, monkeypatch):
    _engine(monkeypatch, "godot")
    assert webbuild.rebuild(root)["error"] == "no game project at this root"


def test_the_arcade_card_and_the_play_panel_agree(root, monkeypatch):
    from bgate_site import collect
    _engine(monkeypatch, "unity")
    _webgl(root)
    facts = collect._build_facts(root, "unity")
    assert facts["built"] is True and facts["stale"] is False
    assert facts["built"] == webbuild.status(root)["built"]
