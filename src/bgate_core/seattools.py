"""Which tools a seat's agent actually needs in its context.

MEASURED, NOT GUESSED. On one project's agent logs, across every run:

    seat        distinct bgate tools actually called
    art         21
    gameplay    22
    qa          12
    director     7

against **256 registered**. So 92% of the catalogue was dead weight in any
given run — and it is not free weight, because a tool's schema rides in the
system prompt of EVERY turn.

WHAT THAT COSTS, measured on the same project in one morning:

    256 tool docstrings + signatures = 378,589 characters ~= 105,000 tokens
    ... present on turn 1 and re-read on all 199 turns after it.

    item  seat      turns   cache_read   ctx/turn
    65    gameplay    200   23,757,514    118,788
    70    art          87   12,353,446    141,994
    71    qa           77   12,192,070    158,339

71.2 MILLION tokens in a day, of which 69.4M — 97.5% — was cache_read. Total
INPUT across every run was 844 tokens. The agents were barely reading the game
at all; they were re-reading the tool catalogue, two hundred times each.

That is the whole finding: on a subscription, context size is multiplied by
turn count, so a schema nobody calls is not idle — it is billed on every turn
of every run forever. `bgate_core.modules` already knew this ("~200 tool
schemas ride in every agent's context on every turn") and gated by FEATURE.
This gates by WHO IS ASKING, which is the bigger cut, because a gameplay agent
editing GDScript has no use for cinematic_plan, image_sprites or blender_rig no
matter which modules the project has switched on.

HOW THE LISTS ARE BUILT, and the rule that keeps this from breaking work:

  * CORE is every seat's, always — the board, the brief, the bible, the lane
    check, spend, notes. A seat that cannot read its own item cannot work.
  * Each seat then adds the families its measured usage shows it using.
  * PREFIXES, not exact names, so a new tool in an existing family is available
    the day it is written rather than the day someone remembers this file.
  * AN UNKNOWN SEAT GETS EVERYTHING. A missing toolset must only ever be the
    result of a stored decision, never of a name this file has not heard of —
    the same rule `_module_registers` follows for an unreadable module choice.

WHEN A SEAT IS GENUINELY MISSING A TOOL, add it here rather than working
around it. The failure mode this must never produce is an agent silently
unable to do its job and reporting the absence as the work being impossible;
that is worse than the tokens. Both `seat_brief` and `tool_index` stay in CORE
so a seat can always ask what it has.
"""
from __future__ import annotations

__all__ = ["tools_for_seat", "seat_registers"]


#: Every seat gets these. The board, its own instructions, canon, and the
#: safety rails — none of which are optional to doing any work at all.
CORE: tuple[str, ...] = (
    "project_status", "project_select", "tool_index",
    "seat_brief", "seat_list", "seat_can_write", "seat_notes", "seat_post_note",
    "queue_", "board_digest", "handoff_note", "handoff_read",
    "bible_read", "decision_list", "decision_add", "pending_decisions",
    "not_building_list", "recall", "ask_human",
    "asset_status", "asset_lock", "asset_release", "asset_track", "asset_verify",
    "agent_activity", "provider_status", "bgate_doctor",
)

#: Seat -> the extra families its MEASURED usage shows it reaching for.
SEATS: dict[str, tuple[str, ...]] = {
    # Mechanics, systems and feel: edits scripts and scenes, drives the engine,
    # and proves it with the engine rather than by reading.
    "gameplay": (
        "godot_", "scene_", "traversal_prove", "scale_check", "scale_record_3d",
        "level_", "encounter_design_set", "room_", "playtest_", "evidence_",
        "iteration_", "telemetry", "game_view_", "causal_", "consistency_check",
    ),
    # Engine plumbing, build and performance — the same engine surface, plus
    # the project-level knobs gameplay does not touch.
    "tech": (
        "godot_", "scene_", "playtest_", "evidence_", "iteration_",
        "game_view_", "local_status", "kie_status", "aseprite_status",
    ),
    # The visual pipeline. The biggest list, and it earns it: this is the only
    # seat that generates, rigs, measures and delivers assets.
    "art": (
        "blender_", "mesh_faceting", "skin_dominance", "animation_",
        "godot_character_wire", "godot_clip_",
        "image_", "art_", "palette_pin", "ref_", "bible_ref_",
        "godot_deliver_asset", "godot_import_asset", "godot_screenshot",
        "godot_check_project", "godot_inspect_resource", "godot_status",
        "godot_test_run", "godot_retarget_check", "godot_evidence",
        "scale_", "sprite_", "tileset_", "item_", "prop_generate",
        "character_generate", "cutout_", "aseprite_", "consistency_check",
        "canon_check", "vfx_animate", "sidescroll_generate", "level_reskin",
    ),
    # Music, SFX and mix.
    "audio": (
        "sfx_", "music_", "audio_", "voice_", "godot_check_project",
        "godot_test_run", "godot_screenshot", "scene_wire", "scene_swap_resource",
    ),
    # Cutscenes and shot sequences.
    "cinematic": (
        "cinematic_", "storyboard_", "kie_video_generate", "image_generate",
        "image_edit", "music_", "voice_", "animation_", "godot_screenshot",
    ),
    # Verification: does the game actually play. Drives the engine hard, writes
    # only tests, and files what it finds.
    "qa": (
        "godot_", "evidence_", "playtest_", "scene_outline", "traversal_prove",
        "scale_check", "consistency_check", "canon_check", "room_review",
        "mesh_faceting", "skin_dominance", "animation_curves", "art_qa_verdict",
        "iteration_", "causal_",
    ),
    # Lore, quests, dialogue.
    "narrative": (
        "lore_", "quest_", "dialogue_", "canon_check", "consistency_check",
        "storyboard_write_script", "brainstorm_",
    ),
    # Owns the pillars and arbitrates. Reads widely, writes design; measured at
    # SEVEN distinct tools, so this list is deliberately near-CORE.
    "director": (
        "bible_", "decision_", "not_building_", "greenlight_", "lore_brief",
        "brainstorm_", "godot_screenshot", "godot_evidence", "plan_status",
        "agent_steer", "agent_steer_all", "scale_contract_set", "profile_",
        "seat_configure", "dialogue_list", "quest_list",
    ),
}


def tools_for_seat(seat: str) -> tuple[str, ...] | None:
    """The prefixes a seat may register, or None meaning "everything".

    None rather than an empty tuple for the unknown-seat case, so a caller
    cannot mistake "no restriction" for "no tools" — an empty tuple would
    register nothing and leave an agent mute.
    """
    key = (seat or "").strip().lower()
    if key not in SEATS:
        return None
    return CORE + SEATS[key]


def seat_registers(tool_name: str, seat: str) -> bool:
    """Does this seat carry this tool?

    Prefix match, so a family stays whole as it grows. An unknown seat — no
    BGATE_SEAT at all, which is what a hand-started session looks like — gets
    every tool, because the human driving it is not the thing being budgeted.
    """
    allowed = tools_for_seat(seat)
    if allowed is None:
        return True
    name = (tool_name or "").strip()
    return any(name == p or name.startswith(p) for p in allowed)
