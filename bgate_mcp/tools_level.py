"""Level generation MCP tools - the isometric-pipeline generation.

server.py held ~226 tools in 12k lines; the domains that never touch each
other live apart. The contract is unchanged: the shared plumbing (_tool,
_root, the gates) stays in server, this module imports it back, and server
star-imports this module at its BOTTOM - by then its globals all exist,
which is what makes the circular import legal - so server.<tool> still
answers for every caller and test.

This file is the isometric-pipeline branch's evolution of these tools
(routes, terraces, synth tilesets, reskin, iso blocks) merged onto the
manifest and gate work that landed on main while that branch was in
flight; the review fixes (walkable iso floors, honest terraces, the
wall_source contract) ride with it.
"""
from __future__ import annotations

from typing import Optional

from bgate_core import autotile as _autotile
from bgate_core import gameview as _gameview
from bgate_core import jump as _jumpmod
from bgate_core import levelgen as _levelgen
from bgate_core import props as _props
from bgate_core import propsheet as _propsheet
from bgate_core import sidescroll as _sidescroll
from bgate_core import spritekit as _spritekit
from bgate_core import tilemap as _tilemap

from bgate_mcp.server import (  # noqa: F401
    _Path, _archive_preview, _artdirection, _ase_master_for, _contained_path,
    _fail, _godot, _json, _log,
    _note_tool_write, _paid_gate, _re, _res_pair,
    _root, _run_tag, _scenewire, _tool,
)

# Level generation
# ---------------------------------------------------------------------------
_WALL_LAYOUTS = ("blob47", "grid16", "solid", "none")

#: The partition knobs `level_generate(tuning=...)` accepts, with their
#: defaults. Exactly `level_plan`'s own arguments minus width/height/seed/
#: layout, so a plan previewed there transfers verbatim - and named in ONE
#: place so the two tools cannot drift apart the way eight duplicated
#: parameter defaults quietly did.
_LEVEL_TUNING = {"min_leaf": 10, "min_room": 4, "margin": 1, "max_depth": 5,
                 "corridor_width": 2, "room_fill": 0.8, "rooms": 5,
                 "side_rooms": 1}

#: One character's physics, which is what `sidescroll_generate(jump=...)`
#: takes. `player_scene` reads the same four numbers off a real .tscn and
#: overrides these - a level built for a jump the character does not have is
#: unplayable in a way no screenshot shows.
_JUMP_SPEC = {"run": 9.0, "jump_speed": 18.0, "gravity": 40.0,
              "body_cells": 2}
_EMPTY_SCENE = ('[gd_scene load_steps=1 format=3]\n\n'
                '[node name="{root}" type="Node2D"]\n')


def _terrain(layout: str, source: int, atlas_x: int, atlas_y: int,
             columns: int, name: str):
    """One of the built-in terrain layouts, or a refusal naming the choices."""
    if layout == "solid":
        return _autotile.Terrain.solid(source, (atlas_x, atlas_y), name=name)
    if layout == "grid16":
        return _autotile.Terrain.grid16(source, columns=columns,
                                        origin=(atlas_x, atlas_y), name=name)
    if layout == "blob47":
        return _autotile.Terrain.blob47(source, columns=columns,
                                        origin=(atlas_x, atlas_y), name=name)
    raise ValueError(f"layout {layout!r} is not one of {_WALL_LAYOUTS}")


# _res_pair lives in server with the shared plumbing - the scene tools
# there and the tests' monkeypatch seam both need server's binding.

#: What each prop IS, in words a generator can draw. The CAMERA is not here —
#: it comes from the project's view, per mount, so a prop set generated for a
#: top-down game and one generated for a platformer differ by declaration
#: rather than by whoever wrote the prompt that day.
_PROP_SUBJECTS = {
    "torch": "an iron wall torch bracket holding a burning flame, angled to "
             "the RIGHT, the bracket ALONE with no wall behind it",
    "sconce": "a stone wall sconce holding a burning flame, the sconce ALONE "
              "with no wall behind it",
    "banner": "a long narrow cloth banner hanging down, the banner ALONE",
    "shelf": "a wooden wall shelf holding jars and clutter, the shelf ALONE",
    "cobweb": "a dusty grey cobweb",
    "barrel": "one wooden barrel with iron bands, standing upright so its "
              "circular lid faces the camera",
    "crate": "one wooden crate, standing so its square lid faces the camera",
    "rubble": "a small pile of broken stone rubble lying on the ground",
    "bones": "a scatter of old bones and a skull lying on the ground",
    "chest": "a closed wooden treasure chest with iron fittings, its lid "
             "facing the camera",
    "pillar": "one round carved stone pillar, its circular capital facing the "
              "camera",
    "crack": "a thin jagged black crack splitting stone, just the fracture "
             "line, thin and irregular",
    "stain": "a dark blackish-brown stain soaked into stone, muted and almost "
             "black, NOT orange and NOT red",
    "drain": "a round iron grate drain, its circular grate facing the camera",
    "altar": "a low rectangular carved stone altar slab, wide and low, its "
             "flat top facing the camera, NOT a cube",
    "well": "a low circular ring of stacked stone blocks forming a round "
            "opening",
    "statue": "a carved stone statue standing on a plinth",
    "door": "a heavy closed wooden door with iron bands in a stone frame",
    "arch": "an empty stone archway, the opening a flat dark shape",
    "stairs_up": "a short flight of stone steps rising, the treads reading as "
                 "stacked horizontal bars",
    "stairs_down": "a dark square opening in the ground with stone steps "
                   "descending into it",
}

_PROP_LOOK = (
    "16-bit SNES-era pixel art game sprite, crisp hard pixel edges, no "
    "anti-aliasing, flat solid pure black background, the object centred and "
    "filling the frame, no ground shadow, no scene, no floor tile, no border, "
    "no text, no logo"
)


@_tool
def prop_generate(name: str, style: str = "", types: str = "",
                  tile_px: int = 32, godot_project: str = "",
                  res_dir: str = "assets/tiles", install: bool = False,
                  max_cost_usd: float = 0.0) -> dict:
    """GENERATE THE PROPS FOR A LEVEL - art, cleanup, atlas and manifest.

    ONE CALL. You do not pack an atlas, you do not work out texture origins,
    you do not build an atlas string, and you do not have to remember which
    cleanup steps exist. Pass a name and the types you want; hand the manifest
    this returns to `level_generate(prop_manifest=...)` and the props are in
    the level.

    THAT IS WHY THIS TOOL EXISTS. The first prop set was made by a hand-written
    script, and the script silently dropped the palette conform and the
    defringe: 32-pixel sprites carrying 600 colours, two thirds of them off the
    pinned palette, with feathered edges. Nobody chose that. There was no
    pipeline for the decision to live in, the way `animation_generate` is the
    pipeline for a character cycle - so the steps were skipped by omission.

    THE CHAIN, all of it mandatory:
      * the project's VIEW decides the camera per prop mount (`game_view_get`)
      * `props.art_spec` decides the canvas, the ground anchor and how many
        DRAWINGS each type needs - a wall mount needs one per facing, because
        the engine mirrors a sprite but NOT its texture_origin
      * kie draws the sprite. RD is for motion and never for originating a look
      * the background is keyed client-side, the sprite is stepped down in
        halves to the contract box, its alpha is hardened to binary, and it is
        conformed to the pinned palette
      * the atlas packs on 2x2 slots so no spanning tile can overlap another,
        which Godot answers by silently dropping the tile
      * a MANIFEST is written beside the atlas with every coordinate, size,
        facing and animation

    `types` is a comma list, "" for the default set. `install=False` leaves
    everything in `.bgate_out/props/` for review.
    """
    try:
        from PIL import Image

        from bgate_adapters import kie as _kie
        from bgate_adapters import retrodiffusion as _rd

        _contained_path(godot_project, "godot_project")
        root = _Path(_root())
        refused = _paid_gate(str(root), "image", 0.0, "a prop-sheet generation")
        if refused:
            return refused
        view = _gameview.load(root)
        want = tuple(types.replace(",", " ").split()) or _props.DEFAULT_TYPES
        for n in want:
            _props.prop_type(n)
            if not _gameview.supports(view, _props.PROP_TYPES[n]["mount"]):
                return {"ok": False, "error": (
                    f"{n} mounts on {_props.PROP_TYPES[n]['mount']!r}, which "
                    f"means nothing in a {view} level")}

        palette = _artdirection.palette_pinned(str(root))
        if not palette:
            return {"ok": False, "error": (
                "no palette is pinned - call palette_pin first. A prop that "
                "skips the conform carries hundreds of off-palette colours "
                "and will not match the tileset it stands on.")}

        specs = [_props.art_spec(n, tile_px=tile_px, view=view) for n in want]
        drawings = sum(max(1, len(s["facings"])) for s in specs)
        estimate = drawings * 0.02
        # 0 means UNCAPPED (the default): ceilings are user-set - a tool that
        # shipped its own dollar guess kept refusing runs nobody asked it to
        # bound.
        if max_cost_usd and estimate > max_cost_usd:
            return {"ok": False, "error": (
                f"{drawings} drawings is about ${estimate:.2f}, over the "
                f"${max_cost_usd:.2f} ceiling you set - raise max_cost_usd or "
                "ask for fewer types")}

        out_dir = root / ".bgate_out" / "props" / name
        out_dir.mkdir(parents=True, exist_ok=True)
        images: dict = {}
        reports: dict = {}
        spent = 0.0
        for spec in specs:
            subject = _PROP_SUBJECTS.get(spec["type"], spec["type"])
            prompt = f"{subject}. {spec['camera']} {style or _PROP_LOOK}"
            raw = out_dir / f"{spec['type']}_raw.png"
            size = ("1024x1024" if spec["cells"][0] == spec["cells"][1]
                    else "1024x2048" if spec["cells"][1] > spec["cells"][0]
                    else "2048x1024")
            got = _kie.generate_image(prompt, str(raw), model="nano-banana-2",
                                      size=size, task_kind="prop",
                                      root=str(root))
            if not got.get("ok"):
                return {"ok": False, "error": (
                    f"{spec['type']} failed to generate: "
                    f"{str(got.get('error'))[:200]}")}
            spent += float(got.get("estimated_usd") or 0.02)
            keyed = _rd.key_background(Image.open(raw))
            img = keyed.get("image") if isinstance(keyed, dict) else keyed
            fitted, rep = _propsheet.conform(img, size=spec["cell_px"],
                                             art_size=spec["art_px"],
                                             palette=palette)
            dest = out_dir / f"{spec['type']}.png"
            fitted.save(dest)
            _spritekit.lock_palette(dest, palette, out_path=dest)
            done = Image.open(dest)
            images[spec["type"]] = done
            reports[spec["type"]] = {**rep,
                                     **_propsheet.measure(done, palette)}

        packed = _propsheet.pack(images, want, tile_px=tile_px, view=view)
        atlas_png = out_dir / f"{name}_props.png"
        _propsheet.write(packed["image"], atlas_png)

        # mount_origins wants a plan; one stub prop per drawing is enough to
        # ask "what offset does this facing need", and it keeps the origin
        # rule in ONE place instead of restating it here
        stub = {"props": [{"type": n, "mount": _props.PROP_TYPES[n]["mount"],
                           "faces": f, "x": 0, "y": 0}
                          for n, f in _propsheet.slots_for(want, view=view)]}
        origins = _props.mount_origins(stub, packed["atlas"],
                                       tile_size=(tile_px, tile_px))
        manifest = {
            "name": name, "view": view, "tile_px": tile_px,
            "texture": f"res://{res_dir}/{name}_props.png",
            "types": list(want),
            "atlas": {k: ({f: list(c) for f, c in v.items()}
                          if isinstance(v, dict) else list(v))
                      for k, v in packed["atlas"].items()},
            "tiles": [list(c) for c in packed["tiles"]],
            "sizes": {f"{k[0]},{k[1]}": list(v)
                      for k, v in packed["sizes"].items()},
            "origins": {f"{k[0]},{k[1]}": list(v) for k, v in origins.items()},
            "animation": {f"{k[0]},{k[1]}": v for k, v in
                          _propsheet.animation_frames(want, packed["atlas"],
                                                      view=view).items()},
            "spec": packed["spec"],
        }
        man_path = out_dir / f"{name}_props.json"
        man_path.write_text(_json.dumps(manifest, indent=1), encoding="utf-8")

        # EVERY AI-GENERATED SHEET GETS AN ASEPRITE MASTER — house rule, not
        # a convenience. The props atlas was the one generated sheet that
        # skipped the cleanup path; a human fixing one bad prop edits the
        # master and aseprite_export brings it back, same as characters.
        ase = _ase_master_for(str(atlas_png), (tile_px, tile_px), {}, None,
                              fps=8.0)

        installed = None
        if install and godot_project:
            import shutil as _shutil

            dest_dir = _Path(godot_project) / res_dir
            dest_dir.mkdir(parents=True, exist_ok=True)
            _shutil.copy(atlas_png, dest_dir / atlas_png.name)
            _shutil.copy(man_path, dest_dir / man_path.name)
            # a freshly copied PNG does not exist to Godot until --import runs
            _godot.check_project(str(godot_project))
            installed = f"res://{res_dir}/{man_path.name}"

        dirty = sorted(k for k, v in reports.items()
                       if v.get("off_palette", 0) > _propsheet.OFF_PALETTE_MAX
                       or v.get("feathered", 0))
        # a prop touching its own border is oversized or brought a background
        bordered = sorted(k for k, v in reports.items()
                          if v.get("border_fill", 0) > _propsheet.BORDER_MAX
                          and _props.prop_type(k).get("footprint", 0.75) < 1.0)
        _log("props", f"generated {len(want)} prop types for {name} ({view})",
             ref=name)
        return {"ok": True, "name": name, "view": view, "types": list(want),
                "drawings": packed["slots"], "atlas": str(atlas_png),
                **({"aseprite": ase} if ase is not None else {}),
                "manifest": str(man_path),
                "prop_manifest": installed or str(man_path),
                "installed": installed, "spent_usd": round(spent, 3),
                "reports": reports,
                "findings": {**({"unconformed": dirty} if dirty else {}),
                             **({"touches_border": bordered} if bordered
                                else {})},
                "next": ("level_generate(prop_manifest=<manifest>) - the atlas "
                         "coordinates, sizes, origins and animation all come "
                         "from that file; you do not pass them")}
    except Exception as exc:
        return _fail(exc)



# @export var speed := 220.0  |  @export var gravity: float = 980.0
_EXPORT_VAR_RE = _re.compile(
    r"^@export\s+var\s+(\w+)\s*(?::\s*\w+\s*=|:=|=)\s*(-?\d+(?:\.\d+)?)\s*$",
    _re.MULTILINE)

_JUMP_TUNABLES = ("speed", "jump_velocity", "gravity")


