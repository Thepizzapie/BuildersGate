"""A Godot scene as engine-neutral JSON: tile layers, tilesets, node instances.

THE PORT READER. Moving a Godot game to another engine means rebuilding every
map, and a map is two things Godot keeps in formats nothing else reads: the
``tile_map_data`` blob of each TileMapLayer and the TileSet .tres it indexes
into. Everything else in a level scene (NPCs, exits, signs, triggers) is a
node instance whose meaning lives in its exported properties. This module
turns all three into one JSON document a C# or TypeScript loader can read
without knowing anything about Godot's text format.

WHY A SECOND PARSER and not ``scenewire.properties``: that one is an editor.
It keeps values as raw Godot literals on purpose, so a round trip never
rewrites a line nobody touched, and it reads one line per property. An
exporter wants the opposite: every value decoded into JSON, and values that
Godot wrapped across lines (arrays, dictionaries) read whole.

What the JSON does NOT do: apply rotation or scale. ``global_position`` is the
sum of the positions up the parent chain, which is exact for the flat Node2D
trees tile maps are made of, and the node carries ``global_approx: true``
whenever an ancestor rotates or scales so a loader knows to compose it.

I/O-free: the caller supplies a ``read(res_path) -> str | None`` callback.
"""
from __future__ import annotations

import base64
import binascii
import re
from typing import Callable, Optional

from bgate_core.level import tilemap

FORMAT = 1

Reader = Callable[[str], Optional[str]]


class ExportError(ValueError):
    """A file this module will not guess at."""


# ---------------------------------------------------------------------------
# Godot literals
# ---------------------------------------------------------------------------
# Constructors whose arguments are a flat list of numbers.
_VECTORS = {"Vector2", "Vector2i", "Vector3", "Vector3i", "Vector4",
            "Vector4i", "Quaternion", "Plane"}
_NUMBER_ARRAYS = {"PackedInt32Array", "PackedInt64Array", "PackedFloat32Array",
                  "PackedFloat64Array"}
# Packed arrays of vectors, and how many numbers make one element.
_VECTOR_ARRAYS = {"PackedVector2Array": 2, "PackedVector3Array": 3,
                  "PackedVector4Array": 4, "PackedColorArray": 4}


