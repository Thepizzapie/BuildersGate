"""Every tool a seat's own brief names is a tool that seat can call.

MEASURED (2026-10-24): the qa brief told qa to run concept_compare,
room_audit, screen_audit, sprite_family_check and ref_list, and none of them
registered for qa; the art mission said to verify meshes with
godot_export_probe, which art did not hold. A brief that names a hidden tool
is worse than either half alone: the agent concludes the step is impossible.

Two gates decide a dispatched seat's registry, `seattools.seat_registers` and
`modules.seat_tool_enabled`, and a tool needs both. This walks the
seat-specific prose (mission, workflow, dispatch rules, the dimension blocks
and traps) against them. The board-wide ownership and production rules are
left out: they name other seats' tools on purpose, to say who owns what.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from bgate_core import seattools
from bgate_core.board import seats
from bgate_core.store import modules

MCP_DIR = Path(__file__).resolve().parents[1] / "src" / "bgate_mcp"
DIMENSIONS = ("2d", "3d", "2d+3d")

#: (seat, tool) pairs the seat's brief names as ANOTHER seat's step, a
#: handoff rather than a call. Each must still appear, or the entry is stale.
HANDOFFS: dict[tuple[str, str], str] = {
    ("art", "level_generate"): "art hands its manifest to the level generator",
    ("art", "sidescroll_generate"): "same handoff, side-scroller view",
    ("level", "tileset_generate"): "level queues tilesets to the art seat",
    ("level", "prop_generate"): "level queues props to the art seat",
    ("tech", "cinematic_keep"): "the .mp4 trap points tech at the cinematic seat",
    ("director", "character_generate"): "the director routes the 3D cast to art",
    ("director", "blender_generate"): "same routing rule",
}


def _tool_names() -> set[str]:
    names = set()
    for path in MCP_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for deco in node.decorator_list:
                    src = ast.unparse(deco)
                    if src in ("_tool", "mcp.tool()") or src.startswith("_tool("):
                        names.add(node.name)
    return names


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _strings(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            yield from _strings(value)


def _seat_text(seat: str) -> str:
    parts = list(_strings(seats.DEFAULT_SEATS.get(seat, {})))
    parts.append(seats.DISPATCH_RULES.get(seat, ""))
    for dim in DIMENSIONS:
        parts.append(seats.workflow_for(seat, dim, ""))
        parts.extend(seats.traps_for(seat, dim))
    if seat == "art":
        parts.extend(_strings(seats.ART_MESH_ROUTE_RULES))
    return "\n".join(p for p in parts if p)


def _named(seat: str, tools: set[str]) -> set[str]:
    words = re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", _seat_text(seat))
    return {w for w in words if w in tools}


def _callable(tool: str, seat: str) -> bool:
    return (seattools.seat_registers(tool, seat)
            and modules.seat_tool_enabled(tool, seat))


TOOLS = _tool_names()


def test_the_scan_sees_the_tool_registry():
    assert len(TOOLS) > 200
    assert {"queue_get", "seat_brief", "concept_compare"} <= TOOLS


@pytest.mark.parametrize("seat", sorted(seattools.SEATS))
def test_every_tool_a_brief_names_is_one_the_seat_holds(seat):
    hidden = sorted(t for t in _named(seat, TOOLS)
                    if (seat, t) not in HANDOFFS and not _callable(t, seat))
    assert not hidden, (
        f"the {seat} brief names {hidden} but the {seat} seat cannot call "
        "them: grant them in seattools.SEATS and modules.CRAFTS, or reword "
        "the brief")


@pytest.mark.parametrize("seat,tool", sorted(HANDOFFS))
def test_every_handoff_entry_is_still_needed(seat, tool):
    assert tool in _named(seat, TOOLS), f"stale HANDOFFS entry: {seat}/{tool}"
    assert not _callable(tool, seat), (
        f"{seat} can call {tool} now, so it is not a handoff any more")


def test_a_trap_for_a_tool_reaches_only_seats_that_hold_it():
    narrative = "\n".join(seats.traps_for("narrative", "2d"))
    assert "godot_screenshot" not in narrative
    assert "godot_screenshot" in "\n".join(seats.traps_for("qa", "2d"))
