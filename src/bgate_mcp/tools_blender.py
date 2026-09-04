"""Blender / 3D MCP tools - carved out of server.py, verbatim.

server.py held ~226 tools in 12k lines; the domains that never
touch each other now live apart. The contract is unchanged: the
shared plumbing (_tool, _root, the gates) stays in server, each
domain imports it back, and server star-imports this module at its
BOTTOM - by then its globals all exist, which is what makes the
circular import legal - so server.<tool> still answers for every
caller and test.
"""
from bgate_adapters import animcurves as _animcurves
from bgate_adapters import blender as _blender
from bgate_adapters import bonepaths as _bonepaths
from bgate_adapters import skinweights as _skinweights
from typing import Annotated

from pydantic import Field

from bgate_mcp.server import (  # noqa: F401
    Optional, _Path, _archive_preview, _contained_path,
    _fail, _json, _log, _provider_gate,
    _register_artifact, _root, _run_tag, _sprites,
    _tool,
)

# ---------------------------------------------------------------------------
# Blender
# ---------------------------------------------------------------------------
@_tool
def blender_status() -> dict:
    """Is Blender available to this machine, and which version? Check before modeling.

    Also reports `generate`: whether image-to-3D is reachable, and from where.
    Folded in here rather than given its own tool - one question ("what can I
    build with?") should cost one call.
    """
    probe = _blender.available()
    out = {**probe, **(_blender.version() if probe["available"] else {})}
    out["generate"] = _imageto3d_summary()
    return out


def _imageto3d_summary() -> dict:
    """A few lines, not the whole catalogue.

    imageto3d.status() carries every backend's full licence prose - right for
    a doctor row a human reads once, far too expensive to hand an agent on
    every status call. Names what is usable and why the rest is not, and
    leaves the reading to blender_generate's own failure.
    """
    try:
        from bgate_adapters import imageto3d as _i3d
    except Exception:
        return {"available": False, "reason": "adapter unavailable"}
    # PASS THE ROOT, OR HOSTED BACKENDS LIE ABOUT THEIR KEYS.
    #
    # imageto3d.api_key() only loads the project's .env when it is given a root
    # (`if root:`), so status() with no root reads a bare os.environ. On a
    # machine whose keys live in the project .env - which is where the setup
    # docs put them - every hosted backend then reports "<KEY> not set".
    #
    # Measured: a project with a working KREA_API_KEY had image_status report the
    # krea leg AVAILABLE while blender_status reported krea BLOCKED for want of
    # the same key, in the same session. image_status was right by accident - it
    # calls _root() first, which loads the .env as a side effect. The 2D and 3D
    # legs disagreeing about one key sent a user hunting for a key they already
    # had, and hid a paid image-to-3D backend they were entitled to use.
    try:
        root = _root()
    except Exception:
        # No project resolvable - a legitimate answer, not an error. Fall back to
        # the bare environment, which is what this did for every call before.
        root = None
    try:
        full = _i3d.status(root)
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    gpu = full.get("gpu") or {}
    usable = list(full.get("usable") or [])
    blocked = {b["backend"]: b.get("reason", "")
               for b in full.get("backends") or []
               if not b.get("available") and b.get("implemented")}
    # SPLIT HOSTED FROM LOCAL, BECAUSE `usable` MEANS CONFIGURED, NOT RUNNING.
    #
    # This flattened status() to one list and dropped the local/hosted split the
    # adapter had already computed. ["hunyuan-local", "krea", "trellis-cpp"] gave
    # no hint that two of those need a server the user has to start and one is a
    # hosted API that needs only its key - and probe=False, which is the default
    # here for the good reason that a status call must not block on a TCP
    # timeout, means a local row says "usable" while nothing is listening.
    #
    # THE COST, MEASURED: an agent tried the two local backends, got connection
    # refused from both, and reported image-to-3D unavailable. That was relayed
    # upward as fact and the whole image-to-3D path was written off for a
    # session. Krea was hosted, its key was set, and it had already produced
    # every texture in the same build. One ambiguous list did that.
    local = list(full.get("local") or [])
    hosted = list(full.get("hosted") or [])
    clear = list(full.get("unconditional_licence") or [])
    hint = ""
    if local and not hosted:
        hint = ("every usable backend here is LOCAL, which means CONFIGURED, not "
                "running - probe one before concluding anything, and do not "
                "report image-to-3D unavailable on a refused connection alone.")
    elif hosted:
        hint = (f"hosted backends ({', '.join(hosted)}) need no local server - "
                "they are reachable now. If a local one refuses a connection, "
                "try a hosted one before reporting the path unavailable.")
    return {"available": bool(usable), "usable": usable,
            # Kept alongside `usable` rather than replacing it: existing callers
            # read that key, and a status field that silently changes shape is
            # its own version of this bug.
            "local": local, "hosted": hosted,
            "unconditional_licence": clear,
            "gpu": gpu.get("name", ""), "vram_gb": gpu.get("vram_gb"),
            "blocked": blocked,
            "checked": "configuration only - a local backend listed usable may "
                       "still have no server running",
            "note": ("nothing configured - see .env.example; a generated mesh "
                     "is a DRAFT and still has to be cleaned, scaled, oriented "
                     "and rigged before it is an asset")
            if not usable else
            ("a generated mesh is a DRAFT: clean, scale, orient and rig it. "
             + hint).strip()}


