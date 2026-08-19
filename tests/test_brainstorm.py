"""The brainstorm room, and the four properties that make it worth having.

This feature is defined by what it does NOT do, so that is what is tested here.

CHEAP. A message writes two rows and takes one turn with a thinking partner. If
it ever creates a work item, dispatches an agent or reaches the queue at all,
the room has become the console with a different title and the whole reason it
exists is gone. That is not a matter of nobody adding the wrong line later — the
module has no queue attribute outside ``deploy``, and the partner is a process
built with no tools in it — and both of those are asserted, because a structural
guarantee nobody checks decays into a comment.

THE PARTNER CHANGED AND SO DID THE PROOF. It used to be a bare OpenAI
chat-completions call, and the test here asserted that a ``tools=`` argument was
dropped by a kwarg allowlist. The partner is now a real Claude Code session —
the same CLI the board dispatches — so the old assertion is not merely stale, it
would pass while describing nothing: there is no kwarg to drop. What is asserted
instead is the ARGV the session is spawned with, which removes the capability
rather than declining to pass it. That is a stronger claim about a bigger risk,
and it is the one that has to hold now.

PREVIEW THEN CONFIRM. ``synthesize`` writes nothing at all: not an item, not the
session's status, not the plan. ``deploy`` files exactly the plan it was handed
and nothing else. A deploy that re-asked the model would file something no human
read, which would make the confirmation step theatre.

ORDER SURVIVES. A chained plan has to arrive on the board as a chain — chain_id,
chain_pos and depends_on — because priority is a preference among ready items
and would let the second agent start before the first produced what it needs.

FILEABLE. A session round-trips whole, drawing scene included. The scene is
stored as ELEMENTS rather than a picture, so the test asserts the elements come
back, not that some bytes did.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from bgate_core import brainstorm as _bs
from bgate_core import bible as _bible
from bgate_core import db, lore as _lore
from bgate_ui import runners as _runners
from bgate_ui.app import app
from bgate_ui.routes import brainstorm as _route

SCENE = {
    "type": "excalidraw",
    "elements": [
        {"id": "hub-1", "type": "rectangle", "x": 10, "y": 20,
         "width": 120, "height": 60, "text": "hub"},
        {"id": "shrine-1", "type": "rectangle", "x": 300, "y": 20,
         "width": 120, "height": 60, "label": {"text": "shrine"}},
        {"id": "arrow-1", "type": "arrow",
         "startBinding": {"elementId": "hub-1"},
         "endBinding": {"elementId": "shrine-1"}, "text": "walk"},
    ],
    "appState": {"viewBackgroundColor": "#ffffff"},
}


@pytest.fixture(autouse=True)
def no_cli(monkeypatch):
    """NO TEST IN THIS FILE MAY SPAWN A CLI. Autouse, and that is the point.

    The partner used to be an HTTP call gated on a key the CI environment did
    not have, so "this test does not reach a model" was true by accident. It is
    now a subprocess, and every machine that runs this suite HAS the claude CLI
    installed — that is what the product is for. Without this fixture, a test
    that forgets to stub ``_ask`` would quietly spawn a real session, bill a
    real turn and take a real minute.

    Neutralising the LOOKUP rather than the spawn is deliberate: it exercises
    the same "no partner here" path a user without the CLI meets, so the tests
    that assert that path are asserting the real one.
    """
    monkeypatch.setattr(_runners, "find_claude", lambda: None)
    monkeypatch.setattr(_runners, "find_codex", lambda: None)


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


@pytest.fixture()
def no_agents(monkeypatch):
    """Any dispatch at all is a test failure, not a mocked-out side effect."""
    def boom(*a, **kw):
        raise AssertionError("the brainstorm room dispatched an agent")

    monkeypatch.setattr("bgate_ui.dispatch.dispatch", boom)
    monkeypatch.setattr("bgate_ui.dispatch.kill_all", boom)


@pytest.fixture()
def answers(monkeypatch):
    """Stand in for the model. Records what it was asked, returns what is next."""
    box: dict = {"replies": [], "asked": []}

    def fake(project, system, turns, **kw):
        box["asked"].append({"system": system, "turns": turns, **kw})
        text = box["replies"].pop(0) if box["replies"] else "sure, go on"
        return {"ok": True, "text": text, "model": "test",
                "seconds": 0.0, "estimated_usd": 0.0}

    monkeypatch.setattr(_route, "_ask", fake)
    return box


def _bs_log(root, session_id: int):
    """Where a session's raw NDJSON lives — the terminal channel's backing."""
    from bgate_ui import brainsession

    return brainsession.log_path(root, session_id)


def _items(root) -> list[dict]:
    return [dict(r) for r in db.connect(root).execute(
        "SELECT * FROM work_item ORDER BY id")]


def _new(client, seat="director", title="") -> dict:
    got = client.post("/api/brainstorm", json={"seat": seat, "title": title})
    assert got.status_code == 200, got.text
    return got.json()["data"]


def _plan(*items, chained=False, summary="a plan") -> str:
    return json.dumps({"summary": summary, "chained": chained,
                       "items": list(items)})


# ---------------------------------------------------------------------------
# A message is a message
# ---------------------------------------------------------------------------

def _quiet(*_args, **_kwargs) -> None:
    """Wait until no room is mid-round.

    A message RETURNS as soon as it is stored now: the seats take their turns on
    a thread and the transcript fills in as each one answers, because holding the
    request open for a four-seat round meant minutes of a spinner and a composer
    nobody could type into. The tests want the finished state, so they wait for
    it here — against the route's own in-flight set rather than a sleep, which
    is both faster and not a guess.

    Takes any arguments and ignores them: the call sites differ in what they
    have to hand, and none of them needs to say WHICH room when the suite only
    ever has one talking.
    """
    import time as _time

    for _ in range(500):
        with _route._answering_lock:
            if not _route._answering:
                return
        _time.sleep(0.01)
    raise AssertionError("a brainstorm round never finished")


@pytest.fixture(autouse=True)
def _no_round_outlives_its_test():
    """Wait for any in-flight brainstorm round before the next test starts.

    The round runs on a thread now, so without this a test that sent a message
    can finish while its seats are still answering — and the thread then calls
    the NEXT test's stubbed `_ask`, writes into the next test's fixtures, and
    fails something that had nothing to do with it. Draining here rather than in
    each test is what makes that impossible to forget.
    """
    yield
    _quiet()

class TestAMessageCannotDispatch:
    def test_no_work_item_and_no_agent(self, client, root, answers, no_agents):
        session = _new(client)
        for text in ("what if the hub had weather", "and a shrine"):
            got = client.post(f"/api/brainstorm/{session['id']}/message",
                              json={"text": text})
            _quiet()
            assert got.status_code == 200, got.text
        assert _items(root) == []

    def test_both_sides_of_the_turn_are_stored(self, client, answers, no_agents):
        session = _new(client)
        answers["replies"].append("weather is a mood, not a mechanic")
        client.post(f"/api/brainstorm/{session['id']}/message",
                    json={"text": "what if the hub had weather"})
        _quiet()
        read = client.get(f"/api/brainstorm/{session['id']}").json()["data"]
        assert [(m["role"], m["text"]) for m in read["messages"]] == [
            ("user", "what if the hub had weather"),
            ("assistant", "weather is a mood, not a mechanic"),
        ]

    def test_the_message_survives_a_dead_partner(self, client, root, no_agents):
        """No CLI, no reply — and the human's sentence is still there.

        The 'ask, then save both' ordering loses what somebody typed to a
        partner that would not start, which is the worst outcome available on
        this endpoint. The cause moved (it used to be a missing API key and it
        is now a missing CLI) and the property did not.
        """
        session = _new(client)
        got = client.post(f"/api/brainstorm/{session['id']}/message",
                          json={"text": "keep this"})
        _quiet()
        assert got.status_code == 200, got.text
        # The POST no longer carries the answer: the round runs on a thread and
        # the transcript is how it arrives. What this test is about survives
        # that unchanged — the sentence is stored whether or not anything
        # answers it, which is the property "ask, then save both" got wrong.
        assert got.json()["data"]["message"]["text"] == "keep this"
        read = client.get(f"/api/brainstorm/{session['id']}").json()["data"]
        assert [m["text"] for m in read["messages"]] == ["keep this"]
        assert _items(root) == []

    def test_the_chat_path_cannot_see_the_queue(self):
        """Structural, not a promise: `queue` is imported inside deploy only."""
        assert not hasattr(_route, "_queue")
        assert not hasattr(_bs, "queue")

    def test_the_partner_is_spawned_without_the_capability(self):
        """THE NO-WRITE GUARANTEE, asserted where it now lives.

        This replaces the kwarg-allowlist test. That one checked that a `tools=`
        argument to a chat-completions call was dropped; there is no such call
        any more, and the risk is a hundred times larger — the partner is the
        same CLI the board dispatches, which arrives holding Write, Edit and
        Bash and inherits every MCP server registered on the machine, including
        builders-gate and its queue_add.

        So the argv is the assertion. Each flag below was checked against the
        CLI's own `system/init` event, which reports the tool list and the MCP
        servers the process actually built: it said `"tools":[]`,
        `"mcp_servers":[]` and `"slash_commands":[]`, and a session asked to
        write a file wrote none.
        """
        argv = _runners.RUNNERS["claude"].chat.build_args(
            "claude", system="think with me", model="sonnet", max_usd=2.0)
        pairs = list(zip(argv, argv[1:]))

        # The BUILT-IN tool set is EMPTY. Not allowlisted — empty. No Write, no
        # Edit, no Bash, no Read.
        assert ("--tools", "") in pairs
        # No MCP server inherited from the user's own config. This is the flag
        # that matters most on a developer machine: builders-gate is registered
        # at user scope, so without it a session spawned in a game project would
        # hold queue_add. With no --mcp-config passed there is no server at all.
        assert "--strict-mcp-config" in argv
        assert "--mcp-config" not in argv
        assert not [a for a in argv if "mcp_servers." in str(a)]
        # Settings cannot add tools, permissions, hooks or MCP servers back.
        assert ("--setting-sources", "") in pairs
        # A brainstorm message is arbitrary human text; one starting with "/"
        # must not be able to run a skill.
        assert "--disable-slash-commands" in argv
        # And none of the dispatch flags that grant an agent its lanes. Note
        # --allowedTools appears ONLY alongside a pad config; see the pad test.
        assert "--allowedTools" not in argv
        assert "--dangerously-skip-permissions" not in argv

    def test_the_pad_server_is_the_only_thing_handed_back(self):
        """WITH pads, the surface is exactly three tools — and nothing else.

        The room reversed its "no MCP server at all" position for one reason:
        a partner that cannot see the diagram the human is drawing beside it is
        answering with one eye shut. It did NOT reverse it by letting the
        builders-gate server through. --mcp-config names a dedicated three-tool
        server, --strict-mcp-config makes that list exhaustive, and
        --allowedTools names those same three by their full names rather than by
        the `mcp__pads` prefix, so the approval cannot widen if the server ever
        grows a fourth tool.
        """
        from bgate_mcp import padconfig, padserver

        cfg = json.dumps(padserver.config("/tmp/game", 7))
        argv = _runners.RUNNERS["claude"].chat.build_args(
            "claude", system="s", model="sonnet", mcp_config=cfg)
        pairs = list(zip(argv, argv[1:]))
        assert ("--mcp-config", cfg) in pairs
        assert "--strict-mcp-config" in argv
        assert ("--tools", "") in pairs

        start = argv.index("--allowedTools") + 1
        allowed = argv[start:start + len(_runners.PAD_TOOLS)]
        assert sorted(allowed) == sorted(_runners.PAD_TOOLS)
        # The prefix form a dispatched agent uses would approve whatever the
        # server grows. This one names them individually.
        assert "mcp__pads" not in argv

        # The registration and the server cannot drift apart because THEY ARE
        # THE SAME OBJECT. This used to compare two hand-maintained copies:
        # runners could not import the real tuple, because padserver builds a
        # FastMCP application at import time and runners is loaded by every
        # dispatch, including where the MCP extra is absent. Both now read
        # bgate_mcp.padconfig, which imports nothing but sys. Asserting
        # identity rather than equality is the point — an equality check would
        # pass again the moment somebody reintroduced the duplicate.
        assert _runners.PAD_TOOLS is padserver.TOOL_NAMES
        assert padserver.TOOL_NAMES is padconfig.TOOL_NAMES
        assert set(json.loads(cfg)["mcpServers"]) == {"pads"}
        assert json.loads(cfg)["mcpServers"]["pads"]["args"] == [
            "-m", "bgate_mcp.padserver"]

    def test_plan_mode_is_gone_and_why(self):
        """--permission-mode plan REFUSED THE PAD TOOLS, so it had to go.

        Recorded as a test rather than only as a comment because the flag looks
        free and the next reader will want it back. Measured: a session holding
        exactly mcp__pads__pad_read answered "I'm currently in plan mode, which
        blocks me from calling tools other than writing to the plan file —
        including the read-only pad_read call you asked for". Shipping it would
        have meant a two-tool server that could never be called, which reads as
        the model being unhelpful rather than as a bug.

        Nothing was lost by removing it: plan mode was never what made the room
        safe. The empty built-in tool set and the exhaustive MCP config are, and
        both are asserted above.
        """
        argv = _runners.RUNNERS["claude"].chat.build_args(
            "claude", system="s", model=None)
        assert "plan" not in argv
        # And session persistence must stay ON, or --resume has nothing to
        # resume and "seamless" becomes a replay every time.
        assert "--no-session-persistence" not in argv

    def test_resume_is_passed_when_there_is_a_session_to_resume(self):
        """Reopening a brainstorm CONTINUES the CLI session rather than
        replaying a transcript into a blank one."""
        argv = _runners.RUNNERS["claude"].chat.build_args(
            "claude", system="s", model=None, resume="abc-123")
        assert list(zip(argv, argv[1:])).count(("--resume", "abc-123")) == 1
        plain = _runners.RUNNERS["claude"].chat.build_args(
            "claude", system="s", model=None)
        assert "--resume" not in plain

    def test_a_runner_with_no_readonly_mode_is_refused_not_improvised(
            self, root, monkeypatch):
        """The room may only run on a runner that has DECLARED how it is kept
        read-only. Anything else is refused rather than started with the
        dispatch flags — which is what keeps "expand later for codex, and local
        llms" one table entry rather than one way to lose the guarantee."""
        assert _runners.RUNNERS["claude"].chat is not None
        assert _runners.RUNNERS["claude"].chat.readonly_by
        assert _runners.RUNNERS["codex"].chat is None

        monkeypatch.setenv("BGATE_BRAINSTORM_RUNNER", "codex")
        ready = _bs.available(root)
        assert ready["available"] is False
        assert ready["runner"] == "codex"
        assert "read-only" in ready["reason"]
        # And it refuses rather than falling through to some other runner.
        answered = _bs.ask(root, "sys", [{"role": "user", "content": "hi"}],
                           session_id=1)
        assert answered["ok"] is False and "read-only" in answered["error"]

    def test_a_message_is_a_turn_in_ONE_session(self, client, answers,
                                                no_agents):
        """The thing the human asked for: a spawned session that persists.

        Every message carries the session id and asks for the persistent room,
        so message two is turn two of the same process rather than a fresh
        partner reading a transcript. A synthesis is the deliberate exception —
        one question, a different system prompt, and its JSON must not land in
        the middle of the conversation.
        """
        session = _new(client)
        sid = session["id"]
        client.post(f"/api/brainstorm/{sid}/message", json={"text": "one"})
        client.post(f"/api/brainstorm/{sid}/message", json={"text": "two"})
        _quiet()
        assert [a.get("session_id") for a in answers["asked"]] == [sid, sid]
        assert all(a.get("persist", True) for a in answers["asked"])

        answers["replies"].append(_plan())
        client.post(f"/api/brainstorm/{sid}/synthesize")
        assert answers["asked"][-1]["session_id"] == sid
        assert answers["asked"][-1]["persist"] is False

    def test_an_empty_message_is_refused(self, client, answers, no_agents):
        session = _new(client)
        got = client.post(f"/api/brainstorm/{session['id']}/message",
                          json={"text": "   "})
        _quiet()
        assert got.status_code == 400
        assert got.json()["ok"] is False


