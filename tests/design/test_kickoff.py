"""Project kickoff: the brief and the screenshots a project starts from.

The start that happened by hand in the director chat every time - paste the
brief, attach the screenshots, "pin these in the bible" - now happens at
creation. These pin the three things that have to be true for the director's
first turn to be the same one the human used to type: the brief is on disk
in the director's lane and pointed at from the bible, the images are pinned
refs anchored beside it, and the prompt says where both are.
"""
from __future__ import annotations

import base64
import struct
import zlib

import pytest

from bgate_core.art import refs as _refs
from bgate_core.board import handoff
from bgate_core.design import bible, bible_refs, kickoff
from bgate_core.store import db, project


def _png() -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b""))


DATA_URL = "data:image/png;base64," + base64.b64encode(_png()).decode()
BRIEF = "Build EXIT 67 as a complete, replayable 2D side-scrolling roguelite.\n" * 30


@pytest.fixture()
def root(tmp_path):
    project.init(tmp_path, "Exit 67", pitch="run and gun")
    yield tmp_path
    db.close_all()


class TestSeed:
    def test_nothing_asked_seeds_nothing(self, root):
        out = kickoff.seed(root, "", [])
        assert out["brief"] is None and out["pinned"] == []
        assert [s["title"] for s in bible.list_sections(root, kind="reference")] == []

    def test_brief_lands_in_the_directors_lane_and_the_bible_points_at_it(self, root):
        out = kickoff.seed(root, BRIEF, [])
        assert out["brief"] == "design/brief.md"
        assert (root / "design" / "brief.md").read_text(encoding="utf-8").strip() == BRIEF.strip()
        section = bible.get(root, out["brief_section"])
        assert section["kind"] == "reference"
        assert section["title"] == kickoff.BRIEF_TITLE
        # The section is a pointer plus an excerpt, never the whole brief: a
        # 20 kB body would be the first thing every seat brief's trim cut.
        assert "design/brief.md" in section["body"]
        assert len(section["body"]) < len(BRIEF)

    def test_images_are_pinned_and_anchored_beside_the_brief(self, root):
        out = kickoff.seed(root, BRIEF, [
            {"name": "exit67 sheet 1", "data": DATA_URL},
            {"name": "hud mock", "data": DATA_URL, "kind": "ui"},
        ])
        assert [p["name"] for p in out["pinned"]] == ["exit67-sheet-1", "hud-mock"]
        assert {p["kind"] for p in out["pinned"]} == {"concept", "ui"}
        pinned = {r["name"] for r in _refs.list_refs(root)}
        assert {"exit67-sheet-1", "hud-mock"} <= pinned
        anchored = {a["ref"] for a in bible_refs.list_for_section(root, out["refs_section"])}
        assert anchored == {"exit67-sheet-1", "hud-mock"}

    def test_a_bad_image_is_reported_and_does_not_stop_the_rest(self, root):
        out = kickoff.seed(root, "", [
            {"name": "broken", "data": "data:image/png;base64,@@@"},
            {"name": "fine", "data": DATA_URL},
        ])
        assert [s["name"] for s in out["skipped"]] == ["broken"]
        assert [p["name"] for p in out["pinned"]] == ["fine"]

    def test_a_file_on_disk_pins_too(self, root, tmp_path):
        src = tmp_path / "shot.png"
        src.write_bytes(_png())
        out = kickoff.seed(root, "", [{"name": "shot", "path": str(src)}])
        assert [p["name"] for p in out["pinned"]] == ["shot"]

    def test_seeding_twice_rewrites_rather_than_duplicating(self, root):
        first = kickoff.seed(root, BRIEF, [{"name": "a", "data": DATA_URL}])
        second = kickoff.seed(root, "v2 " + BRIEF, [{"name": "a", "data": DATA_URL}])
        assert second["brief_section"] == first["brief_section"]
        assert second["refs_section"] == first["refs_section"]
        titles = [s["title"] for s in bible.list_sections(root, kind="reference")]
        assert titles.count(kickoff.BRIEF_TITLE) == 1
        assert titles.count(kickoff.REFS_TITLE) == 1
        assert (root / "design" / "brief.md").read_text(encoding="utf-8").startswith("v2 ")

    def test_the_thread_says_the_kickoff_is_pending(self, root):
        kickoff.seed(root, BRIEF, [])
        assert any("kickoff pending" in line.lower() for line in handoff.digest(root))

    def test_caps_are_refused_not_truncated(self, root):
        with pytest.raises(ValueError):
            kickoff.seed(root, "x" * (kickoff.MAX_BRIEF + 1), [])
        with pytest.raises(ValueError):
            kickoff.seed(root, "", [{"name": f"r{i}", "data": DATA_URL}
                                    for i in range(kickoff.MAX_REFS + 1)])


class TestPrompt:
    def test_the_first_turn_names_the_brief_the_refs_and_the_work(self, root):
        seeded = kickoff.seed(root, BRIEF, [{"name": "sheet", "data": DATA_URL}])
        text = kickoff.prompt(root, seeded, project_name="Exit 67")
        assert text.startswith("PROJECT KICKOFF - Exit 67")
        assert "design/brief.md" in text
        assert f"#{seeded['brief_section']}" in text
        assert "sheet" in text and f"#{seeded['refs_section']}" in text
        for tool in ("greenlight_thesis_set", "bible_add", "not_building_add",
                     "queue_add_chain", "bible_ref_attach"):
            assert tool in text
        # The brief itself rides along, in full, after the instructions.
        assert text.rstrip().endswith(BRIEF.strip())

    def test_refused_images_are_named_so_nobody_hunts_for_them(self, root):
        seeded = kickoff.seed(root, "", [{"name": "broken", "data": "nope"}])
        assert "broken" in kickoff.prompt(root, seeded)
