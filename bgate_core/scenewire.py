"""Wiring an asset into a scene — the .tscn edit, done as a text edit.

Atlas can already SEE that a sheet belongs to no screen. Acting on that meant
opening Godot, finding the scene, dragging the file onto the tree, and picking
the right node type — four steps outside the tool that told you about it. This
module closes that loop: given a scene and an asset, it produces the exact
.tscn text that has the asset wired in, and can write it.

Text, not a scene graph. A .tscn is a line-oriented INI-ish file whose ordering
and formatting Godot preserves on save, so a targeted textual insert leaves a
diff a human can review — an all-of-it reserialiser would rewrite the whole
file and bury the one line that matters. The parse here is deliberately shallow
and REFUSES anything it does not fully understand rather than guessing: a
malformed scene is returned untouched with a reason.

What gets wired is decided by the asset, because the node type is not a
preference:

    .png/.webp/.jpg/.svg  -> Sprite2D           texture = ExtResource(id)
    SpriteFrames .tres    -> AnimatedSprite2D   sprite_frames = ExtResource(id)
    .ogg/.wav/.mp3        -> AudioStreamPlayer2D stream = ExtResource(id)
    .tscn                 -> instance           [node ... instance=ExtResource(id)]
    .gd                   -> script on a node   script = ExtResource(id)

Every entry point takes ``dry_run`` and returns the resulting text plus a
summary, so the UI can show the change before it touches disk — and a backup is
written on every real save.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional, Sequence

_HEADER_RE = re.compile(r"^\[gd_scene(?P<attrs>[^\]]*)\]", re.MULTILINE)
_LOAD_STEPS_RE = re.compile(r"load_steps=(\d+)")
_EXT_RE = re.compile(
    r'^\[ext_resource\s+type="(?P<type>[^"]*)"\s+(?:uid="[^"]*"\s+)?'
    r'path="(?P<path>[^"]+)"\s+id="(?P<id>[^"]+)"\]\s*$', re.MULTILINE)
_SUB_RE = re.compile(r'^\[sub_resource\s', re.MULTILINE)
_NODE_RE = re.compile(
    r'^\[node\s+name="(?P<name>[^"]+)"(?P<rest>[^\]]*)\]', re.MULTILINE)
_PARENT_RE = re.compile(r'parent="(?P<parent>[^"]*)"')
_TYPE_RE = re.compile(r'type="(?P<type>[^"]*)"')

# asset suffix -> (godot node type, the property the resource lands in)
_BY_SUFFIX: dict[str, tuple[str, str]] = {
    ".png": ("Sprite2D", "texture"),
    ".webp": ("Sprite2D", "texture"),
    ".jpg": ("Sprite2D", "texture"),
    ".jpeg": ("Sprite2D", "texture"),
    ".svg": ("Sprite2D", "texture"),
    ".ogg": ("AudioStreamPlayer2D", "stream"),
    ".wav": ("AudioStreamPlayer2D", "stream"),
    ".mp3": ("AudioStreamPlayer2D", "stream"),
    ".tres": ("AnimatedSprite2D", "sprite_frames"),
    ".gd": ("", "script"),
    ".tscn": ("", ""),          # instanced, no property
}
_EXT_TYPE: dict[str, str] = {
    ".png": "Texture2D", ".webp": "Texture2D", ".jpg": "Texture2D",
    ".jpeg": "Texture2D", ".svg": "Texture2D",
    ".ogg": "AudioStream", ".wav": "AudioStream", ".mp3": "AudioStream",
    ".tres": "SpriteFrames", ".gd": "Script", ".tscn": "PackedScene",
}

_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")


class WireError(ValueError):
    """The edit was refused. The message is written for the person, not the log."""


# ---------------------------------------------------------------------------
# Reading a scene
# ---------------------------------------------------------------------------
def parse(text: str) -> dict:
    """The shallow structure this module needs: resources, nodes, counts.

    Deliberately not a full parser. It knows where blocks START, which is all
    an insert needs, and it reports what it found so a caller can refuse.
    """
    header = _HEADER_RE.search(text)
    if not header:
        raise WireError("not a Godot scene — no [gd_scene] header")
    ext = [{"type": m.group("type"), "path": m.group("path"),
            "id": m.group("id"), "span": m.span()}
           for m in _EXT_RE.finditer(text)]
    nodes = []
    for m in _NODE_RE.finditer(text):
        rest = m.group("rest")
        pm, tm = _PARENT_RE.search(rest), _TYPE_RE.search(rest)
        nodes.append({
            "name": m.group("name"),
            "parent": pm.group("parent") if pm else None,   # None == the root
            "type": tm.group("type") if tm else "",
            "instance": "instance=" in rest,
            "start": m.start(),
        })
    return {
        "header_span": header.span(),
        "load_steps": int(_LOAD_STEPS_RE.search(header.group(0)).group(1))
        if _LOAD_STEPS_RE.search(header.group(0)) else None,
        "ext": ext,
        "sub_count": len(_SUB_RE.findall(text)),
        "nodes": nodes,
        "root": nodes[0]["name"] if nodes else None,
    }


def node_path(node: dict) -> str:
    """The parent-relative path a child would use to address this node."""
    parent = node.get("parent")
    if parent is None:
        return "."
    return node["name"] if parent == "." else f"{parent}/{node['name']}"


def sanitize_node_name(raw: str) -> str:
    name = _NAME_RE.sub("", str(raw or "").strip())
    if not name:
        raise WireError("node name is empty after sanitising")
    if name[0].isdigit():
        name = "N" + name
    return name[:48]


def unique_node_name(parsed: dict, want: str, parent: str) -> str:
    """Godot requires sibling names to be unique; make one that is."""
    taken = {n["name"] for n in parsed["nodes"] if (n["parent"] or ".") == parent}
    base = sanitize_node_name(want)
    if base not in taken:
        return base
    for i in range(2, 200):
        cand = f"{base}{i}"
        if cand not in taken:
            return cand
    raise WireError(f"cannot find a free name near {base!r}")


# ---------------------------------------------------------------------------
# Blocks — the span a node's own lines occupy
# ---------------------------------------------------------------------------
def _find(parsed: dict, path: str) -> dict:
    node = next((n for n in parsed["nodes"] if node_path(n) == path), None)
    if node is None:
        raise WireError(f"no node at {path!r} in this scene")
    return node


def block_span(text: str, parsed: dict, node: dict) -> tuple[int, int]:
    """Where this node's own [node] block starts and ends (children excluded)."""
    i = parsed["nodes"].index(node)
    end = (parsed["nodes"][i + 1]["start"] if i + 1 < len(parsed["nodes"])
           else len(text))
    return node["start"], end


