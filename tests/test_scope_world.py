"""The producer's two jobs: authoring the bible and holding the cut line.

Both were MCP-only, so the tests that matter here are the ones that prove the
write paths exist AND that the gates refuse — a scope check that always says yes
and a canon gate you can write straight past are worse than nothing, because the
dashboard then reports a line that is not being held.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core import bible, lore, queue, scope
from bgate_ui import api
from bgate_ui.app import app


@pytest.fixture()
def client(root, monkeypatch):
    """The guard reads BGATE_NO_AUTH once, at import, so conftest's opt-out does
    not reach it here — present the project's real token instead of relying on
    the guard being off."""
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app, headers={"x-bgate-token": api.ensure_token(root)})


@pytest.fixture()
def scoped(root):
    """Two tiers above the line, one below it."""
    return {
        "core": bible.add(root, "scope_tier", "Core loop", rank=1)["id"],
        "enemy": bible.add(root, "scope_tier", "One enemy type", rank=2)["id"],
        "line": bible.add(root, "cut_line", "--- ship it ---", rank=3)["id"],
        "mp": bible.add(root, "scope_tier", "Multiplayer", rank=4)["id"],
    }


def data(response) -> dict:
    body = response.json()
    assert body["ok"] is True, body
    return body["data"]


def err(response) -> dict:
    body = response.json()
    assert body["ok"] is False, body
    return body["error"]


# ---------------------------------------------------------------------------
# Bible CRUD over HTTP
# ---------------------------------------------------------------------------

class TestBibleWrites:
    def test_create_read_update_delete(self, client):
        made = data(client.post("/api/bible", json={
            "kind": "pillar", "title": "Tension over spectacle", "body": "quiet"}))
        assert made["kind"] == "pillar"

        view = data(client.get("/api/bible"))
        assert view["pillars"][0]["title"] == "Tension over spectacle"

        edited = data(client.patch(f"/api/bible/{made['id']}",
                                   json={"title": "Tension, always"}))
        assert edited["title"] == "Tension, always"
        assert edited["body"] == "quiet"  # untouched fields survive

        gone = data(client.delete(f"/api/bible/{made['id']}"))
        assert gone["deleted"]["id"] == made["id"]
        assert data(client.get("/api/bible"))["pillars"] == []

    def test_bad_kind_is_400(self, client):
        assert err(client.post("/api/bible", json={"kind": "vibes", "title": "x"})
                   )["code"] == "bad_request"

    def test_missing_section_is_404(self, client):
        assert err(client.patch("/api/bible/999", json={"title": "x"})
                   )["code"] == "not_found"

    def test_an_agent_cannot_edit_the_document_that_bounds_it(self, client, monkeypatch):
        monkeypatch.setenv("BGATE_ACTOR", "agent:item-7")
        assert err(client.post("/api/bible", json={"kind": "pillar", "title": "more"})
                   )["code"] == "forbidden"


class TestReorder:
    def test_reorder_rewrites_ranks_contiguously(self, client, root, scoped):
        got = data(client.post("/api/bible/reorder", json={
            "kind": "scope_tier",
            "order": [scoped["mp"], scoped["core"], scoped["enemy"]]}))
        assert [s["title"] for s in got] == ["Multiplayer", "Core loop", "One enemy type"]
        assert [s["rank"] for s in got] == [1, 2, 3]

    def test_omitted_sections_keep_their_order_after_the_listed_ones(self, root, scoped):
        got = bible.reorder(root, "scope_tier", [scoped["mp"]])
        assert [s["title"] for s in got] == ["Multiplayer", "Core loop", "One enemy type"]

    def test_reordering_moves_the_cut_line_and_the_verdict_follows(self, root, scoped):
        assert scope.check(root, scoped["mp"])["allowed"] is False
        # Promote multiplayer above the line by ranking it first.
        bible.reorder(root, "scope_tier", [scoped["mp"], scoped["core"]])
        bible.update(root, scoped["line"], rank=99)
        assert scope.check(root, scoped["mp"])["allowed"] is True

    def test_foreign_id_is_refused_whole(self, root, scoped):
        pillar = bible.add(root, "pillar", "not a tier")
        with pytest.raises(ValueError):
            bible.reorder(root, "scope_tier", [scoped["core"], pillar["id"]])
        # Nothing was applied — the original ranks stand.
        assert [s["rank"] for s in bible.list_sections(root, "scope_tier")] == [1, 2, 4]

    def test_duplicate_ids_refused(self, root, scoped):
        with pytest.raises(ValueError):
            bible.reorder(root, "scope_tier", [scoped["core"], scoped["core"]])


class TestDeleteWithDependents:
    def test_delete_refuses_while_work_points_at_the_section(self, client, root, scoped):
        item = queue.add(root, "gameplay", "Build the loop")
        scope.assign(root, item["id"], scoped["core"])
        body = err(client.delete(f"/api/bible/{scoped['core']}"))
        assert body["code"] == "conflict"
        assert body["detail"]["work_items"][0]["id"] == item["id"]

    def test_reassign_moves_the_work(self, client, root, scoped):
        item = queue.add(root, "gameplay", "Build the loop")
        scope.assign(root, item["id"], scoped["core"])
        got = data(client.delete(
            f"/api/bible/{scoped['core']}?reassign_to={scoped['enemy']}"))
        assert got["reassigned_to"] == scoped["enemy"]
        assert scope.tier_of(root, item["id"])["id"] == scoped["enemy"]

    def test_force_untiers_and_says_so(self, client, root, scoped):
        item = queue.add(root, "gameplay", "Build the loop")
        scope.assign(root, item["id"], scoped["core"])
        got = data(client.delete(f"/api/bible/{scoped['core']}?force=true"))
        assert got["untiered"] is True
        assert scope.tier_of(root, item["id"]) is None


# ---------------------------------------------------------------------------
# The cut line
# ---------------------------------------------------------------------------

class TestCutLine:
    def test_tiers_know_which_side_they_are_on(self, root, scoped):
        below = {t["title"]: t["below_cut"] for t in scope.tiers(root)}
        assert below == {"Core loop": False, "One enemy type": False,
                         "Multiplayer": True}

    def test_check_allows_above_and_refuses_at_or_below(self, root, scoped):
        assert scope.check(root, scoped["core"])["allowed"] is True
        refused = scope.check(root, scoped["mp"])
        assert refused["allowed"] is False
        assert refused["code"] == "below_cut_line"
        assert "not being built" in refused["reason"]
        assert refused["cut_line_rank"] == 3

    def test_a_tier_exactly_at_the_line_is_out(self, root, scoped):
        at = bible.add(root, "scope_tier", "Right on the line", rank=3)
        assert scope.check(root, at["id"])["allowed"] is False

    def test_untiered_work_is_flagged_not_refused(self, root, scoped):
        got = scope.check(root, None)
        assert (got["allowed"], got["flagged"], got["code"]) == (True, True, "untiered")

    def test_no_cut_line_means_everything_passes_clean(self, root):
        tier = bible.add(root, "scope_tier", "Whatever", rank=9)
        got = scope.check(root, tier["id"])
        assert (got["allowed"], got["flagged"], got["code"]) == (True, False, "no_cut_line")

    def test_junk_tier_pointers_are_refused_not_ignored(self, root, scoped):
        assert scope.check(root, 12345)["code"] == "unknown_tier"
        pillar = bible.add(root, "pillar", "Tension")
        assert scope.check(root, pillar["id"])["code"] == "not_a_tier"

    def test_enforce_raises_only_on_refusal(self, root, scoped):
        assert scope.enforce(root, scoped["core"])["allowed"] is True
        with pytest.raises(scope.OutOfScope):
            scope.enforce(root, scoped["mp"])

    def test_assign_refuses_a_cut_tier(self, root, scoped):
        item = queue.add(root, "gameplay", "Deathmatch mode")
        with pytest.raises(scope.OutOfScope):
            scope.assign(root, item["id"], scoped["mp"])

    def test_moving_the_line_strands_existing_work(self, root, scoped):
        item = queue.add(root, "gameplay", "One enemy")
        scope.assign(root, item["id"], scoped["enemy"])
        assert scope.overview(root)["stranded"] == []
        bible.update(root, scoped["line"], rank=2)  # cut deeper
        stranded = scope.overview(root)["stranded"]
        assert [s["id"] for s in stranded] == [item["id"]]


class TestScopeEndpoint:
    def test_overview_reports_both_sides_of_the_line(self, client, root, scoped):
        item = queue.add(root, "gameplay", "Build the loop")
        scope.assign(root, item["id"], scoped["core"])
        got = data(client.get("/api/scope"))
        assert got["cut_line"]["title"] == "--- ship it ---"
        assert [t["title"] for t in got["in_scope"]] == ["Core loop", "One enemy type"]
        assert [t["title"] for t in got["cut"]] == ["Multiplayer"]
        core = next(t for t in got["tiers"] if t["id"] == scoped["core"])
        assert core["items"] == {"total": 1, "open": 1}

    def test_check_endpoint_matches_the_core_verdict(self, client, root, scoped):
        got = data(client.get(f"/api/scope/check?scope_tier_id={scoped['mp']}"))
        assert got["allowed"] is False and got["code"] == "below_cut_line"

    def test_assign_endpoint_refuses_a_cut_tier(self, client, root, scoped):
        item = queue.add(root, "gameplay", "Deathmatch mode")
        body = err(client.post("/api/scope/assign", json={
            "item_id": item["id"], "scope_tier_id": scoped["mp"]}))
        assert body["code"] == "forbidden"
        assert body["detail"]["code"] == "below_cut_line"


# ---------------------------------------------------------------------------
# Lore
# ---------------------------------------------------------------------------

@pytest.fixture()
def world(root):
    lore.add_entity(root, "faction", "The Ashen Order", summary="Zealots of the flame.",
                    status="canon")
    lore.add_entity(root, "character", "Sera Vane", summary="Their exiled general.",
                    status="canon")
    lore.add_fact(root, "The Ashen Order", "The Ashen Order worships the flame.",
                  locked=True)
    return root


class TestLoreHttp:
    def test_round_trip_add_link_fact(self, client, root):
        made = data(client.post("/api/lore", json={
            "kind": "faction", "name": "The Ashen Order", "status": "canon"}))
        assert made["slug"] == "the-ashen-order"
        data(client.post("/api/lore", json={
            "kind": "place", "name": "Cinder Vault", "status": "canon"}))

        link = data(client.post("/api/lore/link", json={
            "src": "the-ashen-order", "rel": "holds", "dst": "cinder-vault"}))
        assert link == {"src": "the-ashen-order", "rel": "holds",
                        "dst": "cinder-vault", "note": ""}

        fact = data(client.post("/api/lore/the-ashen-order/facts", json={
            "statement": "The Ashen Order worships the flame.", "locked": True}))
        assert fact["locked"] == 1

        brief = data(client.get("/api/lore/the-ashen-order"))
        assert brief["facts"][0]["statement"].startswith("The Ashen Order")
        assert brief["links"][0]["slug"] == "cinder-vault"

    def test_duplicate_entity_is_409(self, client, world):
        assert err(client.post("/api/lore", json={
            "kind": "faction", "name": "The Ashen Order"}))["code"] == "conflict"

    def test_list_is_paged_and_filterable(self, client, world):
        body = client.get("/api/lore?kind=faction&limit=1").json()
        assert body["page"]["total"] == 1
        assert body["data"][0]["slug"] == "the-ashen-order"

    def test_promoting_to_canon_is_human_only(self, client, world, monkeypatch):
        lore.add_entity(world, "character", "Torv Ekkel")
        monkeypatch.setenv("BGATE_ACTOR", "agent:item-3")
        assert err(client.patch("/api/lore/torv-ekkel", json={"status": "canon"})
                   )["code"] == "forbidden"

    def test_unknown_entity_is_404(self, client, world):
        assert err(client.get("/api/lore/nobody"))["code"] == "not_found"


class TestLoreGraph:
    def test_graph_shape_is_what_the_canvas_consumes(self, client, world):
        lore.add_entity(world, "place", "Cinder Vault", status="canon")
        lore.link(world, "sera-vane", "exiled_from", "the-ashen-order")

        graph = client.get("/api/lore").json()["graph"]
        node = next(n for n in graph["nodes"] if n["id"] == "the-ashen-order")
        assert node["title"] == "The Ashen Order"
        assert node["type"] == "lore-faction"
        assert {"x", "y", "w"} <= set(node)
        assert node["ports"]["in"][0]["id"] == "in"
        assert node["ports"]["out"][0]["id"] == "out"
        assert node["facts"] == 1

        edge = graph["edges"][0]
        assert edge["from"] == ["sera-vane", "out"]
        assert edge["to"] == ["the-ashen-order", "in"]
        assert edge["rel"] == "exiled_from"

    def test_filtered_out_nodes_do_not_leave_dangling_edges(self, client, world):
        lore.add_entity(world, "place", "Cinder Vault", status="canon")
        lore.link(world, "sera-vane", "exiled_from", "the-ashen-order")
        graph = client.get("/api/lore?kind=place").json()["graph"]
        assert [n["id"] for n in graph["nodes"]] == ["cinder-vault"]
        assert graph["edges"] == []


# ---------------------------------------------------------------------------
# Canon — the gate
# ---------------------------------------------------------------------------

class TestCanonHttp:
    def test_clean_text_passes(self, client, world):
        got = data(client.post("/api/canon/check", json={
            "text": "The Ashen Order worships the flame above all else."}))
        assert got["verdict"] == "ok" and got["flags"] == []

    def test_contradiction_of_a_locked_fact_is_a_conflict(self, client, world):
        got = data(client.post("/api/canon/check", json={
            "text": "The Ashen Order does not worship the flame."}))
        assert got["verdict"] == "conflict"
        assert [f["code"] for f in got["flags"]] == ["polarity_conflict"]

    def test_empty_text_is_400(self, client, world):
        assert err(client.post("/api/canon/check", json={"text": "  "})
                   )["code"] == "bad_request"

    def test_a_conflicting_fact_is_refused_at_the_write(self, client, world):
        body = err(client.post("/api/lore/the-ashen-order/facts", json={
            "statement": "The Ashen Order does not worship the flame."}))
        assert body["code"] == "conflict"
        assert body["detail"]["verdict"] == "conflict"
        assert lore.facts_of(world, "the-ashen-order") and len(
            lore.facts_of(world, "the-ashen-order")) == 1  # nothing landed

    def test_a_human_may_override_after_reading_the_flags(self, client, world):
        got = data(client.post("/api/lore/the-ashen-order/facts", json={
            "statement": "The Ashen Order does not worship the flame.",
            "override": True}))
        assert got["id"] > 0
        assert len(lore.facts_of(world, "the-ashen-order")) == 2

    def test_an_agent_may_not_override(self, client, world, monkeypatch):
        monkeypatch.setenv("BGATE_ACTOR", "agent:item-3")
        assert err(client.post("/api/lore/the-ashen-order/facts", json={
            "statement": "The Ashen Order does not worship the flame.",
            "override": True}))["code"] == "forbidden"

    def test_writing_about_a_retired_entity_is_refused(self, client, world):
        lore.update_entity(world, "sera-vane", status="retired")
        assert err(client.patch("/api/lore/the-ashen-order", json={
            "body": "Sera Vane still leads them."}))["code"] == "conflict"
