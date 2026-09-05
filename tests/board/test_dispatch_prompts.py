"""The dispatch prompt is rendered from prompts/dispatch.txt.

These pin the CONTRACTS the prompt carries - who the agent is, which item,
which project_dir, the seat's own rules, the protocol, the chain and persona
lines - not its exact bytes. A byte-for-byte oracle used to live here (a
120-line copy of the template kept in lockstep by hand); every wording change
had to be made twice, and the second copy tested nothing the first did not.
"""
from __future__ import annotations

import pytest

from bgate_core.board import queue
from bgate_ui.agents import dispatch
from bgate_ui.agents.dispatch import seat_rules


def _item(root, seat="gameplay", **extra) -> dict:
    row = queue.add(root, seat, "make the jump feel right",
                    "coyote time and a buffered input")
    item = dict(queue.get(root, row["id"]))
    item.update(extra)
    return item


@pytest.mark.parametrize("seat", ["gameplay", "art", "narrative", "qa", "tech"])
def test_the_prompt_names_the_seat_the_item_and_the_project(root, seat):
    item = _item(root, seat)
    text = dispatch._prompt_for(str(root), item)
    assert f"You are the {seat.upper()} seat" in text
    assert f"WORK ITEM #{item['id']}" in text and item["title"] in text and item["brief"] in text
    assert f"MCP project_dir: {root}" in text
    assert "Protocol, in order:" in text
    assert f"queue_complete with item_id={item['id']}" in text
    rule = seat_rules(str(root), seat)
    if rule:
        assert rule in text


def test_a_chained_item_carries_its_chain_and_a_lone_item_does_not(root):
    chained = dispatch._prompt_for(str(root), _item(root, chain_id="chain-7", chain_pos=2))
    lone = dispatch._prompt_for(str(root), _item(root))
    assert "chain-7" in chained
    assert "chain-7" not in lone


def test_a_persona_line_is_appended_when_the_seat_has_one(root, monkeypatch):
    monkeypatch.setattr(dispatch, "_persona_line", lambda root, seat: "MANNER: terse, dry, exact.")
    assert "MANNER: terse, dry, exact." in dispatch._prompt_for(str(root), _item(root))


def test_native_images_switch_the_image_policy(root):
    native = dispatch._prompt_for(str(root), _item(root, "art"), native_images=True)
    piped = dispatch._prompt_for(str(root), _item(root, "art"), native_images=False)
    assert native != piped
    assert "NATIVE" in native


def test_a_per_seat_file_replaces_only_the_section_it_names(root, tmp_path,
                                                             monkeypatch):
    base = dispatch.PROMPTS_DIR / "dispatch.txt"
    (tmp_path / "dispatch.txt").write_bytes(base.read_bytes())
    (tmp_path / "dispatch.art.txt").write_text(
        "[[spend]]\nART SPEND RULE: one sheet, then stop.\n"
        "[[nonsense]]\nignored\n", encoding="utf-8")
    monkeypatch.setattr(dispatch, "PROMPTS_DIR", tmp_path)
    art = dispatch._prompt_for(str(root), _item(root, "art"))
    assert "ART SPEND RULE" in art
    assert "SPEND TURNS LIKE THEY COST SOMETHING" not in art
    assert "Protocol, in order:" in art and "APPROVAL GATE" in art
    gameplay = dispatch._prompt_for(str(root), _item(root, "gameplay"))
    assert "SPEND TURNS LIKE THEY COST SOMETHING" in gameplay


def test_the_template_ships_in_the_wheel():
    text = open("pyproject.toml", encoding="utf-8").read()
    assert "agents/prompts/*.txt" in text
