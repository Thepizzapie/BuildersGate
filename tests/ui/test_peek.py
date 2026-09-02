"""The file viewer behind the agent rails.

WHAT IT REPLACES. The live rail could name every file a run touched and open
none of them: a 90-character absolute path in the middle of a log sentence, and
a second editor window to find out whether the scene the agent said it baked had
anything in it. For visual work it was worse — the agent is looking at a sprite
sheet, the human is looking at the word "sheet".

Two properties matter more than the happy path. It must never read outside the
project (a dashboard with a `rel` parameter is one traversal away from being a
file browser for the whole disk), and it must SAY what a file is rather than
rendering bytes as text and calling it a viewer.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bgate_ui.agents import phases as _phases
from bgate_ui.app import app


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


class TestSandbox:
    @pytest.mark.parametrize("rel", [
        "../../../../Windows/System32/drivers/etc/hosts",
        "../.bgate/../../secret.txt",
        "game/../../outside.txt",
    ])
    def test_a_path_that_walks_out_is_refused(self, client, rel):
        assert client.get("/api/peek", params={"rel": rel}).status_code == 403

    def test_a_percent_encoded_traversal_is_a_filename_not_an_escape(self, client):
        """`..%2f..%2f.claude.json` arrives as a literal name once the query is
        decoded once — so it resolves INSIDE the project and finds nothing. The
        property under test is that it is not readable, not which no it gets."""
        got = client.get("/api/peek", params={"rel": "..%2f..%2f.claude.json"})
        assert got.status_code == 403 or got.json()["kind"] == "missing"

    def test_an_absolute_path_outside_the_project_is_refused(self, client):
        got = client.get("/api/peek", params={"rel": "C:/Windows/win.ini"})
        # Resolved against the root it either escapes (403) or simply is not
        # there (missing) — what it must never be is readable.
        assert got.status_code == 403 or got.json()["kind"] == "missing"


class TestKinds:
    def test_text_comes_back_as_lines_with_a_starting_number(self, client, root):
        (root / "game").mkdir(exist_ok=True)
        (root / "game" / "combat.gd").write_text(
            "\n".join(f"line {i}" for i in range(1, 21)), encoding="utf-8")
        d = client.get("/api/peek", params={"rel": "game/combat.gd"}).json()
        assert d["kind"] == "text"
        assert d["lines"][0] == "line 1" and d["lines_total"] == 20
        assert d["first_line"] == 1 and d["truncated"] is False

    def test_a_window_reports_that_it_is_one(self, client, root):
        (root / "big.txt").write_text("\n".join(str(i) for i in range(500)),
                                      encoding="utf-8")
        d = client.get("/api/peek",
                       params={"rel": "big.txt", "offset": 10, "lines": 5}).json()
        assert d["lines"] == ["10", "11", "12", "13", "14"]
        assert d["first_line"] == 11 and d["truncated"] is True

    def test_an_image_is_named_not_read(self, client, root):
        (root / "hero.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
        d = client.get("/api/peek", params={"rel": "hero.png"}).json()
        assert d["kind"] == "image"
        # The viewer streams it through the endpoint that does content types and
        # byte ranges properly, rather than base64 through a JSON payload.
        assert d["url"].startswith("/api/preview?rel=")

    def test_binary_says_binary_instead_of_rendering_mojibake(self, client, root):
        (root / "blob.dat").write_bytes(bytes(range(256)))
        assert client.get("/api/peek", params={"rel": "blob.dat"}).json()["kind"] == "binary"

    def test_a_huge_file_is_refused_with_its_size(self, client, root, monkeypatch):
        from bgate_ui import app as _app
        monkeypatch.setattr(_app, "PEEK_MAX_BYTES", 64)
        (root / "long.txt").write_text("x" * 200, encoding="utf-8")
        d = client.get("/api/peek", params={"rel": "long.txt"}).json()
        assert d["kind"] == "too_big" and "200" in d["note"]

    def test_a_path_that_was_never_written_says_so(self, client):
        d = client.get("/api/peek", params={"rel": "game/scenes/floor_0.tscn"}).json()
        assert d["kind"] == "missing"


class TestPathsOnSteps:
    """`look()` already found the pictures; the files that ARE the work — scenes,
    source, data — were left in prose."""

    def test_source_paths_become_openable_and_missing_ones_do_not(self, root):
        (root / "game").mkdir(exist_ok=True)
        (root / "game" / "combat.gd").write_text("func _ready(): pass", encoding="utf-8")
        steps = [{"kind": "tool", "name": "Edit",
                  "hint": f"editing {root / 'game' / 'combat.gd'} and "
                          "game/scenes/ghost.tscn"}]
        out = _phases.look(root, [{"n": 1, "steps": steps, "artifacts": []}])
        assert out[0]["steps"][0]["files"] == ["game/combat.gd"]
        # A path in a prompt that was never written is not a file.
        assert out[0]["read"] == ["game/combat.gd"]

    def test_the_agents_own_log_is_not_offered_as_a_file(self, root):
        logs = root / ".bgate" / "agents"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "item-7.log").write_text("noise", encoding="utf-8")
        steps = [{"kind": "result", "text": "wrote .bgate/agents/item-7.log"}]
        out = _phases.look(root, [{"n": 1, "steps": steps, "artifacts": []}])
        assert out[0]["read"] == []

    def test_what_a_phase_made_is_not_repeated_as_what_it_read(self, root):
        (root / "game").mkdir(exist_ok=True)
        (root / "game" / "made.tscn").write_text("[gd_scene]", encoding="utf-8")
        phase = {"n": 1, "artifacts": [{"path": "game/made.tscn"}],
                 "steps": [{"kind": "result", "text": "baked game/made.tscn"}]}
        out = _phases.look(root, [phase])
        assert out[0]["read"] == []
