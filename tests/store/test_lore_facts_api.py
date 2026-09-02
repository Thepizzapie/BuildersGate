"""`GET /api/lore/facts` — every canon fact in one request.

WHY THE ROUTE EXISTS. The narrative room works AGAINST canon: its most important
panel is the LOCKED facts, because those are the ones a proposal may not
contradict at all. `lore.all_facts` has always returned exactly that and was
never exposed, so the only way to assemble the panel was one `/api/lore/{ref}`
per entity — 28 loopback requests on a real project, growing with the fiction.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import db, project
from bgate_core.design import lore
from bgate_ui.app import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(tmp_path))
    monkeypatch.setenv("BGATE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    project.init(tmp_path, "Lore Facts")
    yield TestClient(app)
    db.close_all()


def data(response) -> dict:
    body = response.json()
    assert body["ok"] is True, body
    return body["data"]


def _seed(root):
    ent = lore.add_entity(root, "character", "Accounting Wizard")
    lore.add_fact(root, ent["id"], "Signs every requisition in triplicate.", locked=True)
    lore.add_fact(root, ent["id"], "Drinks the third-floor coffee.")
    return ent


class TestTheIndex:
    def test_an_empty_project_answers_an_empty_list(self, client):
        assert data(client.get("/api/lore/facts"))["facts"] == []

    def test_every_fact_carries_its_entity(self, client, tmp_path):
        _seed(tmp_path)
        facts = data(client.get("/api/lore/facts"))["facts"]
        assert len(facts) == 2
        # The slug is the point: a fact without its entity cannot be shown in a
        # panel that is sorted by which entity it constrains.
        assert all(f["slug"] == "accounting-wizard" for f in facts)

    def test_locked_can_be_asked_for_on_its_own(self, client, tmp_path):
        _seed(tmp_path)
        locked = data(client.get("/api/lore/facts?locked=true"))["facts"]
        assert [f["locked"] for f in locked] == [1]
        loose = data(client.get("/api/lore/facts?locked=false"))["facts"]
        assert len(loose) == 1 and not loose[0]["locked"]


class TestItDoesNotCollideWithTheWildcard:
    def test_facts_is_not_swallowed_as_an_entity_ref(self, client, tmp_path):
        """`/api/lore/{ref}` is declared AFTER this route on purpose.

        FastAPI matches in declaration order, so the wildcard would otherwise
        take "facts" as an entity reference and answer 404 for something nobody
        asked for — the kind of bug that looks like the route was never added.
        """
        _seed(tmp_path)
        assert "facts" in data(client.get("/api/lore/facts"))
        # and the per-entity route still works
        one = data(client.get("/api/lore/accounting-wizard"))
        assert one["entity"]["slug"] == "accounting-wizard"