# ---------------------------------------------------------------------------
# Round trip, including the drawing
# ---------------------------------------------------------------------------
class TestFileableAndRetrievable:
    def test_a_session_round_trips_whole(self, client, no_agents):
        session = _new(client, title="hub weather")
        sid = session["id"]
        client.patch(f"/api/brainstorm/{sid}",
                     json={"notes": "rain on the hub\n- tint\n- sound"})
        got = client.patch(f"/api/brainstorm/{sid}",
                           json={"drawing": SCENE, "drawing_png": "art/pad.png"})
        assert got.status_code == 200, got.text

        db.close_all()  # from disk, not from a cache
        read = client.get(f"/api/brainstorm/{sid}").json()["data"]
        assert read["title"] == "hub weather"
        assert read["notes"].startswith("rain on the hub")
        assert read["drawing"] == SCENE
        assert read["drawing_png"] == "art/pad.png"
        assert [e["id"] for e in read["drawing"]["elements"]] == [
            "hub-1", "shrine-1", "arrow-1"]

    def test_the_drawing_is_readable_as_words(self, client, no_agents):
        """The point of storing elements: a text model can read the board."""
        digest = _bs.drawing_digest(SCENE)
        assert '"hub"' in digest
        assert '"shrine"' in digest          # a bound label, not an element.text
        assert "hub-1 -> shrine-1" in digest

    def test_a_corrupt_scene_does_not_lock_the_session(self, client, root):
        session = _new(client)
        with db.tx(root) as conn:
            conn.execute("UPDATE brainstorm_session SET drawing_json = ? "
                         "WHERE id = ?", ("{not json", session["id"]))
        read = client.get(f"/api/brainstorm/{session['id']}").json()["data"]
        assert read["drawing"] == {}

    def test_rename_and_list(self, client, no_agents):
        first = _new(client, title="one")
        _new(client, seat="narrative", title="two")
        client.patch(f"/api/brainstorm/{first['id']}", json={"title": "renamed"})
        listed = client.get("/api/brainstorm").json()["data"]["sessions"]
        assert {s["title"] for s in listed} == {"renamed", "two"}
        # The index never ships the pads — it is polled.
        assert "notes" not in listed[0] and "drawing" not in listed[0]
        narrative = client.get("/api/brainstorm?seat=narrative"
                               ).json()["data"]["sessions"]
        assert [s["title"] for s in narrative] == ["two"]

    def test_archive_keeps_everything_and_reopens(self, client, answers,
                                                  no_agents):
        session = _new(client)
        sid = session["id"]
        client.post(f"/api/brainstorm/{sid}/message", json={"text": "an idea"})
        assert client.post(f"/api/brainstorm/{sid}/archive").json(
        )["data"]["status"] == "archived"

        read = client.get(f"/api/brainstorm/{sid}").json()["data"]
        assert read["status"] == "archived"
        assert len(read["messages"]) == 2      # archiving deletes nothing

        blocked = client.post(f"/api/brainstorm/{sid}/message",
                              json={"text": "more"})

        _quiet()
        assert blocked.status_code == 409

        back = client.post(f"/api/brainstorm/{sid}/archive",
                           json={"archived": False}).json()["data"]
        assert back["status"] == "open"

    def test_archived_sessions_sort_last(self, client, no_agents):
        old = _new(client, title="filed")
        client.post(f"/api/brainstorm/{old['id']}/archive")
        _new(client, title="current")
        listed = client.get("/api/brainstorm").json()["data"]["sessions"]
        assert [s["title"] for s in listed] == ["current", "filed"]

    def test_delete_removes_the_session_and_its_messages(self, client, root,
                                                         answers, no_agents):
        session = _new(client)
        sid = session["id"]
        client.post(f"/api/brainstorm/{sid}/message", json={"text": "an idea"})
        assert client.delete(f"/api/brainstorm/{sid}").status_code == 200
        assert client.get(f"/api/brainstorm/{sid}").status_code == 404
        left = db.connect(root).execute(
            "SELECT count(*) AS n FROM brainstorm_message").fetchone()["n"]
        assert left == 0

    def test_a_missing_session_is_a_404(self, client):
        assert client.get("/api/brainstorm/9999").status_code == 404

    def test_closing_the_partner_keeps_everything_it_said(self, client, root,
                                                          answers, no_agents):
        """CLOSE IS ABOUT THE PROCESS. Three end-states that all sound final is
        worse than one, so this pins which is which.

        There was no user-facing way to end a partner at all: it stopped on a
        30-minute idle reap, an LRU eviction or the kill switch, all invisible
        from the browser. "Confidently" was the operative word in the request.
        """
        session = _new(client)
        sid = session["id"]
        client.post(f"/api/brainstorm/{sid}/message", json={"text": "an idea"})
        got = client.post(f"/api/brainstorm/{sid}/close")
        _quiet()
        assert got.status_code == 200, got.text
        body = got.json()["data"]
        assert body["thinker"]["live"] is False
        # Nothing about the DOCUMENT moved: not the status, not the transcript.
        read = client.get(f"/api/brainstorm/{sid}").json()["data"]
        assert read["status"] == "open"
        assert [m["text"] for m in read["messages"]][0] == "an idea"
        # Idempotent — pressing it twice must not be an error.
        assert client.post(f"/api/brainstorm/{sid}/close").status_code == 200
        # And it is not archive: the session still takes new turns.
        assert client.post(f"/api/brainstorm/{sid}/message",
                           json={"text": "and another"}).status_code == 200

    def test_archiving_closes_the_partner_too(self, client, monkeypatch,
                                              answers, no_agents):
        """A room nobody may speak in must not still be paying someone to listen.

        Archiving used to leave the process running until an idle reap noticed
        half an hour later, which is exactly the invisible state the close
        button exists to eliminate.
        """
        from bgate_ui import brainsession

        stopped: list = []
        monkeypatch.setattr(brainsession, "stop",
                            lambda root, sid: stopped.append(int(sid)) or
                            {"ok": True, "stopped": True})
        session = _new(client)
        client.post(f"/api/brainstorm/{session['id']}/archive")
        assert stopped == [session["id"]]

    def test_the_feed_reads_the_raw_session_log(self, client, root, no_agents):
        """A DEBUGGING channel, not a view. NOTHING IN THE UI RENDERS THIS.

        A pane that showed these events was built and removed: asked for "the
        actual terminal claude code embedded", a rendering of stream-json would
        have been a different thing wearing the right label. The endpoint stays
        because it answers a question nothing else can — what did the process
        actually do, and (from its own `init` event) what tools did it really
        hold. That last one is the only account of the room's promise that is
        not the model's own, and the model was caught lying about it once.
        """
        session = _new(client)
        sid = session["id"]
        log = _bs_log(root, sid)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("\n".join([
            json.dumps({"type": "bgate_brainstorm_start", "resumed": True}),
            json.dumps({"type": "system", "subtype": "init",
                        "session_id": "abcdef123456", "model": "sonnet",
                        "tools": ["mcp__pads__pad_read", "mcp__pads__pad_draw"]}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "mcp__pads__pad_read", "input": {}}]}}),
            json.dumps({"type": "result", "subtype": "success",
                        "result": "the hub needs weather",
                        "total_cost_usd": 0.02}),
        ]) + "\n", encoding="utf-8")

        got = client.get(f"/api/brainstorm/{sid}/feed").json()["data"]
        kinds = [e["kind"] for e in got["events"]]
        assert kinds == ["boundary", "init", "tool", "turn_end"]
        assert "resumed" in got["events"][0]["text"]
        # The init readback: the tool list is the CLI's own statement of what it
        # built, and it is the only account that is not the model's.
        assert got["events"][1]["tools"] == ["mcp__pads__pad_read",
                                             "mcp__pads__pad_draw"]
        assert got["cursor"] > 0

        # A cursor means only what is NEW comes back — the view polls.
        again = client.get(
            f"/api/brainstorm/{sid}/feed?cursor={got['cursor']}").json()["data"]
        assert again["events"] == []

    def test_the_payload_says_who_the_partner_is(self, client, no_agents):
        """What the workspace header chip reads.

        It used to render a hardcoded `gpt-4o-mini`, which named a model this
        room no longer talks to at all. `label` is the chip; the rest is what a
        terminal view of the session needs to find and tail the transcript.
        """
        session = _new(client)
        read = client.get(f"/api/brainstorm/{session['id']}").json()["data"]
        who = read["thinker"]
        assert who["runner"] == "claude"
        assert who["label"].startswith("claude · ")
        assert who["session_id"] == session["id"]
        assert who["live"] is False          # nothing spawned by a read
        assert who["log"].endswith(f"session-{session['id']}.log")
        assert set(who) >= {"available", "runner", "model", "label", "readonly",
                            "cost_tracked", "live", "turns", "spent_usd",
                            "cli_session_id", "max_usd", "log", "session_id"}