class _Lit:
    """A recursive-descent reader over one value, with a cursor."""

    def __init__(self, text: str, refs: Callable[[str, str], object]):
        self.s, self.i, self.refs = text, 0, refs

    def fail(self, why: str):
        raise ExportError(f"{why} at {self.i} in {self.s[:80]!r}")

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def peek(self) -> str:
        self.ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def eat(self, ch: str) -> None:
        if self.peek() != ch:
            self.fail(f"expected {ch!r}")
        self.i += 1

    def string(self) -> str:
        self.eat('"')
        out, s = [], self.s
        while self.i < len(s):
            c = s[self.i]
            if c == '"':
                self.i += 1
                return "".join(out)
            if c == "\\" and self.i + 1 < len(s):
                n = s[self.i + 1]
                self.i += 2
                if n == "u" and self.i + 4 <= len(s):
                    out.append(chr(int(s[self.i:self.i + 4], 16)))
                    self.i += 4
                else:
                    out.append({"n": "\n", "t": "\t", "r": "\r"}.get(n, n))
                continue
            out.append(c)
            self.i += 1
        self.fail("unterminated string")

    def args(self) -> list:
        self.eat("(")
        out = []
        while self.peek() != ")":
            out.append(self.value())
            if self.peek() == ",":
                self.i += 1
        self.eat(")")
        return out

    def value(self):
        c = self.peek()
        if not c:
            self.fail("expected a value")
        if c == '"':
            return self.string()
        if c in "&^":          # StringName, NodePath shorthand
            self.i += 1
            text = self.string()
            return text if c == "&" else {"node_path": text}
        if c == "[":
            self.i += 1
            out = []
            while self.peek() != "]":
                out.append(self.value())
                if self.peek() == ",":
                    self.i += 1
            self.eat("]")
            return out
        if c == "{":
            self.i += 1
            out = {}
            while self.peek() != "}":
                key = self.value()
                self.eat(":")
                out[key if isinstance(key, str) else str(key)] = self.value()
                if self.peek() == ",":
                    self.i += 1
            self.eat("}")
            return out
        m = re.compile(r"[-+]?(?:\d+\.?\d*(?:e[-+]?\d+)?|\.\d+(?:e[-+]?\d+)?)",
                       re.I).match(self.s, self.i)
        if m and m.group(0) not in ("-", "+"):
            self.i = m.end()
            tok = m.group(0)
            return float(tok) if any(ch in tok for ch in ".eE") else int(tok)
        m = re.compile(r"[A-Za-z_][A-Za-z0-9_]*").match(self.s, self.i)
        if not m:
            self.fail("unreadable value")
        word = m.group(0)
        self.i = m.end()
        if word in ("true", "false"):
            return word == "true"
        if word == "null":
            return None
        if word in ("inf", "inf_neg", "nan"):
            return {"godot": word}    # JSON has no inf or nan
        if self.peek() == "[":       # Array[int]([...]), Dictionary[K, V]({...})
            depth = 0
            while self.i < len(self.s):
                depth += {"[": 1, "]": -1}.get(self.s[self.i], 0)
                self.i += 1
                if depth == 0:
                    break
            args = self.args()
            return args[0] if args else []
        if self.peek() != "(":
            return {"godot": word}
        start = self.i
        args = self.args()
        return self.construct(word, args, self.s[start:self.i])

    def construct(self, word: str, args: list, raw: str):
        if word in _VECTORS or word in _NUMBER_ARRAYS:
            return args
        if word in _VECTOR_ARRAYS:
            n = _VECTOR_ARRAYS[word]
            return [args[k:k + n] for k in range(0, len(args), n)]
        if word in ("Rect2", "Rect2i"):
            return {"position": args[:2], "size": args[2:4]}
        if word == "Color":
            return args if len(args) == 4 else args + [1.0]
        if word == "Transform2D":
            return {"x": args[0:2], "y": args[2:4], "origin": args[4:6]}
        if word == "PackedStringArray":
            return args
        if word == "PackedByteArray":
            return {"bytes_b64": args[0]} if args and isinstance(args[0], str) \
                else {"bytes_b64": base64.b64encode(bytes(args)).decode("ascii")}
        if word == "NodePath":
            return {"node_path": args[0] if args else ""}
        if word == "StringName":
            return args[0] if args else ""
        if word in ("ExtResource", "SubResource"):
            return self.refs(word, args[0] if args else "")
        return {"godot": word + raw}


def parse_value(text: str, refs: Optional[Callable[[str, str], object]] = None):
    """One Godot literal as JSON-able Python. Unknown constructors survive raw."""
    refs = refs or (lambda kind, rid: {kind: rid})
    lit = _Lit(text, refs)
    out = lit.value()
    if lit.peek():
        lit.fail("trailing text after the value")
    return out


# ---------------------------------------------------------------------------
# Sections: [header attrs] then key = value lines
# ---------------------------------------------------------------------------
_HEAD_RE = re.compile(r"^\[(\w+)(.*)\]\s*$")
_ATTR_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\[[^\]]*\]|[^\s\]]+)')
_KEY_RE = re.compile(r"^([A-Za-z0-9_/:.\-]+)\s*=\s*(.*)$")


def _balanced(value: str) -> bool:
    depth, in_str, esc = 0, False, False
    for c in value:
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
    return depth <= 0 and not in_str


def sections(text: str) -> list[dict]:
    """Every ``[...]`` section with its attributes and raw property values.

    Values stay raw strings here; :func:`parse_value` decodes them once the
    resource ids they refer to are known. A value Godot wrapped across lines
    (an array, a dictionary, a long string) is read until its brackets and
    quotes balance.
    """
    out: list[dict] = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        head = _HEAD_RE.match(stripped) if stripped.startswith("[") else None
        if head:
            attrs = {k: v for k, v in _ATTR_RE.findall(head.group(2))}
            out.append({"tag": head.group(1), "attrs": attrs, "props": {}})
            continue
        if not out:
            continue
        m = _KEY_RE.match(stripped)
        if not m:
            continue
        value = m.group(2)
        while not _balanced(value) and i < len(lines):
            value += "\n" + lines[i]
            i += 1
        out[-1]["props"][m.group(1)] = value
    return out


def _attr(section: dict, key: str, default=None):
    raw = section["attrs"].get(key)
    if raw is None:
        return default
    try:
        return parse_value(raw)
    except ExportError:
        return raw