def descendants(parsed: dict, node: dict) -> list[dict]:
    """Every node under this one, at any depth."""
    path = node_path(node)
    if path == ".":
        return [n for n in parsed["nodes"] if n is not node]
    return [n for n in parsed["nodes"]
            if (n["parent"] or "") == path
            or (n["parent"] or "").startswith(path + "/")]


def properties(text: str, parsed: dict, node: dict) -> dict:
    """The ``key = value`` lines inside a node block, verbatim.

    Values are returned as the raw Godot literals they are — ``Vector2(3, 4)``,
    ``ExtResource("2_hero")``, ``true``. Parsing them into Python and back would
    be a second, worse serialiser, and every round trip through it would be a
    chance to rewrite a line nobody asked to change.
    """
    start, end = block_span(text, parsed, node)
    body = text[start:end]
    head_end = body.find("\n")
    out: dict[str, str] = {}
    for line in body[head_end + 1:].split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "[")):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


# ---------------------------------------------------------------------------
# The edit
# ---------------------------------------------------------------------------
def _default_name(res_path: str) -> str:
    stem = Path(res_path).stem
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", stem) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Node"


def _fresh_id(parsed: dict, res_path: str) -> str:
    """A resource id no other ext_resource in this scene uses.

    Godot 4 writes ``<n>_<hash>``; the numeric prefix is what the engine sorts
    on and the suffix only has to be stable and unique, so a slug of the file
    name reads far better in a diff than five random base-36 characters.
    """
    taken = {e["id"] for e in parsed["ext"]}
    slug = re.sub(r"[^a-z0-9]+", "", Path(res_path).stem.lower())[:10] or "res"
    n = len(parsed["ext"]) + 1
    while f"{n}_{slug}" in taken:
        n += 1
    return f"{n}_{slug}"


def _with_load_steps(text: str, parsed: dict, ext_delta: int) -> str:
    """Rewrite the header's load_steps to match the resource count.

    Recomputed, never incremented: a scene whose header was already wrong stays
    wrong forever under increment, and Godot uses this number to size its
    loader. ext + sub + 1 is the engine's own accounting.
    """
    total = len(parsed["ext"]) + ext_delta + parsed["sub_count"] + 1
    s, e = parsed["header_span"]
    header = text[s:e]
    if _LOAD_STEPS_RE.search(header):
        header = _LOAD_STEPS_RE.sub(f"load_steps={total}", header, count=1)
    else:
        header = header.replace("[gd_scene", f"[gd_scene load_steps={total}", 1)
    return text[:s] + header + text[e:]


def _insert_ext(text: str, parsed: dict, block: str) -> str:
    """Put a new ext_resource after the last one, else after the header."""
    if parsed["ext"]:
        at = parsed["ext"][-1]["span"][1]
        return text[:at] + "\n" + block + text[at:]
    at = parsed["header_span"][1]
    return text[:at] + "\n\n" + block + text[at:]


_RES_HEADER_RE = re.compile(r'^\[gd_resource\s+type="([^"]+)"', re.MULTILINE)


def resource_type_of(tres_text: str) -> Optional[str]:
    """The class a .tres actually IS, read from its own header.

    ``_EXT_TYPE`` can only guess from the suffix, and it guesses SpriteFrames —
    which is right for the .tres files these pipelines generate and wrong for
    every TileSet, Theme and custom Resource in the project. An ext_resource
    that declares the wrong type loads as null and the node silently draws
    nothing, so where the real type is knowable it is read, not assumed.
    """
    m = _RES_HEADER_RE.search(tres_text or "")
    return m.group(1) if m else None


