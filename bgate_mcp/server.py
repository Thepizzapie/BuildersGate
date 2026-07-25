"""Builders Gate MCP server (FastMCP, stdio).

Every tool resolves the project from BGATE_ROOT or the cwd by walking up for a
.bgate dir, so an agent working inside a game repo never passes paths around.

Tool errors return a dict with an "error" key rather than raising: a raised
exception inside a tool call reads to the model as a broken server, while an
error payload reads as a fact it can act on.
"""
from __future__ import annotations

import json as _json
import os
from pathlib import Path as _Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from bgate_adapters import blender as _blender
from bgate_adapters import godot as _godot
from bgate_adapters import recorder as _recorder
from bgate_adapters import sprites as _sprites
from bgate_core import assets as _assets
from bgate_core import artifacts as _artifacts
from bgate_core import refs as _refs
from bgate_core import seats as _seats
from bgate_core import bible as _bible
from bgate_core import playtest as _playtest
from bgate_core import scaffold as _scaffold
from bgate_core import canon as _canon
from bgate_core import db as _db
from bgate_core import lore as _lore
from bgate_core import iterations as _iterations
from bgate_core import items as _items
from bgate_core import project as _project
from bgate_core import search as _search

mcp = FastMCP("builders-gate")


# Runtime project override (set by project_select). Env vars are frozen at
# server spawn, so a session whose cwd is a DIFFERENT repo could never reach
# its project — this is the switch that fixes that.
_ACTIVE_ROOT: Optional[str] = None


def _root() -> str:
    """The active project root: project_select > BGATE_ROOT > walk up from cwd.
    Also loads the project's .env (once) so secrets live with the project."""
    override = _ACTIVE_ROOT or os.environ.get("BGATE_ROOT")
    root = override if override else str(_project.require_root())
    try:
        from bgate_core import envfile
        envfile.load_project_env(root)
    except Exception:
        pass
    return root


def _fail(exc: Exception) -> dict:
    return {"error": f"{type(exc).__name__}: {exc}"}


def _seat() -> str:
    """The session's adopted seat, if any. Each Claude session spawns its own
    stdio server process, so a per-session env var is a per-session identity."""
    return os.environ.get("BGATE_SEAT", "").strip()


def _lock_identity(requested_seat: str) -> tuple[str, str]:
    """Bind asset ownership to the dispatched session when one is present."""
    adopted = _seat()
    if adopted and requested_seat != adopted:
        raise PermissionError(
            f"session adopted seat {adopted!r}; it cannot claim seat {requested_seat!r}")
    return requested_seat, os.environ.get("BGATE_LOCK_OWNER", "").strip()


def _log(kind: str, summary: str, ref: str = "") -> None:
    """Ledger entry against the active project. Never lets telemetry fail work."""
    try:
        from bgate_core import activity
        activity.log(_root(), kind, summary, seat=_seat(), ref=ref)
    except Exception:
        pass


def _archive_preview(src: str, label: str) -> Optional[str]:
    """Copy a render into .bgate/previews/ so the dashboard keeps a history.

    Renders land on a fixed path (render.png) and each run overwrites the last —
    without archiving, the dashboard could only ever show the newest one.
    """
    try:
        import shutil
        import time

        root = _Path(_root())
        previews = root / ".bgate" / "previews"
        previews.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:40]
        dest = previews / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe or 'render'}.png"
        shutil.copy2(src, dest)
        return str(dest)
    except Exception:
        return None


def _register_artifact(logical_name: str, path: str, *, producer: str,
                       model: str = "", prompt: str = "",
                       refs: Optional[list[str]] = None,
                       metadata: Optional[dict] = None) -> Optional[dict]:
    """Best-effort provenance; failure never discards a successfully made file."""
    try:
        work_item = os.environ.get("BGATE_WORK_ITEM", "").strip()
        return _artifacts.register(
            _root(), logical_name, path, producer=producer, model=model,
            prompt=prompt, refs=refs, metadata=metadata,
            work_item_id=int(work_item) if work_item.isdigit() else None)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------