@_tool
def blender_run(script: str, blend_file: Optional[str] = None, render: bool = False,
                engine: str = "BLENDER_WORKBENCH", timeout: int = 180,
                label: str = "", kit: bool = True) -> dict:
    """Run a bpy script in headless Blender and get the scene back as facts.

    `bpy` is already imported. Returns per-object tri/vert counts (evaluated, so
    modifiers count), UV warnings, materials, your print() output, and - with
    render=True - a PNG of the active camera view (archived to the project's
    preview gallery; give a `label` so humans can tell renders apart).

    THE MODELLING KIT IS ALREADY THERE (kit=True, the default). Do not write your
    own material/UV/hygiene helpers - an agent burned 33 KB and most of an hour
    doing exactly that on the first real character run. Available:
      bg_help()                      PRINTS A COMPLETE WORKED LAYER SCRIPT - a
                                     humanoid built from one head-height, a
                                     named rig with roll, the checks, bg_finish
                                     last. Read it before writing your first one.
      bg_wipe()                      empty the scene (no default cube)
      bg_box/bg_cyl/bg_ball/bg_plane named primitives
      bg_mirror/bg_smooth/bg_taper   symmetry, subsurf, limb taper
      bg_join(objs, name)            one layer should leave as ONE mesh
      bg_clean(obj)                  doubles/loose/degenerate/normals - THIS is
                                     what makes automatic weighting work later
      bg_unwrap(obj)                 smart-project UVs (no UVs = no texture)
      bg_mat(obj, name, rgb)         a BLOCKING-IN colour, not a shipped surface
      bg_bone_chain(name, bones)     an armature with NAMED bones. Entries are
                                     (name, head, tail, parent=None, roll_deg=0);
                                     order does not matter, parents are wired in
                                     a second pass, and ROLL IS IN DEGREES - set
                                     it on limbs or a humanoid retarget gives you
                                     the twisted-forearm look.
      bg_finish(obj, colour=...)     clean + apply + unwrap + material, in order
      bg_stats(obj)                  verts/faces/loose/nonmanifold/ngons/flipped
                                     PLUS world-space dims/centre/min/max
      bg_bounds(obj)                 world-space min/max/dims/centre, in metres
      bg_flipped(obj)                how many faces point INWARD (count, measured
                                     on a throwaway copy - the mesh is untouched)
      bg_overlap(a, b)               do two layers' world bounds intersect, and
                                     by how much. Layers are built in isolated
                                     scenes, so "is the cap sunk into the head"
                                     is a question NOTHING else in the pipeline
                                     can ask until they are already combined.

    bg_bone_chain RAISES - deliberately, and it is the only thing in the kit that
    does. Everything else swallows its problems because a helper that raises
    takes the whole run down; a rig cannot afford that trade, because a wrong rig
    looks built and comes apart in the engine several steps later. It refuses: a
    parent no bone in the list defines (which used to produce silent parentless
    roots), a duplicate bone name, head == tail (Blender DELETES zero-length
    bones on leaving edit mode and says nothing, so the bone simply is not in the
    armature you get back), and a name Blender had to rename or truncate (bind=
    'bone:Head' then matches nothing in blender_combine). Every message names the
    bone. Read the message and fix the chain - do not wrap it in a try.

    START A BODY FROM THE BASE MESH LIBRARY, NOT FROM PRIMITIVES. Same kit, same
    namespace, no import:
      bg_human(height=1.8, heads=7.5, build, limbs, shoulders, detail,
               pose="t"|"a", convention="godot"|"blender", rig=True)
      bg_quadruped(...) / bg_prop_frame(...)
                                     each returns {"obj","rig","marks","props",
                                     "convention","pose"} - a correctly
                                     proportioned, closed, unwrapped,
                                     weight-ready body with a NAMED skeleton.
      bg_proportions(...)            45 measurements out of one number
      bg_mark(base, "head_top")      one landmark: position, radius, girth.
                                     RAISES on a name that is not there.
      bg_fit(obj, mark, mode="at"|"on"|"around"|"in", clearance, scale)
                                     places AND resizes a layer onto a landmark
      bg_shell / bg_human_chain / bg_human_skeleton / bg_roll
      bg_bone(base, "hand.R")        the real bone name (RAISES on an unknown
                                     role); BG_BONE_NAMES carries Godot's
                                     SkeletonProfileHumanoid spelling by default
      bg_weight(obj, rig)            binds AND counts what stayed unweighted
      bg_base_report / bg_base_assert  the base's own self-check (assert RAISES)
      bg_base_help()                 prints BG_BASE_EXAMPLE, the worked script
      BG_UNIT="metre", BG_HUMAN_HEIGHT=1.8, BG_GROUND=0.0, BG_FORWARD=(0,1,0),
      BG_LEFT=(-1,0,0), BG_SIDES - the base FACES +Y, which the glTF exporter
                                     turns into -Z, which is what Godot calls
                                     forward. Author faces, visors and emblems
                                     on the +Y side; the figure's own left is -X.
      bg_unit_check / bg_unit_assert (RAISES) / bg_rescale

    FIT LAYERS ONTO LANDMARKS INSTEAD OF GUESSING COORDINATES. MEASURED: a cap
    placed with bg_fit(cap, bg_mark(base, "head_top"), "on") rests on the crown
    at 10% overlap; the same cap at a hand-typed 1.7 m is 89% INSIDE the skull
    and passed every check the old pipeline had. The honest limit - the base has
    no face and no fingers. It is a correctly-proportioned blockout to build the
    character ONTO, not a finished character.

    Pass kit=False only for a script that must run against bare bpy.

    A broken script is a normal result with ok=False plus the traceback, so read
    the result and iterate rather than assuming it worked. engine:
    BLENDER_WORKBENCH (fast preview) | BLENDER_EEVEE_NEXT | CYCLES.
    """
    try:
        _contained_path(blend_file, "blend_file")
        # Per-call render directory. The adapter always writes <out_dir>/render.png,
        # so a shared out_dir means the second seat rendering at the same moment
        # overwrites the first seat's frame at the very path the first call just
        # returned - silent, and it looks like the render simply came out wrong.
        out_dir = str(_Path(_root()) / ".bgate_out" / "renders" / _run_tag(label))
    except Exception:
        out_dir = None  # modeling before project_init is allowed
    try:
        result = _blender.run_script(script, blend_file=blend_file, render=render,
                                     out_dir=out_dir, engine=engine, timeout=timeout,
                                     kit=kit)
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


@_tool
def blender_warmup(engine: str = "BLENDER_EEVEE_NEXT") -> dict:
    """Pay the GPU cold-start cost up front. Run once per machine boot.

    A GPU engine's first render after a cold boot can take MINUTES of shader
    warmup (then ~1-2s forever after). Call this at pipeline start so no agent's
    real render is the one that stalls. Not needed for BLENDER_WORKBENCH.
    """
    try:
        out_dir = str(_Path(_root()) / ".bgate_out" / "renders" / _run_tag("warmup"))
    except Exception:
        out_dir = None
    try:
        return _blender.warmup(engine, out_dir=out_dir)
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_scene_stats(blend_file: str) -> dict:
    """Report an existing .blend without modifying it - objects, tris, materials."""
    _contained_path(blend_file, "blend_file")
    return _blender.scene_stats(blend_file)


@_tool
def blender_export_gltf(out_path: str, blend_file: Optional[str] = None,
                        script: str = "pass", timeout: int = 240) -> dict:
    """Export a .blend (or a bpy-script-built scene) to .glb for Godot.

    Modifiers are APPLIED on export - Blender defaults that off, which silently
    ships the base mesh and makes an asset look right in Blender and wrong in the
    engine. Also returns game-readiness issues (no UVs, n-gons, unapplied scale)
    worth fixing before the asset reaches a level. Pair with godot_import_asset.
    """
    _contained_path(out_path, "out_path")
    _contained_path(blend_file, "blend_file")
    return _blender.export_gltf(out_path, blend_file=blend_file,
                                script=script, timeout=timeout)


def _register_assembly(result: dict, out_path: str, *, root_name: str,
                       rig: str, producer: str) -> Optional[dict]:
    """Put an assembled .glb on the artifact ledger. One shape, two callers.

    blender_combine and blender_layer_rerun produce the SAME asset by the same
    route, so they must register it the same way - one logical name means a
    re-run is revision N+1 of the character rather than a second unrelated one,
    which is the whole reason the QA gate can see a 3D asset at all.
    """
    layers = result.get("parts") or []
    return _register_artifact(
        root_name or _Path(out_path).stem, out_path, producer=producer,
        metadata={"layers": [layer.get("name", "") for layer in layers],
                  "sources": [layer.get("source", "") for layer in layers],
                  "armature": result.get("armature", ""),
                  "rig": rig,
                  "checks": result.get("checks") or [],
                  "warnings": result.get("warnings") or [],
                  "manifest": result.get("manifest", ""),
                  "tris": sum(int(layer.get("tris") or 0) for layer in layers)})


@_tool
def blender_combine(parts: list, out_path: str, rig: str = "",
                    root_name: str = "Assembled", timeout: int = 300) -> dict:
    """Assemble separately-modelled LAYERS into one rigged .glb, and test it.

    `parts` is the layer list, each a path or {"path", "name", "at",
    "rotate", "scale", "bind": deform | bone:<Name> | none, "decal_on":
    <layer>}. A logo or any text goes in as its own layer with decal_on. `rig`
    names the layer holding the armature - without it nothing binds. Returns
    per-layer objects/tris/binding plus `checks` (`unbound`,
    `unweighted_verts`) naming the layer to re-run. REGISTERED as a candidate
    artifact - write out_path inside the project or it cannot be recorded.
    Full notes: docs/tools.md#blender_combine
    """
    _contained_path(out_path, "out_path")
    result = _blender.combine(parts, out_path, rig=rig,
                              root_name=root_name, timeout=timeout)
    if result.get("ok"):
        layers = result.get("parts") or []
        artifact = _register_assembly(result, out_path, root_name=root_name,
                                      rig=rig, producer="blender_combine")
        if artifact:
            result["artifact"] = artifact
            result["artifact_id"] = artifact["id"]
        _log("blender", f"assembled {root_name!r} from {len(layers)} layers",
             ref=str(out_path))
    return result


def _route_if_billing(result: dict, root=None) -> dict:
    """Attach the provider board to a STAGE failure that is about the account.

    server._fail already does this for a billing-shaped EXCEPTION, and that is
    where the observed "no credit, therefore the pipeline is closed, therefore
    I will hand-roll it" turn was being caught. A staged pipeline never gets
    there: character() returns {"ok": False, "stage": "plate", ...} with the
    provider's 429 as a STRING, so the redirect that exists for every other
    paid tool was missing from the one tool whose whole purpose is to stop an
    agent modelling a character by hand. MEASURED on catnip-fiend: the plate
    hit a drained openai account with krea keyed and kie holding 3,446
    credits, and the agent went looking for the answer rather than being
    handed it.
    """
    try:
        from bgate_core.runtime import gateway as _gateway

        errors = [str(result.get("error") or "")]
        errors += [str(s.get("error") or "") for s in result.get("steps") or []]
        if any(_gateway.is_billing_error(e) for e in errors if e):
            result.setdefault("route", _gateway.billing_note(root))
    except Exception:
        pass
    return result


