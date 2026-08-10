"""The model sidecar — what a human knows about a 3D mesh that the geometry does
not.

rigmap.py exists because a sprite sheet is a grid of pixels and nothing else:
which cells form the walk cycle, where the main hand is in frame 7, lives in
the artist's head until it is written down next to the sheet. A .glb has the
identical problem in three dimensions. gear.py can equip a weapon onto a 2D
rig once a hand is labelled; a 3D character has no equivalent place to say
"the sword goes here, tilted like this" — the mesh only knows its own
vertices. This module is that place, for models instead of sheets.

Same shape, same reasons, sidecar and not embedded or DB-resident:

  * Sidecar and not embedded metadata — a .glb re-exported by Blender or
    re-fetched from a generator carries none of the extras a previous editing
    session added, and glTF's own extras mechanism is not something every
    exporter round-trips intact.
  * Sidecar and not the database — the labels belong to the ART. They must
    survive a checkout, a copy into another project, a database that was
    never initialised, exactly like the rig sidecar.

What a sidecar holds:

  * CAMERA — where the editor was last looking, so reopening a model resumes
    the framing instead of the default orbit.
  * DISPLAY — shading mode, grid/ground/autorotate toggles. Cosmetic, but
    saved because "wireframe was the whole point of opening this again" is a
    real workflow (checking topology) and re-clicking it every time is a tax.
  * NODES — per-node visibility and an optional colour tint, keyed by the
    node/mesh name glTF assigns. Lets a reviewer hide a placeholder collision
    mesh or spot-check a material variant without touching the file.
  * SOCKETS — named 3D attachment points, world-space position and Euler
    rotation, optionally hung off a named node. This is the 3D counterpart of
    rigmap's slot anchors: gear.py labels WHERE on a 2D frame a weapon sits;
    a socket says the same thing for a mesh a weapon would be parented to in
    Godot. Slot names are drawn from the SAME taxonomy as rigmap.KNOWN_SLOTS
    on purpose — "main_hand" means the same thing whether the character is a
    sprite sheet or a mesh, and a project that ships both must not maintain
    two vocabularies for one idea.

Pure stdlib. No database, no engine, no network — and deliberately no
Pillow/trimesh/three-in-Python dependency: this module never opens the mesh
itself, it only reads and validates what the browser-side viewer already
computed and sent back.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from .rigmap import KNOWN_SLOTS, RigError, slot_name  # one taxonomy, 2D and 3D alike

SCHEMA_VERSION = 1

DISPLAY_MODES: tuple[str, ...] = ("shaded", "wireframe", "unlit", "normals")

_NAME_RE = re.compile(r"[^a-z0-9_]+")

# A model with more sockets than this is not something a person placed by
# hand — refusing a runaway payload here is the same guard rigmap puts on
# MAX_CELLS.
MAX_SOCKETS = 512
MAX_NODE_OVERRIDES = 4096

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class ModelError(ValueError):
    """A sidecar that cannot be trusted. Never raised for a MISSING sidecar —
    absent metadata is the normal state, and `load` returns an empty record."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def sidecar_path(model: str | os.PathLike[str]) -> Path:
    """``hero.glb`` -> ``hero.model3d.json``, alongside the model.

    Suffix-replacing rather than appending keeps the sidecar from ever being
    mistaken for a Godot-importable resource, same rationale as
    rigmap.sidecar_path.
    """
    p = Path(model)
    return p.with_suffix("").with_name(p.stem + ".model3d.json")


def node_name(raw: str) -> str:
    """Fold a node label to a stable key. Node names come from the glTF file
    itself (whatever Blender/the exporter called them) so, unlike a slot name,
    case and punctuation are preserved — only surrounding whitespace and a
    length cap are enforced, because the name must still match the node the
    viewer resolves it against."""
    name = str(raw or "").strip()
    if not name:
        raise ModelError("a node override needs a name")
    return name[:200]


def socket_name(raw: str) -> str:
    name = _NAME_RE.sub("_", str(raw or "").strip().lower()).strip("_")
    if not name:
        raise ModelError("a socket needs a name")
    return name[:64]


def _vec3(value, field: str, *, lo: float = -1e6, hi: float = 1e6) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ModelError(f"{field}: expected [x, y, z], got {value!r}")
    out = []
    for i, v in enumerate(value):
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ModelError(f"{field}[{i}]: expected a number, got {v!r}")
        if not lo <= f <= hi:
            raise ModelError(f"{field}[{i}]: {f} out of range [{lo}, {hi}]")
        out.append(f)
    return out