# ---------------------------------------------------------------------------
# Synthesize: the preview
# ---------------------------------------------------------------------------
class TestSynthesizeWritesNothing:
    def test_it_proposes_without_filing(self, client, root, answers, no_agents):
        session = _new(client)
        sid = session["id"]
        client.patch(f"/api/brainstorm/{sid}", json={"notes": "rain on the hub"})
        client.post(f"/api/brainstorm/{sid}/message", json={"text": "weather"})
        _quiet()
        answers["replies"].append(_plan(
            {"seat": "art", "title": "paint a rain overlay",
             "brief": "a full-screen rain overlay for the hub, 2 frames"},
            summary="the hub gets weather"))

        got = client.post(f"/api/brainstorm/{sid}/synthesize")
        assert got.status_code == 200, got.text
        body = got.json()["data"]
        assert body["wrote_nothing"] is True
        assert body["plan"]["summary"] == "the hub gets weather"
        assert [i["seat"] for i in body["plan"]["items"]] == ["art"]

        assert _items(root) == []
        after = client.get(f"/api/brainstorm/{sid}").json()["data"]
        assert after["status"] == "open"
        assert after["deploys"] == []

    def test_it_sends_the_notes_and_the_drawing(self, client, answers,
                                                no_agents):
        session = _new(client)
        sid = session["id"]
        client.patch(f"/api/brainstorm/{sid}",
                     json={"notes": "NOTE-MARKER", "drawing": SCENE})
        client.post(f"/api/brainstorm/{sid}/message", json={"text": "weather"})
        _quiet()
        answers["asked"].clear()
        _quiet()
        answers["replies"].append(_plan())
        client.post(f"/api/brainstorm/{sid}/synthesize")
        sent = "\n".join(t["content"] for t in answers["asked"][-1]["turns"])
        assert "NOTE-MARKER" in sent
        assert "hub-1 -> shrine-1" in sent

    def test_a_chat_turn_does_not_pay_for_the_pads(self, client, answers,
                                                   no_agents):
        """The pads are a scratch document that changes constantly; resending
        them per message is the cost this feature exists to avoid."""
        session = _new(client)
        sid = session["id"]
        client.patch(f"/api/brainstorm/{sid}",
                     json={"notes": "NOTE-MARKER", "drawing": SCENE})
        client.post(f"/api/brainstorm/{sid}/message", json={"text": "weather"})
        _quiet()
        sent = "\n".join(t["content"] for t in answers["asked"][-1]["turns"])
        _quiet()
        assert "NOTE-MARKER" not in sent

    def test_an_empty_session_has_nothing_to_synthesize(self, client, answers):
        session = _new(client)
        got = client.post(f"/api/brainstorm/{session['id']}/synthesize")
        assert got.status_code == 400

    def test_a_non_json_reply_becomes_a_summary_with_no_items(self, client,
                                                              answers,
                                                              no_agents):
        session = _new(client)
        sid = session["id"]
        client.post(f"/api/brainstorm/{sid}/message", json={"text": "weather"})
        _quiet()
        answers["replies"].append("I do not think this is ready to file.")
        _quiet()
        plan = client.post(f"/api/brainstorm/{sid}/synthesize"
                           ).json()["data"]["plan"]
        assert plan["items"] == []
        assert plan["summary"] == "I do not think this is ready to file."
        assert plan["notes"]          # the repair is on screen, not silent

    def test_a_fenced_reply_still_parses(self):
        plan = _bs.parse_plan(
            '```json\n{"summary": "s", "items": '
            '[{"seat": "art", "title": "t", "brief": "b"}]}\n```', "director")
        assert [i["title"] for i in plan["items"]] == ["t"]

    def test_no_partner_on_this_machine_is_a_503_not_a_502(self, client,
                                                           no_agents):
        """A settings link and a retry button are different buttons.

        The condition that means "this cannot work here" moved with the
        mechanism — it was a missing OPENAI_API_KEY and it is now a CLI that is
        not installed — and the two answers it distinguishes did not.
        """
        session = _new(client)
        client.post(f"/api/brainstorm/{session['id']}/message",
                    json={"text": "weather"})
        _quiet()
        got = client.post(f"/api/brainstorm/{session['id']}/synthesize")
        assert got.status_code == 503
        assert got.json()["error"]["code"] == "synthesis_failed"


