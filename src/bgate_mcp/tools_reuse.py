"""Reuse MCP tools: kits (systems) and the machine-wide asset library.

Same contract as tools_level: the shared plumbing stays in server, this
module imports it back, and server star-imports this module at its bottom.

Two things a project could not do before this module, and the reason they
share a file: both are about the boundary between one game and the next.

  * KITS are systems: a four-way controller, an inventory, a health
    component. Each is a manifest plus scripts under
    ``src/templates/kits/<engine>/``, installed into THIS project one at a
    time, never overwriting. ``bgate_core.store.kits`` is the mechanism.
  * THE LIBRARY is assets: a sprite sheet one game generated, published to
    ``~/.bgate/library`` and imported into another. Content-addressed, in
    no repository, following the person. ``bgate_core.store.assetlib``.

Kits are Godot-only for now (they are GDScript) and register under that
engine; the library is engine-neutral and registers everywhere.
"""
from __future__ import annotations

from typing import Annotated, Optional

from pydantic import Field

from bgate_core.store import assetlib as _assetlib
from bgate_core.store import kits as _kits

from bgate_mcp.server import (  # noqa: F401
    _Path, _assets, _contained_path, _godot, _log, _note_tool_write,
    _project, _root, _tool,
)


def _game_dir(godot_project: str) -> _Path:
    """The engine project a kit lands in: the argument, or the pinned
    project's own engine directory."""
    if godot_project:
        _contained_path(godot_project, "godot_project")
        return _Path(godot_project)
    root = _root()
    found = _project.game_dir(root, _project.engine_of(root))
    if found is None:
        raise FileNotFoundError(
            f"no engine project under {root}; pass godot_project")
    return _Path(found)


def _dimension() -> str:
    try:
        return _project.get(_root()).get("dimension") or ""
    except Exception:                                             # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Kits
# ---------------------------------------------------------------------------
@_tool
def kit_list(dimension: Annotated[str, Field(description="2d | 3d | any. Empty: the project's own dimension, so a 2D game is not offered a vehicle.")] = "",
             godot_project: Annotated[str, Field(description="Directory holding project.godot. Empty: the pinned project's own.")] = "") -> dict:
    """THE SHELF OF REUSABLE SYSTEMS, and which are already in this project.

    Every kit for this engine: name, what it does, what it needs (input
    actions, autoloads, other kits), what it provides (signals, exports,
    groups), and its `state` here: absent, installed, modified (a file
    was edited since install), partial, or stale (the kit has changed
    upstream since it was installed). Read `usage` before wiring one.
    Free, no side effects. Then kit_install(name).
    Full notes: docs/tools.md#kit_list
    """
    dim = dimension if dimension in ("2d", "3d") else (
        "" if dimension == "any" else _dimension())
    try:
        game = _game_dir(godot_project)
    except FileNotFoundError as exc:
        # No engine project yet: the shelf is still worth reading.
        out = {"kits": _kits.list_kits("godot", dim), "engine": "godot",
               "dimension": dim or "any", "note": str(exc)}
        return out
    return _kits.status(_root(), game, "godot", dim)


@_tool
def kit_install(name: Annotated[str, Field(description="A kit from kit_list: topdown_controller, platformer_controller, third_person_controller, vehicle_controller, interaction_2d, interaction_3d, inventory, health.")],
                godot_project: Annotated[str, Field(description="Directory holding project.godot. Empty: the pinned project's own.")] = "",
                bind_actions: Annotated[bool, Field(description="Append the kit's MISSING input actions to project.godot [input] with default keys. Existing actions are never touched. Default True.")] = True,
                force: Annotated[bool, Field(description="Replace a file that exists and differs (a timestamped .bak is kept). Default False: such a file is reported and left alone.")] = False,
                check: Annotated[bool, Field(description="Load every installed script inside the running project afterwards and report whether the engine compiled it. Default True; skipped with a reason when Godot is not installed.")] = True,
                any_dimension: Annotated[bool, Field(description="Install a kit whose dimension does not match the project's (a 3D kit into a 2D game). Default False: refused with the mismatch named.")] = False) -> dict:
    """DROP A SYSTEM INTO THE GAME: copies one kit's scripts into scripts/,
    adds the input actions it reads, and proves the engine compiles them.

    Never overwrites: a file already present is `kept` with whether it
    matches, and `force` is the only way past that. `actions_added` is
    what landed in project.godot; `autoloads_missing` and `kits_missing`
    are what the kit still needs that this project lacks. `usage` says how
    to wire it. The ledger at .bgate/kits.json is what lets kit_list tell
    installed from modified later.
    Full notes: docs/tools.md#kit_install
    """
    root = _root()
    game = _game_dir(godot_project)
    result = _kits.install(root, game, name, engine="godot", force=force,
                           bind=bind_actions,
                           dimension="" if any_dimension else _dimension())
    for rel in result["written"]:
        _note_tool_write(root, str(game / rel))
    if result["actions_added"]:
        _note_tool_write(root, str(game / "project.godot"))
    if check:
        if _godot.available().get("available") and (game / "project.godot").is_file():
            # load() + can_instantiate() inside the running project, not
            # --import: an unreferenced script is not compiled by an import,
            # and every kit script is unreferenced the moment it lands.
            result["check"] = _kits.prove(
                game, [f["dest"] for f in _kits.load(name)["files"]])
            if not result["check"].get("ok"):
                result["ok"] = False
        else:
            result["check"] = {"skipped": "Godot not installed; run "
                                          "engine_check when it is"}
    _log("kit", f"{name}: wrote {len(result['written'])} file(s), "
                f"added actions {result['actions_added'] or 'none'}"
                + ("" if result["ok"] else "; NOT OK"),
         ref=name)
    return result