class _Doc:
    """One parsed .tscn or .tres: its resources, and a resolver for them."""

    def __init__(self, text: str, res_path: str):
        self.path = res_path
        self.sections = sections(text)
        if not self.sections or self.sections[0]["tag"] not in (
                "gd_scene", "gd_resource"):
            raise ExportError(f"{res_path} is not a Godot text scene or resource")
        self.ext: dict[str, dict] = {}
        self.sub: dict[str, dict] = {}
        for s in self.sections:
            if s["tag"] == "ext_resource":
                self.ext[str(_attr(s, "id"))] = {
                    "type": _attr(s, "type", ""), "path": _attr(s, "path", "")}
            elif s["tag"] == "sub_resource":
                self.sub[str(_attr(s, "id"))] = s

    def ref(self, kind: str, rid: str):
        if kind == "ExtResource":
            hit = self.ext.get(rid)
            if hit is None:
                return {"res": None, "missing_id": rid}
            return {"res": hit["path"], "type": hit["type"]}
        sub = self.sub.get(rid)
        if sub is None:
            return {"sub": rid, "missing": True}
        return {"sub": rid, "type": _attr(sub, "type", ""),
                "props": self.props(sub, skip_subs={rid})}

    def value(self, raw: str, skip_subs: frozenset | set = frozenset()):
        def refs(kind, rid):
            if kind == "SubResource" and rid in skip_subs:
                return {"sub": rid, "cycle": True}
            return self.ref(kind, rid)
        try:
            return parse_value(raw, refs)
        except ExportError:
            return {"godot": raw}

    def props(self, section: dict, skip: tuple = (),
              skip_subs: frozenset | set = frozenset()) -> dict:
        return {k: self.value(v, skip_subs) for k, v in section["props"].items()
                if k not in skip}


# ---------------------------------------------------------------------------
# The TileSet
# ---------------------------------------------------------------------------
_TILE_KEY_RE = re.compile(r"^(-?\d+):(-?\d+)/(.+)$")
_LAYER_KEY_RE = re.compile(r"^(physics|custom_data|navigation|occlusion|terrain_set)"
                           r"_layer_(\d+)/(.+)$")
_PHYS_RE = re.compile(r"^physics_layer_(\d+)/polygon_(\d+)/(points|one_way|"
                      r"one_way_margin)$")
_CUSTOM_RE = re.compile(r"^custom_data_(\d+)$")


def _atlas(doc: _Doc, section: dict, custom_names: dict[int, str]) -> dict:
    """One TileSetAtlasSource: its texture geometry and every tile it defines."""
    tiles: dict[tuple[int, int, int], dict] = {}
    extent: dict[tuple[int, int], dict] = {}
    plain: dict = {}
    for key, raw in section["props"].items():
        m = _TILE_KEY_RE.match(key)
        if not m:
            plain[key] = doc.value(raw)
            continue
        ax, ay, rest = int(m.group(1)), int(m.group(2)), m.group(3)
        head, _, tail = rest.partition("/")
        if not head.isdigit():
            # Atlas-tile level: size_in_atlas, animation_*, next_alternative_id.
            extent.setdefault((ax, ay), {})[rest] = doc.value(raw)
            continue
        tile = tiles.setdefault((ax, ay, int(head)), {
            "ax": ax, "ay": ay, "alt": int(head)})
        if not tail:
            continue
        val = doc.value(raw)
        pm, cm = _PHYS_RE.match(tail), _CUSTOM_RE.match(tail)
        if pm:
            layer, poly, field = int(pm.group(1)), int(pm.group(2)), pm.group(3)
            polys = tile.setdefault("collision", {}).setdefault(str(layer), {})
            polys.setdefault(poly, {})[field] = val
        elif cm:
            idx = int(cm.group(1))
            tile.setdefault("custom", {})[custom_names.get(idx, str(idx))] = val
        else:
            tile.setdefault("props", {})[tail] = val

    out_tiles = []
    for key in sorted(tiles):
        tile = tiles[key]
        if "collision" in tile:
            tile["collision"] = {
                layer: [dict({"points": p.get("points", [])},
                             **{k: v for k, v in p.items() if k != "points"})
                        for _, p in sorted(polys.items())]
                for layer, polys in tile["collision"].items()}
        ext = extent.get((tile["ax"], tile["ay"]), {})
        if ext and tile["alt"] == 0:
            tile["atlas"] = ext
        out_tiles.append(tile)

    tex = plain.pop("texture", None)
    return {
        "kind": "atlas",
        "texture": tex.get("res") if isinstance(tex, dict) else tex,
        "region_size": plain.pop("texture_region_size", None),
        "margins": plain.pop("margins", [0, 0]),
        "separation": plain.pop("separation", [0, 0]),
        "use_texture_padding": plain.pop("use_texture_padding", True),
        "props": plain,
        "tiles": out_tiles,
    }