def _player_jump(godot_dir: _Path, scene_disk: _Path) -> dict:
    """The jump tunables a player scene actually carries, in pixels.

    Two layers, same precedence the engine uses: the script's ``@export``
    defaults, overridden by any value the scene file sets on its root node.
    Returns ``{speed, jump_velocity, gravity, fall_multiplier, script}`` or
    raises naming exactly what is missing — a guessed default here would
    rebuild the drift this function exists to close.
    """
    text = scene_disk.read_text(encoding="utf-8", errors="replace")
    parsed = _scenewire.parse(text)
    if not parsed["nodes"]:
        raise ValueError(f"{scene_disk.name} has no nodes")

    vals: dict = {}
    script_rel = ""
    for ext in parsed["ext"]:
        if str(ext.get("path", "")).endswith(".gd"):
            script_rel = ext["path"][len("res://"):]
            script_disk = (godot_dir / script_rel).resolve()
            if script_disk.is_file():
                body = script_disk.read_text(encoding="utf-8",
                                             errors="replace")
                for name, num in _EXPORT_VAR_RE.findall(body):
                    vals[name] = float(num)
            break

    root_props = _scenewire.properties(text, parsed, parsed["nodes"][0])
    for key in (*_JUMP_TUNABLES, "fall_multiplier"):
        raw = root_props.get(key)
        if raw is not None:
            try:
                vals[key] = float(str(raw))
            except ValueError:
                raise ValueError(
                    f"{scene_disk.name} sets {key} = {raw!r}, which is not a "
                    "number this can convert")

    missing = [k for k in _JUMP_TUNABLES if k not in vals]
    if missing:
        raise ValueError(
            f"{scene_disk.name} does not declare {missing} — looked at the "
            f"scene's root node and {script_rel or 'no attached script'}. The "
            "level is built FROM these numbers; without them there is nothing "
            "to build for. templates/2d's player.gd exports all three.")
    vals.setdefault("fall_multiplier", 1.0)
    vals["script"] = script_rel
    return vals


@_tool
def sidescroll_generate(godot_project: str, scene: str, tileset: str,
                        length: int = 160, height: int = 16, seed: int = 0,
                        jump: Optional[dict] = None,
                        difficulty: float = 0.5, segments: str = "",
                        prop_manifest: str = "", prop_spacing: int = 9,
                        player_scene: str = "",
                        parent: str = ".", names: Optional[dict] = None,
                        create: bool = False, dry_run: bool = False) -> dict:
    """GENERATE A SIDE-SCROLLING LEVEL and write it into a scene.

    The platformer counterpart of `level_generate`, and a separate tool because
    it is a separate problem. `level_generate` partitions a SPACE into rooms and
    guarantees the floor is one connected region. Under gravity that guarantee
    is meaningless — you cannot walk upward — so this builds a SEQUENCE of
    segments left to right and guarantees something else entirely: that the
    goal can be REACHED, by a character with this exact jump.

    THE JUMP IS AN INPUT, not a detail. `jump` is one character's physics -
    {"run": ..., "jump_speed": ..., "gravity": ..., "body_cells": ...}, the
    first three in CELLS PER SECOND - and every segment sizes itself from what
    they allow: a pit is never wider than this character clears, a pipe never
    taller than it rises. An unclearable gap is unrepresentable rather than
    generated and rejected. One dict rather than four loose floats because
    they describe ONE thing, and four separate arguments invite three of them
    being right. An unknown key is refused by name rather than ignored.

    `player_scene` IS THE WAY TO PASS THEM. Point it at the player's .tscn and
    the tunables are read from the scene itself — its script's @export
    defaults, overridden by anything the scene sets — converted to cells by
    the tileset's own tile size, and the player is INSTANCED AT SPAWN in the
    written scene. `jump` is then ignored, because two sources of the same
    number is the drift this parameter closes: a level built for one jump and
    played with another is the failure the whole parameterisation exists to
    prevent. `fall_multiplier` is honoured by modelling with the fall gravity,
    so the error runs only in the safe direction. Without `player_scene` the
    `jump` numbers are trusted as given — then it is on you to keep the player
    scene agreeing with them.

    IT REFUSES AN UNPLAYABLE LEVEL rather than reporting one. The checks are
    `reachable` (the goal is in the flood fill of jump arcs from spawn),
    `clearance` (the body fits where it must pass), `softlock` (nowhere you can
    land and never leave) and `stranded` (no platform outside its own jump).
    A finding here is a bug to report, not a difficulty dial.

    THE TILESET DESCRIBES ITSELF. Where the platform tiles live comes off the
    `<tileset>.tiles.json` sidecar - written by `tileset_generate` for a set it
    made, or by `tileset_describe` once for a hand-built one. This tool takes
    no atlas coordinates; without that file it refuses rather than guessing.

    `segments` is a comma list from flat, pit, stair, hop, blocks, pipe — "" for
    all of them. `prop_manifest` is what `prop_generate` wrote; pass it and the
    props are placed and drawn, and the types come from the manifest too.
    `names` renames the layer nodes, {"solid": ..., "props": ...}.

    Returns the ASCII map, which is the cheapest way to see a level before
    anything is spent on art for it.
    """
    try:
        # THE JUMP, BEHIND ONE DOOR. `run`, `jump_speed`, `gravity` and
        # `body_cells` are one physical description of one character, and four
        # loose floats invite three of them being right. Unknown keys are
        # refused by name: a silently-ignored `jump_height` is a character
        # model that did nothing and reported success.
        jump = dict(jump or {})
        unknown = sorted(set(jump) - set(_JUMP_SPEC))
        if unknown:
            return {"ok": False, "error": (
                f"jump has no key(s) {unknown} - it takes "
                f"{sorted(_JUMP_SPEC)}. Those are the character's own "
                "numbers; player_scene reads them off a real scene instead.")}
        spec_in = {**_JUMP_SPEC, **jump}
        run = float(spec_in["run"]); jump_speed = float(spec_in["jump_speed"])
        gravity = float(spec_in["gravity"])
        body_cells = int(spec_in["body_cells"])

        names = dict(names or {})
        solid_name = str(names.get("solid") or "Solid")
        prop_name = str(names.get("props") or "Props")

        root = _root()
        view = _gameview.load(root)
        if view != "side_scroller":
            return {"ok": False, "error": (
                f"this project's view is {view!r}. A side-scrolling level in a "
                f"{view} game is the wrong geometry, not a style choice — set "
                "it with game_view_set if that is what you meant.")}

        scene_disk, scene_res = _res_pair(godot_project, scene, ".tscn")
        tiles_disk, tiles_res = _res_pair(godot_project, tileset, ".tres")
        if not tiles_disk.is_file():
            return {"ok": False, "error": (
                f"no tileset at {tiles_res} - generate or import it first; a "
                "level cannot pick tiles from nothing")}
        parsed_set = _tilemap.parse_tileset(
            tiles_disk.read_text(encoding="utf-8", errors="replace"))
        if parsed_set["shape"] != _tilemap.SQUARE:
            return {"ok": False, "error": (
                f"{tiles_res} is not a square-tile set (shape "
                f"{parsed_set['shape']}). A platformer's cells are the squares "
                "the jump arithmetic runs on — this tileset belongs to a "
                "different projection.")}

        jump_source = "arguments"
        player_report: dict = {}
        if player_scene:
            player_disk, player_res = _res_pair(godot_project, player_scene,
                                                ".tscn")
            if not player_disk.is_file():
                return {"ok": False, "error": (
                    f"no player scene at {player_res} — the jump is read from "
                    "the player, so the player has to exist first. "
                    "godot_scaffold's 2d template ships one.")}
            tw, th = parsed_set["tile_size"]
            if tw != th:
                return {"ok": False, "error": (
                    f"{tiles_res} has {tw}x{th} tiles. The jump arithmetic "
                    "runs on square cells; converting a player's pixels "
                    "through a rectangular cell would be a guess in one axis.")}
            tuned = _player_jump(_Path(godot_project), player_disk)
            spec = _jumpmod.from_pixels(
                speed=tuned["speed"], jump_velocity=tuned["jump_velocity"],
                gravity=tuned["gravity"],
                fall_multiplier=tuned["fall_multiplier"],
                tile_px=tw, body=(1, max(1, body_cells)))
            jump_source = "player_scene"
            player_report = {"scene": player_res, "script": tuned["script"],
                             "pixels": {k: tuned[k] for k in
                                        (*_JUMP_TUNABLES, "fall_multiplier")}}
        else:
            spec = _jumpmod.JumpSpec(run=run, jump_speed=jump_speed,
                                     gravity=gravity,
                                     body=(1, max(1, body_cells)))
        kinds = tuple(segments.replace(",", " ").split()) or None
        level = _sidescroll.plan(length, height, seed=seed, spec=spec,
                                 difficulty=difficulty, kinds=kinds)
        verdict = _sidescroll.check(level, spec)
        if not verdict["ok"]:
            return {"ok": False, "error": (
                "this level cannot be played by the character it was built "
                "for — that is a generator bug, not a difficulty setting"),
                "findings": verdict["findings"],
                "ascii": _sidescroll.ascii_map(level, width=min(length, 120))}

        fresh = not scene_disk.is_file()
        if fresh and not create:
            return {"ok": False, "error": (
                f"no scene at {scene_res}. Pass create=true to start a new "
                "one, or point at an existing scene.")}
        text = (_EMPTY_SCENE.format(root=scene_disk.stem.title() or "Level")
                if fresh else
                scene_disk.read_text(encoding="utf-8", errors="replace"))

        solid = {tuple(c) for c in level["solid"]}
        # Same manifest rule as level_generate: a tileset_generate set
        # describes its own layout, so the solid_* arguments are for
        # hand-built sheets and any non-default value wins outright.
        manifest = _tileset_manifest(tiles_disk)
        if manifest is None:
            return {"ok": False, "error": (
                f"{tiles_res} has no {tiles_disk.stem}.tiles.json beside it, "
                "so nothing here knows where its platform tiles live. "
                "tileset_generate writes that file for a set it made itself; "
                "for a hand-built or imported sheet call tileset_describe "
                "once. This tool no longer takes atlas coordinates.")}
        manifest_used = True
        terrain = _manifest_floor(manifest, solid_name)
        cells = _autotile.resolve(sorted(solid), terrain,
                                  region=(0, 0, length, height))
        varied = _scatter_variants(cells, tiles_disk)
        layers = [{"name": solid_name, "terrain": solid_name,
                   "cells": cells, "unmapped": {}}]

        prop_report: dict = {}
        if prop_manifest:
            man = _read_prop_manifest(root, prop_manifest)
            prop_source = _manifest_source(tiles_disk, man)
            parsed_set = _tilemap.parse_tileset(
                tiles_disk.read_text(encoding="utf-8", errors="replace"))
            # The manifest names its own types; `prop_types` was a second,
            # retyped opinion about a list already sitting on disk.
            want = tuple(man["types"])
            # GROUND MOUNTS ONLY. A side view has no floor plane to stand a
            # prop on except the surface itself, and `jump.surfaces` already
            # knows which cells those are — the same function the reachability
            # gate uses, so a prop can never sit where the player cannot.
            stand = sorted(_jumpmod.surfaces(solid, body=spec.body))
            import random as _random

            rng = _random.Random(seed)
            placed, used = [], []
            for cell in stand:
                if any(abs(cell[0] - u) < max(2, prop_spacing) for u in used):
                    continue
                name = want[rng.randrange(len(want))]
                spot = man["atlas"].get(name)
                if isinstance(spot, dict):
                    spot = next(iter(spot.values()))
                if not spot:
                    continue
                placed.append({"x": cell[0], "y": cell[1],
                               "source": int(prop_source),
                               "ax": int(spot[0]), "ay": int(spot[1]),
                               "alt": 0})
                used.append(cell[0])
            if placed:
                layers.append({"name": prop_name, "terrain": prop_name,
                               "cells": placed, "unmapped": {}})
            prop_report = {"placed": len(placed), "types": list(want),
                           "manifest": prop_manifest}

        absent = {}
        for layer in layers:
            want_at = {(c["source"], c["ax"], c["ay"]) for c in layer["cells"]}
            gaps = sorted((ax, ay) for src, ax, ay in want_at
                          if (ax, ay) not in set(map(tuple,
                              parsed_set["sources"][src]["tiles"])))
            if gaps:
                absent[layer["name"]] = [list(g) for g in gaps]
        if absent:
            return {"ok": False, "error": (
                f"{tiles_res} does not define these atlas tiles: {absent}. A "
                "cell pointing at an undefined tile draws nothing and says "
                "nothing.")}

        wired = _scenewire.wire_tilemap(text, tiles_res, layers,
                                        parent=parent,
                                        owns=[solid_name, prop_name])

        tw, th = parsed_set["tile_size"]
        spawn_px = [round((level["spawn"][0] + 0.5) * tw, 1),
                    round((level["spawn"][1] + 0.5) * th, 1)]
        goal_px = [round((level["goal"][0] + 0.5) * tw, 1),
                   round((level["goal"][1] + 0.5) * th, 1)]
        if player_scene:
            # The player goes INTO the scene, at spawn — placed here rather
            # than left as a coordinate in the result, because a returned
            # number is a step someone forgets and a placed node is not.
            # Re-running MOVES the existing instance instead of stacking a
            # second player, same discipline wire_tilemap holds for layers.
            ptxt = wired["text"]
            pparsed = _scenewire.parse(ptxt)
            want = _scenewire._default_name(player_res)
            if any(n["name"] == want and n.get("instance")
                   for n in pparsed["nodes"]):
                node_name = want if parent == "." else f"{parent}/{want}"
                placed = "moved"
            else:
                w = _scenewire.wire(ptxt, player_res, parent=parent,
                                    node_name=want)
                ptxt, node_name = w["text"], w["node"]
                node_name = (node_name if parent == "."
                             else f"{parent}/{node_name}")
                placed = "added"
            ptxt = _scenewire.set_property(
                ptxt, node_name, "position",
                f"Vector2({spawn_px[0]}, {spawn_px[1]})")["text"]
            wired["text"] = ptxt
            player_report.update({"node": node_name, "placed": placed,
                                  "position": spawn_px})

        if dry_run:
            written = {"written": False}
        elif fresh:
            scene_disk.parent.mkdir(parents=True, exist_ok=True)
            scene_disk.write_text(wired["text"], encoding="utf-8")
            # The evidence gate reads the harness's writelog, and a fresh
            # scene bypasses scenewire.apply (which notes its own writes).
            _note_tool_write(_root(), scene_disk)
            written = {"written": True, "created": True}
        else:
            written = _scenewire.apply(scene_disk, wired["text"], root=_root())

        if not dry_run:
            _log("level", f"side-scroller {length}x{height} seed {seed} "
                          f"into {scene_res}", ref=scene_res)
        return {"ok": True, "scene": scene_res, "tileset": tiles_res,
                "manifest_used": manifest_used,
                "seed": seed, "size": [length, height],
                "spawn": level["spawn"], "goal": level["goal"],
                "spawn_px": spawn_px, "goal_px": goal_px,
                "segments": len(level["segments"]),
                "tile_variants": varied,
                "jump": {**level["limits"], "source": jump_source},
                "player": player_report,
                "playable": verdict,
                "props": prop_report,
                "layers": wired["layers"], "summary": wired["summary"],
                "written": written.get("written", False),
                "created": bool(written.get("created")),
                "ascii": _sidescroll.ascii_map(level, width=min(length, 120)),
                "next": (("the player is in the scene with the jump the level "
                          "was checked against — godot_run it")
                         if player_scene else
                         ("give your player scene the SAME run/jump_speed/"
                          "gravity — or pass player_scene= and both halves "
                          "of that sentence happen for you"))}
    except Exception as exc:
        return _fail(exc)

