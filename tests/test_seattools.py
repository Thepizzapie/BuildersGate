"""A seat carries the tools it uses, and never fewer.

THE FAILURE THIS GUARDS AGAINST IS NOT THE TOKENS. It is an agent silently
missing a tool it needs, deciding the work is impossible, and reporting that as
a finding. A run that costs 8 million tokens and succeeds is cheaper than one
that costs 3 million and hands back a wrong answer, so every test here is
pointed at "did anything get lost" rather than at the size of the win.
"""
from __future__ import annotations

from bgate_core import seattools


# The tools each seat was MEASURED calling across 76 real agent runs. Not a
# wish list, this is what the logs showed, and it is the floor.
MEASURED = {
    "gameplay": ("godot_test_run", "godot_run", "godot_screenshot",
                 "godot_check_project", "scene_set_property", "scene_wire",
                 "scene_outline", "seat_can_write", "seat_brief",
                 "queue_complete", "queue_claim_next", "queue_add", "queue_get"),
    "art": ("blender_run", "godot_screenshot", "mesh_faceting", "seat_brief",
            "godot_check_project", "godot_test_run", "decision_list",
            "bible_read", "asset_lock", "godot_import_asset", "skin_dominance",
            "godot_deliver_asset", "blender_humanoid_template"),
    "qa": ("godot_test_run", "queue_add", "queue_complete", "seat_brief",
           "queue_claim_next", "queue_get", "mesh_faceting", "godot_screenshot",
           "seat_post_note", "asset_status", "godot_check_project"),
    "director": ("queue_get", "seat_can_write", "seat_brief", "asset_status",
                 "queue_update", "queue_complete", "decision_add"),
}


def test_every_seat_keeps_every_tool_it_was_measured_using():
    for seat, tools in MEASURED.items():
        lost = [t for t in tools if not seattools.seat_registers(t, seat)]
        assert not lost, f"{seat} lost measured tools: {lost}"


def test_an_unknown_seat_gets_everything():
    """A hand-started session has no BGATE_SEAT. The human at the keyboard is
    not the thing being budgeted, and a typo'd seat name must fail open."""
    for seat in ("", "   ", "nonesuch", None):
        assert seattools.tools_for_seat(seat) is None
        assert seattools.seat_registers("cinematic_plan", seat) is True


def test_a_seat_does_not_carry_another_seat_s_speciality():
    """The control. Without this the whole module could return True always and
    every test above would still pass."""
    assert not seattools.seat_registers("cinematic_plan", "gameplay")
    assert not seattools.seat_registers("image_sprites", "gameplay")
    assert not seattools.seat_registers("blender_rig", "qa")
    assert not seattools.seat_registers("music_generate", "director")


def test_core_reaches_every_seat():
    """A seat that cannot read its own item or check its lane cannot work at
    all, so CORE is not negotiable per seat."""
    for seat in MEASURED:
        for tool in ("queue_get", "seat_brief", "seat_can_write", "bible_read",
                     "board_digest", "tool_index", "ask_human"):
            assert seattools.seat_registers(tool, seat), f"{seat} lost {tool}"


def test_prefixes_cover_tools_that_do_not_exist_yet():
    """Families are matched by prefix so a new tool is available the day it is
    written, not the day someone remembers this file."""
    assert seattools.seat_registers("godot_some_new_check", "gameplay")
    assert seattools.seat_registers("blender_some_new_op", "art")
    assert seattools.seat_registers("queue_anything", "director")


def test_every_named_seat_is_a_real_seat():
    """A typo here would silently hand a seat the unknown-seat fallback -
    everything, which is the opposite of what this module is for."""
    from bgate_core.board import seats as _seats

    known = set(_seats.DEFAULT_SEATS)
    unknown = set(seattools.SEATS) - known
    assert not unknown, f"seattools names seats that do not exist: {unknown}"
    # And the reverse, which is the one that actually bites: a seat the product
    # ships but this file forgot gets the unknown-seat fallback, every tool -
    # so the saving silently does not apply to it.
    forgotten = known - set(seattools.SEATS)
    assert not forgotten, f"seats with no toolset, so they pay full price: {forgotten}"


def test_every_seat_gets_the_engine_neutral_spine_and_the_engine_families():
    """A seat brief on a web or Unity project names engine_check,
    engine_screenshot, web_test_run and unity_test_run as the way to verify;
    a seat that cannot register them is told to call tools it does not have."""
    from bgate_core.seattools import seat_registers as ok
    for seat in ("gameplay", "tech", "art", "audio", "cinematic", "qa", "narrative", "director"):
        assert ok("engine_status", seat) and ok("engine_check", seat) and ok("engine_screenshot", seat), seat
    assert ok("web_build", "gameplay") and ok("web_test_run", "qa") and ok("unity_test_run", "qa")
    assert ok("unity_check", "tech") and ok("project_set_engine", "tech")
    assert not ok("web_build", "narrative")