@_tool
def character_generate(prompt: Annotated[str, Field(description='What the character looks like; the pose clause and template conditioning are added.')], out_dir: Annotated[str, Field(description="Directory every stage's artifacts are written into.")], name: Annotated[str, Field(description='Stem for the plate, mesh and rigged files. Default character.')] = "character",
                       provider: Annotated[str, Field(description='Image provider for the plate; "" uses the project\'s routing.')] = "", backend: Annotated[str, Field(description='Image-to-3D backend; "" asks choose(), which refuses licence-conditioned backends - name one after reading its terms.')] = "",
                       height: Annotated[float, Field(description='Character height in metres the mesh is scaled to. Default 1.8.')] = 1.8, budget: Annotated[int, Field(description='Target face count after decimation. Default 45000.')] = 45000,
                       size: Annotated[str, Field(description='Plate image size WxH. Default 1024x1536.')] = "1024x1536", godot_project: Annotated[str, Field(description='Directory holding project.godot; set it to import, wire and load the result in-engine. Empty writes nothing into a game.')] = "",
                       dry_run: Annotated[bool, Field(description='True (default) quotes the backend and stops; False spends real money at the plate and the mesh.')] = True, timeout: Annotated[int, Field(description='Seconds for the whole chain. Default 2400.')] = 2400,
                       ref_images: Annotated[list[str], Field(description='Local images of the SUBJECT - the pinned concept ref. Without one the pose template says which STANCE but not which character, so every run invents a different figure. Passed ahead of the template.')] = [],
                       ref_strength: Annotated[float, Field(description='How hard the subject reference is held, 0..1. Default 0.6.')] = 0.6) -> dict:
    """"I want a model that looks like X." Plate, mesh, rig, into the engine.

    THE WHOLE CHARACTER PATH AS ONE CALL; each stage gates the next. DRY_RUN
    IS TRUE BY DEFAULT: it quotes the backend and stops - pass dry_run=False
    to spend. backend "" asks choose(), which REFUSES a backend whose licence
    carries conditions; name one after reading its terms. godot_project set:
    the rigged .glb is imported, given a body and collider, wired into a
    .tscn and loaded in-engine. `ok` is True only if a RIGGED character came
    out; `stage` names where it stopped.
    Full notes: docs/tools.md#character_generate
    """
    # The decorator injects project_dir on every tool and _root() reads it, so
    # keys and spend land in the project the CALL named rather than whatever a
    # previous call left behind.
    try:
        _contained_path(out_dir, "out_dir")
        _contained_path(godot_project, "godot_project")
        root = _root()
        refused = _provider_gate(root, "image", "a character build")
        if refused:
            return refused
    except Exception:
        root = None
    try:
        return _route_if_billing(_blender.character(
            prompt, out_dir, name=name, provider=provider, backend=backend,
            height=height, budget=budget, size=size,
            godot_project=godot_project, root=root, dry_run=dry_run,
            timeout=timeout, ref_images=list(ref_images or []),
            ref_strength=ref_strength), root)
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_humanoid_template() -> dict:
    """The shipped humanoid skeleton and the pose plate to generate against.

    START A CHARACTER HERE. Conditioning the PLATE on this reference makes the
    art conform to the skeleton, so a clip authored for one character plays on
    the next. Returns the reference image to pass as `ref_images` to
    image_generate, the prompt clause that holds the stance, and the 23
    Godot-profile bone names. Path: image_generate -> key it ->
    blender_generate -> blender_rig -> godot_deliver_asset.
    Full notes: docs/tools.md#blender_humanoid_template
    """
    return _blender.humanoid_template()


@_tool
def blender_rig(model: Annotated[str, Field(description='The generated mesh (.glb/.gltf/.blend) to adopt and bind.')], out_path: Annotated[str, Field(description='Where the rigged .glb is written; keep it inside the project.')], kind: Annotated[str, Field(description='humanoid (reads a front from foot reach) or none (refuses to guess; orientation never established). Default humanoid.')] = "humanoid",
                height: Annotated[float, Field(description='Height in metres the mesh is scaled to. Default 1.8.')] = 1.8, budget: Annotated[int, Field(description='Post-decimation face count; 0 leaves density alone. 45-60k was clean, 8k shattered a character.')] = 0, orient: Annotated[bool, Field(description='Face the mesh +Y and ground it before binding. Default True.')] = True,
                armature_name: Annotated[str, Field(description='Name of the armature object written. Default Skeleton.')] = "Skeleton", symmetrize: Annotated[str, Field(description="auto mirrors skin weights only when the body's sides are within 2% of height; off skips; force runs on an asymmetric body.")] = "auto",
                timeout: Annotated[int, Field(description='Seconds for the Blender session. Default 900.')] = 900) -> dict:
    """Take a GENERATED mesh to a bound, weighted character an engine can move.

    Adopts the mesh, fits a skeleton to its measured height, binds it, and
    PROVES the bind with `unweighted`; `rigged` False is a refusal. kind:
    "humanoid" reads a front from foot reach; "none" refuses to guess. budget
    0 leaves density alone; 45-60k faces was clean. symmetrize: auto (mirror
    weights only when the body is symmetric) | off | force. Read
    `audit.shells` first - a fragmented mesh guarantees a bad bind. Then run
    blender_flex; `rigged: true` is still not "animatable".
    Full notes: docs/tools.md#blender_rig
    """
    _contained_path(out_path, "out_path")
    result = _blender.rig(model, out_path, kind=kind, height=height,
                          budget=budget, orient=orient,
                          armature_name=armature_name,
                          symmetrize=symmetrize, timeout=timeout)
    if result.get("ok"):
        coverage = result.get("coverage") or {}
        note = ""
        if coverage:
            note = (" [coverage OK]" if coverage.get("passed")
                    else f" [coverage MISSING {coverage.get('missing')}]")
        _log("blender",
             f"rigged {model} -> {result.get('bound_with')} "
             f"({result.get('unweighted_pct')}% unweighted){note}",
             ref=str(out_path))
    return result


@_tool
def blender_flex(model: str, out_dir: str = "", stem: str = "flex",
                 render: bool = True, engine: str = "BLENDER_WORKBENCH",
                 volume_tolerance: float = 0.18, pinch_tolerance: float = 0.60,
                 timeout: int = 600) -> dict:
    """Bend a rigged character and report what bending it did to the body.

    THE SECOND HALF OF THE RIG PROOF: zero unweighted vertices says nothing
    about whether an elbow survives being bent. Poses each joint ONE AT A
    TIME and measures volume_ratio (a good bind costs 2-6%), worst_pinch (0.6
    is a visible waist, under 0.4 a straw), new_self_pairs (the increase, not
    the count) and a render - LOOK AT IT. `verdict.passed` False is a refusal:
    raise `budget` on the rig, check `audit.shells`, re-rig with
    symmetrize='force' when only one side failed.
    Full notes: docs/tools.md#blender_flex
    """
    _contained_path(out_dir, "out_dir")
    result = _blender.flex(model, out_dir, stem=stem, render=render,
                           engine=engine,
                           volume_tolerance=volume_tolerance,
                           pinch_tolerance=pinch_tolerance,
                           timeout=timeout)
    verdict = result.get("verdict") or {}
    if result.get("ok"):
        _log("blender",
             f"flexed {model} -> "
             f"{'passed' if verdict.get('passed') else 'FAILED'} "
             f"({len(verdict.get('issues') or [])} issues over "
             f"{verdict.get('checked', 0)} poses)",
             ref=str(model))
    return result


@_tool
def blender_weights(model: str, threshold: float = 0.02,
                    min_bleed_vertices: int = 3, timeout: int = 300) -> dict:
    """Per deform bone, does its weight paint cover one patch of the mesh or two.

    A THIRD RIG PROOF: bleed - a hand painted partly to Spine by a stroke
    that crossed empty viewport - has full coverage and may pass flex, yet
    tears the moment the parts pose differently. Flags a bone whose paint
    makes MORE connected components than the mesh pieces it touches.
    `threshold` (0.02) is the minimum weight that counts; `min_bleed_vertices`
    (3) is the noise floor. `verdict.passed` False names the bones; also
    False when nothing could be measured (`checked: 0`).
    Full notes: docs/tools.md#blender_weights
    """
    report = _blender.weight_islands(model, threshold=threshold, timeout=timeout)
    if report.get("ok"):
        verdict = _blender.weight_islands_verdict(
            report, min_bleed_vertices=min_bleed_vertices)
        report["verdict"] = verdict
        _log("blender",
             f"weight-islands {model} -> "
             f"{'passed' if verdict.get('passed') else 'FAILED'} "
             f"({len(verdict.get('issues') or [])} bleeding bones over "
             f"{verdict.get('checked', 0)} checked)",
             ref=str(model))
    return report


