"""A constraint the human stated is a RULING, and the harness enforces it.

WRITTEN FROM ONE NIGHT. On EXIT 67 (2026-09-20) the human said on night one
that a Metal Slug run-and-gun needs a 2D rig and that generated frame sheets
would not carry it. The director answered "trust the process" and dispatched
the cast on image_sprites. Every art failure of the next twelve hours followed
from that, and nothing in the harness could have refused it: the bible had a
`constraint` kind with no idea who stated it, no seat it bound and no tool it
forbade. These tests are the three places that statement now bites:

  * the seat brief and the dispatch prompt carry it, untrimmed;
  * a dispatched agent's call to a forbidden tool is refused;
  * a brief that names a forbidden tool does not dispatch.

And the one that keeps it honest: an agent cannot write stated_by='human'.
"""
from __future__ import annotations

import json

import pytest

from bgate_core.board import queue, seats
from bgate_core.design import bible
from bgate_mcp import server
from bgate_ui.agents import dispatch as _dispatch


def _rule(root, **over):
    fields = dict(kind="constraint",
                  title="Characters are cutout rigs, not frame sheets",
                  body="A run-and-gun needs 2D rigging. Per-frame generation "
                       "re-rolls identity, proportions and the weapon in hand "
                       "on every call, and no gate can hold them.",
                  stated_by="human", binds=["art"],
                  forbids=["image_sprites", "sprite_fit*"])
    fields.update(over)
    return bible.add(root, **fields)


async def call(tool: str, /, **kwargs) -> dict:
    result = await server.mcp.call_tool(tool, kwargs)
    content = result[0] if isinstance(result, tuple) else result
    block = content[0]
    return json.loads(block.text) if hasattr(block, "text") else block


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

class TestProvenance:
    def test_a_ruling_records_who_what_and_whom(self, root):
        got = _rule(root)
        assert got["stated_by"] == "human"
        assert got["binds"] == "art"
        assert got["forbids"] == "image_sprites,sprite_fit*"

    def test_lists_are_normalised_not_trusted(self, root):
        got = _rule(root, binds="Art, TECH", forbids=["Image_Sprites"])
        assert got["binds"] == "art,tech"
        assert got["forbids"] == "image_sprites"

    def test_an_unknown_stated_by_is_refused(self, root):
        with pytest.raises(ValueError):
            _rule(root, stated_by="the vibes")

    def test_binds_and_forbids_mean_nothing_on_a_pillar(self, root):
        with pytest.raises(bible.NotAConstraint):
            bible.add(root, "pillar", "Tension", stated_by="human",
                      forbids=["image_sprites"])

    def test_update_keeps_provenance_unless_told_otherwise(self, root):
        sid = _rule(root)["id"]
        after = bible.update(root, sid, body="sharper wording")
        assert after["stated_by"] == "human"
        assert after["forbids"] == "image_sprites,sprite_fit*"
        cleared = bible.update(root, sid, forbids=[])
        assert cleared["forbids"] == ""

    def test_plain_constraints_are_not_rulings(self, root):
        bible.add(root, "constraint", "prefer warm palettes")
        assert bible.rulings(root) == []
        _rule(root)
        assert [r["title"] for r in bible.rulings(root)] == [
            "Characters are cutout rigs, not frame sheets"]


class TestReach:
    def test_binds_scopes_the_ruling_to_seats(self, root):
        _rule(root)
        assert bible.rulings(root, "art")
        assert bible.rulings(root, "tech") == []

    def test_an_unbound_ruling_reaches_every_seat(self, root):
        _rule(root, binds=[])
        assert bible.rulings(root, "tech")

    def test_forbidden_tool_and_wildcard(self, root):
        _rule(root)
        assert bible.tool_forbidden(root, "art", "image_sprites")["title"]
        assert bible.tool_forbidden(root, "art", "sprite_fit_batch")
        assert bible.tool_forbidden(root, "art", "image_generate") is None
        assert bible.tool_forbidden(root, "tech", "image_sprites") is None

    def test_brief_violations_match_whole_tool_names(self, root):
        _rule(root)
        hits = bible.brief_violations(
            root, "art", "Regenerate the gnome with image_sprites, 8 frames")
        assert [h["tool"] for h in hits] == ["image_sprites"]
        # A brief that merely contains the letters is not a violation.
        assert bible.brief_violations(root, "art", "reimage_sprites_v2") == []
        assert bible.brief_violations(root, "tech", "use image_sprites") == []

    def test_describe_names_the_forbidden_tools(self, root):
        _rule(root)
        text = bible.describe_rulings(root, "art")
        assert "HUMAN RULINGS" in text
        assert "image_sprites" in text and "sprite_fit*" in text
        assert bible.describe_rulings(root, "tech") == ""


# ---------------------------------------------------------------------------
# Where it reaches the agent
# ---------------------------------------------------------------------------