# ---------------------------------------------------------------------------
# Deploy: exactly the confirmed plan
# ---------------------------------------------------------------------------
class TestDeployFilesTheConfirmedPlan:
    def test_it_files_that_plan_and_nothing_else(self, client, root, answers,
                                                 no_agents):
        session = _new(client)
        sid = session["id"]
        plan = {"summary": "the hub gets weather", "chained": False, "items": [
            {"seat": "art", "title": "paint a rain overlay", "brief": "two frames"},
            {"seat": "audio", "title": "record rain", "brief": "a 20s loop"},
        ]}
        got = client.post(f"/api/brainstorm/{sid}/deploy", json={"plan": plan})
        assert got.status_code == 200, got.text
        filed = got.json()["data"]["filed"]
        assert [(f["seat"], f["title"]) for f in filed] == [
            ("art", "paint a rain overlay"), ("audio", "record rain")]

        board = _items(root)
        assert len(board) == 2
        for item in board:
            assert item["source"] == "brainstorm"
            assert item["source_ref"] == str(sid)
            assert item["status"] == "queued"
            assert item["chain_id"] == ""       # unchained plan, no chain
            assert item["depends_on"] is None
        assert board[0]["brief"] == "two frames"

    def test_the_session_records_what_it_produced(self, client, answers,
                                                  no_agents):
        session = _new(client)
        sid = session["id"]
        plan = {"summary": "s", "items": [
            {"seat": "art", "title": "paint a rain overlay", "brief": "b"}]}
        client.post(f"/api/brainstorm/{sid}/deploy", json={"plan": plan})
        read = client.get(f"/api/brainstorm/{sid}").json()["data"]
        assert read["status"] == "deployed"
        assert len(read["deploys"]) == 1
        assert [i["title"] for i in read["deploys"][0]["items"]] == [
            "paint a rain overlay"]
        # Still writable afterwards: the SESSION is not ended by deploying, and
        # a second batch later is legitimate. What deploying ends is the running
        # partner PROCESS — see test_deploying_shuts_the_partner_down.
        assert client.post(f"/api/brainstorm/{sid}/message",
                           json={"text": "and after that?"}).status_code == 200

    def test_deploying_shuts_the_partner_down(self, client, root, answers,
                                              no_agents, monkeypatch):
        """A room that still answers is a room the next request goes into.

        The failure this closes, observed on this project's own director seat: a
        session deployed one day was still running the next, the seat page
        reopened it silently, and three more requests stacked in the thread on
        top of a plan already on the board.
        """
        from bgate_core import brainstorm as _bs

        stopped = []
        monkeypatch.setattr(_bs, "close_partner",
                            lambda r, sid: stopped.append(int(sid)) or {"ok": True})
        sid = _new(client)["id"]
        got = client.post(f"/api/brainstorm/{sid}/deploy", json={"plan": {
            "items": [{"seat": "art", "title": "t", "brief": "b"}]}})
        assert got.status_code == 200, got.text
        assert stopped == [sid]
        assert got.json()["data"]["closed"] == {"ok": True}

    def test_a_partner_that_will_not_die_does_not_fail_the_deploy(
            self, client, root, answers, no_agents, monkeypatch):
        """The items are filed. Re-running the deploy would file them twice."""
        from bgate_core import brainstorm as _bs

        def boom(root, session_id):
            raise RuntimeError("the CLI is wedged")

        monkeypatch.setattr(_bs, "close_partner", boom)
        sid = _new(client)["id"]
        got = client.post(f"/api/brainstorm/{sid}/deploy", json={"plan": {
            "items": [{"seat": "art", "title": "t", "brief": "b"}]}})
        assert got.status_code == 200, got.text
        assert len(_items(root)) == 1
        closed = got.json()["data"]["closed"]
        assert closed["ok"] is False and "wedged" in closed["note"]

    def test_deploy_never_re_synthesizes(self, client, answers, no_agents):
        """If it asked the model again, the plan filed would be one nobody read."""
        session = _new(client)
        answers["asked"].clear()
        client.post(f"/api/brainstorm/{session['id']}/deploy", json={"plan": {
            "items": [{"seat": "art", "title": "t", "brief": "b"}]}})
        assert answers["asked"] == []

    def test_a_chained_plan_arrives_as_a_chain(self, client, root, no_agents):
        session = _new(client)
        plan = {"summary": "s", "chained": True, "items": [
            {"seat": "art", "title": "draw the shrine", "brief": "a"},
            {"seat": "gameplay", "title": "place the shrine", "brief": "b"},
            {"seat": "qa", "title": "check the shrine", "brief": "c"},
        ]}
        body = client.post(f"/api/brainstorm/{session['id']}/deploy",
                           json={"plan": plan}).json()["data"]
        assert body["chained"] is True
        assert body["chain_id"]

        board = _items(root)
        assert [i["title"] for i in board] == [
            "draw the shrine", "place the shrine", "check the shrine"]
        assert [i["chain_pos"] for i in board] == [1, 2, 3]
        assert {i["chain_id"] for i in board} == {body["chain_id"]}
        # The link that makes ordering real rather than a preference.
        assert board[0]["depends_on"] is None
        assert board[1]["depends_on"] == board[0]["id"]
        assert board[2]["depends_on"] == board[1]["id"]

    def test_a_one_item_plan_marked_chained_is_filed_plain(self, client, root,
                                                           no_agents):
        session = _new(client)
        body = client.post(f"/api/brainstorm/{session['id']}/deploy", json={
            "plan": {"chained": True, "items": [
                {"seat": "art", "title": "one thing", "brief": "b"}]}}
        ).json()["data"]
        assert body["chained"] is False
        assert _items(root)[0]["chain_id"] == ""

    def test_deploy_without_a_plan_is_refused(self, client, root, no_agents):
        session = _new(client)
        assert client.post(f"/api/brainstorm/{session['id']}/deploy",
                           json={}).status_code == 400
        assert client.post(f"/api/brainstorm/{session['id']}/deploy", json={
            "plan": {"items": []}}).status_code == 400
        assert _items(root) == []

    def test_the_same_plan_twice_is_a_conflict(self, client, root, no_agents):
        session = _new(client)
        sid = session["id"]
        plan = {"items": [{"seat": "art", "title": "paint it", "brief": "b"}]}
        assert client.post(f"/api/brainstorm/{sid}/deploy",
                           json={"plan": plan}).status_code == 200
        again = client.post(f"/api/brainstorm/{sid}/deploy", json={"plan": plan})
        assert again.status_code == 409
        assert len(_items(root)) == 1
        forced = client.post(f"/api/brainstorm/{sid}/deploy",
                             json={"plan": plan, "again": True})
        assert forced.status_code == 200
        assert len(_items(root)) == 2

    def test_a_different_plan_from_the_same_session_still_files(self, client,
                                                                root,
                                                                no_agents):
        session = _new(client)
        sid = session["id"]
        client.post(f"/api/brainstorm/{sid}/deploy", json={"plan": {
            "items": [{"seat": "art", "title": "paint it", "brief": "b"}]}})
        second = client.post(f"/api/brainstorm/{sid}/deploy", json={"plan": {
            "items": [{"seat": "audio", "title": "score it", "brief": "b"}]}})
        assert second.status_code == 200
        assert len(_items(root)) == 2

    def test_an_edited_brief_is_what_gets_filed(self, client, root, no_agents):
        """The human may edit the preview; deploy files THEIR text, verbatim."""
        session = _new(client)
        client.post(f"/api/brainstorm/{session['id']}/deploy", json={"plan": {
            "items": [{"seat": "art", "title": "paint it",
                       "brief": "EDITED BY THE HUMAN"}]}})
        assert _items(root)[0]["brief"] == "EDITED BY THE HUMAN"

    def test_an_unknown_seat_is_refused_rather_than_repaired(self, client, root,
                                                             no_agents):
        """validate_plan raises where parse_plan repairs: quietly rewriting a
        CONFIRMED plan means filing something other than what was approved."""
        session = _new(client)
        got = client.post(f"/api/brainstorm/{session['id']}/deploy", json={
            "plan": {"items": [{"seat": "wizard", "title": "t", "brief": "b"}]}})
        assert got.status_code == 400
        assert _items(root) == []

    def test_an_archived_session_cannot_deploy(self, client, root, no_agents):
        session = _new(client)
        client.post(f"/api/brainstorm/{session['id']}/archive")
        got = client.post(f"/api/brainstorm/{session['id']}/deploy", json={
            "plan": {"items": [{"seat": "art", "title": "t", "brief": "b"}]}})
        assert got.status_code == 409
        assert _items(root) == []


