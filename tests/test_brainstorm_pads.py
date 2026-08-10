"""The two-tool pad server, and the line it is allowed to cross.

WHY THIS FILE EXISTS SEPARATELY from test_brainstorm.py. That file asserts what
the brainstorm room does NOT do. This one asserts the one thing it deliberately
started doing: the thinking partner can now read the human's pads and add to
their drawing. That is a reversal of an earlier decision and it needs its
reasoning pinned somewhere a reader will find it before "fixing" it.

THE PROMISE IS UNCHANGED, and the distinction is the whole file. The room
promises that NO WORK IS FILED AND NO PROJECT FILE IS WRITTEN until a human
presses Deploy on a plan they have read. A rectangle in somebody's scratch
diagram is neither. It is the same act as typing a sentence into the
conversation — stored in the same row of the same table, deleted whenever they
delete the session. The scene is stored as structured JSON rather than a PNG
precisely so a text model can take part in it.

WHAT IS ASSERTED HERE:
  * the surface is exactly two tools, and the names match what runners.py
    allowlists — a drift between those two is how a third tool ships approved;
  * the writing pad is READ-ONLY to the partner. It is an hour of somebody's
    typing and a whole-document write from a stale read deletes the rest of it;
  * a draw MERGES and never deletes, so the partner cannot cost the human work;
  * a stale `rev` is refused rather than landed on top of;
  * the session comes from the environment, not from a tool argument, so a
    partner cannot reach another room's pads by asking for a different id.

NOT ASSERTED HERE, because it cannot be: that the CLI really builds this tool
list and no other. Nothing in a Python test can prove what a spawned process
constructed. That was checked by reading the session's own `system/init` event
back — it reported exactly these two names and `mcp_servers: ["pads"]` — and
brainsession records the readback on the live entry so a human can check it in
the UI rather than trust this docstring.
"""
from __future__ import annotations

import pytest

from bgate_core import brainstorm as _bs
from bgate_mcp import padserver
from bgate_ui import runners as _runners

SCENE = {"elements": [
    {"id": "hub-1", "type": "rectangle", "x": 10, "y": 20, "width": 120,
     "height": 60, "text": "hub"},
    {"id": "note-1", "type": "text", "x": 10, "y": 200, "text": "optional?"},
]}


@pytest.fixture()
def pad(root, monkeypatch):
    """A project, a session with both pads filled, and the env the dashboard
    stamps on a spawned pad server."""
    session = _bs.create(root, seat="director", title="hub weather")
    _bs.set_notes(root, session["id"], "rain on the hub\n- tint\n- sound")
    _bs.set_drawing(root, session["id"], SCENE)
    monkeypatch.setenv("BGATE_ROOT", str(root))
    monkeypatch.setenv("BGATE_BRAINSTORM_SESSION", str(session["id"]))
    return {"root": root, "id": int(session["id"])}


class TestTheSurfaceIsTwoTools:
    def test_exactly_two_and_they_match_the_allowlist(self):
        """A third tool must not be able to arrive already approved.

        runners.py names the two by their full CLI-side names rather than by
        the `mcp__pads` prefix, so the allowlist cannot silently widen — but
        that only holds while the two lists agree, which is what this checks.
        """
        assert sorted(padserver.TOOL_NAMES) == sorted(_runners.PAD_TOOLS)
        assert len(padserver.TOOL_NAMES) == 2

    def test_the_module_reaches_nothing_else(self):
        """No queue, no repo, no generators, no subprocess — by import, not by
        intention. The big MCP server is ~150 tools including queue_add,
        blender_run and real-money generators; the answer to "the partner needs
        to see the pads" was a small server rather than a filtered big one."""
        import inspect

        source = inspect.getsource(padserver)
        for banned in ("queue", "subprocess", "blender", "image_generate",
                       "godot", "shutil", "requests"):
            assert banned not in source.lower(), banned

    def test_the_config_registers_this_server_and_only_this_server(self, pad):
        cfg = padserver.config(str(pad["root"]), pad["id"])
        assert set(cfg["mcpServers"]) == {"pads"}
        entry = cfg["mcpServers"]["pads"]
        assert entry["args"] == ["-m", "bgate_mcp.padserver"]
        # The session travels in the ENVIRONMENT, not as a tool argument: a
        # model that states which room it is in can state a different one.
        assert entry["env"]["BGATE_BRAINSTORM_SESSION"] == str(pad["id"])


