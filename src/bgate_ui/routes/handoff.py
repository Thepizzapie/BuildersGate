"""Handoff — the verb that turns a made thing into a thing that is IN the game.

THE COMPLAINT THIS ANSWERS: "rn i can go in sprite sheet edit and create sprites
and save but dont know what i can do after. Audio lab I have no idea how to save
or wire any of that up to specific scenes or triggers … same for sprite and 3d
model editor."

Every editor in this dashboard dead-ended at "saved". The tools that finish the
job all existed — /api/scene/wire, /api/sprite/spriteframes, the Godot deliver
path, the queue — and not one of them was reachable from the place where the
asset had just been made. The asset sat on disk referenced by nothing, and the
next step was a different view, a different vocabulary, or Godot.

MODELLED ON storyboard.promote. That is the one place in this product where a
free thing crosses a boundary and becomes a committed one, and the reason the
cinematic seat reads as finished is that the crossing has a NAME and a button.
This is the same shape for assets, with two exits instead of one:

  A. wire it here — local, free, mechanical, dry-run first;
  B. hand it to an agent — a work item whose brief is already filled in with
     the asset path, the target scene, the trigger and the references, so the
     agent does not re-derive what was on screen and the human does not retype
     it.

ONE endpoint answers everything the panel needs to open (``/api/handoff/context``)
because the alternative is five round trips before a single button can be drawn,
and the panel opens on a click.

Nothing here generates anything. Godot and Blender are the only engines it
touches and both are local; there is no provider call on any path through this
file, deliberately — the moment "put this in the game" can cost money it stops
being the thing you press without thinking.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Sequence

from fastapi import APIRouter, Query, Request

from bgate_ui import api
from bgate_ui.deps import root
# IMPORTED, NOT RE-DERIVED. Two modules that disagree about which directory
# res:// means are two modules that hand each other paths the other cannot
# resolve — scenewire.py already settled that question and godot_ws.py already
# settled how a slow engine call is run without pinning a worker.
from bgate_ui.routes.scenewire import (SKIP_DIRS, _as_res, _godot_dir, _lock,
                                       _resolve)

router = APIRouter()


# ---------------------------------------------------------------------------
# What kind of thing is this, and what does the engine do with it
# ---------------------------------------------------------------------------
# Deliberately the same suffix vocabulary scenewire._BY_SUFFIX uses, because the
# panel's whole promise is that the button it draws is the call it will make.
KIND_BY_SUFFIX = {
    ".png": "sprite", ".webp": "sprite", ".jpg": "sprite", ".jpeg": "sprite",
    ".svg": "sprite",
    ".ogg": "audio", ".wav": "audio", ".mp3": "audio",
    ".glb": "mesh", ".gltf": "mesh", ".obj": "mesh", ".fbx": "mesh",
    ".tres": "resource", ".tscn": "scene", ".gd": "script",
}

# node type -> the property the resource lands in, mirroring scenewire. The
# panel offers these as the "or put it in a …" override; anything not here is
# not offered, because an override that produces a null property is a node that
# draws nothing and says nothing.
NODE_CHOICES = {
    "sprite": [("Sprite2D", "texture"), ("TextureRect", "texture"),
               ("Sprite3D", "texture")],
    "audio": [("AudioStreamPlayer2D", "stream"), ("AudioStreamPlayer", "stream"),
              ("AudioStreamPlayer3D", "stream")],
    "resource": [("AnimatedSprite2D", "sprite_frames"),
                 ("AnimatedSprite3D", "sprite_frames")],
    "scene": [("(instance)", "")],
    "mesh": [],
    "script": [],
}

# Which seat does the WIRING, not which seat made the asset. Art made the sheet;
# putting it in a scene is gameplay work, and filing it to art is how a task
# lands on the seat that has nothing left to do with it.
SEAT_BY_KIND = {
    "sprite": "gameplay", "resource": "gameplay", "scene": "gameplay",
    "audio": "audio", "mesh": "gameplay", "script": "tech", "other": "tech",
}

# Properties the panel is willing to set, per kind. Every one of these goes
# through /api/scene/node/property, which is a real mechanical edit with a
# backup — nothing here needs a line of GDScript, which is exactly the line
# between exit A and exit B.
#
# `literal` IS NOT DECORATION. scenewire._prop_value refuses anything that is
# not already valid Godot literal syntax — deliberately, because a malformed
# property does not fail at save, it fails when the engine next loads the
# scene. So a text field's contents have to be spelled the way the .tscn spells
# them: "idle" for a String and &"Music" for a StringName. Sending the bare word
# is a 400 that reads like the property is not supported, which is what the
# first cut of this panel did to every animation name.
PROPS_BY_KIND = {
    "audio": [
        {"key": "autoplay", "label": "play as soon as the scene loads",
         "type": "bool", "default": False},
        {"key": "volume_db", "label": "volume (dB)", "type": "number",
         "default": 0},
        {"key": "bus", "label": "audio bus", "type": "string",
         "literal": "stringname", "default": "",
         "hint": "Master, Music, SFX - whatever this project's audio bus layout names"},
    ],
    "resource": [
        {"key": "autoplay", "label": "animation to autoplay", "type": "string",
         "literal": "stringname", "default": "",
         "hint": "one of the animation names in the SpriteFrames"},
        {"key": "centered", "label": "centred on its position", "type": "bool",
         "default": True},
    ],
    "sprite": [
        {"key": "centered", "label": "centred on its position", "type": "bool",
         "default": True},
    ],
}

_TYPE_RE = re.compile(r'^\[node\s+name="(?P<name>[^"]+)"\s+type="(?P<type>[^"]+)"'
                      r'(?P<rest>[^\]]*)\]', re.MULTILINE)


def _kind_of(target: Path, override: str = "") -> str:
    if override:
        return override
    return KIND_BY_SUFFIX.get(target.suffix.lower(), "other")


def _rel(project_root: Path, target: Path) -> str:
    return target.relative_to(project_root).as_posix()


def _scan(project_root: Path, asset_res: Optional[str],
          want_types: Sequence[str]) -> tuple[list[dict], Optional[dict]]:
    """One walk of every .tscn: the scene list AND a worked example.

    ``/api/scene/wirable`` already produces the first half, and calling it would
    be a second full walk of a tree that is thousands of files on a real
    project. The second half is the thing an agent brief is useless without: a
    place in THIS project where a node of the type we are about to add already
    exists, so "matching the pattern at …" names a real file instead of a
    principle.

    EVERY type the panel would accept is looked for, not just the default one.
    Asking only about AudioStreamPlayer2D on a project whose sounds all hang off
    plain AudioStreamPlayer returns "no example" while the example is right
    there — and a brief that says "follow the existing pattern" without naming
    one is a brief that sends the agent looking.
    """
    gd = _godot_dir(project_root)
    wanted = {t for t in want_types if t and t != "(instance)"}
    scenes: list[dict] = []
    example: Optional[dict] = None
    for p in sorted(gd.rglob("*.tscn")):
        if SKIP_DIRS & set(p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "[gd_scene" not in text:
            continue
        res = f"res://{p.relative_to(gd).as_posix()}"
        rel_dir = str(Path(res[len("res://"):]).parent.as_posix())
        scenes.append({
            "scene": res,
            "label": p.stem,
            "dir": rel_dir,
            "nodes": text.count("[node "),
            "has_asset": bool(asset_res) and f'path="{asset_res}"' in text,
        })
        if example is None and wanted:
            m = next((m for m in _TYPE_RE.finditer(text)
                      if m.group("type") in wanted), None)
            if m:
                example = {"scene": res, "node": m.group("name"),
                           "type": m.group("type")}

    # ORDERED THE WAY THE QUESTION IS ASKED, not alphabetically. A real project
    # answers this with a hundred .tscn files of which most are one-node probes
    # and test fixtures (119 on the project this was written against, 90 of them
    # under assets/lights), and an A-Z list buries every scene a person would
    # actually wire music into below sixty of them. Scenes under scenes/ first,
    # then the ones that are actually built out.
    scenes.sort(key=lambda s: (0 if s["dir"].split("/")[0] == "scenes" else 1,
                               -s["nodes"], s["label"].lower()))

    # NOT EVERY PATTERN LIVES IN A SCENE. This project — the one this was built
    # against — has no AudioStreamPlayer in any .tscn at all; every sound is
    # constructed in scripts/audio.gd. Reporting "no example" there is
    # technically true and useless: the pattern exists, it is just in GDScript,
    # and that is the file the brief has to name. Only walked when the scene
    # pass found nothing.
    if example is None and wanted:
        for p in sorted(gd.rglob("*.gd")):
            if SKIP_DIRS & set(p.parts):
                continue
            try:
                body = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            hit = next((t for t in sorted(wanted) if t in body), None)
            if hit:
                example = {"scene": f"res://{p.relative_to(gd).as_posix()}",
                           "node": "", "type": hit, "in_script": True}
                break
    return scenes, example


def _sprite_sidecar(target: Path) -> Optional[Path]:
    """A SpriteFrames resource for this sheet, if one already exists.

    NOT JUST THE NAME /api/sprite/spriteframes WOULD EMIT. That endpoint writes
    ``<stem>_frames.tres``, so for ``idle_sheet.png`` it writes
    ``idle_sheet_frames.tres`` — and the sheet on the project this was written
    against already had ``idle_frames.tres`` beside it, written by the sprite
    pipeline under a different convention. Matching only the one name reported
    "no SpriteFrames yet" while one sat in the same directory, which is the
    panel offering to build a thing that exists.

    So: prefer the exact name, then any sibling .tres that actually references
    this sheet. Referencing it is the fact that matters; the filename is a
    convention two producers already disagree about.
    """
    exact = target.with_suffix("").with_name(target.stem + "_frames.tres")
    if exact.is_file():
        return exact
    needle = f'"{target.name}"'
    try:
        siblings = sorted(target.parent.glob("*.tres"))
    except OSError:
        return None
    for cand in siblings:
        try:
            head = cand.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        if "SpriteFrames" in head and (needle in head or target.name in head):
            return cand
    return None


def _mesh_scene(project_root: Path, target: Path) -> Optional[Path]:
    """The .tscn godot_deliver_asset writes for a model, if it has been run.

    A .glb cannot be wired into a scene directly — scenewire refuses the suffix,
    correctly, because Godot's importer turns it into a PackedScene and the
    thing you instance is that scene. So the mesh path is two steps and the
    panel has to know which one it is on.
    """
    gd = _godot_dir(project_root)
    for cand in (gd / "scenes" / f"{target.stem}.tscn",
                 gd / "assets" / f"{target.stem}.tscn"):
        if cand.is_file():
            return cand
    return None


@router.get("/api/handoff/context")
def handoff_context(path: str, kind: str = Query("")) -> dict:
    """Everything the handoff panel needs to draw itself, in one request.

    ``path`` is project-relative or res://. ``kind`` overrides the suffix guess
    for the rare case where the caller knows better (an editor holding a sheet
    it has not written yet).
    """
    project_root = root()
    target = _resolve(project_root, path, must_exist=False)
    exists = target.is_file()

    gd = _godot_dir(project_root)
    in_godot = target.resolve().is_relative_to(gd.resolve())
    asset_res = _as_res(project_root, target) if in_godot else None

    k = _kind_of(target, kind)
    choices = NODE_CHOICES.get(k, [])
    default_type = choices[0][0] if choices else ""

    # PREREQUISITES, not decoration. Each of these is a step that has to happen
    # before /api/scene/wire will accept the file at all, and the panel refuses
    # to offer a wire button until it is satisfied — an offer that can only 400
    # is worse than no offer.
    steps: list[dict] = []
    targets: list[dict] = []
    wire_path = asset_res

    def _target(res: str, name: str, tk: str, label: str) -> dict:
        ch = NODE_CHOICES.get(tk, [])
        return {"res": res, "name": name, "kind": tk, "label": label,
                "node_type": ch[0][0] if ch else "",
                "choices": [{"type": t, "property": p} for t, p in ch],
                # The properties travel WITH the target: an AnimatedSprite2D
                # takes `autoplay` as an animation name, a Sprite2D has no such
                # property at all, and offering the wrong set writes a property
                # the node does not have.
                "props": PROPS_BY_KIND.get(tk, [])}

    if k == "sprite":
        side = _sprite_sidecar(target)
        steps.append({
            "id": "spriteframes",
            "label": "build a SpriteFrames resource from the labelled animations",
            "why": "an animated sprite needs a .tres; a bare sheet wires as one "
                   "static Sprite2D showing the whole grid",
            "done": bool(side),
            "target": _rel(project_root, side) if side else None,
            "endpoint": "/api/sprite/spriteframes",
            "optional": True,
        })
        if side and in_godot:
            # TWO HONEST ANSWERS, so the panel offers both rather than picking
            # for you. A sheet with a SpriteFrames beside it is almost always
            # meant to be wired as the SpriteFrames — that is what makes it
            # animate — but "drop the whole sheet in as one Sprite2D" is a real
            # thing people do for a background or a static prop, and silently
            # doing the other one is the surprise this panel exists to remove.
            side_res = _as_res(project_root, side)
            targets = [
                _target(side_res, side.name, "resource",
                        "the SpriteFrames beside it - an AnimatedSprite2D that "
                        "plays the labelled animations"),
                _target(asset_res, target.name, "sprite",
                        "the raw sheet - one static Sprite2D showing the whole grid"),
            ]
            wire_path = side_res
            choices = NODE_CHOICES["resource"]
            default_type = choices[0][0]
    elif k == "mesh":
        scene = _mesh_scene(project_root, target)
        steps.append({
            "id": "deliver",
            "label": "import it into the engine and give it a scene",
            "why": "a .glb is not a node - Godot imports it as a PackedScene and "
                   "the thing a level instances is that scene",
            "done": bool(scene),
            "target": _rel(project_root, scene) if scene else None,
            "endpoint": "/api/handoff/mesh/deliver",
            "optional": False,
        })
        wire_path = _as_res(project_root, scene) if scene else None

    wirable = bool(wire_path)
    why = ""
    if not exists:
        why = "there is no file at that path yet - save it first"
    elif k == "mesh" and not wire_path:
        # BEING OUTSIDE THE PROJECT IS THE NORMAL CASE FOR A MODEL, not a
        # problem to report. Blender and the generators write to a staging
        # directory (art/3d_inbox on the project this was built against), and
        # godot_import_asset REFUSES a source already inside the project —
        # copying a file onto itself is a Windows error. So the mesh branch
        # answers before the in_godot check, which would otherwise tell a person
        # holding a perfectly normal .glb that their file is in the wrong place.
        why = ("run the engine import first - it copies the model into the "
               "project and writes the scene a level can instance")
    elif not in_godot:
        why = ("this file is not inside the Godot project, so no scene can "
               "reference it")
    elif k in ("script", "other"):
        why = f"there is no mechanical way to wire a {target.suffix or 'file'} " \
              "into a scene - hand it to an agent instead"
        wirable = False

    scenes, example = _scan(project_root, wire_path,
                            [t for t, _p in choices] if k != "scene" else [])

    return api.ok({
        "asset": {
            "rel": _rel(project_root, target) if target.is_relative_to(project_root)
                   else str(target),
            "res": asset_res,
            "wire_res": wire_path,
            "name": target.name,
            "stem": target.stem,
            "suffix": target.suffix.lower(),
            "kind": k,
            "exists": exists,
            "bytes": target.stat().st_size if exists else 0,
            "in_godot": in_godot,
            "preview": _rel(project_root, target)
                       if exists and k == "sprite" else None,
        },
        "wire": {
            "ok": wirable,
            "why": why,
            "node_type": default_type,
            "choices": [{"type": t, "property": p} for t, p in choices],
            # Empty unless the asset genuinely has more than one honest engine
            # form. The panel only draws a chooser when there is a choice.
            "targets": targets,
            "props": PROPS_BY_KIND.get(k, []),
            "suggested_name": re.sub(r"[^A-Za-z0-9_]+", "", target.stem.title()
                                     .replace("_", "")) or "Asset",
        },
        "steps": steps,
        "scenes": scenes,
        "example": example,
        "seat": SEAT_BY_KIND.get(k, "tech"),
        "godot_dir": str(gd),
    })


@router.get("/api/handoff/scene")
def handoff_scene(scene: str) -> dict:
    """The parents a wire can hang off, plus whether the file is claimed.

    A thin read over the same parse /api/scene/outline uses. It exists so the
    panel's second step is one small request rather than the full outline
    payload — resources, previews, properties and all — of which it would use
    the node names and nothing else.
    """
    from bgate_core.level import scenewire as _sw

    project_root = root()
    target = _resolve(project_root, scene)
    if target.suffix.lower() != ".tscn":
        raise api.bad_request("not a scene file", scene=scene)
    text = target.read_text(encoding="utf-8", errors="replace")
    try:
        parsed = _sw.parse(text)
    except _sw.WireError as exc:
        raise api.bad_request(str(exc), scene=scene)
    return api.ok({
        "scene": _as_res(project_root, target),
        "rel": _rel(project_root, target),
        "root": parsed["root"],
        "lock": _lock(project_root, target),
        "nodes": [{"path": _sw.node_path(n), "name": n["name"],
                   "type": n["type"] or ("(instance)" if n["instance"] else "")}
                  for n in parsed["nodes"]],
    })


@router.post("/api/handoff/mesh/deliver")
def handoff_mesh_deliver(payload: dict, request: Request,
                         async_: int = Query(0, alias="async")):
    """Import a model into the engine and give it a scene, collider and frame.

    Wraps godot.deliver_asset — the local, free end of the 3D path. It is the
    only thing in this file that runs for minutes, so it takes the same job
    treatment every other engine call in godot_ws.py does: ``?async=1`` starts a
    job and answers 202 rather than pinning a worker for five minutes.

    ``dry_run`` reports what it WOULD write and touches nothing. It defaults on
    at the caller, because this writes into the game project and the person
    pressing it has usually never seen what "deliver" means.
    """
    from bgate_adapters import godot as _godot
    from bgate_ui.routes.godot_ws import _async_202, _guard, _project, clamp_timeout

    project_root = root()
    src = _resolve(project_root, str(payload.get("path") or ""))
    p = _project(payload.get("project_dir"))
    name = str(payload.get("name") or src.stem)
    dest_rel = str(payload.get("dest_rel") or "assets")
    scene_rel = str(payload.get("scene_rel") or "scenes")

    if payload.get("dry_run"):
        gd = _godot_dir(project_root)
        return api.ok({
            "dry_run": True,
            "plan": [
                f"copy {_rel(project_root, src)} -> res://{dest_rel}/{src.name}",
                "run a headless Godot import and load the result in-engine",
                f"write res://{scene_rel}/{name}.tscn - a body sized from the "
                "mesh, with a collider built from its real geometry",
                "photograph it with Godot's own renderer and report the checks",
            ],
            "writes": [f"{dest_rel}/{src.name}", f"{scene_rel}/{name}.tscn"],
            "project_dir": str(p),
            "existing_scene": (gd / scene_rel / f"{name}.tscn").is_file(),
            "note": "nothing has been written. Run it for real to land the files.",
        })

    timeout = clamp_timeout(payload.get("timeout"), 300)
    call = lambda: _godot.deliver_asset(  # noqa: E731 — the job API wants a thunk
        str(p), str(src), name=name, dest_rel=dest_rel, scene_rel=scene_rel,
        overwrite_scene=bool(payload.get("overwrite_scene")), timeout=timeout)
    if async_ or payload.get("async"):
        return _async_202("godot.deliver", "delivering asset", timeout, call,
                          request_body={"project_dir": str(p),
                                        "path": _rel(project_root, src)},
                          request=request)
    return api.ok(_guard(call))
