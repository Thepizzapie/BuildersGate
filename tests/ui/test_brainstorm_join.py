"""Inviting a seat into a brainstorm, and the one property that makes it allowed.

THE GUARANTEE IS THE WHOLE FEATURE. A seat agent normally arrives holding Write,
Edit, Bash and the entire builders-gate MCP server — queue_add included — because
that is what a dispatched agent is FOR. Put one of those in the brainstorm room
and the board is back inside the conversation, and the room's entire value is
that nothing in it can act. So an invited seat is spawned through the same
read-only path as the room's own thinking partner, and this file asserts that
against the ACTUAL argv the process would have been started with: not the
builder function in isolation, but what came out the far end of invite() at the
moment Popen was called.

That is deliberately a stronger test than tests/ui/test_brainstorm.py's. That one
asks the argv builder directly, which proves the flags are correct; this one
proves the flags are what the INVITE PATH USES — the failure it exists to catch
is a future "participants need tools to be useful" landing as a second spawner
next to the first, with the builder left untouched and the test still green.

WHAT IS ASSERTED, in the order it matters:

  1. the read-only flags are present in the spawned argv;
  2. nothing that could write or queue is: no built-in tool names, no
     builders-gate server, no permission bypass, and --allowedTools naming the
     three pad tools or nothing at all;
  3. a runner that has NOT declared a read-only mode is refused rather than
     started with the dispatch flags — nothing is spawned at all;
  4. the refusals a human meets (not a seat, a disabled seat, a duplicate, the
     owner) each say which, because "invite failed" tells them nothing;
  5. an opinion stays an opinion: a participant's answer is a message row with a
     seat on it, and no work item exists anywhere afterwards.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from bgate_core.design import brainstorm as _bs
from bgate_core.store import db
from bgate_core.board import seats as _seats
from bgate_core.store import settings as _settings
from bgate_ui.agents import brainsession
from bgate_ui.agents import runners as _runners
from bgate_ui.app import app
from bgate_ui.routes import brainstorm as _route

FAKE_CLI = "C:/nowhere/claude.exe"



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

class FakeProc:
    """A process that was started and answers nothing.

    Enough of Popen for _spawn and thinker: a stdin to hold open, a pid, and a
    poll() that says "still running" so the roster sees the seat as live. No
    test in this file takes a TURN with one — the turns are stubbed at _ask —
    because what is under test is how the process was BUILT, not what it says.
    """

    def __init__(self, args, **kw):
        self.args = list(args)
        self.kw = kw
        self.pid = 4242
        self.stdin = io.BytesIO()

    def poll(self):
        return None


@pytest.fixture(autouse=True)
def spawns(monkeypatch):
    """Every spawn in this file is captured instead of executed.

    Neutralising Popen rather than the CLI lookup, which is the opposite of the
    fixture in tests/ui/test_brainstorm.py and is the point: that file needs the
    "no partner here" path, and this one needs the argv a real invite would have
    handed the operating system.
    """
    seen: list[FakeProc] = []

    def fake_popen(args, **kw):
        proc = FakeProc(args, **kw)
        seen.append(proc)
        return proc

    monkeypatch.setattr(_runners, "find_claude", lambda: FAKE_CLI)
    monkeypatch.setattr(_runners, "find_codex", lambda: "C:/nowhere/codex.cmd")
    monkeypatch.setattr(brainsession.subprocess, "Popen", fake_popen)
    # _reap would otherwise run taskkill against a pid this process invented.
    monkeypatch.setattr(brainsession, "_kill_tree", lambda pid: None)
    yield seen
    brainsession.stop_all()


@pytest.fixture()
def client(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    return TestClient(app)


@pytest.fixture()
def answers(monkeypatch):
    """Stand in for every voice in the room. Records who was asked what."""
    box: dict = {"replies": {}, "asked": []}

    def fake(project, system, turns, **kw):
        seat = str(kw.get("seat") or "")
        box["asked"].append({"seat": seat, "system": system, "turns": turns})
        text = box["replies"].get(seat, f"{seat or 'partner'} says something")
        return {"ok": True, "text": text, "model": "test", "seconds": 0.0,
                "usd": 0.25}

    monkeypatch.setattr(_route, "_ask", fake)
    return box


def _new(client, seat="director", title="") -> dict:
    got = client.post("/api/brainstorm", json={"seat": seat, "title": title})
    assert got.status_code == 200, got.text
    return got.json()["data"]


def _invite(client, session_id: int, seat: str):
    return client.post(f"/api/brainstorm/{session_id}/invite",
                       json={"seat": seat})


def _items(root) -> list[dict]:
    return [dict(r) for r in db.connect(root).execute(
        "SELECT * FROM work_item ORDER BY id")]


def _argv_for(spawns, seat: str) -> list[str]:
    """The argv of the process spawned for one seat, found by its own log file.

    Located through the cwd rather than by spawn order, so a test that invites
    two seats cannot silently assert against the wrong one.
    """
    for proc in spawns:
        if str(proc.kw.get("cwd") or "").replace("\\", "/").endswith(seat):
            return proc.args
    raise AssertionError(f"nothing was spawned for the {seat} seat "
                         f"({len(spawns)} spawn(s) seen)")


# ---------------------------------------------------------------------------
# THE GUARANTEE
# ---------------------------------------------------------------------------
class TestASeatEntersWithoutItsTools:
    def test_the_invited_seat_is_spawned_read_only(self, client, spawns):
        """The argv an INVITE produced, not the argv a builder can produce.

        Each flag below was checked (in tests/ui/test_brainstorm.py, against the
        CLI's own system/init event) to do what it says: an empty built-in tool
        set, no inherited MCP server, no settings source that can add either
        back, and no slash command reachable from arbitrary human text.
        """
        session = _new(client)
        got = _invite(client, session["id"], "art")
        assert got.status_code == 200, got.text

        argv = _argv_for(spawns, "art")
        pairs = list(zip(argv, argv[1:]))
        assert argv[0] == FAKE_CLI
        assert ("--tools", "") in pairs
        assert "--strict-mcp-config" in argv
        assert ("--setting-sources", "") in pairs
        assert "--disable-slash-commands" in argv

    def test_the_invited_seat_holds_nothing_that_could_write_or_queue(
            self, client, spawns):
        """The negative half, and it is the half that decays.

        A future change that hands participants "just Read" or "just the
        builders-gate server, read tools only" fails here rather than in
        production, which is where a room that can file work is discovered.
        """
        session = _new(client)
        assert _invite(client, session["id"], "art").status_code == 200
        argv = _argv_for(spawns, "art")
        flat = " ".join(str(a) for a in argv)

        # No built-in tool is named anywhere: the set is empty, so there is
        # nothing to grant and nothing to allow.
        for tool in ("Write", "Edit", "Bash", "Glob", "Grep", "NotebookEdit"):
            assert tool not in argv
        # The server this whole product runs on is not in the room. Its name,
        # its prefix and the codex-side way of registering it are all absent.
        assert _runners.MCP_SERVER_NAME not in flat
        assert "queue_add" not in flat
        assert not [a for a in argv if "mcp_servers." in str(a)]
        # Nothing that hands permissions back.
        assert "--dangerously-skip-permissions" not in argv
        assert "--permission-mode" not in argv or (
            argv[argv.index("--permission-mode") + 1] == "acceptEdits")
        # If anything IS allowed, it is the pad server's tools by full name and
        # nothing else — never the `mcp__pads` prefix, which would approve
        # whatever that server grows.
        if "--allowedTools" in argv:
            start = argv.index("--allowedTools") + 1
            allowed = argv[start:start + len(_runners.PAD_TOOLS)]
            assert sorted(allowed) == sorted(_runners.PAD_TOOLS)
            assert "mcp__pads" not in argv

    def test_the_invited_seat_gets_the_same_argv_as_the_rooms_own_partner(
            self, client, spawns, answers):
        """One spawner, one argv. The seat changes WHERE it runs, never WHAT it is.

        Everything that differs between the two processes is a path or a prompt
        — cwd, log, resume marker, --system-prompt. If a flag ever differs, a
        second way to build a thinking session exists, and the read-only
        guarantee has become two things to keep in step.
        """
        session = _new(client)
        assert _invite(client, session["id"], "art").status_code == 200
        # The room's own partner is spawned by the first message it takes, which
        # the stub answers — so ask it directly for the same shape.
        brainsession.start(root_of(client), session["id"],
                           _bs.chat_system("director"))

        art = _argv_for(spawns, "art")
        own = _argv_for(spawns, f"session-{session['id']}")
        assert _flags(art) == _flags(own)

    def test_the_seat_stamps_are_stripped_from_the_environment(
            self, client, spawns, monkeypatch):
        """It is ANSWERING AS the art seat; it is not HOLDING the art seat.

        BGATE_SEAT is what the hook and every env-sniffing path read to decide
        whether a process is a dispatched agent with lanes and locks. Leaving it
        set would put that machinery back inside a room whose whole value is
        that nothing in it can act.
        """
        monkeypatch.setenv("BGATE_SEAT", "director")
        monkeypatch.setenv("BGATE_WORK_ITEM", "77")
        monkeypatch.setenv("BGATE_LOCK_OWNER", "somebody")
        session = _new(client)
        assert _invite(client, session["id"], "art").status_code == 200
        env = next(p.kw["env"] for p in spawns
                   if str(p.kw.get("cwd") or "").endswith("art"))
        assert "BGATE_SEAT" not in env
        assert "BGATE_WORK_ITEM" not in env
        assert "BGATE_LOCK_OWNER" not in env
        assert env["BGATE_ACTOR"] == f"brainstorm:{session['id']}:art"

    def test_a_runner_without_a_readonly_mode_is_refused_not_dispatched(
            self, client, root, spawns):
        """THE REFUSAL, and the reason it is not merely a missing feature.

        codex has no `chat` entry in the runner table, on purpose and with the
        reason written next to it. The tempting failure is to fall back to
        runner.build_args — which is the argv that grants Write, Bash and the
        whole builders-gate server. So: nothing is spawned, the seat is recorded
        as invited, and the answer says what to do about it.
        """
        _settings.set(root, "brainstorm.runner", "codex")
        session = _new(client)
        got = _invite(client, session["id"], "art")
        assert got.status_code == 503, got.text
        assert "read-only" in got.json()["error"]["message"]
        assert spawns == []
        # The row survives the failed spawn: the human asked for this seat.
        row = _bs.participant(root, session["id"], "art")
        assert row["state"] == "invited"


def root_of(client) -> str:
    from bgate_ui.deps import root as _root

    return _root()


def _flags(argv: list[str]) -> list[str]:
    """Every flag in an argv, without the values that legitimately differ."""
    return [str(a) for a in argv if str(a).startswith("--")]


# ---------------------------------------------------------------------------
# The refusals a human actually meets
# ---------------------------------------------------------------------------
class TestWhoMayBeInvited:
    def test_a_seat_that_is_not_a_seat(self, client, spawns):
        session = _new(client)
        got = _invite(client, session["id"], "wizard")
        assert got.status_code == 400, got.text
        assert "not a seat" in got.json()["error"]["message"]
        assert spawns == []

    def test_a_seat_the_project_disabled(self, client, root, spawns):
        """A disabled seat stays disabled here, or the room is a second place
        seats exist and seat_configure means nothing."""
        _seats.configure(root, "art", enabled=False)
        session = _new(client)
        got = _invite(client, session["id"], "art")
        assert got.status_code == 400, got.text
        assert "disabled" in got.json()["error"]["message"]
        assert spawns == []

    def test_the_owner_cannot_be_invited_into_its_own_room(self, client):
        """The director's partner IS the room's own voice; a second copy of it
        would be two of the same seat arguing in one conversation."""
        session = _new(client, seat="director")
        got = _invite(client, session["id"], "director")
        assert got.status_code == 409, got.text
        assert "already owns this room" in got.json()["error"]["message"]

    def test_a_duplicate_is_refused_and_says_it_is_already_here(self, client):
        session = _new(client)
        assert _invite(client, session["id"], "art").status_code == 200
        got = _invite(client, session["id"], "art")
        assert got.status_code == 409, got.text
        assert "already in this room" in got.json()["error"]["message"]

    def test_leaving_keeps_the_row_and_lets_the_seat_back_in(self, client, root):
        """A `left` row is kept: its messages are still in the transcript, and
        its spend still happened."""
        session = _new(client)
        assert _invite(client, session["id"], "art").status_code == 200
        gone = client.delete(f"/api/brainstorm/{session['id']}/invite/art")
        assert gone.status_code == 200, gone.text
        assert gone.json()["data"]["participant"]["state"] == "left"
        # And back in, on the same row rather than a second one.
        assert _invite(client, session["id"], "art").status_code == 200
        rows = _bs.participants(root, session["id"])
        assert [r["seat"] for r in rows] == ["art"]

    def test_leaving_a_seat_that_was_never_here(self, client):
        session = _new(client)
        got = client.delete(f"/api/brainstorm/{session['id']}/invite/art")
        assert got.status_code == 404, got.text


# ---------------------------------------------------------------------------
# Addressing, attribution, and the roster
# ---------------------------------------------------------------------------
class TestTheRoomTalks:
    def test_without_to_everyone_present_answers(self, client, answers):
        session = _new(client)
        _invite(client, session["id"], "art")
        _invite(client, session["id"], "gameplay")
        got = client.post(f"/api/brainstorm/{session['id']}/message",
                          json={"text": "what if the hub had weather"})
        _quiet()
        assert got.status_code == 200, got.text
        body = got.json()["data"]
        # Owner's partner first, then the guests in invite order.
        assert body["spoke"] == ["", "art", "gameplay"]
        assert [a["seat"] for a in answers["asked"]] == ["", "art", "gameplay"]

    def test_to_reaches_exactly_one_seat(self, client, answers):
        session = _new(client)
        _invite(client, session["id"], "art")
        _invite(client, session["id"], "gameplay")
        got = client.post(f"/api/brainstorm/{session['id']}/message",
                          json={"text": "how long is a weather shader", "to": "art"})
        _quiet()
        assert got.status_code == 200, got.text
        assert got.json()["data"]["spoke"] == ["art"]
        assert [a["seat"] for a in answers["asked"]] == ["art"]

    def test_to_a_seat_that_is_not_here_is_refused_before_anything_is_stored(
            self, client, root, answers):
        """A question addressed to nobody must not sit in the transcript waiting
        for an answer that cannot come."""
        session = _new(client)
        got = client.post(f"/api/brainstorm/{session['id']}/message",
                          json={"text": "hello?", "to": "art"})
        _quiet()
        assert got.status_code == 400, got.text
        assert "not in this room" in got.json()["error"]["message"]
        assert _bs.messages(root, session["id"]) == []

    def test_every_reply_carries_the_seat_that_said_it(self, client, root,
                                                       answers):
        session = _new(client)
        _invite(client, session["id"], "art")
        answers["replies"] = {"": "interesting", "art": "two days of shader work"}
        client.post(f"/api/brainstorm/{session['id']}/message",
                    json={"text": "weather?"})
        _quiet()
        rows = _bs.messages(root, session["id"])
        assert [(m["role"], m["seat"], m["text"]) for m in rows] == [
            ("user", "", "weather?"),
            ("assistant", "", "interesting"),
            ("assistant", "art", "two days of shader work"),
        ]

    def test_a_participant_reads_the_other_voices_as_labelled_not_as_the_human(
            self, client, root, answers):
        """Without the label a guest answers another seat's opinion as though
        the human had said it, and argues with the wrong person."""
        session = _new(client)
        _invite(client, session["id"], "art")
        answers["replies"] = {"": "the room's own view", "art": "mine"}
        client.post(f"/api/brainstorm/{session['id']}/message",
                    json={"text": "weather?"})
        _quiet()
        art_turns = [a for a in answers["asked"] if a["seat"] == "art"][-1]
        contents = [t["content"] for t in art_turns["turns"]]
        assert "THE ROOM'S PARTNER: the room's own view" in contents
        # Its own earlier turns, when it has any, come back as assistant turns.
        client.post(f"/api/brainstorm/{session['id']}/message",
                    json={"text": "and rain?", "to": "art"})
        _quiet()
        again = [a for a in answers["asked"] if a["seat"] == "art"][-1]
        assert {"role": "assistant", "content": "mine"} in again["turns"]

    def test_the_roster_reports_each_seat_and_its_turns(self, client, root,
                                                        answers):
        session = _new(client)
        _invite(client, session["id"], "art")
        for text in ("weather?", "and rain?"):
            client.post(f"/api/brainstorm/{session['id']}/message",
                        json={"text": text, "to": "art"})
            _quiet()
        body = client.get(f"/api/brainstorm/{session['id']}").json()["data"]
        art = next(p for p in body["participants"] if p["seat"] == "art")
        assert art["state"] in _bs.PARTICIPANT_STATES
        assert art["turns"] == 2
        assert art["invited_at"]
        # The header chip's shape, per participant, so the roster draws one with
        # the code that draws the other.
        assert art["thinker"]["runner"] == "claude"

    def test_nothing_a_participant_says_becomes_work(self, client, root,
                                                     answers, monkeypatch):
        """The room's whole promise, with one more voice in it."""
        def boom(*a, **kw):
            raise AssertionError("the brainstorm room dispatched an agent")

        monkeypatch.setattr("bgate_ui.agents.dispatch.dispatch", boom)
        session = _new(client)
        _invite(client, session["id"], "art")
        client.post(f"/api/brainstorm/{session['id']}/message",
                    json={"text": "build the weather system then"})
        _quiet()
        assert _items(root) == []
        assert not hasattr(_bs, "queue")


# ---------------------------------------------------------------------------
# Free discussion — the room answering itself, and the switch that stops it
# ---------------------------------------------------------------------------
class TestTheRoomTalksToItself:
    def _room(self, client, answers, rounds):
        session = _new(client)
        _invite(client, session["id"], "art")
        _invite(client, session["id"], "gameplay")
        got = client.patch(f"/api/brainstorm/{session['id']}",
                           json={"discuss_rounds": rounds})
        assert got.status_code == 200, got.text
        return session

    def test_off_by_default_is_one_round_and_nothing_more(self, client, answers):
        """The old behaviour, byte for byte: nobody starts paying for a feature
        they did not turn on."""
        session = _new(client)
        _invite(client, session["id"], "art")
        client.post(f"/api/brainstorm/{session['id']}/message",
                    json={"text": "weather?"})
        _quiet()
        assert [a["seat"] for a in answers["asked"]] == ["", "art"]

    def test_a_round_of_discussion_asks_everyone_again(self, client, answers):
        session = self._room(client, answers, 1)
        got = client.post(f"/api/brainstorm/{session['id']}/message",
                          json={"text": "weather?"})
        _quiet()
        # Two rounds' worth of turns, in order. The POST cannot report the
        # round any more — it returns as soon as the message is stored — so the
        # turns themselves are the evidence.
        assert [a["seat"] for a in answers["asked"]] == [
            "", "art", "gameplay", "", "art", "gameplay"]
        assert got.json()["data"]["answering"] is True

    def test_the_follow_up_round_reads_what_the_others_just_said(
            self, client, answers):
        """The delivery mechanism is the transcript itself — no synthetic turn
        is injected, so what a seat argues with is the real row."""
        session = self._room(client, answers, 1)
        answers["replies"] = {"art": "two days", "gameplay": "not worth it"}
        client.post(f"/api/brainstorm/{session['id']}/message",
                    json={"text": "weather?"})
        _quiet()
        last_art = [a for a in answers["asked"] if a["seat"] == "art"][-1]
        contents = [t["content"] for t in last_art["turns"]]
        assert "GAMEPLAY SEAT: not worth it" in contents
        # And it is told it is in a discussion, which is what licenses a PASS.
        assert "THE ROOM IS STILL TALKING" in last_art["system"]

    def test_a_pass_is_not_written_into_the_transcript(self, client, root,
                                                       answers):
        session = self._room(client, answers, 2)
        answers["replies"] = {"": "PASS", "art": "PASS", "gameplay": "PASS"}
        client.post(f"/api/brainstorm/{session['id']}/message",
                    json={"text": "weather?"})
        _quiet()
        # Round one still lands — a PASS only means something in a follow-up.
        assert [(m["role"], m["seat"]) for m in _bs.messages(root, session["id"])] == [
            ("user", ""), ("assistant", ""), ("assistant", "art"),
            ("assistant", "gameplay")]
        # …and the silence ends it after ONE follow-up round, not two: six
        # turns were taken, not nine.
        assert [a["seat"] for a in answers["asked"]] == [
            "", "art", "gameplay", "", "art", "gameplay"]

    def test_a_voice_repeating_itself_is_not_written_twice(self, client, root,
                                                           answers):
        """A follow-up round re-asks every voice, and a voice with nothing new
        to say often answers with its previous message again, near-verbatim —
        the room's partner posted the same "amended #41" paragraph twice in a
        row that way. The turn is taken and billed; it is not written down
        twice."""
        session = self._room(client, answers, 1)
        answers["replies"] = {"": "same thing", "art": "same thing",
                              "gameplay": "same thing"}
        client.post(f"/api/brainstorm/{session['id']}/message",
                    json={"text": "weather?"})
        _quiet()
        said = [(m["seat"], m["text"]) for m in _bs.messages(root, session["id"])
                if m["role"] == "assistant"]
        # One each from the opening round, and nothing from the follow-up.
        assert said == [("", "same thing"), ("art", "same thing"),
                        ("gameplay", "same thing")]

    def test_asking_one_seat_never_opens_a_discussion(self, client, answers):
        """A direct question gets an answer, not a debate the human did not
        open — and a one-voice round would be that voice talking to itself."""
        session = self._room(client, answers, 3)
        client.post(f"/api/brainstorm/{session['id']}/message",
                    json={"text": "how long is a weather shader", "to": "art"})
        _quiet()
        assert [a["seat"] for a in answers["asked"]] == ["art"]

    def test_the_ceiling_is_refused_not_clamped(self, client):
        session = _new(client)
        got = client.patch(f"/api/brainstorm/{session['id']}",
                           json={"discuss_rounds": 40})
        assert got.status_code == 400, got.text
        assert "between 0" in got.json()["error"]["message"]
        body = client.get(f"/api/brainstorm/{session['id']}").json()["data"]
        assert body["discuss_rounds"] == 0

    def test_the_setting_is_per_room(self, client, answers):
        loud = self._room(client, answers, 1)
        quiet = _new(client)
        _invite(client, quiet["id"], "art")
        client.post(f"/api/brainstorm/{quiet['id']}/message",
                    json={"text": "weather?"})
        _quiet()
        assert [a["seat"] for a in answers["asked"]] == ["", "art"]
        answers["asked"].clear()
        client.post(f"/api/brainstorm/{loud['id']}/message",
                    json={"text": "weather?"})
        _quiet()
        assert len(answers["asked"]) == 6


# ---------------------------------------------------------------------------
# The migration, and what an old row means
# ---------------------------------------------------------------------------
class TestHistoryIsNotRewritten:
    def test_an_existing_message_has_no_seat_and_that_means_the_partner(
            self, client, root, answers):
        """'' is "the room's own partner", not "unknown".

        Every row written before participants existed was said by the human or
        by the owner's partner, and writing a seat name into them would invent
        an attendee who was never there.
        """
        session = _new(client)
        client.post(f"/api/brainstorm/{session['id']}/message",
                    json={"text": "just the two of us"})
        _quiet()
        rows = _bs.messages(root, session["id"])
        assert {m["seat"] for m in rows} == {""}
        # And the transcript that partner is handed is unchanged by the feature:
        # its own turns are assistant turns, the human's are user turns, and
        # nothing is labelled because there is nobody else to label.
        turns = _bs.transcript(root, session["id"])
        assert turns == [{"role": "user", "content": "just the two of us"},
                         {"role": "assistant",
                          "content": "partner says something"}]

    def test_closing_the_room_stops_every_voice_in_it(self, client, root):
        """A room nobody may speak in must not still be paying three people to
        listen — and a row saying 'live' after its process was reaped is the
        roster lying about the one thing it is for."""
        session = _new(client)
        _invite(client, session["id"], "art")
        assert _bs.participant(root, session["id"], "art")["state"] == "live"
        assert client.post(
            f"/api/brainstorm/{session['id']}/close").status_code == 200
        assert _bs.participant(root, session["id"], "art")["state"] == "invited"
        assert brainsession._live == {}
