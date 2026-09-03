"""Tool nodes — every MCP tool this product owns, reachable from the canvas.

THE PROBLEM THIS FILE EXISTS TO REMOVE
--------------------------------------
The workflow engine in :mod:`bgate_core.board.workflows` is a real DAG runner:
threaded workers, artifact registration, spend reporting, gates that block,
picks that resolve to a value, input-satisfaction checks that name the parent
that has not finished. What it could actually EXECUTE was two things — an image
(``chroma.generate``) and a short piece of text (``generate._write_prompt``).

Meanwhile the palette drew about forty-five cards: music, storyboards,
cinematics, items, levels, vfx, cutouts, Blender, Godot. Behind almost all of
them there was no executor at all. Pressing run on one either queued a Claude
session to go and do it by hand, or did nothing. The owner's report — "nothing
triggers" — was not flakiness. It was absence.

THE UNLOCK
----------
An MCP tool in :mod:`bgate_mcp.server` is a plain Python function. The ``_tool``
decorator wraps it with :func:`functools.wraps`, so ``server.<name>.__wrapped__``
is the undecorated, synchronous original and is directly callable in-process.
That means the engine does not need forty-five hand-written executors. It needs
ONE — "call tool X with these arguments and register whatever comes back" — plus
a TABLE saying which tool a node type maps to and where each argument comes
from. Adding a tool to the canvas is then a table row, not a code path.

WHY THE TABLE IS DATA AND NOT CODE
----------------------------------
:data:`REGISTRY` is also what the dashboard's palette is built from
(``GET /api/workflows/nodes``). A second, hand-written copy of the argument
names in JavaScript would drift from the signatures in ``server.py`` the first
time one of them changed, and the failure would surface as a 422 in the middle
of a paid run. One table, two consumers.

THE IMPORT IS LAZY, ON PURPOSE
------------------------------
``import bgate_mcp.server`` constructs a FastMCP application and pulls in
Blender, Godot, provider and adapter modules. That is far too heavy to happen
because someone opened a workflow, and importing it at module scope would put
that cost on every ``bgate_core`` consumer including the CLI. So the server is
resolved inside :func:`call_tool`, and a tool that does not exist on this build
fails the NODE with a sentence, rather than failing the import of the engine.

EVERY FAILURE IS A SENTENCE
---------------------------
``generate.run``'s docstring states the rule this module inherits: a node that
fails silently is the failure mode the whole engine exists to remove. So every
refusal here names the node, the tool, and the thing the human has to change.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Argument sources — where a tool's parameter gets its value
# ---------------------------------------------------------------------------
#
# A tool node has two kinds of input: what the user typed on the card, and what
# the wire carried in. Naming the source per-argument (rather than "config wins"
# or "wire wins" globally) is what lets one node take its prompt from an
# upstream text node AND its output name from its own card, which is the shape
# almost every real graph has.

SRC_CONFIG = "config"    # the node's own card field
SRC_TEXT = "text"        # the first upstream text output, card value as fallback
SRC_PATH = "path"        # an upstream picked/candidate artifact's absolute path
SRC_PATHS = "paths"      # every upstream artifact path, as a list
SRC_ROOT = "root"        # the Builders Gate project root (for godot_project)
SRC_CONST = "const"      # a fixed value the node cannot change
SRC_DATA = "data"        # the first upstream structured value (a plan, a list)

SOURCES = (SRC_CONFIG, SRC_TEXT, SRC_PATH, SRC_PATHS, SRC_ROOT, SRC_CONST,
           SRC_DATA)

# What a card widget should be for an argument. The palette reads this; nothing
# in the engine depends on it.
WIDGETS = ("text", "area", "number", "toggle", "select", "hidden")


class ToolNodeRefused(ValueError):
    """This tool node cannot run, and the reason is something a human can fix.

    ValueError so the route layer's existing 400/409 mapping applies unchanged,
    exactly like :class:`bgate_core.board.generate.GenerateRefused`.
    """


@dataclass
class Arg:
    """One parameter of one tool, and where its value comes from.

    ``cast`` is not decoration. The canvas stores every widget value as a string
    or a number with no type memory, so ``frames`` arrives as ``"6"`` and a tool
    annotated ``frames: int`` would either coerce it silently or raise a
    TypeError a long way from the card that caused it. Casting here means a bad
    value is refused by name — "frames must be a whole number, got 'six'" —
    before any provider is touched.
    """
    source: str = SRC_CONFIG
    field: str = ""            # config key; defaults to the argument's own name
    default: Any = None
    cast: str = "str"          # str|int|float|bool|list|list_int|dict|raw
    required: bool = False
    label: str = ""
    help: str = ""
    widget: str = "text"
    options: tuple = ()
    # Omit the argument entirely when the resolved value is empty. Tool
    # signatures use "" and None as "you did not ask for this"; passing an empty
    # string where the tool expected an unset optional is how a default gets
    # overwritten with nothing.
    omit_if_empty: bool = True


@dataclass
class ToolNode:
    """One palette card: a node type bound to a tool and an argument map."""
    type: str
    tool: str
    label: str
    category: str
    args: dict[str, Arg] = field(default_factory=dict)
    glyph: str = "⚙"
    accent: str = "var(--ember)"
    summary: str = ""
    # Money. A paid node is reachable from the canvas and is NEVER fired by
    # anything but a human pressing run on it — the palette badges it so nobody
    # discovers the bill afterwards.
    paid: bool = False
    # Does this node write into the game repo or drive an engine process?
    #
    # Two Godot runs, two scene edits, or two Blender jobs at once on the same
    # project is last-write-wins — the same reason agent steps are single-file
    # in workflows.advance. Generation tools write only NEW files under
    # .bgate_out and collide with nothing, so they fan out like generate nodes.
    exclusive: bool = True
    produces: str = "data"     # data|image|audio|video|scene|text
    ports_in: tuple = ("in",)
    ports_out: tuple = ("out",)


def _arg(source=SRC_CONFIG, **kw) -> Arg:
    return Arg(source=source, **kw)


def _game_dir(root) -> str:
    """The Godot project inside this Builders Gate project, or the root itself.

    Falling back to the root rather than refusing is deliberate: a project with
    no engine directory yet should get the ENGINE's own error ("no project.godot
    in <path>"), which names the directory it looked in, rather than a Builders
    Gate error that names nothing.
    """
    try:
        from ..store import project as _project
        found = _project.game_dir(root)
        if found:
            return str(found)
    except Exception:
        pass
    return str(root)


def _project_arg() -> Arg:
    """``godot_project`` — the engine project, defaulting to this project's root.

    Every Godot tool takes it and nobody wants to type an absolute path into a
    node card. Blank on the card means "this project", which is right ~always;
    a sub-directory game (a repo holding several) can still name one.
    """
    return Arg(source=SRC_ROOT, field="godot_project", default="",
               label="Godot project",
               help="blank = this Builders Gate project's root",
               widget="text")


# ---------------------------------------------------------------------------
# THE TABLE
# ---------------------------------------------------------------------------
#
# Ordering is by family, and each family leads with its FREE tools. That is not
# cosmetic: a family whose first card costs money is a family nobody can try.

_NODES: list[ToolNode] = [

    # ---- Engine · Godot ---------------------------------------------------
    ToolNode(
        type="tool.godot.status", tool="godot_status",
        label="Godot status", category="engine", glyph="◈",
        accent="var(--c-tech)", produces="data", exclusive=False,
        summary="Is a Godot binary reachable, and which version. Free, "
                "instant, and the honest first node of any engine graph.",
        args={}),
    ToolNode(
        type="tool.scene.outline", tool="scene_outline",
        label="Scene outline", category="engine", glyph="◈",
        accent="var(--c-tech)", produces="data", exclusive=False,
        summary="Read a scene's node tree - names, types, parents. Free, and "
                "it is how a graph learns what it is allowed to wire to.",
        args={
            "godot_project": _project_arg(),
            "scene": _arg(field="scene", required=True, label="Scene",
                          help="res:// path or a project-relative .tscn"),
            "match": _arg(label="Match", help="substring filter on node names"),
            "role": _arg(label="Role", help="filter by inferred role"),
            "properties": _arg(cast="bool", widget="toggle", default=False,
                               label="Properties"),
            "limit": _arg(cast="int", default=120, widget="number",
                          label="Limit"),
        }),
    ToolNode(
        type="tool.godot.check", tool="godot_check_project",
        label="Check Godot project", category="engine", glyph="◈",
        accent="var(--c-tech)", produces="data",
        summary="Open the project headless and report what the importer said. "
                "Free, and the cheapest proof that an import actually landed.",
        args={
            "godot_project": _project_arg(),
            "timeout": _arg(cast="int", default=180, widget="number",
                            label="Timeout s"),
        }),
    ToolNode(
        type="tool.godot.import", tool="godot_import_asset",
        label="Import asset to Godot", category="engine", glyph="◈",
        accent="var(--c-tech)", produces="scene",
        summary="Copy a produced file into the Godot project and let the "
                "importer see it. Takes the upstream artifact's path.",
        args={
            "godot_project": _project_arg(),
            # THE WIRE IS THE POINT. src_path comes from whatever the upstream
            # node produced; typing a path on the card is the fallback, not the
            # normal case.
            "src_path": Arg(source=SRC_PATH, field="src_path", required=True,
                            label="Source file",
                            help="wired in from the upstream node, or a path"),
            "dest_rel": _arg(default="assets", label="Dest",
                             help="project-relative folder inside the game"),
            "timeout": _arg(cast="int", default=240, widget="number",
                            label="Timeout s"),
        }),
    ToolNode(
        type="tool.godot.deliver", tool="godot_deliver_asset",
        label="Deliver GLB to Godot", category="engine", glyph="◈",
        accent="var(--c-tech)", produces="scene",
        summary="Import a .glb, build a scene around it with collision, and "
                "screenshot it. The whole 3D handover as one node.",
        args={
            "godot_project": _project_arg(),
            "glb": Arg(source=SRC_PATH, field="glb", required=True,
                       label="GLB", help="wired in from the export node"),
            "name": _arg(label="Name"),
            "dest_rel": _arg(default="assets", label="Dest"),
            "scene_rel": _arg(default="scenes", label="Scenes"),
            "physics": _arg(default="auto", label="Physics", widget="select",
                            options=("auto", "static", "rigid", "character",
                                     "none")),
            "shape_type": _arg(default="trimesh", label="Shape",
                               widget="select",
                               options=("trimesh", "convex", "box", "capsule")),
            "with_camera": _arg(cast="bool", default=False, widget="toggle",
                                label="Camera"),
            "overwrite_scene": _arg(cast="bool", default=False,
                                    widget="toggle", label="Overwrite"),
            "timeout": _arg(cast="int", default=300, widget="number",
                            label="Timeout s"),
        }),
    ToolNode(
        type="tool.godot.screenshot", tool="godot_screenshot",
        label="Godot screenshot", category="engine", glyph="◈",
        accent="var(--c-tech)", produces="image",
        summary="Run the game headless for a moment and capture a frame. The "
                "only node that produces evidence a scene edit did anything.",
        args={
            "godot_project": _project_arg(),
            "at": _arg(cast="float", default=1.0, widget="number",
                       label="At (s)", help="seconds of runtime before capture"),
            "scene": _arg(label="Scene", help="blank = the main scene"),
            "label": _arg(label="Label"),
            "timeout": _arg(cast="int", default=120, widget="number",
                            label="Timeout s"),
        }),
    ToolNode(
        type="tool.godot.run", tool="godot_run",
        label="Run a Godot script", category="engine", glyph="◈",
        accent="var(--c-tech)", produces="data",
        summary="Execute a GDScript in the engine headless. The escape hatch "
                "for anything the typed nodes do not cover.",
        args={
            "script": Arg(source=SRC_TEXT, field="script", required=True,
                          label="Script", widget="area",
                          help="GDScript source, or wire text in"),
            "godot_project": _project_arg(),
            "timeout": _arg(cast="int", default=120, widget="number",
                            label="Timeout s"),
        }),

    # ---- Engine · scene surgery -------------------------------------------
    ToolNode(
        type="tool.scene.node_add", tool="scene_node_add",
        label="Add a scene node", category="engine", glyph="⊞",
        accent="var(--c-tech)", produces="scene",
        summary="Insert a node into a .tscn. dry_run on means it reports the "
                "edit it would make and touches nothing.",
        args={
            "godot_project": _project_arg(),
            "scene": _arg(required=True, label="Scene"),
            "name": _arg(required=True, label="Node name"),
            "node_type": _arg(required=True, label="Node type",
                              help="e.g. Sprite2D, StaticBody3D"),
            "parent": _arg(default=".", label="Parent"),
            "props": _arg(cast="dict", default=None, label="Props",
                          widget="area", help="JSON object of properties"),
            # DRY RUN DEFAULTS ON. This node writes into the user's game. The
            # first press of run should show the edit, not make it.
            "dry_run": _arg(cast="bool", default=True, widget="toggle",
                            label="Dry run"),
            "force": _arg(cast="bool", default=False, widget="toggle",
                          label="Force"),
        }),
    ToolNode(
        type="tool.scene.wire", tool="scene_wire",
        label="Wire an asset into a scene", category="engine", glyph="⊞",
        accent="var(--c-tech)", produces="scene",
        summary="Put a produced asset into a scene as the right node type. "
                "Takes the asset from the wire.",
        args={
            "godot_project": _project_arg(),
            "scene": _arg(required=True, label="Scene"),
            "asset": Arg(source=SRC_PATH, field="asset", required=True,
                         label="Asset", help="wired in, or a res:// path"),
            "parent": _arg(default=".", label="Parent"),
            "node_name": _arg(label="Node name"),
            "node_type": _arg(label="Node type"),
            "dry_run": _arg(cast="bool", default=True, widget="toggle",
                            label="Dry run"),
            "force": _arg(cast="bool", default=False, widget="toggle",
                          label="Force"),
        }),
    ToolNode(
        type="tool.scene.set_property", tool="scene_set_property",
        label="Set a node property", category="engine", glyph="⊞",
        accent="var(--c-tech)", produces="scene",
        summary="Change one property on one node of a scene.",
        args={
            "godot_project": _project_arg(),
            "scene": _arg(required=True, label="Scene"),
            "node": _arg(required=True, label="Node"),
            "key": _arg(required=True, label="Property"),
            "value": _arg(cast="raw", label="Value",
                          help="JSON if it is not a plain string"),
            "clear": _arg(cast="bool", default=False, widget="toggle",
                          label="Clear"),
            "dry_run": _arg(cast="bool", default=True, widget="toggle",
                            label="Dry run"),
            "force": _arg(cast="bool", default=False, widget="toggle",
                          label="Force"),
        }),
    ToolNode(
        type="tool.scene.attach_script", tool="scene_attach_script",
        label="Attach a script", category="engine", glyph="⊞",
        accent="var(--c-tech)", produces="scene",
        summary="Attach a .gd script to a node in a scene.",
        args={
            "godot_project": _project_arg(),
            "scene": _arg(required=True, label="Scene"),
            "script": _arg(required=True, label="Script",
                           help="res:// path to the .gd"),
            "node": _arg(default=".", label="Node"),
            "dry_run": _arg(cast="bool", default=True, widget="toggle",
                            label="Dry run"),
            "force": _arg(cast="bool", default=False, widget="toggle",
                          label="Force"),
        }),

    # ---- Levels -----------------------------------------------------------
    ToolNode(
        type="tool.level.plan", tool="level_plan",
        label="Plan a level", category="level", glyph="▦",
        accent="var(--c-design)", produces="data", exclusive=False,
        summary="BSP rooms and corridors as data. Free and local - run it "
                "until the layout is right, then generate once.",
        args={
            "width": _arg(cast="int", default=48, widget="number",
                          label="Width"),
            "height": _arg(cast="int", default=32, widget="number",
                           label="Height"),
            "seed": _arg(cast="int", default=0, widget="number", label="Seed"),
            "min_leaf": _arg(cast="int", default=10, widget="number",
                             label="Min leaf"),
            "min_room": _arg(cast="int", default=4, widget="number",
                             label="Min room"),
            "max_depth": _arg(cast="int", default=5, widget="number",
                              label="Depth"),
            "corridor_width": _arg(cast="int", default=1, widget="number",
                                   label="Corridor"),
        }),
    ToolNode(
        type="tool.tileset.describe", tool="tileset_describe",
        label="Describe a hand-built tileset", category="level", glyph="▤",
        accent="var(--c-design)", produces="manifest",
        summary="Write the sidecar that says where this sheet's tiles are - "
                "once per sheet, then every level reads it.",
        args={
            "godot_project": _project_arg(),
            "tileset": _arg(required=True, label="TileSet",
                            help="res:// path to the .tres"),
            "floor_layout": _arg(default="solid", label="Floor layout"),
            "floor_source": _arg(cast="int", default=0, widget="number",
                                 label="Floor source"),
            "floor_atlas_x": _arg(cast="int", default=0, widget="number",
                                  label="Floor atlas x"),
            "floor_atlas_y": _arg(cast="int", default=0, widget="number",
                                  label="Floor atlas y"),
            "wall_layout": _arg(default="blob47", label="Wall layout"),
            "wall_source": _arg(cast="int", default=0, widget="number",
                                label="Wall source"),
            "wall_atlas_x": _arg(cast="int", default=0, widget="number",
                                 label="Wall atlas x"),
            "wall_atlas_y": _arg(cast="int", default=0, widget="number",
                                 label="Wall atlas y"),
            "wall_columns": _arg(cast="int", default=8, widget="number",
                                 label="Wall columns"),
            "overwrite": _arg(cast="bool", default=False, widget="toggle",
                              label="Overwrite"),
        }),
    ToolNode(
        type="tool.level.generate", tool="level_generate",
        label="Generate a level scene", category="level", glyph="▦",
        accent="var(--c-design)", produces="scene",
        summary="Write the planned level into a Godot TileMap scene.",
        args={
            "godot_project": _project_arg(),
            "scene": _arg(required=True, label="Scene"),
            "tileset": _arg(required=True, label="TileSet",
                            help="res:// path to the .tres"),
            "width": _arg(cast="int", default=48, widget="number",
                          label="Width"),
            "height": _arg(cast="int", default=32, widget="number",
                           label="Height"),
            "seed": _arg(cast="int", default=0, widget="number", label="Seed"),
            # The wall LAYOUT is a fact about the sheet and lives in its
            # sidecar now; what is left here is whether this level wants walls
            # at all, which is a fact about the level.
            "walls": _arg(cast="bool", default=True, widget="toggle",
                          label="Walls"),
            "create": _arg(cast="bool", default=False, widget="toggle",
                           label="Create scene"),
            "dry_run": _arg(cast="bool", default=True, widget="toggle",
                            label="Dry run"),
        }),

    # ---- Music ------------------------------------------------------------
    ToolNode(
        type="tool.music.options", tool="music_options",
        label="Music options", category="audio", glyph="♪",
        accent="var(--c-audio)", produces="data", exclusive=False,
        summary="Which music models and styles this build can reach. Free, "
                "and the node that tells you whether the key is live.",
        args={}),
    ToolNode(
        type="tool.music.generate", tool="music_generate",
        label="Generate music", category="audio", glyph="♪",
        accent="var(--c-audio)", produces="audio", paid=True, exclusive=False,
        summary="A track from a prompt. PAID - it calls a provider and the "
                "bill is real. Wire a prompt in or type one.",
        args={
            "prompt": Arg(source=SRC_TEXT, field="prompt", required=True,
                          label="Prompt", widget="area"),
            "name": _arg(label="Name", help="logical name for the artifact"),
            "instrumental": _arg(cast="bool", default=True, widget="toggle",
                                 label="Instrumental"),
            "model": _arg(label="Model"),
            "style": _arg(label="Style"),
            "title": _arg(label="Title"),
            "negative_tags": _arg(label="Avoid"),
            "duration": _arg(cast="int", default=None, widget="number",
                             label="Seconds"),
            "timeout": _arg(cast="float", default=900.0, widget="number",
                            label="Timeout s"),
        }),
    ToolNode(
        type="tool.music.candidates", tool="music_candidates",
        label="Music candidates", category="audio", glyph="♪",
        accent="var(--c-audio)", produces="audio", exclusive=False,
        summary="Every track generated so far, so a pick node has something "
                "to choose between. Free.",
        args={
            "logical_name": _arg(label="Name", help="blank = all of them"),
            "limit": _arg(cast="int", default=100, widget="number",
                          label="Limit"),
        }),
    ToolNode(
        type="tool.music.keep", tool="music_keep",
        label="Keep a track", category="audio", glyph="♪",
        accent="var(--c-audio)", produces="audio",
        summary="Mark one candidate as the keeper.",
        args={
            "artifact_id": _arg(cast="int", required=True, widget="number",
                                label="Artifact #"),
            "note": _arg(label="Note"),
        }),
    ToolNode(
        type="tool.music.install", tool="music_install",
        label="Install a track", category="audio", glyph="♪",
        accent="var(--c-audio)", produces="audio",
        summary="Copy the kept track into the game where the engine loads it.",
        args={
            "artifact_id": _arg(cast="int", required=True, widget="number",
                                label="Artifact #"),
        }),
    ToolNode(
        type="tool.music.discard", tool="music_discard",
        label="Discard a track", category="audio", glyph="♪",
        accent="var(--c-audio)", produces="data",
        summary="Reject a candidate and say why.",
        args={
            "artifact_id": _arg(cast="int", required=True, widget="number",
                                label="Artifact #"),
            "note": _arg(label="Note"),
        }),

    # ---- Voice ------------------------------------------------------------
    ToolNode(
        type="tool.voice.status", tool="voice_status",
        label="Voice status", category="audio", glyph="◍",
        accent="var(--c-audio)", produces="data", exclusive=False,
        summary="Whether text-to-speech is reachable on this machine. Free.",
        args={}),
    ToolNode(
        type="tool.voice.speak", tool="voice_speak",
        label="Speak a line", category="audio", glyph="◍",
        accent="var(--c-audio)", produces="audio", paid=True, exclusive=False,
        summary="Turn a line of text into audio. Takes its text from the wire.",
        args={
            "text": Arg(source=SRC_TEXT, field="text", required=True,
                        label="Text", widget="area"),
            "out_path": _arg(label="Out path"),
            "model": _arg(label="Model"),
        }),

    # ---- Storyboard / video ------------------------------------------------
    ToolNode(
        type="tool.cinematic.styles", tool="cinematic_styles",
        label="Cinematic styles", category="video", glyph="▷",
        accent="var(--c-narrative)", produces="data", exclusive=False,
        summary="The named looks a cinematic can be planned in. Free.",
        args={}),
    ToolNode(
        type="tool.storyboard.plan", tool="storyboard_plan",
        label="Plan a storyboard", category="video", glyph="▷",
        accent="var(--c-narrative)", produces="data", exclusive=False,
        summary="Lay out the frames of a storyboard without drawing any of "
                "them. Free - this is the cheap half of the loop.",
        args={
            "name": _arg(required=True, label="Name"),
            "premise": Arg(source=SRC_TEXT, field="premise", label="Premise",
                           widget="area"),
            "logline": _arg(label="Logline"),
            "style": _arg(label="Style"),
            "style_note": _arg(label="Style note", widget="area"),
            "aspect_ratio": _arg(default="16:9", label="Aspect"),
            "frames": _arg(cast="raw", default=None, label="Frames",
                           widget="area",
                           help="JSON list of frame dicts; blank = auto"),
        }),
    ToolNode(
        type="tool.storyboard.auto", tool="storyboard_auto",
        label="Auto storyboard", category="video", glyph="▷",
        accent="var(--c-narrative)", produces="image", paid=True,
        exclusive=False,
        summary="Plan AND draw every frame in one call. PAID - one image per "
                "frame.",
        args={
            "name": _arg(required=True, label="Name"),
            "premise": Arg(source=SRC_TEXT, field="premise", label="Premise",
                           widget="area"),
            "frames": _arg(cast="int", default=6, widget="number",
                           label="Frames"),
            "style": _arg(label="Style"),
            "aspect_ratio": _arg(default="16:9", label="Aspect"),
            "quality": _arg(default="low", label="Quality", widget="select",
                            options=("low", "medium", "high")),
            "model": _arg(label="Model"),
        }),
    ToolNode(
        type="tool.storyboard.frame", tool="storyboard_frame_generate",
        label="Draw one frame", category="video", glyph="▷",
        accent="var(--c-narrative)", produces="image", paid=True,
        exclusive=False,
        summary="Redraw a single storyboard frame. PAID, but one image at a "
                "time - the node you use to fix the one frame that is wrong.",
        args={
            "name": _arg(required=True, label="Board"),
            "idx": _arg(cast="int", required=True, widget="number",
                        label="Frame #"),
            "prompt": Arg(source=SRC_TEXT, field="prompt", label="Prompt",
                          widget="area"),
            "provider": _arg(label="Provider"),
            "model": _arg(label="Model"),
            "use_cast": _arg(cast="bool", default=True, widget="toggle",
                             label="Use cast"),
            "ref_strength": _arg(cast="float", default=0.5, widget="number",
                                 label="Ref strength"),
            "quality": _arg(default="medium", label="Quality", widget="select",
                            options=("low", "medium", "high")),
        }),
    ToolNode(
        type="tool.cinematic.plan", tool="cinematic_plan",
        label="Plan a cinematic", category="video", glyph="▷",
        accent="var(--c-narrative)", produces="data", exclusive=False,
        summary="Define the shot list. Free - nothing renders here.",
        args={
            "name": _arg(required=True, label="Name"),
            "shots": _arg(cast="raw", required=True, label="Shots",
                          widget="area", help="JSON list of shot dicts"),
            "logline": _arg(label="Logline"),
            "style": _arg(label="Style"),
            "model": _arg(label="Model"),
            "aspect_ratio": _arg(default="16:9", label="Aspect"),
            "resolution": _arg(default="720p", label="Resolution"),
            "audio_track": _arg(label="Audio"),
        }),
    ToolNode(
        type="tool.cinematic.shot", tool="cinematic_generate_shot",
        label="Generate a shot", category="video", glyph="▷",
        accent="var(--c-narrative)", produces="video", paid=True,
        exclusive=False,
        summary="Render one shot of a planned cinematic. PAID, and video is "
                "the most expensive thing this product can buy.",
        args={
            "name": _arg(required=True, label="Cinematic"),
            "idx": _arg(cast="int", required=True, widget="number",
                        label="Shot #"),
            "model": _arg(label="Model"),
            "generate_audio": _arg(cast="bool", default=False, widget="toggle",
                                   label="Audio"),
            "overwrite": _arg(cast="bool", default=False, widget="toggle",
                              label="Overwrite"),
            "timeout": _arg(cast="float", default=1800.0, widget="number",
                            label="Timeout s"),
        }),
    ToolNode(
        type="tool.cinematic.assemble", tool="cinematic_assemble",
        label="Assemble the cinematic", category="video", glyph="▷",
        accent="var(--c-narrative)", produces="video",
        summary="Cut the rendered shots together with ffmpeg. Local, free.",
        args={
            "name": _arg(required=True, label="Cinematic"),
            "quality": _arg(cast="int", default=6, widget="number",
                            label="Quality"),
        }),
    ToolNode(
        type="tool.cinematic.deliver", tool="cinematic_deliver",
        label="Deliver the cinematic", category="video", glyph="▷",
        accent="var(--c-narrative)", produces="video",
        summary="Put the finished cut where the game can play it.",
        args={
            "name": _arg(required=True, label="Cinematic"),
            "force": _arg(cast="bool", default=False, widget="toggle",
                          label="Force"),
        }),

    # ---- Items ------------------------------------------------------------
    ToolNode(
        type="tool.item.classes", tool="item_classes",
        label="Item classes", category="asset", glyph="⬗",
        accent="var(--c-art)", produces="data", exclusive=False,
        summary="The item archetypes this project knows. Free.",
        args={}),
    ToolNode(
        type="tool.item.generate", tool="item_generate",
        label="Generate an item", category="asset", glyph="⬗",
        accent="var(--c-art)", produces="image", paid=True, exclusive=False,
        summary="One item sprite, on-style. PAID.",
        args={
            "item_class": _arg(required=True, label="Class"),
            "name": _arg(required=True, label="Name"),
            "descriptor": Arg(source=SRC_TEXT, field="descriptor",
                              required=True, label="Descriptor", widget="area"),
            "material": _arg(label="Material"),
            "element": _arg(label="Element"),
            "tier": _arg(label="Tier"),
            "quality": _arg(default="medium", label="Quality", widget="select",
                            options=("low", "medium", "high")),
            "character": _arg(label="Character",
                              help="style-match this character's palette"),
            "force": _arg(cast="bool", default=False, widget="toggle",
                          label="Force"),
        }),
    ToolNode(
        type="tool.item.variants", tool="item_variants",
        label="Item variants", category="asset", glyph="⬗",
        accent="var(--c-art)", produces="image", paid=True, exclusive=False,
        summary="A grid of the same item across materials and tiers. PAID, "
                "and the bill multiplies - the limit is the ceiling.",
        args={
            "item_class": _arg(required=True, label="Class"),
            "base_name": _arg(required=True, label="Base name"),
            "descriptor": Arg(source=SRC_TEXT, field="descriptor",
                              required=True, label="Descriptor", widget="area"),
            "materials": _arg(cast="list", default=None, label="Materials",
                              help="comma separated"),
            "elements": _arg(cast="list", default=None, label="Elements",
                             help="comma separated"),
            "tiers": _arg(cast="list", default=None, label="Tiers",
                          help="comma separated"),
            "quality": _arg(default="medium", label="Quality", widget="select",
                            options=("low", "medium", "high")),
            "limit": _arg(cast="int", default=12, widget="number",
                          label="Limit"),
        }),
    ToolNode(
        type="tool.item.spriteframes", tool="item_to_spriteframes",
        label="Item to SpriteFrames", category="asset", glyph="⬗",
        accent="var(--c-tech)", produces="scene",
        summary="Turn an item sheet into a Godot SpriteFrames resource. Local.",
        args={
            "sprite": Arg(source=SRC_PATH, field="sprite", required=True,
                          label="Sprite"),
            "name": _arg(required=True, label="Name"),
            "res_dir": _arg(default="assets/gear", label="Res dir"),
            "frame_size": _arg(cast="list_int", default=None, label="Frame",
                               help="w,h"),
        }),

    # ---- VFX --------------------------------------------------------------
    ToolNode(
        type="tool.vfx.animate", tool="vfx_animate",
        label="Animate a VFX", category="asset", glyph="✦",
        accent="var(--c-art)", produces="image", exclusive=False,
        summary="Grow a key frame into a motion cycle locally - no provider "
                "call, so it is free and fast.",
        args={
            "key_frame": Arg(source=SRC_PATH, field="key_frame", required=True,
                             label="Key frame"),
            "name": _arg(required=True, label="Name"),
            "motion": _arg(default="burst", label="Motion"),
            "frames": _arg(cast="int", default=4, widget="number",
                           label="Frames"),
            "peak": _arg(cast="int", default=1, widget="number", label="Peak"),
            "fps": _arg(cast="float", default=14.0, widget="number",
                        label="FPS"),
            "res_dir": _arg(default="assets/vfx", label="Res dir"),
        }),
    ToolNode(
        type="tool.anim.curves", tool="animation_curves",
        label="Animation curve check", category="3d", glyph="✦",
        accent="var(--c-tech)", produces="data",
        summary="Measure foot skate, smoothness and anticipation on a rigged "
                "model. Local and free - a real gate for animation quality.",
        args={
            "model": Arg(source=SRC_PATH, field="model", required=True,
                         label="Model"),
            "foot_bones": _arg(cast="list", default=None, label="Foot bones",
                               help="comma separated"),
            "ground_axis": _arg(cast="int", default=1, widget="number",
                                label="Ground axis"),
            "max_skating_frames": _arg(cast="int", default=0, widget="number",
                                       label="Max skating"),
            "check_anticipation": _arg(cast="bool", default=True,
                                       widget="toggle", label="Anticipation"),
        }),

    # ---- Cutout -----------------------------------------------------------
    ToolNode(
        type="tool.cutout.templates", tool="cutout_templates",
        label="Cutout templates", category="asset", glyph="⌘",
        accent="var(--c-art)", produces="data", exclusive=False,
        summary="The bone templates a cutout character can be built on. Free.",
        args={}),
    ToolNode(
        type="tool.cutout.status", tool="cutout_status",
        label="Cutout status", category="asset", glyph="⌘",
        accent="var(--c-art)", produces="data", exclusive=False,
        summary="What a cutout character currently has assembled. Free.",
        args={"name": _arg(required=True, label="Name")}),
    ToolNode(
        type="tool.cutout.assemble", tool="cutout_assemble",
        label="Assemble a cutout", category="asset", glyph="⌘",
        accent="var(--c-art)", produces="scene",
        summary="Build a bone-based cutout character out of part images.",
        args={
            "name": _arg(required=True, label="Name"),
            "parts": _arg(cast="dict", required=True, label="Parts",
                          widget="area", help="JSON: slot -> image path"),
            "template": _arg(default="biped_v1", label="Template"),
            "adjustments": _arg(cast="dict", default=None, label="Adjustments",
                                widget="area"),
            "notes": _arg(label="Notes"),
            "force": _arg(cast="bool", default=False, widget="toggle",
                          label="Force"),
        }),
    ToolNode(
        type="tool.cutout.equip", tool="cutout_equip",
        label="Equip a cutout slot", category="asset", glyph="⌘",
        accent="var(--c-art)", produces="scene",
        summary="Swap one texture into one slot of an assembled cutout.",
        args={
            "name": _arg(required=True, label="Name"),
            "slot": _arg(required=True, label="Slot"),
            "texture": Arg(source=SRC_PATH, field="texture", required=True,
                           label="Texture"),
            "force": _arg(cast="bool", default=False, widget="toggle",
                          label="Force"),
        }),

    # ---- 3D · Blender ------------------------------------------------------
    ToolNode(
        type="tool.blender.status", tool="blender_status",
        label="Blender status", category="3d", glyph="◳",
        accent="var(--c-tech)", produces="data", exclusive=False,
        summary="Is Blender reachable and which backends are installed. Free.",
        args={}),
    ToolNode(
        type="tool.blender.generate", tool="blender_generate",
        label="Image to 3D", category="3d", glyph="◳",
        accent="var(--c-tech)", produces="data",
        summary="Turn a picture into a mesh with the local image-to-3D "
                "backend. Runs on this machine, so it costs time not money.",
        args={
            "image": Arg(source=SRC_PATH, field="image", required=True,
                         label="Image"),
            "out_path": _arg(required=True, label="Out path"),
            "backend": _arg(label="Backend"),
            "label": _arg(label="Label"),
            "parts": _arg(cast="bool", default=False, widget="toggle",
                          label="Part aware"),
            "dry_run": _arg(cast="bool", default=False, widget="toggle",
                            label="Dry run"),
            "timeout": _arg(cast="int", default=900, widget="number",
                            label="Timeout s"),
        }),
    ToolNode(
        type="tool.blender.rig", tool="blender_rig",
        label="Rig a model", category="3d", glyph="◳",
        accent="var(--c-tech)", produces="data",
        summary="Fit an armature to a mesh and skin it. Local.",
        args={
            "model": Arg(source=SRC_PATH, field="model", required=True,
                         label="Model"),
            "out_path": _arg(required=True, label="Out path"),
            "kind": _arg(default="humanoid", label="Kind"),
            "height": _arg(cast="float", default=1.8, widget="number",
                           label="Height m"),
            "timeout": _arg(cast="int", default=900, widget="number",
                            label="Timeout s"),
        }),
    ToolNode(
        type="tool.blender.gltf", tool="blender_export_gltf",
        label="Export glTF", category="3d", glyph="◳",
        accent="var(--c-tech)", produces="data",
        summary="Export a .blend to glb/gltf for the engine. Local.",
        args={
            "out_path": _arg(required=True, label="Out path"),
            "blend_file": Arg(source=SRC_PATH, field="blend_file",
                              label="Blend file"),
            "script": _arg(default="pass", label="Script", widget="area"),
            "timeout": _arg(cast="int", default=240, widget="number",
                            label="Timeout s"),
        }),
    ToolNode(
        type="tool.blender.sprites", tool="blender_sprites",
        label="3D to sprite sheet", category="3d", glyph="◳",
        accent="var(--c-art)", produces="image",
        summary="Render a 3D model down to 2D sprite frames. Local.",
        args={
            "base_script": _arg(required=True, label="Base script",
                                widget="area"),
            "poses": _arg(cast="raw", required=True, label="Poses",
                          widget="area", help="JSON list of pose dicts"),
            "name": _arg(default="sprite", label="Name"),
            "width": _arg(cast="int", default=128, widget="number",
                          label="Width"),
            "height": _arg(cast="int", default=128, widget="number",
                           label="Height"),
            "fps": _arg(cast="float", default=8.0, widget="number",
                        label="FPS"),
            "res_dir": _arg(default="assets/sprites", label="Res dir"),
            "timeout": _arg(cast="int", default=420, widget="number",
                            label="Timeout s"),
        }),

    # ---- 2D generation, as a tool node -------------------------------------
    ToolNode(
        type="tool.image.status", tool="image_status",
        label="Image provider status", category="asset", glyph="◉",
        accent="var(--c-art)", produces="data", exclusive=False,
        summary="Which image providers have a live key. Free.",
        args={}),
    ToolNode(
        type="tool.local.status", tool="local_status",
        label="Local generation status", category="asset", glyph="◉",
        accent="var(--c-art)", produces="data", exclusive=False,
        summary="Whether local (ComfyUI) generation is reachable. Free.",
        args={}),
    ToolNode(
        type="tool.image.generate", tool="image_generate",
        label="Image (full options)", category="asset", glyph="◉",
        accent="var(--c-art)", produces="image", paid=True, exclusive=False,
        summary="The raw image tool with every option the model card hides - "
                "tileable, anchors, pinned refs. PAID.",
        args={
            "prompt": Arg(source=SRC_TEXT, field="prompt", required=True,
                          label="Prompt", widget="area"),
            "filename": _arg(required=True, label="Filename"),
            "size": _arg(default="1024x1024", label="Size"),
            "quality": _arg(default="medium", label="Quality", widget="select",
                            options=("low", "medium", "high")),
            "transparent": _arg(cast="bool", default=False, widget="toggle",
                                label="Transparent"),
            "use_pinned": _arg(label="Pinned refs"),
            "task_kind": _arg(label="Task kind"),
            "tileable": _arg(cast="bool", default=False, widget="toggle",
                             label="Tileable"),
            "ref_strength": _arg(cast="float", default=0.5, widget="number",
                                 label="Ref strength"),
            "provider": _arg(label="Provider"),
            "model": _arg(label="Model"),
        }),
]

REGISTRY: dict[str, ToolNode] = {n.type: n for n in _NODES}


def is_tool_node(node_type: str) -> bool:
    return str(node_type or "") in REGISTRY


def spec_for(node_type: str) -> Optional[ToolNode]:
    return REGISTRY.get(str(node_type or ""))


def catalogue() -> list[dict]:
    """The registry as JSON, for the palette to build itself from.

    The dashboard used to hold its own copy of every argument name. One typo
    there is a 422 in the middle of a paid run, discovered by the user. This is
    the same table the executor calls with, so the card and the call cannot
    disagree.
    """
    out = []
    for node in _NODES:
        out.append({
            "type": node.type, "tool": node.tool, "label": node.label,
            "category": node.category, "glyph": node.glyph,
            "accent": node.accent, "summary": node.summary,
            "paid": node.paid, "exclusive": node.exclusive,
            "produces": node.produces,
            "ports_in": list(node.ports_in), "ports_out": list(node.ports_out),
            "args": [{
                "name": name, "source": arg.source,
                "field": arg.field or name,
                "default": arg.default, "cast": arg.cast,
                "required": arg.required,
                "label": arg.label or (arg.field or name),
                "help": arg.help, "widget": arg.widget,
                "options": list(arg.options),
            } for name, arg in node.args.items()],
        })
    return out


# ---------------------------------------------------------------------------
# Casting a card value into the type the tool's signature wants
# ---------------------------------------------------------------------------

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}


def _cast(value: Any, how: str, *, arg_name: str, node_label: str) -> Any:
    """Coerce, or refuse with the field's name in the sentence.

    A silent coercion is worse than a refusal here: ``frames="six"`` becoming 0
    produces an empty sheet and a bill, and nothing in the run says which card
    was wrong.
    """
    if how == "raw":
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return json.loads(text)
            except (TypeError, ValueError):
                return value      # a plain string is a legitimate raw value
        return value
    if value is None:
        return None
    if how == "str":
        return str(value)
    if how == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ToolNodeRefused(
            f"{node_label}: '{arg_name}' must be a yes/no value, got {value!r}")
    if how in ("int", "float"):
        text = str(value).strip()
        if text == "":
            return None
        try:
            return int(float(text)) if how == "int" else float(text)
        except (TypeError, ValueError):
            raise ToolNodeRefused(
                f"{node_label}: '{arg_name}' must be a "
                f"{'whole ' if how == 'int' else ''}number, got {value!r}"
            ) from None
    if how in ("list", "list_int"):
        if isinstance(value, (list, tuple)):
            items = list(value)
        else:
            text = str(value).strip()
            if not text:
                return None
            if text.startswith("["):
                try:
                    items = list(json.loads(text))
                except (TypeError, ValueError):
                    raise ToolNodeRefused(
                        f"{node_label}: '{arg_name}' looks like JSON but does "
                        f"not parse: {value!r}") from None
            else:
                items = [p.strip() for p in text.split(",") if p.strip()]
        if how == "list_int":
            try:
                return [int(float(str(i))) for i in items]
            except (TypeError, ValueError):
                raise ToolNodeRefused(
                    f"{node_label}: '{arg_name}' must be numbers, got "
                    f"{value!r}") from None
        return items
    if how == "dict":
        if isinstance(value, dict):
            return value
        text = str(value or "").strip()
        if not text:
            return None
        try:
            loaded = json.loads(text)
        except (TypeError, ValueError):
            raise ToolNodeRefused(
                f"{node_label}: '{arg_name}' must be a JSON object, and this "
                f"does not parse: {value!r}") from None
        if not isinstance(loaded, dict):
            raise ToolNodeRefused(
                f"{node_label}: '{arg_name}' must be a JSON object, got a "
                f"{type(loaded).__name__}")
        return loaded
    return value


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (
        isinstance(value, (list, tuple, dict)) and not value)


# ---------------------------------------------------------------------------
# Templating — how a generated value reaches the next tool's argument
# ---------------------------------------------------------------------------

def interpolate(text: str, inputs: dict) -> str:
    """Substitute the wire's values into a card's text.

    Without this the only way an upstream result could become a downstream
    ARGUMENT was to be the whole argument. A generated character name could not
    be dropped into "res://characters/{input}.tscn", so a graph that produced a
    name and a graph that used one could not be the same graph.

    Placeholders, all optional: ``{input}`` / ``{text}`` (the upstream text),
    ``{path}`` (the first upstream artifact's absolute path), ``{name}`` (its
    logical name), ``{artifact_id}``.
    """
    if not isinstance(text, str) or "{" not in text:
        return text
    first = _first_artifact(inputs)
    values = {
        "input": str(inputs.get("text") or ""),
        "text": str(inputs.get("text") or ""),
        "path": str((first or {}).get("abspath") or (first or {}).get("path") or ""),
        "name": str((first or {}).get("logical_name") or ""),
        "artifact_id": str((first or {}).get("artifact_id") or ""),
    }
    out = text
    for key, value in values.items():
        token = "{" + key + "}"
        if token in out:
            out = out.replace(token, value)
    return out


def _first_artifact(inputs: dict) -> Optional[dict]:
    """The one artifact a downstream tool should act on.

    A human's pick outranks a raw candidate, always. If a pick node chose
    candidate #7 and this node then acted on candidate #1 because it happened to
    be produced first, the pick would have been decoration.
    """
    for chosen in inputs.get("picked") or ():
        if isinstance(chosen, dict):
            return chosen
    for cand in inputs.get("candidates") or ():
        if isinstance(cand, dict):
            return cand
    for ref in inputs.get("refs") or ():
        if isinstance(ref, dict) and ref.get("path"):
            return {"path": ref["path"], "abspath": ref["path"]}
    # A raw file a tool made but never registered — a screenshot, a .glb, an
    # ffmpeg cut. It is not pickable (no artifact row) but it is absolutely
    # wireable, and refusing to see it is what would break every engine-side
    # chain: export -> import, render -> screenshot, assemble -> deliver.
    for path in inputs.get("paths") or ():
        if isinstance(path, str) and path.strip():
            return {"path": path, "abspath": path}
    return None


def _abspath(root, entry: dict) -> str:
    """Artifacts are registered PROJECT-RELATIVE; tools take real paths."""
    path = str((entry or {}).get("abspath") or (entry or {}).get("path") or "")
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return str(Path(str(root)) / path)


# ---------------------------------------------------------------------------
# Building the call
# ---------------------------------------------------------------------------

def build_kwargs(root, spec: ToolNode, config: dict, inputs: dict) -> dict:
    """Map this node's card + its wires onto the tool's parameters.

    Refuses BEFORE the tool is imported when a required argument is missing, so
    the message names the field on the card rather than arriving as a pydantic
    validation error about a Python signature the user has never seen.
    """
    config = config if isinstance(config, dict) else {}
    inputs = inputs or {}
    label = spec.label
    kwargs: dict[str, Any] = {}
    first = _first_artifact(inputs)

    for name, arg in spec.args.items():
        key = arg.field or name
        raw: Any = None

        if arg.source == SRC_CONST:
            raw = arg.default
        elif arg.source == SRC_ROOT:
            # The card wins when it names one; blank means this project's ENGINE
            # project, which is not the same directory as the Builders Gate root
            # and is the first thing every one of these nodes got wrong.
            # `bgate init` writes project.godot at the root, godot_scaffold
            # writes it under game/ — project.game_dir already knows which, and
            # guessing either one makes every Godot node fail on half the
            # projects in existence with "that is not a Godot project".
            raw = config.get(key)
            if _blank(raw):
                raw = _game_dir(root)
        elif arg.source == SRC_TEXT:
            # THE WIRE WINS, and the card composes with it. This is the same
            # rule generate._prompt_for follows, restated here so a tool node
            # and a model node behave identically on the same wire: a card
            # containing {input} composes, a card with plain text is the
            # fallback when nothing is wired in.
            card = config.get(key)
            wired = str(inputs.get("text") or "").strip()
            if isinstance(card, str) and "{" in card:
                raw = interpolate(card, inputs)
            elif wired:
                raw = wired
            else:
                raw = card
        elif arg.source == SRC_PATH:
            card = config.get(key)
            if not _blank(card):
                raw = interpolate(str(card), inputs)
            elif first:
                raw = _abspath(root, first)
            else:
                raw = None
        elif arg.source == SRC_PATHS:
            paths = [_abspath(root, c) for c in (inputs.get("candidates") or ())]
            raw = [p for p in paths if p] or None
        elif arg.source == SRC_DATA:
            card = config.get(key)
            raw = card if not _blank(card) else (inputs.get("data") or [None])[0]
        else:  # SRC_CONFIG
            raw = config.get(key)
            if isinstance(raw, str):
                raw = interpolate(raw, inputs)

        if _blank(raw) and arg.default is not None:
            raw = arg.default

        value = _cast(raw, arg.cast, arg_name=arg.label or key,
                      node_label=label)

        if arg.required and _blank(value) and value is not False and value != 0:
            raise ToolNodeRefused(
                f"{label}: '{arg.label or key}' is required and this node has "
                f"nothing for it — "
                + ("wire a value into it or type one on the card"
                   if arg.source in (SRC_TEXT, SRC_PATH, SRC_PATHS)
                   else "fill it in on the card"))
        if _blank(value) and arg.omit_if_empty and not arg.required:
            continue  # let the tool's own default stand
        kwargs[name] = value
    return kwargs


# ---------------------------------------------------------------------------
# Resolving and calling the tool
# ---------------------------------------------------------------------------

def resolve(tool_name: str) -> tuple[Any, Callable]:
    """(server module, the undecorated callable). Imported LAZILY.

    ``bgate_mcp.server`` builds a FastMCP app and imports Blender, Godot and
    every provider adapter. Doing that at this module's import time would put it
    on the CLI, on the dashboard boot and on every core consumer that has never
    heard of a workflow. It happens here, once, when a tool node actually runs.
    """
    try:
        import importlib
        module = importlib.import_module("bgate_mcp.server")
    except Exception as exc:
        raise ToolNodeRefused(
            f"the MCP tool layer could not be loaded, so no tool node can run "
            f"on this build ({type(exc).__name__}: {exc}). The dashboard and "
            f"the model cards still work.") from exc
    fn = getattr(module, tool_name, None)
    if fn is None:
        raise ToolNodeRefused(
            f"this build has no tool named {tool_name!r} — the node type is "
            f"declared but the tool it points at is not present. Update "
            f"Builders Gate, or remove this node.")
    # functools.wraps put the original, synchronous function here. The decorated
    # one is an async FastMCP wrapper: awaiting it from a worker thread would
    # need a loop we do not have, and it is the same body either way.
    inner = getattr(fn, "__wrapped__", None)
    if inner is None or not callable(inner):
        raise ToolNodeRefused(
            f"{tool_name!r} is not shaped like an MCP tool on this build "
            f"(no __wrapped__ to call directly) — this is a Builders Gate bug, "
            f"not something the graph did wrong.")
    return module, inner


def call_tool(root, spec: ToolNode, kwargs: dict) -> dict:
    """Run the tool against THIS project and hand back its raw payload.

    The project root travels in the server's own ``_CALL_ROOT`` contextvar
    rather than as an argument, because that is the seam every tool body reads
    through ``_root()``. Setting it here is what makes a tool node act on the
    project the dashboard is serving instead of on whatever directory the
    process happens to be standing in — the exact bug the server's module
    docstring says the deleted ``_ACTIVE_ROOT`` used to cause.

    Reset in a ``finally``: the worker pool reuses threads, and a root left
    behind on one would silently become the default for the next node that runs
    on that thread.
    """
    module, inner = resolve(spec.tool)
    call_root = getattr(module, "_CALL_ROOT", None)
    token = None
    if call_root is not None:
        token = call_root.set(str(root))
    try:
        raw = inner(**kwargs)
    except TypeError as exc:
        # A signature mismatch between the table and this build of the tool.
        # Say which tool and which arguments, because the fix is a table row.
        raise ToolNodeRefused(
            f"{spec.label}: {spec.tool} would not accept the arguments this "
            f"node built ({exc}). Arguments sent: "
            f"{sorted(kwargs)}") from exc
    finally:
        if token is not None and call_root is not None:
            call_root.reset(token)
    normalize = getattr(module, "_normalize", None)
    if callable(normalize):
        try:
            raw = normalize(raw)
        except Exception:
            pass
    return raw if isinstance(raw, dict) else {"result": raw}


# ---------------------------------------------------------------------------
# Turning a tool's payload into what the engine stores on a node
# ---------------------------------------------------------------------------

# Where a tool puts the file it made. Every one of these is a real key used by
# at least one tool in server.py; the list is deliberately generous because a
# node that produced a file and reported none is invisible to everything
# downstream.
_PATH_KEYS = ("path", "out_path", "sheet", "png", "file", "screenshot",
              "shot", "video", "audio", "res_path", "scene_path", "glb",
              "gltf", "preview", "output")
_PATH_LIST_KEYS = ("paths", "frames", "files", "images", "shots", "tracks",
                   "renders")
_TEXT_KEYS = ("text", "summary", "note", "message", "verdict", "report")
# Lists whose entries are per-item results a tool produced several of.
_ITEM_LIST_KEYS = ("artifacts", "candidates", "variants", "items", "frames",
                   "shots", "tracks", "results", "entries")


def _as_artifact(entry: Any, root) -> Optional[dict]:
    """One registered artifact, in the shape ``workflows._inputs`` reads.

    That shape is not negotiable: a pick node chooses between dicts carrying
    ``artifact_id`` and ``path``, and anything that does not carry both is
    invisible to the picker no matter how real the file is.
    """
    if not isinstance(entry, dict):
        return None
    inner = entry.get("artifact") if isinstance(entry.get("artifact"), dict) else entry
    artifact_id = inner.get("artifact_id", inner.get("id"))
    path = inner.get("path") or entry.get("path")
    if artifact_id is None or not path:
        return None
    try:
        artifact_id = int(artifact_id)
    except (TypeError, ValueError):
        return None
    return {
        "artifact_id": artifact_id,
        "revision": inner.get("revision"),
        "logical_name": inner.get("logical_name") or entry.get("logical_name") or "",
        "path": str(path),
        "provider": str(entry.get("provider") or inner.get("provider") or ""),
        "model": str(entry.get("model") or inner.get("model") or ""),
    }


def harvest(root, spec: ToolNode, raw: dict) -> dict:
    """Everything downstream nodes could want, pulled out of one payload.

    Generic on purpose. Forty-five tools do not agree on a result schema and
    never will — ``image_generate`` answers ``{artifact:{...}, path}``,
    ``item_variants`` answers a list, ``scene_outline`` answers a tree with no
    files at all. Rather than a per-tool adapter (which is the thing this whole
    module exists to avoid), this looks for the shapes that MEAN something to
    the engine and leaves the rest under ``data`` for a human to read.
    """
    artifacts: list[dict] = []
    seen: set[int] = set()

    def take(entry: Any) -> None:
        art = _as_artifact(entry, root)
        if art and art["artifact_id"] not in seen:
            seen.add(art["artifact_id"])
            artifacts.append(art)

    take(raw)
    take(raw.get("artifact"))
    for key in _ITEM_LIST_KEYS:
        value = raw.get(key)
        if isinstance(value, list):
            for entry in value:
                take(entry)
        elif isinstance(value, dict):
            for entry in value.values():
                take(entry)

    paths: list[str] = []
    for key in _PATH_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value)
    for key in _PATH_LIST_KEYS:
        value = raw.get(key)
        if isinstance(value, list):
            paths.extend(str(v) for v in value if isinstance(v, str) and v.strip())
    # de-dupe, order preserved: the first path a tool names is the one it
    # considers its result.
    ordered: list[str] = []
    for path in paths:
        if path not in ordered:
            ordered.append(path)

    text = ""
    for key in _TEXT_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break

    usd = 0.0
    for key in ("usd", "usd", "cost_usd", "spend_usd"):
        try:
            if raw.get(key) is not None:
                usd = float(raw[key])
                break
        except (TypeError, ValueError):
            continue

    return {"artifacts": artifacts, "paths": ordered, "text": text,
            "usd": round(usd, 4)}


def _headline(spec: ToolNode, raw: dict, harvested: dict) -> str:
    """One sentence for the node card. What it DID, not that it finished."""
    bits: list[str] = []
    if spec.produces == "data" and not harvested["artifacts"]:
        # A tool whose answer is FACTS, not files. Counting the incidental paths
        # in its payload as "1 file" (godot_status reports the engine binary's
        # location) tells the reader something true and useless. Name the facts.
        keys = [k for k in raw
                if k not in ("ok", "error") and not k.startswith("_")]
        head = ", ".join(sorted(keys)[:6]) or "no detail reported"
        return f"{spec.tool}: {head}"
    if harvested["artifacts"]:
        count = len(harvested["artifacts"])
        bits.append(f"{count} artifact{'' if count == 1 else 's'}")
    elif harvested["paths"]:
        count = len(harvested["paths"])
        bits.append(f"{count} file{'' if count == 1 else 's'}")
    if harvested["usd"]:
        bits.append(f"~${harvested['usd']:.3f}")
    if harvested["text"]:
        bits.append(harvested["text"][:160])
    if not bits:
        # A tool that answered with facts and no files — scene_outline,
        # godot_status. Say what it answered rather than "done", which tells a
        # human nothing about whether to keep going.
        keys = [k for k in raw
                if k not in ("ok", "error") and not k.startswith("_")]
        bits.append(", ".join(sorted(keys)[:6]) or "no detail reported")
    return f"{spec.tool}: " + " · ".join(bits)


def _compact(raw: dict, limit: int = 4000) -> Any:
    """The payload, small enough to store on a node row.

    A run's node rows are polled by the browser on a 2.5s loop. A scene outline
    of a big level, stored whole, would be shipped on every tick — so the blob
    is capped and says so rather than being silently truncated into invalid
    data.
    """
    try:
        text = json.dumps(raw, default=str)
    except (TypeError, ValueError):
        return {"unserialisable": True}
    if len(text) <= limit:
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return {"unserialisable": True}
    return {"truncated": True, "bytes": len(text),
            "preview": text[:limit],
            "note": "this tool's payload was too large to keep on the node; "
                    "the files it produced are still registered"}


def run(root, *, run_id: int, node_id: str, label: str = "",
        node_type: str = "", config: Optional[dict] = None,
        inputs: Optional[dict] = None) -> dict:
    """Execute one tool node. Returns the same envelope ``generate.run`` does.

    ``{ok, error, artifacts, provider, model, logical_name, usd, output}`` —
    identical on purpose, so :mod:`bgate_core.board.workflows` stores a tool node's
    result through exactly the code path it already uses for a generate node.
    Two result shapes would mean two writers of ``output_json`` and two ways for
    the wire to be empty.

    ``ok=False`` always carries an ``error`` a human can act on.
    """
    spec = spec_for(node_type)
    if spec is None:
        return {"ok": False, "artifacts": [], "usd": 0.0,
                "error": f"{node_type!r} is not a tool node this build knows "
                         f"about — it was drawn by a newer (or older) "
                         f"dashboard than this server."}
    config = config if isinstance(config, dict) else {}
    inputs = inputs or {}

    try:
        kwargs = build_kwargs(root, spec, config, inputs)
    except ToolNodeRefused as exc:
        return {"ok": False, "artifacts": [], "usd": 0.0, "error": str(exc)}

    try:
        raw = call_tool(root, spec, kwargs)
    except ToolNodeRefused as exc:
        return {"ok": False, "artifacts": [], "usd": 0.0, "error": str(exc)}
    except Exception as exc:  # a tool that raised must not leave a bare status
        return {"ok": False, "artifacts": [], "usd": 0.0,
                "error": f"{spec.label}: {spec.tool} raised "
                         f"{type(exc).__name__}: {exc}"}

    if raw.get("error") or raw.get("ok") is False:
        reason = str(raw.get("error") or "").strip()
        return {"ok": False, "artifacts": [], "usd": 0.0,
                "error": f"{spec.label}: {reason or 'the tool refused and gave no reason'}",
                "output": {"data": _compact(raw)}}

    harvested = harvest(root, spec, raw)
    output = {
        "artifacts": harvested["artifacts"],
        "paths": harvested["paths"],
        "provider": str(raw.get("provider") or ""),
        "model": str(raw.get("model") or ""),
        "logical_name": (harvested["artifacts"][0]["logical_name"]
                         if harvested["artifacts"] else str(raw.get("name") or "")),
        "tool": spec.tool,
        "data": _compact(raw),
    }
    # `text` only when there is one: an empty string on the wire would satisfy
    # a downstream node's "is anything wired in" test and then send nothing.
    if harvested["text"]:
        output["text"] = harvested["text"]
    elif not harvested["artifacts"] and not harvested["paths"]:
        # A pure-data tool still has to be usable as an INPUT to the next node.
        # Its payload, as compact JSON, is the honest text form of "what this
        # step learned" — and it is what {input} will interpolate.
        try:
            output["text"] = json.dumps(_compact(raw, 1200), default=str)
        except (TypeError, ValueError):
            pass

    return {"ok": True, "error": "", "artifacts": harvested["artifacts"],
            "provider": output["provider"], "model": output["model"],
            "logical_name": output["logical_name"],
            "usd": harvested["usd"],
            "message": _headline(spec, raw, harvested),
            "output": output}


# ---------------------------------------------------------------------------
# GLUE — the in-between nodes that make an arbitrary graph possible
# ---------------------------------------------------------------------------
#
# Before these, the only thing between two steps was a gate, a pick or a
# consistency check: three ways for a HUMAN to intervene and no way for the
# graph to reshape a value. So a tool that produced a list and a tool that
# consumed one item could not be joined, and a generated name could not become
# the next tool's argument. These cost nothing, call nothing, and run inline —
# they are pure data flow, which is exactly why they are what unlocks the rest.

FLOW_TYPES = ("flow.format", "flow.branch", "flow.merge", "flow.filter",
              "flow.each")


def is_flow_node(node_type: str) -> bool:
    return str(node_type or "") in FLOW_TYPES


def _flow_items(inputs: dict) -> list[dict]:
    """The list a filter/index node is working over.

    Registered artifacts AND raw files, because half the engine side produces
    the second kind and a glue node that could only see the first would be
    unusable exactly where the graph gets interesting.
    """
    items = list(inputs.get("picked") or ())
    for cand in inputs.get("candidates") or ():
        if isinstance(cand, dict) and cand not in items:
            items.append(cand)
    known = {str(i.get("path") or "") for i in items}
    for path in inputs.get("paths") or ():
        if isinstance(path, str) and path.strip() and path not in known:
            items.append({"artifact_id": None, "path": path, "abspath": path,
                          "logical_name": Path(path).stem, "model": "",
                          "provider": ""})
    return items


def _flow_emit(items: list[dict], **extra) -> dict:
    """Split a glue node's items back into the two wires the engine reads.

    ``artifacts`` must contain ONLY things with an artifact row: a pick node
    downstream resolves a choice by id, and an entry without one would be
    offered and then refused. Raw files ride on ``paths``, which is what a tool
    node reads.
    """
    out: dict = dict(extra)
    out["artifacts"] = [i for i in items if i.get("artifact_id") is not None]
    out["paths"] = [str(i.get("path")) for i in items if i.get("path")]
    return out


def flow_output(root, spec: dict, inputs: dict) -> dict:
    """Run one glue node. Returns the node's output dict, or a refusal.

    A refusal here is ``{"flow_error": "..."}``; the engine fails the node with
    that sentence. Silence would be worse than a failure: a merge node that
    quietly emitted nothing looks exactly like a merge node whose parents have
    not run yet.
    """
    node_type = str(spec.get("type") or "")
    config = spec.get("config") if isinstance(spec.get("config"), dict) else {}
    inputs = inputs or {}

    if node_type == "flow.format":
        # A STRING BUILT FROM THE WIRE. This is the node that lets a generated
        # value become the NEXT tool's argument: "res://characters/{input}.tscn"
        # is a scene path built from a name the graph produced two steps ago.
        template = str(config.get("template") or "").strip()
        if not template:
            return {"flow_error":
                    "this format node has no template — write one, using "
                    "{input} for the upstream text and {path} for the upstream "
                    "file"}
        return {"text": interpolate(template, inputs)}

    if node_type == "flow.merge":
        # Several wires into one value. Join order is the parents' order, which
        # is the order the canvas drew them, so the result is stable across runs.
        parts = [str(inputs.get("text") or "").strip()]
        items = _flow_items(inputs)
        separator = str(config.get("separator") or "\n")
        if config.get("include_paths", True):
            parts.extend(str(i.get("path") or "") for i in items)
        text = separator.join(p for p in parts if p)
        return _flow_emit(items, text=text)

    if node_type == "flow.filter":
        # Keep the upstream candidates whose path or name matches. A comparison
        # that fanned into four models and only wants the krea ones is a filter,
        # not four hand-deleted wires.
        needle = str(config.get("contains") or "").strip().lower()
        field_name = str(config.get("field") or "path").strip() or "path"
        items = _flow_items(inputs)
        if not needle:
            kept = items
        else:
            kept = [i for i in items
                    if needle in str(i.get(field_name) or "").lower()]
        if not kept:
            return {"flow_error":
                    f"nothing upstream matched {needle!r} on '{field_name}' — "
                    f"{len(items)} candidate(s) were offered, so either the "
                    f"filter is wrong or the step before it produced the wrong "
                    f"thing"}
        emitted = _flow_emit(kept)
        if kept[0].get("artifact_id") is not None:
            emitted["picked"] = kept[0]
        return emitted

    if node_type == "flow.each":
        # ONE ITEM OF A LIST, BY INDEX.
        #
        # An honest half of foreach. A true map would have to MINT NODES at run
        # time, and a run's graph is snapshotted at start precisely so a page
        # reload repaints what actually happened — expanding it mid-run would
        # make that snapshot a lie. So this selects item N and reports the
        # count; N of these side by side (the canvas can duplicate a card) is a
        # map you can see, which is also the only kind you can debug.
        items = _flow_items(inputs)
        try:
            index = int(config.get("index", 0))
        except (TypeError, ValueError):
            return {"flow_error": "the index must be a whole number"}
        if not items:
            return {"flow_error":
                    "nothing upstream produced a list to step through"}
        if index < 0 or index >= len(items):
            return {"flow_error":
                    f"index {index} is outside the {len(items)} item(s) "
                    f"upstream — this list has 0..{len(items) - 1}"}
        chosen = items[index]
        emitted = _flow_emit([chosen],
                             text=str(chosen.get("logical_name") or ""),
                             count=len(items))
        if chosen.get("artifact_id") is not None:
            emitted["picked"] = chosen
        return emitted

    if node_type == "flow.branch":
        # A CONDITION, EVALUATED ON THE WIRE — never on arbitrary code. `eval`
        # here would make a saved workflow an execution vector: a graph is a
        # document that gets shared, and a document must not be able to run
        # anything. Four comparisons cover what a graph actually branches on.
        left = str(config.get("left") or "{input}")
        left = interpolate(left, inputs)
        right = str(config.get("right") or "")
        test = str(config.get("test") or "contains").strip().lower()
        if test == "contains":
            passed = right.lower() in left.lower()
        elif test == "equals":
            passed = left.strip() == right.strip()
        elif test == "not_empty":
            passed = bool(left.strip())
        elif test in (">", "<", ">=", "<="):
            try:
                a, b = float(left), float(right)
            except (TypeError, ValueError):
                return {"flow_error":
                        f"'{left}' and '{right}' are not both numbers, so they "
                        f"cannot be compared with {test}"}
            passed = {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[test]
        else:
            return {"flow_error":
                    f"unknown test {test!r} — use contains, equals, not_empty, "
                    f">, <, >= or <="}
        if not passed and config.get("stop_when_false", True):
            # A branch that does not branch is a gate that always opens. Failing
            # the node is what actually stops the downstream half of the graph,
            # and the run bar then says which condition stopped it.
            return {"flow_error":
                    f"the condition did not hold: {left[:120]!r} {test} "
                    f"{right[:120]!r} — the steps after this one will not run"}
        return _flow_emit(_flow_items(inputs), text=left, passed=passed)

    return {}


def flow_catalogue() -> list[dict]:
    """The glue nodes, for the palette. Same contract as :func:`catalogue`."""
    return [
        {"type": "flow.format", "label": "Format text", "category": "control",
         "glyph": "⌇", "accent": "var(--spark)",
         "summary": "Build a string out of what the wires carried - {input} is "
                    "the upstream text, {path} its file, {name} its logical "
                    "name. This is how a generated value becomes the next "
                    "tool's argument.",
         "args": [{"name": "template", "label": "Template", "widget": "area",
                   "default": "{input}", "help": "{input} {text} {path} {name}"}]},
        {"type": "flow.merge", "label": "Merge", "category": "control",
         "glyph": "⌇", "accent": "var(--spark)",
         "summary": "Join several wires into one value - the upstream text "
                    "plus every upstream file, in the order they were drawn.",
         "args": [{"name": "separator", "label": "Separator", "widget": "text",
                   "default": "\n"},
                  {"name": "include_paths", "label": "Include files",
                   "widget": "toggle", "default": True}]},
        {"type": "flow.filter", "label": "Filter", "category": "control",
         "glyph": "⌇", "accent": "var(--spark)",
         "summary": "Keep only the upstream candidates that match. Fails "
                    "loudly when nothing matches, because an empty wire that "
                    "looks like a working one is the worst outcome here.",
         "args": [{"name": "contains", "label": "Contains", "widget": "text",
                   "default": ""},
                  {"name": "field", "label": "Field", "widget": "select",
                   "default": "path",
                   "options": ["path", "logical_name", "model", "provider"]}]},
        {"type": "flow.each", "label": "Item N", "category": "control",
         "glyph": "⌇", "accent": "var(--spark)",
         "summary": "One item of an upstream list, by index. Duplicate the "
                    "card to walk the list - a map you can see and step "
                    "through, rather than nodes that appear mid-run.",
         "args": [{"name": "index", "label": "Index", "widget": "number",
                   "default": 0}]},
        {"type": "flow.branch", "label": "Branch", "category": "control",
         "glyph": "⌇", "accent": "var(--warn)",
         "summary": "Stop the graph unless a condition holds. No code is "
                    "evaluated - a saved workflow is a document people share, "
                    "and a document must not be able to run anything.",
         "args": [{"name": "left", "label": "Value", "widget": "text",
                   "default": "{input}"},
                  {"name": "test", "label": "Test", "widget": "select",
                   "default": "contains",
                   "options": ["contains", "equals", "not_empty",
                               ">", "<", ">=", "<="]},
                  {"name": "right", "label": "Compare to", "widget": "text",
                   "default": ""},
                  {"name": "stop_when_false", "label": "Stop if false",
                   "widget": "toggle", "default": True}]},
    ]
