"""The form tools: build the smooth shape FIRST, as a .glb, in one call.

Each wraps one kit function from _blender_form_kit (bg_hull, bg_skin, bg_blob,
bg_sweep, bg_rock) so an agent that is not writing a bpy script still starts
from a silhouette, a stick figure or blobs rather than a stack of boxes. The
scripts run through surface._run: the report goes to a file, the glb through
the runner's export (which flattens image-less procedural inputs to their
constants).
"""

from __future__ import annotations

from typing import Optional

from .surface import MATERIAL_PRESETS, _DUMP, _run

__all__ = ["hull", "skin", "blob", "sweep", "rock", "MATERIAL_PRESETS"]


_HEAD = r'''
import json
P = json.loads(r"""__PAYLOAD__""")
''' + _DUMP + r'''
bg_wipe()
mat = None
if P.get("preset"):
    mat = bg_material(P.get("material_name") or (P["name"] + "Mat"), P["preset"], P.get("colour"),
                      scale=float(P.get("scale") or 1.0))

def _done(obj, **extra):
    obj.data.calc_loop_triangles()
    row = {"ok": True, "name": obj.name, "tris": len(obj.data.loop_triangles),
           "dims": list(bg_bounds(obj)["dims"]),
           "materials": [m.name for m in obj.data.materials if m]}
    row.update(extra)
    _dump(row)
'''

_HULL = _HEAD + r'''
obj = bg_hull(P["name"], P["side"], P["top"], front=P.get("front"), round=int(P.get("round") or 0),
              voxel=float(P.get("voxel") or 0.0), target_tris=int(P.get("target_tris") or 0),
              material=mat, bevel=float(P.get("bevel") or 0.0))
_done(obj, round=int(P.get("round") or 0))
'''

_SKIN = _HEAD + r'''
obj = bg_skin(P["name"], P["joints"], P["links"], subsurf=int(P.get("subsurf") or 0),
              material=mat, mirror_x=bool(P.get("mirror_x")))
_done(obj, joints=len(P["joints"]))
'''

_BLOB = _HEAD + r'''
obj = bg_blob(P["name"], P["balls"], resolution=float(P.get("resolution") or 0.06),
              threshold=float(P.get("threshold") or 0.6), target_tris=int(P.get("target_tris") or 0),
              material=mat)
_done(obj, balls=len(P["balls"]))
'''

_SWEEP = _HEAD + r'''
obj = bg_sweep(P["name"], P["points"], P["radii"], segments=int(P.get("segments") or 12),
               caps=bool(P.get("caps", True)), smooth_path=bool(P.get("smooth_path", True)), material=mat)
_done(obj, points=len(P["points"]))
'''

_ROCK = _HEAD + r'''
obj = bg_rock(P["name"], size=tuple(P.get("size") or (1.0, 0.8, 0.6)), seed=int(P.get("seed") or 1),
              detail=int(P.get("detail") or 3), roughness=float(P.get("roughness") or 0.3),
              facets=float(P.get("facets") or 0.0), flat_bottom=bool(P.get("flat_bottom", True)),
              material=mat)
_done(obj, seed=int(P.get("seed") or 1))
'''


def _preset(preset):
    if preset and preset not in MATERIAL_PRESETS:
        raise ValueError(f"unknown preset {preset!r}; one of {sorted(MATERIAL_PRESETS)}")
    return preset or None


def _outline(points, what, arity=2):
    pts = [list(p) for p in (points or [])]
    if len(pts) < 3 or any(len(p) != arity for p in pts):
        raise ValueError(f"{what} must be a closed outline of at least three [{'a, b' if arity == 2 else 'x, y, z'}] points")
    return pts


