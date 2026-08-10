"""The transport contract the review overlay and the dashboard lists depend on.

Two audit findings live here. Byte ranges: the review overlay's markers, moment
dots and transcript lines are all seeks, and a seek is a Range request — without
206 the player can only start from zero. And pagination: the list endpoints were
unbounded, so they are windowed now, but the frontend still reads their bare
payload, which is why the window is opt-in and these tests assert BOTH shapes.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_core import db, queue as _queue
from bgate_ui.app import app


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


BODY = bytes(range(256)) * 40  # 10240 bytes, every offset distinguishable


def _session_with_video(root, body: bytes = BODY) -> int:
    """A ready session whose mp4 really exists under .bgate/playtests."""
    store = root / ".bgate" / "playtests"
    store.mkdir(parents=True, exist_ok=True)
    video = store / "session.mp4"
    video.write_bytes(body)
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO playtest_session (name, slug, status, video_path) "
            "VALUES ('recorded', 'recorded', 'ready', ?)", (str(video),))
        return int(cur.lastrowid)


def _session_without_video(root, *, stage: str = "transcribing") -> int:
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO playtest_session (name, slug, status, video_path, "
            "processing_stage) VALUES ('pending', 'pending', 'processing', '', ?)",
            (stage,))
        return int(cur.lastrowid)


class TestVideoRanges:
    def test_no_range_serves_the_whole_body(self, client, root):
        sid = _session_with_video(root)
        got = client.get(f"/api/playtest/{sid}/video")
        assert got.status_code == 200
        assert got.content == BODY
        assert got.headers["accept-ranges"] == "bytes"

    def test_closed_range_returns_exactly_those_bytes(self, client, root):
        sid = _session_with_video(root)
        got = client.get(f"/api/playtest/{sid}/video",
                         headers={"Range": "bytes=100-199"})
        assert got.status_code == 206
        assert got.content == BODY[100:200]
        assert got.headers["content-range"] == f"bytes 100-199/{len(BODY)}"
        assert got.headers["content-length"] == "100"
        assert got.headers["accept-ranges"] == "bytes"

    def test_open_ended_range_runs_to_the_end(self, client, root):
        sid = _session_with_video(root)
        got = client.get(f"/api/playtest/{sid}/video",
                         headers={"Range": "bytes=10000-"})
        assert got.status_code == 206
        assert got.content == BODY[10000:]
        assert got.headers["content-range"] == f"bytes 10000-10239/{len(BODY)}"
        assert got.headers["content-length"] == "240"

    def test_suffix_range_returns_the_tail(self, client, root):
        # How a player finds the moov atom of a file that indexes at the end.
        sid = _session_with_video(root)
        got = client.get(f"/api/playtest/{sid}/video",
                         headers={"Range": "bytes=-256"})
        assert got.status_code == 206
        assert got.content == BODY[-256:]
        assert got.headers["content-range"] == f"bytes 9984-10239/{len(BODY)}"

    def test_range_past_the_end_is_416(self, client, root):
        sid = _session_with_video(root)
        got = client.get(f"/api/playtest/{sid}/video",
                         headers={"Range": "bytes=99999-"})
        assert got.status_code == 416
        assert got.headers["content-range"] == f"bytes */{len(BODY)}"
        assert got.json()["error"]["code"] == "range_not_satisfiable"

    def test_malformed_range_is_416(self, client, root):
        sid = _session_with_video(root)
        got = client.get(f"/api/playtest/{sid}/video",
                         headers={"Range": "bytes=abc-def"})
        assert got.status_code == 416
        assert got.headers["content-range"] == f"bytes */{len(BODY)}"

    def test_unknown_range_unit_is_ignored(self, client, root):
        # RFC 7233: a unit we do not speak is not an error, it is a full body.
        sid = _session_with_video(root)
        got = client.get(f"/api/playtest/{sid}/video",
                         headers={"Range": "frames=1-2"})
        assert got.status_code == 200
        assert got.content == BODY

    def test_a_seek_reads_the_same_bytes_the_full_body_has(self, client, root):
        """The property the overlay actually needs: seeking anywhere is honest."""
        sid = _session_with_video(root)
        for start, end in ((0, 0), (1, 3), (5000, 5010), (10239, 10239)):
            got = client.get(f"/api/playtest/{sid}/video",
                             headers={"Range": f"bytes={start}-{end}"})
            assert got.status_code == 206
            assert got.content == BODY[start:end + 1]


class TestVideoErrors:
    def test_unknown_session_is_a_clean_404(self, client):
        got = client.get("/api/playtest/9999/video")
        assert got.status_code == 404
        assert got.json()["error"]["code"] == "not_found"

    def test_session_without_video_says_so_and_not_security(self, client, root):
        sid = _session_without_video(root, stage="transcribing")
        got = client.get(f"/api/playtest/{sid}/video")
        assert got.status_code == 404
        body = got.json()["error"]
        assert "no video yet" in body["message"]
        assert "escape" not in body["message"]
        assert body["detail"]["stage"] == "transcribing"

    def test_recorded_path_that_vanished_also_reads_as_no_video(self, client, root):
        sid = _session_with_video(root)
        (root / ".bgate" / "playtests" / "session.mp4").unlink()
        got = client.get(f"/api/playtest/{sid}/video")
        assert got.status_code == 404
        assert "no video yet" in got.json()["error"]["message"]


class TestPagination:
    """Opt-in: bare shape without params, full envelope with them."""

    def _fill(self, root, n: int = 12) -> None:
        for i in range(n):
            _queue.add(root, "tech", f"task {i}")

    def test_bare_shape_is_unchanged_when_no_params_are_sent(self, client, root):
        self._fill(root)
        got = client.get("/api/queue").json()
        assert isinstance(got["items"], list)
        assert len(got["items"]) == 12
        assert "ok" not in got and "data" not in got

    def test_page_metadata_rides_alongside_without_moving_anything(self, client, root):
        self._fill(root)
        got = client.get("/api/queue").json()
        assert got["page"]["total"] == 12
        assert got["page"]["offset"] == 0
        assert got["page"]["next_offset"] is None

    def test_supplying_limit_switches_to_the_envelope(self, client, root):
        self._fill(root)
        got = client.get("/api/queue?limit=5").json()
        assert got["ok"] is True
        assert len(got["data"]) == 5
        assert got["page"] == {"limit": 5, "offset": 0, "total": 12,
                               "next_offset": 5}

    def test_offset_alone_also_switches(self, client, root):
        self._fill(root)
        got = client.get("/api/queue?offset=2").json()
        assert got["ok"] is True
        assert got["page"]["offset"] == 2

    def test_windows_tile_the_whole_list_without_gaps_or_repeats(self, client, root):
        self._fill(root)
        seen = []
        offset = 0
        while offset is not None:
            page = client.get(f"/api/queue?limit=5&offset={offset}").json()
            seen.extend(row["id"] for row in page["data"])
            offset = page["page"]["next_offset"]
        assert len(seen) == 12
        assert len(set(seen)) == 12

    def test_total_counts_the_table_not_the_window(self, client, root):
        self._fill(root, 12)
        got = client.get("/api/queue?limit=1").json()
        assert len(got["data"]) == 1
        assert got["page"]["total"] == 12

    def test_a_filter_narrows_the_total_too(self, client, root):
        self._fill(root, 4)
        got = client.get("/api/queue?status=queued&limit=2").json()
        assert got["page"]["total"] == 4
        got = client.get("/api/queue?status=done&limit=2").json()
        assert got["page"]["total"] == 0

    def test_limit_over_the_cap_is_refused(self, client, root):
        got = client.get("/api/queue?limit=5000")
        assert got.status_code == 422
        assert got.json()["ok"] is False

    @pytest.mark.parametrize("path,key", [
        ("/api/queue", "items"),
        ("/api/activity", "events"),
        ("/api/artifacts", "artifacts"),
        ("/api/iterations", "iterations"),
        ("/api/agents", "agents"),
        ("/api/assets/workspace", "groups"),
    ])
    def test_every_list_endpoint_keeps_its_key_and_gains_a_page(
            self, client, root, path, key):
        got = client.get(path).json()
        assert isinstance(got[key], list)
        assert set(got["page"]) == {"limit", "offset", "total", "next_offset"}

    @pytest.mark.parametrize("path", [
        "/api/queue", "/api/activity", "/api/artifacts", "/api/iterations",
        "/api/agents", "/api/assets/workspace",
    ])
    def test_every_list_endpoint_envelopes_when_asked(self, client, root, path):
        got = client.get(f"{path}?limit=2").json()
        assert got["ok"] is True
        assert isinstance(got["data"], list)
        assert got["page"]["limit"] == 2

    def test_activity_still_reads_incrementally(self, client, root):
        from bgate_core import activity
        for i in range(5):
            activity.log(root, "note", f"event {i}")
        first = client.get("/api/activity").json()["events"]
        newest = first[0]["id"]
        assert client.get(f"/api/activity?after_id={newest}").json()["events"] == []

    def test_playtest_detail_keeps_its_body_and_windows_its_items(self, client, root):
        sid = _session_with_video(root)
        got = client.get(f"/api/playtest/{sid}").json()
        assert "session" in got and isinstance(got["items"], list)
        assert got["page"]["offset"] == 0


class TestExceptionsDoNotLeak:
    """Nothing derived from an exception reaches a response body.

    CodeQL's py/stack-trace-exposure has an abstract Sanitizer class with ZERO
    implementations, so no amount of redaction clears it — the taint simply must
    not reach the sink. Two earlier attempts are recorded in safe_error's
    docstring: scrubbing (still flagged) and logging instead (a HIGH, worse).

    The cost is bounded on purpose. Only UNEXPECTED failures lose their text;
    every deliberate refusal raises ApiError with a message written as a literal
    in our source and is untouched.
    """

    @pytest.mark.parametrize("exc", [
        OSError(2, "No such file", "/home/marta/private/notes.txt"),
        RuntimeError("rejected sk-live-NOTREAL-abcdefghijklmnop"),
        ValueError("nothing on disk at game/assets/cinematics/intro.ogv"),
    ])
    def test_no_part_of_the_exception_survives(self, exc):
        from bgate_ui import api

        out = api.safe_error(exc)
        assert "marta" not in out and "NOTREAL" not in out
        # Not even the type name: an attribute read on a caught exception is
        # still a read of the exception.
        assert type(exc).__name__ not in out

    def test_it_is_the_same_string_every_time(self):
        """A constant, which is what makes it untainted rather than sanitised."""
        from bgate_ui import api

        assert api.safe_error(ValueError("a")) == api.safe_error(OSError("b"))

    def test_it_still_says_something_a_person_can_act_on(self):
        """A blank panel is what api.py exists to prevent. The message has to
        explain that detail was withheld and where to find it."""
        from bgate_ui import api

        out = api.safe_error(ValueError("x"))
        assert "traceback" in out.lower() and len(out) > 40

    def test_deliberate_refusals_keep_their_message(self, client):
        """The 95% of errors a user actually hits. These raise ApiError with a
        literal we wrote, never touch safe_error, and must not be collateral."""
        got = client.post("/api/cinematic/plan", json={"name": "x", "shots": []})
        assert got.status_code == 400
        assert "not a plan" in got.text

    def test_a_traversal_refusal_still_explains_itself(self, client):
        got = client.post("/api/cinematic/plan", json={
            "name": "x", "shots": [{"action": "a", "duration": 5,
                                    "first_frame": "../../../etc/passwd"}]})
        assert got.status_code == 400
        assert "outside the project" in got.text

    def test_the_unhandled_handler_says_nothing_either(self, client,
                                                       monkeypatch):
        """The widest sink in the product."""
        import bgate_ui.app as app_module

        @app_module.app.get("/api/_boom_for_test")
        def _boom():
            raise RuntimeError("leaked /home/marta/secret sk-live-NOTREAL-xyz")

        got = TestClient(app_module.app, raise_server_exceptions=False).get(
            "/api/_boom_for_test")
        assert got.status_code == 500
        assert "marta" not in got.text and "NOTREAL" not in got.text
