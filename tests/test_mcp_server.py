"""The MCP surface, exercised the way a client hits it.

Tools are called through the registered handler (not the plain function) so this
covers schema generation and dispatch, and asserts the contract the model sees:
errors come back as an "error" payload, never as a raised exception.
"""
from __future__ import annotations

import json

import pytest

from bgate_core import seats
from bgate_mcp import server


@pytest.fixture()
def wired(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return root


async def call(tool: str, /, **kwargs) -> dict:
    """Dispatch through FastMCP and decode the payload a client would receive.

    ``tool`` is positional-only — tools have their own 'name' argument.
    """
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


class TestInstructions:
    """The session-level brief the client loads before the agent does anything.

    This is the only channel that survives a change of directory: the server is
    registered `--scope user`, so it is delivered in every session on the machine
    regardless of which project (or none) the cwd is in. Everything else the
    pipeline uses to explain itself is conditional — the CLAUDE.md block needs a
    stamped project you are standing in, seat_brief needs the agent to think to
    call it, and the dispatch prompt only exists for agents the dashboard spawned.
    """

    def test_the_server_actually_ships_instructions(self):
        # The whole regression: FastMCP("builders-gate") with no instructions=
        # left a top-level session staring at ~150 tool names with no statement
        # that seats, the board or the QA gate existed.
        assert server.mcp.instructions
        assert len(server.mcp.instructions) > 200

    def test_a_seatless_session_is_told_it_holds_the_director_seat(self):
        text = seats.director_instructions("")
        # The DIRECTOR seat, by name — not a parallel role invented for the
        # top-level session. qa_gate escalates to it and routes/orchestrator.py
        # is built around it; a second name for the same job is how two
        # descriptions of one role start disagreeing.
        assert "DIRECTOR SEAT" in text
        assert "queue_add" in text
        # The trap this text exists to close: a row on a dead board reads exactly
        # like delegated work and is not.
        assert "bgate serve" in text
        assert "qa" in text.lower()

    def test_the_director_mission_is_read_not_retyped(self, root):
        """A project that rewrites its director mission rewrites this brief.

        An earlier draft hardcoded the remit inline, which would have drifted
        from the seat table the moment anyone customised a director. This is the
        assertion that keeps it derived.
        """
        assert seats.DEFAULT_SEATS["director"]["mission"] in \
            seats.director_instructions("", root)
        seats.configure(root, "director",
                        mission="Ship the slice. Say no twice a day.")
        after = seats.director_instructions("", root)
        assert "Ship the slice. Say no twice a day." in after
        assert seats.DEFAULT_SEATS["director"]["mission"] not in after

    def test_no_project_and_broken_project_both_degrade_to_the_default(self, tmp_path):
        """A server may legitimately boot outside any project, and must not die.

        `instructions` is computed at import; an exception here is a server that
        never starts, which costs the session every tool rather than one string.
        """
        default = seats.DEFAULT_SEATS["director"]["mission"]
        assert default in seats.director_instructions("", None)
        assert default in seats.director_instructions("", tmp_path / "nope")

    def test_a_seat_worker_is_not_told_it_is_the_director(self):
        text = seats.director_instructions("art")
        assert "YOU HOLD THE DIRECTOR SEAT" not in text
        assert "seat worker" in text
        assert "seat_brief('art')" in text
        # It already received SEAT_IDENTITY in its task prompt; re-sending the
        # whole thing here would spend context repeating what it has been told.
        assert len(text) < len(seats.DIRECTOR_PROTOCOL)

    def test_the_two_identities_do_not_contradict_each_other(self):
        director = seats.director_instructions("")
        # A seatless session must be able to tell which of the two applies to it,
        # so the director text names the variable that would make it the other.
        assert "BGATE_SEAT" in director
        assert "you are NOT the director" in director


@pytest.mark.anyio
async def test_tools_are_registered():
    names = {t.name for t in await server.mcp.list_tools()}
    assert {
        "project_init", "project_status", "bible_add", "bible_update", "bible_read",
        "lore_add", "lore_update", "lore_brief", "lore_list",
        "lore_link", "lore_fact", "canon_check", "recall",
    } <= names


@pytest.mark.anyio
async def test_every_tool_has_a_description():
    for tool in await server.mcp.list_tools():
        assert tool.description and len(tool.description) > 30, tool.name


@pytest.mark.anyio
async def test_full_authoring_flow(wired):
    assert (await call("bible_add", kind="pillar", title="Tension over spectacle"))["id"] > 0
    await call("bible_add", kind="loop", title="Core loop", rank=1)

    view = await call("bible_read")
    assert [s["title"] for s in view["loop"]] == ["Core loop"]

    await call("lore_add", kind="faction", name="The Ashen Order", status="canon")
    await call("lore_fact", ref="The Ashen Order",
               statement="The Ashen Order worships the flame.", locked=True)

    status = await call("project_status")
    assert status["counts"] == {"bible_sections": 2, "entities": 1,
                                "canon_entities": 1, "facts": 1, "links": 0}

    assert (await call("recall", query="flame"))["results"]

    clean = await call("canon_check", text="The Ashen Order worships the flame.")
    assert clean["verdict"] == "ok"
    broken = await call("canon_check", text="The Ashen Order does not worship the flame.")
    assert broken["verdict"] == "conflict"


@pytest.mark.anyio
async def test_errors_return_payload_not_raise(wired):
    assert "error" in await call("lore_brief", ref="nobody-here")
    assert "error" in await call("bible_add", kind="vibes", title="x")


@pytest.mark.anyio
async def test_missing_project_explains_itself(tmp_path, monkeypatch):
    monkeypatch.delenv("BGATE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    got = await call("project_status")
    assert "error" in got and "project_init" in got["error"]


@pytest.mark.anyio
async def test_handoff_tools_round_trip(wired):
    """The thread through the MCP surface a client actually calls."""
    assert (await call("handoff_note", kind="decision", text="chose the 0 PTO price",
                       refs=["bible#12"]))["kind"] == "decision"
    await call("handoff_note", kind="deferred", text="archive pays nothing on purpose")
    got = await call("handoff_read")
    assert got["count"] == 2
    assert [n["kind"] for n in got["notes"]] == ["decision", "deferred"]
    only = await call("handoff_read", kind="deferred")
    assert only["count"] == 1


@pytest.mark.anyio
async def test_handoff_rejects_a_bad_kind_as_a_payload(wired):
    # The contract this whole server keeps: a failure is a fact the model can
    # act on, not a raised exception that reads as a broken server.
    got = await call("handoff_note", kind="vibes", text="x")
    assert got.get("error") and got.get("ok") is False


@pytest.mark.anyio
class TestQueueReopenCountsRounds:
    """queue_reopen used to bypass queue.reopen, so ``attempts`` stayed 0
    forever — and attempts is the round counter the QA gate's max-rounds cap
    reads, so the fail/reopen loop the cap exists for could never trip it."""

    async def test_reopen_increments_attempts_and_appends_the_reason(self, wired):
        from bgate_core import queue as _q
        item = _q.add(wired, "tech", "verify the door")
        _q.set_status(wired, item["id"], "done", result="looked fine")
        got = await call("queue_reopen", item_id=item["id"],
                         reason="the hinge is backwards")
        assert got["status"] == "queued"
        assert int(got["attempts"]) == 1
        assert "hinge is backwards" in got["brief"]

    async def test_reopen_refuses_a_running_item(self, wired):
        from bgate_core import queue as _q
        item = _q.add(wired, "tech", "in flight")
        _q.reserve(wired, item["id"])
        got = await call("queue_reopen", item_id=item["id"], reason="too soon")
        assert "error" in got and "dispatched" in got["error"]


@pytest.mark.anyio
class TestQueueClaimNext:
    """The pickup loop, at the surface a worker actually calls."""

    async def test_a_session_with_no_work_item_may_not_claim(self, wired, monkeypatch):
        monkeypatch.delenv("BGATE_SEAT", raising=False)
        monkeypatch.delenv("BGATE_WORK_ITEM", raising=False)
        got = await call("queue_claim_next")
        assert "error" in got and "pickup loop" in got["error"]

    async def test_a_worker_claims_for_its_own_seat_with_its_run_identity(
            self, wired, monkeypatch):
        from bgate_core import queue as _q
        monkeypatch.setenv("BGATE_SEAT", "art")
        monkeypatch.setenv("BGATE_WORK_ITEM", "41")
        _q.add(wired, "tech", "not art's problem to claim")
        target = _q.add(wired, "art", "paint the next thing")
        got = await call("queue_claim_next")
        assert got.get("claimed") is True
        assert got["id"] == target["id"]
        assert got["actor"] == "agent:item-41"
        # And the well runs dry honestly: nothing ready means finish the shift.
        again = await call("queue_claim_next")
        assert again.get("empty") is True


class TestRawPathContainment:
    """The gate the raw-path tools lacked: godot_project/out_path arguments
    now pass through _contained_path, which puts the TARGET's own project
    through the same aegis decision project_dir faces. These test the wiring
    (the seated refusal itself is aegis's, tested with _contained)."""

    def test_res_pair_asks_containment_about_the_godot_project(
            self, root, monkeypatch):
        from bgate_mcp import server

        asked = {}

        def spy(target, what="path"):
            asked["target"] = str(target)
            return target

        monkeypatch.setattr(server, "_contained_path", spy)
        gd = root / "game"
        gd.mkdir(exist_ok=True)
        (gd / "project.godot").write_text("", encoding="utf-8")
        server._res_pair(str(gd), "scenes/x.tscn", ".tscn")
        assert asked["target"] == str(gd)

    def test_a_refusal_travels_out_as_the_tools_error(self, root, monkeypatch):
        from bgate_mcp import server

        def refuse(target, what="path"):
            raise server.ContainmentRefused("not your tree")

        monkeypatch.setattr(server, "_contained_path", refuse)
        gd = root / "game"
        gd.mkdir(exist_ok=True)
        (gd / "project.godot").write_text("", encoding="utf-8")
        import pytest as _pytest

        with _pytest.raises(server.ContainmentRefused):
            server._res_pair(str(gd), "scenes/x.tscn", ".tscn")

    def test_seatless_sessions_pass_untouched(self, root, monkeypatch):
        from bgate_mcp import server

        monkeypatch.delenv("BGATE_SEAT", raising=False)
        assert server._contained_path(str(root)) == str(root)