@_tool
def blender_template_deviation(model: str, reference: str = "",
                               max_deviation: float = 0.08,
                               timeout: int = 300) -> dict:
    """How far a rigged character's joints sit from the shipped humanoid template.

    A FOURTH RIG PROOF: a bone can be weighted, pinch-free and bleed-free and
    still sit in the wrong place. Compares bone LENGTHS (as fractions of body
    height, so poses and sizes do not matter) and parent links against
    HUMANOID_SKELETON or a supplied `reference`. NOT a weight comparison.
    `max_deviation` (0.08 body-heights) is a gross-error line, not a fidelity
    one. `verdict.passed` False names mis-proportioned or misparented bones;
    also False when nothing could be compared (`checked: 0`).
    Full notes: docs/tools.md#blender_template_deviation
    """
    report = _blender.template_deviation(
        model, reference=(reference or None), timeout=timeout)
    if report.get("ok"):
        verdict = _blender.template_deviation_verdict(
            report, max_deviation=max_deviation)
        report["verdict"] = verdict
        _log("blender",
             f"template-deviation {model} -> "
             f"{'passed' if verdict.get('passed') else 'FAILED'} "
             f"({len(verdict.get('issues') or [])} displaced bones over "
             f"{verdict.get('checked', 0)} checked)",
             ref=str(model))
    return report


@_tool
def blender_silhouette(model: str, min_ratio: float = 0.15,
                       max_ratio: float = 4.0, timeout: int = 600) -> dict:
    """The character's projected 2D outline across flex's own pose sweep.

    EXPERIMENTAL. Catches what volume and pinch cannot: a limb that folds
    behind the torso and vanishes on screen, or a shoulder that balloons
    without losing volume. Projects flex's sweep through flex's fixed camera
    and measures convex-hull area. `verdict.passed` False means the silhouette
    nearly vanished (min_ratio) or ballooned (max_ratio) - or that the sweep
    proved nothing: every pose skipped, or every pose identical to rest,
    which is what an unbound mesh does.
    Full notes: docs/tools.md#blender_silhouette
    """
    report = _blender.silhouette(model, timeout=timeout)
    if report.get("ok"):
        verdict = _blender.silhouette_verdict(
            report, min_ratio=min_ratio, max_ratio=max_ratio)
        report["verdict"] = verdict
        _log("blender",
             f"silhouette {model} -> "
             f"{'passed' if verdict.get('passed') else 'FAILED'} "
             f"({len(verdict.get('issues') or [])} issues over "
             f"{verdict.get('checked', 0)} poses)",
             ref=str(model))
    return report


@_tool
def animation_curves(model: Annotated[str, Field(description='The exported .glb whose animation channels are read.')], foot_bones: Annotated[Optional[list[str]], Field(description='Channel node names (exact match) that also get the foot_skate check.')] = None,
                     ground_axis: Annotated[int, Field(description='Index of the up axis in the file (0=x, 1=y, 2=z). Default 1.')] = 1, max_cruising_fraction: Annotated[float, Field(description='velocity_profile fails above this share of the clip spent near peak speed. Default 0.6.')] = 0.6,
                     min_sparc: Annotated[float, Field(description='sparc fails below this spectral arc length. Default -8.0; a starting point, not validated on stylized clips.')] = -8.0, max_skating_frames: Annotated[int, Field(description='foot_skate fails past this many sliding frames. Default 0.')] = 0,
                     check_anticipation: Annotated[bool, Field(description='Run the EXPERIMENTAL anticipation detector. Default True; set False to skip it.')] = True,
                     min_anticipation_width: Annotated[float, Field(description='Minimum curvature spread (frames) that counts as a shaped transition. Default 6.0.')] = 6.0,
                     max_burst_ratio: Annotated[float, Field(description='concentration fails above this multiple of even pacing. Default 3.0.')] = 3.0) -> dict:
    """Measure an exported animation clip's curves - no Blender/Godot needed.

    Parses a GLB's channels directly and reports per channel: arc_deviation
    (descriptive only), velocity_profile (share of duration near peak speed -
    the linear-keyframe signature), concentration (share of travel in the
    fastest tenth of frames; a snap runs 4x and up), sparc (smoothness;
    threshold borrowed from gait research, treat FAILs as worth a look), and
    anticipation (EXPERIMENTAL; check_anticipation=False skips it).
    `foot_bones` additionally get foot_skate. A pass means "no obvious
    curve-math defect", not "looks good".
    Full notes: docs/tools.md#animation_curves
    """
    data = _animcurves.extract_animations(model)
    if not data.get("ok"):
        return data
    feet = set(foot_bones or [])
    clips = []
    for anim in data["animations"]:
        channels = []
        for ch in anim["channels"]:
            times, values = ch["times"], ch["values"]
            entry = {"node": ch["node"], "path": ch["path"],
                     "interpolation": ch["interpolation"], "samples": len(times)}
            if len(times) >= 2:
                profile = _animcurves.velocity_profile(times, values)
                entry["velocity"] = {
                    "peak_speed": profile["peak_speed"],
                    "cruising_fraction": profile["cruising_fraction"],
                    "verdict": _animcurves.velocity_profile_verdict(
                        profile, max_cruising_fraction=max_cruising_fraction)}
                sp = _animcurves.sparc(times, values)
                entry["sparc"] = {**sp, "verdict": _animcurves.sparc_verdict(
                    sp, min_sparc=min_sparc)}
                burst = _animcurves.motion_concentration(times, values)
                entry["concentration"] = {
                    **burst,
                    "verdict": _animcurves.motion_concentration_verdict(
                        burst, max_burst_ratio=max_burst_ratio)}
                if check_anticipation:
                    axis_values = (list(zip(*values)) if values
                                  and isinstance(values[0], (tuple, list))
                                  else [values])
                    issues = []
                    events = 0
                    for axis in axis_values:
                        av = _animcurves.anticipation_verdict(
                            times, list(axis),
                            min_width_samples=min_anticipation_width)
                        issues.extend(av["issues"])
                        events += av["events"]
                    entry["anticipation"] = {
                        "verdict": {"passed": not issues, "issues": issues},
                        "events": events}
                if ch["path"] == "translation":
                    entry["arc"] = _animcurves.arc_deviation(times, values)
                    if ch["node"] in feet:
                        skate = _animcurves.foot_skate(
                            times, values, ground_axis=ground_axis)
                        entry["foot_skate"] = {
                            **skate, "verdict": _animcurves.foot_skate_verdict(
                                skate, max_skating_frames=max_skating_frames)}
            channels.append(entry)
        # A BONE NOBODY ANIMATED IS NOT A BONE WITH BAD CURVES, and until this
        # split existed the tool said otherwise about every one of them. The
        # metrics below each refuse rather than pass when they cannot measure
        # - correctly, an unknown is not a clean bill of health - and a real
        # humanoid clip animates a dozen bones and leaves the other ten
        # holding two constant keys. Folding those refusals into `failed`
        # flagged all 23 bones of all six clips on both characters of the
        # project this was written against: a tool that flags everything says
        # nothing, and its headline `passed` could never be True on any rig.
        #
        # So a channel is FLAGGED only for a defect that was actually seen,
        # and the refusals are counted separately rather than swallowed -
        # `unmeasured_channels` is how a caller still learns that most of this
        # clip could not be judged.
        def _defects(entry: dict) -> list[str]:
            out = []
            for metric in ("velocity", "sparc", "concentration",
                           "foot_skate", "anticipation"):
                verdict = (entry.get(metric) or {}).get("verdict") or {}
                if verdict.get("passed", True):
                    continue
                if all(i.get("kind") == "unmeasured"
                       for i in verdict.get("issues") or [{}]):
                    continue
                out.append(metric)
            return out

        failed, unmeasured = [], 0
        for c in channels:
            hits = _defects(c)
            if hits:
                failed.append(c["node"])
            elif any((c.get(m) or {}).get("measured") is False
                     for m in ("sparc", "concentration")):
                unmeasured += 1
        measured = [c for c in channels if "velocity" in c]
        clips.append({"name": anim["name"], "channels": channels,
                     "measured_channels": len(measured),
                     "unmeasured_channels": unmeasured,
                     "passed": bool(measured) and not failed,
                     "flagged_bones": sorted(set(failed))})
    # A FILE WITH NO CLIPS IS NOT A FILE WITH CLEAN CLIPS. `failed` never
    # evaluates on an empty channel list, so every aggregate here reported
    # a pass for a model carrying no animation at all - which is exactly
    # what blender_rig hands back, and exactly the file an agent reaches
    # for this tool with.
    if not clips:
        return {"ok": False, "clips": [],
                "error": f"{model} contains no animations - nothing was "
                         "measured. A rigged-but-unanimated export (what "
                         "blender_rig produces) has no curves to judge; "
                         "animate or bake a clip into it first."}
    _log("blender", f"animation-curves {model} -> "
         f"{sum(1 for c in clips if c['passed'])}/{len(clips)} clips clean",
         ref=str(model))
    return {"ok": True, "clips": clips}


