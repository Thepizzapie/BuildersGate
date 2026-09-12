"""Wiring endpoints, turn an Atlas edge you can see into an edge that exists.

Atlas derives the whole screen/asset graph by reading scenes and scripts, which
makes it a perfect map and a read-only one: the answer to "this sheet is wired
to nothing" was always "go do it in Godot". These endpoints make the graph
WRITABLE in the one direction that is safely mechanical, adding a node for an
asset, attaching a script, removing a node again.

Every mutation is available as a dry run first and takes a backup when it is
not, because the file being edited is one the engine also owns.

The res:// namespace is the addressing scheme throughout, matching /api/screenmap
- a caller that got a node id out of the Atlas graph can pass it straight back.
"""
from __future__ import annotations

import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

from bgate_core.level import scenedraw, scenedraw3d, scenewire, tilemap
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

# Trees that hold .tscn files which are not scenes of this game. .bgate_out is
# where THIS module's own backups land, so without it every wire would add a
# phantom scene to the next "wire into…" list, each one a copy of a real one.
SKIP_DIRS = {".godot", ".bgate_out", ".bgate", ".git", ".asset_work",
             "export", "build", "__pycache__"}


def _godot_dir(project_root: Path) -> Path:
    """Where res:// points. Same resolution order as bgate_core.art.screenmap."""
    for cand in (project_root, project_root / "game"):
        if (cand / "project.godot").is_file():
            return cand
    hits = [p.parent for p in project_root.glob("*/project.godot")]
    if hits:
        return hits[0]
    raise api.not_found("no project.godot at the root or one level down")


def _resolve(project_root: Path, res_path: str, *, must_exist: bool = True) -> Path:
    """res:// (or a root-relative path) -> a real file inside the project."""
    raw = str(res_path or "").strip()
    if not raw:
        raise api.bad_request("empty path")
    gd = _godot_dir(project_root)
    if raw.startswith("res://"):
        target = (gd / raw[len("res://"):]).resolve()
    else:
        target = (project_root / raw).resolve()
    try:
        target.relative_to(project_root.resolve())
    except ValueError:
        raise api.forbidden("path escapes the project root", path=res_path)
    if must_exist and not target.is_file():
        raise api.not_found(f"no file at {res_path}", path=res_path)
    return target


def _as_res(project_root: Path, target: Path) -> str:
    gd = _godot_dir(project_root)
    try:
        return f"res://{target.relative_to(gd).as_posix()}"
    except ValueError:
        raise api.bad_request(
            f"{target.name} is not inside the Godot project, so no scene can "
            "reference it", path=str(target))


def _lock(project_root: Path, scene_file: Path) -> Optional[dict]:
    """``{seat, owner}`` if a seat holds this scene, else None.

    Reported on every read so the builder can say so BEFORE twenty drags are
    staged against a file the write is going to refuse.
    """
    from bgate_core.store import assets as _assets
    held = _assets.lock_holder(project_root, scene_file)
    return {"seat": held.get("lock_seat"), "owner": held.get("lock_owner")} \
        if held else None


def _res_type(asset_file: Path) -> Optional[str]:
    """The class a .tres declares itself to be. None for anything else.

    Suffix-guessing calls every .tres a SpriteFrames, which is right for what
    the sprite pipeline writes and wrong for the project's TileSet, and an
    ext_resource with the wrong type loads as null, so the node draws nothing
    and says nothing.
    """
    if asset_file.suffix.lower() != ".tres":
        return None
    try:
        head = asset_file.read_text(encoding="utf-8", errors="replace")[:400]
    except OSError:
        return None
    return scenewire.resource_type_of(head)


def _tree(text: str) -> list[dict]:
    parsed = scenewire.parse(text)
    return [{"name": n["name"], "path": scenewire.node_path(n),
             "parent": n["parent"], "type": n["type"] or ("(instance)"
                                                          if n["instance"] else ""),
             "instance": n["instance"]}
            for n in parsed["nodes"]]


@router.get("/api/scene/tree")
def scene_tree(scene: str) -> dict:
    """The node tree of one scene, what a wire can parent itself to."""
    project_root = root()
    target = _resolve(project_root, scene)
    if target.suffix.lower() != ".tscn":
        raise api.bad_request("not a scene file", scene=scene)
    text = target.read_text(encoding="utf-8", errors="replace")
    try:
        parsed = scenewire.parse(text)
    except scenewire.WireError as exc:
        raise api.bad_request(str(exc), scene=scene)
    return {
        "scene": _as_res(project_root, target),
        "rel": target.relative_to(project_root).as_posix(),
        "root": parsed["root"],
        "lock": _lock(project_root, target),
        "nodes": _tree(text),
        "resources": [{"id": e["id"], "type": e["type"], "path": e["path"]}
                      for e in parsed["ext"]],
    }