@_tool
def game_view_get() -> dict:
    """WHICH 2D VIEW THIS GAME IS, and everything that follows from it.

    Read this BEFORE generating any level art or any prop. The view is not a
    style preference, it decides what "correct" means: a barrel showing its lid
    and a sliver of side is right for a top-down game and wrong for a
    platformer, and a barrel showing two side faces is right for an isometric
    game and wrong for both of the others.

    It was not declared anywhere until now, and the cost was measured: a prop
    batch prompted with "a high 3/4 top-down game view" came back ISOMETRIC —
    to an image model "three-quarter" means the standard product render — and
    every prop showed two side faces, to stand on a floor tileset drawn flat
    top-down. The prompt was the proximate cause. The real one was that the
    view lived in a prompt instead of in the project, so each agent re-derived
    it and drifted.

    The result carries the camera clause per prop mount (`cameras`), the tile
    geometry, which prop mounts exist at all in this view, and how the level
    generator checks playability. USE `cameras`, do not paraphrase it: the
    clauses forbid the wrong reading BY NAME, because a clause that only says
    what it wants inherits the model's default for everything it forgot to
    forbid.
    """
    try:
        root = _root()
        view = _gameview.load(root)
        out = _gameview.describe(view)
        out["cameras"] = {m: _gameview.camera_clause(view, m)
                          for m in _gameview.mounts(view)}
        out["views_available"] = list(_gameview.VIEWS)
        out["ok"] = True
        return out
    except Exception as exc:
        return _fail(exc)


@_tool
def game_view_set(view: str) -> dict:
    """Declare which 2D view this game is: top_down, side_scroller, isometric.

    DIRECTOR CALL, and it should be made before any level or prop art exists —
    changing it later invalidates every prop that was drawn to the old camera,
    because a sprite cannot be re-projected after the fact.

    Accepts the obvious aliases (platformer, iso, top-down) and refuses
    anything else by name rather than guessing.
    """
    try:
        root = _root()
        spec = _gameview.save(root, view)
        _log("view", f"game view declared: {spec['view']}", ref=spec["view"])
        out = _gameview.describe(spec["view"])
        out["ok"] = True
        out["warning"] = ("Art already drawn to a different camera cannot be "
                          "re-projected — it has to be regenerated.")
        return out
    except Exception as exc:
        return _fail(exc)

