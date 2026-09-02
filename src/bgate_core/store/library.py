"""The asset library as it actually exists on disk — families, sheets, and use.

The Assets workspace was a flat wall of artifact revisions: one tile per
generated image, in creation order, with no idea whether the thing was a whole
sprite sheet or one pose out of twelve, and no idea whether the game loads it.
That is three separate lies of omission on one screen.

  * A sheet is not an image. ``pm_paladin_walk.png`` is twelve frames of a walk
    cycle; showing it beside ``pm_paladin_idle.png`` as two peers of equal
    weight hides that they are one character's animation set. So the unit here
    is the FAMILY — every file in a directory that shares a name prefix — and
    the tile shows the sheets, not a crop of one.
  * "Approved" is not "shipping". An artifact can be approved, on disk, and
    referenced by nothing; the engine will never load it. Usage is derived from
    the same screen map Atlas uses, so a family says which screens reach it and
    which of its files reach nothing.
  * Rigged is not a guess. A family reports how many of its sheets carry a rig
    sidecar, because an unlabelled sheet is one the gear pipeline has to guess
    about.

Everything here is read-only and derived. No manifest, no new database table,
nothing to keep in sync — the same reason screenmap.py exists in this shape.

FAMILY DETECTION. Within one directory, stems are grouped by the longest
underscore-prefix that at least two of them share:

    pm_paladin_idle, pm_paladin_walk, pm_paladin_ko   ->  pm_paladin
    prop_copier_ne, prop_copier_se                    ->  prop_copier
    prop_dead_plant                                   ->  prop_dead_plant

Directory-scoped on purpose: ``pm_paladin_idle.png`` under characters/ and
under items/main_hand/animations/ are a body sheet and a gear layer, not two
revisions of one thing, and merging them would be worse than not grouping.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from ..art import rigmap, screenmap

IMAGE_SUFFIXES = frozenset({".png", ".webp", ".jpg", ".jpeg", ".svg"})
AUDIO_SUFFIXES = frozenset({".ogg", ".wav", ".mp3"})
RESOURCE_SUFFIXES = frozenset({".tres"})
COLLECTED = IMAGE_SUFFIXES | AUDIO_SUFFIXES | RESOURCE_SUFFIXES

SKIP_DIRS = frozenset({".git", ".godot", ".bgate", ".bgate_out", ".asset_work",
                       "__pycache__", "node_modules", "export", "build",
                       ".pytest_cache", ".qa_deps", ".qa_run_project"})

# A project with tens of thousands of scratch renders must not turn one panel
# into a minute of stat() calls. Stated in the payload when it bites.
SCAN_CAP = 8000


def _skip(path: Path) -> bool:
    return bool(SKIP_DIRS & set(path.parts))


def _kind(suffix: str) -> str:
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    return "resource"


# ---------------------------------------------------------------------------
# Family detection
# ---------------------------------------------------------------------------
def _prefixes(stem: str) -> list[str]:
    """Every underscore-prefix of a stem, longest first, excluding the bare stem."""
    parts = stem.split("_")
    return ["_".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)]


def group_stems(stems: Iterable[str]) -> dict[str, str]:
    """stem -> family label. Three rules, in order, each fixing a real misread.

    1. LONGEST SHARED PREFIX. A stem joins the longest underscore-prefix that
       at least one other stem also has. This is what separates ``prop_copier``
       from ``prop_conference_table`` instead of piling both under ``prop``.

    2. A ONE-WORD PREFIX IS A CATEGORY, NOT A NAME. ``prop_dead_plant``,
       ``prop_trash_can`` and ``prop_water_cooler`` share only ``prop`` — the
       directory's subject, not an asset's identity. When a family's label is a
       single token and no member's variant is a single token either, it is a
       bucket rather than a thing, and its members go back to their own names.
       ``hero_idle`` + ``hero_walk`` stay as ``hero``: those variants ARE one
       token, which is what a family's members look like.

    3. ABSORB SUB-FAMILIES INTO THE FAMILY THEY EXTEND. Rule 1 splits a subject
       whenever some of its files agree on a longer prefix than the rest —
       a weapon's two dual-wield frames, or the loose source frames a sheet was
       packed from. Any family whose label extends another family's label, in
       the same directory, is a facet of that one and merges back.

       THIS IS THE RULE THAT DECIDES WHETHER USAGE READS TRUE. Usage is a
       property of the subject, not of the file: ``bolt_impact_sheet.png`` ships
       and the four ``bolt_impact_default_N.png`` frames it was packed from do
       not, and while those were two families the library showed one green row
       and one grey row for one effect. Measured on a real project, 293 of 651
       files reported dead sat in a directory beside a referenced sibling
       sharing their prefix — 45% of the dead list was this split.

       It runs LAST: run before rule 2 it pulled real two-facing families into
       the bucket it was about to shred. Longest label first, so a three-deep
       chain (``a_b_c`` -> ``a_b`` -> ``a``) collapses in one pass.
    """
    stems = list(stems)
    counts: dict[str, int] = {}
    for stem in stems:
        for prefix in set(_prefixes(stem)):
            counts[prefix] = counts.get(prefix, 0) + 1

    out: dict[str, str] = {
        stem: next((p for p in _prefixes(stem) if counts.get(p, 0) >= 2), stem)
        for stem in stems}

    def _members() -> dict[str, list[str]]:
        by: dict[str, list[str]] = {}
        for stem, label in out.items():
            by.setdefault(label, []).append(stem)
        return by

    # 2 — dissolve buckets FIRST. Doing this after the absorb pass let a real
    # two-facing family (prop_conference_table) get pulled into the ``prop``
    # bucket and then shredded with it.
    for label, stems_in in _members().items():
        if "_" in label:
            continue
        if any("_" not in stem[len(label):].strip("_") for stem in stems_in):
            continue
        for stem in stems_in:
            out[stem] = stem

    # 3 — absorb sub-families into the family they extend
    members = _members()
    for label in sorted(members, key=len, reverse=True):
        if label not in members:
            continue
        # The LONGEST host, so a_b_c joins a_b rather than jumping to a and
        # stranding a_b as a sibling of its own child.
        host = max((h for h in members
                    if h != label and label.startswith(h + "_")),
                   key=len, default=None)
        if host:
            for stem in members[label]:
                out[stem] = host
            members[host] += members.pop(label)
    return out


def is_working(res: Optional[str]) -> bool:
    """Is this a working file rather than something the game can ship?

    Two things share a disk and are not the same kind of object: the engine's
    ``res://assets/**``, which the game can load, and everything else — the art
    seat's scratch renders, `tmp/`, test fixtures, the intermediate passes a
    tile went through on its way to being a tile.

    Measured on a real project, 453 of 834 families were the second kind. They
    are all, correctly, referenced by nothing, so the library was 54% grey rows
    that were never going to turn green and could not be acted on. That is not
    a usage problem to fix, it is a different drawer.

    Outside the engine project entirely (``res`` is None) or inside it but not
    under ``assets/``: working. This is deliberately a PATH test — asking usage
    would put every not-yet-wired asset in the same bucket as a scratch render,
    and those two need opposite responses from a person.
    """
    return not (res or "").startswith("res://assets/")


def _category(rel: str) -> str:
    """The bucket a family belongs to — the segment under ``assets/``."""
    parts = Path(rel).parts
    if "assets" in parts:
        i = parts.index("assets")
        if i + 1 < len(parts) - 1:
            return parts[i + 1]
        return "assets"
    return parts[0] if len(parts) > 1 else "root"


# ---------------------------------------------------------------------------
# Usage, from the derived screen map
# ---------------------------------------------------------------------------
def usage_index(smap: dict) -> dict[str, list[str]]:
    """res:// id -> the screen LABELS that reach it, directly or through a .tres.

    A sheet referenced only by a SpriteFrames resource is still shipping: the
    screen loads the .tres, the .tres loads the sheet. Following that hop is
    the difference between "used by 3 screens" and a wall of false orphans.
    """
    if not smap or smap.get("error"):
        return {}
    nodes = smap.get("nodes") or {}
    edges = smap.get("edges") or []
    screen_ids = {s["id"] for s in smap.get("screens") or []}

    # A SCRIPT NOBODY REACHES IS STILL A ROOT. Screens are not the only things
    # that hold an asset: a helper a scene attaches, a class_name registry, an
    # autoload — none of them is a screen, and a file one of them preloads is
    # shipping. Seeding only from screens made every such asset an orphan, and
    # the orphan list is what a human deletes from.
    #
    # Only UNREACHED scripts, because a script a screen already reaches is
    # covered by the walk below and seeding it again would attribute its assets
    # to the script instead of to the screen that actually shows them.
    reached = {e["to"] for e in edges}
    roots = set(screen_ids) | {
        # Every node the map calls a screen, not only the ones in the screens
        # LIST — project.godot is a root that is not a .tscn, and it holds the
        # icon, the audio bus layout and the autoloads.
        nid for nid, node in nodes.items() if node.get("kind") == "screen"} | {
        nid for nid, node in nodes.items()
        if node.get("kind") == "script" and nid not in reached} | {
        # A MANIFEST NOBODY REACHES IS ALSO A ROOT, for the same reason. An
        # asset manifest's whole job is to say which art belongs to what, and
        # the ones that matter most are precisely the ones no .gd loads — the
        # index an art tool writes and a human reads. Without this the screen
        # map grew the edges (ui_manifest.json -> 66 HUD files) and the library
        # still showed every one of them dead, because nothing propagated from
        # a node that no screen could reach.
        #
        # The family then reports "used by ui_manifest.json" rather than by a
        # screen, which is the true and useful answer: it says the art is
        # SPOKEN FOR, and it does not claim a scene draws it.
        nid for nid, node in nodes.items()
        if nid.endswith(".json") and nid not in reached}

    direct: dict[str, set[str]] = {}
    children: dict[str, list[str]] = {}
    for e in edges:
        if e["from"] in roots:
            direct.setdefault(e["to"], set()).add(e["from"])
        else:
            children.setdefault(e["from"], []).append(e["to"])

    # One breadth-first pass per reached node, following .tres/derived chains.
    reach: dict[str, set[str]] = {k: set(v) for k, v in direct.items()}
    frontier = list(reach.items())
    seen_pairs: set[tuple[str, str]] = set()
    while frontier:
        node, screens = frontier.pop()
        for child in children.get(node, ()):
            new = {s for s in screens if (child, s) not in seen_pairs}
            if not new:
                continue
            for s in new:
                seen_pairs.add((child, s))
            reach.setdefault(child, set()).update(new)
            frontier.append((child, reach[child]))

    label = lambda sid: (nodes.get(sid) or {}).get("label") or sid
    return {nid: sorted({label(s) for s in screens})
            for nid, screens in reach.items()}


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------
def _res_root(root: Path) -> Optional[Path]:
    for cand in (root, root / "game"):
        if (cand / "project.godot").is_file():
            return cand
    hits = [p.parent for p in root.glob("*/project.godot")]
    return hits[0] if hits else None


def _dimensions(path: Path) -> tuple[Optional[int], Optional[int]]:
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        return None, None
    try:
        from PIL import Image
        with Image.open(path) as im:      # header only — no decode
            return im.size
    except Exception:
        return None, None


def scan(root: str | os.PathLike[str], *, smap: Optional[dict] = None) -> dict:
    """Every asset family in the project, with sheets, rig state, and usage."""
    root = Path(root).resolve()
    if smap is None:
        smap = screenmap.scan_cached(root)
    use = usage_index(smap)
    gd = _res_root(root)

    def res_of(path: Path) -> Optional[str]:
        if gd is None:
            return None
        try:
            return f"res://{path.relative_to(gd).as_posix()}"
        except ValueError:
            return None

    # --- collect, bucketed by directory --------------------------------------
    by_dir: dict[str, list[Path]] = {}
    scanned = 0
    truncated = False
    for path in root.rglob("*"):
        if scanned >= SCAN_CAP:
            truncated = True
            break
        if path.suffix.lower() not in COLLECTED or not path.is_file():
            continue
        if _skip(path) or path.name.endswith(".import"):
            continue
        scanned += 1
        by_dir.setdefault(path.parent.relative_to(root).as_posix(), []).append(path)

    families: dict[str, dict] = {}
    for dirrel, paths in by_dir.items():
        labels = group_stems(p.stem for p in paths)
        for path in paths:
            label = labels[path.stem]
            key = f"{dirrel}::{label}"
            rel = path.relative_to(root).as_posix()
            res = res_of(path)
            fam = families.setdefault(key, {
                "key": key, "label": label, "dir": dirrel,
                "category": _category(rel),
                "working": is_working(res),
                "members": [], "used_by": [], "kinds": set(),
            })
            width, height = _dimensions(path)
            # Audio carries its own two facts: how long it is, and whether the
            # engine will loop it. The second one is invisible in every other
            # view — it lives in a .import sidecar — and a music track that
            # silently plays once is exactly what a library is for surfacing.
            sound = None
            if path.suffix.lower() in AUDIO_SUFFIXES:
                try:
                    from ..audio import audiolab
                    probe = audiolab.probe(path)
                    loop = audiolab.loop_state(path, info=probe)
                    sound = {"seconds": probe.get("seconds"),
                             "sample_rate": probe.get("sample_rate"),
                             "channels": probe.get("channels"),
                             "loops": bool(loop.get("enabled")),
                             "loop_supported": bool(loop.get("supported"))}
                except Exception:
                    sound = None
            rig = None
            if path.suffix.lower() in (".png", ".webp"):
                side = rigmap.sidecar_path(path)
                if side.is_file():
                    try:
                        data = rigmap.load(path)
                        grid = data.get("grid") or {}
                        rig = {
                            "slots": rigmap.slots_used(data),
                            "animations": [a["name"] for a in data["animations"]],
                            "frames": (int(grid.get("cols", 0))
                                       * int(grid.get("rows", 0))) or None,
                        }
                    except rigmap.RigError:
                        rig = {"slots": [], "animations": [], "frames": None,
                               "error": "sidecar unreadable"}
            screens = use.get(res or "", [])
            stat = path.stat()
            fam["kinds"].add(_kind(path.suffix.lower()))
            fam["members"].append({
                "rel": rel,
                "name": path.name,
                # What distinguishes this member inside its family: the action,
                # the facing, the variant. Empty when the family is one file.
                "variant": path.stem[len(label):].strip("_") or path.stem,
                "kind": _kind(path.suffix.lower()),
                "width": width, "height": height,
                "bytes": stat.st_size, "mtime": int(stat.st_mtime),
                "res_path": res,
                "editable": path.suffix.lower() in (".png", ".webp"),
                "audio_editable": path.suffix.lower() in AUDIO_SUFFIXES,
                "sound": sound,
                "rig": rig,
                "used_by": screens,
                "in_use": bool(screens),
            })

    out = []
    for fam in families.values():
        members = sorted(fam["members"], key=lambda m: m["name"])
        used_by = sorted({s for m in members for s in m["used_by"]})
        images = [m for m in members if m["kind"] == "image"]
        # The tile's picture: the widest image, because on a sheet-per-action
        # family that is a whole sheet, and on a one-off it is the asset itself.
        cover = max(images, key=lambda m: (m["width"] or 0) * (m["height"] or 0),
                    default=None)
        out.append({
            "key": fam["key"], "label": fam["label"], "dir": fam["dir"],
            "category": fam["category"],
            "working": fam["working"],
            "kinds": sorted(fam["kinds"]),
            "members": members,
            "count": len(members),
            "cover": cover["rel"] if cover else None,
            "cover_size": [cover["width"], cover["height"]] if cover else None,
            "sheets": [m["rel"] for m in images],
            "used_by": used_by,
            "in_use": bool(used_by),
            "unused": [m["rel"] for m in members if not m["used_by"]],
            "rigged": sum(1 for m in members if m["rig"]),
            "seconds": round(sum(m["sound"]["seconds"] for m in members
                                 if m.get("sound") and m["sound"].get("seconds")), 2)
            or None,
            "loops": sum(1 for m in members
                         if m.get("sound") and m["sound"]["loops"]),
            "bytes": sum(m["bytes"] for m in members),
            "mtime": max((m["mtime"] for m in members), default=0),
        })
    out.sort(key=lambda f: (f["category"].lower(), f["label"].lower()))

    return {
        "families": out,
        "truncated": truncated,
        "godot_dir": gd.relative_to(root).as_posix() if gd else None,
        "map_error": smap.get("error") if smap else None,
        "stats": {
            "families": len(out),
            "files": sum(f["count"] for f in out),
            # Shipping-only, because that is the number a person is asking about
            # when they ask how much of the library is wired up. Counting
            # scratch renders in the denominator made it permanently terrible
            # and told nobody anything.
            "in_use": sum(1 for f in out if f["in_use"] and not f["working"]),
            "unused": sum(1 for f in out
                          if not f["in_use"] and not f["working"]),
            "working": sum(1 for f in out if f["working"]),
            "rigged": sum(1 for f in out if f["rigged"]),
            "categories": sorted({f["category"] for f in out
                                  if not f["working"]}),
            "working_categories": sorted({f["category"] for f in out
                                          if f["working"]}),
        },
    }
