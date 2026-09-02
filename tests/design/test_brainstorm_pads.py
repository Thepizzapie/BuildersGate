"""The three-tool pad server, and the line it is allowed to cross.

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
  * the surface is exactly three tools, and the names match what runners.py
    allowlists — a drift between those two is how a fourth tool ships approved;
  * the board is READ-ONLY. queue is importable now, which is exactly the crack
    this file exists to guard, so the assertion is no longer "queue is
    unreachable" but "only queue's read functions are called";
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

import pathlib

import pytest

from bgate_core.design import brainstorm as _bs
from bgate_mcp import padserver
from bgate_ui.agents import runners as _runners

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


class TestTheSurfaceIsWhatTheAllowlistSays:
    def test_the_names_match_the_allowlist_exactly(self):
        """A new tool must not be able to arrive already approved.

        runners.py names them by their full CLI-side names rather than by the
        `mcp__pads` prefix, so the allowlist cannot silently widen — but that
        only holds while the two lists agree, which is what this checks.
        """
        assert sorted(padserver.TOOL_NAMES) == sorted(_runners.PAD_TOOLS)

    def test_nothing_on_the_surface_can_file_work_or_dispatch(self):
        """The count used to be the guard, and a count is the wrong guard: it
        forbids growth rather than forbidding the thing that matters. The canon
        tools grew this surface on purpose. What must never appear is a name
        that files, moves or starts work — the board is reached by a human
        pressing Deploy and by nothing else in this process."""
        banned = ("queue", "dispatch", "deploy", "approve", "stop", "steer",
                  "spawn", "run", "write_file", "shell")
        for name in padserver.TOOL_NAMES:
            leaf = name.rsplit("__", 1)[-1]
            for word in banned:
                assert word not in leaf, f"{name} reads like a door to the board"

    def test_the_module_reaches_nothing_else(self):
        """No queue, no repo, no generators, no subprocess — BY IMPORT, which
        is what this now checks.

        It used to grep the whole source for the banned words, so the module
        describing what it deliberately cannot do ("Nothing here queues work,
        dispatches anyone or writes a project file") counted as doing it. The
        docstring already said "by import, not by intention"; the assertion did
        not. Walking the AST asserts the property that matters and leaves the
        module free to explain itself.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(padserver))
        reached: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                reached.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                reached.add(base)
                reached.update(f"{base}.{a.name}" for a in node.names)

        joined = " ".join(reached).lower()
        for banned in ("subprocess", "blender", "image_generate",
                       "godot", "shutil", "requests"):
            assert banned not in joined, f"{banned} is reachable: {sorted(reached)}"

    def test_the_board_is_reachable_but_only_to_read(self):
        """queue IS imported now, so the import ban cannot be the guard any more.

        board_read exists because the seat holding these tools is the DIRECTOR,
        and a director reasoning about priority with no access to the list of
        work is guessing. Reading files nothing. But `queue` also holds `add`,
        `set_status`, `approve` and `stop` — the writes that would end the
        room's promise that NOTHING IS FILED until a human presses Deploy.

        So the assertion moved down a level: walk every attribute this module
        takes off the queue module and require it to be a read. A future edit
        that reaches for queue.add fails HERE, which is the whole point.
        """
        import ast
        import inspect

        allowed = {"list_items", "get", "chain", "blocker", "parents",
                   "successors", "awaiting_review"}
        tree = ast.parse(inspect.getsource(padserver))
        used = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "_queue"
        }
        assert used, "nothing calls _queue — this test has gone vacuous"
        forbidden = sorted(used - allowed)
        assert not forbidden, (
            f"padserver calls queue.{{{', '.join(forbidden)}}}, which is not a "
            "read. The brainstorm room promises no work is filed until a human "
            "presses Deploy; a write here ends that promise.")

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


