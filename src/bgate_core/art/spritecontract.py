"""The sprite contract: what a sheet IS for this game, declared once.

Every game needs a different sheet shape — an E/W side-scroller, a four-corner
top-down office, an eight-direction isometric — and until now that shape lived
nowhere the pipeline could read. `view` was a prose prompt prefix that died in
animspec; direction sets existed only as post-hoc measurement; mirroring was a
hint inside an error string. The cost is shipped and documented: downsizing's
unit_marker.gd hand-encodes all of it in game code, per character, per action,
because generation could not be trusted to agree with itself — its comments
record ten of the drone's twenty facings shipping WRONG, each sheet generated
one action at a time, each generation picking its own side.

This module inverts that: the contract is DECLARED (a preset plus overrides),
generation is parameterised by it, the checks grade against it, and the
emitters lay sheets out to it. Swapping a game's whole sprite shape is one
`sprite_contract_set(preset=...)`.

Resolution precedence, most specific first, the same shape seats and settings
use: per-(character, action) override -> per-character override -> project doc
-> preset -> DEFAULTS. Stored as ONE workspace doc so structured config rides
the store that already exists for it (settings' registry doc pattern; the
settings table itself has no dict kind, deliberately).

Vocabulary: compass directions, lowercase (n, ne, e, se, s, sw, w, nw).
`directions` is what the GAME plays; `drawn` is what gets GENERATED; `mirror`
maps every non-drawn direction to the drawn one it flips from. Sheet `rows`
name which drawn direction each grid row holds, top to bottom.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from ..store import workspace as _ws

#: Where the contract lives. The director owns sheet shape the way it owns the
#: registry settings doc: it is project canon, not any one seat's scratch.
SEAT = "director"
DOC_KEY = "sprite_contract"

DIRECTIONS = ("n", "ne", "e", "se", "s", "sw", "w", "nw")

#: What each direction mirrors to under a horizontal flip. Vertical flips are
#: not a thing sprite work does (an upside-down walk is not a direction).
H_MIRROR = {"e": "w", "w": "e", "ne": "nw", "nw": "ne", "se": "sw", "sw": "se"}

VIEWS = ("side", "top_down_3q", "isometric", "top_down")

#: The projection directive per view — prompt text a generator appends. The
#: isometric wording matches artdirection's own vocabulary entry so the bible
#: path and the contract path cannot disagree about what isometric means.
VIEW_CLAUSES = {
    "side": "strict side view, pure profile, no perspective",
    "top_down_3q": "three-quarter top-down view, seen from slightly above",
    "isometric": ("angled 3/4 isometric view, 2:1 tile geometry, never flat "
                  "top-down and never straight-on front view"),
    "top_down": "flat top-down view, seen from directly above",
}

DEFAULTS: dict[str, Any] = {
    "preset": "single",
    "view": "side",
    "directions": ["e"],
    "drawn": ["e"],
    "mirror": {},
    "rows": ["e"],
    "cell": [128, 128],
    "layout": "strip",
    "actions": {},           # {} = defer to animspec's archetype catalogue
    "characters": {},
}

PRESETS: dict[str, dict] = {
    # One facing, a plain strip — the shape every existing sheet already has,
    # so a project that never sets a contract changes nothing.
    "single": {},
    # Classic side-scroller: draw east, flip for west. Half the sheets for free
    # and the two facings CANNOT disagree, because they are the same pixels.
    "sidescroller": {
        "view": "side",
        "directions": ["e", "w"], "drawn": ["e"], "mirror": {"w": "e"},
        "rows": ["e"], "layout": "strip",
    },
    # Downsizing's proven shape: four corner facings, two drawn (one back, one
    # front), two mirrored; sheets are a cols x 2 grid, back row above front.
    "four_corner": {
        "view": "top_down_3q",
        "directions": ["ne", "nw", "se", "sw"],
        "drawn": ["nw", "sw"], "mirror": {"ne": "nw", "se": "sw"},
        "rows": ["nw", "sw"], "layout": "grid_rows",
        "cell": [96, 80],
    },
    # Cardinal four: north and south have no mirror partner and must be drawn.
    "four_dir": {
        "view": "top_down_3q",
        "directions": ["n", "e", "s", "w"],
        "drawn": ["n", "e", "s"], "mirror": {"w": "e"},
        "rows": ["n", "e", "s"], "layout": "grid_rows",
    },
    # Full eight: five drawn, three mirrored.
    "eight_dir": {
        "view": "top_down_3q",
        "directions": list(DIRECTIONS),
        "drawn": ["n", "ne", "e", "se", "s"],
        "mirror": {"nw": "ne", "w": "e", "sw": "se"},
        "rows": ["n", "ne", "e", "se", "s"], "layout": "grid_rows",
    },
}


class ContractError(ValueError):
    """A contract that cannot be trusted. Refused, never repaired — a typo'd
    direction silently becoming a new facing is exactly the drift this module
    exists to end."""


def normalise(data: dict) -> dict:
    """Validate and canonicalise a contract payload. Raises ContractError.

    Everything a set() writes comes through here, so the stored doc is always
    in one shape and every reader downstream can skip defensive parsing —
    rigmap.normalise's contract, applied to a different document.
    """
    if not isinstance(data, dict):
        raise ContractError("contract must be an object")
    out = dict(DEFAULTS)
    out.update({k: data[k] for k in DEFAULTS if k in data})

    preset = str(out.get("preset") or "single")
    if preset not in PRESETS:
        raise ContractError(f"no preset {preset!r} — have: {sorted(PRESETS)}")
    out["preset"] = preset

    view = str(out.get("view") or "side")
    if view not in VIEWS:
        raise ContractError(f"view must be one of {VIEWS}, got {view!r}")
    out["view"] = view

    def _dirs(field: str) -> list[str]:
        raw = out.get(field) or []
        if not isinstance(raw, (list, tuple)) or not raw:
            raise ContractError(f"{field} must be a non-empty list")
        seen: list[str] = []
        for entry in raw:
            name = str(entry).strip().lower()
            if name not in DIRECTIONS:
                raise ContractError(
                    f"{field}: {entry!r} is not a direction — compass names "
                    f"only: {DIRECTIONS}")
            if name not in seen:
                seen.append(name)
        return seen

    out["directions"] = _dirs("directions")
    out["drawn"] = _dirs("drawn")
    out["rows"] = _dirs("rows")

    for name in out["drawn"]:
        if name not in out["directions"]:
            raise ContractError(f"drawn direction {name!r} is not in directions")
    for name in out["rows"]:
        if name not in out["drawn"]:
            raise ContractError(
                f"row {name!r} is not a drawn direction — a sheet row holds "
                "generated pixels, and mirrored facings are made at runtime")

    mirror = out.get("mirror") or {}
    if not isinstance(mirror, dict):
        raise ContractError("mirror must be an object of direction -> direction")
    clean_mirror: dict[str, str] = {}
    for target, source in mirror.items():
        t, s = str(target).strip().lower(), str(source).strip().lower()
        if t not in DIRECTIONS or s not in DIRECTIONS:
            raise ContractError(f"mirror {target!r}: {source!r} names a non-direction")
        if s not in out["drawn"]:
            raise ContractError(
                f"mirror {t!r} <- {s!r}: the source is not drawn, so there is "
                "nothing to flip")
        if H_MIRROR.get(s) != t:
            raise ContractError(
                f"mirror {t!r} <- {s!r} is not a horizontal flip pair — a "
                f"flipped {s!r} faces {H_MIRROR.get(s, '(nothing)')!r}")
        clean_mirror[t] = s
    out["mirror"] = clean_mirror

    for name in out["directions"]:
        if name not in out["drawn"] and name not in clean_mirror:
            raise ContractError(
                f"direction {name!r} is neither drawn nor mirrored — the game "
                "would ask for a facing nobody makes")

    cell = out.get("cell") or []
    try:
        w, h = int(cell[0]), int(cell[1])
    except (IndexError, TypeError, ValueError) as exc:
        raise ContractError("cell must be [width, height]") from exc
    if not (8 <= w <= 1024 and 8 <= h <= 1024):
        raise ContractError(f"cell {w}x{h} is outside 8..1024")
    out["cell"] = [w, h]

    layout = str(out.get("layout") or "strip")
    if layout not in ("strip", "grid_rows"):
        raise ContractError(f"layout must be strip or grid_rows, got {layout!r}")
    if layout == "strip" and len(out["rows"]) > 1:
        raise ContractError("a strip has one row; use layout grid_rows")
    out["layout"] = layout

    out["actions"] = _actions(out.get("actions") or {}, "actions")

    characters = out.get("characters") or {}
    if not isinstance(characters, dict):
        raise ContractError("characters must be an object")
    clean_chars: dict[str, dict] = {}
    for raw_name, raw_over in characters.items():
        name = str(raw_name).strip()
        if not name or not isinstance(raw_over, dict):
            raise ContractError(f"characters[{raw_name!r}] must be an object")
        over: dict[str, Any] = {}
        if "drawn" in raw_over:
            drawn = [str(d).strip().lower() for d in raw_over["drawn"] or []]
            for d in drawn:
                if d not in DIRECTIONS:
                    raise ContractError(
                        f"characters[{name}].drawn: {d!r} is not a direction")
            over["drawn"] = drawn
        if "actions" in raw_over:
            over["actions"] = _actions(raw_over["actions"] or {},
                                       f"characters[{name}].actions")
        clean_chars[name] = over
    out["characters"] = clean_chars
    return out


def _actions(raw: dict, field: str) -> dict:
    if not isinstance(raw, dict):
        raise ContractError(f"{field} must be an object")
    out: dict[str, dict] = {}
    for raw_name, raw_spec in raw.items():
        name = str(raw_name).strip().lower()
        if not name:
            raise ContractError(f"{field}: an action needs a name")
        spec = dict(raw_spec or {})
        entry: dict[str, Any] = {}
        if "frames" in spec:
            frames = int(spec["frames"])
            if not (1 <= frames <= 64):
                raise ContractError(f"{field}.{name}.frames outside 1..64")
            entry["frames"] = frames
        if "fps" in spec:
            fps = float(spec["fps"])
            if not (0.1 <= fps <= 240.0):
                raise ContractError(f"{field}.{name}.fps outside 0.1..240")
            entry["fps"] = fps
        if "loop" in spec and spec["loop"] is not None:
            entry["loop"] = bool(spec["loop"])
        if "drawn" in spec:
            drawn = [str(d).strip().lower() for d in spec["drawn"] or []]
            for d in drawn:
                if d not in DIRECTIONS:
                    raise ContractError(f"{field}.{name}.drawn: {d!r} is not a direction")
            entry["drawn"] = drawn
        out[name] = entry
    return out


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
def load(root: str | os.PathLike[str]) -> dict:
    """The project's contract, normalised. Defaults when none was ever set."""
    doc = _ws.get(root, SEAT, DOC_KEY, {}) or {}
    payload = {k: v for k, v in doc.items() if k in DEFAULTS}
    if not payload:
        return normalise({})
    return normalise(payload)