@router.post("/api/scene/wire")
def scene_wire(payload: dict) -> dict:
    """Add a node for ``asset`` to ``scene``. Pass dry_run to just see the text.

    The node type is derived from the asset, not chosen by the caller, a PNG
    becomes a Sprite2D, a SpriteFrames becomes an AnimatedSprite2D, because
    "which node holds this file" is a fact about the file. ``node_type`` is
    accepted as an override for the cases where it genuinely is a choice
    (a TextureRect in a Control scene, say).
    """
    project_root = root()
    scene_file = _resolve(project_root, str(payload.get("scene") or ""))
    if scene_file.suffix.lower() != ".tscn":
        raise api.bad_request("scene must be a .tscn", scene=payload.get("scene"))
    asset_file = _resolve(project_root, str(payload.get("asset") or ""))
    asset_res = _as_res(project_root, asset_file)

    text = scene_file.read_text(encoding="utf-8", errors="replace")
    try:
        if asset_file.suffix.lower() == ".gd":
            result = scenewire.attach_script(
                text, asset_res, node=str(payload.get("parent") or "."))
        else:
            result = scenewire.wire(
                text, asset_res,
                node_name=payload.get("node_name") or None,
                parent=str(payload.get("parent") or "."),
                node_type=payload.get("node_type") or None,
                res_type=_res_type(asset_file))
    except scenewire.WireError as exc:
        raise api.bad_request(str(exc), scene=payload.get("scene"),
                              asset=payload.get("asset"))

    dry = bool(payload.get("dry_run"))
    written = scenewire.apply(scene_file, result["text"], root=project_root,
                              dry_run=dry)
    out = {k: v for k, v in result.items() if k != "text"}
    out.update({"scene": _as_res(project_root, scene_file), "asset": asset_res,
                "dry_run": dry, **written})
    if dry:
        out["text"] = result["text"]
    else:
        out["nodes"] = _tree(result["text"])
    return api.ok(out)


@router.post("/api/scene/unwire")
def scene_unwire(payload: dict) -> dict:
    """Remove a node, and any resource it was the last user of."""
    project_root = root()
    scene_file = _resolve(project_root, str(payload.get("scene") or ""))
    node = str(payload.get("node") or "")
    if not node:
        raise api.bad_request("no node to remove")
    text = scene_file.read_text(encoding="utf-8", errors="replace")
    try:
        result = scenewire.unwire(text, node,
                                  recursive=bool(payload.get("recursive")))
    except scenewire.WireError as exc:
        raise api.bad_request(str(exc), scene=payload.get("scene"), node=node)

    dry = bool(payload.get("dry_run"))
    written = scenewire.apply(scene_file, result["text"], root=project_root,
                              dry_run=dry)
    out = {k: v for k, v in result.items() if k != "text"}
    out.update({"scene": _as_res(project_root, scene_file), "dry_run": dry,
                **written})
    if dry:
        out["text"] = result["text"]
    else:
        out["nodes"] = _tree(result["text"])
    return api.ok(out)


def _model_url_for(project_root: Path, res_path: str, kind: str) -> Optional[str]:
    """The URL the 3D viewport fetches a model or image from, or None.

    Models go through /api/model3d/raw, which serves only the model formats
    and their companions and resolves a .gltf's buffers relative to it.
    Images go through /api/preview like the 2D viewport's sprites.
    """
    if not res_path.startswith("res://"):
        return None
    try:
        target = _resolve(project_root, res_path)
    except Exception:
        return None
    rel = target.relative_to(project_root).as_posix()
    if kind == "image":
        return f"/api/preview?rel={rel}" if target.suffix.lower() in {
            ".png", ".webp", ".jpg", ".jpeg", ".svg"} else None
    if target.suffix.lower() not in {".glb", ".gltf", ".obj"}:
        return None
    try:
        stat = target.stat()
        return f"/api/model3d/raw/{rel}?v={int(stat.st_mtime)}-{stat.st_size}"
    except OSError:
        return f"/api/model3d/raw/{rel}"


def _preview_for(project_root: Path, res_path: str) -> Optional[str]:
    """The root-relative path /api/preview will accept, for a res:// image."""
    if not res_path.startswith("res://"):
        return None
    if Path(res_path).suffix.lower() not in {".png", ".webp", ".jpg", ".jpeg",
                                             ".svg", ".gif"}:
        return None
    try:
        target = _resolve(project_root, res_path)
    except Exception:
        return None
    return target.relative_to(project_root).as_posix()