def wire(text: str, res_path: str, *, node_name: Optional[str] = None,
         parent: str = ".", node_type: Optional[str] = None,
         res_type: Optional[str] = None) -> dict:
    """Return the scene text with ``res_path`` wired in as a new node.

    Never mutates a file. Returns ``{text, node, id, reused, node_type, summary}``.
    """
    if not res_path.startswith("res://"):
        raise WireError(f"expected a res:// path, got {res_path!r}")
    parsed = parse(text)
    if not parsed["nodes"]:
        raise WireError("scene has no nodes — nothing to parent to")

    suffix = Path(res_path).suffix.lower()
    if suffix not in _BY_SUFFIX:
        raise WireError(f"don't know how to wire a {suffix or 'file'} into a scene")
    default_type, prop = _BY_SUFFIX[suffix]

    if suffix == ".gd":
        raise WireError("a script attaches to an existing node — use attach_script()")

    parents = {node_path(n) for n in parsed["nodes"]}
    if parent not in parents:
        raise WireError(f"no node at {parent!r} in this scene "
                        f"(have: {', '.join(sorted(parents))})")

    existing = next((e for e in parsed["ext"] if e["path"] == res_path), None)
    if existing:
        rid, reused = existing["id"], True
        out = text
    else:
        rid, reused = _fresh_id(parsed, res_path), False
        etype = res_type or _EXT_TYPE.get(suffix, "Resource")
        out = _insert_ext(
            text, parsed,
            f'[ext_resource type="{etype}" path="{res_path}" id="{rid}"]\n')

    name = unique_node_name(parsed, node_name or _default_name(res_path), parent)
    ntype = sanitize_node_name(node_type) if node_type else default_type

    if suffix == ".tscn":
        block = (f'\n[node name="{name}" parent="{parent}" '
                 f'instance=ExtResource("{rid}")]\n')
        ntype = "(instance)"
    else:
        block = (f'\n[node name="{name}" type="{ntype}" parent="{parent}"]\n'
                 f'{prop} = ExtResource("{rid}")\n')

    out = out.rstrip("\n") + "\n" + block
    out = _with_load_steps(out, parse(out), 0)
    return {
        "text": out, "node": name, "parent": parent, "id": rid,
        "reused": reused, "node_type": ntype, "property": prop,
        "summary": (f"{'reuse' if reused else 'add'} resource {rid} · "
                    f"new {ntype} '{name}' under {parent}"),
    }


def attach_script(text: str, res_path: str, *, node: str = ".") -> dict:
    """Put ``script = ExtResource(id)`` on an existing node.

    Scripts are the one asset kind that does not become a node of its own —
    wiring one as a child would be nonsense, so it gets its own entry point
    rather than a flag that changes what `wire` means.
    """
    if not res_path.endswith(".gd"):
        raise WireError("attach_script takes a .gd path")
    parsed = parse(text)
    target = next((n for n in parsed["nodes"] if node_path(n) == node), None)
    if target is None:
        raise WireError(f"no node at {node!r} in this scene")

    existing = next((e for e in parsed["ext"] if e["path"] == res_path), None)
    if existing:
        rid, reused, out = existing["id"], True, text
    else:
        rid, reused = _fresh_id(parsed, res_path), False
        out = _insert_ext(
            text, parsed,
            f'[ext_resource type="Script" path="{res_path}" id="{rid}"]\n')

    # Re-parse: inserting the resource moved every node offset downstream.
    reparsed = parse(out)
    tgt = next(n for n in reparsed["nodes"] if node_path(n) == node)
    idx = reparsed["nodes"].index(tgt)
    end = (reparsed["nodes"][idx + 1]["start"] if idx + 1 < len(reparsed["nodes"])
           else len(out))
    body = out[tgt["start"]:end]
    if re.search(r"^script\s*=", body, re.MULTILINE):
        body = re.sub(r"^script\s*=.*$", f'script = ExtResource("{rid}")',
                      body, count=1, flags=re.MULTILINE)
    else:
        head_end = body.index("\n") + 1 if "\n" in body else len(body)
        body = body[:head_end] + f'script = ExtResource("{rid}")\n' + body[head_end:]
    out = out[:tgt["start"]] + body + out[end:]
    # Replacing a script leaves the OLD one as an ext_resource nobody uses, and
    # Atlas counts an ext_resource as a reference — so the previous script would
    # never show up as dead again.
    out, dropped = _drop_unused(out)
    out = _with_load_steps(out, parse(out), 0)
    return {"text": out, "node": node, "id": rid, "reused": reused,
            "dropped_resources": dropped,
            "summary": f"{'reuse' if reused else 'add'} script {rid} on {node}"
                       + (f" · drop {len(dropped)} unused resource(s)" if dropped else "")}


def _drop_unused(text: str) -> tuple[str, list[str]]:
    """Remove every ext_resource no remaining line references."""
    dropped: list[str] = []
    for e in parse(text)["ext"]:
        if f'ExtResource("{e["id"]}")' in text or f"ExtResource('{e['id']}')" in text:
            continue
        m = re.search(rf'^\[ext_resource[^\]]*id="{re.escape(e["id"])}"\]\n?',
                      text, re.MULTILINE)
        if m:
            text = text[:m.start()] + text[m.end():]
            dropped.append(e["path"])
    return text, dropped