# ---------------------------------------------------------------------------
# The narrative variant — same machinery, a different target
# ---------------------------------------------------------------------------
class TestNarrativeStaysInItsLane:
    def test_a_narrative_preview_moves_a_stray_item_and_says_so(self):
        plan = _bs.parse_plan(_plan(
            {"seat": "art", "title": "paint the shrine", "brief": "b"},
            {"seat": "narrative", "title": "write the shrine's origin",
             "brief": "b"}), "narrative")
        assert [i["seat"] for i in plan["items"]] == ["narrative", "narrative"]
        assert any("art" in note for note in plan["notes"])

    def test_a_narrative_deploy_refuses_a_game_dev_item(self, client, root,
                                                        no_agents):
        session = _new(client, seat="narrative")
        got = client.post(f"/api/brainstorm/{session['id']}/deploy", json={
            "plan": {"items": [{"seat": "art", "title": "t", "brief": "b"}]}})
        assert got.status_code == 400
        assert _items(root) == []

    def test_a_narrative_deploy_files_canon_work(self, client, root, no_agents):
        session = _new(client, seat="narrative")
        body = client.post(f"/api/brainstorm/{session['id']}/deploy", json={
            "plan": {"summary": "the shrine predates the hub", "items": [
                {"seat": "narrative", "title": "write the shrine's origin",
                 "brief": "add a lore entity 'shrine' and one canon fact"}]}}
        ).json()["data"]
        assert [f["seat"] for f in body["filed"]] == ["narrative"]
        assert _items(root)[0]["source_ref"] == str(session["id"])

    def test_the_two_seats_get_different_prompts(self):
        assert "CANON" in _bs.synthesis_system("narrative").upper()