@router.get("/api/scene/outline")
def scene_outline(scene: str) -> dict:
    """The scene as a buildable graph: every node, its role, what hangs off it.

    One request, everything a canvas needs to draw the whole scene, role for
    grouping, resources with previews for the cards, properties for the
    inspector. Per-node requests would be N round trips per repaint, which is
    the mistake ``/api/node/media`` already exists to avoid elsewhere.
    """
    project_root = root()
    target = _resolve(project_root, scene)
    if target.suffix.lower() != ".tscn":
        raise api.bad_request("not a scene file", scene=scene)
    text = target.read_text(encoding="utf-8", errors="replace")
    try:
        nodes = scenewire.outline(text)
        parsed = scenewire.parse(text)
    except scenewire.WireError as exc:
        raise api.bad_request(str(exc), scene=scene)

    for node in nodes:
        for res in node["resources"]:
            res["preview"] = _preview_for(project_root, res["path"])
            res["exists"] = _exists(project_root, res["path"])
    roles: dict[str, int] = {}
    for node in nodes:
        roles[node["role"]] = roles.get(node["role"], 0) + 1
    dimension = scenewire.scene_dimension(text)
    return {
        "scene": _as_res(project_root, target),
        "rel": target.relative_to(project_root).as_posix(),
        "root": parsed["root"],
        "lock": _lock(project_root, target),
        "nodes": nodes,
        "roles": roles,
        "dimension": dimension,
        "resources": [{"id": e["id"], "type": e["type"], "path": e["path"],
                       "preview": _preview_for(project_root, e["path"]),
                       "exists": _exists(project_root, e["path"])}
                      for e in parsed["ext"]],
    }


def _exists(project_root: Path, res_path: str) -> bool:
    try:
        return _resolve(project_root, res_path, must_exist=False).is_file()
    except Exception:
        return False


# What a script pulls in that the scene file never mentions. A scene lists its
# ext_resources; it says nothing about the four things the attached script
# preloads, and those are exactly the files you go looking for next.
_PRELOAD_RE = re.compile(r'\b(?:preload|load)\s*\(\s*["\'](res://[^"\']+)["\']')

_KIND_BY_SUFFIX = {
    ".gd": "script", ".cs": "script", ".tscn": "scene", ".tres": "resource",
    ".png": "texture", ".webp": "texture", ".jpg": "texture", ".jpeg": "texture",
    ".svg": "texture", ".ogg": "audio", ".wav": "audio", ".mp3": "audio",
    ".ttf": "font", ".otf": "font", ".gdshader": "shader", ".json": "data",
}
# The suffixes the code editor will open. Deliberately narrower than "text":
# offering to edit a .png in a code pane is an offer to corrupt it.
_EDITABLE = {".gd", ".cs", ".tscn", ".tres", ".gdshader", ".json", ".cfg"}


@router.get("/api/scene/files")
def scene_files(scene: str) -> dict:
    """Every file this scene reaches, and the folders they live in.

    The question behind "open the scene" is rarely just the .tscn, it is the
    script on the player, the SpriteFrames that script preloads, the shared
    constants file two of them import. Answering it meant a file tree and some
    guessing, so this walks it instead: the scene's own ext_resources, then one
    hop through every script it attaches, following preload/load.

    ONE HOP, NOT TRANSITIVE. A full closure on a project with a shared autoload
    returns most of the codebase and stops being a picture of THIS scene. What
    the second hop buys is the resources a script owns that the scene never
    names; a third would only buy the whole graph back.
    """
    project_root = root()
    target = _resolve(project_root, scene)
    if target.suffix.lower() != ".tscn":
        raise api.bad_request("not a scene file", scene=scene)
    text = target.read_text(encoding="utf-8", errors="replace")
    try:
        parsed = scenewire.parse(text)
    except scenewire.WireError as exc:
        raise api.bad_request(str(exc), scene=scene)

    self_res = _as_res(project_root, target)
    gd = _godot_dir(project_root)
    found: dict[str, dict] = {}

    def add(res_path: str, via: str) -> Optional[Path]:
        res_path = str(res_path or "")
        if not res_path.startswith("res://") or res_path in found:
            return None
        try:
            file = _resolve(project_root, res_path, must_exist=False)
        except Exception:
            return None
        suffix = file.suffix.lower()
        found[res_path] = {
            "res": res_path,
            "rel": file.relative_to(project_root).as_posix()
                   if file.is_relative_to(project_root) else None,
            # What /api/godot/file wants: relative to the GODOT dir, not the bg
            # root. The two differ by a leading "game/" on a scaffolded project
            # and by nothing on an adopted one, which is exactly the kind of
            # difference that works on the author's machine and nowhere else.
            "edit_rel": file.relative_to(gd).as_posix()
                        if file.is_relative_to(gd) else None,
            "name": file.name,
            "dir": str(Path(res_path[len("res://"):]).parent.as_posix()),
            "kind": _KIND_BY_SUFFIX.get(suffix, "other"),
            "editable": suffix in _EDITABLE,
            "exists": file.is_file(),
            "bytes": file.stat().st_size if file.is_file() else 0,
            "preview": _preview_for(project_root, res_path),
            "via": via,
        }
        return file

    add(self_res, "self")
    scripts: list[tuple[str, Path]] = []
    for ext in parsed["ext"]:
        file = add(ext["path"], "scene")
        if file is not None and file.suffix.lower() == ".gd" and file.is_file():
            scripts.append((ext["path"], file))

    for owner, file in scripts:
        try:
            body = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for hit in _PRELOAD_RE.findall(body):
            add(hit, f"script:{owner}")

    files = sorted(found.values(), key=lambda f: (f["dir"], f["name"].lower()))
    folders: dict[str, int] = {}
    for f in files:
        folders[f["dir"]] = folders.get(f["dir"], 0) + 1
    return {
        "scene": self_res,
        "rel": target.relative_to(project_root).as_posix(),
        # The editor passes this straight back to /api/godot/file so both ends
        # agree on which directory res:// means.
        "project_dir": str(gd),
        "files": files,
        "folders": [{"dir": d, "count": n} for d, n in sorted(folders.items())],
        "missing": [f["res"] for f in files if not f["exists"]],
    }