def add_node(text: str, *, name: str, node_type: str, parent: str = ".",
             props: Optional[dict] = None) -> dict:
    """Add a plain node — no resource attached.

    The counterpart to `wire`: a scene is not only the assets in it. A
    CanvasLayer for the HUD, a Camera2D, a Timer, a bare Node2D to group the
    enemies under — none of those are a file on disk, and a builder that can
    only add things that ARE files can only ever build half a scene.
    """
    parsed = parse(text)
    if not parsed["nodes"]:
        raise WireError("scene has no nodes — nothing to parent to")
    parents = {node_path(n) for n in parsed["nodes"]}
    if parent not in parents:
        raise WireError(f"no node at {parent!r} in this scene")
    ntype = sanitize_node_name(node_type)
    if not ntype:
        raise WireError("a node needs a type")
    final = unique_node_name(parsed, name or ntype, parent)
    lines = [f'\n[node name="{final}" type="{ntype}" parent="{parent}"]']
    for key, value in (props or {}).items():
        lines.append(f"{_prop_key(key)} = {_prop_value(value)}")
    out = text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    out = _with_load_steps(out, parse(out), 0)
    return {"text": out, "node": final, "parent": parent, "node_type": ntype,
            "summary": f"add {ntype} '{final}' under {parent}"}


TILE_LAYER_TYPE = "TileMapLayer"


def _layer_block(name: str, parent: str, packed: str, rid: str,
                 props: Optional[dict]) -> str:
    lines = [f'[node name="{name}" type="{TILE_LAYER_TYPE}" parent="{parent}"]']
    if packed:
        lines.append(f'tile_map_data = PackedByteArray("{packed}")')
    lines.append(f'tile_set = ExtResource("{rid}")')
    for key, value in (props or {}).items():
        lines.append(f"{_prop_key(key)} = {_prop_value(value)}")
    return "\n".join(lines) + "\n"


def wire_tilemap(text: str, tileset_res: str, layers: Sequence[dict], *,
                 parent: str = ".", owns: Optional[Sequence[str]] = None) -> dict:
    """Write generated tile layers into a scene, REPLACING same-named ones.

    Replacing is the whole point. A generator is re-run — new seed, wider
    corridors, a different tileset — and an append-only writer turns that into
    Ground, Ground2, Ground3 stacked on each other, all still drawing. The
    second run of a level generator looked identical to the first because the
    old layer was on top of the new one, and nothing about the scene said so.

    So a node of the same name under the same parent is overwritten in place,
    and one that exists but is NOT a TileMapLayer is refused rather than
    clobbered — that name belongs to something the generator did not make.

    ``owns`` names every layer this generator MAY produce, and any of them not
    in `layers` this run is REMOVED. Replacing by name alone covers the run
    that produces the same layers again; it does not cover the run that
    produces FEWER. A level generated with decals and then regenerated without
    them left the old decal layer in place, still drawing — forty-two stains
    over a level that had asked for none, and the scene loads perfectly. Same
    failure family as the stacked-Ground case above, in the other direction.

    Each layer is ``{name, cells, props?}`` where cells are the dicts
    ``tilemap.encode_cells`` takes. Returns ``{text, layers, id, summary}``;
    writes nothing.
    """
    from bgate_core import tilemap                      # local: keeps the
    # dependency one-way — tilemap knows nothing about scenes.

    if not tileset_res.startswith("res://"):
        raise WireError(f"expected a res:// path for the tileset, got "
                        f"{tileset_res!r}")
    if not layers:
        raise WireError("no layers to write")

    parsed = parse(text)
    if not parsed["nodes"]:
        raise WireError("scene has no nodes — nothing to parent to")
    if parent not in {node_path(n) for n in parsed["nodes"]}:
        raise WireError(f"no node at {parent!r} in this scene")

    existing = next((e for e in parsed["ext"] if e["path"] == tileset_res), None)
    if existing:
        rid, out = existing["id"], text
    else:
        rid = _fresh_id(parsed, tileset_res)
        out = _insert_ext(
            text, parsed,
            f'[ext_resource type="TileSet" path="{tileset_res}" id="{rid}"]\n')

    written = []
    for layer in layers:
        name = sanitize_node_name(layer.get("name") or "TileMapLayer")
        packed = tilemap.encode_cells(layer.get("cells") or [])
        block = _layer_block(name, parent, packed, rid, layer.get("props"))

        parsed = parse(out)
        full = f"{parent}/{name}" if parent != "." else name
        target = next((n for n in parsed["nodes"] if node_path(n) == full), None)
        if target is None:
            out = out.rstrip("\n") + "\n\n" + block
            action = "add"
        elif target["type"] != TILE_LAYER_TYPE:
            raise WireError(
                f"{full!r} is a {target['type'] or 'node'}, not a "
                f"{TILE_LAYER_TYPE} — refusing to overwrite it")
        else:
            start, end = block_span(out, parsed, target)
            out = out[:start] + block + "\n" + out[end:]
            action = "replace"
        written.append({"node": full, "action": action,
                        "cells": len(layer.get("cells") or [])})

    # -- drop the layers this generator owns but did not produce -----------
    made = {sanitize_node_name(ly.get("name") or "TileMapLayer")
            for ly in layers}
    for name in sorted({sanitize_node_name(n) for n in (owns or ())} - made):
        parsed = parse(out)
        full = f"{parent}/{name}" if parent != "." else name
        target = next((n for n in parsed["nodes"] if node_path(n) == full), None)
        if target is None:
            continue
        if target["type"] != TILE_LAYER_TYPE:
            # a name this generator claims but something else now owns is left
            # alone — removing it would delete work nobody asked us to touch
            continue
        start, end = block_span(out, parsed, target)
        out = out[:start] + out[end:]
        written.append({"node": full, "action": "remove", "cells": 0})

    out = _with_load_steps(out, parse(out), 0)
    return {
        "text": out, "id": rid, "parent": parent, "tileset": tileset_res,
        "layers": written,
        "summary": " · ".join(
            f"{w['action']} {w['node']} ({w['cells']} cells)" for w in written),
    }