class TestBriefAndPrompt:
    def test_the_seat_brief_carries_rulings_past_the_bible_trim(self, root):
        _rule(root, body="x" * 1500)   # past BODY_CHARS, the bible's per-section cap
        brief = seats.brief(root, "art")
        assert len(brief["rulings"]) == 1
        assert len(brief["rulings"][0]["body"]) == 1500
        assert brief["rulings"][0]["forbids"] == ["image_sprites", "sprite_fit*"]
        assert any("`rulings` above outrank" in rule for rule in brief["rules"])
        assert seats.brief(root, "tech")["rulings"] == []

    def test_an_over_budget_brief_keeps_the_ruling_and_its_tools(self, root):
        # The art brief is ~21k of fixed prose under a 24k ceiling; a long
        # ruling pushes it over. What the fitter may cut is prose past a page,
        # never the title or the forbidden tools.
        _rule(root, body="x" * 6000)
        brief = seats.brief(root, "art")
        assert len(json.dumps(brief, default=str)) <= seats.BRIEF_CHARS
        assert brief["rulings"][0]["title"]
        assert len(brief["rulings"][0]["body"]) == 1200
        assert brief["rulings"][0]["forbids"] == ["image_sprites", "sprite_fit*"]

    def test_the_dispatch_prompt_prints_the_ruling_under_the_item(self, root):
        _rule(root)
        item = queue.add(root, "art", "draw the gnome",
                         brief="cutout kit for the gnome")
        text = _dispatch._prompt_for(str(root), queue.get(root, item["id"]))
        assert "HUMAN RULINGS BINDING THIS SEAT" in text
        assert "image_sprites" in text
        # Under the item and before the protocol, like the canon block.
        assert text.index("HUMAN RULINGS") < text.index("Protocol, in order")

    def test_the_director_protocol_states_the_rule(self):
        assert "BIBLE CONSTRAINT BEFORE IT IS A WORK" in seats.DIRECTOR_PROTOCOL
        assert "stated_by='human'" in seats.DIRECTOR_PROTOCOL


class TestDispatchGate:
    def test_a_brief_naming_a_forbidden_tool_does_not_dispatch(
            self, root, monkeypatch):
        monkeypatch.setattr(_dispatch, "find_claude", lambda: "claude")
        _rule(root)
        item = queue.add(root, "art", "gnome frames",
                         brief="run image_sprites for the gnome, 6 poses")
        got = _dispatch.dispatch(str(root), item["id"])
        assert got["ok"] is False
        assert got["code"] == "forbidden_by_ruling"
        assert got["detail"]["tools"] == ["image_sprites"]
        assert "image_sprites" in got["error"]
        # Still queued: nothing was reserved, nothing needs releasing.
        assert queue.get(root, item["id"])["status"] == "queued"

    def test_a_ruling_for_another_seat_does_not_hold_this_one(
            self, root, monkeypatch):
        # The gate sits inside _spawn before the reservation; stub everything
        # past it so the test can never start a real process.
        seen = {}

        def _fake_reserve(root_, item_id):
            seen["reached_reservation"] = item_id
            return False
        monkeypatch.setattr(_dispatch._queue, "reserve", _fake_reserve)
        _rule(root)
        item = queue.add(root, "tech", "wire the sheet",
                         brief="load the image_sprites output into the scene")
        got = _dispatch._spawn(str(root), item["id"])
        assert got.get("code") == "not_queued", got     # past the ruling gate
        assert seen["reached_reservation"] == item["id"]


# ---------------------------------------------------------------------------
# The MCP surface
# ---------------------------------------------------------------------------

@pytest.fixture()
def wired(root, monkeypatch):
    monkeypatch.setenv("BGATE_ROOT", str(root))
    monkeypatch.delenv("BGATE_SEAT", raising=False)
    monkeypatch.delenv("BGATE_WORK_ITEM", raising=False)
    monkeypatch.delenv("BGATE_ACTOR", raising=False)
    return root


@pytest.mark.anyio
async def test_the_human_session_records_a_ruling(wired):
    got = await call("bible_add", kind="constraint", title="Rigs not sheets",
                     stated_by="human", binds=["art"], forbids=["image_sprites"])
    assert got["stated_by"] == "human"
    assert got["forbids"] == "image_sprites"


@pytest.mark.anyio
async def test_an_agent_may_not_record_or_alter_a_human_ruling(wired, monkeypatch):
    sid = _rule(wired)["id"]
    monkeypatch.setenv("BGATE_SEAT", "art")
    refused = await call("bible_add", kind="constraint", title="my own rule",
                         stated_by="human")
    assert "error" in refused and "HUMAN ruling" in refused["error"]
    # A dispatched agent may still write a constraint it does not sign as human.
    ok = await call("bible_add", kind="constraint", title="my own rule",
                    stated_by="agent")
    assert ok["stated_by"] == "agent"
    lifted = await call("bible_update", section_id=sid, forbids=[])
    assert "error" in lifted
    reworded = await call("bible_update", section_id=sid, body="clearer")
    assert reworded["body"] == "clearer" and reworded["forbids"]


@pytest.mark.anyio
async def test_a_bound_seat_is_refused_the_forbidden_tool(wired, monkeypatch):
    _rule(wired, forbids=["cutout_templates"])
    monkeypatch.setenv("BGATE_SEAT", "art")
    got = await call("cutout_templates")
    assert got["ok"] is False
    assert got["refused"] == "ruling"
    assert "HUMAN RULING" in got["error"]
    # An unbound seat, and the human's own session, are not.
    monkeypatch.setenv("BGATE_SEAT", "tech")
    assert (await call("cutout_templates"))["ok"] is True
    monkeypatch.delenv("BGATE_SEAT")
    assert (await call("cutout_templates"))["ok"] is True
