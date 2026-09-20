"""Which engine a project is built in, the table every engine-bound surface asks.

THE COLUMN EXISTED AND MEANT NOTHING. ``project.engine`` has been in the schema
since migration 0001 (``store/db.py``), but it had two legal values and exactly
one reader that changed behaviour: ``bgate_ui/agents/dispatch.py`` picked which
sentence to put in a seat brief. Every ``godot_*`` tool, every ``scene_*`` tool,
doctor's Godot rows and the whole level surface registered and ran regardless of
what the row said. A project recorded as ``engine="none"`` still advertised 18
Godot tools to every agent it dispatched.

So this is the table that gives the column teeth. It is DATA, not an interface:
the house pattern for "a capability a project may or may not have" is already a
dict of specs plus module functions (``store/modules.py``'s MODULES, CRAFTS,
SPINE_GROUPS), and it is the right one here for a reason that an abstract base
class would get wrong. Godot's adapter is ~6,000 lines across five modules -
scene surgery, resource inspection, import freshness, in-engine animation
capture, humanoid retargeting. A web engine implements maybe a dozen of those
concepts and a Unity engine a different dozen. An ABC would demand every engine
answer every question, and the honest answer for most pairs is "that question
does not exist here". A table lets an engine own the surfaces it has and be
absent from the rest, which is what ``ENGINE_TOOLS`` (phase 2) will read.

WHAT A SPEC MEANS
-----------------
``markers``   Relative paths that identify the engine's project directory. Read
              by ``project.game_dir``, the "where does the engine project
              actually live" resolver, and by ``adopt.detect``.
``adapter``   Dotted module path, imported lazily. ``None`` means DECLARED BUT
              NOT IMPLEMENTED: the engine is detected and named correctly, and
              every tool that would need it stays unregistered. Unity is here on
              those terms deliberately, so ``bgate adopt`` on a Unity project
              says "Unity, unsupported" instead of "not a game", which is the
              answer that sends someone to the docs instead of to a bug report.
``binary_env``The environment override for the engine's executable, mirroring
              BGATE_GODOT. Named here so ``dispatch._toolchain_env`` can resolve
              whichever one this project needs without knowing the engines.
``template``  Subdirectory of ``src/templates`` holding the scaffold, under the
              ``<engine>/<kind>`` layout. Empty for an engine that is adopted
              and never scaffolded (unity).
``shared``    Subdirectory of ``src/templates`` whose ``shared/`` tree adopt
              stamps (CLAUDE.md, .gitignore, the telemetry sources). Defaults
              to ``template``; set separately when an engine has the second
              without the first.
``doctor``    Doctor rows only this engine needs. A row named by an engine that
              is not this project's is MARKED, not removed, the same rule, and
              for the same reason, as a disabled module's row.
``consumer_suffixes``
              File types that can REFERENCE an asset in this engine. ``.tscn``
              and ``.gd`` are how ``store/assets.py`` answers "is this sprite
              actually used"; a web project's answer lives in ``.ts`` and
              ``.html``. Recorded now, consumed when the asset graph learns the
              engine axis.

NOT HERE ON PURPOSE: anything about HOW an engine runs, screenshots or tests.
That is the adapter's job and it belongs in ``bgate_adapters/<engine>.py``. This
module knows only which engines exist, how to recognise one on disk, and where
to find its adapter, so importing it is cheap and cannot drag Godot, Blender or
a browser driver into a process that only wanted to read a project row.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Optional

#: What a project is recorded as when nothing said otherwise. Every existing
#: project reads this already, the schema default is the same string, so
#: widening the legal set costs no migration and changes no row.
DEFAULT = "godot"

#: The engine of a directory that holds no game at all. `adopt` downgrades to
#: this when it finds no marker, and it is what the scratch project carries.
NONE = "none"


ENGINES: dict[str, dict] = {
    "godot": {
        "label": "Godot",
        "blurb": "Godot 4. Scene surgery, in-engine checks, screenshots and "
                 "the telemetry autoload. The engine Builders Gate grew up in.",
        "markers": ("project.godot",),
        "adapter": "bgate_adapters.godot",
        "binary_env": "BGATE_GODOT",
        "template": "godot",
        "doctor": ("godot", "godot_web_templates"),
        "consumer_suffixes": (".gd", ".tscn", ".tres", ".godot", ".gdshader"),
    },
    "web": {
        "label": "Web",
        "blurb": "A TypeScript game that ships to a URL. Dev server for "
                 "running it, a headless browser for evidence, and a payload "
                 "budget so a build that nobody could download fails loudly.",
        # package.json is a WEAK marker and is checked last for it, see
        # DETECT_ORDER. A Godot project with a tooling package.json at its root
        # is a real thing and must not read as a web game.
        "markers": ("package.json",),
        "adapter": "bgate_adapters.web",
        "binary_env": "BGATE_NODE",
        "template": "web",
        "doctor": ("node", "playwright"),
        "consumer_suffixes": (".ts", ".tsx", ".js", ".jsx", ".html", ".json"),
    },
    "unity": {
        "label": "Unity",
        "blurb": "Unity, adopted rather than scaffolded. Batchmode compile "
                 "checks, the Test Framework scored into the shared history, "
                 "editor-rendered scene stills and a telemetry MonoBehaviour "
                 "that needs no scene wiring.",
        "markers": ("ProjectSettings/ProjectVersion.txt",),
        "adapter": "bgate_adapters.unity",
        "binary_env": "BGATE_UNITY",
        # No scaffold: a Unity project is made by the Hub with a version and a
        # render pipeline the user chose. `shared` is what adopt stamps.
        "template": "",
        "shared": "unity",
        "doctor": ("unity",),
        "consumer_suffixes": (".cs", ".unity", ".prefab", ".asset", ".mat",
                              ".asmdef", ".controller", ".anim"),
    },
    NONE: {
        "label": "No engine",
        "blurb": "A directory Builders Gate tracks that is not a game project "
                 "- art, audio and canon work with nothing to run.",
        "markers": (),
        "adapter": None,
        "binary_env": "",
        "template": "",
        "doctor": (),
        "consumer_suffixes": (),
    },
}

#: THE ORDER DETECTION ASKS IN, AND WHY IT IS NOT ALPHABETICAL.
#:
#: Markers are not equally trustworthy. `project.godot` and
#: `ProjectSettings/ProjectVersion.txt` are written by an engine and mean one
#: thing; `package.json` is written by anything with a build step. Godot
#: projects carry one (this repo's own dashboard assets do), so a run that
#: checked web first would relabel a Godot game as a web game on a file that
#: has nothing to do with the engine. Strong markers first, weak marker last.
DETECT_ORDER: tuple[str, ...] = ("godot", "unity", "web")


class EngineUnsupported(RuntimeError):
    """This engine is recognised but Builders Gate cannot drive it."""


# ---------------------------------------------------------------------------
# Which tools belong to which engine
# ---------------------------------------------------------------------------
# EXACT NAMES, NOT PREFIXES, and the choice is deliberate. `MODULES` matches
# prefixes because a module owns a whole family (`music_`, `blender_`) and a new
# member of that family should be gated the day it is written. Engine ownership
# is the opposite shape: `godot_` looks like a family and is not one, three of
# its members are the evidence spine every seat is asked for, and `scene_`,
# `level_` and `tileset_` are engine-bound without carrying an engine's name at
# all. A prefix rule here would be wrong in both directions at once, so each
# tool is listed and a test guards the list against ghosts.
#
# THE TEST FOR MEMBERSHIP: can this tool do its job at all without the engine?
# Not "does it mention the engine somewhere", several tools finish by writing
# an engine file and are useful long before they get there.
ENGINE_TOOLS: dict[str, frozenset[str]] = {
    "godot": frozenset({
        # The adapter surface: the binary, the project, the resources.
        "godot_character_wire", "godot_check_project", "godot_clip_capture",
        "godot_clip_retarget", "godot_deliver_asset", "godot_evidence",
        "godot_export_probe", "godot_export_verify", "godot_import_asset",
        "godot_inspect_resource", "godot_retarget_check", "godot_run",
        "godot_scaffold", "godot_scene_audit", "godot_screenshot",
        "godot_status", "godot_templates", "godot_test_run",
        # Scene surgery. `bgate_core.level.scenewire` parses .tscn as text, so
        # these never touch the binary, and are no less Godot for it.
        "scene_attach_script", "scene_node_add", "scene_outline",
        "scene_rename_node", "scene_reparent_node", "scene_set_property",
        "scene_swap_resource", "scene_unwire", "scene_wire",
        # The level emitters. Every one takes `godot_project` and writes .tscn
        # or a TileSet .tres; there is nothing left of them without that.
        "blockout_generate", "level_generate", "level_reskin",
        "sidescroll_generate", "tileset_describe", "tileset_generate",
        "tileset_synth", "track_generate",
        # Verdicts that reach into the running engine.
        "traversal_prove", "evidence_check_ui",
        # Deliverables whose only product is a Godot resource: a cutout rig is
        # a .tscn of Sprite2Ds, and SpriteFrames is a .tres or it is nothing.
        "cutout_assemble", "cutout_equip", "item_to_spriteframes",
        # Kits are GDScript with project.godot input actions; a web or
        # Unity project has nothing for them to land in.
        "kit_list", "kit_install", "kit_remove",
    }),
    "web": frozenset({
        # The adapter surface. `engine_check` and `engine_status` are NOT here:
        # they are the neutral layer that ASKS whichever engine the project
        # records, so owning them for web would hide them from Godot.
        "web_status", "web_build", "web_payload", "web_dev", "web_dev_stop",
        "web_test_run", "web_run",
    }),
    "unity": frozenset({
        "unity_status", "unity_check", "unity_test_run", "unity_execute",
        "unity_install_scripts", "unity_screenshot",
    }),
    NONE: frozenset(),
}

# WHAT IS DELIBERATELY NOT ENGINE-OWNED, having been looked at and left open:
#
# `playtest_*`, the launcher already has its own escape hatch (`game_cmd` in
#   bgate_core.qa.playtest), and eight of the nine tools are analysis: listing,
#   promoting and dismissing findings that are already recorded. Gating the set
#   would take the analysis away from a project that captured its playtests some
#   other way, to prevent one tool from failing with a clear message.
# `iteration_record_checks` / `iteration_status`, a ledger. They live in the
#   `engine` SPINE group because of which SEATS carry them, which is a different
#   question from which engine can run them; neither touches a scene.
# `evidence_assert`, records what a human or agent SAW in a file, with a digest.
#   The file came from an engine; the claim does not need one.
# `level_plan`, lays out rooms and prints ascii. It is the half of the level
#   pipeline that has no scene in it, and that is the whole point of it existing
#   separately from `level_generate`.
# `ui_concept`, paints concept frames and derives a palette and brief; the
#   Theme .tres is the last of three outputs. The frames and the brief are the
#   value and they are engine-free.
# `room_review` / `scale_check` / `sprite_sheet_check` / `canon_check`: read
#   images and records, never a scene.


def tool_owner(tool_name: str) -> str:
    """Which engine owns this tool; '' when it belongs to none of them.

    A tool nobody owns is engine-free and registers everywhere, the same
    default the module and craft tables use, and for the same reason: guessing
    that something is specialised when it is not breaks a workflow silently.
    """
    for engine, owned in ENGINE_TOOLS.items():
        if tool_name in owned:
            return engine
    return ""


def tool_enabled(tool_name: str, engine: str) -> bool:
    """Does this MCP tool survive the project's engine?"""
    owner = tool_owner(tool_name)
    return (not owner) or owner == engine