_PROP_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(/[A-Za-z_][A-Za-z0-9_]*)*$")
# Godot literals this module will write. Anything else is refused rather than
# quoted-and-hoped: a malformed property value does not fail at save, it fails
# when the engine next loads the scene, with a message pointing at a line
# number nobody wrote by hand.
_PROP_VALUE_RE = re.compile(
    r"^(true|false|-?\d+(\.\d+)?|\"[^\"\\\n]*\"|&\"[^\"\\\n]*\"|"
    r"(Vector2|Vector2i|Vector3|Color|Rect2)\([-\d\s.,]*\)|"
    r"(Ext|Sub)Resource\(\"[^\"\\\n]+\"\)|NodePath\(\"[^\"\\\n]*\"\))$")


def _prop_key(key: str) -> str:
    key = str(key or "").strip()
    if not _PROP_KEY_RE.match(key):
        raise WireError(f"{key!r} is not a property name")
    return key


def _prop_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value).strip()
    if not _PROP_VALUE_RE.match(text):
        raise WireError(
            f"{text!r} is not a Godot value this can write safely — numbers, "
            "true/false, \"strings\", Vector2(x, y), Color(...), "
            "ExtResource(\"id\") and NodePath(\"...\") are accepted")
    return text


def set_property(text: str, node: str, key: str, value) -> dict:
    """Set (or with value None, remove) one property line on a node."""
    parsed = parse(text)
    target = _find(parsed, node)
    start, end = block_span(text, parsed, target)
    body = text[start:end]
    name = _prop_key(key)
    pattern = re.compile(rf"^{re.escape(name)}\s*=.*$", re.MULTILINE)

    if value is None:
        if not pattern.search(body):
            return {"text": text, "node": node, "summary": f"{name} was not set"}
        body = pattern.sub("", body, count=1)
        body = re.sub(r"\n{3,}", "\n\n", body)
        action = f"clear {name}"
    else:
        line = f"{name} = {_prop_value(value)}"
        if pattern.search(body):
            body = pattern.sub(line, body, count=1)
        else:
            head_end = body.index("\n") + 1 if "\n" in body else len(body)
            body = body[:head_end] + line + "\n" + body[head_end:]
        action = f"{name} = {_prop_value(value)}"
    out = text[:start] + body + text[end:]
    out, dropped = _drop_unused(out)
    out = _with_load_steps(out, parse(out), 0)
    return {"text": out, "node": node, "dropped_resources": dropped,
            "summary": f"{node}: {action}"}


def swap_resource(text: str, node: str, res_path: str, *,
                  prop: Optional[str] = None,
                  res_type: Optional[str] = None) -> dict:
    """Point a node's resource property at a different file.

    This is the move the whole scene builder exists for — try that sheet, try
    that music, try the other enemy — and doing it by hand is: find the scene,
    add an ext_resource, retype the property, then remember to delete the old
    resource so it does not read as still-used. All four steps are one call.
    """
    if not res_path.startswith("res://"):
        raise WireError(f"expected a res:// path, got {res_path!r}")
    suffix = Path(res_path).suffix.lower()
    if suffix not in _BY_SUFFIX:
        raise WireError(f"don't know how to hang a {suffix or 'file'} on a node")
    parsed = parse(text)
    target = _find(parsed, node)

    default_type, default_prop = _BY_SUFFIX[suffix]
    prop = prop or default_prop
    if not prop:
        raise WireError("a scene instance cannot be swapped in place — "
                        "remove the node and add the other scene")
    if target["instance"]:
        raise WireError(f"{node!r} is an instanced scene, not a node with a "
                        "resource property")

    existing = next((e for e in parsed["ext"] if e["path"] == res_path), None)
    if existing:
        rid, reused, out = existing["id"], True, text
    else:
        rid, reused = _fresh_id(parsed, res_path), False
        etype = res_type or _EXT_TYPE.get(suffix, "Resource")
        out = _insert_ext(text, parsed,
                          f'[ext_resource type="{etype}" '
                          f'path="{res_path}" id="{rid}"]\n')
    result = set_property(out, node, prop, f'ExtResource("{rid}")')
    return {
        "text": result["text"], "node": node, "id": rid, "reused": reused,
        "property": prop, "expects": default_type,
        "dropped_resources": result.get("dropped_resources", []),
        "summary": f"{node}.{prop} -> {Path(res_path).name}"
                   + (f" · dropped {len(result['dropped_resources'])} unused"
                      if result.get("dropped_resources") else ""),
    }