@mcp.tool()
def project_init(name: str, pitch: str = "", engine: str = "godot",
                 dimension: str = "2d", root: Optional[str] = None) -> dict:
    """Create a Builders Gate project (.bgate/game.db) at root (default: cwd).

    engine: godot | none. dimension: 2d | 3d | 2d+3d. Safe to re-run.
    """
    try:
        target = root or os.environ.get("BGATE_ROOT") or os.getcwd()
        return _project.init(target, name, pitch=pitch, engine=engine, dimension=dimension)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def project_select(project: str = "") -> dict:
    """Point this session at a Builders Gate project — by registered name or
    absolute path. Fixes the "no .bgate project found" error when the session's
    cwd is a different repo. Empty arg: report the active root + known projects.
    """
    global _ACTIVE_ROOT
    try:
        known = _project.known_projects()
        if not project:
            active = None
            try:
                active = _root()
            except Exception:
                pass
            return {"active": active, "known": known}
        root = known.get(project, project)  # name wins, else treat as a path
        if not (_Path(root) / _db.DB_DIRNAME / _db.DB_FILENAME).exists():
            raise LookupError(
                f"{project!r} is not a known project name or a project root. "
                f"Known: {known}")
        _ACTIVE_ROOT = str(_Path(root).resolve())
        _project.register(_ACTIVE_ROOT)
        return {"active": _ACTIVE_ROOT, "project": _project.get(_ACTIVE_ROOT)}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def project_status() -> dict:
    """The project's identity plus a count of what's in the bible and lore."""
    try:
        root = _root()
        conn = _db.connect(root)
        counts = {
            "bible_sections": conn.execute(
                "SELECT count(*) FROM bible_section").fetchone()[0],
            "entities": conn.execute("SELECT count(*) FROM lore_entity").fetchone()[0],
            "canon_entities": conn.execute(
                "SELECT count(*) FROM lore_entity WHERE status = 'canon'").fetchone()[0],
            "facts": conn.execute("SELECT count(*) FROM canon_fact").fetchone()[0],
            "links": conn.execute("SELECT count(*) FROM lore_link").fetchone()[0],
        }
        return {"project": _project.get(root), "root": root, "counts": counts}
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Design bible
# ---------------------------------------------------------------------------
@mcp.tool()
def bible_add(kind: str, title: str, body: str = "", rank: int = 0) -> dict:
    """Add a bible section.

    kind: pillar | loop | scope_tier | cut_line | constraint | reference.
    rank orders within a kind; for scope_tier, LOWER rank = higher priority, and
    anything ranked at or below the cut_line's rank is explicitly not being built.
    """
    try:
        return _bible.add(_root(), kind, title, body=body, rank=rank)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def bible_update(section_id: int, title: Optional[str] = None,
                 body: Optional[str] = None, rank: Optional[int] = None) -> dict:
    """Update a bible section in place. Omitted fields keep their current value."""
    try:
        return _bible.update(_root(), section_id, title=title, body=body, rank=rank)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def bible_read(kind: Optional[str] = None) -> dict:
    """Read the bible. No kind: the grouped overview with the scope cut applied."""
    try:
        root = _root()
        if kind:
            return {"kind": kind, "sections": _bible.list_sections(root, kind)}
        return _bible.overview(root)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def scope_check(rank: int) -> dict:
    """Is work at this rank above the cut line? Call before building anything."""
    try:
        root = _root()
        line = _bible.cut_line(root)
        return {
            "rank": rank,
            "in_scope": _bible.in_scope(root, rank),
            "cut_line": line,
            "note": "no cut line set — scope call not yet made" if line is None else "",
        }
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Lore
# ---------------------------------------------------------------------------
@mcp.tool()
def lore_add(kind: str, name: str, summary: str = "", body: str = "",
             status: str = "draft") -> dict:
    """Create a lore entity.

    kind: faction | character | place | event | item | concept | species.
    status: draft | canon | retired. Names are unique — update, don't duplicate.
    """
    try:
        return _lore.add_entity(_root(), kind, name, summary=summary, body=body,
                                status=status)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def lore_update(ref: str, summary: Optional[str] = None, body: Optional[str] = None,
                status: Optional[str] = None) -> dict:
    """Update an entity by slug or name. Promote draft to canon with status='canon'."""
    try:
        return _lore.update_entity(_root(), ref, summary=summary, body=body, status=status)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def lore_brief(ref: str) -> dict:
    """Everything about one entity — record, facts, and edges. Read before writing it."""
    try:
        return _lore.brief(_root(), ref)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def lore_list(kind: Optional[str] = None, status: Optional[str] = None) -> dict:
    """List entities, optionally filtered by kind and/or status."""
    try:
        return {"entities": _lore.list_entities(_root(), kind=kind, status=status)}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def lore_link(src: str, rel: str, dst: str, note: str = "") -> dict:
    """Connect two entities. rel is free-form: 'rules', 'allied_with', 'born_in'."""
    try:
        return _lore.link(_root(), src, rel, dst, note=note)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def lore_fact(ref: str, statement: str, source: str = "", locked: bool = False) -> dict:
    """Assert ONE atomic fact about an entity — canon_check compares against these.

    Keep it to a single checkable claim ("The siege lasted seven years"), not a
    paragraph. locked=True marks it immovable: conflicts against it are hard.
    """
    try:
        return _lore.add_fact(_root(), ref, statement, source=source, locked=locked)
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Canon + recall
# ---------------------------------------------------------------------------
@mcp.tool()
def canon_check(text: str, entities: Optional[list[str]] = None) -> dict:
    """Check text against canon BEFORE it lands. Run on every narrative write.

    Returns verdict (ok | review | conflict), the entities it touches, the canon
    facts in play, and flags. Deterministic lexical checks: catches retired
    entities, invented proper nouns, polarity flips, and number disagreements.
    It does not judge tone or theme — 'ok' means nothing mechanical is wrong.
    """
    try:
        return _canon.check(_root(), text, entities=entities)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def recall(query: str, limit: int = 10, kind: Optional[str] = None) -> dict:
    """Search the bible and lore. Call this BEFORE inventing anything."""
    try:
        conn = _db.connect(_root())
        return {"query": query, "results": _search.find(conn, query, limit=limit, kind=kind)}
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Blender
# ---------------------------------------------------------------------------
@mcp.tool()
def blender_status() -> dict:
    """Is Blender available to this machine, and which version? Check before modeling."""
    try:
        probe = _blender.available()
        return {**probe, **(_blender.version() if probe["available"] else {})}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def blender_run(script: str, blend_file: Optional[str] = None, render: bool = False,
                engine: str = "BLENDER_WORKBENCH", timeout: int = 180,
                label: str = "") -> dict:
    """Run a bpy script in headless Blender and get the scene back as facts.

    `bpy` is already imported. Returns per-object tri/vert counts (evaluated, so
    modifiers count), UV warnings, materials, your print() output, and — with
    render=True — a PNG of the active camera view (archived to the project's
    preview gallery; give a `label` so humans can tell renders apart).

    A broken script is a normal result with ok=False plus the traceback, so read
    the result and iterate rather than assuming it worked. engine:
    BLENDER_WORKBENCH (fast preview) | BLENDER_EEVEE_NEXT | CYCLES.
    """
    try:
        out_dir = str(_Path(_root()) / ".bgate_out")
    except Exception:
        out_dir = None  # modeling before project_init is allowed
    try:
        result = _blender.run_script(script, blend_file=blend_file, render=render,
                                     out_dir=out_dir, engine=engine, timeout=timeout)
        rendered = result.get("render", {}) if isinstance(result.get("render"), dict) else {}
        if rendered.get("rendered") and rendered.get("path"):
            archived = _archive_preview(rendered["path"], label or "render")
            if archived:
                result["render"]["preview"] = archived
            artifact = _register_artifact(
                label or "blender-render", rendered["path"],
                producer="blender_run",
                metadata={"engine": engine, "preview": archived or "",
                          "scene": result.get("scene", {})})
            if artifact:
                result["render"]["artifact"] = artifact
                _log("render", f"rendered {label or 'a preview'} "
                               f"({result['scene']['totals']['tris']} tris)",
                     ref=archived)
        elif result.get("ok"):
            _log("blender", f"blender run: {label}" if label else
                 f"blender run ({result.get('scene', {}).get('totals', {}).get('tris', '?')} tris)")
        return result
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def blender_warmup(engine: str = "BLENDER_EEVEE_NEXT") -> dict:
    """Pay the GPU cold-start cost up front. Run once per machine boot.

    A GPU engine's first render after a cold boot can take MINUTES of shader
    warmup (then ~1-2s forever after). Call this at pipeline start so no agent's
    real render is the one that stalls. Not needed for BLENDER_WORKBENCH.
    """
    try:
        out_dir = str(_Path(_root()) / ".bgate_out")
    except Exception:
        out_dir = None
    try:
        return _blender.warmup(engine, out_dir=out_dir)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def blender_scene_stats(blend_file: str) -> dict:
    """Report an existing .blend without modifying it — objects, tris, materials."""
    try:
        return _blender.scene_stats(blend_file)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def blender_export_gltf(out_path: str, blend_file: Optional[str] = None,
                        script: str = "pass", timeout: int = 240) -> dict:
    """Export a .blend (or a bpy-script-built scene) to .glb for Godot.

    Modifiers are APPLIED on export — Blender defaults that off, which silently
    ships the base mesh and makes an asset look right in Blender and wrong in the
    engine. Also returns game-readiness issues (no UVs, n-gons, unapplied scale)
    worth fixing before the asset reaches a level. Pair with godot_import_asset.
    """
    try:
        return _blender.export_gltf(out_path, blend_file=blend_file,
                                    script=script, timeout=timeout)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def blender_sprites(base_script: str, poses: list[dict], name: str = "sprite",
                    width: int = 128, height: int = 128,
                    engine: str = "BLENDER_EEVEE_NEXT", fps: float = 8.0,
                    res_dir: str = "assets/sprites", out_dir: Optional[str] = None,
                    timeout: int = 420) -> dict:
    """Render a Blender-built character as a transparent 2D sprite set.

    THE 2D art path: build the model once in base_script (bpy; lights included —
    camera optional, an auto-framed ORTHO one is added if missing), then each
    pose in poses=[{"name","script"}] tweaks the scene and renders one frame.
    Output: per-pose PNGs + <name>_sheet.png + <name>_frames.tres (a Godot
    SpriteFrames with one animation per pose) ready for an AnimatedSprite2D via
    godot_import_asset into res_dir. Rendered sprites cannot drift between
    poses the way hand-drawn ones do — same rig, camera, light every frame.

    A pose script that errors fails only that pose; check `failed` in the result.
    The sheet is archived to the preview gallery.
    """
    try:
        out = out_dir or str(_Path(_root()) / ".bgate_out" / "sprites")
    except Exception:
        out = out_dir or "sprites_out"
    try:
        result = _sprites.render_sprites(base_script, poses, out_dir=out,
                                         name=name, size=(width, height),
                                         engine=engine, fps=fps,
                                         res_dir=res_dir, timeout=timeout)
        if result.get("ok"):
            archived = _archive_preview(result["sheet"], f"sprites-{name}")
            if archived:
                result["preview"] = archived
            artifact = _register_artifact(
                name, result["sheet"], producer="blender_sprites",
                metadata={"poses": [p.get("name", "") for p in poses],
                          "frames": result.get("frames", {}),
                          "failed": result.get("failed", []),
                          "engine": engine, "preview": archived or ""})
            if artifact:
                result["artifact"] = artifact
            _log("sprites", f"rendered {len(result['frames'])} sprite frames "
                            f"for {name!r}" +
                            (f" ({len(result['failed'])} failed)" if result["failed"] else ""),
                 ref=result["sheet"])
        return result
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Painted art (gpt-image)
# ---------------------------------------------------------------------------
@mcp.tool()
def image_status() -> dict:
    """Is the painted-art leg (gpt-image) usable? Checks the key without exposing it."""
    try:
        _root()  # triggers .env load
        from bgate_adapters import imagegen
        return imagegen.available()
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def image_generate(prompt: str, filename: str, size: str = "1024x1024",
                   quality: str = "medium", transparent: bool = False) -> dict:
    """Generate PAINTED art via gpt-image — portraits, select-screen cards,
    title splashes, stage paint-overs. Costs real money per image (~$0.02-0.19).

    Division of labor: use blender_sprites for anything needing the SAME
    character across multiple frames (an image model can't hold a rig steady);
    use this for one-off illustrated pieces. transparent=True for art that
    composites over the game; false for full backdrops.

    filename is relative to the project's .bgate_out/art/ (e.g. "tommy_portrait.png").
    The result is archived to the preview gallery — LOOK at it before importing
    into the game with godot_import_asset.
    """
    try:
        root = _Path(_root())
        out = root / ".bgate_out" / "art" / filename
        from bgate_adapters import imagegen
        result = imagegen.generate(prompt, str(out), size=size, quality=quality,
                                   transparent=transparent)
        if result.get("ok"):
            archived = _archive_preview(result["path"], f"art-{_Path(filename).stem}")
            if archived:
                result["preview"] = archived
            artifact = _register_artifact(
                _Path(filename).stem, result["path"], producer="image_generate",
                model=result.get("model", ""), prompt=prompt,
                metadata={"size": size, "quality": quality,
                          "transparent": transparent,
                          "preview": archived or ""})
            if artifact:
                result["artifact"] = artifact
            _log("art", f"generated painted art {filename} ({size}, {quality})",
                 ref=archived or result["path"])
        return result
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def image_edit(prompt: str, ref_images: list[str], filename: str,
               size: str = "1024x1536", quality: str = "medium",
               transparent: bool = False) -> dict:
    """Generate an image CONDITIONED ON reference image(s) — the consistency
    primitive, exposed raw. Use it to regenerate a single sprite pose against a
    character's existing reference (~$0.04 at medium) instead of re-buying the
    whole set, or to derive variants that must stay on-model.

    ref_images: PINNED REFERENCE NAMES (see ref_list — preferred) or absolute
    paths. filename lands under the project's .bgate_out/art/. Result is
    archived to the gallery — LOOK at it. Note: transparent output requires
    gpt-image-1 (gpt-image-2 rejects it).
    """
    try:
        root = _Path(_root())
        out = root / ".bgate_out" / "art" / filename
        from bgate_adapters import imagegen
        resolved = [_refs.resolve(root, r) for r in ref_images]
        result = imagegen.edit(prompt, resolved, str(out), size=size,
                               quality=quality, transparent=transparent)
        if result.get("ok"):
            archived = _archive_preview(result["path"], f"edit-{_Path(filename).stem}")
            if archived:
                result["preview"] = archived
            artifact = _register_artifact(
                _Path(filename).stem, result["path"], producer="image_edit",
                model=result.get("model", ""), prompt=prompt, refs=ref_images,
                metadata={"resolved_refs": resolved, "size": size,
                          "quality": quality, "transparent": transparent,
                          "preview": archived or ""})
            if artifact:
                result["artifact"] = artifact
            _log("art", f"reference-edit {filename}", ref=archived or result["path"])
        return result
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# The item-art pipeline — item-as-object, class-templated, Codex-drivable.
# Variants are cheap and classes are expensive: one prompt template per class
# holds framing/light/scale/background invariant, a parameter grid mints the
# variants. See bgate_core/items.py for the taxonomy and the pure builders.
# ---------------------------------------------------------------------------
@mcp.tool()
def item_classes() -> dict:
    """The item-art taxonomy: the classes, their equip slot, and the variant
    axes. This IS the contract to drive item_generate / item_variants — read it
    before minting gear so names/slots line up with the equip/layer system."""
    return {
        "ok": True,
        "classes": {
            name: {"label": c["label"], "slot": c["slot"], "worn": c["worn"],
                   "subject": c["subject"]}
            for name, c in _items.ITEM_CLASSES.items()
        },
        "axes": {"material": "free text (e.g. iron, damascus steel, bone)",
                 "element": list(_items.ELEMENTS),
                 "tier": list(_items.TIERS)},
        "slots": list(_items.SLOTS),
    }