@_tool
def skin_dominance(model: str, max_ratio: float = 3.0,
                   max_rigid_fraction: float = 0.50,
                   flag_dead_bones: bool = False,
                   min_weight: float = 0.5, sample: int = 0) -> dict:
    """Is each vertex driven by a bone that is anywhere NEAR it - no Blender needed.

    A FIFTH RIG PROOF, catching the character that tears in motion while every
    other gate passes (legs weighted to the spine). Per vertex: distance to
    its dominant bone SEGMENT over distance to the nearest deform bone. THE
    MEDIAN IS 1.00 ON BROKEN RIGS TOO - the verdict reads the maximum
    (max_ratio) and the rigid share (max_rigid_fraction). `flag_dead_bones`
    is off by default. `verdict.passed` False names the bones; an unrigged
    file refuses rather than passing empty.
    Full notes: docs/tools.md#skin_dominance
    """
    path = _contained_path(model, "model")
    report = _skinweights.dominance(path, min_weight=min_weight, sample=sample)
    if report.get("measured"):
        verdict = _skinweights.dominance_verdict(
            report, max_ratio=max_ratio,
            max_rigid_fraction=max_rigid_fraction,
            flag_dead_bones=flag_dead_bones)
        report["verdict"] = verdict
        _log("blender",
             f"skin-dominance {model} -> "
             f"{'passed' if verdict.get('passed') else 'FAILED'} "
             f"(max ratio {report.get('max_ratio')}, "
             f"{report.get('rigid_fraction', 0.0):.0%} rigid, "
             f"{len(verdict.get('issues') or [])} issues)",
             ref=str(model))
    return report


@_tool
def blender_texture(model: Annotated[str, Field(description='The layer (.glb/.gltf/.blend) whose material gets the maps.')], image: Annotated[str, Field(description='Albedo / base colour map (sRGB). "" applies the other maps without changing the base colour.')], out_path: Annotated[str, Field(description='Where the re-exported layer is written; keep it inside the project so it can be registered.')], material: Annotated[str, Field(description='The ONE material slot to paint. Effectively required on a model with more than one; a name matching no slot fails.')] = "",
                    all_slots: Annotated[bool, Field(description='Explicit opt-in to paint every material slot. Default False.')] = False, roughness: Annotated[str, Field(description='Roughness map path (Non-Color); how glossy, per texel.')] = "",
                    metallic: Annotated[str, Field(description='Metallic map path (Non-Color); 0 dielectric, 1 metal.')] = "", normal: Annotated[str, Field(description='Tangent-space normal map path (Non-Color).')] = "", emission: Annotated[str, Field(description='Emission map path (sRGB); what glows.')] = "",
                    normal_strength: Annotated[float, Field(description='Scales the Normal Map node. Default 1.0.')] = 1.0, alpha: Annotated[str, Field(description='auto | opaque | clip | blend. auto picks clip only when the base image actually carries transparent pixels; a decal needs clip.')] = "auto",
                    alpha_cutoff: Annotated[float, Field(description='Threshold for alphaMode MASK when alpha is clip. Default 0.5.')] = 0.5,
                    backface_cull: Annotated[Optional[bool], Field(description='Force backface culling on or off; omitted, decal decides.')] = None, decal: Annotated[bool, Field(description='Shorthand for a conformed graphic: implies backface culling. Default False.')] = False,
                    timeout: Annotated[int, Field(description='Seconds for the Blender session. Default 240.')] = 240) -> dict:
    """Put GENERATED maps on a 3D layer's material and re-export it.

    Generate maps with image_generate(task_kind="texture"); apply per layer
    before blender_combine. `image` is the albedo; roughness / metallic /
    normal / emission each drive their own BSDF input. image="" applies maps
    without changing the base colour. alpha: auto | opaque | clip | blend - a
    decal NEEDS clip. `material` names ONE slot and is effectively required
    on a multi-material model; all_slots=True is the explicit opt-in. Meshes
    with no UVs are unwrapped first. Registered as a candidate artifact -
    write out_path inside the project.
    Full notes: docs/tools.md#blender_texture
    """
    _contained_path(out_path, "out_path")
    maps = {"roughness": roughness, "metallic": metallic,
            "normal": normal, "emission": emission}
    result = _blender.apply_texture(
        model, image or None, out_path, material=material,
        all_slots=all_slots,
        **{kind: (path or None) for kind, path in maps.items()},
        normal_strength=normal_strength, alpha=alpha,
        alpha_cutoff=alpha_cutoff, backface_cull=backface_cull,
        decal=decal, timeout=timeout)
    if result.get("ok"):
        given = {kind: str(path) for kind, path in
                 {"base_color": image, **maps}.items() if path}
        artifact = _register_artifact(
            _Path(out_path).stem, out_path, producer="blender_texture",
            refs=list(given.values()),
            metadata={"model": str(model), "texture": str(image),
                      "material": material, "all_slots": bool(all_slots),
                      "maps": given, "decal": bool(decal),
                      # The mode the adapter RESOLVED, not the one asked
                      # for: `auto` is the common call and the answer it
                      # reached is what decides alphaMode in the glTF.
                      "alpha": result.get("alpha") or alpha,
                      "alpha_cutoff": alpha_cutoff,
                      "textured": result.get("textured") or [],
                      "unwrapped": result.get("unwrapped") or []})
        if artifact:
            result["artifact"] = artifact
            result["artifact_id"] = artifact["id"]
        _log("blender", f"textured {_Path(out_path).name} with "
                        f"{len(given)} map(s)", ref=str(out_path))
    return result


def _turnaround_frames(result: dict) -> list[str]:
    """The frame files this turnaround actually wrote, for the image blocks."""
    return [frame["path"] for frame in (result.get("renders") or [])
            if isinstance(frame, dict) and frame.get("exists") and frame.get("path")]


@_tool(images=_turnaround_frames)
def blender_turnaround(model: str, out_dir: str, stem: str = "turnaround",
                       width: int = 640, height: int = 960,
                       engine: str = "BLENDER_EEVEE_NEXT",
                       exposure: float = 0.0, timeout: int = 480) -> dict:
    """Render a model from four angles under a fixed rig - and JUDGE each frame.

    THE FRAMES COME BACK IN THIS RESULT AS IMAGES. Camera and three-point
    lighting scale to the subject's bounding box. Every frame returns
    `blown`/`mean` and a verdict; `ok` is False when any frame is unreadable -
    a lighting problem, not a modelling one. Each frame is archived and
    REGISTERED as a candidate artifact (`artifact_id`); point out_dir INSIDE
    the project or frames cannot be registered.
    Full notes: docs/tools.md#blender_turnaround
    """
    _contained_path(out_dir, "out_dir")
    result = _blender.turnaround(model, out_dir, stem=stem,
                                 size=(width, height), engine=engine,
                                 exposure=exposure, timeout=timeout)
    frames = [f for f in (result.get("renders") or []) if isinstance(f, dict)]
    registered = []
    for frame in frames:
        path = frame.get("path")
        if not path or not frame.get("exists"):
            continue
        label = str(frame.get("label") or "frame")
        archived = _archive_preview(path, f"{stem}-{label}")
        if archived:
            frame["preview"] = archived
        # One logical name PER ANGLE: a re-render after fixing the lights is
        # revision 2 of "hero-front", not a second unrelated artifact, which
        # is what lets a reviewer see that the white one was superseded.
        artifact = _register_artifact(
            f"{stem}-{label}", path, producer="blender_turnaround",
            metadata={"model": str(model), "angle": label,
                      "degrees": frame.get("degrees"),
                      "engine": engine, "exposure": exposure,
                      "blown": frame.get("blown"), "mean": frame.get("mean"),
                      "readable": bool(frame.get("ok")),
                      "verdict": frame.get("verdict") or "",
                      "preview": archived or ""})
        if artifact:
            frame["artifact"] = artifact
            frame["artifact_id"] = artifact["id"]
            registered.append(artifact["id"])
    if registered:
        result["artifact_ids"] = registered
    elif frames:
        result["artifact_note"] = (
            "no artifact was registered for these frames - out_dir is "
            "outside the project root, so art QA and the dashboard cannot "
            "see them; re-render into the project to put them on the ledger")
    if frames:
        unreadable = len(result.get("unreadable") or [])
        _log("render", f"turnaround {stem!r}: {len(frames)} frames"
                       + (f", {unreadable} unreadable" if unreadable else ""),
             ref=str(out_dir))
    return result