def rename_node(text: str, node: str, new_name: str) -> dict:
    """Rename a node and repoint every child's ``parent=`` at the new path.

    NodePath strings elsewhere in the scene (an AnimationPlayer track, an
    exported node reference) are NOT rewritten — finding them all reliably
    means understanding every property type. They are counted and reported so
    the rename is never silently half-done.
    """
    parsed = parse(text)
    target = _find(parsed, node)
    if target["parent"] is None:
        raise WireError("renaming the root changes the scene's own name — "
                        "do that in Godot, where the uid is updated with it")
    final = sanitize_node_name(new_name)
    parent = target["parent"]
    siblings = {n["name"] for n in parsed["nodes"]
                if (n["parent"] or "") == parent and n is not target}
    if final in siblings:
        raise WireError(f"{parent}/{final} already exists")

    old_path, new_path = node_path(target), (
        final if parent == "." else f"{parent}/{final}")

    # Every edit is planned against the ORIGINAL parse and applied back to
    # front, so no offset is ever stale. Re-parsing between edits was the bug
    # here first time round: the node had already been renamed, so looking it
    # up again by its old path failed.
    edits: list[tuple[int, int, str]] = []
    for child in descendants(parsed, target):
        old_parent = child["parent"] or ""
        if old_parent != old_path and not old_parent.startswith(old_path + "/"):
            continue
        fixed = new_path + old_parent[len(old_path):]
        cs, ce = block_span(text, parsed, child)
        edits.append((cs, ce, text[cs:ce].replace(
            f'parent="{old_parent}"', f'parent="{fixed}"', 1)))
    ts, te = block_span(text, parsed, target)
    edits.append((ts, te, text[ts:te].replace(
        f'name="{target["name"]}"', f'name="{final}"', 1)))

    out = text
    for start, end, body in sorted(edits, reverse=True):
        out = out[:start] + body + out[end:]

    stale = len(re.findall(rf'NodePath\("[^"]*\b{re.escape(target["name"])}\b',
                           out))
    return {"text": out, "node": new_path, "was": old_path,
            "nodepath_references": stale,
            "summary": f"rename {old_path} -> {new_path}"
                       + (f" · {stale} NodePath reference(s) still name the old "
                          "node and were left alone" if stale else "")}


def reparent(text: str, node: str, new_parent: str) -> dict:
    """Move a node (and everything under it) beneath a different parent.

    Godot requires a parent's block to appear before its children's, so this
    RELOCATES the block group rather than editing an attribute in place. The
    group must be contiguous — which is how Godot itself writes scenes — and a
    file where it is not is refused rather than shuffled into something that
    loads differently than it reads.
    """
    parsed = parse(text)
    target = _find(parsed, node)
    if target["parent"] is None:
        raise WireError("the root node has no parent to change")
    parents = {node_path(n) for n in parsed["nodes"]}
    if new_parent not in parents:
        raise WireError(f"no node at {new_parent!r} in this scene")
    if new_parent == node or new_parent.startswith(node + "/"):
        raise WireError("a node cannot be moved inside itself")
    if (target["parent"] or "") == new_parent:
        return {"text": text, "node": node, "summary": "already there"}

    group = [target] + descendants(parsed, target)
    indices = sorted(parsed["nodes"].index(n) for n in group)
    if indices != list(range(indices[0], indices[0] + len(indices))):
        raise WireError(f"{node!r} and its children are not contiguous in this "
                        "file — open it in Godot and re-save it first")

    first, last = parsed["nodes"][indices[0]], parsed["nodes"][indices[-1]]
    start = first["start"]
    end = block_span(text, parsed, last)[1]
    chunk = text[start:end]
    remainder = text[:start] + text[end:]

    # Rewrite the moved group's parent paths onto the new location.
    old_path = node_path(target)
    final = unique_node_name(parse(remainder), target["name"], new_parent)
    new_path = final if new_parent == "." else f"{new_parent}/{final}"
    chunk = chunk.replace(f'name="{target["name"]}" ', f'name="{final}" ', 1)
    chunk = re.sub(rf'parent="{re.escape(old_path)}(?=[/"])',
                   f'parent="{new_path}', chunk)
    chunk = chunk.replace(f'parent="{target["parent"]}"',
                          f'parent="{new_parent}"', 1)

    host = _find(parse(remainder), new_parent)
    host_group = [host] + descendants(parse(remainder), host)
    reparsed = parse(remainder)
    insert_at = max(block_span(remainder, reparsed, n)[1] for n in host_group) \
        if new_parent != "." else len(remainder)
    out = (remainder[:insert_at].rstrip("\n") + "\n"
           + chunk.strip("\n") + "\n" + remainder[insert_at:])
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = _with_load_steps(out, parse(out), 0)
    return {"text": out, "node": new_path, "was": old_path,
            "moved": len(group),
            "summary": f"move {old_path} under {new_parent}"
                       + (f" (with {len(group) - 1} child node(s))"
                          if len(group) > 1 else "")}


