"""Builders Gate MCP server (FastMCP, stdio).

Every tool resolves the project from BGATE_ROOT or the cwd by walking up for a
.bgate dir, so an agent working inside a game repo never passes paths around.

Tool errors return a dict with an "error" key rather than raising: a raised
exception inside a tool call reads to the model as a broken server, while an
error payload reads as a fact it can act on.
"""
from __future__ import annotations

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
from bgate_core import project as _project
from bgate_core import search as _search

mcp = FastMCP("builders-gate")


def _root() -> str:
    """The active project root. BGATE_ROOT wins, else walk up from cwd.
    Also loads the project's .env (once) so secrets live with the project."""
    override = os.environ.get("BGATE_ROOT")
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


@mcp.tool()
def image_sprites(character_prompt: str, poses: list[dict], name: str,
                  ref_image: Optional[str] = None, frame_width: int = 160,
                  frame_height: int = 240, quality: str = "medium",
                  ref_quality: str = "high", fps: float = 8.0,
                  res_dir: str = "assets/sprites") -> dict:
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
            ref = imagegen.generate(
                character_prompt + " Exactly one character, full body head to "
                "toe, neutral idle stance, centered, fully transparent "
                "background, no text, no logo, no ground shadow.",
                ref_path, size="1024x1536", quality=ref_quality, transparent=True)
            if not ref.get("ok"):
                return {"ok": False, "stage": "reference", **ref}
            archived_ref = _archive_preview(ref_path, f"ref-{name}")
            result["reference_preview"] = archived_ref
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
        for pose in poses:
            pname = pose["name"]
            desc = pose.get("description", pname)
            anim, _, idx = pname.partition("/")
            out_png = str(art_dir / f"pose_{pname.replace('/', '_')}.png")
            refs = [ref_path]
            is_last_of_cycle = (idx.isdigit() and anim_counts[anim] > 1
                                and int(idx) == anim_counts[anim] - 1)
            if is_last_of_cycle and anim in anim_first and anim_first[anim] != prev_frame:
                refs.append(anim_first[anim])
            if prev_frame:
                refs.append(prev_frame)
            got = imagegen.edit(
                "This exact character from the reference image"
                + (" (shown again in the other image(s) in different poses of "
                   "the same motion)" if len(refs) > 1 else "")
                + " — identical design, colors, proportions, face, and art "
                f"style — now in this stance: {desc}. ONE single full-body "
                "character head to toe, exactly one figure, fully transparent "
                "background, no text, no cropping of limbs." + identity,
                refs, out_png, size="1024x1536", quality=quality,
                transparent=True)
            if got.get("ok"):
                pose_files.append((pname, out_png))
                prev_frame = out_png
                if anim not in anim_first:
                    anim_first[anim] = out_png
            else:
                pose_errors.append({"name": pname, "error": got.get("error")})

        if not pose_files:
            return {"ok": False, "stage": "poses", "failed": pose_errors,
                    "reference": ref_path,
                    "error": "every pose generation failed"}

        # 3. Assemble into the standard engine contract.
        assembled = _sp.from_pose_images(
            pose_files, out_dir=str(root / ".bgate_out" / "sprites"), name=name,
            frame_size=(frame_width, frame_height), res_dir=res_dir, fps=fps)
        assembled.setdefault("failed", [])
        assembled["failed"].extend(pose_errors)
        assembled["reference"] = ref_path
        if "reference_preview" in result:
            assembled["reference_preview"] = result["reference_preview"]
        if assembled.get("ok"):
            archived = _archive_preview(assembled["sheet"], f"painted-{name}")
            if archived:
                assembled["preview"] = archived
            artifact = _register_artifact(
                name, assembled["sheet"], producer="image_sprites",
                prompt=character_prompt,
                refs=[str(ref_image)] if ref_image else [ref_path],
                metadata={"poses": poses, "frames": assembled.get("frames", {}),
                          "failed": assembled.get("failed", []),
                          "preview": archived or ""})
            if artifact:
                assembled["artifact"] = artifact
            _log("sprites", f"painted sprite set {name!r} (reference-first): "
                            f"{len(assembled['frames'])}/{len(poses)} poses"
                            + (f", {len(assembled['failed'])} FAILED" if assembled["failed"] else ""),
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

        result = {"composite": archived or str(out), "reference": ref_path,
                  "palette_drift": drift,
                  "palette_note": "advisory: >30 = color drift likely; low values "
                                  "do NOT prove identity",
                  "checklist": checklist,
                  "instruction": "LOOK at the composite. Verdict every checklist "
                                 "line explicitly. Any fail = do not land."}
        try:
            _artifacts.record_check(
                _root(), candidate_path, "consistency", result)
        except Exception:
            pass
        return result
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