@_tool
def blender_generate(image: str, out_path: str, backend: str = "",
                     label: str = "", timeout: int = 900,
                     dry_run: bool = False, parts: bool = False,
                     options: Optional[dict] = None) -> dict:
    """Turn ONE generated image into a draft mesh. The other way to get geometry.

    WHAT COMES BACK IS A DRAFT, NOT AN ASSET: it goes through blender_rig /
    blender_combine, never straight to godot_deliver_asset. Nothing runs
    until a backend is configured (blender_status); a hosted one is priced
    first and `dry_run=True` returns the quote plus the licence verdict.
    LICENCE IS PART OF THE RESULT - read it. parts=True asks for a body in
    PIECES (better for characters): `out_path` becomes a DIRECTORY and the
    result carries `parts` and a `combine` list; needs
    BGATE_COMFY_PARTS_WORKFLOW and says so rather than falling back.
    Full notes: docs/tools.md#blender_generate
    """
    try:
        _contained_path(out_path, "out_path")
        from bgate_adapters import imageto3d as _i3d
    except Exception as exc:
        return _fail(exc)
    try:
        root = _root()
    except Exception:
        root = None                        # modelling before project_init is allowed
    try:
        plate = _i3d.check_input(image)
        if not plate.get("ok"):
            return {"ok": False, "error": plate.get("reason", "unusable plate"),
                    "input": plate}
        picked = backend or ("comfy-parts" if parts else
                             (_i3d.choose(root) or {}).get("backend", ""))
        if parts and not _i3d.supports(picked, "parts"):
            return {"ok": False,
                    "error": f"backend {picked!r} does not generate parts - "
                             "the part-aware path needs a graph exported to "
                             "BGATE_COMFY_PARTS_WORKFLOW that saves each part "
                             "separately",
                    "capabilities": _i3d.capabilities(picked)}
        if not picked:
            return {"ok": False, "error": "no image-to-3D backend is configured "
                    " - see .env.example; blender_status reports what is reachable",
                    "status": _imageto3d_summary()}
        opts = dict(options or {})
        quote = {"backend": picked,
                 "usd": _i3d.price_for(picked, **{k: v for k, v in opts.items()
                                                  if k in ("texture", "quad", "rig")}),
                 "licence": _i3d.model_licence(_i3d.declared_model())}
        if dry_run:
            return {"ok": True, "dry_run": True, "quote": quote,
                    "input": plate, "next_steps": list(_i3d.NEXT_STEPS)}
        if parts:
            got = _i3d.generate_parts(image, out_path, backend=picked,
                                      root=root, timeout=float(timeout),
                                      logical_name=label, **opts)
            got.setdefault("quote", quote)
            # EVERY PART REGISTERED, not just the first. A part left
            # unregistered is invisible to the dashboard and to art QA, and an
            # unreviewed limb is exactly the one that ships wrong.
            if got.get("ok") and root:
                registered = []
                for part in got.get("parts") or []:
                    try:
                        registered.append(_register_artifact(
                            root, part["path"],
                            f"{label or _Path(out_path).name}_{part['name']}",
                            producer="blender_generate", refs=[str(image)],
                            metadata={"backend": picked, "draft": True,
                                      "part": part["name"],
                                      "licence": got.get("licence")
                                                 or quote["licence"],
                                      "plate": str(image)}))
                    except Exception:
                        pass
                got["artifacts"] = registered
            return got
        got = _i3d.generate(image, out_path, backend=picked, root=root,
                            timeout=float(timeout), logical_name=label,
                            **opts)
        got.setdefault("quote", quote)
        # generate() names the written file `path`, the same key every other
        # adapter here returns. This asked for `out_path` - the name of THIS
        # function's argument, never a key on the result - so the guard was
        # always false and the mesh landed on disk unregistered: invisible to
        # the dashboard and to art QA, which is the one failure a generated
        # draft must not have.
        if got.get("ok") and root and got.get("path"):
            try:
                got["artifact"] = _register_artifact(
                    root, got["path"], label or _Path(out_path).stem,
                    producer="blender_generate", refs=[str(image)],
                    metadata={"backend": picked, "draft": True,
                              "licence": got.get("licence") or quote["licence"],
                              "plate": str(image)})
            except Exception:
                pass                       # a mesh on disk beats a bookkeeping raise
        return got
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_sweep(out_path: str, dry_run: bool = True,
                  keep_renders: bool = True) -> dict:
    """Delete a finished asset's intermediate layer files, keeping the record.

    Removes the layer sources listed in that asset's manifest and NOTHING
    ELSE. Kept: the assembled file, its manifest, the renders. What was
    removed is written back into the manifest so a layer can still be
    rebuilt (blender_layer_rerun). Defaults to dry_run=True - look at the
    list, then call again with dry_run=False.
    Full notes: docs/tools.md#blender_sweep
    """
    _contained_path(out_path, "out_path")
    return _blender.sweep(out_path, dry_run=dry_run,
                          keep_renders=keep_renders)


def _manifest_layers(asset: str) -> dict:
    """The assembled manifest's per-layer record, by name. {} if unreadable.

    Read BEFORE re-assembling: combine rewrites the manifest at the same path,
    so the tri counts and object lists a re-run is compared against exist only
    until the moment it succeeds.
    """
    try:
        doc = _json.loads(_blender.manifest_path(asset).read_text(encoding="utf-8"))
        return {str(layer.get("name", "")): layer
                for layer in (doc.get("layers") or [])}
    except Exception:
        return {}