def export_tileset(doc: _Doc, resource: dict) -> dict:
    """A TileSet's resource section (from a .tres or an inline sub_resource)."""
    raw = resource["props"]
    layers: dict[str, dict] = {}
    for key, value in raw.items():
        m = _LAYER_KEY_RE.match(key)
        if m:
            layers.setdefault(m.group(1), {}).setdefault(
                int(m.group(2)), {})[m.group(3)] = doc.value(value)
    custom = layers.get("custom_data", {})
    custom_names = {i: str(v.get("name", i)) for i, v in custom.items()}

    sources = {}
    for key, value in raw.items():
        m = re.match(r"^sources/(\d+)$", key)
        if not m:
            continue
        ref = re.match(r'\s*(SubResource|ExtResource)\("([^"]+)"\)', value)
        section = doc.sub.get(ref.group(2)) if ref and ref.group(1) == \
            "SubResource" else None
        if section is None:
            sources[m.group(1)] = {"kind": "unsupported", "raw": value}
            continue
        stype = _attr(section, "type", "")
        if stype == "TileSetAtlasSource":
            sources[m.group(1)] = _atlas(doc, section, custom_names)
        else:
            sources[m.group(1)] = {"kind": stype,
                                   "props": doc.props(section)}

    def _int(name, default):
        v = doc.value(raw[name]) if name in raw else default
        return v if isinstance(v, int) else default

    return {
        "tile_size": doc.value(raw["tile_size"]) if "tile_size" in raw
        else [16, 16],
        "shape": _int("tile_shape", tilemap.SQUARE),
        "layout": _int("tile_layout", 0),
        "offset_axis": _int("tile_offset_axis", 0),
        "layers": {kind: [dict(v, index=i) for i, v in sorted(by.items())]
                   for kind, by in layers.items()},
        "sources": sources,
    }


def _tileset_for(doc: _Doc, value_raw: str, read: Reader,
                 cache: dict) -> tuple[Optional[str], Optional[dict], list]:
    """The tileset a layer's ``tile_set`` points at, keyed for the output."""
    warnings: list[str] = []
    m = re.match(r'\s*(ExtResource|SubResource)\("([^"]+)"\)', value_raw or "")
    if not m:
        return None, None, [f"tile_set {value_raw!r} is not a resource reference"]
    if m.group(1) == "SubResource":
        key = f"{doc.path}::{m.group(2)}"
        if key not in cache:
            section = doc.sub.get(m.group(2))
            cache[key] = export_tileset(doc, section) if section else None
        return key, cache[key], warnings
    hit = doc.ext.get(m.group(2))
    if not hit:
        return None, None, [f"tile_set id {m.group(2)!r} has no ext_resource"]
    path = hit["path"]
    if path not in cache:
        text = read(path)
        if text is None:
            cache[path] = None
            warnings.append(f"tileset {path} could not be read")
        else:
            tdoc = _Doc(text, path)
            resource = next((s for s in tdoc.sections if s["tag"] == "resource"),
                            None)
            cache[path] = export_tileset(tdoc, resource) if resource else None
    return path, cache[path], warnings


# ---------------------------------------------------------------------------
# The scene
# ---------------------------------------------------------------------------
_TRANSFORM_KEYS = ("rotation", "rotation_degrees", "scale", "skew", "transform")


def _instance_info(path: str, read: Reader, cache: dict, depth: int = 0) -> dict:
    """What an instanced scene's root IS: its type and script, followed down
    nested instances, because ``npc.tscn`` may itself instance ``actor.tscn``.
    """
    if path in cache:
        return cache[path]
    cache[path] = {"type": "", "script": None}     # cycle guard
    text = read(path) if depth < 8 else None
    if text is None:
        return cache[path]
    try:
        doc = _Doc(text, path)
    except ExportError:
        return cache[path]
    root = next((s for s in doc.sections if s["tag"] == "node"), None)
    if root is None:
        return cache[path]
    info = {"type": _attr(root, "type", "") or "", "script": None}
    inst = root["attrs"].get("instance")
    if inst:
        ref = doc.value(inst)
        if isinstance(ref, dict) and ref.get("res"):
            info = dict(_instance_info(ref["res"], read, cache, depth + 1))
    script = root["props"].get("script")
    if script:
        ref = doc.value(script)
        if isinstance(ref, dict) and ref.get("res"):
            info["script"] = ref["res"]
    cache[path] = info
    return info