# THE RENDER IS MEMOISED ON THE FILE. The builder re-mounts its viewport on
# every panel repaint and each mount asks for the draw list again; on a
# 5,000-node scene that is five seconds of parsing per click. Keyed on the
# file's mtime and size, so a write (every one of which goes through
# _mutate here) misses on the next read and nothing stale survives.
_RENDER_CACHE: dict[str, tuple[tuple, dict]] = {}
_RENDER_CACHE_MAX = 4
_CACHE_LOCK = threading.Lock()

# THE TEXT TOO. A 75 MB scene read once per mesh request is 115 reads of 75
# MB while the viewport streams its geometry in; the parse and sub-resource
# caches key on the string's hash, so handing them the same object is what
# makes them hit at all. Keyed on the file's stamp, like the render.
_TEXT_CACHE: dict[str, tuple[tuple[int, int], str]] = {}


def _stamp(target: Path) -> tuple[int, int]:
    try:
        stat = target.stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (0, 0)


def _scene_text(target: Path) -> str:
    stamp = _stamp(target)
    with _CACHE_LOCK:
        hit = _TEXT_CACHE.get(str(target))
    if hit and hit[0] == stamp:
        return hit[1]
    text = target.read_text(encoding="utf-8", errors="replace")
    with _CACHE_LOCK:
        while len(_TEXT_CACHE) >= 4:
            _TEXT_CACHE.pop(next(iter(_TEXT_CACHE)))
        _TEXT_CACHE[str(target)] = (stamp, text)
    return text


def _render_stamp(project_root: Path, target: Path, text: str) -> tuple:
    """The host file's stamp plus every file the draw list reads through it.

    A render opens instanced .tscn files and names .glb models; a crate
    edited in kit/crate.tscn changed the picture of main.tscn without
    touching main.tscn. Stat each referenced file, one level down: the
    nested ones are read by the same walk, and a stamp that recursed would
    cost what it is saving.
    """
    parts: list = [_stamp(target)]
    try:
        parsed = scenewire.parse(text)
    except scenewire.WireError:
        return tuple(parts)
    for ext in parsed["ext"]:
        if ext["type"] != "PackedScene" and not ext["path"].lower().endswith(
                (".tscn", ".glb", ".gltf", ".obj", ".png", ".webp", ".jpg", ".jpeg")):
            continue
        try:
            parts.append((ext["path"], _stamp(_resolve(project_root, ext["path"]))))
        except Exception:                                        # noqa: BLE001
            parts.append((ext["path"], (0, 0)))
    return tuple(parts)


@router.get("/api/scene/render")
def scene_render(scene: str) -> dict:
    project_root = root()
    target = _resolve(project_root, scene)
    stamp = _render_stamp(project_root, target, _scene_text(target))
    with _CACHE_LOCK:
        cached = _RENDER_CACHE.get(str(target))
    if cached and cached[0] == stamp:
        out = dict(cached[1])
        out["lock"] = _lock(project_root, target)      # never stale
        return out
    out = _scene_render(scene)
    with _CACHE_LOCK:
        while len(_RENDER_CACHE) >= _RENDER_CACHE_MAX:
            _RENDER_CACHE.pop(next(iter(_RENDER_CACHE)))
        _RENDER_CACHE[str(target)] = (stamp, out)
    return out