# ---------------------------------------------------------------------------
# CANON — the boundary that moved, and the three things that keep it honest
# ---------------------------------------------------------------------------
class TestTheRoomCanReadAndWriteCanon:
    """A seat invited to think about this world could not read what the world
    already says, so it argued from the transcript and contradicted the bible
    confidently; and it could not write down the one thing it was asked for, so
    a decision reached in the room died there unless the human retyped it.

    Both are fixed here, and the line that did NOT move is asserted alongside:
    canon is a row in this project's database, not work on the board.
    """

    def test_canon_read_returns_the_bible_and_the_lore(self, pad):
        from bgate_core.design import bible as _bible, lore as _lore

        _bible.add(pad["root"], "pillar", "Deadlines kill",
                   "the tower runs on a clock")
        ent = _lore.add_entity(pad["root"], "character", "The KPI",
                               "the AI overlord")
        _lore.add_fact(pad["root"], ent["slug"], "speaks only in metrics")

        got = padserver.canon_read()
        assert got["ok"] is True
        assert any(s["title"] == "Deadlines kill" for s in got["sections"])
        kpi = next(e for e in got["entities"] if e["name"] == "The KPI")
        assert "speaks only in metrics" in [f["statement"] for f in kpi["facts"]]

    def test_canon_read_filters_by_word(self, pad):
        from bgate_core.design import bible as _bible

        _bible.add(pad["root"], "pillar", "Deadlines kill", "")
        _bible.add(pad["root"], "constraint", "One tower only", "")
        got = padserver.canon_read(q="tower")
        assert [s["title"] for s in got["sections"]] == ["One tower only"]

    def test_a_bible_write_lands_and_announces_itself_under_its_seat(
            self, pad, monkeypatch):
        """The write is allowed BECAUSE it is visible. A seat that can change
        the bible silently changed it without the human reading it."""
        monkeypatch.setenv("BGATE_BRAINSTORM_SEAT", "narrative")
        got = padserver.bible_write("The clock is the antagonist",
                                    "every floor is timed", kind="pillar")
        assert got["ok"] is True and got["action"] == "wrote"

        from bgate_core.design import bible as _bible
        assert any(s["title"] == "The clock is the antagonist"
                   for s in _bible.list_sections(pad["root"]))
        said = _bs.messages(pad["root"], pad["id"])[-1]
        assert said["seat"] == "narrative"
        assert "[canon]" in said["text"] and "clock" in said["text"]

    def test_amending_a_section_does_not_add_a_second_one(self, pad):
        from bgate_core.design import bible as _bible

        first = _bible.add(pad["root"], "pillar", "Deadlines kill", "old")
        got = padserver.bible_write("Deadlines kill", "new body",
                                    section_id=first["id"],
                                    expect=_bible.version_of(first))
        assert got["action"] == "amended"
        rows = [s for s in _bible.list_sections(pad["root"])
                if s["title"] == "Deadlines kill"]
        assert len(rows) == 1 and rows[0]["body"] == "new body"

    def test_a_blind_amend_is_refused_and_hands_back_the_version(self, pad):
        """No `expect`, no write. A body is stored whole, so an amend that did
        not say what it was editing silently erases whatever another voice
        wrote in between."""
        from bgate_core.design import bible as _bible

        first = _bible.add(pad["root"], "pillar", "Deadlines kill", "old")
        got = padserver.bible_write("Deadlines kill", "clobbered",
                                    section_id=first["id"])
        assert got["ok"] is False and got["code"] == "version_required"
        assert got["section"]["version"] == _bible.version_of(first)
        assert _bible.get(pad["root"], first["id"])["body"] == "old"

    def test_two_voices_amending_the_same_section_cannot_lose_an_edit(self, pad):
        """THE #41 RACE, as it actually happened: the gameplay seat wrote a
        constraint and the room's partner amended it seconds later, in the same
        round, from a copy read before the first write landed. Whole-body
        writes meant the second silently won."""
        from bgate_core.design import bible as _bible

        section = _bible.add(pad["root"], "constraint", "QA", "first draft")
        stale = _bible.version_of(section)          # what both voices read

        first = padserver.bible_write("QA", "gameplay's version",
                                      section_id=section["id"], expect=stale)
        assert first["ok"] is True

        second = padserver.bible_write("QA", "the partner's version",
                                       section_id=section["id"], expect=stale)
        assert second["ok"] is False and second["code"] == "stale"
        # The refusal carries what is actually there, so the loser can fold its
        # change in rather than going back through canon_read.
        assert second["section"]["body"] == "gameplay's version"
        assert _bible.get(pad["root"], section["id"])["body"] == "gameplay's version"

        # And the fresh version is what lets the retry land.
        again = padserver.bible_write("QA", "both, merged",
                                      section_id=section["id"],
                                      expect=second["section"]["version"])
        assert again["ok"] is True
        assert _bible.get(pad["root"], section["id"])["body"] == "both, merged"

    def test_canon_read_hands_out_the_version_an_amend_needs(self, pad):
        from bgate_core.design import bible as _bible

        made = _bible.add(pad["root"], "pillar", "Deadlines kill", "body")
        seen = next(s for s in padserver.canon_read()["sections"]
                    if s["id"] == made["id"])
        assert seen["version"] == _bible.version_of(made)

    def test_lore_write_fact_and_link_all_land(self, pad):
        from bgate_core.design import lore as _lore

        a = padserver.lore_write("Floor Thirty One", kind="place",
                                 summary="where the boss waits")
        b = padserver.lore_write("The KPI", kind="character")
        assert a["ok"] and b["ok"]
        fact = padserver.lore_fact(a["entity"]["slug"], "has no windows")
        assert fact["ok"] is True
        link = padserver.lore_link(b["entity"]["slug"], "rules",
                                   a["entity"]["slug"])
        assert link["ok"] is True
        facts = _lore.facts_of(pad["root"], a["entity"]["slug"])
        assert [f["statement"] for f in facts] == ["has no windows"]
        assert [l["rel"] for l in _lore.links_of(pad["root"], b["entity"]["slug"])] \
            == ["rules"]

    def test_a_fact_written_here_is_sourced_to_the_room_and_not_locked(
            self, pad, monkeypatch):
        """Locking is settled canon and that is a human's call. A room that
        could lock a fact could end an argument by writing it down."""
        monkeypatch.setenv("BGATE_BRAINSTORM_SEAT", "art")
        from bgate_core.design import lore as _lore

        padserver.lore_write("The KPI", kind="character")
        padserver.lore_fact("the-kpi", "is rendered as a spreadsheet")
        fact = _lore.facts_of(pad["root"], "the-kpi")[0]
        assert fact["locked"] == 0
        assert "art" in fact["source"] and str(pad["id"]) in fact["source"]

    def test_amending_an_entity_cannot_rename_it(self, pad):
        """The slug is derived from the name and is how every fact and link
        finds the entity — renaming through this door would orphan them."""
        made = padserver.lore_write("The KPI", kind="character")
        padserver.lore_write("Something Else", entity=made["entity"]["slug"],
                             summary="amended")
        from bgate_core.design import lore as _lore
        again = _lore.get_entity(pad["root"], made["entity"]["slug"])
        assert again["name"] == "The KPI" and again["summary"] == "amended"

    def test_room_post_puts_a_seats_own_idea_in_front_of_the_room(
            self, pad, monkeypatch):
        """The collaboration door: a seat that noticed a collision mid-thought
        had nowhere to put it until its next turn came round."""
        monkeypatch.setenv("BGATE_BRAINSTORM_SEAT", "tech")
        got = padserver.room_post("the art plan and the tile budget collide")
        assert got["ok"] is True and got["seat"] == "tech"
        last = _bs.messages(pad["root"], pad["id"])[-1]
        assert last["role"] == "assistant" and last["seat"] == "tech"
        assert "collide" in last["text"]

    def test_a_canon_tool_cannot_reach_another_room(self, pad, monkeypatch):
        """Same guarantee as the pads: the session is the environment's, never
        an argument, so nothing can post into a room it was not spawned for."""
        import inspect

        src = inspect.getsource(padserver.room_post)
        assert "_session_id()" in src and "session_id=" not in src

    def test_the_seat_is_stamped_by_the_spawner_not_claimed_by_the_model(self):
        """`seat` is a parameter of config(), not of any tool. A model that
        could name its own seat could sign another seat's name to canon."""
        cfg = padserver.config("/tmp/x", 7, seat="art")
        assert cfg["mcpServers"]["pads"]["env"]["BGATE_BRAINSTORM_SEAT"] == "art"
        import inspect

        for tool in (padserver.bible_write, padserver.lore_fact,
                     padserver.room_post):
            assert "seat" not in inspect.signature(tool).parameters