@_tool
def blender_layer_rerun(asset: str, layer: str, script: str = "",
                        source: str = "", kit: bool = True,
                        out_path: str = "", timeout: int = 300) -> dict:
    """Rebuild ONE layer of an assembled asset and re-assemble it. Not the
    character - the layer.

    `asset` is the ASSEMBLED .glb (manifest beside it); `layer` the name
    blender_combine reported. Then ONE of: `script` (bpy source, recorded
    beside the layer), `source` (a file you built), or neither (the RECORDED
    script is re-run - the recovery path after blender_sweep). Placement,
    binding and the rig layer come off the manifest. Refuses when another
    layer's source is missing. Registered as revision N+1; returns the combine
    result plus `changed`.
    Full notes: docs/tools.md#blender_layer_rerun
    """
    _contained_path(out_path, "out_path")
    recipe = _blender.manifest_recipe(asset)
    parts = [dict(part) for part in recipe.get("parts") or []]
    names = [str(part.get("name", "")) for part in parts]
    index = next((i for i, name in enumerate(names) if name == layer), -1)
    if index < 0:
        return {"ok": False, "error": (
            f"{layer!r} is not a layer of {_Path(asset).name} - this asset's "
            f"layers are: {', '.join(n for n in names if n) or 'none'}")}
    target = parts[index]
    before = _manifest_layers(asset).get(layer, {})
    recorded = {entry.get("name", ""): entry
                for entry in recipe.get("missing") or []}

    # 1. Every OTHER layer has to be on disk, or the assembly quietly loses
    #    it - combine assembles happily around the hole and hands back a
    #    character with no arms, ok=True. Refuse FIRST, before a rebuild
    #    spends minutes in Blender on an assembly that cannot happen, and
    #    say which of the missing ones still carry a script.
    gone = [part for i, part in enumerate(parts)
            if i != index and not _Path(str(part.get("path") or "")).is_file()]
    if gone:
        return {"ok": False, "error": (
            "cannot re-assemble: "
            + "; ".join(
                f"layer {part.get('name')!r} has no file at "
                f"{part.get('path')} ("
                + ("its script is in the manifest - re-run it too"
                   if recorded.get(part.get("name", ""), {}).get("script")
                   else "and the manifest recorded no script for it")
                + ")" for part in gone))}

    # 2. Put the layer's file back, by whichever of the three routes applies.
    built: dict = {}
    if source:
        replacement = _Path(source)
        if not replacement.is_file():
            return {"ok": False, "error": f"no such layer file: {source}"}
        target["path"] = str(replacement.resolve())
        rebuilt = "file"
    else:
        text = script or (recorded.get(layer, {}).get("script")
                          or _blender.read_layer_record(
                              target.get("path", "")).get("script", ""))
        if text:
            built = _blender.run_script(text, export_glb=target["path"],
                                        kit=kit, timeout=timeout)
            if not built.get("ok"):
                return {**built, "ok": False, "layer": layer,
                        "stage": "layer",
                        "error": built.get("error")
                                 or f"the script for layer {layer!r} failed"}
            rebuilt = "script"
        elif _Path(target.get("path", "")).is_file():
            rebuilt = "reused"
        else:
            return {"ok": False, "error": (
                f"layer {layer!r} has no file at {target.get('path')!r} and "
                "the manifest recorded no script for it - pass script= to "
                "rebuild it, or source= to point at a file you already have")}

    out = str(out_path or asset)
    # The SAME name the first assembly used, so the re-run supersedes it
    # rather than sitting beside it as an unrelated asset.
    root_name = recipe.get("root_name", "") or _Path(asset).stem
    result = _blender.combine(parts, out, rig=recipe.get("rig", ""),
                              root_name=root_name, timeout=timeout)
    after = next((part for part in (result.get("parts") or [])
                  if part.get("name") == layer), {})
    result.update({
        "layer": layer, "rebuilt": rebuilt, "source": target.get("path", ""),
        "asset": out,
        "layer_run": {k: built.get(k) for k in ("ok", "seconds", "print")
                      if k in built},
        "changed": {
            "tris_before": before.get("tris"), "tris_after": after.get("tris"),
            "objects_before": before.get("objects") or [],
            "objects_after": after.get("objects") or [],
            "bound_before": before.get("bound"),
            "bound_after": after.get("bound"),
        },
        "reassembled": [name for name in names if name],
    })
    if result.get("ok"):
        artifact = _register_assembly(
            result, out, root_name=root_name, rig=recipe.get("rig", ""),
            producer="blender_layer_rerun")
        if artifact:
            result["artifact"] = artifact
            result["artifact_id"] = artifact["id"]
        _log("blender", f"re-ran layer {layer!r} ({rebuilt}) and re-assembled "
                        f"{_Path(out).name}", ref=out)
    return result


@_tool
def blender_sprites(base_script: Annotated[str, Field(description='bpy source that builds the model, lights and (optionally) camera once.')], poses: Annotated[list[dict], Field(description='[{"name", "script"}]; each script tweaks the scene and renders one frame named after the pose.')], name: Annotated[str, Field(description='Sheet name; emits <name>_sheet.png and <name>_frames.tres. Default sprite.')] = "sprite",
                    width: Annotated[int, Field(description='Frame width in px. Default 128.')] = 128, height: Annotated[int, Field(description='Frame height in px. Default 128.')] = 128,
                    engine: Annotated[str, Field(description='Render engine: BLENDER_EEVEE_NEXT (default), BLENDER_WORKBENCH or CYCLES.')] = "BLENDER_EEVEE_NEXT", fps: Annotated[float, Field(description='Playback speed written into the SpriteFrames. Default 8.')] = 8.0,
                    res_dir: Annotated[str, Field(description='res:// directory the sheet is meant to import under. Default assets/sprites.')] = "assets/sprites", out_dir: Annotated[Optional[str], Field(description="Where the PNGs and sheet are written; omitted uses the project's sprite output directory.")] = None,
                    timeout: Annotated[int, Field(description='Seconds for the whole render. Default 420.')] = 420) -> dict:
    """Render a Blender-built character as a transparent 2D sprite set.

    Build the model once in base_script (bpy; lights included, an auto-framed
    ORTHO camera is added if missing), then each pose in poses=[{"name",
    "script"}] tweaks the scene and renders one frame. Output: per-pose PNGs
    + <name>_sheet.png + <name>_frames.tres, ready for godot_import_asset
    into res_dir. A pose script that errors fails only that pose - check
    `failed`. The sheet is archived to the preview gallery.
    Full notes: docs/tools.md#blender_sprites
    """
    try:
        _contained_path(out_dir, "out_dir")
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
                          "engine": engine, "preview": archived or "",
                          "fps": fps,
                          "animations": result.get("animations", {}),
                          "sequence": result.get("sequence")})
            if artifact:
                result["artifact"] = artifact
            _log("sprites", f"rendered {len(result['frames'])} sprite frames "
                            f"for {name!r}" +
                            (f" ({len(result['failed'])} failed)" if result["failed"] else ""),
                 ref=result["sheet"])
        return result
    except Exception as exc:
        return _fail(exc)




@_tool
def animation_contacts(model: Annotated[str, Field(description='The exported .glb whose skeleton and clip are evaluated.')], feet: Annotated[Optional[list[str]], Field(description="Exact contact joint names (LeftFoot/RightFoot on this project's humanoids, four paws on a quadruped); omitted, guessed and reported.")] = None,
                       gait: Annotated[Optional[str], Field(description='What the clip was MEANT to be: walk, run, stand, any. No default - the support verdict refuses without it.')] = None,
                       clip: Annotated[Optional[str], Field(description='Animation name to evaluate when the file carries several; omitted, the first.')] = None,
                       ground_axis: Annotated[int, Field(description='Index of the up axis (0=x, 1=y, 2=z). Default 1.')] = 1,
                       band_fraction: Annotated[float, Field(description="Height band above a foot's lowest point that counts as down, as a fraction of its vertical travel. Default 0.25.")] = 0.25,
                       max_slide: Annotated[float, Field(description='Planted-foot ground-plane speed allowed on a ROOT-MOTION clip, metres per frame. Default 0.02.')] = 0.02,
                       max_variation: Annotated[float, Field(description="Allowed variation of the planted foot's slide speed on an IN-PLACE clip, as a fraction. Default 0.2.")] = 0.20,
                       floor: Annotated[Optional[float], Field(description="The real floor height (usually 0.0); omitted, clearance is judged against each foot's own resting contact.")] = None) -> dict:
    """Where a character's feet ACTUALLY are, frame by frame - the question
    animation_curves structurally cannot answer.

    Forward kinematics off the file, then: support (feet down per frame, and
    FLIGHT - judged only against a DECLARED `gait`; undeclared returns the
    measurement with a refusal), contact (planted-foot speed judged by the
    clip's detected convention: root-motion holds still, in-place slides
    steadily), clearance (below `floor`, or below the foot's own rest by
    default). `feet` names the joints exactly; omitted, guessed and reported.
    `gait`: walk, run, stand, any - no default.
    Full notes: docs/tools.md#animation_contacts
    """
    src = _Path(model)
    paths = _bonepaths.joint_paths(src, clip=clip)
    if not paths.get("ok"):
        return {"ok": False, "error": paths.get("reason", "no trajectories")}
    height = paths.get("model_height")
    guessed = False
    clips = []
    for entry in paths["clips"]:
        if not entry.get("measured"):
            clips.append({"name": entry["name"], "measured": False,
                          "reason": entry.get("reason")})
            continue
        positions = entry["positions"]
        names = feet
        if not names:
            names = [n for n in positions
                     if any(tag in n.lower()
                            for tag in ("foot", "feet", "paw", "toe"))]
            guessed = True
        names = [n for n in names if n in positions]
        if not names:
            clips.append({"name": entry["name"], "measured": False,
                          "reason": ("no contact joints named or found in "
                                     f"{sorted(positions)[:8]} - pass `feet`")})
            continue
        support = _bonepaths.support_phases(
            {n: positions[n] for n in names}, entry["times"],
            up_axis=ground_axis, band_fraction=band_fraction,
            model_height=height)
        # JUDGE FIRST, THEN TRIM. The per-frame counts are the raw trace and
        # can run to hundreds of entries, so they do not belong in a tool
        # result — but the "stand" verdict reads them, and popping them first
        # turned every standing clip into a tool error.
        support_verdict = _bonepaths.support_verdict(support, gait)
        support.pop("counts", None)
        per_foot = {}
        for name in names:
            slide = _bonepaths.contact_slide(
                positions[name], entry["times"],
                root_motion=entry["root_motion"], up_axis=ground_axis,
                band_fraction=band_fraction, model_height=height)
            clear = _bonepaths.ground_clearance(
                positions[name], up_axis=ground_axis, floor=floor)
            per_foot[name] = {
                "contact": {**slide, "verdict":
                            _bonepaths.contact_slide_verdict(
                                slide, max_slide=max_slide,
                                max_variation=max_variation)},
                "clearance": {**clear, "verdict":
                              _bonepaths.ground_clearance_verdict(clear)},
            }
        # DEFECTS SEEN, NOT QUESTIONS UNANSWERED — the same split animation
        # _curves needed. A standing clip's foot is correctly planted and
        # correctly not receding, so its contact trace is unmeasurable rather
        # than faulty; folding that into the flag list marked both feet of
        # every idle in the project as defective.
        def _real(verdict: dict) -> bool:
            if verdict.get("passed", True):
                return False
            return not all(i.get("kind") == "unmeasured"
                           for i in verdict.get("issues") or [{}])

        failed = [n for n, f in per_foot.items()
                  if _real(f["contact"]["verdict"])
                  or _real(f["clearance"]["verdict"])]
        unmeasured = sorted(n for n, f in per_foot.items()
                            if n not in failed
                            and f["contact"].get("measured") is False)
        clips.append({
            "name": entry["name"], "measured": True,
            "frames": len(entry["times"]),
            "convention": "root_motion" if entry["root_motion"] else "in_place",
            "root_travel": entry["root_travel"],
            "support": {**support, "verdict": support_verdict},
            "feet": per_foot,
            "flagged_feet": sorted(failed),
            "unmeasured_feet": unmeasured,
            "passed": support_verdict.get("passed", False) and not failed,
        })
    _log("blender", f"animation-contacts {model} -> "
         f"{sum(1 for c in clips if c.get('passed'))}/{len(clips)} clips clean",
         ref=str(model))
    return {"ok": True, "model_height": height, "gait": gait,
            "feet_guessed": guessed, "clips": clips}