class TestReading:
    def test_it_reads_both_pads(self, pad):
        got = padserver.pad_read()
        assert got["notes"].startswith("rain on the hub")
        # The drawing arrives as READABLE LINES, which is the whole reason the
        # scene is stored as elements rather than as a PNG.
        assert '"hub"' in got["drawing_text"]
        assert [e["id"] for e in got["elements"]] == ["hub-1", "note-1"]
        assert got["rev"]

    def test_no_session_in_the_environment_is_an_error_not_a_guess(
            self, pad, monkeypatch):
        monkeypatch.delenv("BGATE_BRAINSTORM_SESSION")
        got = padserver.pad_read()
        assert got["ok"] is False and "session id" in got["error"]


class TestDrawing:
    def test_it_adds_without_removing_anything(self, pad):
        got = padserver.pad_draw([
            {"id": "shrine-1", "type": "rectangle", "x": 320, "y": 20,
             "width": 120, "height": 60, "text": "shrine"},
            {"id": "a1", "type": "arrow",
             "startBinding": {"elementId": "hub-1"},
             "endBinding": {"elementId": "shrine-1"}},
        ])
        assert got["added"] == 2 and got["amended"] == 0
        after = _bs.get(pad["root"], pad["id"])["drawing"]
        ids = [e["id"] for e in _bs.elements(after)]
        # The human's own elements are still there, still in their order — the
        # order is z-order, and reshuffling somebody's diagram every time the
        # partner touches it looks exactly like a bug.
        assert ids == ["hub-1", "note-1", "shrine-1", "a1"]
        assert "hub-1 -> shrine-1" in got["drawing_text"]

    def test_a_known_id_amends_that_one_shape_and_only_that_one(self, pad):
        got = padserver.pad_draw([
            {"id": "hub-1", "type": "rectangle", "x": 10, "y": 20,
             "width": 120, "height": 60, "text": "the hub"}])
        assert got["amended"] == 1 and got["added"] == 0
        after = _bs.elements(_bs.get(pad["root"], pad["id"])["drawing"])
        assert [e["id"] for e in after] == ["hub-1", "note-1"]
        assert after[0]["text"] == "the hub"

    def test_a_stale_rev_is_refused_rather_than_landed_on_top_of(self, pad):
        """The human may be drawing at the moment the partner writes.

        Merging by id means nothing of theirs is ever dropped; `rev` is the
        second defence, for the partner that read, thought for twenty seconds
        and is about to describe a board that has moved. Refusal comes back
        WITH the current drawing so it re-reads instead of guessing.
        """
        stale = padserver.pad_read()["rev"]
        _bs.set_drawing(pad["root"], pad["id"], {"elements": [
            *SCENE["elements"],
            {"id": "human-2", "type": "ellipse", "x": 0, "y": 400,
             "width": 40, "height": 40, "text": "mine"}]})
        got = padserver.pad_draw(
            [{"id": "x1", "type": "rectangle", "x": 0, "y": 0}], rev=stale)
        assert got["ok"] is False
        assert "changed while you were thinking" in got["error"]
        assert "mine" in got["drawing_text"]
        # Nothing was written.
        ids = [e["id"] for e in _bs.elements(
            _bs.get(pad["root"], pad["id"])["drawing"])]
        assert "x1" not in ids

    def test_a_current_rev_goes_through(self, pad):
        got = padserver.pad_draw(
            [{"id": "x1", "type": "rectangle", "x": 0, "y": 0}],
            rev=padserver.pad_read()["rev"])
        assert got.get("ok") is not False and got["added"] == 1

    def test_an_element_with_no_type_is_dropped_not_fatal(self, pad):
        """Lenient in one direction only. A missing width is a correctable
        mistake and refusing the whole call over it costs a turn to learn
        something the default could have said; a shape whose KIND nobody knows
        renders as nothing, which would read as the tool silently failing."""
        got = padserver.pad_draw([
            {"id": "ok-1", "type": "rectangle"},
            {"id": "bad-1"},
            "not an object",
        ])
        assert got["added"] == 1
        assert "ok-1" in got["drawing_text"]

    def test_nothing_drawable_is_an_error_with_a_reason(self, pad):
        got = padserver.pad_draw([{"id": "no-type"}])
        assert got["ok"] is False and "id and a type" in got["error"]

    def test_the_writing_pad_cannot_be_written(self):
        """The one pad the partner may only read.

        It is the human's own document — an hour of their typing — and the pad
        is a WHOLE-DOCUMENT write, so a model that read a stale copy and sent it
        back deletes everything they added in between. brainstorm_note on the
        big server is the door for that, and it is a door a human opens.
        """
        names = {t for t in dir(padserver) if t.startswith("pad_")}
        assert names == {"pad_read", "pad_draw"}
        import inspect

        assert "set_notes" not in inspect.getsource(padserver)