class TestTheRoomReadsTheGameAndNothingElse:
    """GROUND TRUTH IS SCOPED TO THE PROJECT, and the scoping is the feature.

    project_files/file_read/scene_tree exist because a room whose only evidence
    was past task results re-derived the board's answer forever. They read the
    repository — so the only thing standing between a seat and the rest of the
    machine is the containment check, and a seat is semi-trusted: project files
    it reads can carry instructions.
    """

    ESCAPES = [
        "../../secrets.txt",
        r"..\..\secrets.txt",
        "/etc/passwd",
        "C:/Windows/System32/drivers/etc/hosts",
        r"\\server\share\x",
        # DRIVE-RELATIVE, and the one that actually got out. Joining an anchored
        # path discards the base, so `project / "C:pyproject.toml"` is just
        # "C:pyproject.toml" — resolved against the PROCESS's working directory,
        # which is not the project. It read a file two directories above the
        # game before this was refused.
        "C:pyproject.toml",
        "game/../../pyproject.toml",
        "....//....//pyproject.toml",
    ]

    @pytest.mark.parametrize("path", ESCAPES)
    def test_file_read_cannot_leave_the_project(self, pad, path):
        got = padserver.file_read(path)
        assert got["ok"] is False, f"{path!r} was read from outside the project"

    @pytest.mark.parametrize("path", ESCAPES)
    def test_scene_tree_cannot_leave_the_project(self, pad, path):
        got = padserver.scene_tree(path)
        assert got["ok"] is False, f"{path!r} was read from outside the project"

    @pytest.mark.parametrize("pattern", ["../../*", "../*", r"..\..\*"])
    def test_a_climbing_glob_matches_nothing(self, pad, pattern):
        """pathlib ACCEPTS `../../*` and walks straight out of the directory —
        measured, not assumed. Every hit is re-checked, and one that escaped is
        skipped rather than returned."""
        got = padserver.project_files(pattern=pattern, limit=5)
        assert got["ok"] is True and got["total"] == 0

    def test_a_normal_read_still_works(self, pad):
        """The guard is worth nothing if it also refuses the legitimate case."""
        target = pathlib.Path(pad["root"]) / "hello.txt"
        target.write_text("in the project\n", encoding="utf-8")
        got = padserver.file_read("hello.txt")
        assert got["ok"] is True and "in the project" in got["text"]