def owned_tools() -> frozenset[str]:
    """Every tool any engine claims."""
    return frozenset().union(*ENGINE_TOOLS.values())


def names() -> tuple[str, ...]:
    """Every legal value of ``project.engine``."""
    return tuple(ENGINES)


def known(engine: str) -> bool:
    return str(engine or "").strip() in ENGINES


def spec(engine: str) -> dict:
    key = str(engine or "").strip()
    if key not in ENGINES:
        raise ValueError(f"engine must be one of {names()}, got {engine!r}")
    return ENGINES[key]


def label(engine: str) -> str:
    """The engine's display name, or the raw value when it is not one of ours.

    Tolerant on purpose: this is called to render a row that already exists, and
    a stored value we no longer recognise should read as itself in the UI rather
    than take the page down.
    """
    try:
        return spec(engine)["label"]
    except ValueError:
        return str(engine or "")


def catalog() -> list[dict]:
    """Every engine with its label, blurb and whether it can actually be driven."""
    return [{"name": name, "label": item["label"], "blurb": item["blurb"],
             "supported": item["adapter"] is not None}
            for name, item in ENGINES.items()]


def markers(engine: str) -> tuple[str, ...]:
    return tuple(spec(engine)["markers"])


def supported(engine: str) -> bool:
    """Is there an adapter for this engine, or is it declaration only?"""
    try:
        return spec(engine)["adapter"] is not None
    except ValueError:
        return False