@_tool
def animation_library(pack: Annotated[str, Field(description='One pack to detail (its clips, bone map coverage); empty lists every pack.')] = "") -> dict:
    """Which hand-keyed CC0 clip packs are fetched, and what is in them.

    A pack's clips ride into blender_animate as {"clip": "Walk_Loop",
    "name": "walk"} (pack defaults to quaternius-ual) and are RETARGETED onto
    the rig - animator-keyed motion instead of procedural. This tool never
    downloads: a missing pack is fetched by the OWNER with the printed
    command (`bgate animlib fetch <pack>`, commit-pinned, SHA-256 checked),
    the same rule that keeps key-writing out of an agent's hands.
    Full notes: docs/tools.md#animation_library
    """
    from bgate_adapters import animlib as _animlib
    if not pack:
        return _animlib.status()
    resolved = _animlib.resolve(pack)
    if not resolved.get("ok"):
        return resolved
    return {"ok": True, "pack": pack, "license": resolved["license"],
            "clips": sorted(resolved["clips"].values(), key=lambda c: c["name"]),
            "mapped_bones": sorted(set(resolved["bone_map"].values())),
            "unmapped_source_bones": resolved["unmapped"],
            "how": ('blender_animate(model, out_path, clips=[{"clip": "Walk_Loop", '
                    '"name": "walk"}, {"kind": "idle"}]) - library and procedural '
                    'clips mix in one call')}


def _proof_sheet_paths(result: dict) -> list[str]:
    """The per-clip proof sheets this run wrote, for the image blocks."""
    return [s["path"] for s in (result.get("sheets") or [])
            if isinstance(s, dict) and s.get("path")]


@_tool(images=_proof_sheet_paths)
def blender_animate(model: Annotated[str, Field(description='The RIGGED humanoid .glb (what blender_rig wrote) to author clips on.')],
                    out_path: Annotated[str, Field(description='Where the animated .glb is written; keep it inside the project.')],
                    clips: Annotated[Optional[list[dict]], Field(description='[{"name", "kind", ...}]. kind: idle | walk | run | sneak | crouch_idle | pickup | look_around | wave | hit | jump | keyed, OR a LIBRARY clip {"clip": "Walk_Loop", "name": "walk", "pack": "quaternius-ual"} retargeted from a fetched CC0 pack (animation_library lists them) - prefer these where the pack has the motion. Omitted: procedural idle, walk, run, crouch_idle, pickup, look_around. A "keyed" clip carries {"keys": [{"t": s, "lean": deg, "hips_up": m, "reach_r": deg, ...}], "loop": bool} in CHARACTER terms - never bone rotations. Gaits take "overrides" ({"stride": 0.4, "lean": 10, ...}, fractions of leg length where they are distances).')] = None,
                    fps: Annotated[int, Field(description='Keys per second. Default 30.')] = 30,
                    out_dir: Annotated[str, Field(description='Where the proof frames and sheets go. Default: anim_proof/ beside out_path.')] = "",
                    stem: Annotated[str, Field(description='File stem for the proof images. Default proof.')] = "proof",
                    proof_frames: Annotated[int, Field(description='Frames rendered per clip per view for the proof sheet; 0 renders nothing. Default 6.')] = 6,
                    facing: Annotated[str, Field(description="What to do when the skin's toes and the skeleton's foot bones disagree about forward: check (refuse, the default), repair (re-aim the foot bones to the skin), skeleton (trust the bones).")] = "check",
                    textured: Annotated[bool, Field(description='Proof frames show the material; False renders a clay figure, which reads deformation better. Default True.')] = True,
                    loop_suffix: Annotated[bool, Field(description="Name looping clips '<name>-loop' so Godot's importer marks them looping on import. Default False.")] = False,
                    orient: Annotated[bool, Field(description="Turn a character whose measured forward is -Y to the pipeline's +Y (Godot's -Z) before authoring, so the game does not play it backwards. Default True.")] = True,
                    timeout: Annotated[int, Field(description='Seconds for the Blender session. Default 900.')] = 900) -> dict:
    """Put gameplay clips on a rigged humanoid, export the .glb, and SHOW them.

    The animation layer the 3D path was missing: walk, run, idle, crouch,
    pickup and the rest are AUTHORED ON THE RIG IT IS GIVEN - forward, left,
    leg length and hip height are measured off the bones, feet are solved by
    IK so they stay where they are put, the spine bends cumulatively and the
    arms counter-swing. Do not write a bpy pose script by hand; it was done
    once and every clip walked backwards with every gate green. THE PROOF
    SHEETS COME BACK AS IMAGES - look at them; `support` is the foot-contact
    gate per clip judged against what the clip was MEANT to be. `refused`
    True is the facing gate: the skin and the skeleton disagree about which
    way is forward, and `error` says how to fix it rather than override it.
    Full notes: docs/tools.md#blender_animate
    """
    _contained_path(out_path, "out_path")
    if out_dir:
        _contained_path(out_dir, "out_dir")
    result = _blender.animate(model, out_path, clips=clips, fps=fps,
                              out_dir=out_dir, stem=stem,
                              proof_frames=proof_frames, facing=facing,
                              textured=textured, loop_suffix=loop_suffix,
                              orient=orient, timeout=timeout)
    registered = []
    for sheet in (result.get("sheets") or []):
        path = sheet.get("path")
        if not path:
            continue
        label = f"{stem}-{sheet.get('clip', 'clip')}"
        archived = _archive_preview(path, label)
        if archived:
            sheet["preview"] = archived
        artifact = _register_artifact(
            label, path, producer="blender_animate",
            metadata={"model": str(model), "clip": sheet.get("clip"),
                      "out_path": str(out_path), "preview": archived or ""})
        if artifact:
            sheet["artifact_id"] = artifact["id"]
            registered.append(artifact["id"])
    if registered:
        result["artifact_ids"] = registered
    if result.get("ok"):
        made = [c["action"] for c in (result.get("clips") or []) if c.get("ok")]
        failed = (result.get("support") or {}).get("failed") or []
        _log("blender",
             f"animated {model} -> {len(made)} clips ({', '.join(made)})"
             + (f"; support FAILED on {', '.join(failed)}" if failed else ""),
             ref=str(out_path))
    elif result.get("refused"):
        _log("blender", f"blender_animate REFUSED {model}: skin and skeleton "
                        "disagree about forward", ref=str(model))
    return result