def _float(value, field: str, *, lo: float, hi: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ModelError(f"{field}: expected a number, got {value!r}")
    if not lo <= f <= hi:
        raise ModelError(f"{field}: {f} out of range [{lo}, {hi}]")
    return f


def empty() -> dict:
    """A well-formed record with nothing in it. What `load` returns for no
    sidecar."""
    return {
        "version": SCHEMA_VERSION,
        "camera": None,
        "display": {
            "mode": "shaded", "grid": True, "ground": False,
            "autorotate": False, "background": None,
        },
        "nodes": {},
        "sockets": [],
        "notes": "",
        "updated_at": None,
    }


def normalise(data: dict) -> dict:
    """Validate and canonicalise a model-editor payload. Raises ModelError on
    anything bad. Everything the editor POSTs comes through here, so the
    on-disk file is always in one shape."""
    if not isinstance(data, dict):
        raise ModelError("payload must be an object")

    out = empty()
    out["notes"] = str(data.get("notes") or "")[:4000]

    cam = data.get("camera")
    if cam:
        if not isinstance(cam, dict):
            raise ModelError("camera must be an object")
        out["camera"] = {
            "position": _vec3(cam.get("position"), "camera.position"),
            "target": _vec3(cam.get("target"), "camera.target"),
            "fov": _float(cam.get("fov", 50), "camera.fov", lo=5.0, hi=150.0),
        }

    disp = data.get("display") or {}
    if not isinstance(disp, dict):
        raise ModelError("display must be an object")
    mode = str(disp.get("mode") or "shaded")
    if mode not in DISPLAY_MODES:
        raise ModelError(f"display.mode must be one of {DISPLAY_MODES}")
    bg = disp.get("background")
    if bg is not None and not _HEX_RE.match(str(bg)):
        raise ModelError("display.background must be a #rrggbb colour or null")
    out["display"] = {
        "mode": mode,
        "grid": bool(disp.get("grid", True)),
        "ground": bool(disp.get("ground", False)),
        "autorotate": bool(disp.get("autorotate", False)),
        "background": bg,
    }

    nodes_in = data.get("nodes") or {}
    if not isinstance(nodes_in, dict):
        raise ModelError("nodes must be an object keyed by node name")
    if len(nodes_in) > MAX_NODE_OVERRIDES:
        raise ModelError(f"{len(nodes_in)} node overrides exceeds the cap of "
                         f"{MAX_NODE_OVERRIDES}")
    nodes_out: dict[str, dict] = {}
    for raw_name, raw_ov in nodes_in.items():
        name = node_name(raw_name)
        if not isinstance(raw_ov, dict):
            raise ModelError(f"nodes[{name!r}] must be an object")
        color = raw_ov.get("color")
        if color is not None and not _HEX_RE.match(str(color)):
            raise ModelError(f"nodes[{name!r}].color must be a #rrggbb colour "
                             "or null")
        ov = {"visible": bool(raw_ov.get("visible", True)), "color": color}
        if raw_ov.get("opacity") is not None:
            ov["opacity"] = _float(raw_ov["opacity"], f"nodes[{name!r}].opacity",
                                   lo=0.0, hi=1.0)
        nodes_out[name] = ov
    out["nodes"] = nodes_out

    sockets_in = data.get("sockets") or []
    if not isinstance(sockets_in, list):
        raise ModelError("sockets must be a list")
    if len(sockets_in) > MAX_SOCKETS:
        raise ModelError(f"{len(sockets_in)} sockets exceeds the cap of "
                         f"{MAX_SOCKETS}")
    seen: set[str] = set()
    sockets_out = []
    for i, raw in enumerate(sockets_in):
        if not isinstance(raw, dict):
            raise ModelError(f"sockets[{i}] must be an object")
        name = socket_name(raw.get("name"))
        if name in seen:
            raise ModelError(f"duplicate socket name {name!r}")
        seen.add(name)
        entry = {
            "name": name,
            "node": (node_name(raw["node"]) if raw.get("node") else None),
            "position": _vec3(raw.get("position") or [0, 0, 0],
                              f"sockets[{i}].position"),
            "rotation": _vec3(raw.get("rotation") or [0, 0, 0],
                              f"sockets[{i}].rotation", lo=-360.0, hi=360.0),
            "note": str(raw.get("note") or "")[:200],
        }
        sockets_out.append(entry)
    out["sockets"] = sockets_out

    return out


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------
def load(model: str | os.PathLike[str]) -> dict:
    """The sidecar for a model. A missing sidecar is an EMPTY record, not an
    error. A corrupt sidecar IS an error — silently discarding hand-placed
    sockets because a byte flipped would be the worst possible failure here."""
    path = sidecar_path(model)
    if not path.is_file():
        return empty()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelError(f"{path.name} is unreadable: {exc}") from exc
    data = normalise(raw)
    data["updated_at"] = raw.get("updated_at")
    return data


def save(model: str | os.PathLike[str], data: dict) -> dict:
    """Normalise, then write the sidecar atomically. Returns what was written."""
    out = normalise(data)
    out["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = sidecar_path(model)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return out


def delete(model: str | os.PathLike[str]) -> bool:
    path = sidecar_path(model)
    if path.is_file():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def sockets_for(data: dict, node: Optional[str] = None) -> list[dict]:
    """Sockets hung off one node, or every socket when ``node`` is omitted."""
    if node is None:
        return list(data.get("sockets") or [])
    want = node_name(node)
    return [s for s in (data.get("sockets") or []) if s.get("node") == want]


def known_slot_hint(name: str) -> bool:
    """Whether a socket name matches the shared 2D/3D slot taxonomy — the
    editor uses this to suggest the known list first without REQUIRING it;
    free-form socket names (a custom prop point, a VFX origin) are legal."""
    try:
        return slot_name(name) in KNOWN_SLOTS
    except RigError:
        return False


__all__ = [
    "SCHEMA_VERSION", "DISPLAY_MODES", "MAX_SOCKETS", "MAX_NODE_OVERRIDES",
    "ModelError", "sidecar_path", "node_name", "socket_name", "empty",
    "normalise", "load", "save", "delete", "sockets_for", "known_slot_hint",
    "KNOWN_SLOTS",
]