def unwire(text: str, node: str, *, recursive: bool = False) -> dict:
    """Remove a node block, and any ext_resource nothing else references.

    The orphaned-resource sweep is the whole point: a scene that keeps the
    ext_resource after its only user is gone still counts as a reference in
    Atlas, so the asset would never show up as dead again.

    `recursive` takes the node's children with it — deleting a character means
    deleting its sprite, its collision shape and its hitbox, and making that
    four separate confirmations is a worse answer than one honest count.
    """
    parsed = parse(text)
    target = next((n for n in parsed["nodes"] if node_path(n) == node), None)
    if target is None:
        raise WireError(f"no node at {node!r} in this scene")
    if target["parent"] is None:
        raise WireError("refusing to remove the scene's root node")
    # A child's `parent` is the target's path exactly, or that path plus a
    # separator — a prefix test alone would call "GroundVisual" a child of
    # "Ground" and refuse a removal that is perfectly safe.
    children = descendants(parsed, target)
    if children and not recursive:
        raise WireError(f"{node!r} has {len(children)} child node(s) — pass "
                        "recursive to take them with it")

    doomed = [target] + (children if recursive else [])
    spans = sorted((block_span(text, parsed, n) for n in doomed), reverse=True)
    out = text
    for start, end in spans:                     # back to front: earlier spans
        out = out[:start] + out[end:]            # keep their offsets
    out, dropped = _drop_unused(out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = _with_load_steps(out, parse(out), 0)
    return {"text": out, "removed": node, "dropped_resources": dropped,
            "removed_count": len(doomed),
            "summary": f"remove '{node}'"
                       + (f" and {len(doomed) - 1} child node(s)"
                          if len(doomed) > 1 else "")
                       + (f" · drop {len(dropped)} unused resource(s)" if dropped else "")}


# ---------------------------------------------------------------------------
# Roles — what a node IS, for someone building a scene
# ---------------------------------------------------------------------------
# A scene builder needs "the enemies" and "the controllers", not "the
# CharacterBody2Ds". Godot's type is one input to that and rarely the decisive
# one: a Node2D is a character or a spawn point or a group depending entirely on
# what hangs off it. So the role is inferred from three signals, strongest
# first — the paths its resources live under, its script's path, then its type.
#
# This is presentation, not truth. It groups a canvas so a person can find
# things; nothing downstream branches on it, so a wrong guess costs a
# misfiled card and nothing else.
_ROLE_BY_TYPE: dict[str, str] = {
    "CanvasLayer": "layer", "ParallaxBackground": "layer",
    "ParallaxLayer": "layer", "TileMap": "layer", "TileMapLayer": "layer",
    "Camera2D": "camera", "Camera3D": "camera",
    "AudioStreamPlayer": "audio", "AudioStreamPlayer2D": "audio",
    "AudioStreamPlayer3D": "audio",
    "CollisionShape2D": "collision", "CollisionPolygon2D": "collision",
    "Area2D": "collision", "StaticBody2D": "collision",
    "GPUParticles2D": "fx", "CPUParticles2D": "fx", "AnimationPlayer": "fx",
    "Timer": "controller", "Node": "controller", "Marker2D": "marker",
    "Control": "ui", "Label": "ui", "Button": "ui", "TextureRect": "ui",
    "ColorRect": "ui", "Panel": "ui", "VBoxContainer": "ui",
    "HBoxContainer": "ui", "RichTextLabel": "ui", "ProgressBar": "ui",
}
_ROLE_BY_PATH: tuple[tuple[str, str], ...] = (
    ("/enemies/", "enemy"), ("/enemy", "enemy"), ("/monsters/", "enemy"),
    ("/characters/", "character"), ("/player", "character"),
    ("/props/", "prop"), ("/items/", "item"), ("/gear/", "item"),
    ("/tiles/", "layer"), ("/ui/", "ui"), ("/hud", "ui"),
    ("/audio/", "audio"), ("/music", "audio"), ("/sfx", "audio"),
    ("/vfx/", "fx"), ("/shaders/", "fx"),
)
# A script whose name says what it drives beats everything else about it.
_ROLE_BY_SCRIPT: tuple[tuple[str, str], ...] = (
    ("enemy", "enemy"), ("spawner", "controller"), ("manager", "controller"),
    ("controller", "controller"), ("director", "controller"),
    ("state", "controller"), ("hud", "ui"), ("menu", "ui"),
    ("player", "character"), ("fighter", "character"), ("rig", "character"),
)


def role_for(node_type: str, *, script: str = "", resources: Sequence[str] = (),
             name: str = "", instance: bool = False) -> str:
    """Which bucket a node belongs in on a scene canvas."""
    hay = " ".join([*(r.lower() for r in resources), name.lower()])
    for needle, role in _ROLE_BY_PATH:
        if needle in hay:
            return role
    low_script = (script or "").lower()
    for needle, role in _ROLE_BY_SCRIPT:
        if needle in low_script:
            return role
    if node_type in _ROLE_BY_TYPE:
        return _ROLE_BY_TYPE[node_type]
    if instance:
        return "instance"
    if node_type in ("CharacterBody2D", "CharacterBody3D", "RigidBody2D"):
        return "character"
    if node_type in ("Sprite2D", "AnimatedSprite2D", "Sprite3D"):
        return "visual"
    if script:
        return "controller"
    return "node"


def outline(text: str) -> list[dict]:
    """The scene as a list of nodes with their role, script and resources.

    One pass over the file: the canvas needs every node's card content at once,
    and a per-node request would be N round trips per repaint.
    """
    parsed = parse(text)
    by_id = {e["id"]: e for e in parsed["ext"]}
    out = []
    for node in parsed["nodes"]:
        props = properties(text, parsed, node)
        refs, script = [], ""
        for key, value in props.items():
            for rid in re.findall(r'ExtResource\("([^"]+)"\)', value):
                ext = by_id.get(rid)
                if not ext:
                    continue
                refs.append({"property": key, "id": rid, "path": ext["path"],
                             "type": ext["type"]})
                if key == "script":
                    script = ext["path"]
        if node["instance"]:
            for rid in re.findall(r'instance=ExtResource\("([^"]+)"\)',
                                  text[slice(*block_span(text, parsed, node))]):
                ext = by_id.get(rid)
                if ext:
                    refs.append({"property": "instance", "id": rid,
                                 "path": ext["path"], "type": ext["type"]})
        out.append({
            "name": node["name"],
            "path": node_path(node),
            "parent": node["parent"],
            "type": node["type"] or ("(instance)" if node["instance"] else ""),
            "instance": node["instance"],
            "script": script,
            "resources": refs,
            "properties": props,
            "role": role_for(node["type"], script=script, name=node["name"],
                             resources=[r["path"] for r in refs],
                             instance=node["instance"]),
        })

    # UI is a place in the tree, not a node type — but ONLY for the handful of
    # classes that are genuinely ambiguous. A ColorRect standing in for a
    # platform's art sits under a StaticBody2D and is a placeholder VISUAL; the
    # same class under a CanvasLayer is the HUD. A Label is never placeholder
    # art, and a scripted Node2D whose script happens to live in scripts/ui/ is
    # not a rectangle — demoting either of those (the first cut of this did)
    # turns a correct answer into a wrong one.
    by_path = {n["path"]: n for n in out}
    ui_hosts = {"CanvasLayer", "Control", "Panel", "VBoxContainer",
                "HBoxContainer", "PanelContainer", "MarginContainer"}
    ambiguous = {"ColorRect", "TextureRect", "NinePatchRect"}

    def under_ui(entry: dict) -> bool:
        parent = entry["parent"]
        while parent and parent != ".":
            host = by_path.get(parent)
            if host is None:
                return False
            if host["type"] in ui_hosts:
                return True
            parent = host["parent"]
        return False

    for entry in out:
        if entry["role"] == "ui" and entry["type"] in ambiguous \
                and not under_ui(entry):
            entry["role"] = "visual"
    return out


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------
def backup_dir(root: str | os.PathLike[str]) -> Path:
    return Path(root) / ".bgate_out" / "scene_backups"


def apply(scene_file: str | os.PathLike[str], new_text: str, *,
          root: str | os.PathLike[str], dry_run: bool = False) -> dict:
    """Write the edited scene, keeping a timestamped copy of what was there.

    A wiring mistake in a .tscn can cost an afternoon, and "undo" is not a thing
    a web UI has over a file the engine also owns — so the previous bytes are
    always still on disk under .bgate_out/scene_backups.
    """
    scene_file = Path(scene_file)
    if dry_run:
        return {"written": False, "backup": None, "bytes": len(new_text)}
    if not scene_file.is_file():
        raise WireError(f"{scene_file.name} does not exist")
    bdir = backup_dir(root)
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = bdir / f"{scene_file.stem}.{stamp}.tscn"
    shutil.copy2(scene_file, backup)
    tmp = scene_file.with_suffix(".tscn.tmp")
    tmp.write_text(new_text, encoding="utf-8", newline="\n")
    os.replace(tmp, scene_file)
    _note_write(root, scene_file)
    return {"written": True, "backup": str(backup.relative_to(Path(root))),
            "bytes": len(new_text)}


def _note_write(root, path) -> None:
    """A dispatched run's server-side scene write lands in its writelog.

    The PreToolUse hook records only the writes the AGENT'S OWN tools make
    (Write/Edit/Bash) - a scene written through an MCP tool went through this
    function in the server process and the hook never saw it. That blinded
    everything writelog feeds: the reopen brief's observed-writes block, the
    escalation report, and the completion evidence gate, which could not see
    that a level_generate run had written a scene at all. Best-effort by
    writelog's own rule: a bookkeeping line must never fail the write it
    describes.
    """
    try:
        owner = os.environ.get("BGATE_WORK_ITEM", "").strip()
        if not owner:
            return
        from . import writelog
        rel = Path(path).resolve().relative_to(Path(root).resolve())
        writelog.record(root, str(rel),
                        os.environ.get("BGATE_SEAT", "").strip(),
                        f"item-{owner}", tool="scenewire")
    except Exception:
        pass