# ---------------------------------------------------------------------------
# What the synthesis knows about the world
# ---------------------------------------------------------------------------
class TestWorldContext:
    def test_narrative_synthesis_sees_existing_canon(self, root):
        _lore.add_entity(root, "place", "The Shrine", summary="a quiet ruin")
        _lore.add_fact(root, "the-shrine", "the shrine predates the hub",
                       locked=True)
        block = _bs.world_context(root, "narrative")
        assert "the-shrine" in block
        assert "[LOCKED] the shrine predates the hub" in block

    def test_it_is_only_sent_on_a_synthesis(self, client, root, answers,
                                            no_agents):
        _bible.add(root, "pillar", "PILLAR-MARKER", body="one thing")
        session = _new(client)
        sid = session["id"]
        client.post(f"/api/brainstorm/{sid}/message", json={"text": "weather"})
        _quiet()
        chat = "\n".join(t["content"] for t in answers["asked"][-1]["turns"])
        _quiet()
        assert "PILLAR-MARKER" not in chat

        answers["replies"].append(_plan())
        client.post(f"/api/brainstorm/{sid}/synthesize")
        synth = "\n".join(t["content"] for t in answers["asked"][-1]["turns"])
        assert "PILLAR-MARKER" in synth

    def test_an_unreadable_world_is_a_thinner_prompt_not_a_failure(self, root,
                                                                   monkeypatch):
        monkeypatch.setattr("bgate_core.lore.list_entities",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError))
        assert _bs.world_context(root, "narrative") == ""


