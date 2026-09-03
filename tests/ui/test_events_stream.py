"""The event bus over HTTP: the JSON read and the push channel.

The stream is what replaced forty timers in the dashboard, so the properties
worth pinning are the ones a subscriber leans on: the wire shape (``event:``
is the kind, ``id:`` is the row id), that a fresh subscriber starts from NOW
rather than replaying the log, that ``Last-Event-ID`` resumes exactly after
the row it names, that a kind outside the vocabulary is announced before it is
sent, and that a plain GET does not hang on a response that never ends.

Streaming through TestClient: every stream here is bounded with ``limit`` so
the generator returns on its own. Breaking out of an unbounded stream is what
hangs the portal thread, and a test that hangs teaches nothing. TestClient
also runs the whole response to completion before handing back the first
byte, so "a row that lands while the subscriber is connected" is a thread
that emits after a short delay, not an emit between two reads.
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from bgate_core.store import events
from bgate_ui.app import app

SSE = {"Accept": "text/event-stream"}


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


def _frames(text: str) -> list[dict]:
    """SSE text -> [{event, id, data, comment}] in order."""
    out = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        frame: dict = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                frame["comment"] = line[1:].strip()
            elif ":" in line:
                key, value = line.split(":", 1)
                frame[key.strip()] = value.strip()
        if "data" in frame:
            frame["data"] = json.loads(frame["data"])
        out.append(frame)
    return out


def _emit_later(root, kind: str, ref: str, delay: float = 0.8) -> None:
    def go():
        time.sleep(delay)
        events.emit(root, kind, ref=ref)
    threading.Thread(target=go, daemon=True).start()


def _stream(client, path: str, **headers) -> list[dict]:
    with client.stream("GET", path, headers={**SSE, **headers}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        return _frames("".join(r.iter_text()))


class TestWireShape:
    def test_hello_first_then_one_frame_per_row(self, client, root):
        events.emit(root, "item.done", ref="7", payload={"seat": "art"})
        got = _stream(client, "/api/events/stream?after=0&limit=1")
        assert got[0]["event"] == "hello"
        assert "item.done" in got[0]["data"]["kinds"]
        row = got[1]
        assert row["event"] == "item.done"
        assert row["id"] == "1"
        assert row["data"]["ref"] == "7"
        assert row["data"]["payload"] == {"seat": "art"}

    def test_a_fresh_subscriber_starts_from_now(self, client, root):
        """No Last-Event-ID and no ``after`` means no replay. The hello names
        the head as its id, so the browser's first reconnect resumes there,
        and the first row the subscriber sees is the first one emitted AFTER
        it connected."""
        events.emit(root, "item.done", ref="1")
        events.emit(root, "item.failed", ref="2")
        _emit_later(root, "item.done", "3")
        got = _stream(client, "/api/events/stream?limit=1")
        assert got[0]["event"] == "hello"
        assert got[0]["id"] == "2"
        rows = [f for f in got if f.get("event") not in ("hello", "vocabulary")]
        assert [r["id"] for r in rows] == ["3"]

    def test_the_hello_id_is_the_head(self, client, root):
        events.emit(root, "item.done", ref="1")
        events.emit(root, "item.failed", ref="2")
        events.emit(root, "item.done", ref="3")
        got = _stream(client, "/api/events/stream?after=2&limit=1")
        assert got[0]["event"] == "hello"
        assert got[0]["data"]["head"] == 3
        assert got[1]["id"] == "3"


class TestResume:
    def test_last_event_id_resumes_after_that_row(self, client, root):
        for n in range(1, 5):
            events.emit(root, "item.done", ref=str(n))
        got = _stream(client, "/api/events/stream?limit=2", **{"Last-Event-ID": "2"})
        rows = [f for f in got if f.get("event") not in ("hello", "vocabulary")]
        assert [r["id"] for r in rows] == ["3", "4"]
        assert got[0]["id"] == "2"

    def test_the_header_beats_the_query(self, client, root):
        for n in range(1, 4):
            events.emit(root, "item.done", ref=str(n))
        got = _stream(client, "/api/events/stream?after=0&limit=1",
                      **{"Last-Event-ID": "2"})
        assert got[1]["id"] == "3"

    def test_a_kind_outside_the_vocabulary_is_announced_first(self, client, root):
        """EventSource dispatches a named event only to a listener registered
        for that name, so a kind the hello did not list must be announced
        before it is sent or the browser drops it on the floor."""
        events.emit(root, "item.done", ref="1")
        _emit_later(root, "greenlight.stage", "graybox")
        got = _stream(client, "/api/events/stream?limit=1")
        assert "greenlight.stage" not in got[0]["data"]["kinds"]
        assert got[1]["event"] == "vocabulary"
        assert got[1]["data"]["kinds"] == ["greenlight.stage"]
        assert got[2]["event"] == "greenlight.stage"
        # And once the log holds it, the next hello lists it up front.
        again = _stream(client, "/api/events/stream?after=1&limit=1")
        assert "greenlight.stage" in again[0]["data"]["kinds"]
        assert again[1]["event"] == "greenlight.stage"

    def test_kinds_filters_the_stream(self, client, root):
        events.emit(root, "item.done", ref="1")
        events.emit(root, "item.failed", ref="2")
        events.emit(root, "item.done", ref="3")
        got = _stream(client, "/api/events/stream?after=0&limit=2&kinds=item.done")
        rows = [f for f in got if f.get("event") == "item.done"]
        assert [r["id"] for r in rows] == ["1", "3"]


class TestNegotiation:
    def test_a_plain_get_on_the_stream_path_does_not_hang(self, client):
        """A browser address bar, a smoke test, curl with no header: 406 with
        the instruction, never a response that has no end."""
        got = client.get("/api/events/stream")
        assert got.status_code == 406
        assert "text/event-stream" in got.json()["error"]["message"]

    def test_the_json_read_still_answers_at_api_events(self, client, root):
        events.emit(root, "item.done", ref="1")
        body = client.get("/api/events").json()
        assert body["ok"] is True
        assert body["data"]["tail"] is True
        assert [e["id"] for e in body["data"]["events"]] == [1]

    def test_the_first_frame_is_the_hello(self, client, root):
        """What a subscriber reads before anything else: the vocabulary and
        the head, so a resume has an id before a single row has landed."""
        events.emit(root, "item.done", ref="1")
        with client.stream("GET", "/api/events/stream?after=0&limit=1",
                           headers=SSE) as r:
            first = next(r.iter_lines())
        assert first.strip() == "event: hello"

    def test_reads_need_no_token(self, root, monkeypatch):
        """The guard exempts safe methods; a viewer may subscribe."""
        monkeypatch.delenv("BGATE_NO_AUTH", raising=False)
        monkeypatch.setenv("BGATE_ROOT", str(root))
        events.emit(root, "item.done", ref="1")
        got = _stream(TestClient(app), "/api/events/stream?after=0&limit=1")
        assert got[1]["event"] == "item.done"