def _item_style_clause(root: _Path, character: str) -> str:
    """The cross-leg style rail: a character's stored visual profile -> the
    style clause appended to every item prompt, so worn gear reads as the same
    set as the body it hangs on. Same fallback chain image_sprites uses.
    Naming a character with no profile raises — silently minting unstyled gear
    would LOOK like a result."""
    if not character.strip():
        return ""
    for key in (character, f"{character}-character"):
        profile = _refs.profile_get(root, key)
        if profile:
            return profile.get("style", "")
    raise ValueError(
        f"no visual profile for {character!r} — set one with profile_set "
        "(or drop the character param to mint unstyled)")


def _index_item(root: _Path, man: dict) -> bool:
    """Upsert one manifest into .bgate_out/items/_index.json — the one-shot
    rollup the equip UI reads. Loose per-item manifests stay the source of
    truth; a missing/corrupt index is rebuilt from them, never trusted."""
    path = root / _items.INDEX_REL
    index: dict = {}
    try:
        loaded = _json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("items"), dict):
            index = loaded
    except Exception:
        pass
    if not index:  # first write or corrupt — rebuild from the loose manifests
        index = {"items": {}}
        for f in sorted(path.parent.glob("*.json")) if path.parent.is_dir() else []:
            if f.name == path.name:
                continue
            try:
                loose = _json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(loose, dict) and loose.get("name"):
                index["items"][loose["name"]] = loose
    _items.update_index(index, man)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(index, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False  # rollup is a cache; never a reason to lose a made item


def _mint_item(root: _Path, spec: dict, quality: str) -> dict:
    """Generate one item from a variant spec, then archive + track + manifest.

    A single spec (from items.plan_variants) carries its own prompt, so this is
    pure I/O: paint it transparent, register provenance, track the binary so the
    QA gate and dashboard see it, and drop the JSON bridge record the equip
    system reads. Returns the per-item result; failures are reported, not raised,
    so one bad variant never sinks a batch."""
    from bgate_adapters import imagegen
    rel = _items.rel_art_path(spec["item_class"], spec["name"])
    out = root / rel
    result = imagegen.generate(spec["prompt"], str(out), quality=quality,
                               transparent=True)
    if not result.get("ok"):
        return {"ok": False, "name": spec["name"], "error": result.get("error"),
                "prompt": spec["prompt"]}

    archived = _archive_preview(result["path"], f"item-{spec['name']}")
    _register_artifact(spec["name"], result["path"], producer="item_generate",
                       model=result.get("model", ""), prompt=spec["prompt"],
                       metadata={"item_class": spec["item_class"],
                                 "slot": spec["slot"], "params": spec["params"],
                                 "preview": archived or ""})
    try:
        _assets.track(root, out)
    except Exception:
        pass  # tracking is provenance, never a reason to lose a made file
    man = _items.manifest(spec, rel)
    man_path = root / _items.rel_manifest_path(spec["name"])
    man_path.parent.mkdir(parents=True, exist_ok=True)
    man_path.write_text(_json.dumps(man, indent=2), encoding="utf-8")
    indexed = _index_item(root, man)
    return {"ok": True, "name": spec["name"], "item_class": spec["item_class"],
            "slot": spec["slot"], "sprite": rel,
            "manifest": _items.rel_manifest_path(spec["name"]),
            "indexed": indexed,
            "preview": archived or result["path"]}


@mcp.tool()
def item_generate(item_class: str, name: str, descriptor: str,
                  material: str = "", element: str = "", tier: str = "",
                  quality: str = "medium", character: str = "",
                  force: bool = False) -> dict:
    """Mint ONE gear/item icon — transparent, class-templated, tracked.

    item_class is one of item_classes() (main_hand, off_hand, head, body, feet,
    consumable, throwable, ranged). descriptor names the item ("curved saber").
    material/element/tier are the variant axes. `character` names a pinned ref
    with a visual profile (profile_set) — its style is appended so worn gear
    reads as the same set as the fighter it hangs on. An already-minted item
    (manifest on disk) is skipped, not re-bought; force=true regenerates.
    Costs real money per image (~$0.02-0.19 at `quality`). For a batch, use
    item_variants. LOOK at the preview before importing into the game."""
    try:
        root = _Path(_root())
        style_clause = _item_style_clause(root, character)
        [spec] = _items.plan_variants(
            item_class, name, descriptor,
            materials=[material] if material else None,
            elements=[element] if element else None,
            tiers=[tier] if tier else None,
            style_clause=style_clause)
        if not force:
            _, skipped = _items.split_existing(
                [spec], lambda rel: (root / rel).is_file())
            if skipped:
                return {"ok": True, "name": spec["name"], "skipped": True,
                        "manifest": _items.rel_manifest_path(spec["name"]),
                        "estimated_cost_usd": 0.0,
                        "note": "already minted — manifest exists; pass "
                                "force=true to re-buy"}
        res = _mint_item(root, spec, quality)
        if res.get("ok"):
            res["estimated_cost_usd"] = _items.estimate_cost(1, quality)
            res["style_rail"] = bool(style_clause)
            _log("art", f"minted {item_class} item {spec['name']}",
                 ref=res["preview"])
        return res
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def item_variants(item_class: str, base_name: str, descriptor: str,
                  materials: Optional[list[str]] = None,
                  elements: Optional[list[str]] = None,
                  tiers: Optional[list[str]] = None,
                  quality: str = "medium", limit: int = 12,
                  character: str = "", force: bool = False) -> dict:
    """Mint a BATCH of variants of one class from a parameter grid — the
    cartesian product of the axes you pass, each a self-contained item.

    This is the "plethora of gear, easily" engine: pass materials=[...],
    tiers=[...], elements=[...] and get one on-set icon per combination.
    `character` names a pinned ref with a visual profile — its style is woven
    into every prompt so the whole set matches the fighter that wears it.
    Already-minted variants (manifest on disk) are skipped and reported, so a
    re-run finishes a batch instead of re-buying it; force=true re-buys.
    Every image costs money, so `limit` caps what a run may BUY (default 12) —
    the plan and its $ estimate are reported and refused if new images exceed
    the cap, so you confirm the spend before it happens. LOOK at the set
    before importing."""
    try:
        root = _Path(_root())
        style_clause = _item_style_clause(root, character)
        specs = _items.plan_variants(item_class, base_name, descriptor,
                                     materials=materials, elements=elements,
                                     tiers=tiers, style_clause=style_clause)
        to_mint, skipped = (specs, []) if force else _items.split_existing(
            specs, lambda rel: (root / rel).is_file())
        estimate = _items.estimate_cost(len(to_mint), quality)
        if len(to_mint) > max(1, limit):
            return {"ok": False, "planned": len(specs),
                    "to_buy": len(to_mint), "already_minted": len(skipped),
                    "limit": limit, "estimated_cost_usd": estimate,
                    "names": [s["name"] for s in to_mint],
                    "error": f"grid needs {len(to_mint)} new images "
                             f"(~${estimate:.2f} at {quality!r}, > limit "
                             f"{limit}); raise limit to confirm the spend or "
                             "narrow the axes"}
        results = [_mint_item(root, s, quality) for s in to_mint]
        made = [r for r in results if r.get("ok")]
        _log("art", f"minted {len(made)}/{len(to_mint)} {item_class} variants "
             f"of {base_name}"
             + (f" ({len(skipped)} already on disk)" if skipped else ""))
        return {"ok": all(r.get("ok") for r in results),
                "class": item_class, "count": len(made),
                "skipped": [s["name"] for s in skipped],
                "estimated_cost_usd": estimate,
                "style_rail": bool(style_clause),
                "items": results}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def item_to_spriteframes(sprite: str, name: str, res_dir: str = "assets/gear",
                         frame_size: Optional[list[int]] = None) -> dict:
    """Wrap a single item PNG into a 1-frame Godot SpriteFrames .tres so it drops
    straight into an equip slot — the bridge from the item pipeline to the
    equip/layer system (templates/2d gear_rig.gd).

    A static held weapon/shield with one frame is the honest v1 for worn gear: it
    shows in-hand and rides the fighter's facing, before the per-frame worn-gear
    rig exists. sprite is a repo-relative or absolute PNG path. Emits the .tres
    next to the sheet the equip layer will load from res://<res_dir>/."""
    try:
        root = _Path(_root())
        rel = _assets.normalize_path(root, sprite)
        src = root / rel
        if not src.exists():
            return {"ok": False, "error": f"no image at {rel}"}
        from PIL import Image as _Img
        with _Img.open(src) as im:
            size = tuple(frame_size) if frame_size else im.size
        slug = _items.slugify(name)
        out_dir = src.parent
        sheet_name = f"{slug}_sheet.png"
        # The single frame IS the sheet — copy under the sheet name the tres
        # expects, so the pair imports together like every other SpriteFrames.
        from shutil import copyfile
        copyfile(src, out_dir / sheet_name)
        tres = _sprites._sprite_frames_tres(  # noqa: SLF001 — shared emitter
            sheet_name, [("default", 1)], (int(size[0]), int(size[1])),
            1.0, res_dir)
        tres_rel = out_dir / f"{slug}_frames.tres"
        tres_rel.write_text(tres, encoding="utf-8")
        return {"ok": True, "tres": _assets.normalize_path(root, tres_rel),
                "sheet": _assets.normalize_path(root, out_dir / sheet_name),
                "animation": "default", "res_dir": res_dir}
    except Exception as exc:
        return _fail(exc)


# Frames the vision judge scores at/below this are flagged for regen.
_CONSISTENCY_FLOOR = 78
# Deterministic palette gates (opaque-pixel histograms, 4 bits/channel).
# Measured on the failed PM-Paladin batch: adjacent same-character frames
# intersect ~0.45; recolored frames vs their siblings crater to ~0.06; and
# ref-vs-frame runs low (~0.1-0.3) even for GOOD frames because the ref's
# rendering differs — so BATCH COHESION (each frame vs the batch median) is
# the primary gate, and vs-ref only trips on catastrophic recolors.
_PALETTE_COHESION_FLOOR = float(os.environ.get("BGATE_PALETTE_COHESION", "0.35"))
_PALETTE_REF_FLOOR = float(os.environ.get("BGATE_PALETTE_FLOOR", "0.10"))


def _palette_hist(path):
    from PIL import Image as _Img
    im = _Img.open(path).convert("RGBA")
    im.thumbnail((160, 160))
    h = [0.0] * 4096
    n = 0
    for r, g, b, a in im.getdata():
        if a > 96:
            h[(r >> 4) << 8 | (g >> 4) << 4 | (b >> 4)] += 1
            n += 1
    return [v / n for v in h] if n else h


def _hist_intersect(a, b) -> float:
    return sum(min(x, y) for x, y in zip(a, b))


def _palette_similarity(ref_path, frame_path) -> float:
    """Histogram intersection (0..1) of opaque-pixel colors."""
    return _hist_intersect(_palette_hist(ref_path), _palette_hist(frame_path))


def _vision_consistency(ref_path, frame_items, pass_floor=_CONSISTENCY_FLOOR):
    """Score generated frames against an approved reference for CHARACTER IDENTITY.

    Cheap pixel metrics (palette, silhouette) can't judge "same character" pose-
    invariantly, so this asks a vision model to score each frame 0-100 (identity
    only — pose/expression ignored). frame_items: list of (label, path). Returns
    {"ok": True, "frames": [{"label","score","reason","pass"}], "min", "flagged"}
    or {"ok": False, "error": ...} — callers must treat failure as non-blocking.
    """
    import base64 as _b64, io as _io, json as _json
    try:
        from PIL import Image as _Img
        from openai import OpenAI as _OpenAI

        def _url(p):
            im = _Img.open(p).convert("RGBA"); im.thumbnail((256, 256))
            bg = _Img.new("RGBA", im.size, (255, 255, 255, 255)); bg.alpha_composite(im)
            b = _io.BytesIO(); bg.convert("RGB").save(b, "PNG")
            return "data:image/png;base64," + _b64.b64encode(b.getvalue()).decode()

        labels = [lab for lab, _ in frame_items]
        content = [{"type": "text", "text":
            "The FIRST image is the APPROVED reference for a game character. The remaining "
            "images are generated frames of ONE animation of that character. Pose, action "
            "and expression WILL differ between frames — IGNORE those.\n"
            "Judge TWO things:\n"
            "(1) IDENTITY: score each frame 0-100 for being the SAME character as the "
            "reference (body proportions, art style, line weight, palette, defining "
            "features). <78 = noticeable drift.\n"
            "(2) FRAME-TO-FRAME CONSISTENCY: the frames must also look consistent WITH "
            "EACH OTHER — same build, proportions, weight, head size and style across the "
            "set. Mark outlier=true for any frame whose PROPORTIONS/BUILD/STYLE visibly "
            "differ from the majority of the other frames (e.g. suddenly buffer, rounder, "
            "bigger head, different line weight), even if it still resembles the reference.\n"
            "Respond ONLY as JSON {\"frames\":[{\"score\":0,\"outlier\":false,\"reason\":\"\"}...]} "
            "in the SAME order as the frames, one entry per frame."}]
        content.append({"type": "image_url", "image_url": {"url": _url(ref_path)}})
        for _, p in frame_items:
            content.append({"type": "image_url", "image_url": {"url": _url(p)}})

        cli = _OpenAI()
        r = cli.chat.completions.create(
            model=os.environ.get("BGATE_VISION_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"}, temperature=0)
        raw = _json.loads(r.choices[0].message.content).get("frames", [])
        # Deterministic palette gates — the vision judge kept passing
        # outfit/skin recolors. Primary: cohesion of each frame against the
        # BATCH MEDIAN histogram; secondary: catastrophic drift vs the ref.
        try:
            hists = [_palette_hist(p) for _, p in frame_items]
            med = [sorted(col)[len(col) // 2] for col in zip(*hists)]
            s = sum(med) or 1.0
            med = [v / s for v in med]
            ref_hist = _palette_hist(ref_path)
            cohesion = [round(_hist_intersect(h, med), 3) for h in hists]
            vs_ref = [round(_hist_intersect(h, ref_hist), 3) for h in hists]
        except Exception:
            cohesion = [None] * len(labels)
            vs_ref = [None] * len(labels)
        out = []
        for i, lab in enumerate(labels):
            e = raw[i] if i < len(raw) else {}
            sc = int(e.get("score", 0))
            outlier = bool(e.get("outlier", False))
            coh, vr = cohesion[i], vs_ref[i]
            pal_ok = ((coh is None or coh >= _PALETTE_COHESION_FLOOR)
                      and (vr is None or vr >= _PALETTE_REF_FLOOR))
            reason = str(e.get("reason", ""))[:160]
            if not pal_ok:
                reason = (f"PALETTE DRIFT (cohesion {coh} < "
                          f"{_PALETTE_COHESION_FLOOR} or vs-ref {vr} < "
                          f"{_PALETTE_REF_FLOOR}). " + reason)[:160]
            # A frame passes only if it matches the reference, isn't a
            # frame-to-frame outlier, AND holds the batch's palette.
            out.append({"label": lab, "score": sc, "outlier": outlier,
                        "palette_cohesion": coh, "palette_vs_ref": vr,
                        "reason": reason,
                        "pass": sc >= pass_floor and not outlier and pal_ok})
        flagged = [f["label"] for f in out if not f["pass"]]
        return {"ok": True, "frames": out, "floor": pass_floor,
                "min": min((f["score"] for f in out), default=None),
                "outliers": [f["label"] for f in out if f["outlier"]],
                "flagged": flagged}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _reference_sanity(path):
    """Structural gate for a freshly generated character reference — run BEFORE
    spending one edit per pose conditioned on it. gpt-image sometimes returns an
    'ok' result that is still unusable: a near-empty frame, or one whose
    background never keyed to transparent (a fully-filled rectangle). Identity
    can't be auto-judged with no ground truth, but 'is this a usable single
    transparent figure' can. Catching it here caps a broken run at ~1 spend
    instead of N poses that all inherit the flaw and all fail the pose gate.
    Returns (ok: bool, reason: str). Any checker error is treated as PASS — this
    must never block a good run; the per-pose consistency gate still runs after.
    """
    try:
        from PIL import Image as _Img
        im = _Img.open(path).convert("RGBA")
        im.thumbnail((128, 128))
        data = list(im.getdata())
        n = len(data) or 1
        opaque = sum(1 for _, _, _, a in data if a > 40)
        cov = opaque / n
        if cov < 0.04:
            return False, (f"near-empty reference (opaque coverage {cov:.3f}) — "
                           "the generation produced almost nothing")
        if cov > 0.93:
            return False, (f"background did not key to transparent (opaque "
                           f"coverage {cov:.3f}) — the reference is a filled "
                           "frame, not a cut-out character")
        return True, f"coverage {cov:.3f}"
    except Exception as exc:
        return True, f"sanity check skipped: {type(exc).__name__}"


# Chroma-key candidates. Generating on a solid backdrop the character never uses,
# then keying it out, keeps white/light interiors (eyes) OPAQUE — gpt-image's
# transparent mode punched those to holes. The color is chosen per character so it
# never collides with the art (Tommy has green features -> green screen would eat
# them; magenta wins).
_CHROMA = [("magenta", (255, 0, 255)), ("green", (0, 255, 0)),
           ("cyan", (0, 255, 255)), ("blue", (0, 64, 255)), ("yellow", (255, 235, 0))]


def _pick_chroma(ref_path):
    """Pick the chroma color FARTHEST from the character's own palette."""
    try:
        from PIL import Image as _Img
        im = _Img.open(ref_path).convert("RGBA"); im.thumbnail((128, 128))
        px = [(r, g, b) for r, g, b, a in im.getdata() if a > 60]
        if not px:
            return _CHROMA[0]
        q = _Img.new("RGB", (len(px), 1)); q.putdata(px); q = q.quantize(10)
        pal = q.getpalette()[:30]
        doms = [tuple(pal[i * 3:i * 3 + 3]) for i in range(10)]
        best, best_d = _CHROMA[0], -1.0
        for nm, c in _CHROMA:
            d = min(sum((a - b) ** 2 for a, b in zip(c, dom)) ** 0.5 for dom in doms)
            if d > best_d:
                best_d, best = d, (nm, c)
        return best
    except Exception:
        return _CHROMA[0]


def _chroma_key(img, chroma, tol=125, despill=185):
    """Key a solid chroma backdrop to transparent, in place, with edge despill.
    Distance-based; safe because the chroma is auto-picked far from the art."""
    px = img.load(); W, H = img.size; cr, cg, cb = chroma
    for y in range(H):
        for x in range(W):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            d = ((r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2) ** 0.5
            if d < tol:
                px[x, y] = (0, 0, 0, 0)
            elif d < despill:                       # pull fringe away from the chroma
                m = (r + g + b) // 3
                px[x, y] = ((r + m) // 2, (g + m) // 2, (b + m) // 2, a)
    return img


@mcp.tool()
def image_sprites(character_prompt: str, poses: list[dict], name: str,
                  ref_image: Optional[str] = None, frame_width: int = 160,
                  frame_height: int = 240, quality: str = "medium",
                  ref_quality: str = "high", fps: float = 8.0,
                  res_dir: str = "assets/sprites", max_retries: int = 1) -> dict:
    """PAINTED sprite set via gpt-image — REFERENCE-FIRST for consistency.

    How it works (and why): a fresh generation invents a new character every
    time, and asking for many poses in one image comes back misaligned. So:
    (1) generate ONE reference character (or pass ref_image to reuse an approved
    one — reusing the ref is also how you REGENERATE a single pose later without
    changing the fighter); (2) each pose is an EDIT conditioned on that
    reference — same character, new stance; (3) frames are alpha-trimmed,
    bottom-centered, stitched into <name>_sheet.png + <name>_frames.tres (one
    animation per pose) — drop-in for AnimatedSprite2D.

    character_prompt: the character + art style (full body, single character —
    framing/transparency contracts are appended automatically).
    poses: [{"name": "jab", "description": "lead fist fully extended right,
    body driving forward"}] — name becomes the animation; description is the
    stance. LOOK at the reference preview before the poses run wild, and at the
    sheet preview before importing. Cost: 1 ref + 1 edit per pose (~$0.04-0.25
    each by quality). Failed poses are listed, never silently shipped.
    """
    try:
        if not poses:
            raise ValueError("poses list is empty")
        for p in poses:
            if "name" not in p:
                raise ValueError(f"each pose needs a 'name': {p}")
        root = _Path(_root())
        art_dir = root / ".bgate_out" / "art" / name
        from bgate_adapters import imagegen, sprites as _sp

        # The stored visual identity, if one exists — injected into EVERY
        # prompt so no generation depends on anyone's memory of the character.
        profile = None
        for key in ((str(ref_image),) if ref_image else ()) + (name, f"{name}-character"):
            profile = _refs.profile_get(root, key)
            if profile:
                break
        identity = ""
        if profile:
            identity = (f" IDENTITY (must hold exactly): {profile['traits']}. "
                        f"STYLE (must hold exactly): {profile['style']}. "
                        f"NEVER: {profile['negative']}.")

        # 1. The reference — the single source of who this character is.
        result: dict = {"poses_attempted": len(poses),
                        "profile_used": bool(profile)}
        if ref_image:
            ref_path = _refs.resolve(root, str(ref_image))
        else:
            ref_path = str(art_dir / "reference.png")

            def _gen_ref():
                r = imagegen.generate(
                    character_prompt + " Exactly one character, full body head to "
                    "toe, neutral idle stance, centered, fully transparent "
                    "background, no text, no logo, no ground shadow.",
                    ref_path, size="1024x1536", quality=ref_quality,
                    transparent=True)
                if r.get("ok"):
                    result["reference_preview"] = _archive_preview(
                        ref_path, f"ref-{name}")
                return r

            ref = _gen_ref()
            if not ref.get("ok"):
                return {"ok": False, "stage": "reference", **ref}
            # REFERENCE GATE: validate the anchor and re-roll it BEFORE paying
            # for N poses. A broken reference makes every pose broken — every one
            # fails the pose gate, every one gets retried, and the run costs ~2N
            # against garbage. Catch it here at ~1 spend. A passed-in ref_image is
            # already approved and skips this.
            ok_ref, ref_reason = _reference_sanity(ref_path)
            rtries = max(0, int(max_retries))
            while not ok_ref and rtries > 0:
                rtries -= 1
                ref = _gen_ref()
                if not ref.get("ok"):
                    return {"ok": False, "stage": "reference", **ref}
                ok_ref, ref_reason = _reference_sanity(ref_path)
            result["reference_gate"] = {"ok": ok_ref, "reason": ref_reason}
            if not ok_ref:
                return {"ok": False, "stage": "reference_gate",
                        "reference": ref_path,
                        "reference_preview": result.get("reference_preview"),
                        "error": f"reference failed the structural gate: "
                                 f"{ref_reason}. Not spending on poses against a "
                                 "broken anchor — adjust character_prompt and retry."}
        result["reference"] = ref_path

        # 2. Each pose derives from the reference — same fighter, new stance.
        # ANCHOR + ROLLING conditioning: every edit carries (a) the character
        # ANCHOR — always present, so identity re-grounds each call and drift
        # can't compound telephone-style, (b) the PREVIOUS successful frame —
        # motion continuity, (c) for the closing frame of a multi-frame
        # animation, that animation's FIRST frame — so cycles loop smoothly
        # (walk/2 flows back into walk/0). ONE frame per API call, always.
        pose_files: list[tuple[str, str]] = []
        pose_errors: list[dict] = []
        prev_frame: Optional[str] = None
        anim_first: dict[str, str] = {}
        anim_counts: dict[str, int] = {}
        for p in poses:
            anim_counts[p["name"].split("/", 1)[0]] = \
                anim_counts.get(p["name"].split("/", 1)[0], 0) + 1
        pose_desc: dict[str, str] = {}
        # STAGE 1 — pick a chroma backdrop this character never uses.
        chroma_name, chroma_rgb = _pick_chroma(ref_path)
        result["chroma"] = chroma_name

        def _edit_pose(desc, refs, out_png):
            # STAGE 2 — generate the pose on a FLAT chroma backdrop (opaque), so
            # white interiors (eyes) stay solid instead of being punched transparent.
            got = imagegen.edit(
                "This exact character from the reference image"
                + (" (shown again in the other image(s) in different poses of "
                   "the same motion)" if len(refs) > 1 else "")
                + " — identical design, colors, face, and art style. CRITICAL: "
                "keep the EXACT SAME BODY BUILD, musculature, height, weight, head "
                "size and limb proportions as the reference in EVERY frame — do NOT "
                "slim him down, bulk him up, change his muscle definition, or restyle "
                "the body between frames; ONLY the pose changes"
                f" — now in this stance: {desc}. ONE single full-body "
                "character head to toe, exactly one figure, no text, no cropping of "
                f"limbs. Place the character on a COMPLETELY FLAT SOLID pure "
                f"{chroma_name} background (RGB {chroma_rgb[0]},{chroma_rgb[1]},"
                f"{chroma_rgb[2]}), the entire background filled edge-to-edge with "
                "that one flat color, NO gradient, NO shadow, NO other objects."
                + identity,
                refs, out_png, size="1024x1536", quality=quality,
                transparent=False)
            # STAGE 3 — key the chroma backdrop out to clean transparency.
            if got.get("ok"):
                try:
                    from PIL import Image as _I
                    im = _I.open(out_png).convert("RGBA")
                    _chroma_key(im, chroma_rgb)
                    im.save(out_png)
                except Exception:
                    pass
            return got

        for pose in poses:
            pname = pose["name"]
            desc = pose.get("description", pname)
            pose_desc[pname] = desc
            anim, _, idx = pname.partition("/")
            out_png = str(art_dir / f"pose_{pname.replace('/', '_')}.png")
            refs = [ref_path]
            is_last_of_cycle = (idx.isdigit() and anim_counts[anim] > 1
                                and int(idx) == anim_counts[anim] - 1)
            if is_last_of_cycle and anim in anim_first and anim_first[anim] != prev_frame:
                refs.append(anim_first[anim])
            if prev_frame:
                refs.append(prev_frame)
            got = _edit_pose(desc, refs, out_png)
            if got.get("ok"):
                pose_files.append((pname, out_png))
                prev_frame = out_png
                if anim not in anim_first:
                    anim_first[anim] = out_png
                # Register each pose as a candidate the moment it exists: the
                # Assets gallery streams the batch live (reviewable mid-run)
                # instead of going dark for a 30-minute silent mega-call.
                try:
                    _register_artifact(
                        f"{name}_{pname.replace('/', '_')}", out_png,
                        producer="image_sprites",
                        prompt=str(desc)[:500])
                except Exception:
                    pass
            else:
                pose_errors.append({"name": pname, "error": got.get("error")})

        if not pose_files:
            return {"ok": False, "stage": "poses", "failed": pose_errors,
                    "reference": ref_path,
                    "error": "every pose generation failed"}

        # 3. Assemble + AUTO CONSISTENCY GATE, with bounded retry of flagged frames.
        # The gate scores every frame vs the reference AND for frame-to-frame build
        # drift; any flagged pose is re-rolled (on its rolling refs, not the bare
        # anchor — see _rolling_refs) up to max_retries, keeping whichever roll
        # scores best. This turns the gate from "detects drift" into "converges
        # on a consistent sheet".
        import shutil as _shutil
        pose_order = [p for p, _ in pose_files]
        pose_path = {p: fp for p, fp in pose_files}

        def _rolling_refs(pname):
            """Reconstruct the ANCHOR+ROLLING ref list a pose was first built
            with, so a RETRY keeps motion continuity. Re-rolling on the bare
            anchor (the old behavior) optimizes the gate's identity metric while
            silently dropping the cross-frame conditioning — a re-rolled mid-cycle
            frame could score better on identity yet pop out of the walk. The gate
            doesn't measure motion, so nothing caught it. Rebuild: anchor, plus
            the cycle's first frame for a closing frame, plus the previous frame."""
            anim, _, idx = pname.partition("/")
            refs = [ref_path]
            if (idx.isdigit() and anim_counts.get(anim, 1) > 1
                    and int(idx) == anim_counts[anim] - 1):
                first = pose_path.get(f"{anim}/0")
                if first and first not in refs:
                    refs.append(first)
            i = pose_order.index(pname)
            if i > 0:
                prev = pose_path.get(pose_order[i - 1])
                if prev and prev not in refs:
                    refs.append(prev)
            return refs

        def _assemble_and_gate():
            asm = _sp.from_pose_images(
                [(p, pose_path[p]) for p in pose_order],
                out_dir=str(root / ".bgate_out" / "sprites"), name=name,
                frame_size=(frame_width, frame_height), res_dir=res_dir, fps=fps,
                ref_path=ref_path)
            asm.setdefault("failed", [])
            asm["failed"].extend(pose_errors)
            cons = {"ok": False}
            if asm.get("ok"):
                fm = asm.get("frames", {})
                cons = _vision_consistency(ref_path, [(p, fp) for p, fp in fm.items()])
            return asm, cons

        assembled, consistency = _assemble_and_gate()
        best_min = consistency.get("min") if consistency.get("ok") else None
        tries = max(0, int(max_retries))
        while (consistency.get("ok") and consistency.get("flagged") and tries > 0):
            tries -= 1
            flagged = list(consistency["flagged"])
            backups = {}
            for pname in flagged:
                if pname not in pose_path or pname not in pose_desc:
                    continue
                bak = pose_path[pname] + ".bak"
                try:
                    _shutil.copy2(pose_path[pname], bak); backups[pname] = bak
                except Exception:
                    pass
                # Re-roll WITH the rolling refs, not the bare anchor — keep motion
                # continuity while the gate chases identity (see _rolling_refs).
                _edit_pose(pose_desc[pname], _rolling_refs(pname), pose_path[pname])
            asm2, cons2 = _assemble_and_gate()
            new_min = cons2.get("min") if cons2.get("ok") else None
            if new_min is not None and (best_min is None or new_min > best_min):
                best_min = new_min; assembled, consistency = asm2, cons2
                for bak in backups.values():
                    try: os.remove(bak)
                    except Exception: pass
            else:
                for pname, bak in backups.items():   # revert: this roll was no better
                    try: _shutil.copy2(bak, pose_path[pname]); os.remove(bak)
                    except Exception: pass
                assembled, consistency = _assemble_and_gate()

        assembled["reference"] = ref_path
        assembled["chroma"] = result.get("chroma")
        if "reference_preview" in result:
            assembled["reference_preview"] = result["reference_preview"]
        if assembled.get("ok"):
            archived = _archive_preview(assembled["sheet"], f"painted-{name}")
            if archived:
                assembled["preview"] = archived

            frame_map = assembled.get("frames", {})
            assembled["consistency"] = consistency
            needs_review = bool(consistency.get("ok") and consistency.get("flagged"))

            artifact = _register_artifact(
                name, assembled["sheet"], producer="image_sprites",
                prompt=character_prompt,
                refs=[str(ref_image)] if ref_image else [ref_path],
                metadata={"poses": poses, "frames": frame_map,
                          "failed": assembled.get("failed", []),
                          "preview": archived or "",
                          "consistency": consistency,
                          "sequence": assembled.get("sequence")})
            if artifact:
                assembled["artifact"] = artifact
                # Record the check on the revision so the dashboard shows it and the
                # sheet stops reading as "NOT CHECKED · consistency".
                try:
                    _artifacts.record_check(_root(), assembled["sheet"], "consistency",
                                            consistency)
                except Exception:
                    pass
            cons_note = ""
            if consistency.get("ok"):
                cons_note = (f", consistency min {consistency.get('min')}"
                             + (f" — REGEN {consistency['flagged']}" if consistency.get("flagged")
                                else " (all pass)"))
            seq = assembled.get("sequence") or {}
            seq_note = (f", motion-jitter in {seq['flagged']}"
                        if seq.get("flagged") else "")
            _log("sprites", f"painted sprite set {name!r} (reference-first): "
                            f"{len(frame_map)}/{len(poses)} poses"
                            + (f", {len(assembled['failed'])} FAILED" if assembled["failed"] else "")
                            + cons_note + seq_note,
                 ref=assembled["sheet"])
        return assembled
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Godot
# ---------------------------------------------------------------------------
@mcp.tool()
def godot_status() -> dict:
    """Is Godot available, and which version? Check before engine work."""
    try:
        probe = _godot.available()
        return {**probe, **(_godot.version() if probe["available"] else {})}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def godot_run(script: str, project_dir: Optional[str] = None,
              timeout: int = 120) -> dict:
    """Run a GDScript headless and capture its output.

    The script MUST `extends SceneTree`, do its work in `_init()`, and call
    `quit()` — without quit() it runs until the timeout. Returns stdout, stderr,
    and any parse/script errors (Godot prints SCRIPT ERROR and still exits 0, so
    check `errors`, not just the exit code).
    """
    try:
        return _godot.run_script(script, project_dir=project_dir, timeout=timeout)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def godot_templates() -> dict:
    """What project templates are available to scaffold."""
    try:
        return {"templates": _scaffold.list_templates()}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def godot_scaffold(name: str, kind: str = "2d", dest: Optional[str] = None,
                   force: bool = False) -> dict:
    """Create a runnable Godot project wired for playtesting.

    kind: 2d (platformer slice) | 3d (first-person slice). dest defaults to
    <project root>/game.

    The template ships the BGate telemetry autoload already registered, and a
    player whose feel tunables (gravity, fall_multiplier, coyote_time) are both
    exported AND emitted on jump/land — so the first playtest already produces
    the telemetry join. Refuses a non-empty dest unless force=True.
    """
    try:
        target = dest or str(_Path(_root()) / "game")
        result = _scaffold.new_project(target, name, kind=kind, force=force)
        _log("scaffold", f"scaffolded {kind} project {name!r}", ref=result["path"])
        return result
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def godot_check_project(project_dir: str, timeout: int = 180) -> dict:
    """Import/validate a project headless — the 'does it still build' check."""
    try:
        return _godot.check_project(project_dir, timeout=timeout)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def godot_import_asset(project_dir: str, src_path: str, dest_rel: str = "assets",
                       timeout: int = 240) -> dict:
    """Bring an asset (e.g. a Blender .glb) into a project and VERIFY the engine loads it.

    Copies the file in, triggers a headless import, then loads the resource
    IN-ENGINE and reports the meshes Godot actually built — tri counts, UVs,
    materials, bounding box. Copying a file in is not integration: an asset that
    imports with zero surfaces is a silent failure, and this catches it by
    checking the engine's view, not the file's presence. The end of the
    Blender→Godot round trip.
    """
    try:
        result = _godot.import_asset(project_dir, src_path, dest_rel=dest_rel,
                                     timeout=timeout)
        # Register the landed asset so asset_verify covers it from birth. Only
        # possible when the game project lives inside the bgate root.
        if result.get("ok") and result.get("copied_to"):
            try:
                result["registry"] = _assets.track(_root(), result["copied_to"])
            except Exception as exc:
                result["registry"] = {"tracked": False, "reason": str(exc)}
            tris = result.get("engine_view", {}).get("total_tris", "?")
            _log("asset", f"landed {result['res_path']} ({tris} tris in-engine)",
                 ref=result["res_path"])
            try:
                linked = _artifacts.record_check(
                    _root(), result["copied_to"], "engine_import", result)
                if linked is None:
                    _artifacts.record_check(
                        _root(), src_path, "engine_import", result)
            except Exception:
                pass
        return result
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def godot_screenshot(project_dir: str, at: float = 1.0, scene: Optional[str] = None,
                     label: str = "", timeout: int = 120) -> dict:
    """Run the ACTUAL game and capture the viewport to a PNG at `at` seconds.

    The look-iteration loop: headless checks prove the game boots, this shows
    what it LOOKS like. A game window appears briefly on the user's screen
    (rendering needs a display) and closes itself after the capture. The shot
    is archived to the preview gallery — check it before and after visual work.
    """
    try:
        out = str(_Path(_root()) / ".bgate_out" / "shot.png")
    except Exception:
        out = "bgate_shot.png"
    try:
        result = _godot.screenshot(project_dir, out, at=at, scene=scene,
                                   timeout=timeout)
        if result.get("ok"):
            archived = _archive_preview(result["path"], f"shot-{label or 'game'}")
            if archived:
                result["preview"] = archived
            _log("screenshot", f"captured the running game at t={at}s"
                               + (f" ({label})" if label else ""),
                 ref=archived or result["path"])
        return result
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def godot_inspect_resource(project_dir: str, res_path: str, timeout: int = 180) -> dict:
    """Load a res:// resource in-engine and report what it actually became.

    Meshes, tri counts, per-surface UV/material, bounding box — the engine's
    view of an asset already in the project.
    """
    try:
        return _godot.inspect_resource(project_dir, res_path, timeout=timeout)
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Reference anchors
# ---------------------------------------------------------------------------
@mcp.tool()
def ref_pin(name: str, path: str, kind: str = "style", note: str = "") -> dict:
    """Pin an APPROVED image as a canonical reference anchor.

    The file is copied into .bgate/refs/ (durable, travels with the project)
    under the given name; every seat brief lists the pins, and image_edit /
    image_sprites accept pin names anywhere they accept paths. Pin a character's
    approved reference, the style anchor, concept mocks from the user — the
    things art must stay consistent WITH. Re-pinning a name upgrades the anchor
    in place. kind: character | style | ui | concept.
    """
    try:
        return _refs.pin(_root(), name, path, kind=kind, note=note)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def ref_list(kind: Optional[str] = None) -> dict:
    """The pinned reference anchors. Check BEFORE generating character/style art."""
    try:
        return {"refs": _refs.list_refs(_root(), kind=kind)}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def profile_set(name: str, traits: str, style: str, negative: str) -> dict:
    """Store a character's visual identity — written while LOOKING at the pinned
    reference, never from memory. Injected automatically into every
    image_sprites generation for this character, and consistency_check judges
    against it. traits = what the character IS; style = the rendering style
    every frame must hold; negative = what must never appear.
    """
    try:
        return _refs.profile_set(_root(), name, traits=traits, style=style,
                                 negative=negative)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def profile_get(name: str) -> dict:
    """A character's stored visual identity (or {missing: true})."""
    try:
        got = _refs.profile_get(_root(), name)
        return got if got else {"missing": True, "name": name}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def consistency_check(candidate_path: str, character: str) -> dict:
    """Judge a generated frame against its character — from a BUILT comparison,
    never from memory. Composes reference | candidate side-by-side on a
    checkerboard (alpha honesty), archives it to the gallery, and returns the
    profile checklist + a palette-drift tripwire. YOU then look at the
    composite and verdict each checklist line. A frame only lands if every
    line passes. This exists because three off-style batches were approved by
    agents judging frames in isolation.
    """
    try:
        from PIL import Image

        root = _Path(_root())
        ref_path = _refs.resolve(root, character)
        profile = _refs.profile_get(root, character)

        def _board(img: Image.Image) -> Image.Image:
            board = Image.new("RGB", img.size, (140, 140, 140))
            tile = 16
            for y in range(0, img.size[1], tile):
                for x in range(0, img.size[0], tile):
                    if (x // tile + y // tile) % 2:
                        board.paste((180, 180, 180), (x, y, min(x + tile, img.size[0]),
                                                      min(y + tile, img.size[1])))
            board.paste(img, (0, 0), img)
            return board

        ref = Image.open(ref_path).convert("RGBA")
        cand = Image.open(candidate_path).convert("RGBA")
        h = 512
        ref.thumbnail((h, h))
        cand.thumbnail((h, h))
        combo = Image.new("RGB", (ref.width + cand.width + 12, max(ref.height, cand.height)),
                          (24, 24, 28))
        combo.paste(_board(ref), (0, 0))
        combo.paste(_board(cand), (ref.width + 12, 0))
        out = root / ".bgate_out" / "art" / "consistency_check.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        combo.save(out)
        archived = _archive_preview(str(out),
                                    f"check-{_Path(candidate_path).stem}"[:40])

        # Palette tripwire (advisory — catches color drift, blind to identity).
        def _pal(img, n=6):
            img = img.copy()
            img.thumbnail((128, 128))
            px = [(r, g, b) for r, g, b, a in img.getdata() if a > 64]
            if not px:
                return []
            q = Image.new("RGB", (len(px), 1))
            q.putdata(px)
            q = q.quantize(n)
            pal = q.getpalette()[:n * 3]
            return [tuple(pal[i * 3:i * 3 + 3]) for _, i in
                    sorted(q.getcolors(), reverse=True)[:n]]

        pa, pb = _pal(ref), _pal(cand)
        drift = (round(sum(min(sum((x - y) ** 2 for x, y in zip(c, d)) ** 0.5
                               for d in pb) for c in pa) / len(pa), 1)
                 if pa and pb else None)

        checklist = ["same character design (species/build/proportions)",
                     "same rendering style (brushwork/detail level — no added "
                     "texture like fur, hair, etched lines)",
                     "same palette family", "no extra elements (glow, shadow, props)"]
        if profile:
            checklist.insert(0, f"matches traits: {profile['traits'][:160]}")
            checklist.insert(1, f"holds style: {profile['style'][:160]}")
            checklist.append(f"nothing from the negative list: {profile['negative'][:160]}")

        # ALPHA / TRANSPARENCY TRIPWIRE (automated — the palette check above is
        # blind to transparency because it samples only a>64). gpt-image leaves
        # white halos, feathered fringes, opaque background bleed, dirty RGB under
        # zero alpha, and hollow interiors that a checklist-by-eye keeps missing.
        # These are measured, not guessed at, and any flag is a hard fail.
        def _alpha_flags(path):
            im = Image.open(path).convert("RGBA")
            im.thumbnail((256, 256))
            W, H = im.size
            px = im.load()
            border = border_op = soft = opaque = softc = whal = 0
            dirty = transp = 0
            xs0 = ys0 = 10 ** 9
            xs1 = ys1 = -1
            for y in range(H):
                for x in range(W):
                    r, g, b, a = px[x, y]
                    edge = (x == 0 or y == 0 or x == W - 1 or y == H - 1)
                    if edge:
                        border += 1
                        if a > 32:
                            border_op += 1
                    if a >= 224:
                        opaque += 1
                    if 24 < a < 224:
                        soft += 1
                    if 24 < a < 240:
                        softc += 1
                        if r > 228 and g > 228 and b > 228:
                            whal += 1
                    if a <= 8:
                        transp += 1
                        if r > 16 or g > 16 or b > 16:
                            dirty += 1
                    if a > 32:
                        xs0 = min(xs0, x); xs1 = max(xs1, x)
                        ys0 = min(ys0, y); ys1 = max(ys1, y)
            border_opaque = border_op / max(1, border)
            soft_ratio = soft / max(1, opaque + soft)
            white_fringe = whal / max(1, softc)
            dirty_alpha = dirty / max(1, transp)
            # HOLLOW = transparent ENCLOSED by opaque (a real hole), not the open
            # gaps between spread limbs. Flood-fill transparency inward from the
            # frame border; whatever transparency it can't reach is enclosed.
            seen = bytearray(W * H)
            stack = []
            for x in range(W):
                for y in (0, H - 1):
                    i = y * W + x
                    if px[x, y][3] <= 16 and not seen[i]:
                        seen[i] = 1; stack.append((x, y))
            for y in range(H):
                for x in (0, W - 1):
                    i = y * W + x
                    if px[x, y][3] <= 16 and not seen[i]:
                        seen[i] = 1; stack.append((x, y))
            while stack:
                x, y = stack.pop()
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < W and 0 <= ny < H:
                        i = ny * W + nx
                        if not seen[i] and px[nx, ny][3] <= 16:
                            seen[i] = 1; stack.append((nx, ny))
            enclosed = sum(1 for y in range(H) for x in range(W)
                           if px[x, y][3] <= 16 and not seen[y * W + x])
            hollow = enclosed / max(1, opaque)

            # HARD flags (auto-fail): these read ~0 on a clean transparent sprite.
            flags = []
            if border_opaque > 0.06:
                flags.append(f"background bleed: {border_opaque:.0%} of the frame "
                             "border is opaque — sprite not isolated on transparency")
            if white_fringe > 0.20:
                flags.append(f"white halo: {white_fringe:.0%} of soft-edge pixels are "
                             "near-white — feathered white fringe around the sprite")
            if soft_ratio > 0.35:
                flags.append(f"feathered alpha: {soft_ratio:.0%} soft/partial-alpha — "
                             "edges aren't crisp (gpt-image halo)")
            if dirty_alpha > 0.15:
                flags.append(f"dirty alpha: {dirty_alpha:.0%} of transparent pixels "
                             "carry nonzero RGB — clean RGB:=0 where alpha==0")
            # SOFT (advisory): enclosed gaps can be legit (a curled arm), so flag
            # for a human look rather than auto-failing.
            review = []
            if hollow > 0.05:
                review.append(f"possible hole: {hollow:.0%} of the figure is "
                              "transparent area ENCLOSED by the sprite — look for an "
                              "empty/holed region (vs. intended open gaps)")
            return {"border_opaque": round(border_opaque, 3),
                    "white_fringe": round(white_fringe, 3),
                    "soft_alpha": round(soft_ratio, 3),
                    "dirty_alpha": round(dirty_alpha, 3),
                    "hollow": round(hollow, 3),
                    "flags": flags, "review": review, "clean": not flags}

        try:
            alpha = _alpha_flags(candidate_path)
        except Exception as ae:
            alpha = {"flags": [], "clean": None, "error": str(ae)}

        checklist = (["ALPHA fail: " + f for f in alpha.get("flags", [])]
                     + ["ALPHA look: " + f for f in alpha.get("review", [])]
                     + checklist)

        result = {"composite": archived or str(out), "reference": ref_path,
                  "palette_drift": drift,
                  "palette_note": "advisory: >30 = color drift likely; low values "
                                  "do NOT prove identity",
                  "alpha": alpha,
                  "auto_fail": bool(alpha.get("flags")),
                  "checklist": checklist,
                  "instruction": ("LOOK at the composite. Verdict every checklist "
                                  "line explicitly. Any fail = do not land. If "
                                  "alpha.flags is non-empty the frame AUTO-FAILS on "
                                  "transparency (white halo / bleed / hollow / dirty "
                                  "alpha) — regenerate; do not land it.")}
        try:
            _artifacts.record_check(
                _root(), candidate_path, "consistency", result)
        except Exception:
            pass
        return result
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def art_qa_verdict(artifact_id: int, verdict: str, score: int = 0,
                   reasons: str = "") -> dict:
    """Record an INDEPENDENT art-QA reviewer's verdict on a candidate artifact.

    For the art-consistency reviewer (a seat that did NOT make the image) after
    it has run consistency_check and looked at the produced image beside its
    reference. verdict 'pass' approves the revision; 'fail' rejects it. The
    score (0-100 similarity) and reasons are stored on the revision under
    metadata.qa_review so the dashboard can show why. This is what stops the art
    seat from self-approving drift — the accept/reject is made here, by review.
    """
    verdict = (verdict or "").strip().lower()
    if verdict not in ("pass", "fail"):
        return _fail(ValueError("verdict must be 'pass' or 'fail'"))
    try:
        root = _root()
        art = _artifacts.get(root, int(artifact_id))
        try:
            score = max(0, min(100, int(score)))
        except (TypeError, ValueError):
            score = 0
        _artifacts.record_check(root, art["path"], "qa_review", {
            "verdict": verdict, "score": score, "reasons": reasons[:1000]})
        status = "approved" if verdict == "pass" else "rejected"
        reviewed = _artifacts.review(root, int(artifact_id), status,
                                     note=f"art-QA {verdict} ({score}/100): {reasons[:400]}")
        return {"ok": True, "artifact_id": int(artifact_id), "verdict": verdict,
                "score": score, "status": reviewed["status"],
                "logical_name": art["logical_name"], "revision": art["revision"]}
    except LookupError as exc:
        return _fail(exc)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def ref_unpin(name: str) -> dict:
    """Remove a pin (the file itself is kept — deleting canon art is a human call)."""
    try:
        return _refs.unpin(_root(), name)
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Assets — locks for the files git can't merge
# ---------------------------------------------------------------------------
@mcp.tool()
def asset_lock(path: str, seat: str) -> dict:
    """Claim a binary asset for one seat BEFORE editing it.

    Binary files (.blend, .glb, textures, audio) don't merge — two agents editing
    one .blend loses someone's work. Lock first, edit, then asset_release. A held
    lock errors rather than queues: decide to wait, or work on something else.
    Lock-before-create is the normal flow for new assets.
    """
    try:
        bound_seat, owner = _lock_identity(seat)
        return _assets.lock(_root(), path, bound_seat, owner=owner)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def asset_release(path: str, seat: str, force: bool = False) -> dict:
    """Release a lock when the edit is done — records the new content hash.

    Only the holding seat can release. force=True breaks anyone's lock (for a
    dead agent's stale claim) — a human's call, not a convenience.
    """
    try:
        if force:
            return _assets.force_release(_root(), path)
        bound_seat, owner = _lock_identity(seat)
        return _assets.release(_root(), path, bound_seat, owner=owner)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def asset_track(path: str) -> dict:
    """Register an existing file under its content hash (sha256)."""
    try:
        return _assets.track(_root(), path)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def asset_status(kind: Optional[str] = None, locked_only: bool = False) -> dict:
    """List tracked assets, optionally by kind or only the locked ones."""
    try:
        return {"assets": _assets.list_assets(_root(), kind=kind,
                                              locked_only=locked_only)}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def asset_verify() -> dict:
    """Audit every tracked asset against disk — catches silent clobbers.

    'modified' means content changed with NO lock held: an unlocked write or an
    outside edit. Locked files are expected to differ and aren't drift. Run this
    before builds and after any multi-agent session.
    """
    try:
        return _assets.verify(_root())
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Iterations
# ---------------------------------------------------------------------------
@mcp.tool()
def iteration_status(limit: int = 10) -> dict:
    """Causal iteration history: snapshots, assets, playtests, decisions, work, outcome."""
    try:
        return {"iterations": _iterations.list_iterations(_root(), limit=limit)}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def iteration_record_checks(status: str, summary: str = "",
                            checks: Optional[dict] = None) -> dict:
    """Attach automated-check results to the active iteration and next snapshot."""
    try:
        return _iterations.record_checks(
            _root(), {"status": status, "summary": summary,
                      "checks": checks or {}})
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Playtest
# ---------------------------------------------------------------------------
@mcp.tool()
def playtest_devices(filter_text: str = "") -> dict:
    """List mic inputs and open windows — pick what to record before starting."""
    try:
        return {
            "inputs": _recorder.list_inputs(),
            "windows": _recorder.list_windows(filter_text),
            "note": "pass an input 'index' as mic_device, and a window 'title' "
                    "as window_title",
        }
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def playtest_check(mic_device: Optional[int] = None,
                   window_title: Optional[str] = None,
                   native: bool = False) -> dict:
    """Preflight a session: ffmpeg, mic SIGNAL, transcriber, target window.

    ALWAYS run this before playtest_start. It records a short mic sample and
    measures level — a muted or unplugged mic records perfect digital silence,
    which looks identical to a working one until the transcript comes back empty
    and the whole playthrough is wasted.
    """
    try:
        return _playtest.preflight(
            mic_device=mic_device, window_title=window_title,
            root=_root(), native=native)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def playtest_start(name: str, window_title: Optional[str] = None,
                   mic_device: Optional[int] = None, build_ref: str = "",
                   fps: int = 30, launch_native: bool = False,
                   game_cmd: str = "") -> dict:
    """Start recording a play session — game window video + your voice.

    Play the game and talk out loud about what you like and what needs changing.
    Say it near when it happens; feedback is matched to game events by timestamp.

    window_title: match the game window (None = whole desktop). build_ref: the
    commit/build under test. Set launch_native to let the backend launch Godot
    with BGATE_TELEMETRY already attached; game_cmd optionally overrides the
    default <root>/game project command.
    """
    try:
        return _playtest.start(_root(), name, window_title=window_title,
                               mic_device=mic_device, build_ref=build_ref, fps=fps,
                               launch_native=launch_native, game_cmd=game_cmd)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def playtest_stop(session_id: Optional[int] = None, model: str = "base",
                  transcribe_now: bool = True) -> dict:
    """Stop recording, then transcribe, align, and classify feedback.

    Transcription runs a whisper model in a subprocess; expect roughly a minute
    per 10 minutes of audio on CPU (the first run also downloads the model).
    Items land as 'new' — nothing becomes work until you promote it.
    """
    try:
        return _playtest.stop(_root(), session_id, model=model,
                              transcribe_now=transcribe_now)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def playtest_brief(session_id: int, include_transcript: bool = False,
                   window_s: float = 4.0) -> dict:
    """The session as agents should read it: video frames + feedback + telemetry.

    You CAN watch the recording: `video_frames` is an ordered strip of stills
    ({i, t, path}) sampled across the whole session — Read them in order to see
    what happened. Each feedback item also carries a frame at its own moment and
    the game events within window_s of it, and `transcript` is what the player
    said, timestamped. Line frames up with the transcript by t.
    """
    try:
        return _playtest.brief(_root(), session_id, window_s=window_s,
                               include_transcript=include_transcript)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def playtest_list(status: Optional[str] = None) -> dict:
    """List play sessions. status: recording | processing | ready | failed."""
    try:
        return {"sessions": _playtest.list_sessions(_root(), status=status)}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def playtest_promote(item_id: int, seat: Optional[str] = None,
                     kind: Optional[str] = None, ref: str = "") -> dict:
    """Accept a feedback item as real work, optionally re-routing it.

    This is the human's call. Do not promote items on the user's behalf without
    being asked — thinking out loud mid-play is not a decision to build.
    """
    try:
        return _playtest.promote(_root(), item_id, seat=seat, kind=kind, ref=ref)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def playtest_dismiss(item_id: int) -> dict:
    """Drop a feedback item — noise, or already handled."""
    try:
        return _playtest.dismiss(_root(), item_id)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def playtest_telemetry_contract() -> dict:
    """What the game must emit so spoken feedback becomes actionable numbers."""
    try:
        return _playtest.telemetry_contract()
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Seats — stable roles, write lanes, and the blackboard
# ---------------------------------------------------------------------------
@mcp.tool()
def seat_list() -> dict:
    """The project's seats: role, mission, write lanes. Adopt one before working."""
    try:
        return {"seats": list(_seats.roles_for(_root()).values())}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def seat_brief(role: str) -> dict:
    """Everything a seat needs to start working, in one call.

    Mission, write lanes, the bible (with the scope cut applied), canon entities,
    the promoted playtest feedback routed to this seat, held/others' locks, and
    recent blackboard notes. Read this BEFORE doing seat work — it replaces
    re-deriving the project state from scratch.
    """
    try:
        return _seats.brief(_root(), role)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def seat_can_write(role: str, path: str) -> dict:
    """May this seat write this path? Check BEFORE editing outside your obvious lane.

    Two gates, both must pass: the path must be inside the seat's write lanes,
    and the file must not be locked by another seat — being in-lane does not
    excuse stomping a locked binary. Fails closed for unknown/disabled seats.
    """
    try:
        return _seats.can_write(_root(), role, path)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def seat_configure(role: str, enabled: Optional[bool] = None,
                   write_globs: Optional[list[str]] = None,
                   mission: Optional[str] = None) -> dict:
    """Override a seat for this project: disable it, or change lanes/mission."""
    try:
        return _seats.configure(_root(), role, enabled=enabled,
                                write_globs=write_globs, mission=mission)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def seat_post_note(role: str, body: str, topic: str = "") -> dict:
    """Leave a note on the blackboard for other seats.

    Post when your work changes another seat's world: an asset re-exported, a
    tunable renamed, a scope call made. Short and factual beats long and vague.
    """
    try:
        return _seats.post_note(_root(), role, body, topic=topic)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def seat_notes(topic: Optional[str] = None, role: Optional[str] = None,
               limit: int = 20) -> dict:
    """Read the blackboard, newest first, optionally filtered by topic or role."""
    try:
        return {"notes": _seats.read_notes(_root(), topic=topic, role=role,
                                           limit=limit)}
    except Exception as exc:
        return _fail(exc)


# ---------------------------------------------------------------------------
# Work queue
# ---------------------------------------------------------------------------
@mcp.tool()
def queue_list(status: Optional[str] = None, seat: Optional[str] = None) -> dict:
    """The work queue. status: queued | dispatched | done | failed."""
    try:
        from bgate_core import queue as _q
        return {"items": _q.list_items(_root(), status=status, seat=seat)}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def queue_add(seat: str, title: str, brief: str = "", priority: int = 0) -> dict:
    """Queue work for a seat. Use when your work uncovers work that isn't yours."""
    try:
        from bgate_core import queue as _q
        return _q.add(_root(), seat, title, brief=brief, priority=priority,
                      source=f"seat:{_seat() or 'unknown'}")
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def queue_update(item_id: int, title: Optional[str] = None, brief: Optional[str] = None,
                 seat: Optional[str] = None, priority: Optional[int] = None) -> dict:
    """Edit an existing work item in place (title/brief/seat/priority).

    For enriching a ticket without re-filing it — e.g. rewriting a transcript-
    era brief to add the frames, timestamps, and telemetry you saw while
    watching the recording. Only the fields you pass change; status and lineage
    stay put. Pass the full new brief text (this replaces, it does not append).
    """
    try:
        from bgate_core import queue as _q
        return _q.update(_root(), item_id, title=title, brief=brief,
                         seat=seat, priority=priority)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def queue_next(seat: str) -> dict:
    """The highest-priority queued item for a seat — what to work on next."""
    try:
        from bgate_core import queue as _q
        item = _q.next_for(_root(), seat)
        return item if item else {"empty": True, "seat": seat}
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def queue_complete(item_id: int, result: str, failed: bool = False) -> dict:
    """Close out a work item with an honest one-paragraph result.

    failed=True when the work did not land — say why plainly; a false 'done'
    poisons the queue's trustworthiness for everyone.
    """
    try:
        from bgate_core import queue as _q
        return _q.set_status(_root(), item_id, "failed" if failed else "done",
                             result=result)
    except Exception as exc:
        return _fail(exc)


@mcp.tool()
def queue_reopen(item_id: int, reason: str) -> dict:
    """Send a done/failed item back to 'queued' for another round.

    The QA gate's FAIL path: reason is the ranked nitpick list (specific
    problems + fixes). It is APPENDED to the item's brief so the next
    dispatched agent reads exactly what to fix, and recorded as the result.
    """
    try:
        from bgate_core import queue as _q
        root = _root()
        item = _q.get(root, item_id)
        if item["status"] not in ("done", "failed"):
            raise ValueError(
                f"item {item_id} is {item['status']!r} — only done/failed "
                "items can be reopened")
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("reason is required — say exactly what to fix")
        stamp = ("\n\n--- REOPENED (QA gate) ---\n" + reason)[:3000]
        _q.update(root, item_id, brief=(item["brief"] or "") + stamp)
        return _q.set_status(root, item_id, "queued",
                             result=f"reopened: {reason[:1900]}")
    except Exception as exc:
        return _fail(exc)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