def hull(side: list, top: list, out_path, *, front: Optional[list] = None, name: str = "Hull",
         round: int = 2, voxel: float = 0.0, target_tris: int = 0, bevel: float = 0.0,
         preset: str = "", colour=None, timeout: int = 600) -> dict:
    """A body from its silhouettes: side [(x, z)] x top [(x, y)] (x front [(y, z)]). See bg_hull."""
    payload = {"name": name, "side": _outline(side, "side"), "top": _outline(top, "top"),
               "front": _outline(front, "front") if front else None,
               "round": int(round), "voxel": float(voxel), "target_tris": int(target_tris),
               "bevel": float(bevel), "preset": _preset(preset), "colour": colour}
    return _run(_HULL, payload, out_path, timeout)


def skin(joints: list, links: list, out_path, *, name: str = "Skin", subsurf: int = 2,
         mirror_x: bool = False, preset: str = "", colour=None, timeout: int = 600) -> dict:
    """A body from a stick figure: joints [(x, y, z, r)], links [(i, j)]. See bg_skin."""
    js = [list(j) for j in (joints or [])]
    if len(js) < 2 or any(len(j) != 4 for j in js):
        raise ValueError("joints must be at least two [x, y, z, radius] entries")
    ls = [list(l) for l in (links or [])]
    if not ls or any(len(l) != 2 or not (0 <= int(l[0]) < len(js) and 0 <= int(l[1]) < len(js)) for l in ls):
        raise ValueError("links must be [i, j] index pairs into joints")
    payload = {"name": name, "joints": js, "links": ls, "subsurf": int(subsurf), "mirror_x": bool(mirror_x),
               "preset": _preset(preset), "colour": colour}
    return _run(_SKIN, payload, out_path, timeout)


def blob(balls: list, out_path, *, name: str = "Blob", resolution: float = 0.06, threshold: float = 0.6,
         target_tris: int = 0, preset: str = "", colour=None, timeout: int = 600) -> dict:
    """A body from merging blobs: balls [(x, y, z, r)] or [(x, y, z, r, sx, sy, sz)]. See bg_blob."""
    bs = [list(b) for b in (balls or [])]
    if not bs or any(len(b) not in (4, 7) for b in bs):
        raise ValueError("balls must be [x, y, z, radius] or [x, y, z, radius, sx, sy, sz] entries")
    payload = {"name": name, "balls": bs, "resolution": float(resolution), "threshold": float(threshold),
               "target_tris": int(target_tris), "preset": _preset(preset), "colour": colour}
    return _run(_BLOB, payload, out_path, timeout)


def sweep(points: list, radii, out_path, *, name: str = "Sweep", segments: int = 12, caps: bool = True,
          smooth_path: bool = True, preset: str = "", colour=None, timeout: int = 300) -> dict:
    """A tube along a path with a radius per point. See bg_sweep."""
    pts = [list(p) for p in (points or [])]
    if len(pts) < 2 or any(len(p) != 3 for p in pts):
        raise ValueError("points must be at least two [x, y, z] entries")
    if isinstance(radii, (int, float)):
        radii = [float(radii)] * len(pts)
    radii = [float(r) for r in radii]
    if len(radii) != len(pts):
        raise ValueError("radii must be one number per point (or a single number)")
    payload = {"name": name, "points": pts, "radii": radii, "segments": int(segments), "caps": bool(caps),
               "smooth_path": bool(smooth_path), "preset": _preset(preset), "colour": colour}
    return _run(_SWEEP, payload, out_path, timeout)


def rock(out_path, *, name: str = "Rock", size=(1.0, 0.8, 0.6), seed: int = 1, detail: int = 4,
         roughness: float = 0.3, facets: float = 0.0, flat_bottom: bool = True, preset: str = "",
         colour=None, timeout: int = 300) -> dict:
    """A seeded boulder. See bg_rock."""
    sz = [float(s) for s in size]
    if len(sz) != 3 or min(sz) <= 0:
        raise ValueError("size must be three positive metres [x, y, z]")
    payload = {"name": name, "size": sz, "seed": int(seed), "detail": max(1, min(5, int(detail))),
               "roughness": float(roughness), "facets": float(facets), "flat_bottom": bool(flat_bottom),
               "preset": _preset(preset), "colour": colour}
    return _run(_ROCK, payload, out_path, timeout)