# --- the rail's aggregates -------------------------------------------------
# The rooms rail draws a dot per seat present and the room's spend, so both ride
# on list_sessions rather than costing a read per row. These assert the two
# choices that are easy to get backwards.

def _seed_room(root):
    from bgate_core import db as _db
    session = _bs.create(root, "director", "Combat feel")
    with _db.tx(root) as conn:
        for seat, state, spent in (("gameplay", "live", 0.28),
                                   ("art", "invited", 0.19),
                                   ("audio", "left", 0.06)):
            conn.execute(
                "INSERT INTO brainstorm_participant "
                "(session_id, seat, state, turns, spent_usd) VALUES (?,?,?,1,?)",
                (session["id"], seat, state, spent))
    return session


def test_listing_names_only_the_seats_still_in_the_room(tmp_path):
    _seed_room(tmp_path)
    assert sorted(_bs.list_sessions(tmp_path)[0]["guests"]) == ["art", "gameplay"]


def test_listing_spend_keeps_a_seat_that_left(tmp_path):
    """A room must not get cheaper because somebody tidied the roster."""
    _seed_room(tmp_path)
    assert round(_bs.list_sessions(tmp_path)[0]["spent_usd"], 2) == 0.53


def test_listing_has_no_guests_when_nobody_was_invited(tmp_path):
    _bs.create(tmp_path, "director", "Empty")
    row = _bs.list_sessions(tmp_path)[0]
    assert row["guests"] == [] and row["spent_usd"] == 0