def _scene_render(scene: str) -> dict:
    """The scene as a draw list, what it looks like, in paint order.

    Everything a canvas needs to composite the scene the way the engine does:
    world transforms, resolved textures with their atlas regions, sizes,
    z-order. One request, because a viewport repaints constantly and a
    per-node fetch would make panning a network operation.
    """
    project_root = root()
    target = _resolve(project_root, scene)
    if target.suffix.lower() != ".tscn":
        raise api.bad_request("not a scene file", scene=scene)
    gd = _godot_dir(project_root)

    # MEMOISED FOR THE LIFE OF THE REQUEST. A dressed floor asks for the same
    # handful of prop textures hundreds of times, 233 props over ~40 distinct
    # images, and every uncached `size_of` is a PIL open of a file already on
    # the page. Panning must not be a network operation and this endpoint must
    # not be a disk one either.
    @lru_cache(maxsize=None)
    def read(res_path: Optional[str]) -> Optional[str]:
        if not res_path:
            return None
        try:
            return _resolve(project_root, res_path).read_text(
                encoding="utf-8", errors="replace")
        except Exception:
            return None

    @lru_cache(maxsize=None)
    def rel_of(res_path: str) -> Optional[str]:
        return _preview_for(project_root, res_path)

    @lru_cache(maxsize=None)
    def size_of(res_path: str):
        try:
            from PIL import Image
            with Image.open(_resolve(project_root, res_path)) as im:
                return im.size
        except Exception:
            return None

    project_text = ""
    try:
        project_text = (gd / "project.godot").read_text(
            encoding="utf-8", errors="replace")
    except Exception:
        pass

    text = _scene_text(target)
    try:
        if scenedraw3d.is_3d_scene(text):
            # THE SAME ENDPOINT, A DIFFERENT PICTURE. A 3D scene has no paint
            # order and no canvas; it gets world matrices and shapes for the
            # WebGL viewport, and `dimension` tells the client which one to
            # mount. One URL, so the builder never has to know in advance.
            out = scenedraw3d.draw_list(
                text, read=read,
                model_url_of=lambda res, kind="model": _model_url_for(
                    project_root, res, kind),
                scene_path=_as_res(project_root, target))
        else:
            out = scenedraw.draw_list(
                text, read=read, size_of=size_of, rel_of=rel_of,
                viewport=scenedraw.viewport_of(project_text),
                scale=scenedraw.content_scale(project_text))
            out["dimension"] = "2d"
    except scenewire.WireError as exc:
        raise api.bad_request(str(exc), scene=scene)
    out["scene"] = _as_res(project_root, target)
    out["rel"] = target.relative_to(project_root).as_posix()
    # The builder drags against THIS response, so the lock has to arrive with
    # it. Learning the file is claimed at `apply`, after twenty staged edits, is
    # learning it too late to matter.
    out["lock"] = _lock(project_root, target)
    return out