def save(root: str | os.PathLike[str], data: dict) -> dict:
    """Normalise and store. Returns what was written."""
    clean = normalise(data)
    doc = dict(clean)
    _ws.set(root, SEAT, DOC_KEY, doc)
    return clean


def apply_preset(root: str | os.PathLike[str], preset: str,
                 patch: Optional[dict] = None) -> dict:
    """Start from a preset, apply an optional patch, store the result.

    A preset REPLACES the shape fields wholesale — mixing half of four_corner
    with half of eight_dir is how contradictions are born, so the merge order
    is defaults <- preset <- patch, never <- previous doc.
    """
    if preset not in PRESETS:
        raise ContractError(f"no preset {preset!r} — have: {sorted(PRESETS)}")
    merged = dict(DEFAULTS)
    merged.update(PRESETS[preset])
    merged["preset"] = preset
    for key, value in (patch or {}).items():
        if key not in DEFAULTS:
            raise ContractError(f"unknown contract field {key!r}")
        merged[key] = value
    return save(root, merged)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def contract_for(root: str | os.PathLike[str], character: str = "",
                 action: str = "") -> dict:
    """The effective contract for one piece of work, fully merged.

    Returns the project contract with `drawn` (and rows, which follow drawn
    order) and the action spec resolved through the override ladder. `rows`
    for an overridden `drawn` keeps the drawn order — the override IS the row
    plan, that being exactly what downsizing's SPRITE_ROWS encodes.
    """
    base = load(root)
    out = dict(base)
    char = (base.get("characters") or {}).get(str(character or "").strip(), {})
    act_name = str(action or "").strip().lower()

    drawn = char.get("drawn") or base["drawn"]
    char_act = (char.get("actions") or {}).get(act_name, {})
    base_act = (base.get("actions") or {}).get(act_name, {})
    if char_act.get("drawn"):
        drawn = char_act["drawn"]
    elif base_act.get("drawn"):
        drawn = base_act["drawn"]
    out["drawn"] = list(drawn)
    # Rows follow the RESOLVED drawn set under every layout, not only
    # grid_rows: a strip-layout override to a station facing ("working is
    # drawn north") still needs its start frame sliced from a north row —
    # keeping the project's default rows here pointed the slicer at pixels
    # of the wrong facing.
    out["rows"] = list(drawn)

    spec = {}
    spec.update(base_act)
    spec.update(char_act)
    spec.pop("drawn", None)
    out["action"] = {"name": act_name, **spec} if act_name else {}

    # Mirrors for facings whose drawn source changed: recompute from H_MIRROR
    # against the resolved drawn set so the pair stays a true flip. A facing
    # left with neither pixels nor a flip source is REPORTED, not raised — an
    # override that narrows `drawn` for one action may deliberately drop a
    # facing, and the caller decides whether that is a plan or a hole.
    mirror = {}
    unplayable = []
    for name in out["directions"]:
        if name in out["drawn"]:
            continue
        source = H_MIRROR.get(name)
        if source in out["drawn"]:
            mirror[name] = source
        else:
            unplayable.append(name)
    out["mirror"] = mirror
    if unplayable:
        out["unplayable"] = unplayable
    return out


def view_clause(view: str) -> str:
    """The projection directive for a view, '' for an unknown one."""
    return VIEW_CLAUSES.get(str(view or "").strip().lower(), "")
