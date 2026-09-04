"""The two new HTTP surfaces, over the wire.

Route modules under bgate_ui/routes/ are AUTO-DISCOVERED, and the register()
that mounts them swallows an import failure so a broken workspace still leaves
the rest of the dashboard usable. That is the right behaviour and it is also the
one that can hide a whole feature: a module that fails to import is simply
MISSING, and the console renders as if the endpoints were never written. The
first test here is the one that catches that.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import events, settings
from bgate_core.board import gates, queue
from bgate_ui.app import app


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


class TestTheyAreActuallyMounted:
    def test_no_route_module_failed_to_import(self, client):
        got = client.get("/api/routes/status").json()
        data = got.get("data", got)
        assert data.get("failed") == [], \
            f"a route module did not import, so its endpoints are missing: {data}"
        assert {"events", "settings"} <= set(data.get("registered") or [])

    @pytest.mark.parametrize("path", ["/api/events", "/api/settings"])
    def test_the_endpoint_answers(self, client, path):
        assert client.get(path).status_code == 200


class TestEventsFeed:
    def test_the_feed_returns_what_the_bus_holds(self, client, root):
        events.emit(root, "item.done", ref="41", payload={"item": 41})
        got = client.get("/api/events").json()
        data = got.get("data", got)
        assert data["events"] and data["events"][0]["kind"] == "item.done"
        assert data["events"][0]["payload"] == {"item": 41}

    def test_an_omitted_since_is_the_TAIL_not_the_head_of_the_log(self, client, root):
        """A cold drawer asking for "the latest" and a poller asking for "what is
        new" are different questions. Conflating them showed a fortnight-old row
        as the first thing in the panel."""
        made = [events.emit(root, "item.done", ref=str(i)) for i in range(3)]
        got = client.get("/api/events", params={"limit": 2}).json()
        got = got.get("data", got)
        assert [e["id"] for e in got["events"]] == made[-2:]
        assert got["tail"] is True

    def test_since_pages_forward(self, client, root):
        for i in range(3):
            events.emit(root, "item.done", ref=str(i))
        first = client.get("/api/events", params={"since": 0, "limit": 2}).json()
        first = first.get("data", first)
        assert len(first["events"]) == 2 and first["more"] is True
        second = client.get("/api/events", params={"since": first["seq"]}).json()
        second = second.get("data", second)
        assert len(second["events"]) == 1 and second["more"] is False

    def test_a_kind_filter_narrows_it(self, client, root):
        events.emit(root, "item.done", ref="1")
        events.emit(root, "agent.spawned", ref="2")
        got = client.get("/api/events", params={"kinds": "item.done"}).json()
        got = got.get("data", got)
        assert {e["kind"] for e in got["events"]} == {"item.done"}

    def test_the_unread_count_drops_when_the_bell_is_read(self, client, root):
        for i in range(3):
            events.emit(root, "item.done", ref=str(i))
        before = client.get("/api/events").json()
        before = before.get("data", before)
        assert before.get("unread", 0) >= 1
        marked = client.post("/api/events/read", json={}).json()
        marked = marked.get("data", marked)
        assert marked.get("unread", 0) == 0
        after = client.get("/api/events").json()
        assert (after.get("data", after)).get("unread", 0) == 0

    def test_a_bad_limit_is_a_400_not_a_500(self, client):
        assert client.get("/api/events", params={"limit": 0}).status_code == 422
        assert client.get("/api/events", params={"since": -5}).status_code == 422


class TestSettingsApi:
    def test_the_description_carries_what_a_panel_needs_to_render(self, client):
        got = client.get("/api/settings").json()
        data = got.get("data", got)
        groups = data.get("groups") or data
        assert groups, "no groups returned"
        flat = []
        for group in (groups if isinstance(groups, list) else groups.values()):
            flat.extend(group.get("fields") or group.get("settings") or [])
        assert flat, "groups carried no fields"
        row = flat[0]
        for required in ("key", "value", "default", "source", "help", "kind"):
            assert required in row, f"a field is missing {required!r}: {row}"

    def test_a_patch_takes_effect_and_returns_the_new_state(self, client, root):
        got = client.patch("/api/settings", json={"gate.mode": "builders"})
        assert got.status_code == 200
        assert gates.mode(root) == "builders"

    def test_an_unknown_key_is_a_400(self, client):
        assert client.patch("/api/settings", json={"nope.nope": 1}).status_code == 400

    def test_a_value_outside_the_declared_range_is_a_400(self, client):
        assert client.patch("/api/settings",
                            json={"qa.max_rounds": 0}).status_code == 400
        assert client.patch("/api/settings",
                            json={"gate.mode": "whenever"}).status_code == 400

    def test_one_key_can_be_read_on_its_own(self, client):
        got = client.get("/api/settings/gate.mode").json()
        data = got.get("data", got)
        assert data["key"] == "gate.mode" and "value" in data
        assert client.get("/api/settings/nope.nope").status_code == 404

    def test_an_env_owned_field_is_reported_as_env_owned(self, client, monkeypatch):
        """A panel that lets you edit a value an env var owns is the most
        expensive lie it can tell — so the API has to say who won."""
        monkeypatch.setenv("BGATE_QA_GATE", "0")
        got = client.get("/api/settings/gate.mode").json()
        data = got.get("data", got)
        assert data["source"] == "env"


class TestTheBudgetAliasIsGone:
    def test_the_old_route_answers_404(self, client):
        """`/api/spend/budget` wrote the per-item/per-day/per-project dollar
        ceilings. There are no ceilings any more (db migration 0045), so the
        route is gone — a 404 rather than a 200 that stores nothing."""
        assert client.patch("/api/spend/budget",
                            json={"per_day_usd": 150}).status_code == 404
        assert client.get("/api/spend").status_code == 404


class TestConsoleAnswer:
    def test_an_answer_to_a_question_that_does_not_exist_is_not_a_500(self, client):
        got = client.post("/api/console/answer",
                          json={"seq": 999999, "answer": "yes"})
        assert got.status_code in (400, 404)

    def test_open_questions_ride_the_console_payload(self, client, root):
        item = queue.add(root, "art", "a sprite")
        events.emit(root, "director.question", ref=str(item["id"]),
                    payload={"item": item["id"], "question": "which palette?"})
        got = client.get("/api/console/state?steps=false").json()
        data = got.get("data", got)
        assert "questions" in data