class TestPartnerEnvironmentIsScrubbed:
    """The thinking partner's environment is dispatch's allowlist, not the
    dashboard's whole shell. The spawn was forked from dispatch WITHOUT the
    scrub, so the one process whose promise is 'cannot act' inherited every
    credential the dashboard's shell held."""

    class _StubChat:
        cost_tracked = True
        readonly_by = "stub"

        def build_args(self, exe, **kw):
            return [exe]

    class _StubRunner:
        name = "claude"
        chat = None  # set per instance below

        def __init__(self):
            self.chat = TestPartnerEnvironmentIsScrubbed._StubChat()

        def find(self):
            return "claude-stub"

    def test_the_spawn_env_drops_foreign_credentials(self, root, monkeypatch):
        from bgate_ui import brainsession

        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-for-the-room")
        monkeypatch.setenv("BGATE_SEAT", "art")          # a stray seat stamp
        monkeypatch.setenv("BGATE_CUSTOM_TOGGLE", "keep")
        seen = {}

        def fake_popen(args, cwd=None, env=None, **kw):
            seen["env"] = env
            raise OSError("stop before a real process")

        monkeypatch.setattr(brainsession.subprocess, "Popen", fake_popen)
        with pytest.raises(brainsession.Unavailable):
            brainsession._spawn(str(root), 1, self._StubRunner(), "think",
                                register=False, pads=False)

        env = seen["env"]
        assert "AWS_SECRET_ACCESS_KEY" not in env
        # The seat stamps come off even though BGATE_* rides the allowlist:
        # a thinking session is nobody's seat and holds no work item.
        assert "BGATE_SEAT" not in env
        assert "BGATE_WORK_ITEM" not in env
        assert "BGATE_LOCK_OWNER" not in env
        # The harness's own namespace still passes, and the room is stamped.
        assert env.get("BGATE_CUSTOM_TOGGLE") == "keep"
        assert env["BGATE_ACTOR"].startswith("brainstorm:1")
        # The toolchain still starts (the scrub keeps the original casing,
        # which on Windows can be "Path").
        assert any(k.upper() == "PATH" for k in env)

    def test_the_kill_logic_is_dispatchs_not_a_fork(self):
        from bgate_ui import brainsession, dispatch as _dispatch

        assert brainsession._kill_tree is _dispatch._kill_tree