@_tool
def tileset_synth(name: str, floors: str, walls: str = "",
                  tile_px: int = 64, wall_lift: int = 68,
                  godot_project: str = "", res_dir: str = "assets/tiles",
                  install: bool = False, collide: bool = True) -> dict:
    """BUILD AN ISOMETRIC TILESET FROM THE PALETTE, with no image model at all.

    The counterpart to `tileset_generate`, and the right tool for the
    surfaces a building is mostly made of. A generated texture carries
    structure at roughly tile scale, so cropping one onto a diamond grid
    lays a visible lattice of motifs across the floor, and mirroring it to
    hide the diamond seams trades that lattice for symmetry. The tiles a
    real project ships are nearly featureless — near-black, a faint grain,
    at most one soft panel seam — and that is arithmetic's job: per-pixel
    noise cannot repeat, every value is a palette entry by construction, a
    variant is a different SEED rather than a different crop, and the whole
    set costs nothing and arrives in a second.

    Reach for `tileset_generate` when a material's features are meant to be
    read individually — terrazzo chips, a checkerboard lino, a poster wall.
    Reach for this for carpet, concrete, vinyl, asphalt and every other
    surface whose job is to be quiet.

    `floors` and `walls` are semicolon lists of
    ``name=#rrggbb[,grain][,seam][,speck]`` — one atlas source each, so a
    level generator can put a different surface in every room.
    """
    try:
        from PIL import Image as _Img

        from bgate_core import tilemap as _tilemap
        from bgate_core import tilemask as _tilemask

        root = _Path(_root())
        view = _gameview.load(root)
        if view != "isometric":
            return {"ok": False, "error": (
                f"this project's view is {view!r}; tileset_synth builds the "
                "diamond set. Use tileset_generate for a square view.")}
        tw, th = int(tile_px), int(tile_px) // 2
        lift = int(wall_lift) or th
        pal = _artdirection.palette_pinned(str(root)) or None
        wanted = list(range(16))
        full = (_tilemask.BIT_N | _tilemask.BIT_E |
                _tilemask.BIT_S | _tilemask.BIT_W)
        out_dir = root / ".bgate_out" / "tiles"
        out_dir.mkdir(parents=True, exist_ok=True)

        def _spec(text):
            got = []
            for chunk in [c for c in str(text).split(";") if c.strip()]:
                key, _, rest = chunk.partition("=")
                bits = [b.strip() for b in rest.split(",")]
                got.append({
                    "name": key.strip(),
                    "base": bits[0] if bits and bits[0] else "#242424",
                    "grain": float(bits[1]) if len(bits) > 1 and bits[1] else 0.22,
                    "seam": float(bits[2]) if len(bits) > 2 and bits[2] else 0.0,
                    "speck": float(bits[3]) if len(bits) > 3 and bits[3] else 0.0,
                })
            return got

        sources, meta_floor, meta_wall = [], {}, {}
        sid = 0
        for spec in _spec(floors):
            # THREE VARIANTS, THREE SEEDS. The repeat a floor shows is the
            # tile's own content coming back; different noise per variant
            # means there is no motif to recognise in the first place.
            tiles = [_tilemask.synth_material(
                spec["base"], tile_size=(tw, th), palette=pal,
                grain=spec["grain"], seam=spec["seam"], speck=spec["speck"],
                seed=abs(hash((name, spec["name"], v))) % 10_000)
                for v in range(4)]
            built = _tilemask.diamond_tiles(tiles[0], tiles[0], wanted,
                                            tile_size=(tw, th))
            if not built.get("ok"):
                return {"ok": False, "error": built.get("reason")}
            sheet, table = built["image"], built["table"]
            cols = max(1, sheet.width // tw)
            need = (len(wanted) + 3 + cols - 1) // cols
            if need * th > sheet.height:
                grown = _Img.new("RGBA", (sheet.width, need * th), (0, 0, 0, 0))
                grown.paste(sheet, (0, 0))
                sheet = grown
            variant_at = []
            for j, vt in enumerate(tiles[1:]):
                vd = _tilemask.crop_tile(
                    _tilemask.diamond_tiles(vt, vt, [full],
                                            tile_size=(tw, th))["image"],
                    (0, 0), (tw, th))
                tx, ty = (len(wanted) + j) % cols, (len(wanted) + j) // cols
                sheet.paste(vd, (tx * tw, ty * th))
                variant_at.append([tx, ty])
            png = out_dir / f"{name}_{spec['name']}.png"
            sheet.save(png)
            sources.append({"id": sid, "path": str(png),
                            "texture": f"res://{res_dir.strip('/')}/{png.name}",
                            "tiles": sorted(table.values()) + [tuple(v) for v
                                                               in variant_at]})
            meta_floor[spec["name"]] = {"source": sid,
                                        "interior": list(table[15]),
                                        "variants": variant_at,
                                        "table": {str(m): list(c) for m, c
                                                  in sorted(table.items())}}
            sid += 1

        for spec in _spec(walls):
            mat = _tilemask.synth_material(
                spec["base"], tile_size=(tw, th), palette=pal,
                grain=spec["grain"], seam=0.0, speck=spec["speck"],
                seed=abs(hash((name, spec["name"]))) % 10_000)
            panels = {}
            imgs = []
            for mk in range(16):
                got = _tilemask.iso_panel(mat, mk, tile_size=(tw, th),
                                          lift=lift)
                if not got.get("ok"):
                    return {"ok": False, "error": got.get("reason")}
                imgs.append(got["image"])
            bw, bh = tw, th + lift
            strip = _Img.new("RGBA", (bw * 16, bh), (0, 0, 0, 0))
            for mk, im in enumerate(imgs):
                strip.paste(im, (mk * bw, 0))
                panels[f"panel{mk}"] = [mk, 0]
            png = out_dir / f"{name}_{spec['name']}_wall.png"
            strip.save(png)
            sources.append({
                "id": sid, "path": str(png),
                "texture": f"res://{res_dir.strip('/')}/{png.name}",
                "tiles": [(mk, 0) for mk in range(16)],
                "region": (bw, bh),
                "origins": {(mk, 0): (0, lift // 2) for mk in range(16)},
                "collision": ({(mk, 0): [_tilemask.diamond_polygon((tw, th))]
                               for mk in range(16)} if collide else {})})
            meta_wall[spec["name"]] = {"source": sid, "blocks": panels,
                                       "lift": lift}
            sid += 1

        if not sources:
            return {"ok": False, "error": "no materials given"}
        tres = _tilemap.write_tileset(
            [{k: v for k, v in s.items() if k != "path"} for s in sources],
            tile_size=(tw, th), shape=_tilemap.ISOMETRIC,
            layout=_tilemap.DIAMOND_DOWN, physics=bool(collide))
        tres_path = out_dir / f"{name}.tres"
        tres_path.write_text(tres, encoding="utf-8")
        first = next(iter(meta_floor.values()), {})
        side = {"interior": first.get("interior", [3, 3]),
                "variants": first.get("variants", []),
                "materials": meta_floor, "wall_sets": meta_wall,
                "lift": lift}
        if meta_wall:
            first_wall = next(iter(meta_wall.values()))
            side["blocks"] = first_wall["blocks"]
            # THE BLOCKS NAME THEIR OWN SOURCE. Sources here are sequential,
            # floors first, so a two-floor set puts its wall panels at
            # source 2 — and level_generate/level_reskin read
            # side["wall_source"] with a default of 1, which silently painted
            # walls and terraces from the second floor material at panel
            # coordinates that happened to exist there.
            side["wall_source"] = int(first_wall["source"])
        side_path = out_dir / f"{name}.tiles.json"
        side_path.write_text(_json.dumps(side, indent=1), encoding="utf-8")

        result = {"ok": True, "name": name, "tileset": str(tres_path),
                  "tile_size": [tw, th], "lift": lift,
                  "floors": {k: v["source"] for k, v in meta_floor.items()},
                  "walls": {k: v["source"] for k, v in meta_wall.items()},
                  "palette": len(pal or []), "spend": {"usd": 0.0}}
        if install and godot_project:
            import shutil as _sh
            proj = _Path(godot_project)
            dest = proj / res_dir.strip("/")
            dest.mkdir(parents=True, exist_ok=True)
            for s in sources:
                _sh.copyfile(s["path"], dest / _Path(s["path"]).name)
            _sh.copyfile(side_path, dest / side_path.name)
            (dest / f"{name}.tres").write_text(tres, encoding="utf-8")
            result["import"] = _godot.check_project(str(proj))
            result["engine"] = _godot.inspect_tileset(
                str(proj), f"res://{res_dir.strip('/')}/{name}.tres")
            result["installed"] = str(dest / f"{name}.tres")
        _log("art", f"synth tileset {name}: {len(meta_floor)} floors, "
                    f"{len(meta_wall)} walls", ref=str(tres_path))
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def level_reskin(godot_project: str, scene: str, tileset: str,
                 out_scene: str = "", floor_layer: str = "",
                 wall_layer: str = "Walls", sunken: str = "",
                 doors: str = "", material_map: str = "",
                 wall_map: str = "", keep_art: bool = True,
                 parent: str = ".",
                 dry_run: bool = False) -> dict:
    """RE-BUILD AN EXISTING LEVEL'S LAYOUT against a different tileset.

    The layout is the expensive part and the art is not. A floor somebody
    designed by hand — where the rooms are, which cells are corridor, where
    the walls run — is worth keeping when the tile set under it changes, and
    re-drawing it by hand in the editor is how a re-skin never happens.

    So this reads the CELL SETS out of a scene's TileMapLayers and emits them
    again against a new tileset: the floor re-autotiled from its own shape, so
    every cell gets the edge its neighbours imply rather than the flat tile it
    had, and the walls placed as whatever the new set uses for a wall — in an
    isometric project that is the raised BLOCK, which is what turns a flat
    wall layer into a room you can see the inside of.

    It writes a NEW scene by default (`out_scene`, or `<scene>_reskin.tscn`).
    The source scene is never modified: a level carries props, scripts,
    spawns and quest wiring that this tool knows nothing about, and quietly
    rewriting the layers under them is not a re-skin, it is a demolition.

    `sunken` is "x,y,w,h" — a region that stays on the base plane while
    everything else rises one level, which is how you get a BASEMENT out of a
    generator that only knows how to raise things. The rim of the drop is
    ramped wherever the two heights actually touch, and because the walls of
    a designed floor already separate its rooms, the only places they touch
    are its doorways. Reachability is then checked the same way the
    side-scroller checks its jumps: if a walker cannot get from the high
    ground into the hole, that is refused rather than rendered.

    `doors` is "x,y x,y ..." — cells the WALL layer holds that are actually
    openings. A designed floor does not have to leave gaps in its wall layer
    to have doorways: downsizing's tutorial floor draws a door tile inside
    the wall run and records the opening in its level data, which is a scene
    reader's blind spot. Without them the walkable set comes apart into one
    component per room — measured here, eighteen of them — and any question
    about reaching anything is answered wrongly rather than refused. Given
    them, the cells stop being walls and become floor, which is what a
    doorway looks like when the wall is a solid block.

    Returns the cell counts it moved and the masks the new set could not
    answer, which is the list to hand an artist.
    """
    try:
        import re as _re2

        root = _Path(_root())
        view = _gameview.load(root)
        iso = view == "isometric"
        src_disk, src_res = _res_pair(godot_project, scene, ".tscn")
        tiles_disk, tiles_res = _res_pair(godot_project, tileset, ".tres")
        if not src_disk.is_file():
            return {"ok": False, "error": f"no scene at {src_res}"}
        if not tiles_disk.is_file():
            return {"ok": False, "error": f"no tileset at {tiles_res}"}
        parsed_set = _tilemap.parse_tileset(
            tiles_disk.read_text(encoding="utf-8", errors="replace"))
        want_shape = _tilemap.ISOMETRIC if iso else _tilemap.SQUARE
        if parsed_set["shape"] != want_shape:
            return {"ok": False, "error": (
                f"{tiles_res} has tile shape {parsed_set['shape']} and this "
                f"project's view is {view!r} — the re-skin would draw the "
                "layout in the wrong projection")}

        text = src_disk.read_text(encoding="utf-8", errors="replace")
        found, found_packed = {}, {}
        for name, body in _re2.findall(
                r'\[node name="([^"]+)" type="TileMapLayer"[^\]]*\]'
                r'((?:(?!\n\[node).)*)', text, _re2.S):
            hit = _re2.search(r'tile_map_data = PackedByteArray\("([^"]*)"\)',
                              body)
            if hit:
                found_packed[name] = hit.group(1)
                found[name] = {(c["x"], c["y"])
                               for c in _tilemap.decode_cells(hit.group(1))}
        if not found:
            return {"ok": False, "error": (
                f"{src_res} has no TileMapLayer carrying cells — there is no "
                "layout in it to re-skin")}

        # WHICH LAYER IS THE FLOOR: named, or the biggest one that is not the
        # wall layer. A guess is fine here and a wrong guess is visible
        # immediately, which is not true of most guesses in this pipeline.
        floor_name = floor_layer or next(
            (n for n, c in sorted(found.items(), key=lambda kv: -len(kv[1]))
             if n != wall_layer), "")
        if floor_name not in found:
            return {"ok": False, "error": (
                f"no layer named {floor_name!r} in {src_res} — it has "
                f"{sorted(found)}")}
        floor_cells = found[floor_name]
        wall_cells = found.get(wall_layer, set())
        door_cells = set()
        for pair in str(doors).replace(";", " ").split():
            try:
                dx, dy = (int(v) for v in pair.split(","))
            except ValueError:
                return {"ok": False, "error": (
                    f"doors={doors!r} is 'x,y x,y ...'")}
            door_cells.add((dx, dy))
        wall_cells -= door_cells
        floor_cells |= door_cells

        side = _iso_blocks(tiles_disk)
        layers = []
        # CLIP THE FLOOR TO WHAT THE WALLS ENCLOSE. A designed level paints
        # floor past its own perimeter — margin, underlay, whatever the
        # original wall art covered — and the original covers it because its
        # wall tiles are tall enough to hide the strip. Re-emitted honestly,
        # that strip leaks out beyond the boundary as a fringe of carpet
        # floating outside the building. The enclosed region is the walkable
        # component the doors connect, plus the cells under the walls
        # themselves; anything else is backing, not floor.
        walk_all = (floor_cells - wall_cells) | door_cells
        keep, seen = set(), set()
        for cell in sorted(walk_all):
            if cell in seen:
                continue
            comp, stack = set(), [cell]
            while stack:
                cx0, cy0 = stack.pop()
                if (cx0, cy0) in comp:
                    continue
                comp.add((cx0, cy0))
                for q in ((cx0 + 1, cy0), (cx0 - 1, cy0),
                          (cx0, cy0 + 1), (cx0, cy0 - 1)):
                    if q in walk_all and q not in comp:
                        stack.append(q)
            seen |= comp
            if len(comp) > len(keep):
                keep = comp
        # WHAT IS OUTSIDE IS WHAT YOU CAN REACH FROM OUTSIDE. The shell
        # heuristic this replaces — drop wall cells with no walkable
        # neighbour — left a fringe anywhere the perimeter was two cells
        # thick or stepped, because the inner of the two still touched the
        # room. Flooding inward from beyond the bounding box answers the
        # actual question: a cell is outside the building when open air
        # reaches it without crossing a wall. Everything else is interior,
        # including the floor under interior walls, which has to stay because
        # a thin panel does not cover its own cell.
        xs = [c[0] for c in floor_cells] or [0]
        ys = [c[1] for c in floor_cells] or [0]
        x0, x1 = min(xs) - 1, max(xs) + 1
        y0, y1 = min(ys) - 1, max(ys) + 1
        air, stack = set(), [(x0, y0)]
        while stack:
            cx0, cy0 = stack.pop()
            if (cx0, cy0) in air or not (x0 <= cx0 <= x1 and y0 <= cy0 <= y1):
                continue
            if (cx0, cy0) in wall_cells:
                continue                      # air does not cross a wall
            air.add((cx0, cy0))
            stack.extend(((cx0 + 1, cy0), (cx0 - 1, cy0),
                          (cx0, cy0 + 1), (cx0, cy0 - 1)))
        outside = len(floor_cells & air)
        floor_cells = floor_cells - air
        # KEEP THE ART THAT IS ALREADY THERE. This defaulted to re-autotiling
        # every floor cell against a generated set, which on a project that
        # already ships correct tiles is not a re-skin, it is a downgrade —
        # and it was: the tiles it replaced were hand-made, in the game's own
        # palette, and the ones it painted were invented. A layout tool has
        # no business inventing colours for a game that has them. Pass an
        # explicit material_map to restyle a surface deliberately; by default
        # every cell keeps the source and atlas coordinate it already had.
        if keep_art:
            cells = [{"x": c["x"], "y": c["y"], "source": c["source"],
                      "ax": c["ax"], "ay": c["ay"], "alt": c["alt"]}
                     for c in _tilemap.decode_cells(found_packed[floor_name])
                     if (c["x"], c["y"]) in floor_cells]
            missing = {}
        else:
            terrain = _terrain("grid16", 0, 0, 0, 4, "Floor")
            cells = _autotile.resolve(sorted(floor_cells), terrain)
            missing = _autotile.unmapped(sorted(floor_cells), terrain)
            _scatter_variants(cells, tiles_disk)

        # KEEP THE SURFACES THE LAYOUT ALREADY HAD. A designed floor changes
        # underfoot at every threshold and says so by drawing those cells
        # from a different atlas source — this floor uses fifteen. Collapsing
        # them all onto one material is what makes a re-skin read as bland
        # when the original did not, so `material_map` carries the original
        # source id onto a generated material and the thresholds survive.
        mats = (side or {}).get("materials") or {}
        mapped = 0
        if material_map and mats:
            want = {}
            for pair in material_map.replace(";", " ").split():
                sid, _, mat = pair.partition("=")
                if sid.strip().isdigit() and mats.get(mat.strip(), {}).get("source"):
                    want[int(sid)] = mats[mat.strip()]
            if want:
                by_cell = {}
                for name0, cset in found.items():
                    if name0 != floor_name:
                        continue
                for c in _tilemap.decode_cells(found_packed[floor_name]):
                    by_cell[(c["x"], c["y"])] = c["source"]
                for cell in cells:
                    meta = want.get(by_cell.get((cell["x"], cell["y"])))
                    if not meta:
                        continue
                    cell["source"] = int(meta["source"])
                    ax, ay = meta["interior"]
                    variants = [tuple(v) for v in meta.get("variants") or []]
                    pick = ((cell["x"] * 928_371 + cell["y"] * 689_287)
                            % (len(variants) + 1)) if variants else 0
                    at = variants[pick - 1] if pick else (ax, ay)
                    cell["ax"], cell["ay"] = int(at[0]), int(at[1])
                    mapped += 1
        layers.append({"name": "Floor", "terrain": "Floor", "cells": cells,
                       "unmapped": {}})

        # ELEVATION, if a region was named. Everything rises except the hole,
        # which is the same thing as digging it and is the only one of the two
        # the block primitive can draw.
        elevation = None
        if sunken:
            try:
                sx, sy, sw, sh = (int(v) for v in
                                  sunken.replace(",", " ").split())
            except ValueError:
                return {"ok": False, "error": (
                    f"sunken={sunken!r} is not 'x,y,w,h'")}
            low = {(x, y) for x in range(sx, sx + sw)
                   for y in range(sy, sy + sh)} & floor_cells
            if not low:
                return {"ok": False, "error": (
                    f"the sunken region {sunken!r} holds no floor cells")}
            # WALKABLE IS NOT THE SAME AS FLOORED. A designed level paints
            # floor UNDER its walls — downsizing ships a `floor_underwall`
            # tile for exactly that — so the floor layer alone says the
            # basement's whole rim touches open ground and ramps it, all
            # forty cells of it. The walls are what make a doorway a doorway,
            # so they decide where the two heights are allowed to meet.
            walk = floor_cells - wall_cells
            low_walk = low & walk
            heights = {c: 1 for c in walk if c not in low}
            ramps = {}
            for (x, y) in sorted(heights):
                for face, (dx, dy) in _levelgen.RAMP_DIRS.items():
                    if (x + dx, y + dy) in low_walk:
                        ramps[(x, y)] = face
                        break
            start = next(iter(sorted(walk - low)), None)
            got = _levelgen.reachable(walk, heights, ramps, start=start)
            if len(got) != len(walk):
                return {"ok": False, "error": (
                    f"{len(walk) - len(got)} walkable cells cannot be reached "
                    "once that region is sunk — the hole has no way in"),
                    "unreachable": sorted(walk - got)[:20]}
            side_blocks = (side or {}).get("blocks") or {}
            if not side_blocks.get("terrace"):
                return {"ok": False, "error": (
                    "this tileset has no raised tiles, so a sunken region "
                    "cannot be drawn")}
            # every floored cell that is not the hole draws raised, walls
            # included: a wall standing on the high ground has to sit on it.
            # The sidecar names the blocks' source; hardcoded 1 painted the
            # terraces from whatever source 1 is (downsizing: carpet).
            blk_src = int((side or {}).get("wall_source", 1))
            raised = []
            for cell in sorted({c: 1 for c in floor_cells if c not in low}):
                face = ramps.get(cell)
                at = (side_blocks.get(f"ramp_{face}") if face
                      else side_blocks.get("terrace"))
                if at:
                    raised.append({"x": cell[0], "y": cell[1],
                                   "source": blk_src,
                                   "ax": int(at[0]), "ay": int(at[1]),
                                   "alt": 0})
            if raised:
                layers.append({"name": "Terrace", "terrain": "Terrace",
                               "cells": raised, "unmapped": {},
                               "props": {"y_sort_enabled": True}})
            elevation = {"sunken_cells": len(low_walk),
                         "raised_cells": len(heights),
                         "ramps": {f"{x},{y}": d for (x, y), d in
                                   sorted(ramps.items())},
                         "reachable": True}

        wall_at, wall_src = None, None
        if wall_cells:
            # The sidecar names the blocks' source; 1 is only the fallback
            # for a set whose sidecar predates the key.
            blk_src = int((side or {}).get("wall_source", 1))
            if side and side["blocks"].get("wall") \
                    and blk_src in parsed_set["sources"]:
                wall_at, wall_src = tuple(side["blocks"]["wall"]), blk_src
            elif blk_src in parsed_set["sources"]:
                wall_at, wall_src = (0, 0), blk_src
            if wall_at is not None:
                blocks = (side or {}).get("blocks") or {}
                # PER-AREA WALLS, the same way the floors work. A partition,
                # a glazed meeting room and an exec office are different
                # surfaces in the original too — it draws them from different
                # atlases — and a re-skin that puts one panel everywhere
                # throws away the reading that tells you which room you are
                # standing in.
                wsets = (side or {}).get("wall_sets") or {}
                wwant = {}
                for pair in wall_map.replace(";", " ").split():
                    sid0, _, wname = pair.partition("=")
                    if sid0.strip().isdigit() and wname.strip() in wsets:
                        wwant[int(sid0)] = wsets[wname.strip()]
                by_wall = {}
                if wwant and found_packed.get(wall_layer):
                    for c in _tilemap.decode_cells(found_packed[wall_layer]):
                        by_wall[(c["x"], c["y"])] = c["source"]
                wall_out = []
                if keep_art and not wwant and found_packed.get(wall_layer):
                    wall_out = [
                        {"x": c["x"], "y": c["y"], "source": c["source"],
                         "ax": c["ax"], "ay": c["ay"], "alt": c["alt"]}
                        for c in _tilemap.decode_cells(
                            found_packed[wall_layer])
                        if (c["x"], c["y"]) in wall_cells]
                for (x, y) in ([] if wall_out else sorted(wall_cells)):
                    setm = wwant.get(by_wall.get((x, y)))
                    use_blocks = (setm or {}).get("blocks") or blocks
                    src = int((setm or {}).get("source", wall_src))
                    at = (_wall_tile_at(use_blocks, (x, y), wall_cells)
                          if iso else None) or wall_at
                    wall_out.append({"x": x, "y": y, "source": src,
                                     "ax": int(at[0]), "ay": int(at[1]),
                                     "alt": 0})
                layers.append({
                    "name": "Walls", "terrain": "Walls", "cells": wall_out,
                    "unmapped": {},
                    **({"props": {"y_sort_enabled": True}} if iso else {})})

        dest = out_scene or scene.replace(".tscn", "_reskin.tscn")
        dest_disk, dest_res = _res_pair(godot_project, dest, ".tscn")
        base = (_EMPTY_SCENE.format(root=dest_disk.stem.title() or "Level")
                if not dest_disk.is_file() else
                dest_disk.read_text(encoding="utf-8", errors="replace"))
        wired = _scenewire.wire_tilemap(base, tiles_res, layers, parent=parent,
                                        owns=["Floor", "Walls", "Terrace"])
        if not dry_run:
            dest_disk.parent.mkdir(parents=True, exist_ok=True)
            dest_disk.write_text(wired["text"], encoding="utf-8")
            # Evidence-gate honesty: a freshly written scene bypasses
            # scenewire.apply, which is what notes writes to the writelog.
            _note_tool_write(_root(), dest_disk)
            _log("level", f"reskinned {src_res} onto {tiles_res}",
                 ref=dest_res)
        return {"ok": True, "source": src_res, "scene": dest_res,
                "tileset": tiles_res, "view": view,
                **({"elevation": elevation} if elevation else {}),
                "read": {n: len(c) for n, c in sorted(found.items())},
                "floor_layer": floor_name,
                "floor_cells": len(floor_cells),
                "wall_cells": len(wall_cells),
                "doors": len(door_cells),
                "clipped_outside": outside,
                "material_cells": mapped,
                "kept_art": bool(keep_art),
                "walls": ("blocks" if side and wall_src else
                          "source 1" if wall_src else
                          "not drawn: the new tileset has no wall source"),
                "unmapped": {str(m): n for m, n in sorted(missing.items())},
                "written": not dry_run,
                "summary": wired["summary"]}
    except Exception as exc:
        return _fail(exc)


@_tool
def level_plan(width: int = 48, height: int = 32, seed: int = 0,
               layout: str = "bsp", rooms: int = 5, side_rooms: int = 1,
               min_leaf: int = 10, min_room: int = 4, margin: int = 1,
               max_depth: int = 5, corridor_width: int = 2,
               room_fill: float = 0.8) -> dict:
    """Lay out a room-and-corridor level and show it, WITHOUT touching a scene.

    `room_fill` is the share of its BSP cell a room must take, and it is the
    difference between a dungeon and a set of thin rooms with slabs between
    them: at 0 a room is a uniform-random slice of its cell, so the rest of the
    cell stays solid. `corridor_width` defaults to 2 because a one-cell passage
    loses most of its width to the wall the tile art draws inside its own edge.

    BSP: cut the map in two until a piece holds one room, put a room in each
    piece, then join the two halves of every cut on the way back up. That join
    is the guarantee - it builds a spanning tree over the rooms, so every room
    is reachable from every other by construction rather than by luck. The
    result says `connected` and it is checked with a flood fill, not asserted.

    Read the `ascii` field. It is the fastest way to see that a level is one big
    room, or two halves joined by nothing, and it costs no engine and no
    screenshot. Iterate on `seed` here until the shape is right, THEN call
    level_generate with the same numbers to write it.

    Knobs that actually change the shape:
      seed            same seed, same level, forever.
      min_leaf        bigger -> fewer, larger rooms. Must be at least
                      min_room + 2*margin or nothing fits and it says so.
      max_depth       caps how many times the map is cut, so it caps room count.
      corridor_width  1 reads as a dungeon, 2+ as a complex.
      margin          gap between a room and its leaf's edge; 0 lets neighbouring
                      rooms fuse into one L-shaped cavity.
    """
    try:
        # A PARTITION OR A ROUTE. BSP gives rooms that are all the same KIND
        # of thing — every one a box off a corridor, none first or last — and
        # a designed floor is not that. Shown a real tutorial floor its author
        # described it as five main rooms with a side room and drew the path
        # through them, which is a sequence with branches; `layout="path"`
        # builds that, and reports which rooms are the route and which are the
        # detour so a caller can dress them differently.
        if str(layout).strip().lower() in ("path", "route", "chain"):
            level = _levelgen.plan_path(
                width, height, seed=seed, rooms=rooms,
                side_rooms=side_rooms, corridor_width=corridor_width,
                margin=max(1, margin), room_w=min_leaf, room_h=min_leaf)
        else:
            level = _levelgen.plan(width, height, seed=seed, min_leaf=min_leaf,
                                   min_room=min_room, margin=margin,
                                   max_depth=max_depth,
                                   corridor_width=corridor_width,
                                   room_fill=room_fill)
        return {"ok": True, "seed": seed, "width": width, "height": height,
                "rooms": level["rooms"], "room_count": len(level["rooms"]),
                "corridor_count": len(level["corridors"]),
                "floor_cells": len(level["floor"]),
                "wall_cells": len(level["walls"]),
                "connected": level["connected"], "spawn": level["spawn"],
                "exit": level["exit"], "ascii": _levelgen.ascii_map(level)}
    except Exception as exc:
        return _fail(exc)


def _read_prop_manifest(root, ref: str) -> dict:
    """A prop manifest written by `prop_generate`, by res:// path or disk path.

    Refuses a manifest whose view disagrees with the project's, because that is
    art drawn to a camera this game does not use — every sprite in it shows the
    wrong faces, and no placement rule can correct a projection after the fact.
    """
    from bgate_core import gameview as _gv

    path = _Path(str(ref).replace("res://", "").strip()) if str(ref).startswith(
        "res://") else _Path(str(ref))
    if not path.is_absolute():
        for base in (_Path(root), _Path(root) / ".bgate_out" / "props"):
            if (base / path).exists():
                path = base / path
                break
    if not path.exists():
        raise ValueError(f"no prop manifest at {ref!r} — prop_generate writes "
                         "one beside the atlas it packs")
    man = _json.loads(path.read_text(encoding="utf-8"))
    want = _gv.load(root)
    if man.get("view") and man["view"] != want:
        raise ValueError(
            f"that prop sheet was drawn for a {man['view']} game and this "
            f"project is {want} — the sprites show the wrong faces, and a "
            "projection cannot be corrected after the fact. Regenerate it.")
    man["atlas"] = {k: ({f: tuple(c) for f, c in v.items()}
                        if isinstance(v, dict) else tuple(v))
                    for k, v in (man.get("atlas") or {}).items()}
    return man


#: Which wall tile a cell wants, from the wall cells around it. A run of wall
#: along the cell x axis renders down-right, along y down-left, and a corner
#: gets stubs on exactly the two sides that continue — a tile that always
#: reached all four ways put a nub out into open floor at every corner.
def _panel_mask(cell, wall_cells) -> int:
    from bgate_core import tilemask as _tm

    x, y = cell
    mask = 0
    if (x, y - 1) in wall_cells:
        mask |= _tm.BIT_N
    if (x + 1, y) in wall_cells:
        mask |= _tm.BIT_E
    if (x, y + 1) in wall_cells:
        mask |= _tm.BIT_S
    if (x - 1, y) in wall_cells:
        mask |= _tm.BIT_W
    return mask


def _wall_tile_at(blocks: dict, cell, wall_cells):
    """The atlas coordinate for one wall cell — a masked panel when the set
    has them, the solid block when it does not."""
    if not blocks:
        return None
    at = blocks.get(f"panel{_panel_mask(cell, wall_cells)}")
    if at is None:
        at = blocks.get("wall")
    # A LIST MEANS STAGGER THEM. These tiles carry a vertical panel joint,
    # and one tile repeated down a run puts that joint on every cell — a
    # regular rib that reads as a waffle rather than a wall. The set ships
    # two variants with the joint on opposite sides for exactly this, and
    # the original level alternates them; picking by cell parity reproduces
    # that and keeps the choice deterministic, so a re-run is the same scene.
    if at and isinstance(at[0], (list, tuple)):
        at = at[(cell[0] + cell[1]) % len(at)]
    return at


def _tileset_manifest(tiles_disk: _Path) -> Optional[dict]:
    """The layout knowledge tileset_generate wrote beside its .tres, or None.

    None means a hand-built or older tileset: the caller falls back to the
    explicit floor_*/wall_* parameters exactly as before. A sidecar that
    exists but predates the manifest schema (no "kind") is the same answer -
    it still serves _scatter_variants, it just cannot describe a layout.
    """
    side = tiles_disk.with_name(tiles_disk.stem + ".tiles.json")
    if not side.is_file():
        return None
    try:
        meta = _json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("kind") != "bgate-tileset":
        return None
    return meta


def _manifest_floor(meta: dict, name: str):
    """The floor Terrain a manifest describes. Raises on a malformed table -
    a half-read manifest must fail loudly, not draw a half-right level."""
    floor = meta.get("floor") or {}
    table = {int(m): (int(c[0]), int(c[1]))
             for m, c in (floor.get("table") or {}).items()}
    solid = floor.get("solid")
    return _autotile.Terrain.from_table(
        int(floor.get("source", 0)), table,
        bits=int(meta.get("bits") or 8), name=name,
        fallback=(int(solid[0]), int(solid[1])) if solid else None)


@_tool
def tileset_describe(godot_project: str, tileset: str,
                     floor_layout: str = "solid", floor_source: int = 0,
                     floor_atlas_x: int = 0, floor_atlas_y: int = 0,
                     floor_columns: int = 4,
                     wall_layout: str = "blob47", wall_source: int = 0,
                     wall_atlas_x: int = 0, wall_atlas_y: int = 0,
                     wall_columns: int = 8,
                     variants: str = "", interior: str = "",
                     overwrite: bool = False) -> dict:
    """TEACH THE LEVEL TOOLS A HAND-BUILT TILESET. Once per sheet, then never again.

    A sheet bought from an asset pack or authored in Tilesetter knows where
    its tiles are; nothing else does. That knowledge used to travel as ten
    parameters on EVERY `level_generate` call - re-typed per level, per seat,
    per session, and wrong in exactly one of them. The art seat holds the
    sheet; gameplay and tech build the levels; a pasted string was the only
    channel between them.

    So it is written down instead. This produces the same
    `<tileset>.tiles.json` sidecar `tileset_generate` writes for a set it made
    itself, which is why `level_generate` now takes no atlas coordinates from
    anybody: generated or hand-built, the sheet describes itself on disk.

    LAYOUTS, for both floor and wall:
      blob47   8-bit mask, 47 tiles, row-major from (atlas_x, atlas_y),
               `columns` wide, masks ascending. Sides plus corners.
      grid16   4-bit mask, 16 tiles, same layout rule. Sides only - right for
               a wall one cell thick.
      solid    one tile everywhere, at (atlas_x, atlas_y). No autotiling.
      none     no layer of this kind at all (walls only).

    THAT ORDER IS A CONVENTION, NOT A STANDARD, and a wrong one draws a
    complete, confident, wrong-looking level rather than an error. Check the
    first screenshot after describing a sheet, not the tenth.

    variants/interior are optional and only affect floors: `interior` is the
    plain fill tile "x,y" and `variants` is a space list of alternates
    ("3,0 4,0") scattered deterministically over it, which is what stops a
    large room reading as one repeated square.

    Re-describing a sheet needs `overwrite=True` - a set generated by
    `tileset_generate` already has a manifest holding its full mask table, and
    silently replacing it with a guess is how a working level starts drawing
    at (0, 0).
    """
    try:
        _contained_path(godot_project, "godot_project")
        tiles_disk, tiles_res = _res_pair(godot_project, tileset, ".tres")
        if not tiles_disk.is_file():
            return {"ok": False, "error": (
                f"no tileset at {tiles_res} - import or generate the sheet "
                "first; there is nothing here to describe")}
        for got, field in ((floor_layout, "floor_layout"),
                           (wall_layout, "wall_layout")):
            if got not in _WALL_LAYOUTS:
                return {"ok": False, "error": (
                    f"{field} {got!r} is not one of {_WALL_LAYOUTS}")}
        if floor_layout == "none":
            return {"ok": False, "error": (
                "a level with no floor is not a level - floor_layout takes "
                "solid, grid16 or blob47")}
        side = tiles_disk.with_name(tiles_disk.stem + ".tiles.json")
        if side.is_file() and not overwrite:
            return {"ok": False, "error": (
                f"{side.name} already exists. If tileset_generate made this "
                "set, that file holds its real mask table and this call would "
                "replace it with a guess; pass overwrite=True only if you "
                "know the sheet was hand-built and the description is wrong.")}

        floor = _terrain(floor_layout, floor_source, floor_atlas_x,
                         floor_atlas_y, floor_columns, "Floor")
        wall = (None if wall_layout == "none" else
                _terrain(wall_layout, wall_source, wall_atlas_x,
                         wall_atlas_y, wall_columns, "Walls"))
        parsed = _tilemap.parse_tileset(
            tiles_disk.read_text(encoding="utf-8", errors="replace"))
        # THE SOURCES MUST EXIST IN THE .tres. A described sheet pointing at a
        # source the resource never defines draws an empty layer and reports
        # success - the same silent shape this whole sidecar exists to kill.
        known = set(parsed.get("sources") or ())
        wanted = {floor_source} | ({wall_source} if wall else set())
        missing = sorted(wanted - known) if known else []
        if missing:
            return {"ok": False, "error": (
                f"{tiles_res} defines sources {sorted(known)}, so source(s) "
                f"{missing} would draw nothing. Fix the source ids or import "
                "the missing atlas into the tileset first.")}

        meta: dict = {"kind": "bgate-tileset", "version": 1,
                      "described": True,
                      "tile_px": int(parsed.get("tile_px") or 0) or 32,
                      "bits": int(floor.bits),
                      "floor": _terrain_block(floor, floor_layout)}
        if wall is not None:
            meta["wall"] = _terrain_block(wall, wall_layout)
        picks = [tuple(int(v) for v in pair.split(","))
                 for pair in str(variants).replace(",", ",").split()
                 if pair.strip()]
        if interior.strip():
            at = [int(v) for v in interior.split(",")]
            meta["interior"] = at
            meta["floor"]["solid"] = at
        if picks:
            meta["variants"] = [list(p) for p in picks]
            meta["floor"]["variants"] = [list(p) for p in picks]
        side.write_text(_json.dumps(meta, indent=1), encoding="utf-8")
        _log("level", f"described the hand-built tileset {tiles_res}",
             ref=tiles_res)
        return {"ok": True, "tileset": tiles_res, "manifest": str(side),
                "floor": {"layout": floor_layout, "source": floor_source,
                          "tiles": len(floor.table) or 1},
                "wall": (None if wall is None else
                         {"layout": wall_layout, "source": wall_source,
                          "tiles": len(wall.table) or 1}),
                "next": ("level_generate(godot_project, scene, tileset) - it "
                         "reads this file, so you pass no atlas coordinates "
                         "and no layouts. Look at the first screenshot: a "
                         "wrong tile order draws a confident wrong level.")}
    except Exception as exc:
        return _fail(exc)


def _manifest_wall(meta: dict, name: str):
    """The wall Terrain a manifest describes, or None for a floor-only set.

    TWO SHAPES, and both are current. `tileset_generate` writes a wall as one
    solid block (`{"source": n, "atlas": [x, y]}`) because its walls ARE
    blocks; a hand-built sheet described by `tileset_describe` carries a mask
    table exactly like the floor, because an authored sheet is where blob47
    and grid16 came from in the first place. Reading only the first shape is
    what kept `wall_layout`/`wall_columns`/`wall_atlas_*` alive as tool
    parameters long after the sidecar existed.
    """
    wall = meta.get("wall")
    if not wall:
        return None
    table = {int(m): (int(c[0]), int(c[1]))
             for m, c in (wall.get("table") or {}).items()}
    if table:
        solid = wall.get("solid")
        return _autotile.Terrain.from_table(
            int(wall.get("source", 0)), table,
            bits=int(wall.get("bits") or meta.get("bits") or 8), name=name,
            fallback=(int(solid[0]), int(solid[1])) if solid else None)
    # `atlas` is tileset_generate's word for the one block tile; `solid` is
    # what _terrain_block writes for a solid layout, since that is where a
    # Terrain with no table keeps its coordinate. Same fact, two spellings,
    # and refusing one of them would make a described set draw at (0, 0).
    at = wall.get("atlas") or wall.get("solid") or (0, 0)
    return _autotile.Terrain.solid(
        int(wall.get("source", 1)),
        (int(at[0]), int(at[1])), name=name)


def _terrain_block(terrain, layout: str) -> dict:
    """A Terrain as the sidecar stores it. The inverse of `_manifest_*`."""
    block: dict = {"source": int(terrain.source), "layout": layout,
                   "bits": int(terrain.bits),
                   "table": {str(m): [int(c[0]), int(c[1])]
                             for m, c in sorted(terrain.table.items())}}
    if terrain.fallback:
        block["solid"] = [int(terrain.fallback[0]), int(terrain.fallback[1])]
    return block


def _iso_blocks(tiles_disk: _Path) -> Optional[dict]:
    """The raised-tile map tileset_generate wrote beside an isometric set.

    A .tres can say a tile exists; it cannot say which one is a ramp facing
    east. Without this file a level can still be drawn flat, so its absence
    is a None rather than a raise.
    """
    side = tiles_disk.with_name(tiles_disk.stem + ".tiles.json")
    if not side.is_file():
        return None
    try:
        meta = _json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return meta if meta.get("blocks") else None


def _scatter_variants(cells: list, tiles_disk: _Path) -> int:
    """Swap interior cells between the tileset's variant tiles, in place.

    Reads the ``<name>.tiles.json`` sidecar tileset_generate writes; without
    one this is a no-op, so hand-built tilesets are untouched. Deterministic
    by coordinate — the same seed keeps producing byte-identical scenes.
    """
    side = tiles_disk.with_name(tiles_disk.stem + ".tiles.json")
    if not side.is_file():
        return 0
    try:
        meta = _json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    interior = tuple(meta.get("interior") or ())
    variants = [tuple(v) for v in meta.get("variants") or []]
    if not interior or not variants:
        return 0
    swapped = 0
    for c in cells:
        if (c["ax"], c["ay"]) != interior:
            continue
        pick = (c["x"] * 928_371 + c["y"] * 689_287) % (len(variants) + 1)
        if pick:
            c["ax"], c["ay"] = variants[pick - 1]
            swapped += 1
    return swapped


def _manifest_source(tiles_disk: _Path, man: dict) -> int:
    """The tileset source id of the manifest's atlas, ADDING it if absent.

    The seam nobody closed: prop_generate installs an atlas and writes a
    manifest, the level generators place cells referencing it by source id —
    and the tileset had never been told the atlas exists, so every prop cell
    pointed at a source the resource did not define. The manifest carries
    everything a source needs (texture, tiles, spans, origins, animation);
    this hands it to the tileset once and is idempotent after that.
    """
    def _keyed(d):
        return {tuple(int(v) for v in k.split(",")): tuple(vv)
                for k, vv in (d or {}).items()}

    text = tiles_disk.read_text(encoding="utf-8", errors="replace")
    got = _tilemap.append_source(text, {
        "texture": man["texture"],
        "tiles": [tuple(t) for t in (man.get("tiles") or [])],
        "region": (int(man.get("tile_px") or 32),) * 2,
        "sizes": _keyed(man.get("sizes")),
        "origins": _keyed(man.get("origins")),
        "animation": {tuple(int(v) for v in k.split(",")): dict(vv)
                      for k, vv in (man.get("animation") or {}).items()},
    })
    if not got["reused"]:
        tiles_disk.write_text(got["text"], encoding="utf-8")
    return got["id"]


#: THE MEDIUM IS PART OF THE ORDER. Every prompt here used to name a material
#: and nothing else — "worn olive office carpet, soft even light" — so the
#: models did the sensible thing and returned a PHOTOGRAPH of a floor, one of
#: them with a vanishing point in it. Downscaling a photograph to a 64x32
#: diamond gives exactly what it sounds like: mush with no readable detail,
#: which is what "bland and repetitive" actually was. A tile for a 16-bit game
#: has to be ordered as one: the era, the projection, the palette discipline
#: and the pixel scale, before the material is ever mentioned.
# _TEXTURE_STYLE/_TEXTURE_ZOOM live in server beside tileset_generate,
# their only caller.



@_tool
def level_generate(godot_project: str, scene: str, tileset: str,
                   width: int = 48, height: int = 32, seed: int = 0,
                   layout: str = "bsp", walls: bool = True,
                   tuning: Optional[dict] = None,
                   levels: int = 1, raised: float = 0.35,
                   props: bool = False, prop_manifest: str = "",
                   prop_density: float = 0.1,
                   parent: str = ".", names: Optional[dict] = None,
                   create: bool = False,
                   dry_run: bool = False) -> dict:
    """Generate a level and write it into a scene as TileMapLayer nodes.

    The whole chain: BSP layout -> neighbour-bitmask autotiling -> the packed
    binary Godot stores tiles in -> a .tscn edit, backed up. No engine and no
    editor involved, so it runs headless and is a normal reviewable diff.

    THE TILESET DESCRIBES ITSELF, so this call takes NO atlas coordinates.
    `tileset_generate` writes a `<name>.tiles.json` sidecar for a set it made;
    for a hand-built or imported sheet, `tileset_describe` writes the same file
    once. Either way the mask table, the sources and the layouts come off disk.
    Without that file this refuses rather than guessing - a guessed coordinate
    draws a complete, confident, wrong-looking level, which is worse than an
    error. `walls=False` writes the floor layer only.

    WHICH TILE GOES WHERE is decided by a neighbour bitmask, the same job the
    Godot editor's terrain sets do - and they only run in the editor, which is
    why it is redone here.

    `props=True` adds a third layer of DRESSING — wall torches, clutter against
    the architecture, cover in the rooms you walk through, a feature in the dead
    ends. Placement is by what the room is for (see `bgate_core.props`) and every
    solid prop is refused if it would break the level into two regions, checked
    by flood filling rather than by reasoning about it. It needs
    `prop_manifest`, the file `prop_generate` writes beside its atlas: the
    types, coordinates, spans, texture origins and animation all come from
    there. `prop_density` is the only dial here, because it is the only one
    that is about the LEVEL rather than about the sheet.

    EACH TYPE DECLARES ITS OWN CONSTRAINTS and the placer obeys them instead of
    assuming a prop goes anywhere. A wall mount occupies the WALL cell, so it is
    attached rather than floating in the room beside it. A side-view or angled
    sprite declares which walls it can be drawn on — `torch` is ("e", "w"), so it
    never lands on a horizontal wall where a three-quarter view reads as pasted
    on. Nothing mounts on the wall south of a room, whose inner face points away
    from the camera, and nothing mounts on a corner, where the face it needs is
    interrupted. If your north walls come back dark that is `no_side` in the
    report, and the fix is a front-facing type such as `sconce` rather than a
    wider tolerance. Godot's flip bit mirrors a sprite whose type allows it.

    THAT ORDER IS A CONVENTION, NOT A STANDARD. A sheet authored in Tilesetter
    or bought from an asset pack has its own order, and a wrong order draws a
    complete, confident, wrong-looking level. Check the first screenshot. If
    `unmapped` in the result is non-empty, the sheet is missing shapes the level
    needs and that field says which masks and how often - that is what to hand
    an artist.

    Re-running REPLACES the layers it wrote rather than adding more, so
    iterating on `seed` leaves one Floor and one Walls, not eight.

    godot_project: the directory holding project.godot.
    scene/tileset: res:// paths, or paths relative to that directory.
    tuning: the eight BSP partition knobs, all optional — min_leaf, min_room,
      margin, max_depth, corridor_width, room_fill, rooms, side_rooms. The
      same numbers `level_plan` takes, so preview there and pass the dict
      here. An unknown key is refused by name rather than ignored.
    names: layer node names, {"floor": ..., "walls": ..., "props": ...};
      defaults Floor/Walls/Props.
    """
    try:
        # THE EIGHT BSP KNOBS, BEHIND ONE DOOR. They were eight top-level
        # parameters that almost nobody set and everybody had to read past;
        # they are the same eight `level_plan` takes, and they tune the
        # PARTITION rather than the level. Unknown keys are refused by name
        # because a silently-ignored `min_rooms` typo is a tuning dict that
        # did nothing and said it worked.
        tuning = dict(tuning or {})
        unknown = sorted(set(tuning) - set(_LEVEL_TUNING))
        if unknown:
            return {"ok": False, "error": (
                f"tuning has no key(s) {unknown} - it takes "
                f"{sorted(_LEVEL_TUNING)}. level_plan previews the same "
                "numbers without writing a scene.")}
        tune = {**_LEVEL_TUNING, **{k: v for k, v in tuning.items()}}
        min_leaf = int(tune["min_leaf"]); min_room = int(tune["min_room"])
        margin = int(tune["margin"]); max_depth = int(tune["max_depth"])
        corridor_width = int(tune["corridor_width"])
        room_fill = float(tune["room_fill"])
        rooms = int(tune["rooms"]); side_rooms = int(tune["side_rooms"])

        # Layer node names: cosmetic, three parameters deep, and only ever
        # touched by a project whose scene already has a `Floor`.
        names = dict(names or {})
        floor_name = str(names.get("floor") or "Floor")
        wall_name = str(names.get("walls") or "Walls")
        prop_name = str(names.get("props") or "Props")

        # THE SAME GATE sidescroll_generate holds in the other direction.
        # Under gravity a connected floor guarantees nothing — you cannot
        # walk upward — so rooms-and-corridors geometry in a platformer is
        # the wrong geometry, not a style choice.
        view = _gameview.load(_root())
        if view == "side_scroller":
            return {"ok": False, "error": (
                "this project's view is 'side_scroller'. Rooms and corridors "
                "are top-down geometry — use sidescroll_generate, which "
                "builds for this character's jump, or game_view_set if the "
                "declared view is wrong.")}
        iso = view == "isometric"
        scene_disk, scene_res = _res_pair(godot_project, scene, ".tscn")
        tiles_disk, tiles_res = _res_pair(godot_project, tileset, ".tres")

        if not tiles_disk.is_file():
            raise ValueError(f"no tileset at {tiles_res} - generate or import it "
                             "first; a level cannot pick tiles from nothing")
        parsed_set = _tilemap.parse_tileset(
            tiles_disk.read_text(encoding="utf-8", errors="replace"))
        # THE TILESET'S SHAPE MUST AGREE WITH THE VIEW. A square set on an
        # isometric project renders a plausible, wrong-looking grid — the
        # exact silent failure tilemap.py documents — and the inverse draws
        # diamonds flat. Neither errors in the engine, so it errors here.
        want_shape = _tilemap.ISOMETRIC if iso else _tilemap.SQUARE
        if parsed_set["shape"] != want_shape:
            raise ValueError(
                f"{tiles_res} has tile shape {parsed_set['shape']} and this "
                f"project's view is {view!r} (wants shape {want_shape}). "
                "Godot renders the mismatch without complaint and it looks "
                "wrong everywhere — fix the tileset or the declared view, "
                "not this call.")
        # THE SHEET DESCRIBES ITSELF, AND THAT IS THE WHOLE POINT.
        #
        # Ten parameters used to carry the atlas layout on EVERY call -
        # floor_source, floor_atlas_x/_y, floor_layout, floor_columns and the
        # same five for walls - re-typed per level, per seat, per session. The
        # art seat holds the sheet and knows every coordinate; gameplay and
        # tech build the levels and need them; the only channel between the
        # two was a pasted string, and a string is wrong in exactly one call
        # out of ten. `tileset_generate` has written the sidecar for its own
        # sets for a while; what was missing was a way for a HAND-BUILT sheet
        # to have one, which is `tileset_describe`. With both, this tool takes
        # no atlas coordinates from anyone.
        manifest = _tileset_manifest(tiles_disk)
        if manifest is None:
            return {"ok": False, "error": (
                f"{tiles_res} has no {tiles_disk.stem}.tiles.json beside it, "
                "so nothing here knows where its floor and wall tiles live. "
                "tileset_generate writes that file for a set it made itself; "
                "for a hand-built or imported sheet call tileset_describe "
                "ONCE and every level after it needs no atlas coordinates. "
                "This tool no longer takes them - a coordinate typed per "
                "call is the bug that file exists to end.")}
        floor_terrain = _manifest_floor(manifest, floor_name)
        floor_source = int(floor_terrain.source)
        wall_terrain = _manifest_wall(manifest, wall_name) if walls else None
        wall_layout = ("none" if wall_terrain is None else
                       str((manifest.get("wall") or {}).get("layout")
                           or "solid"))
        wall_source = int(wall_terrain.source) if wall_terrain else 0
        # AN ISOMETRIC WALL IS A BLOCK, AND A BLOCK IS A BLOCK. The 47- and
        # 16-mask layouts exist so a FLAT wall can show which sides face open
        # floor; a raised cell shows its two camera-facing sides whatever its
        # neighbours do, and the cell in front covers what it should because
        # the layer is y-sorted. So those layouts are not reduced here, they
        # are meaningless. The blocks live in the tileset's second source,
        # which is where tileset_generate writes them — and a set that has no
        # block source draws its floor and says so, rather than refusing over
        # a default the caller never typed.
        iso_walls = None
        # WANTED, NOT "WHICH MASK LAYOUT". This read `wall_layout in
        # ("blob47", "grid16")`, which worked only because wall_layout
        # defaulted to blob47 - now that the layout comes off the manifest, a
        # set describing no flat wall at all reads as "none" and the block
        # routing below never ran, so an isometric level came out floorless
        # of walls. The question here was always "does this level want
        # walls", and for an iso set the answer routes to blocks regardless of
        # what the flat sheet says.
        if iso and walls:
            # ONLY A SET THAT SAYS SO. This used to route walls to source 1
            # on the assumption that source 1 is the block strip, which is
            # true of every tileset this tool writes and false of a hand-built
            # one: pointed at a real project's set it chose `floor_carpet_b`
            # for the walls, because that is what its source 1 happens to be.
            # The sidecar is the only thing that can answer the question, so
            # a set without one keeps whatever the caller passed.
            side = _iso_blocks(tiles_disk)
            # THE SIDECAR NAMES ITS OWN SOURCE. Source 1 is where this tool
            # writes blocks; a project's hand-built set numbers them however
            # it likes — downsizing's wall panels are source 41 — and the
            # whole point of the sidecar is that a set can describe itself
            # instead of matching one tool's habits.
            block_at = (int(side.get("wall_source", 1))
                        if (side and side["blocks"]) else None)
            if block_at is not None and block_at in parsed_set["sources"]:
                # the sidecar NAMES the source; the resource has to actually
                # carry it. A stale sidecar beside an edited tileset would
                # otherwise route every wall at a source that is not there.
                wall_layout = "solid"
                if wall_source == 0:
                    wall_source = block_at
                iso_walls = "blocks"
            elif wall_source in parsed_set["sources"] and wall_source != 0:
                wall_layout = "solid"
                iso_walls = f"solid tiles from source {wall_source}"
            else:
                wall_layout = "none"
                iso_walls = ("floor only: this tileset has no block source. "
                             "tileset_generate writes one for an isometric "
                             "project; a set imported from elsewhere needs "
                             "its wall blocks as source 1.")
            # THE TERRAIN FOLLOWS THE ROUTING, not the manifest. The sidecar
            # describes the FLAT wall sheet's mask table, which is exactly
            # what an iso block strip does not have: a raised cell shows its
            # two camera-facing sides whatever its neighbours do. So the
            # decision above is honoured by rebuilding the terrain, rather
            # than by the old code's trick of reading a "did the caller pass
            # defaults" flag to decide which branch won.
            wall_terrain = (None if wall_layout == "none" else
                            _autotile.Terrain.solid(wall_source, (0, 0),
                                                    name=wall_name))
        have = sorted(parsed_set["sources"])
        wanted = {floor_source} | ({wall_source} if wall_layout != "none" else set())
        missing = sorted(w for w in wanted if w not in parsed_set["sources"])
        if missing:
            raise ValueError(
                f"{tiles_res} has no source {missing} - it has {have}. Source "
                "ids are not indexes; a tileset numbers them however it likes.")

        fresh = not scene_disk.is_file()
        if not fresh:
            text = scene_disk.read_text(encoding="utf-8", errors="replace")
        elif create:
            text = _EMPTY_SCENE.format(root=scene_disk.stem.title() or "Level")
        else:
            raise ValueError(
                f"no scene at {scene_res}. Pass create=true to start a new one, "
                "or point at an existing scene to add the layers to.")

        # A PARTITION OR A ROUTE. BSP gives rooms that are all the same
        # KIND of thing — every one a box off a corridor, none first or
        # last — and a designed floor is not that. Shown a real tutorial
        # floor its author described it as five main rooms with a side
        # room and drew the path through them: a sequence with branches,
        # which no partition of a rectangle contains.
        if str(layout).strip().lower() in ("path", "route", "chain"):
            level = _levelgen.plan_path(
                width, height, seed=seed, rooms=rooms,
                side_rooms=side_rooms, corridor_width=corridor_width,
                margin=max(1, margin), room_w=min_leaf, room_h=min_leaf)
        else:
            level = _levelgen.plan(width, height, seed=seed, min_leaf=min_leaf,
                                   min_room=min_room, margin=margin,
                                   max_depth=max_depth,
                                   corridor_width=corridor_width,
                                   room_fill=room_fill)
        # A PARTITION IS A LINE, ROCK IS A MASS. wall_fill paints every
        # non-floor cell, which is right for a dungeon carved out of stone —
        # the wall has rock behind it and the boundary is drawn once. An
        # office is the other case: its walls are one cell thick with floor
        # on both sides, and filling every gap rendered the space BETWEEN
        # rooms as a solid slab of partition. When the set draws thin panels,
        # take the ring.
        thin_walls = bool(iso and iso_walls == "blocks")
        # floor_terrain/wall_terrain came off the manifest above, before the
        # isometric routing had its say. Nothing is decided here any more.
        manifest_used = True
        layers = _levelgen.layers(
            level,
            wall_fill=not thin_walls,
            floor=floor_terrain, wall=wall_terrain,
            floor_name=floor_name, wall_name=wall_name)
        if iso:
            # Isometric is a depth sort or it is a lie: a wall drawn after
            # the player standing south of it reads as the player inside the
            # wall. The floor stays flat — nothing ever stands behind a
            # floor — everything above it y-sorts.
            for ly in layers:
                if ly["name"] != floor_name:
                    ly["props"] = {**(ly.get("props") or {}),
                                   "y_sort_enabled": True}
        # ELEVATION, and the ramps that make it terrain rather than scenery.
        # The heights go on AFTER the flat plan because every guarantee the
        # BSP gives — rooms that do not touch, a floor that is one region —
        # is still wanted; what changes is that `connected` now has to mean
        # "a walker can get there", which is a different question the moment
        # two adjacent cells sit at different altitudes.
        terraced = None
        if iso and levels > 1:
            terraced = _levelgen.terrace(level, seed=seed, levels=levels,
                                         raised=raised)
            if not terraced["connected"]:
                return {"ok": False, "error": (
                    f"{len(terraced['unreachable'])} floor cells cannot be "
                    "walked to once the terraces are placed — that is a "
                    "generator bug, not a layout to ship"),
                    "unreachable": terraced["unreachable"][:20]}
            side = _iso_blocks(tiles_disk)
            if not side:
                return {"ok": False, "error": (
                    "this tileset has no raised tiles, so a level with "
                    "levels>1 cannot be drawn. tileset_generate writes them "
                    "for an isometric project; a hand-built set needs a "
                    "<name>.tiles.json naming its terrace and ramp tiles.")}
            # THE BLOCKS MUST INCLUDE TERRACE TILES, and a set without them
            # refuses HERE rather than drawing a flat level that reports its
            # elevation as shipped. tileset_synth sidecars carry only
            # panel0..15, so every lookup below would miss, `continue`, and
            # leave no Terrace layer while the result still claimed
            # raised_cells and reachable=true — level_reskin's sunken path
            # already refuses on exactly these keys; the generator must too.
            if not side["blocks"].get("terrace"):
                return {"ok": False, "error": (
                    "this tileset's blocks carry no 'terrace' tile, so "
                    "levels>1 cannot be drawn honestly (tileset_synth sets "
                    "carry wall panels only). Regenerate with "
                    "tileset_generate, add terrace/ramp_* entries to the "
                    ".tiles.json, or pass levels=1.")}
            # The blocks' own source, the same key the wall path reads: a
            # hardcoded 1 painted terraces from whatever source 1 happens to
            # be — in a two-floor synth set, a floor material; in
            # downsizing's hand-built set, carpet.
            terr_src = int(side.get("wall_source", 1))
            heights = {tuple(int(v) for v in k.split(",")): h
                       for k, h in terraced["heights"].items()}
            ramps = {tuple(int(v) for v in k.split(",")): d
                     for k, d in terraced["ramps"].items()}
            raised_cells = []
            for cell, h in sorted(heights.items()):
                if not h:
                    continue
                face = ramps.get(cell)
                at = (side["blocks"].get(f"ramp_{face}") if face
                      else side["blocks"].get("terrace"))
                if not at:
                    continue
                raised_cells.append({"x": cell[0], "y": cell[1],
                                     "source": terr_src, "ax": int(at[0]),
                                     "ay": int(at[1]), "alt": 0})
            if raised_cells:
                # ITS OWN LAYER, above the floor and y-sorted: a terrace
                # overlaps the cells behind it, which is the whole point of
                # drawing it raised, and only a sorted layer draws the one in
                # front last.
                layers.append({"name": "Terrace", "terrain": "Terrace",
                               "cells": raised_cells, "unmapped": {},
                               "props": {"y_sort_enabled": True}})

        # A FLOOR PER ROOM. One surface across a whole level is the other
        # half of why a generated floor reads as generated: a building
        # changes underfoot at every threshold, and the layout already knows
        # where its rooms are. The sources to deal out — the project's own
        # carpets, walkway, breakroom, whatever it ships — are a fact about
        # the SHEET, so they come off the sheet's manifest rather than off a
        # string retyped per call; the route gets the first one so the
        # critical path stays legible as you walk it.
        room_floors = {}
        picks = [int(v) for v in (manifest.get("floor_sources") or [])
                 if str(v).strip().lstrip("-").isdigit()]
        if picks:
            for i, room in enumerate(level["rooms"]):
                src = picks[i % len(picks)]
                for cy in range(room["y"], room["y"] + room["h"]):
                    for cx in range(room["x"], room["x"] + room["w"]):
                        room_floors[(cx, cy)] = src
            for ly in layers:
                if ly["name"] != floor_name:
                    continue
                for c in ly["cells"]:
                    src = room_floors.get((c["x"], c["y"]))
                    if src is not None:
                        c["source"] = src

        # PER-CELL WALL TILES HERE TOO. The iso wall path routes to a SOLID
        # layout, which paints one atlas coordinate at every wall cell — and
        # with a set whose straights are thin panels that renders as a picket
        # fence with daylight between the posts. level_reskin already chose a
        # tile per cell from its neighbours; the generator has to do the same
        # or the walls it builds are not walls.
        if iso and iso_walls == "blocks":
            side_w = _iso_blocks(tiles_disk)
            blocks_w = (side_w or {}).get("blocks") or {}
            wall_set = {(c["x"], c["y"]) for ly in layers
                        if ly["name"] == wall_name for c in ly["cells"]}
            for ly in layers:
                if ly["name"] != wall_name:
                    continue
                for c in ly["cells"]:
                    at = _wall_tile_at(blocks_w, (c["x"], c["y"]), wall_set)
                    if at:
                        c["ax"], c["ay"] = int(at[0]), int(at[1])

        varied = sum(_scatter_variants(ly["cells"], tiles_disk)
                     for ly in layers)

        prop_report: dict = {}
        if props:
            # THE MANIFEST IS THE ONLY PATH NOW. `prop_generate` writes it and
            # it already knows every atlas coordinate, span, texture origin
            # and animation. What stood beside it was `prop_atlas`, a string
            # mini-language - "torch=0,0 barrel=1,0;2,0" - parsed by hand with
            # its own bespoke errors, plus `prop_types` and `prop_source` to
            # go with it. Three parameters and a DSL to re-state, per call,
            # what a file next to the atlas already said.
            if not prop_manifest:
                return {"ok": False, "error": (
                    "props=True needs prop_manifest=<path>. `prop_generate` "
                    "writes that file beside the atlas it packs, and it "
                    "carries the types, coordinates, spans, texture origins "
                    "and animation frames - none of which are tool "
                    "parameters any more. Generate the props first, or pass "
                    "props=False for a bare level.")}
            man = _read_prop_manifest(_root(), prop_manifest)
            atlas = man["atlas"]
            prop_source = _manifest_source(tiles_disk, man)
            want = tuple(man["types"]) or None
            # NOT `walls`: that is this tool's own parameter, and rebinding it
            # here worked only because the wall terrain was already built.
            wall_ring = _levelgen.wall_ring({tuple(c) for c in level["floor"]})
            plan_props = _props.plan(level, seed=seed, density=prop_density,
                                     walls=wall_ring, types=want,
                                     view=_gameview.load(_root()))
            # ONE LAYER PER DRAW LEVEL. A TileMapLayer holds a single tile
            # per coordinate, so a crack in the floor and the barrel standing
            # on it can only coexist as two layers — and the decals have to be
            # under, which is what the LAYERS order is.
            built = {"types": {}, "mirrored": 0, "cells": []}
            for lname in _props.LAYERS:
                part = _props.cells(plan_props, atlas, source=prop_source,
                                    layer=lname)
                if not part["cells"]:
                    continue
                node = prop_name if lname == "props" else f"{prop_name}{lname.title()}"
                layers.append({"name": node, "terrain": node,
                               "cells": part["cells"], "unmapped": {},
                               **({"props": {"y_sort_enabled": True}}
                                  if iso and lname != "decals" else {})})
                built["cells"] += part["cells"]
                built["mirrored"] += part["mirrored"]
                for k, v in part["types"].items():
                    built["types"][k] = built["types"].get(k, 0) + v
            prop_report = {"placed": built["types"],
                           "mirrored": built["mirrored"],
                           "purposes": plan_props["purposes"],
                           "layers": plan_props["layers"],
                           "view": plan_props["view"],
                           "skipped": plan_props["skipped"],
                           "checks": plan_props["checks"]}
            if not plan_props["checks"]["still_connected"]:
                raise ValueError(
                    "the props broke the level into more than one region — "
                    "this should be impossible, every solid prop is gated on a "
                    "flood fill, so treat it as a bug and not as a dial")

        # THE CHECK THAT MATTERS. The built-in layouts are complete by
        # construction - every mask has an entry - so "unmapped" can only ever
        # catch a hand-written table. What actually goes wrong is the layout
        # pointing at atlas coordinates the SHEET does not define: Godot places
        # nothing there, reports nothing, and the level is invisible in exactly
        # the places the shape is most complicated. The .tres lists the tiles it
        # defines, so this is knowable before anything is written.
        absent = {}
        for layer in layers:
            want = {(c["source"], c["ax"], c["ay"]) for c in layer["cells"]}
            gaps = sorted(
                (ax, ay) for src, ax, ay in want
                if (ax, ay) not in set(map(tuple,
                                           parsed_set["sources"][src]["tiles"])))
            if gaps:
                absent[layer["name"]] = [list(g) for g in gaps]
        if absent:
            raise ValueError(
                f"{tiles_res} does not define these atlas tiles: "
                + "; ".join(f"{name} wants {coords}"
                            for name, coords in absent.items())
                + ". A cell pointing at an undefined tile draws nothing and "
                  "says nothing - add the tiles to the atlas, move the layout "
                  "with *_atlas_x/_atlas_y, or change wall_layout.")

        # THE LAYERS THIS GENERATOR OWNS, so a run that produces fewer than the
        # last one removes what it no longer makes. Turning props off left the
        # old prop and decal layers in the scene, still drawing, and the scene
        # loads perfectly — the level just quietly keeps dressing nobody asked
        # for.
        owns = [floor_name, wall_name, prop_name,
                f"{prop_name}{'decals'.title()}",
                # TERRACE IS OWNED TOO, and leaving it off was the same bug
                # this list exists to prevent, committed one layer later. A
                # level generated with levels=2 and then regenerated FLAT
                # kept its raised layer: 300 blocks of a previous elevation
                # still drawing over the new floor, which reads as a second
                # storey hanging off the map and notches the walls wherever
                # a stale block overlaps one. The scene loads perfectly.
                "Terrace"]
        wired = _scenewire.wire_tilemap(text, tiles_res, layers, parent=parent,
                                        owns=owns)
        if dry_run:
            written = {"written": False, "backup": None}
        elif fresh:
            # A brand-new scene has no previous bytes to back up, and apply()
            # refuses a missing file on purpose - that refusal is what catches a
            # typo'd path everywhere else.
            scene_disk.parent.mkdir(parents=True, exist_ok=True)
            scene_disk.write_text(wired["text"], encoding="utf-8")
            # The evidence gate reads the harness's writelog, and a fresh
            # scene bypasses scenewire.apply (which notes its own writes).
            _note_tool_write(_root(), scene_disk)
            written = {"written": True, "backup": None, "created": True}
        else:
            written = _scenewire.apply(scene_disk, wired["text"], root=_root())

        result = {
            "ok": True, "scene": scene_res, "tileset": tiles_res,
            # Which layout authority drew this: True = the tileset's own
            # manifest, False = the explicit floor_*/wall_* arguments.
            "manifest_used": manifest_used,
            "seed": seed, "size": [width, height],
            "rooms": len(level["rooms"]),
            "corridors": len(level["corridors"]),
            "tile_variants": varied,
            # WHICH ROOMS ARE THE ROUTE AND WHICH ARE THE DETOUR. A caller
            # dressing a floor wants that distinction — the critical path
            # earns the set pieces — and it is free here because the layout
            # knew it while it was building.
            **({"main_path": level["main_path"],
                "side_rooms": level["side"]}
               if level.get("main_path") is not None else {}),
            **({"iso_walls": iso_walls} if iso_walls else {}),
            **({"elevation": {
                "levels": levels,
                "raised_cells": len(terraced["heights"]),
                "ramps": terraced["ramps"],
                "reachable": True}} if terraced else {}),
            "connected": level["connected"],
            "spawn": level["spawn"], "exit": level["exit"],
            "layers": wired["layers"], "summary": wired["summary"],
            "written": written.get("written", False),
            "backup": written.get("backup"),
            "created": bool(written.get("created")),
            "dry_run": bool(dry_run),
            "ascii": _levelgen.ascii_map(level),
        }
        if props:
            result["props"] = prop_report
        if not dry_run:
            _log("level", f"generated {width}x{height} level seed {seed} "
                          f"({len(level['rooms'])} rooms) into {scene_res}",
                 ref=scene_res)
        return result
    except Exception as exc:
        return _fail(exc)

# ---------------------------------------------------------------------------
# Godot capture + evidence - moved here by the original split; the
# isometric branch never touched them, so these are main's copies
# (with the evidence-gate writelog notes) restored after a bad merge
# dropped them from both files.
# ---------------------------------------------------------------------------
@_tool
def godot_screenshot(godot_project: str, at: float = 1.0, scene: Optional[str] = None,
                     label: str = "", timeout: int = 120) -> dict:
    """Run the ACTUAL game and capture the viewport to a PNG at `at` seconds.

    The look-iteration loop: headless checks prove the game boots, this shows
    what it LOOKS like. A game window appears briefly on the user's screen
    (rendering needs a display) and closes itself after the capture. The shot
    is archived to the preview gallery - check it before and after visual work.

    THE WINDOW NEVER GAINS TRUE FOREGROUND FOCUS ON WINDOWS, AND THAT IS THIS
    TOOL'S ARTIFACT, NOT YOUR GAME'S. Read this before "fixing" anything it
    seems to reveal about input.

    The capture window is spawned by a background process, so Windows does not
    hand it the foreground. The mouse is therefore never captured:
    `Input.mouse_mode` stays VISIBLE no matter what `_ready` asked for, and any
    code gated on MOUSE_MODE_CAPTURED - a viewfinder, a first-person look
    controller, a pointer-lock HUD - collapses in the shot while working
    perfectly for a human running the same build.

    Measured cost of not knowing: a previous pass "fixed" this by re-asserting
    mouse capture EVERY FRAME from the shot rig, with a comment blaming the
    game. That masked the real finding for a whole pass. **When a fix has to run
    every frame forever, it is a symptom, not a cure.**

    So: do not conclude anything about input capture from a screenshot, and do
    not add per-frame re-capture to make one look right. To check input for
    real, run the game with `godot_run` and assert on state, or have a human
    play it. `focus` in the result says this out loud on every call.

    Two more things this tool cannot give you. It returns NO STDOUT, so a
    windowed run's diagnostics must be written to `user://` and read back. And
    `res://.bgate_shot.gd` is the harness's own helper - it can error during
    unrelated headless runs; work around it, do not delete it.

    godot_project: the directory holding project.godot.
    """
    try:
        _contained_path(godot_project, "godot_project")
        # One file per capture. A single shot.png meant two seats screenshotting
        # at the same moment each got back a path holding the OTHER one's game.
        out = str(_Path(_root()) / ".bgate_out" / "shots" /
                  f"{_run_tag(label or 'game')}.png")
    except Exception:
        out = f"bgate_shot_{_run_tag()}.png"
    try:
        result = _godot.screenshot(godot_project, out, at=at, scene=scene,
                                   timeout=timeout)
        if result.get("ok"):
            _note_tool_write(_root(), result["path"])
            archived = _archive_preview(result["path"], f"shot-{label or 'game'}")
            if archived:
                result["preview"] = archived
            _log("screenshot", f"captured the running game at t={at}s"
                               + (f" ({label})" if label else ""),
                 ref=archived or result["path"])
        # ON EVERY RESULT, not only when something looks wrong. The failure this
        # prevents is a correct-looking screenshot being read as evidence about
        # input - by which point the misreading has already been acted on. A
        # caveat that only appears once you suspect a problem arrives after the
        # bug report has been written.
        result["focus"] = {
            "foreground": False,
            "mouse_capture": "unavailable",
            "note": ("this window never takes true foreground focus on Windows, "
                     "so Input.mouse_mode stays VISIBLE and anything gated on "
                     "MOUSE_MODE_CAPTURED will not appear in this shot. That is "
                     "the harness, not the game - do not fix the game for it, "
                     "and do not re-assert capture per frame to make the shot "
                     "look right. Check input with godot_run and an assertion, "
                     "or with a human at the keyboard."),
        }
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def godot_inspect_resource(godot_project: str, res_path: str, timeout: int = 180) -> dict:
    """Load a res:// resource in-engine and report what it actually became.

    Meshes, tri counts, per-surface UV/material, bounding box - the engine's
    view of an asset already in the project.

    godot_project: the directory holding project.godot.
    """
    _contained_path(godot_project, "godot_project")
    return _godot.inspect_resource(godot_project, res_path, timeout=timeout)


@_tool
def godot_retarget_check(godot_project: str, res_path: str,
                         bone_map_res: str = "", timeout: int = 180) -> dict:
    """Ask the ENGINE whether a rigged character is a humanoid it can retarget.

    The rigs this pipeline builds carry Godot's own SkeletonProfileHumanoid bone
    names, and the whole point of that is that any humanoid animation library
    then plays on the character. Nothing tested that claim until this tool. A
    .glb can export 23 perfectly-named bones in a FLAT hierarchy - blender_rig
    reports 0 unweighted, godot_deliver_asset photographs it happily, and the
    character can be animated by nothing except a clip authored for it alone.

    Three answers, and they fail independently:

      missing / extra   coverage against the profile, by exact name.
      chain[].propagates  rotating a shoulder moves the hand. This is the one
                        that catches a lost hierarchy, and it is invisible to
                        every other check in the product.
      clip.drives       a profile-authored rotation track actually turns the
                        bone. A NodePath that resolves to nothing plays
                        silently and moves zero.

    `retargetable` is the verdict. False means the humanoid animation ecosystem
    is unavailable to this asset - treat it the way you treat `rigged: false`.

    bone_map_res: a res:// path to save the BoneMap to, or "" to skip. Written,
    it is what the user's import settings point at to retarget real clips.

    res_path must already be imported - godot_import_asset first.
    """
    _contained_path(godot_project, "godot_project")
    result = _godot.retarget_check(godot_project, res_path,
                                   bone_map_res=bone_map_res,
                                   timeout=timeout)
    if result.get("ok"):
        _log("godot",
             f"retarget check {res_path}: "
             f"{'retargetable' if result.get('retargetable') else 'NOT retargetable'} "
             f"({result.get('mapped')}/{result.get('profile_bones')} profile bones)",
             ref=res_path)
    return result


def _annotate_evidence(result: dict, godot_project: str,
                       default_run: bool) -> dict:
    """Default-scene proof, and the honest 2D/3D caveat. Never raises."""
    try:
        from bgate_core import project as _project, sceneproof as _proof
    except Exception:                                             # noqa: BLE001
        return result

    dimension = ""
    try:
        dimension = str((_project.get(_root()) or {}).get("dimension") or "")
    except Exception:                                             # noqa: BLE001
        pass

    # NOT `ok: true` WITH NOTHING IN IT. The manifest walk reports screen-space
    # bounds, which is a 2D question; on a 3D project an empty `entities` means
    # "this walk could not see the world", not "the world is empty".
    if dimension == "3d" and not (result.get("entities") or {}):
        result["entities_note"] = (
            "`entities` is EMPTY and this is a 3D project. That is a limit of "
            "this tool, NOT a fact about the scene: the manifest walk reports "
            "screen-space bounds and is not authoritative about 3D contents. "
            "Measure the world with godot_inspect_resource (engine mesh and "
            "collider bounds) or read the frame. It used to come back as "
            "ok:true with entities:{} on a populated scene, which reads as a "
            "verdict and is not one.")
        result["entities_authoritative"] = False
    elif dimension == "3d":
        result["entities_authoritative"] = False

    if not default_run:
        result["default_scene_proof"] = False
        result["why_not_proof"] = (
            "a named scene was captured, so this does NOT satisfy the release "
            "gate's default-scene row. Call godot_evidence with no `scene` to "
            "launch what the player actually boots into.")
        return result

    state = _proof.default_scene_state(_root())
    result["default_scene_proof"] = True
    result["declared_scene"] = state.get("scene", "")
    result["scaffold"] = state.get("scaffold", False)
    if state.get("scene") and result.get("beauty"):
        result["beauty_digest"] = _proof.digest(result["beauty"])
    if not state.get("ok"):
        result["ok"] = False
        result["error"] = state.get("why") or result.get("error", "")
    try:
        _proof.record_capture(_root(), state.get("scene", ""), result)
    except Exception:                                             # noqa: BLE001
        pass
    result["next"] = ("evidence_assert(scene, frame, says=...) - say what is "
                      "IN this frame. A captured file nobody opened is not "
                      "evidence, and the release gate holds until somebody "
                      "has said something about it.")
    return result


@_tool
def godot_evidence(godot_project: str, at: float = 1.0, scene: Optional[str] = None,
                   overlay: bool = True, label: str = "",
                   timeout: int = 120) -> dict:
    """Capture a frame PLUS a screen-space manifest of what is actually where.

    The upgrade over godot_screenshot. A PNG shows what the game looks like; it
    cannot tell you whether the health bar matches the fighter's real hp,
    whether a hitbox lines up with its sprite, or whether an entity is on
    screen at all. This runs the game the same way, then walks the live tree at
    capture time and reports every measurable node as screen-pixel bounds,
    visibility, z, and - for progress bars and labels - its RUNTIME VALUE.

    Returns beauty.png, an overlay.png with collision shapes (red) and other
    bounds (blue) stroked over the frame, and manifest.json with `entities` and
    `ui`. Pair with `causal_chains` - the manifest says what was on screen, the
    chains say why it happened.

    CALL IT WITH NO `scene` TO PROVE THE DEFAULT. That is the capture the
    release gate requires and the one nobody was taking: a 3D benchmark shipped
    with `application/run/main_scene` still pointing at the scaffold demo while
    every named-scene test passed, because each of those tests named the scene
    it tested and none of them named the one the game boots into. With no
    `scene` this launches exactly what pressing play launches, records the
    proof against the release gate, and FAILS if the default scene is missing,
    broken, or still the template's demo room. Named-scene evidence does not
    substitute for it.

    ENTITIES CAN BE EMPTY ON A POPULATED 3D SCENE, and that used to come back
    as `ok: true` with `entities: {}`. The manifest walk is screen-space and
    2D-shaped; on a 3D project it is not authoritative about what is in the
    world. The result now says so rather than reading as "this scene has
    nothing in it" - measure 3D contents with godot_inspect_resource.

    THE CAPTURE IS NOT THE EVIDENCE. Record what you saw in the frame with
    evidence_assert(scene, frame, says=...) - a file on disk that nobody opened
    is what let a two-tailed character ship for a day.

    godot_project: the directory holding project.godot.
    """
    try:
        _contained_path(godot_project, "godot_project")
        out_dir = str(_Path(_root()) / ".bgate_out" / "evidence" /
                      _run_tag(label or "frame"))
    except Exception:
        out_dir = f"bgate_evidence_{_run_tag()}"
    default_run = not scene
    try:
        result = _godot.evidence(godot_project, out_dir, at=at, scene=scene,
                                 overlay=overlay, timeout=timeout)
        result = _annotate_evidence(result, godot_project, default_run)
        if result.get("ok"):
            for key, tag in (("beauty", "beauty"), ("overlay", "overlay")):
                path = result.get(key)
                if path:
                    archived = _archive_preview(
                        path, f"evidence-{tag}-{label or 'frame'}")
                    if archived:
                        result[f"{key}_preview"] = archived
            counts = result.get("counts", {})
            _log("evidence",
                 f"captured {counts.get('entities', 0)} entities / "
                 f"{counts.get('ui', 0)} ui elements at t={at}s"
                 + (f" ({label})" if label else ""),
                 ref=result.get("beauty_preview") or result.get("beauty") or "")
        return result
    except Exception as exc:
        return _fail(exc)


@_tool
def evidence_check_ui(manifest_path: str, expect: dict,
                      tolerance: float = 0.5) -> dict:
    """Assert HUD values from an evidence manifest against expected state.

    `expect` maps a UI node name to the value it should be showing, e.g.
    {"PlayerHealth": 92}. Numeric checks use `tolerance` so a bar mid-tween
    does not fail as a bug. This is the assertion godot_screenshot could never
    support: proof the HUD agrees with the sim, not a picture of a bar.
    """
    manifest = _json.loads(_Path(manifest_path).read_text(encoding="utf-8"))
    return _godot.check_ui_matches(manifest, expect, tolerance=tolerance)


# ---------------------------------------------------------------------------
# Traversal — can the player actually GET THERE
# ---------------------------------------------------------------------------
# A NEW VERB, AND THE ONLY ONE THIS PASS ADDS. Nothing existing could express
# it: `godot_run` runs a script, `godot_evidence` photographs a frame, and the
# geometry tools measure shapes. "Drive the real controller with real input
# from the real launch surface and prove it ENDS UP SETTLED inside the
# destination's own volume" is a different question from all three, and the
# four-of-six false-green climbing routes are what happens when it is answered
# by proxy.
@_tool
def traversal_prove(godot_project: str, scene: str, launch: str,
                    destination: str, inputs: list, name: str = "route",
                    player: str = "", player_script: str = "",
                    settle_frames: int = 3, timeout: int = 180) -> dict:
    """DRIVE THE PLAYER and prove it arrives — settled, in the real volume.

    FOUR OF SIX CLIMBING ROUTES PASSED AND WERE NOT TRAVERSABLE. The tests
    measured vertical rise against jump height. None measured the horizontal
    edge gap, the launch surface size, the landing pad size, the player's own
    body width, or what the controller does when you hold the stick that way.

    Then the gate written to catch that produced its own false green, which is
    the sharpest defect in the whole benchmark: the driver accepted arrival on
    ANY frame where the body was near the target - including mid-ballistic-arc
    during a jump that MISSED. Requiring `is_on_floor` was not enough either: a
    scripted mantle carries that flag from before it began, so the check passed
    mid-interpolation while an AnimationPlayer was lerping the body through the
    air.

    So arrival here is three conditions, all required:

      IN THE VOLUME  inside `destination`, which must be the destination's OWN
                     Area2D/Area3D - the volume the GAME uses to know the
                     player is there. Not a marker plus a radius; a radius
                     around a point is what passed mid-jump.
      SETTLED        grounded AND not inside any scripted or interpolated move
      HELD           for `settle_frames` CONSECUTIVE frames. One frame is a
                     sample of a trajectory; N frames is a state.

    YOUR CONTROLLER MUST SAY WHEN IT IS BUSY. Nothing outside a controller can
    tell "standing on the ledge" from "being lerped by a mantle" - that is
    exactly why the grounded check failed. Expose
    `func is_in_scripted_move() -> bool` (or is_busy / is_traversal_busy /
    is_animation_driving / is_scripted_move_active) returning true while any
    scripted move owns the body. Pass `player_script` and this REFUSES a
    controller without one rather than sampling it naively, because sampling it
    naively is the bug.

    `inputs` is the real input program through the real input map:
    [{"action": "move_forward", "frames": 20}, {"action": "jump", "frames": 1}]

    Bounded by construction: the run stops at a frame ceiling and prints a
    heartbeat, because an unbounded wait-until-condition loop is
    indistinguishable from a hang and three agents were killed after 25 minutes
    of silence having written nothing.

    Geometry (rise, gap, pad sizes, player bounds) is reported when available
    as EXPLANATION. It is never the verdict.
    """
    from bgate_core import traversal as _traversal

    _contained_path(godot_project, "godot_project")
    try:
        spec = _traversal.route(name=name, scene=scene, launch=launch,
                                destination=destination, inputs=list(inputs),
                                player=player, settle_frames=settle_frames)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if player_script:
        source = ""
        for candidate in (_Path(player_script),
                          _Path(godot_project) / player_script,
                          _Path(godot_project) /
                          player_script.replace("res://", "")):
            if candidate.is_file():
                source = candidate.read_text(encoding="utf-8", errors="replace")
                break
        if not source:
            return {"ok": False,
                    "error": f"player_script {player_script} is not readable"}
        try:
            _traversal.require_controller(source)
        except _traversal.ControllerRefused as exc:
            return {"ok": False, "controller_refused": True,
                    "error": str(exc)}

    out_path = _Path(_root()) / ".bgate_out" / "traversal" / f"{_run_tag(name)}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    driver = _traversal.driver_source(spec, out_path.as_posix())
    ran = _godot.run_script(driver, project_dir=godot_project, timeout=timeout)

    payload = {"ok": False, "error": "the driver wrote no result"}
    if out_path.exists():
        try:
            payload = _json.loads(out_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            payload = {"ok": False, "error": f"unreadable driver output: {exc}"}
    if not ran.get("ok") and not payload.get("samples"):
        payload = {"ok": False,
                   "error": str(ran.get("error") or "the driver did not run"),
                   "engine": {"stdout": (ran.get("stdout") or "")[-1200:],
                              "errors": ran.get("errors") or []}}

    verdict = _traversal.verdict(spec, payload)
    verdict["route_spec"] = {k: spec[k] for k in
                             ("name", "scene", "launch", "destination",
                              "settle_frames", "max_frames")}
    verdict["driver_output"] = str(out_path)
    verdict["engine_contended"] = bool(ran.get("engine_contended"))
    _log("traversal",
         f"{name}: {'REACHABLE' if verdict['ok'] else 'NOT REACHABLE'} — "
         f"{verdict.get('why') or 'settled in the destination volume'}",
         ref=scene)
    return verdict