def adapter(engine: str):
    """The engine's adapter module, imported on demand.

    Raises EngineUnsupported rather than ImportError for a declared-but-unbuilt
    engine, because the two failures need different answers: "Unity has no
    adapter" is a product fact a caller can report to a user, and "the Godot
    adapter would not import" is a broken install.
    """
    item = spec(engine)
    dotted = item["adapter"]
    if not dotted:
        raise EngineUnsupported(
            f"{item['label']} projects are recognised but not driven: Builders "
            f"Gate has no adapter for {engine!r}, so its engine tools are not "
            "registered. The board, canon and art pipeline still work.")
    return importlib.import_module(dotted)


def binary(engine: str) -> str:
    """Absolute path to this engine's executable.

    THE SINGLE POINT THE 13 SCATTERED find_godot() CALLS COLLAPSE INTO. Each
    adapter exposes ``find_binary()`` with no arguments; Godot's is a thin
    delegate to ``find_godot()``, which keeps its ``prefer_console`` argument
    for the one caller that wants a visible console window.
    """
    return adapter(engine).find_binary()


def binary_env(engine: str) -> str:
    """Name of the environment variable that overrides this engine's binary."""
    try:
        return spec(engine)["binary_env"]
    except ValueError:
        return ""


def template_dir_name(engine: str) -> str:
    """Subdirectory of src/templates holding this engine's scaffold ('' = none)."""
    try:
        return spec(engine)["template"]
    except ValueError:
        return ""