def export_scene(text: str, res_path: str, read: Reader) -> dict:
    """The whole scene as one JSON-able dict. See the module docstring."""
    doc = _Doc(text, res_path)
    if doc.sections[0]["tag"] != "gd_scene":
        raise ExportError(f"{res_path} is a resource, not a scene")
    warnings: list[str] = []
    tilesets: dict[str, Optional[dict]] = {}
    instances: dict[str, dict] = {}
    nodes: list[dict] = []
    layers: list[dict] = []
    by_path: dict[str, dict] = {}

    for s in doc.sections:
        if s["tag"] != "node":
            continue
        name = str(_attr(s, "name", ""))
        parent = _attr(s, "parent")
        path = "." if parent is None else (
            name if parent == "." else f"{parent}/{name}")
        node = {"path": path, "name": name,
                "parent": parent, "type": _attr(s, "type", "") or ""}
        inst = s["attrs"].get("instance")
        if inst:
            ref = doc.value(inst)
            scene = ref.get("res") if isinstance(ref, dict) else None
            node["instance"] = scene
            if scene:
                info = _instance_info(scene, read, instances)
                node["type"] = node["type"] or info["type"]
                node["script"] = info["script"]
        groups = _attr(s, "groups")
        if groups:
            node["groups"] = groups

        skip = ("tile_map_data",)
        props = doc.props(s, skip=skip)
        script = props.get("script")
        if isinstance(script, dict) and script.get("res"):
            node["script"] = script["res"]
            props.pop("script")

        # Position up the chain. Exact for flat Node2D trees; flagged otherwise.
        own = props.get("position")
        own = own if isinstance(own, list) and len(own) == 2 else [0, 0]
        anc = by_path.get(parent if parent not in (None, ".") else ".")
        base = anc["global_position"] if anc and parent is not None else [0, 0]
        node["global_position"] = [base[0] + own[0], base[1] + own[1]]
        approx = bool(anc and anc.get("_xform")) or any(
            k in props for k in _TRANSFORM_KEYS)
        node["_xform"] = approx
        if bool(anc and anc.get("_xform")):
            node["global_approx"] = True
        node["props"] = props

        if node["type"] == "TileMapLayer":
            key, ts, warn = _tileset_for(doc, s["props"].get("tile_set", ""),
                                         read, tilesets)
            warnings += [f"{path}: {w}" for w in warn]
            try:
                packed = doc.value(s["props"].get("tile_map_data", '""'))
                blob = packed.get("bytes_b64", "") if isinstance(packed, dict) \
                    else ""
                cells = tilemap.decode_cells(blob)
            except (tilemap.TileError, binascii.Error) as exc:
                warnings.append(f"{path}: tile data unreadable: {exc}")
                cells = []
            layers.append({
                "path": path, "name": name, "tileset": key,
                "z_index": props.get("z_index", 0),
                "visible": props.get("visible", True),
                "y_sort": props.get("y_sort_enabled", False),
                "collision": props.get("collision_enabled", True),
                "global_position": node["global_position"],
                "cells": [[c["x"], c["y"], c["source"], c["ax"], c["ay"],
                           c["alt"]] for c in sorted(
                               cells, key=lambda c: (c["y"], c["x"]))],
            })
            props.pop("tile_set", None)
        elif node["type"] == "TileMap" and any(
                k.endswith("/tile_data") for k in s["props"]):
            warnings.append(f"{path}: legacy TileMap (Godot 4.0-4.2 layers) is "
                            "not exported; convert it to TileMapLayer in Godot")
        by_path[path] = node
        nodes.append(node)

    for n in nodes:
        n.pop("_xform", None)
    header = doc.sections[0]
    return {
        "format": FORMAT,
        "scene": res_path,
        "uid": _attr(header, "uid"),
        "root": nodes[0]["name"] if nodes else None,
        "tilesets": tilesets,
        "layers": layers,
        "nodes": nodes,
        "warnings": warnings,
    }