@_tool
def kit_remove(name: Annotated[str, Field(description="A kit the ledger says is installed.")],
               godot_project: Annotated[str, Field(description="Directory holding project.godot. Empty: the pinned project's own.")] = "",
               force: Annotated[bool, Field(description="Delete files even when they were edited after install. Default False: an edited file is refused and named.")] = False) -> dict:
    """Take a kit's files back out. Refuses any file edited since install
    unless `force`. Input actions stay: another script may read them by
    now, and an unbound action is a silent no-op, not an error.
    Full notes: docs/tools.md#kit_remove
    """
    root = _root()
    game = _game_dir(godot_project)
    result = _kits.remove(root, game, name, engine="godot", force=force)
    for rel in result["removed"]:
        _note_tool_write(root, str(game / rel))
    _log("kit", f"{name}: removed {len(result['removed'])}, "
                f"refused {len(result['refused'])}", ref=name)
    return result


# ---------------------------------------------------------------------------
# The library
# ---------------------------------------------------------------------------
@_tool
def library_publish(paths: Annotated[list[str], Field(description="Files or directories INSIDE this project. A directory publishes every image, audio, model, resource, scene and script under it (never .import files). Sidecar .rig.json files ride with their sheet.")],
                    tags: Annotated[Optional[list[str]], Field(description="Free tags, lowercased: [\"hero\", \"walk\", \"pixel\"]. Searchable.")] = None,
                    collection: Annotated[str, Field(description="A name grouping these entries (a character, a tileset, a prop set). One word a person would say.")] = "",
                    note: Annotated[str, Field(description="What this is and how it was made, for whoever finds it next year.")] = "") -> dict:
    """PUT AN ASSET WHERE THE NEXT GAME CAN FIND IT: copies files out of
    this project into the machine-wide library at ~/.bgate/library.

    Content-addressed: the same bytes published twice are stored once and
    come back under `existing` with the new tags merged. Nothing leaves the
    machine and nothing lands in any repository. Each entry records the
    project and path it came from. Then library_search finds it from any
    project and library_import brings it in.
    Full notes: docs/tools.md#library_publish
    """
    root = _root()
    try:
        project_name = _project.get(root).get("name") or ""
    except Exception:                                             # noqa: BLE001
        project_name = ""
    result = _assetlib.publish(root, paths, tags=tags, collection=collection,
                               note=note, project_name=project_name)
    _log("library", f"published {len(result['published'])} new, "
                    f"{len(result['existing'])} already held"
                    + (f" -> {collection}" if collection else ""),
         ref=collection)
    return result


@_tool
def library_search(query: Annotated[str, Field(description="Substring over name, tags, collection and note. Empty matches everything.")] = "",
                   kind: Annotated[str, Field(description="texture | audio | model | blender | scene | resource | script | vector. Empty: any.")] = "",
                   tags: Annotated[Optional[list[str]], Field(description="Every tag listed must be present.")] = None,
                   collection: Annotated[str, Field(description="Exact collection name.")] = "",
                   limit: Annotated[int, Field(description="Most entries to return, newest first. Default 50.")] = 50) -> dict:
    """WHAT THE MACHINE ALREADY HAS: search the shared asset library before
    generating anything. Free, no side effects.

    Each entry carries its id (what library_import takes), name, kind,
    image dimensions, tags, collection, and the project it came from.
    `kinds` and `collections` count the whole library so an empty result
    still says what is there.
    Full notes: docs/tools.md#library_search
    """
    return _assetlib.search(query, kind=kind, tags=tags, collection=collection,
                            limit=limit)


@_tool
def library_import(ids: Annotated[list[str], Field(description="Entry ids from library_search (an exact filename works when only one entry has it).")],
                   dest: Annotated[str, Field(description="Directory under the engine project to land in. Default assets/library.")] = "assets/library",
                   godot_project: Annotated[str, Field(description="Directory holding project.godot (or the web root). Empty: the pinned project's own.")] = "",
                   overwrite: Annotated[bool, Field(description="Replace a file that exists and differs. Default False: reported and refused.")] = False,
                   rename: Annotated[str, Field(description="New filename when importing exactly one entry; the extension is kept if omitted.")] = "") -> dict:
    """BRING A LIBRARY ASSET INTO THIS GAME: copies the bytes (and any rig
    sidecar) under `dest`, tracks each landed file under its content hash
    so asset_verify covers it from birth, and returns its res:// path.

    A file already present and identical is reported, not rewritten; one
    that differs is refused unless `overwrite`. Godot reimports on next
    open; call godot_import_asset or engine_check to do it now.
    Full notes: docs/tools.md#library_import
    """
    root = _root()
    game = _game_dir(godot_project)
    result = _assetlib.import_entries(game, ids, dest=dest, overwrite=overwrite,
                                      rename=rename)
    tracked = []
    for row in result["imported"]:
        if not row.get("landed"):
            continue
        full = game / row["path"]
        _note_tool_write(root, str(full))
        try:
            tracked.append(_assets.track(root, full)["path"])
        except (ValueError, FileNotFoundError):
            # The engine project lives outside the bgate root: nothing to
            # register against. The copy still happened.
            pass
    result["tracked"] = tracked
    _log("library", f"imported {sum(1 for r in result['imported'] if r.get('landed'))} "
                    f"into {dest}" + ("" if result["ok"] else "; some refused"),
         ref=dest)
    return result