def shared_dir_name(engine: str) -> str:
    """Subdirectory of src/templates whose shared/ tree adopt stamps ('' = none)."""
    try:
        item = spec(engine)
    except ValueError:
        return ""
    return item.get("shared") or item.get("template") or ""


def consumer_suffixes(engine: str) -> tuple[str, ...]:
    """File types that can reference an asset in this engine."""
    try:
        return tuple(spec(engine)["consumer_suffixes"])
    except ValueError:
        return ()


# ---------------------------------------------------------------------------
# Recognising an engine on disk
# ---------------------------------------------------------------------------
def marker_hit(directory: str | os.PathLike[str], engine: str) -> Optional[Path]:
    """The marker file proving `directory` is this engine's project, or None."""
    base = Path(directory)
    for rel in markers(engine):
        candidate = base / rel
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def detect_engine(directory: str | os.PathLike[str]) -> str:
    """Which engine's project this directory IS, '' when none of them.

    Only the directory handed in; no recursion and no <root>/game fallback. The
    two-candidate search is ``project.game_dir``'s job and stays there, so this
    stays a pure predicate that a caller can point anywhere.
    """
    for engine in DETECT_ORDER:
        if marker_hit(directory, engine) is not None:
            return engine
    return ""


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------
def doctor_rows(engine: str) -> tuple[str, ...]:
    try:
        return tuple(spec(engine)["doctor"])
    except ValueError:
        return ()


def engine_rows() -> frozenset[str]:
    """Every doctor row that belongs to SOME engine."""
    return frozenset().union(*(frozenset(item["doctor"])
                               for item in ENGINES.values()))


def doctor_row_enabled(row_name: str, engine: str) -> bool:
    """Is this doctor row anyone's requirement on THIS project?

    Same shape and same rule as ``modules.doctor_row_enabled``: a row nobody
    claims is a core row and always graded; a row an engine claims is graded
    only for that engine. A web project must not open doctor to a red
    ``godot_web_templates``, and a Godot project must not be graded on a browser
    driver it will never launch.
    """
    if row_name not in engine_rows():
        return True
    return row_name in doctor_rows(engine)
