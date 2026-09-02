"""The paid flag on the tool-node table — the registry the money gate reads.

``workflows._node_spends`` and the route's ``spends_money`` both answer from
``wfnodes.REGISTRY``, so a paying tool that arrives without ``paid=True`` is a
node an agent can fire and a tick can auto-start. The list below is the human
answer to "which tools bill" — written out, not derived, because deriving it
from the flag it checks would assert nothing.
"""
from __future__ import annotations

from bgate_core.board import wfnodes, workflows

# Every tool name that reaches a provider that bills, whether or not a palette
# card exists for it yet. The families' read-side tools (image_status,
# item_classes, music_options, cinematic_plan, ...) are free and must stay
# unmarked — a free node that needed a human press would make every graph a
# sequence of clicks.
PAYING_TOOLS = {
    # image family
    "image_generate", "image_edit", "image_sprites", "image_talkhead",
    # item family
    "item_generate", "item_variants",
    # audio
    "music_generate", "voice_speak",
    # video / narrative
    "kie_video_generate", "cinematic_generate_shot",
    "storyboard_frame_generate", "storyboard_auto",
    # asset generators
    "character_generate", "animation_generate", "tileset_generate",
    "prop_generate",
}


class TestPaidCoverage:
    def test_every_node_calling_a_paying_tool_is_marked_paid(self):
        for node_type, spec in wfnodes.REGISTRY.items():
            if spec.tool in PAYING_TOOLS:
                assert spec.paid is True, (
                    f"{node_type} calls {spec.tool}, which bills, and is not "
                    "marked paid — an agent can fire it and a tick will "
                    "auto-start it")

    def test_no_free_tool_is_marked_paid(self):
        # The other direction keeps the flag meaningful: a status node badged
        # PAID trains users to ignore the badge.
        for node_type, spec in wfnodes.REGISTRY.items():
            if spec.tool not in PAYING_TOOLS:
                assert spec.paid is False, (
                    f"{node_type} calls {spec.tool}, which does not bill, and "
                    "is marked paid")

    def test_the_palette_carries_the_flag(self):
        cards = {c["type"]: c["paid"] for c in wfnodes.catalogue()}
        for node_type, spec in wfnodes.REGISTRY.items():
            assert cards[node_type] == spec.paid

    def test_the_engine_reads_the_flag_from_the_table_not_the_graph(self):
        # A hand-POSTed graph cannot declare its way out of the bill.
        lying = {"type": "tool.music.generate", "kind": "tool",
                 "paid": False, "config": {}}
        assert workflows._tool_paid(lying) is True
        assert workflows._node_spends(lying) is True

    def test_a_generate_node_spends_by_definition(self):
        # bgate_core.board.generate does nothing but call an image provider, so the
        # kind itself is the flag — no table entry to forget.
        assert workflows._node_spends(
            {"type": "model.image", "kind": "generate"}) is True

    def test_an_unknown_tool_does_not_spend(self):
        assert workflows._node_spends(
            {"type": "tool.from.the.future", "kind": "tool"}) is False


class TestErrorOutputsAreNotSatisfying:
    def test_every_error_key_reads_as_a_problem(self):
        # Three writers, one meaning. Only flow_error used to be inspected, so
        # a bible section that did not exist painted its node green and the
        # generation behind it billed for a prompt with no subject.
        for key in ("flow_error", "context_error", "ref_error"):
            assert workflows._passive_problem({key: "went wrong"}) == "went wrong"
        assert workflows._passive_problem({"text": "fine"}) == ""
        assert workflows._passive_problem({}) == ""
