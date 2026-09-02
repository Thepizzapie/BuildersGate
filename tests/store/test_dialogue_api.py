"""The dialogue trees, over HTTP.

These endpoints exist because the narrative seat page draws a tree as a GRAPH —
the three things `dialogue.validate` refuses a write for are shapes, not lines,
and a screen that cannot read the trees can only say so. They are READ ONLY on
purpose: `write` is where canon_check, the lane rules and the refusal live, and
a second quieter path to the same files would have none of that around it.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import db, project
from bgate_core.design import dialogue
from bgate_ui.app import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(tmp_path))
    monkeypatch.setenv("BGATE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    project.init(tmp_path, "Dialogue Test")
    yield TestClient(app)
    db.close_all()


def _write(root, name, nodes, start="a"):
    """Straight to disk — `dialogue.write` runs canon_check, which wants a
    project with lore in it, and none of these tests are about that."""
    base = dialogue.dialogue_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    (base / f"{name}{dialogue.SUFFIX}").write_text(
        json.dumps({"title": name, "start": start, "nodes": nodes}),
        encoding="utf-8")


def data(response) -> dict:
    body = response.json()
    assert body["ok"] is True, body
    return body["data"]


class TestIndex:
    def test_an_empty_lane_is_an_empty_list_not_an_error(self, client):
        assert data(client.get("/api/dialogue"))["dialogues"] == []

    def test_it_lists_what_is_there_with_the_verdict_already_on_it(
            self, client, tmp_path):
        _write(tmp_path, "greeting",
               [{"id": "a", "text": "hello", "choices": [{"text": "bye", "goto": "b"}]},
                {"id": "b", "text": "bye", "end": True}])
        rows = data(client.get("/api/dialogue"))["dialogues"]
        assert [r["name"] for r in rows] == ["greeting"]
        # The listing is where a broken tree is cheapest to notice, so the
        # verdict rides along with the name.
        assert rows[0]["ok"] is True
        assert rows[0]["nodes"] == 2


class TestRead:
    def test_a_tree_comes_back_whole(self, client, tmp_path):
        _write(tmp_path, "greeting", [{"id": "a", "text": "hello", "end": True}])
        got = data(client.get("/api/dialogue/greeting"))
        assert got["start"] == "a"
        assert [n["id"] for n in got["nodes"]] == ["a"]

    def test_a_name_that_is_not_there_says_where_to_look(self, client):
        body = client.get("/api/dialogue/nope").json()
        assert body["ok"] is False
        assert body["error"]["code"] == "not_found"
        # Nearly always a typo or a moved file, and the listing answers both.
        assert "dialogue_list" in body["error"]["message"]

    def test_unreadable_is_not_missing(self, client, tmp_path):
        base = dialogue.dialogue_dir(tmp_path)
        base.mkdir(parents=True, exist_ok=True)
        (base / f"broken{dialogue.SUFFIX}").write_text("{not json", encoding="utf-8")
        body = client.get("/api/dialogue/broken").json()
        # The file is THERE and its JSON is broken — a different fix from a
        # missing name, so a different status.
        assert body["error"]["code"] == "bad_request"


class TestValidate:
    def test_it_prints_the_refusal_write_would_have(self, client, tmp_path):
        # A choice pointing nowhere: one of the three failures the graph exists
        # to make visible.
        _write(tmp_path, "dangling",
               [{"id": "a", "text": "hi", "choices": [{"text": "go", "goto": "ghost"}]}])
        got = data(client.get("/api/dialogue/dangling/validate"))
        assert got["ok"] is False
        # The sentence names the node, which is the whole point of drawing it.
        assert "ghost" in got["problem"]

    def test_a_clean_tree_validates(self, client, tmp_path):
        _write(tmp_path, "fine",
               [{"id": "a", "text": "hi", "choices": [{"text": "on", "goto": "b"}]},
                {"id": "b", "text": "done", "end": True}])
        assert data(client.get("/api/dialogue/fine/validate"))["ok"] is True
