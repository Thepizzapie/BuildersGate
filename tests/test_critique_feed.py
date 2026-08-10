"""The critique feed — the endpoint a stream overlay polls.

WHY IT EXISTS SEPARATELY FROM THE CONSOLE PANEL. The Agents view draws a
critique card when new artwork lands, but only while that view is open in a
browser somebody is looking at, and only for artifacts belonging to a STILL-
RUNNING item (the phase assembly it reads is built for live agents). An overlay
in an OBS browser source has neither property: it runs with no dashboard open,
it wants to own its own animation, and a card missed because a tab was shut is
gone forever.

So this reads the artifact table directly and answers the same question after
the run has finished.

THE CURSOR IS THE POINT. An overlay that has to diff payloads to decide whether
to play its entry animation will play it again the first time an unrelated field
changes wording. `since` makes "is there something new" a server-side yes/no.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core import artifacts, queue as _queue
from bgate_ui.app import app


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


def _make(root, name="species_red_fox", item_id=None, body=b"plate"):
    f = root / f"{name}.png"
    f.write_bytes(body)
    return artifacts.register(root, name, f, producer="image_generate",
                              work_item_id=item_id)


def _get(client, **params):
    r = client.get("/api/critique", params=params)
    assert r.status_code == 200, r.text
    return r.json()["data"]


class TestTheEmptyCase:
    def test_a_project_with_no_art_says_so(self, client):
        got = _get(client)
        assert got["new"] is False
        assert got["critiques"] == []
        assert "no artwork" in got["note"]


class TestTheCard:
    def test_it_reports_the_newest(self, root, client):
        _make(root, "a")
        b = _make(root, "b")
        got = _get(client)
        assert got["latest"]["artifact_id"] == b["id"]
        assert got["latest"]["logical_name"] == "b"

    def test_it_names_the_seat_that_made_it(self, root, client):
        item = _queue.add(root, "art", title="fox plate", brief="a fox")
        _make(root, item_id=item["id"])
        assert _get(client)["latest"]["seat"] == "art"

    def test_a_preview_url_is_built_for_the_caller(self, root, client):
        """Every consumer would otherwise rebuild the same link by hand, and the
        one that gets percent-encoding wrong breaks on the first path with a
        space in it."""
        _make(root, "two words")
        url = _get(client)["latest"]["preview_url"]
        assert url.startswith("/api/preview?rel=")
        assert " " not in url

    def test_no_absolute_path_reaches_the_payload(self, root, client):
        """THIS FEED IS FOR A BROWSER SOURCE THAT IS ON CAMERA. Some generators
        write metadata.preview absolute, /api/preview accepts either, and the
        machine's user name and directory layout would ride out in the URL —
        the exact thing the dashboard's redaction mode exists to stop."""
        shot = root / ".bgate" / "previews" / "hero.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        shot.write_bytes(b"render")
        f = root / "hero.png"
        f.write_bytes(b"plate")
        artifacts.register(root, "hero", f, producer="image_generate",
                           metadata={"preview": str(shot)})   # absolute, on purpose
        card = _get(client)["latest"]
        blob = f"{card['preview_url']} {card['path']}"
        assert str(root) not in blob
        assert str(root).replace("\\", "/") not in blob
        assert "%3A" not in card["preview_url"]     # no drive letter survived

    def test_the_verdict_rides_along(self, root, client):
        art = _make(root)
        artifacts.qa_verdict(root, art["id"], passed=False, score=34,
                             note="legs cropped at the frame edge",
                             actor="agent:item-3")
        card = _get(client)["latest"]
        assert card["verdict"] == "fail"
        assert card["score"] == 34
        assert "legs cropped" in card["note"]

    def test_no_verdict_is_not_a_failing_one(self, root, client):
        """An agent that has not judged a candidate yet and one that judged it
        badly must not render the same."""
        _make(root)
        assert _get(client)["latest"]["verdict"] == ""

    def test_it_says_whether_a_human_still_owes_a_decision(self, root, client):
        art = _make(root)
        assert _get(client)["latest"]["awaiting_human"] is True
        artifacts.review(root, art["id"], "approved", actor="Sam")
        assert _get(client)["latest"]["awaiting_human"] is False

    def test_a_reviewed_revision_is_still_reported(self, root, client):
        """An overlay wants to SHOW the render; whether a human has since
        approved it is a field on the card, not a reason to hide it."""
        art = _make(root)
        artifacts.review(root, art["id"], "approved", actor="Sam")
        assert _get(client)["latest"]["artifact_id"] == art["id"]


class TestTheCursor:
    def test_since_the_newest_reports_nothing_new(self, root, client):
        art = _make(root)
        got = _get(client, since=art["id"])
        assert got["new"] is False
        assert got["critiques"] == []

    def test_the_cursor_is_echoed_so_one_variable_suffices(self, root, client):
        art = _make(root)
        assert _get(client, since=art["id"])["seq"] == art["id"]

    def test_something_newer_comes_back(self, root, client):
        first = _make(root, "a")
        second = _make(root, "b")
        got = _get(client, since=first["id"])
        assert got["new"] is True
        assert got["latest"]["artifact_id"] == second["id"]

    def test_a_junk_cursor_does_not_500(self, client, root):
        _make(root)
        r = client.get("/api/critique", params={"since": "banana"})
        # FastAPI coerces the query type; either a 422 or a clean answer is
        # fine, a stack trace into an OBS browser source is not.
        assert r.status_code in (200, 422)


class TestBounds:
    def test_limit_is_capped(self, root, client):
        for i in range(30):
            _make(root, f"a{i}")
        from bgate_ui.routes import critique as mod
        assert len(_get(client, limit=999)["critiques"]) <= mod.MAX_LIMIT

    def test_one_by_default(self, root, client):
        _make(root, "a")
        _make(root, "b")
        assert len(_get(client)["critiques"]) == 1