class TestSecretsInsideTheProjectAreStillOffLimits:
    """CONTAINMENT WAS NOT THE WHOLE QUESTION.

    `bgate key set` writes .env at the PROJECT ROOT, and game_dir() returns the
    root itself for every project that keeps project.godot at the top — which is
    what `bgate init` and `bgate adopt` produce. So "inside the project" included
    the API keys, and the seat holding these tools reads dialogue, scenes and
    other seats' messages, all of which an attacker can influence. It also holds
    room_post and lore_fact, which would carry whatever it read into the
    transcript and the project's own database.
    """

    @pytest.mark.parametrize("path", [
        ".env", ".env.local", ".git/config", ".bgate/game.db",
        ".claude/settings.json", ".ssh/id_rsa",
    ])
    def test_file_read_refuses_it(self, pad, path):
        got = padserver.file_read(path)
        assert got["ok"] is False
        assert "off limits" in got["error"]

    def test_a_listing_does_not_even_name_the_env_file(self, pad):
        """Listed is disclosed: a seat that sees .env in a file list knows the
        key is there and where."""
        (pathlib.Path(pad["root"]) / ".env").write_text(
            "OPENAI_API_KEY=sk-not-a-real-key\n", encoding="utf-8")
        got = padserver.project_files(pattern="**/*", limit=200)
        assert got["ok"] is True
        assert not [f for f in got["files"] if f["path"].endswith(".env")]

    def test_the_rest_of_the_project_still_reads(self, pad):
        (pathlib.Path(pad["root"]) / "design.md").write_text(
            "the tower\n", encoding="utf-8")
        got = padserver.file_read("design.md")
        assert got["ok"] is True and "the tower" in got["text"]
