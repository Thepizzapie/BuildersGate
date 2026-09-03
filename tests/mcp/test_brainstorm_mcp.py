"""The brainstorm room from the OTHER front door, and the rule that keeps both.

tests/ui/test_brainstorm.py covers what the room is. This covers what changes when
an agent is the one standing in it, which is three things:

THE DISPATCH BAN STOPS BEING FREE. On the web side "a message cannot dispatch"
used to be nearly automatic — that process held no tools. These tools run INSIDE
the MCP server, where queue_add is a hundred lines away, so the guarantee has to
be structural here or it is nothing. Asserted both ways: no brainstorm tool
names the queue at all, exactly one of them can reach the board (through
``brainstorm.file_plan``), and a turn taken through the tool leaves the
work_item table empty with dispatch rigged to explode.

AND IT STOPPED BEING FREE ON THE OTHER SIDE TOO. The thinking partner is now a
spawned Claude Code session rather than a bare chat-completions call, so the
question "could the partner file work" is live in BOTH doors — and worst here,
because the tool it would need is the one this very module exports at user
scope. The answer is ``--strict-mcp-config`` with no ``--mcp-config``: the
spawned session loads no MCP server at all, so it cannot inherit the one it is
sitting inside. That, plus an empty built-in tool set, is what the argv test
below pins.

DEPLOY IS A HUMAN'S. The room exists so somebody reads a plan before agents are
dispatched against it; an agent that deploys its own proposals is the review
step reviewing itself, and each item it files spawns another agent. Refused on
the same fail-closed signal seat_configure and the human-only settings use —
BGATE_SEAT / BGATE_WORK_ITEM, not the actor string, because a gate that reads
one stamp is disabled by forgetting one line. Everything else in the room stays
open to an agent: thinking is not the dangerous part.

THE TWO DOORS DO NOT DRIFT. TestParity is the half of this file that is meant to
fail for somebody else's feature later; read its docstring for exactly what it
does and does not catch.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import json

import pytest

from bgate_core.design import brainstorm as _bs
from bgate_core.store import db
from bgate_mcp import server
from bgate_ui.agents import runners as _runners

SCENE = {
    "elements": [
        {"id": "hub-1", "type": "rectangle", "x": 10, "y": 20,
         "width": 120, "height": 60, "text": "hub"},
        {"id": "arrow-1", "type": "arrow",
         "startBinding": {"elementId": "hub-1"},
         "endBinding": {"elementId": "shrine-1"}},
        {"id": "shrine-1", "type": "rectangle", "x": 300, "y": 20,
         "width": 120, "height": 60, "label": {"text": "shrine"}},
    ],
    "appState": {"viewBackgroundColor": "#ffffff"},
}

TOOLS = ("brainstorm_list", "brainstorm_new", "brainstorm_open",
         "brainstorm_say", "brainstorm_note", "brainstorm_synthesize",
         "brainstorm_deploy", "brainstorm_archive", "brainstorm_delete")


@pytest.fixture(autouse=True)
def no_cli(monkeypatch):
    """NO TEST IN THIS FILE MAY SPAWN A CLI. Autouse, and that is the point.

    The partner used to be an HTTP call gated on a key CI did not have, so "this
    test does not reach a model" was true by accident. It is a subprocess now,
    and every machine that runs this suite HAS the claude CLI — that is what the
    product is for. Neutralising the LOOKUP rather than the spawn is deliberate:
    it exercises the same "no partner here" path a user without the CLI meets.
    """
    monkeypatch.setattr(_runners, "find_claude", lambda: None)
    monkeypatch.setattr(_runners, "find_codex", lambda: None)


@pytest.fixture()
def wired(root, monkeypatch):
    """A project, and a session that reads as the human at the machine.

    The identity vars are cleared rather than assumed absent: a developer who
    exports BGATE_SEAT in their own shell would otherwise see the human-only
    tests pass for the wrong reason.
    """
    monkeypatch.setenv("BGATE_ROOT", str(root))
    for var in ("BGATE_SEAT", "BGATE_WORK_ITEM", "BGATE_ACTOR"):
        monkeypatch.delenv(var, raising=False)
    return root


@pytest.fixture()
def no_agents(monkeypatch):
    """Any dispatch at all is a test failure, not a mocked-out side effect."""
    def boom(*a, **kw):
        raise AssertionError("the brainstorm room dispatched an agent")

    monkeypatch.setattr("bgate_ui.agents.dispatch.dispatch", boom)
    monkeypatch.setattr("bgate_ui.agents.dispatch.kill_all", boom)


async def call(tool: str, /, **kwargs) -> dict:
    """Dispatch through FastMCP and decode what a client would receive."""
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


def _items(root) -> list[dict]:
    return [dict(r) for r in db.connect(root).execute(
        "SELECT * FROM work_item ORDER BY id")]


def _tool_body(name: str) -> ast.FunctionDef:
    """The tool's own source, as parsed — for the assertions about what it
    cannot reach, which are about the code and not about one call of it.

    The brainstorm tools were carved out of server.py into
    bgate_mcp.tools_brainstorm (server star-imports it back), so the source
    is read from the module that actually DEFINES each name — inspect can
    say which, and a reader pinned to one file would silently pass on a
    tool it could no longer see."""
    fn = getattr(server, name)
    tree = ast.parse(inspect.getsource(inspect.getmodule(fn)))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined in bgate_mcp.server")


def _identifiers(node: ast.AST) -> set[str]:
    """Every name the code touches. Docstrings are Constants, not Names, so
    prose that MENTIONS queue_add does not read as reaching for it."""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
        elif isinstance(child, ast.Attribute):
            found.add(child.attr)
        elif isinstance(child, ast.alias):
            found.add(child.asname or child.name)
    return found


# ---------------------------------------------------------------------------
# The surface
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_the_room_is_in_the_tool_list():
    names = {t.name for t in await server.mcp.list_tools()}
    assert set(TOOLS) <= names


def test_every_brainstorm_tool_takes_project_dir():
    """The room must be addressable in a fleet sharing one server, like the rest
    of the surface: a call that cannot say WHICH game it means lands in whichever
    one the cwd happened to resolve to."""
    for name in TOOLS:
        params = inspect.signature(getattr(server, name)).parameters
        assert "project_dir" in params, name


# ---------------------------------------------------------------------------
# A turn cannot dispatch — the guarantee that does not come across for free
# ---------------------------------------------------------------------------
class TestATurnCannotDispatch:
    @pytest.mark.anyio
    async def test_saying_something_writes_rows_and_nothing_else(
            self, wired, no_agents):
        session = await call("brainstorm_new", title="hub weather")
        said = await call("brainstorm_say", session_id=session["id"],
                          text="what if the hub had weather",
                          reply="weather is a mood, not a mechanic")
        assert said["message"]["role"] == "user"
        assert said["reply"]["text"] == "weather is a mood, not a mechanic"
        assert _items(wired) == []

    @pytest.mark.anyio
    async def test_the_caller_can_answer_without_spawning_anything(
            self, wired, no_agents):
        """The affordance this door has and the browser does not, and it is
        worth MORE now than it was: the caller is already a model, so a session
        is useful here with no CLI, no key and no second process."""
        session = await call("brainstorm_new")
        await call("brainstorm_say", session_id=session["id"], text="an idea",
                   reply="a better idea")
        read = await call("brainstorm_open", session_id=session["id"])
        assert [(m["role"], m["text"]) for m in read["messages"]] == [
            ("user", "an idea"), ("assistant", "a better idea")]
        assert read["thinker"]["live"] is False

    @pytest.mark.anyio
    async def test_the_sentence_survives_a_dead_partner(self, wired, no_agents):
        """No CLI, no reply — and what was typed is still there. Losing that is
        the worst outcome available on this call. The cause moved with the
        mechanism (a missing key became a missing CLI); the property did not."""
        session = await call("brainstorm_new")
        got = await call("brainstorm_say", session_id=session["id"],
                         text="keep this")
        assert got["reply"] is None
        assert got["model"]["ok"] is False
        assert "not found on PATH" in got["model"]["error"]
        assert "reply=" in got["note"]
        read = await call("brainstorm_open", session_id=session["id"])
        assert [m["text"] for m in read["messages"]] == ["keep this"]
        assert _items(wired) == []

    @pytest.mark.anyio
    async def test_an_agent_must_answer_itself_rather_than_spawn_a_partner(
            self, wired, no_agents, monkeypatch):
        """A model that is already a session must not pay to start another one.

        This did not exist while the partner was a cheap API call. It exists now
        because leaving `reply=` empty from inside a dispatched agent would
        spawn a nested CLI session and bill a turn against the subscription for
        an answer the caller could simply have written.
        """
        session = await call("brainstorm_new")
        monkeypatch.setenv("BGATE_SEAT", "art")
        got = await call("brainstorm_say", session_id=session["id"],
                         text="the rain needs a palette")
        assert got["reply"] is None
        assert got["model"]["ok"] is False
        assert "reply=" in got["model"]["error"]
        # And the sentence is still stored — refusing to answer is not refusing
        # to listen.
        read = await call("brainstorm_open", session_id=session["id"])
        assert [m["text"] for m in read["messages"]] == [
            "the rain needs a palette"]

    @pytest.mark.anyio
    async def test_the_model_is_reached_only_through_the_shared_bare_call(
            self, wired, no_agents, monkeypatch):
        """brainstorm.ask is the one way out to a model from either door.

        Patched at the core rather than stubbed here on purpose: if a tool ever
        built its own client, this test would go quiet instead of failing, so it
        asserts the stub was actually consulted.
        """
        seen: list[dict] = []

        def fake(root, system, turns, **kw):
            seen.append({"system": system, "turns": turns, **kw})
            return {"ok": True, "text": "asked", "model": "test",
                    "seconds": 0.0, "usd": 0.0}

        monkeypatch.setattr(_bs, "ask", fake)
        session = await call("brainstorm_new")
        await call("brainstorm_say", session_id=session["id"], text="weather")
        assert len(seen) == 1
        assert "BRAINSTORM" in seen[0]["system"]
        assert seen[0]["turns"] == [{"role": "user", "content": "weather"}]
        assert "tools" not in seen[0] and "functions" not in seen[0]
        # A turn here is a turn in THE session, not a fresh partner per message.
        assert seen[0]["session_id"] == session["id"]

    def test_the_partner_cannot_inherit_the_server_it_is_sitting_inside(self):
        """THE NO-WRITE GUARANTEE, in the door where it is hardest.

        This replaces the kwarg-allowlist test. That one asserted that a
        `tools=` argument to a chat-completions call was dropped; there is no
        such call now, and the risk is far larger — the partner is a real CLI
        session, and builders-gate is registered at USER scope on any machine
        that runs this product. A plainly-spawned `claude` would therefore hold
        queue_add, bible_add and image_generate before it said a word.

        The mechanism is that the session loads NO MCP server at all — not an
        allowlist of safe ones, which would put the whole promise on nobody ever
        mistyping an entry. Verified against the CLI's own `system/init` event,
        which reported `"mcp_servers":[]` and `"tools":[]`.
        """
        argv = _runners.RUNNERS["claude"].chat.build_args(
            "claude", system="think with me", model="sonnet", max_usd=1.0)
        pairs = list(zip(argv, argv[1:]))
        assert "--strict-mcp-config" in argv
        assert "--mcp-config" not in argv
        assert not [a for a in argv if _runners.MCP_SERVER_NAME in str(a)]
        assert ("--tools", "") in pairs
        assert ("--setting-sources", "") in pairs
        # NOT ("--permission-mode", "plan"). That flag was removed on
        # measurement, not preference: it refused the pad tools too, so a
        # session holding exactly mcp__pads__pad_read answered that plan mode
        # blocked the read-only call it had just been asked to make. Shipping a
        # two-tool server that can never be called is worse than either
        # alternative. runners.py carries the observed transcript.
        #
        # The promise rests on the capability surface being EMPTY and the MCP
        # config being exhaustive, both asserted above and neither changed by
        # this flag.
        assert ("--permission-mode", "acceptEdits") in pairs
        assert "--disable-slash-commands" in argv

    def test_no_brainstorm_tool_can_see_the_queue(self):
        """Structural, because in THIS module it cannot be accidental.

        bgate_ui.routes.brainstorm gets this for free — nothing in that process
        holds a tool. Here the queue is imported by name a hundred lines down,
        so the property has to be asserted about the code rather than inferred
        from a run that happened not to file anything.
        """
        for name in TOOLS:
            reached = {ident for ident in _identifiers(_tool_body(name))
                       if "queue" in ident.lower()}
            assert not reached, f"{name} reaches for {reached}"

    def test_exactly_one_tool_can_reach_the_board(self):
        """file_plan is the only function in bgate_core.design.brainstorm that imports
        the queue. Whoever calls it is the deploy path, whatever it is named."""
        callers = [name for name in TOOLS
                   if "file_plan" in _identifiers(_tool_body(name))]
        assert callers == ["brainstorm_deploy"]


# ---------------------------------------------------------------------------
# Who may deploy
# ---------------------------------------------------------------------------
class TestOnlyAHumanDeploys:
    PLAN = {"summary": "the hub gets weather", "items": [
        {"seat": "art", "title": "paint a rain overlay", "brief": "two frames"}]}

    @pytest.mark.anyio
    async def test_the_humans_own_session_files_normally(self, wired,
                                                         no_agents):
        session = await call("brainstorm_new")
        got = await call("brainstorm_deploy", session_id=session["id"],
                         plan=self.PLAN)
        assert [f["title"] for f in got["filed"]] == ["paint a rain overlay"]
        board = _items(wired)
        assert len(board) == 1
        # The provenance that going through here buys over a bare queue_add.
        assert board[0]["source"] == "brainstorm"
        assert board[0]["source_ref"] == str(session["id"])

    @pytest.mark.anyio
    @pytest.mark.parametrize("var,value", [
        ("BGATE_SEAT", "art"),            # a dispatched seat worker
        ("BGATE_WORK_ITEM", "41"),        # a session executing an item
        ("BGATE_ACTOR", "agent:item-41"),  # the explicit stamp
    ])
    async def test_a_machine_is_refused_by_any_of_its_stamps(
            self, wired, no_agents, monkeypatch, var, value):
        """Three signals, any one is enough — the point of reading more than the
        actor string is that dispatch cannot avoid setting the other two."""
        session = await call("brainstorm_new")
        monkeypatch.setenv(var, value)
        got = await call("brainstorm_deploy", session_id=session["id"],
                         plan=self.PLAN)
        assert got["ok"] is False
        assert "may not deploy" in got["error"]
        # It says what to do instead, or the agent files it under a worse route.
        assert "ask_human" in got["error"] and "queue_add" in got["error"]
        assert _items(wired) == []

    @pytest.mark.anyio
    async def test_an_agent_may_still_think_out_loud(self, wired, no_agents,
                                                     monkeypatch):
        """Only the writing operation is gated. An agent that cannot open a
        session, read one or leave a note in it would simply not use the room,
        and the conversation is the part worth having."""
        monkeypatch.setenv("BGATE_SEAT", "art")
        session = await call("brainstorm_new", title="from the art seat")
        assert session.get("error") is None
        assert (await call("brainstorm_say", session_id=session["id"],
                           text="the rain needs a palette",
                           reply="two frames is enough")).get("error") is None
        noted = await call("brainstorm_note", session_id=session["id"],
                           notes="- tint\n- sound")
        assert noted["changed"] == ["notes"]
        archived = await call("brainstorm_archive", session_id=session["id"])
        assert archived["status"] == "archived"
        assert _items(wired) == []

    @pytest.mark.anyio
    async def test_delete_is_human_only_and_archive_is_the_way_round_it(
            self, wired, no_agents, monkeypatch):
        session = await call("brainstorm_new")
        monkeypatch.setenv("BGATE_WORK_ITEM", "7")
        refused = await call("brainstorm_delete", session_id=session["id"])
        assert refused["ok"] is False
        assert "brainstorm_archive" in refused["error"]
        assert (await call("brainstorm_open",
                           session_id=session["id"]))["id"] == session["id"]

    @pytest.mark.anyio
    async def test_the_same_plan_twice_is_refused_and_says_what_it_became(
            self, wired, no_agents):
        session = await call("brainstorm_new")
        await call("brainstorm_deploy", session_id=session["id"],
                   plan=self.PLAN)
        again = await call("brainstorm_deploy", session_id=session["id"],
                           plan=self.PLAN)
        assert again["ok"] is False
        assert [i["title"] for i in again["already_filed"]["items"]] == [
            "paint a rain overlay"]
        assert len(_items(wired)) == 1
        forced = await call("brainstorm_deploy", session_id=session["id"],
                            plan=self.PLAN, again=True)
        assert forced.get("error") is None
        assert len(_items(wired)) == 2

    @pytest.mark.anyio
    async def test_a_chained_plan_arrives_as_a_chain(self, wired, no_agents):
        session = await call("brainstorm_new")
        got = await call("brainstorm_deploy", session_id=session["id"], plan={
            "summary": "s", "chained": True, "items": [
                {"seat": "art", "title": "draw the shrine", "brief": "a"},
                {"seat": "gameplay", "title": "place the shrine", "brief": "b"}]})
        assert got["chained"] is True
        board = _items(wired)
        assert board[1]["depends_on"] == board[0]["id"]

    @pytest.mark.anyio
    async def test_a_narrative_session_cannot_file_game_dev_work(self, wired,
                                                                 no_agents):
        session = await call("brainstorm_new", seat="narrative")
        got = await call("brainstorm_deploy", session_id=session["id"],
                         plan=self.PLAN)
        assert got["ok"] is False
        assert _items(wired) == []


# ---------------------------------------------------------------------------
# The pads, and the drawing as words
# ---------------------------------------------------------------------------
class TestThePads:
    @pytest.mark.anyio
    async def test_a_session_round_trips_through_the_tools(self, wired,
                                                           no_agents):
        session = await call("brainstorm_new", title="hub weather")
        sid = session["id"]
        await call("brainstorm_note", session_id=sid, notes="rain on the hub",
                   drawing=SCENE)
        db.close_all()  # from disk, not from a cache
        read = await call("brainstorm_open", session_id=sid)
        assert read["notes"] == "rain on the hub"
        assert [e["id"] for e in read["drawing"]["elements"]] == [
            "hub-1", "arrow-1", "shrine-1"]
        # The reason the scene is stored as elements: it is readable without
        # vision, and the ids are what a caller writes back through.
        assert '"hub"' in read["drawing_text"]
        assert "hub-1 -> shrine-1" in read["drawing_text"]

    @pytest.mark.anyio
    async def test_an_archived_session_takes_nothing_new(self, wired,
                                                         no_agents):
        session = await call("brainstorm_new")
        sid = session["id"]
        await call("brainstorm_archive", session_id=sid)
        for refused in (await call("brainstorm_say", session_id=sid, text="hi",
                                   reply="no"),
                        await call("brainstorm_note", session_id=sid, notes="x"),
                        await call("brainstorm_deploy", session_id=sid, plan={
                            "items": [{"seat": "art", "title": "t",
                                       "brief": "b"}]})):
            assert refused["ok"] is False
            assert "archived" in refused["error"]
        assert _items(wired) == []
        back = await call("brainstorm_archive", session_id=sid, archived=False)
        assert back["status"] == "open"

    @pytest.mark.anyio
    async def test_the_index_lists_without_shipping_the_pads(self, wired,
                                                             no_agents):
        first = await call("brainstorm_new", title="one")
        await call("brainstorm_note", session_id=first["id"],
                   notes="a long scratch document")
        await call("brainstorm_new", seat="narrative", title="two")
        listed = await call("brainstorm_list")
        assert {s["title"] for s in listed["sessions"]} == {"one", "two"}
        assert all("notes" not in s and "drawing" not in s
                   for s in listed["sessions"])
        # No CLI in the fixture, so no partner. The index says which runner it
        # WOULD have used either way — that is what the header chip renders.
        assert listed["model"]["available"] is False
        assert listed["model"]["runner"] == "claude"
        assert "not found on PATH" in listed["model"]["reason"]
        narrative = await call("brainstorm_list", seat="narrative")
        assert [s["title"] for s in narrative["sessions"]] == ["two"]

    @pytest.mark.anyio
    async def test_a_missing_session_is_an_error_payload_not_a_crash(
            self, wired):
        got = await call("brainstorm_open", session_id=9999)
        assert got["ok"] is False and got["error"]


# ---------------------------------------------------------------------------
# Parity — the half of this file meant to fail for somebody else's feature
# ---------------------------------------------------------------------------
# One row per capability that has both doors. Adding a row is the whole cost of
# holding a new feature to this rule.
#
#   (core module, the route module that fronts it, the alias BOTH must use)
MIRRORED = [
    ("bgate_core.design.brainstorm", "bgate_ui.routes.brainstorm", "_bs"),
    ("bgate_core.audio.music", "bgate_ui.routes.music", "_music"),
]


def _core_functions(core) -> set[str]:
    return {name for name, value in vars(core).items()
            if inspect.isfunction(value) and not name.startswith("_")
            and value.__module__ == core.__name__}


def _called_through(module, alias: str, names: set[str]) -> set[str]:
    """Which of `names` this module reaches for as ``<alias>.<name>``.

    Source-level rather than runtime: the question is what the code CAN call,
    and a coverage-shaped answer would need every branch exercised to say so.
    """
    tree = ast.parse(inspect.getsource(module))
    return {node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == alias
            and node.attr in names}


class TestParity:
    """A capability behind one front door is a capability half the system lacks.

    WHAT THIS CATCHES. For each declared capability: a public function of the
    core module that the dashboard's routes call and that no MCP tool calls.
    That is the shape the drift actually takes — somebody adds
    ``POST /api/brainstorm/{id}/pin``, wires it to ``brainstorm.pin``, and the
    tool list silently falls a verb behind.

    WHAT IT DOES NOT CATCH, said plainly so nobody trusts it further than it
    goes. It does not check that the two doors AGREE: a tool may take different
    arguments, apply different permissions (brainstorm_deploy deliberately
    refuses a caller the HTTP route accepts), or answer a different shape. It
    does not see a capability with no core module, or one whose core functions
    are called from somewhere other than a routes module. And it does not
    discover a NEW capability — a feature that ships with routes and no tools at
    all is caught only when someone adds its row to MIRRORED. Auto-discovering
    that was tried and rejected: every route module predating this rule would
    fail on day one, and a parity test that false-positives on somebody else's
    work gets deleted, which is worse than one with a declared list.
    """

    @pytest.mark.parametrize("core_name,route_name,alias", MIRRORED)
    def test_every_web_verb_has_a_tool(self, core_name, route_name, alias):
        core = importlib.import_module(core_name)
        route = importlib.import_module(route_name)
        public = _core_functions(core)
        assert public, f"{core_name} exposes no public functions to compare"

        web = _called_through(route, alias, public)
        # The tool surface is server.py plus the carved-out domain modules
        # (server star-imports them back); a reader pinned to one file would
        # report a moved-not-missing verb as a hole in the tool list.
        import pkgutil

        import bgate_mcp
        tool_modules = [server] + [
            importlib.import_module(f"bgate_mcp.{m.name}")
            for m in pkgutil.iter_modules(bgate_mcp.__path__)
            if m.name.startswith("tools_")]
        tools = set().union(*(_called_through(mod, alias, public)
                              for mod in tool_modules))
        # Neither side may be silently empty: a renamed alias would otherwise
        # turn this whole test into a pair of empty sets that always agree.
        assert web, (f"{route_name} does not reach {core_name} as {alias!r} — "
                     "fix the alias or this test is vacuous")
        assert tools, (f"bgate_mcp.server does not reach {core_name} as "
                       f"{alias!r} — fix the alias or this test is vacuous")

        missing = sorted(web - tools)
        assert not missing, (
            f"{route_name} calls {core_name}.{{{', '.join(missing)}}} and no "
            f"MCP tool does. The dashboard can do something the tool list "
            f"cannot, which means half the system cannot do it at all — add a "
            f"tool in bgate_mcp.server that calls {alias}.<name>, in the "
            f"section for this capability.")