@router.get("/api/scene/mesh")
def scene_mesh(scene: str, id: str):
    """One ArrayMesh's triangles, decoded from the scene text, as bytes.

    Binary rather than JSON because a street's road surface is 120,000
    vertices and base64 in JSON would be half again as big and parsed
    twice. Layout: ``BGM1``, uint32 vertex_count, uint32 index_count, then
    vertex_count x 3 float32 positions, then index_count x uint32 indices,
    all little-endian. The viewport builds a BufferGeometry from it directly.
    """
    import struct

    from fastapi.responses import Response

    project_root = root()
    target = _resolve(project_root, scene)
    if target.suffix.lower() != ".tscn":
        raise api.bad_request("not a scene file", scene=scene)
    if not re.match(r"^[A-Za-z0-9_]+$", id or ""):
        raise api.bad_request("not a sub-resource id", id=id)
    got = scenedraw3d.mesh_data(_scene_text(target), id)
    if got is None:
        raise api.not_found(f"no ArrayMesh {id} in {scene}", id=id)
    head = b"BGM1" + struct.pack("<II", got["vertex_count"], len(got["indices"]) // 4)
    return Response(content=head + got["positions"] + got["indices"],
                    media_type="application/octet-stream",
                    headers={"Cache-Control": "no-cache"})


@router.post("/api/scene/snapshot")
def scene_snapshot(payload: dict) -> dict:
    """Save what the viewport is showing as a PNG under .bgate_out/scene_shots.

    A canvas is not a screenshot: nothing outside the browser can see it, so
    "here is what my scene looks like" was un-shareable, you could not paste it
    into a review, attach it to a task, or send it to anyone. This writes the
    exact pixels the viewport drew, gizmos and all, to a real file.
    """
    import base64
    import binascii
    import time

    project_root = root()
    raw = str(payload.get("png") or "")
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    if not raw:
        raise api.bad_request("no image data in the payload")
    if len(raw) > 24 * 1024 * 1024:
        raise api.ApiError(413, "snapshot too large", detail={"chars": len(raw)})
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise api.bad_request(f"image data is not valid base64: {exc}")
    if not blob.startswith(b"\x89PNG"):
        raise api.bad_request("snapshot must be a PNG")

    label = re.sub(r"[^A-Za-z0-9_-]+", "-",
                   str(payload.get("scene") or "scene").split("/")[-1]) or "scene"
    out_dir = project_root / ".bgate_out" / "scene_shots"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{label}.{time.strftime('%Y%m%d-%H%M%S')}.png"
    out.write_bytes(blob)
    return api.ok({"rel": out.relative_to(project_root).as_posix(),
                   "bytes": out.stat().st_size})


def _mutate(payload: dict, apply_fn) -> dict:
    """Shared shape for every scene edit: dry run, then write with a backup.

    Every mutation goes through here so none of them can quietly skip the
    backup, the LOCK, or the dry-run contract, that consistency is worth more
    than the handful of lines it saves.

    THE LOCK CHECK IS NOT OPTIONAL POLISH. Every other writer in the system asks
    (the code editor at godot_ws.godot_file_write, every agent through
    asset_lock); these endpoints did not, which was survivable only while a
    human clicking buttons was the sole caller. They are on the MCP surface now,
    so two agents and a person can reach the same .tscn, and a backup per write
    is recovery, not prevention. A dry run is exempt: it reads and returns text,
    and refusing to LOOK at a locked file helps nobody.
    """
    project_root = root()
    scene_file = _resolve(project_root, str(payload.get("scene") or ""))
    if scene_file.suffix.lower() != ".tscn":
        raise api.bad_request("scene must be a .tscn", scene=payload.get("scene"))
    if not payload.get("dry_run") and not payload.get("force"):
        held = _lock(project_root, scene_file)
        if held:
            raise api.locked(
                f"{scene_file.name} is locked by the {held['seat']} seat, it "
                "may be mid-edit and about to write its own copy over this one",
                scene=payload.get("scene"), **held)
    text = scene_file.read_text(encoding="utf-8", errors="replace")
    try:
        result = apply_fn(text, project_root)
    except scenewire.WireError as exc:
        raise api.bad_request(str(exc), scene=payload.get("scene"))
    dry = bool(payload.get("dry_run"))
    written = scenewire.apply(scene_file, result["text"], root=project_root,
                              dry_run=dry)
    out = {k: v for k, v in result.items() if k != "text"}
    out.update({"scene": _as_res(project_root, scene_file), "dry_run": dry,
                **written})
    if dry:
        out["text"] = result["text"]
    else:
        out["nodes"] = scenewire.outline(result["text"])
        # Every edge Atlas draws comes out of this file. A dry run changed
        # nothing, so it must NOT drop the cache, that would hand a free full
        # rescan to anyone hovering over a preview.
        from bgate_core.art import screenmap as _screenmap
        _screenmap.invalidate(project_root)
    return api.ok(out)


@router.post("/api/scene/node/add")
def scene_node_add(payload: dict) -> dict:
    """Add a plain node, a CanvasLayer, a Camera2D, a Timer, a grouping Node2D.

    A scene is not only the files in it, and a builder that can only place
    assets can only ever build half of one.
    """
    return _mutate(payload, lambda text, _root: scenewire.add_node(
        text,
        name=str(payload.get("name") or payload.get("node_type") or "Node"),
        node_type=str(payload.get("node_type") or ""),
        parent=str(payload.get("parent") or "."),
        props=payload.get("props") or {}))


@router.post("/api/scene/node/property")
def scene_node_property(payload: dict) -> dict:
    """Set or clear one property on a node. Pass value: null to clear it."""
    return _mutate(payload, lambda text, _root: scenewire.set_property(
        text, str(payload.get("node") or ""), str(payload.get("key") or ""),
        payload.get("value")))


@router.post("/api/scene/node/swap")
def scene_node_swap(payload: dict) -> dict:
    """Point a node's resource at a different file, the swap the builder is for.

    Try that sheet, try that music, try the other enemy. By hand this is four
    steps (find the scene, add an ext_resource, retype the property, delete the
    resource that is now unused); here it is one, and the old resource is swept
    so Atlas stops counting it as referenced.
    """
    project_root = root()
    asset_file = _resolve(project_root, str(payload.get("asset") or ""))
    asset_res = _as_res(project_root, asset_file)
    return _mutate(payload, lambda text, _root: scenewire.swap_resource(
        text, str(payload.get("node") or ""), asset_res,
        prop=payload.get("property") or None,
        res_type=_res_type(asset_file)))


@router.post("/api/scene/node/rename")
def scene_node_rename(payload: dict) -> dict:
    return _mutate(payload, lambda text, _root: scenewire.rename_node(
        text, str(payload.get("node") or ""), str(payload.get("name") or "")))


@router.post("/api/scene/node/reparent")
def scene_node_reparent(payload: dict) -> dict:
    """Move a node and everything under it beneath a different parent."""
    return _mutate(payload, lambda text, _root: scenewire.reparent(
        text, str(payload.get("node") or ""), str(payload.get("parent") or ".")))


_TYPES_3D = [
    {"role": "layer", "label": "world", "types": [
        "Node3D", "WorldEnvironment", "NavigationRegion3D", "CanvasLayer"]},
    {"role": "character", "label": "bodies", "types": [
        "CharacterBody3D", "RigidBody3D", "StaticBody3D", "Area3D",
        "AnimatableBody3D", "VehicleBody3D"]},
    {"role": "visual", "label": "visuals", "types": [
        "MeshInstance3D", "CSGBox3D", "CSGCylinder3D", "CSGSphere3D",
        "Sprite3D", "Label3D", "GPUParticles3D", "CPUParticles3D",
        "MultiMeshInstance3D", "Decal"]},
    {"role": "collision", "label": "collision", "types": [
        "CollisionShape3D", "CollisionPolygon3D"]},
    {"role": "light", "label": "lights", "types": [
        "DirectionalLight3D", "OmniLight3D", "SpotLight3D"]},
    {"role": "controller", "label": "controllers", "types": [
        "Node", "Timer", "AnimationPlayer", "AnimationTree", "Marker3D",
        "Path3D", "PathFollow3D", "SpringArm3D", "RemoteTransform3D",
        "RayCast3D"]},
    {"role": "camera", "label": "camera", "types": ["Camera3D"]},
    {"role": "audio", "label": "audio", "types": [
        "AudioStreamPlayer", "AudioStreamPlayer3D"]},
    {"role": "ui", "label": "ui", "types": [
        "Control", "Panel", "Label", "Button", "RichTextLabel",
        "ProgressBar", "VBoxContainer", "HBoxContainer"]},
]


@router.get("/api/scene/node/types")
def scene_node_types(scene: Optional[str] = None) -> dict:
    """The node types the builder offers, grouped the way a scene is thought about.

    Deliberately a curated list, not Godot's full ClassDB: a palette with two
    thousand entries is a search box with no answers. Anything absent can still
    be typed in, because refusing a valid engine class would be worse than a
    short list. Pass ``scene`` and the palette is the one for that scene's
    dimension: a 3D world is offered MeshInstance3D and lights, not Sprite2D.
    """
    dimension = "2d"
    if scene:
        try:
            project_root = root()
            text = _resolve(project_root, scene).read_text(
                encoding="utf-8", errors="replace")
            dimension = scenewire.scene_dimension(text)
        except Exception:
            dimension = "2d"
    if dimension == "3d":
        return {"dimension": "3d", "groups": _TYPES_3D}
    return {"dimension": "2d", "groups": [
        {"role": "layer", "label": "layers & world", "types": [
            "Node2D", "CanvasLayer", "ParallaxBackground", "ParallaxLayer",
            "TileMapLayer", "YSort"]},
        {"role": "character", "label": "bodies", "types": [
            "CharacterBody2D", "RigidBody2D", "StaticBody2D", "Area2D"]},
        {"role": "visual", "label": "visuals", "types": [
            "Sprite2D", "AnimatedSprite2D", "TextureRect", "ColorRect",
            "Line2D", "GPUParticles2D", "CPUParticles2D"]},
        {"role": "collision", "label": "collision", "types": [
            "CollisionShape2D", "CollisionPolygon2D"]},
        {"role": "controller", "label": "controllers", "types": [
            "Node", "Timer", "AnimationPlayer", "Marker2D", "Path2D",
            "RemoteTransform2D"]},
        {"role": "camera", "label": "camera", "types": ["Camera2D"]},
        {"role": "audio", "label": "audio", "types": [
            "AudioStreamPlayer", "AudioStreamPlayer2D"]},
        {"role": "ui", "label": "ui", "types": [
            "Control", "Panel", "Label", "Button", "RichTextLabel",
            "ProgressBar", "VBoxContainer", "HBoxContainer"]},
    ]}


@router.get("/api/scene/wirable")
def scene_wirable(asset: Optional[str] = None) -> dict:
    """Which scenes exist, and whether each already references ``asset``.

    One request behind the graph's "wire this to…" menu, so the menu can grey
    out the scenes that already have it instead of offering a no-op.
    """
    project_root = root()
    gd = _godot_dir(project_root)
    asset_res = None
    if asset:
        asset_res = _as_res(project_root, _resolve(project_root, asset))
    scenes = []
    for p in sorted(gd.rglob("*.tscn")):
        if SKIP_DIRS & set(p.parts):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        try:
            parsed = scenewire.parse(text)
        except scenewire.WireError:
            continue
        scenes.append({
            "scene": f"res://{p.relative_to(gd).as_posix()}",
            "label": p.stem,
            "root": parsed["root"],
            "nodes": len(parsed["nodes"]),
            "has_asset": bool(asset_res) and any(
                e["path"] == asset_res for e in parsed["ext"]),
        })
    return {"scenes": scenes, "asset": asset_res}


@router.get("/api/scene/tilesets")
def scene_tilesets() -> dict:
    """Every TileSet in the project, with the source ids a level can draw with.

    The level template used to ship a hardcoded `res://assets/tiles/main.tres`.
    No project has ever had that file, so the one template whose whole promise
    is "this generates a level" failed on its third node in every project it was
    opened in. `level_generate` refuses a missing tileset correctly, which meant
    the tool was honest and the card that drove it was not.

    Source ids are the other half of the same trap: they are ids, not indexes,
    and a tileset is free to number its only source 3. Handing back the real
    ones lets a caller prefill `floor_source` with something the file defines
    rather than the 0 that looks safe and usually is not.
    """
    project_root = root()
    gd = _godot_dir(project_root)
    sets = []
    for p in sorted(gd.rglob("*.tres")):
        if SKIP_DIRS & set(p.parts):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        # Cheap reject first: every .tres in a project is a candidate here and
        # most of them are materials, curves and audio buses.
        if "TileSet" not in text:
            continue
        try:
            parsed = tilemap.parse_tileset(text)
        except tilemap.TileError:
            continue
        if not parsed["sources"]:
            continue
        sets.append({
            "res": f"res://{p.relative_to(gd).as_posix()}",
            "label": p.stem,
            "tile_size": parsed["tile_size"],
            "sources": sorted(parsed["sources"]),
            "tiles": {str(sid): len(src["tiles"])
                      for sid, src in sorted(parsed["sources"].items())},
            "draws": [_source_fit(sid, src["tiles"])
                      for sid, src in sorted(parsed["sources"].items())],
        })
    return {"tilesets": sets}


# How many masks each wall layout addresses. level_generate walks them
# row-major from (wall_atlas_x, wall_atlas_y), `wall_columns` wide.
_LAYOUT_TILES = {"blob47": 47, "grid16": 16, "solid": 1}


def _source_fit(sid: int, tiles: list) -> dict:
    """Which wall layouts this atlas source can actually draw, and from where.

    A tile COUNT does not answer this. blob47 wants 47 tiles at 47 specific
    row-major coordinates, so a source holding 36 tiles in a 6x6 block fails it,
    and a source holding 50 tiles in an L fails it too while looking sufficient.
    level_generate already refuses that case with the exact list of coordinates
    the atlas is missing, which is the right behaviour and a terrible thing to
    discover from a template that chose the layout for you.

    So the arithmetic level_generate checks with is done here first, against the
    coordinates the .tres actually defines: origin at the source's top-left,
    width from its own extent, and a layout only offered when every cell it
    addresses is present.
    """
    have = {(int(x), int(y)) for x, y in tiles}
    if not have:
        return {"source": sid, "layouts": [], "columns": 0,
                "atlas_x": 0, "atlas_y": 0, "tiles": 0}
    ax = min(x for x, _ in have)
    ay = min(y for _, y in have)
    cols = max(x for x, _ in have) - ax + 1
    fits = []
    for name, need in _LAYOUT_TILES.items():
        if all((ax + i % cols, ay + i // cols) in have for i in range(need)):
            fits.append(name)
    # Richest first, so a caller taking [0] gets the most detailed wall it can
    # actually draw rather than whichever name sorted first.
    fits.sort(key=lambda n: -_LAYOUT_TILES[n])
    return {"source": sid, "layouts": fits, "columns": cols,
            "atlas_x": ax, "atlas_y": ay, "tiles": len(have)}
