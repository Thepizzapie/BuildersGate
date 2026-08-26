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
from bgate_mcp.server import (  # noqa: F401
    Optional, _Path, _archive_preview, _contained_path,
    _fail, _json, _log, _paid_gate,
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

    The end of the layered 3D path: model body, clothing, hard accessories and
    any logo as their own files, then join them here. Built in ONE pass instead,
    a figure comes back with the parts that lost the attention budget deformed - on a real baseball player, the hands, the cap, and a scrambled team logo.

    `parts` is the layer list, each a path or a dict:
      {"path": "out/uniform.glb",   # .glb / .gltf / .blend
       "name": "uniform",           # how it is reported and referenced
       "at": [0,0,0], "rotate": [0,0,0], "scale": 1.0,
       "bind": "deform",            # deform | bone:<Name> | none
       "decal_on": "cap"}           # conform to that layer's surface

    A LOGO OR ANY TEXT GOES IN AS ITS OWN LAYER WITH decal_on. Flush against the
    surface it z-fights and tears in-engine; shrinkwrap plus an offset fixes it.
    Hard geometry rides a bone (a cap does not bend), soft geometry deforms.
    `rig` names the layer holding the armature - without it nothing binds, which
    is right for a prop and a shipped statue for a character.

    Returns per-layer objects/tris/binding, plus `checks`: `unbound` and
    `unweighted_verts` name the layer that detaches or tears the first time it
    animates, so you re-run that layer instead of the whole character.

    The assembled file is REGISTERED as a candidate artifact (`artifact_id`),
    which is what puts it under the same QA gate every 2D asset passes through.
    Write out_path inside the project - an artifact cannot be recorded for a
    file outside it, and an unregistered asset is one no reviewer ever sees.
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


@_tool
def character_generate(prompt: str, out_dir: str, name: str = "character",
                       provider: str = "", backend: str = "",
                       height: float = 1.8, budget: int = 45000,
                       size: str = "1024x1536", godot_project: str = "",
                       dry_run: bool = True, timeout: int = 2400) -> dict:
    """"I want a model that looks like X." Plate, mesh, rig, into the engine.

    THE WHOLE CHARACTER PATH AS ONE CALL. Every stage was already reachable and
    a caller still had to know: condition the plate on the humanoid template or
    the skeleton will not fit; key it or the backdrop arrives as geometry no
    bone can reach; which backend takes which knobs; that a bind reports success
    having weighted nothing. Get any of those wrong and it costs ten GPU minutes
    to find out. They are the same five steps in the same order every time.

    Each stage gates the next, so a failure costs the stage that found it.
    Measured on the runs this was built from - an unkeyed plate took 605 s and
    came back 21% non-manifold, refused by the quality gate, against 216 s and
    16% for the same subject keyed; a collapse met its triangle budget with
    20,799 of 39,803 faces inside out; a bind created all 22 vertex groups and
    filled NONE, 64,878 of 64,878 vertices carrying no weight with every other
    check green.

    DRY_RUN IS TRUE BY DEFAULT. It quotes the backend and stops. This spends
    real money at the plate and again at the mesh, and a tool that bills on the
    first call is a tool nobody trusts twice - pass dry_run=False to run it.

    backend   "" asks choose(), which REFUSES to pick a backend whose licence
              carries conditions. That refusal is the design: this tool does not
              know your revenue, territory or monthly actives. Name one after
              reading its terms.
    godot_project  set it and the rigged .glb is imported, given a body and
              collider suited to what it is, wired into a .tscn and loaded
              through the engine to prove it opens. Leave it empty and nothing
              is written into a game project.

    Returns every artifact by path, the gate result from each stage, and `stage`
    naming where it stopped. `ok` is True only if a RIGGED character came out - a mesh that failed to bind reports ok=False with the unweighted count, and
    that is a refusal, not a warning.
    """
    # The decorator injects project_dir on every tool and _root() reads it, so
    # keys and spend land in the project the CALL named rather than whatever a
    # previous call left behind.
    try:
        _contained_path(out_dir, "out_dir")
        _contained_path(godot_project, "godot_project")
        root = _root()
        refused = _paid_gate(root, "image", 0.0, "a character build")
        if refused:
            return refused
    except Exception:
        root = None
    try:
        return _blender.character(
            prompt, out_dir, name=name, provider=provider, backend=backend,
            height=height, budget=budget, size=size,
            godot_project=godot_project, root=root, dry_run=dry_run,
            timeout=timeout)
    except Exception as exc:
        return _fail(exc)


@_tool
def blender_humanoid_template() -> dict:
    """The shipped humanoid skeleton and the pose plate to generate against.

    START A CHARACTER HERE. Every generated mesh used to invent its own
    proportions, so the skeleton had to be bent to fit each one and no two
    characters could share an animation. Conditioning the PLATE on this
    reference inverts that - the art conforms to the skeleton, and a clip
    authored for one character plays on the next.

    Measured on one character, bones further than 6 cm from any mesh vertex:
      template scaled by height only ............ 16 of 24
      landmark fitting alone ..................... 5 of 23
      plate conditioned on this reference alone .. 8 of 23
      BOTH ....................................... 0 of 23, and 0 unweighted

    Returns the reference image to pass as `ref_images` to image_generate, the
    prompt clause that holds the stance, and the 23 Godot-profile bone names
    every humanoid from this pipeline carries - so BoneMap retargeting works
    and animations move between characters.

    The five-step path:
      1. image_generate(prompt + pose_clause, ref_images=[pose_front])
      2. key it - an opaque plate becomes geometry, measured 2.8x slower and
         21% non-manifold against 16% keyed
      3. blender_generate(plate, out)          draft mesh
      4. blender_rig(mesh, out)                adopt, fit, bind, PROVE it
      5. godot_deliver_asset(project, rigged)  .tscn, verified in-engine
    """
    return _blender.humanoid_template()


@_tool
def blender_rig(model: str, out_path: str, kind: str = "humanoid",
                height: float = 1.8, budget: int = 0, orient: bool = True,
                armature_name: str = "Skeleton", symmetrize: str = "auto",
                timeout: int = 900) -> dict:
    """Take a GENERATED mesh to a bound, weighted character an engine can move.

    Every image-to-3D backend returns `rigged: false` - geometry and nothing
    else. This is the missing step between that and a character: adopt the mesh
    (weld, decimate, scale, orient, ground), fit a skeleton to its own measured
    height, bind it, and PROVE the bind took.

    THE PROOF IS `unweighted`, AND NOTHING CHEAPER WORKS. Blender's parent_set
    returns cleanly, creates all 22 vertex groups, and can leave every one of
    them empty. The modifier attaches. Godot loads it and shows a Skeleton3D.
    The character animates not at all. MEASURED on a real generation: 64,878 of
    64,878 vertices carrying no weight with every other check green.

    Adopt and bind happen in ONE Blender session on purpose. Round-tripping
    through a file between them is what produced that failure: glTF re-import
    carries a root transform, the skeleton lands in a different space from the
    mesh, and heat finds no vertices near any bone. Same mesh in one session:
    3 of 19,556.

    Bone heat is tried first because it deforms properly; ARMATURE_ENVELOPE is
    the fallback and is rigid, so elbows and shoulders pinch. `bound_with` says
    which one shipped. **`rigged` False means the asset is not animatable** - it is not a warning to pass along, it is a refusal.

    kind    "humanoid" reads a front from foot reach; "none" refuses to guess.
            A subject with no feet (a prop, a bust) wants "none", and then
            orientation is NEVER ESTABLISHED - check the turnaround yourself.
    budget  0 leaves the density alone. A local backend with no face_count knob
            hands back ~280k faces, and post-decimation here is the only lever
            those users have. 8k shattered a character; 45-60k was clean.

    symmetrize  "auto" (default) mirrors the skin weights across the body's own
            centre plane, but ONLY when the audit says the two sides are within
            2% of the character's height of each other. Heat fails differently
            on each side - one clean elbow and one bound to the ribs is the
            normal outcome - and averaging the pair fixes it without picking a
            winner. "off" skips it. "force" runs it on an asymmetric body, which
            is right for a cosmetic asymmetry (one pauldron, a cloak) and wrong
            for anything else.

    THE REPORT NOW CARRIES `audit` BEFORE THE BIND, and it is the part worth
    reading first. `audit.shells` is the fragmentation count - a real user's
    character arrived as 940 separate shells, which passes every
    well-formedness gate and guarantees a bad bind, because heat will not cross
    the gaps and loose islands weight to whichever bone is nearest.
    `audit.symmetry.mean` is how far the body is from its own mirror image.

    AND `rigged: true` IS STILL NOT "ANIMATABLE". Run blender_flex on the
    output: it bends the thing and measures what bending it did.

    `coverage` (kind="humanoid" only) is a fast pre-check for the 15 bone
    names godot_retarget_check calls essential - Hips, the spine/head chain,
    both arms, both legs, under the EXACT name a BoneMap-free retarget
    matches by. It cannot see hierarchy or binding, only naming, so a pass
    here is not a substitute for retarget_check against the real engine - it just means a naming problem shows up now instead of after the Godot
    round-trip.
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

    THE SECOND HALF OF THE RIG PROOF. `blender_rig` answers "were weights
    written" with the unweighted count, and that is the only thing it can
    answer. It says NOTHING about whether the elbow survives being bent, and a
    rig with zero unweighted vertices routinely collapses a joint to a straw,
    loses a quarter of its volume in one bend, or drives the forearm through the
    ribs. Every number stays green while the character animates like a bag of
    spanners. Run this before you deliver one.

    Poses each joint a walk cycle moves, ONE AT A TIME so a failure is
    diagnosable, and per pose measures:

      volume_ratio      posed volume over rest volume. A good bind costs 2-6%.
      worst_pinch       the joint that lost the most cross-section. 1.0 is
                        rigid, 0.6 is a visible waist, under 0.4 is a straw.
      new_self_pairs    faces that intersect in this pose and did not at rest.
                        The increase, not the count - a generated mesh arrives
                        with overlapping shells and the absolute number is
                        meaningless.
      render            a PNG of the pose. LOOK AT IT. The whole lesson of this
                        pipeline is that green gates are not evidence.

    `verdict.passed` False is a refusal, not a warning: those weights are not
    animatable as they stand. The usual fixes, in order - raise `budget` on the
    rig so the joint has enough loops to bend, check `audit.shells` for a
    fragmented mesh heat could not cross, and re-run the rig with
    symmetrize='force' when only one side failed.
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

    A THIRD RIG PROOF, ALONGSIDE `blender_rig` AND `blender_flex`. Neither of
    those catches this: `rig()`'s `unweighted` count only sees vertices with
    NO weight, and `flex()` only sees a joint after it bends. Bleed is
    neither - a hand painted mostly to Hand but partly to Spine, because a
    brush stroke crossed empty space in the viewport rather than the mesh
    surface, has full weight coverage and may not even move wrong at any of
    flex's six test poses if the bleed region is small. It still reads as a
    seam-tearing glitch the moment the spine and the hand pose differently.

    Reports each deform bone's weighted vertices as connected components on
    the mesh surface, and flags a bone whose paint makes MORE components than
    the number of separate mesh pieces it touches - a split inside one
    connected piece of surface, which only a stray stroke explains. Spanning
    several pieces is not itself a fault: this pipeline assembles bodies from
    joined primitives, so a hip bone legitimately covers three of them.

    `threshold` is the minimum weight at which a vertex counts as belonging to
    a bone (0.02). `min_bleed_vertices` (3) is a noise floor - a single stray
    vertex is a cleanup nit, not the seam-tearing failure this exists to catch.

    `verdict.passed` False names which bones split and how many vertices sit
    off their own patch. It is also False when nothing could be measured - a
    bind with no weights above `threshold` reports `checked: 0` and refuses,
    rather than passing an empty result as a clean one.
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

    A FOURTH RIG PROOF. `blender_rig`, `blender_flex`, and `blender_weights`
    all ask questions about ONE character in isolation - is it bound, does it
    survive bending, is the paint contiguous. None of them can tell you the
    fit itself landed a bone somewhere anatomically wrong, because a bone
    can be fully weighted, pinch-free, and bleed-free while still sitting in
    the wrong place on the body if height/limb fitting mis-solved.

    Compares bone LENGTHS against HUMANOID_SKELETON (or a supplied
    `reference`), matched by name and each expressed as a fraction of its own
    file's body height, so two characters of different heights aren't
    penalised for that alone. Lengths rather than joint positions because the
    two skeletons are never posed alike - this pipeline rigs in an A-pose and
    the template is a T-pose, and a positional check reports that difference
    as a fault on every correctly-rigged character. Bone length does not move
    when a joint rotates. Parent links are compared too, so a rig that kept
    the 23 names but rewired the chain is caught.

    NOT a weight comparison - the reference skeleton and a generated character
    never share mesh topology, so there is nothing to diff vertex-for-vertex.

    `max_deviation` (0.08 body-heights) is a GROSS-ERROR line - a limb
    collapsed to nothing or stretched across the body - not a proportional-
    fidelity one. Fitting is meant to adapt the template to each body.

    `verdict.passed` False names which bones are mis-proportioned or
    misparented. It is also False when nothing could be compared: a candidate
    whose bones are named on another scheme entirely reports `checked: 0` and
    refuses, rather than passing an empty intersection as agreement.
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

    EXPERIMENTAL - no production rig-QA tool anywhere this project's
    research found automates a pose-sweep silhouette check; studios render
    the sweep and a human watches it. This is a real attempt at that, not
    an adopted technique.

    A DIFFERENT QUESTION FROM blender_flex. Volume and pinch are 3D
    measures against the mesh itself and cannot see a failure that only
    shows up from a CAMERA's point of view - a limb that folds directly
    behind the torso and vanishes from the silhouette while its 3D volume
    stays intact, or a shoulder that balloons on screen without losing any
    measured volume. This projects the SAME pose sweep through the SAME
    fixed, rest-fitted camera flex() uses (never refit per pose) and
    measures the projected convex-hull area.

    'Preserved' means SANITY BOUNDS, not 'unchanged' - a pose is EXPECTED
    to change how a character reads on screen. `verdict.passed` False means
    the silhouette nearly vanished (min_ratio) or ballooned far past what a
    single joint's rotation should produce (max_ratio), not that anything
    changed at all.

    It is ALSO False on a sweep that proves nothing: every pose skipped for
    want of the bones it rotates, or every pose projecting the identical
    outline as rest. The second is the important one - an unbound mesh does
    exactly that, and bounds that only fire far from 1.0 would otherwise call
    a ratio of exactly 1.0 across the whole sweep a perfect result.
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
def animation_curves(model: str, foot_bones: Optional[list[str]] = None,
                     ground_axis: int = 1, max_cruising_fraction: float = 0.6,
                     min_sparc: float = -8.0, max_skating_frames: int = 0,
                     check_anticipation: bool = True,
                     min_anticipation_width: float = 6.0,
                     max_burst_ratio: float = 3.0) -> dict:
    """Measure an exported animation clip's curves - no Blender/Godot needed.

    Reads a GLB's animation channels directly (glTF is a public format, so
    this is a plain file parse, not another headless spawn) and reports, per
    channel:

      arc_deviation      (translation only) how far the path bows from the
                         straight line between its endpoints. DESCRIPTIVE,
                         not pass/fail - an arc is right for a swinging limb
                         and wrong for a jab's extension, and this cannot
                         tell which the clip is doing.
      velocity_profile   what fraction of the clip's DURATION is spent near
                         its own peak speed. High means the motion travels
                         at near-constant speed rather than easing in/out - the curve-math signature of raw linear-interpolated
                         keyframes.
      concentration      THE OPPOSITE TAIL OF THAT SAME PROFILE, and
                         velocity_profile is blind to it: what share of a
                         track's whole travel lands in its fastest tenth of
                         frames, against what even pacing would put there.
                         1.0 is evenly paced, 1.5 a clean sine swing; a clip
                         whose entire pose change happens in two frames with
                         a drift around it runs 4x and up. A snap is about as
                         far from constant-speed as a curve gets, so it sails
                         through the check above.
      sparc              spectral arc length of the speed profile - a
                         smoothness/jitter measure from the mocap-cleanup
                         literature. Its threshold is a starting point
                         borrowed from gait research, not yet validated on
                         this project's own stylized clips - treat FAILs as
                         worth a look, not as certain defects.
      anticipation       EXPERIMENTAL, per axis. Laplacian-of-Gaussian
                         correlation looking for curvature spread across a
                         transition (shaped, eased, wound-up) vs. a narrow
                         spike (a raw interpolated corner). No prior art
                         exists for this as a detector - the cited research
                         (Wang/Xu/Cohen SIGGRAPH 2006) shows the FORWARD
                         direction, that this filter CREATES anticipation;
                         using it to detect whether anticipation is already
                         present is this project's own experiment. Also has
                         a real resolution floor: quick transitions sampled
                         at only a few frames are unreliable to call either
                         way. Set check_anticipation=False to skip it.

    `foot_bones` (channel node names, exact match) additionally get
    foot_skate: frames where the bone sits near its lowest point in the clip
    but still moves horizontally - a planted foot sliding.

    None of this measures appeal or exaggeration - nothing computational
    does. A clean pass here means "no obvious curve-math defect", not
    "looks good"; it is a floor, not a ceiling.
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

    A FIFTH RIG PROOF, and the one that catches a character which tears when
    it animates while every other gate reports it clean. Found on a shipped
    cat whose walk and run were reported as tearing, with the idle tail wag
    the only motion that read as smooth. At that moment:

      blender_rig            passed - `unweighted` was 0, every vertex had weight
      weights summed to 1.0  on every single vertex
      blender_weights        passed - the guilty bone's paint was ONE connected
                             patch; it just ran too far down the legs, and a
                             patch that is too big is not a patch that split
      blender_flex           passed - its six poses did not open the seam far
                             enough to trip the volume/pinch bounds
      blender_template_dev.  passed - the SKELETON was correct. Names, lengths
                             and parenting were all fine. Only the paint was wrong

    The defect: 42% of the vertices in the lower third of the model - the legs
    and paws - had their dominant weight on `spine`, `hips`, `chest` or `neck`.
    The worst sat at y=0.005, ON THE FLOOR, driven by a bone 0.15 m up inside
    the body. That geometry cannot follow the leg it belongs to, so the leg
    stretches away from the body as soon as the leg swings. It is invisible in
    bind pose, which is what every stand-up photograph in this pipeline
    captures, and invisible to every check above.

    Measures, per vertex, the distance to the BONE SEGMENT that dominates it
    against the distance to the nearest deform bone segment available. Segments
    rather than joint origins because a vertex halfway down a thigh is far from
    both the hip and the knee. A ratio rather than a distance because a
    tolerance in metres would need retuning per asset, which means it would not
    get run.

    ONLY DEFORM BONES ARE CANDIDATES for "nearest" - a root or an IK target
    skins nothing and is often parked at the origin, and comparing against one
    inflated this check's own first run to a 6.91x false positive.

    Defaults are set from two rigs measured in the same project, one known-good
    and one known-bad:

                        median   p95    max    rigid
        good rig          1.00   1.06   1.56      9%
        the torn cat      1.00   2.32   3.76     57%

    THE MEDIAN IS 1.00 FOR BOTH. Most vertices in a broken bind are painted
    correctly; the defect lives entirely in the tail, so any average hides it.
    That is why the verdict reads the maximum and the rigid share.

    `flag_dead_bones` is off by default: every rig legitimately carries bones
    that deform nothing, and the good rig above fails on exactly that when it
    is on. They are always listed in the report as information.

    `verdict.passed` False names the bones that reach too far and, separately,
    a bind with no falloff at all. It is False rather than True when nothing
    could be measured - an unrigged file refuses instead of passing empty.
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
def blender_texture(model: str, image: str, out_path: str, material: str = "",
                    all_slots: bool = False, roughness: str = "",
                    metallic: str = "", normal: str = "", emission: str = "",
                    normal_strength: float = 1.0, alpha: str = "auto",
                    alpha_cutoff: float = 0.5,
                    backface_cull: Optional[bool] = None, decal: bool = False,
                    timeout: int = 240) -> dict:
    """Put GENERATED maps on a 3D layer's material and re-export it.

    The surface half of the layered path. Measured on the first real character
    run: the assembled asset carried 21 materials and ZERO images - every
    surface a flat colour an agent typed by hand, because nothing connected the
    image adapter to the 3D layers. Generate the maps with image_generate
    (task_kind="texture", conditioned on the pinned refs via use_pinned), then
    apply them here, per layer, before blender_combine.

    `image` is the albedo / base colour and is what the one-image call has
    always meant. The rest are optional and each drives its own BSDF input.
    WITHOUT THEM EVERY SURFACE IS THE SAME PLASTIC - the modelling kit types
    rough=0.6, metal=0.0, so cloth, leather, skin and steel all ship as one
    dielectric and colour is the only thing that varies across an asset:
      roughness   how glossy, per texel        metallic  0 dielectric, 1 metal
      normal      tangent-space normals        emission  what glows
    Those four are DATA and are loaded Non-Color; `image` and `emission` feed
    colour sockets and stay sRGB. Pass image="" to apply maps without changing
    the base colour. normal_strength scales the Normal Map node.

    ALPHA - auto | opaque | clip | blend. MEASURED: a decal needs alpha="clip"
    to export `alphaMode: MASK`. Without it the logo layer ships as a solid
    rectangle of key colour glued over the cap, which is worse than the
    z-fighting the decal layer exists to prevent. `auto` inspects the base image
    and picks clip only when it ACTUALLY carries transparent pixels - an opaque
    PNG with an RGBA header is not a cut-out - so say clip explicitly when you
    know it is one. alpha_cutoff is the MASK threshold. decal=True is shorthand
    for a conformed graphic and implies backface culling; backface_cull
    overrides it either way.

    `material` names ONE slot. IT IS EFFECTIVELY REQUIRED on a model carrying
    more than one authored material: `all_slots=True` is the explicit opt-in
    that says you meant to paint every slot, because that used to be the DEFAULT
    and it put one image over skin, eyes and mouth and called the layer
    textured. A named material matching no slot is a failure, not a cheerful
    ok=True with an empty list. Meshes with no UVs are unwrapped first - a map
    on an unwrapped mesh is silently ignored, which looks exactly like the
    generation having failed.

    The re-exported layer is REGISTERED as a candidate artifact (`artifact_id`)
    and carries the maps it was given, so the surface a reviewer is judging can
    be traced to the images that produced it. Write out_path inside the
    project; a file outside it cannot be recorded.
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

    THE FRAMES COME BACK IN THIS RESULT AS IMAGES, not as paths you are trusted
    to go and open. Measured: four turnarounds of a correctly-coloured model
    came back white because the lights were far too hot, and were reported as
    finished without anybody opening them. The model was fine; the render was
    not, and nothing could tell the difference. Look at what you were handed,
    and read the verdicts - they are the half of the check you cannot argue with.

    Camera and three-point lighting are scaled to the subject's own bounding
    box, so a giant and a doll both frame correctly. Every frame returns a
    `blown`/`mean` reading and a verdict; `ok` is False when any frame is
    unreadable, and the verdict of the frame that failed is the `error`. A
    failing frame is a lighting problem, not a modelling one - do not go back
    and change the mesh because a render was white.

    Each frame is archived to the preview gallery and REGISTERED as a candidate
    artifact, so a turnaround can be handed to an independent reviewer by
    `artifact_id` (see art_qa_verdict) and shows up in the dashboard beside the
    2D work. Point out_dir INSIDE the project - frames written outside it cannot
    be registered, and an unregistered render is one nobody reviews.
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

    The primitive path (blender_run + the kit) is for props, vehicles, terrain
    and block-out - things made of boxes and cylinders. It tops out at a
    proportioned blockout with no face and no fingers, so a hero character
    seen close up comes from here instead: generate the plate with
    image_generate, then hand it over.

    WHAT COMES BACK IS A DRAFT, NOT AN ASSET. Expect dense, unpredictable
    topology, no armature, no unit convention, and possibly baked lighting in
    the texture. It has to be scaled to 1.8 m, faced +Y, cleaned, unwrapped
    and weighted to a skeleton before blender_combine will make anything of
    it - bg_human's rig is the one to weight it to. `draft` is True in the
    result and `next_steps` says so; there is no path straight to
    godot_deliver_asset and that is deliberate.

    Nothing runs until you configure a backend (see .env.example) - this
    machine ships no model and downloads none. blender_status reports what is
    reachable. A local backend costs nothing per generation; a hosted one is
    priced before it submits, and `dry_run=True` returns that quote plus the
    licence verdict without spending anything.

    LICENCE IS PART OF THE RESULT. A local server is only a transport, so the
    model must be declared (BGATE_LOCAL_MODEL) - undeclared reads as unknown,
    never as permission. Some grants exclude whole territories and some
    forbid commercial use outright, which is a shipping problem rather than a
    technical one, so read `licence` before building on the mesh.

    parts=True ASKS FOR A BODY IN PIECES, and for a character it is the better
    request. A monolithic generation gives one blob - measured on a real user's
    asset, 940 disconnected shells with no relationship to anatomy - and bone
    heat then has to guess where the arm stops and the torso starts, which is
    how fingers end up weighted to a hip. A part-aware graph returns a head, a
    torso, arms and legs as SEPARATE meshes, and every step after it gets
    easier: `out_path` is read as a DIRECTORY, the result carries `parts` and a
    `combine` list ready for blender_combine, and a run that comes back with
    one mesh is flagged rather than reported as a success.

    It needs its own workflow (BGATE_COMFY_PARTS_WORKFLOW) whose saver writes
    one file per part. Without it this says so instead of quietly falling back
    to the monolith, because a silent fallback here is indistinguishable from
    the feature working.
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

    A character run leaves a per-layer .glb each, a .blend rig, the assembled
    asset and its renders - fourteen files for one request. This removes the
    layer sources listed in that asset's manifest and NOTHING ELSE, so a
    neighbouring asset's layers survive.

    Kept: the assembled file, its manifest, the renders. What was removed is
    written back into the manifest, so the run's history outlives its files and
    a single layer can still be identified and rebuilt later.

    Defaults to dry_run=True. Look at the list, then call again with
    dry_run=False.
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

    "Re-run that one layer, not the whole character" is the promise the layered
    3D path is built on, and until this tool existed there was no way to keep
    it: the recipe lived in the manifest and nothing read it back, so a bad cap
    meant re-modelling, re-texturing and re-assembling everything beside it.
    blender_combine names the layer that failed (`checks`: unbound,
    unweighted_verts, and the per-layer tri counts) - this is what you do with
    that name.

    `asset` is the ASSEMBLED .glb (the manifest sits beside it). `layer` is the
    layer name as blender_combine reported it. Then ONE of:
      script   bpy source for that layer, run and exported over the layer's own
               file. The modelling kit is injected (kit=True) exactly as in
               blender_run, and the script is recorded beside the layer so the
               next re-run has it.
      source   a .glb/.gltf/.blend you already built - used in place, nothing
               is run.
      neither  the layer's RECORDED script is re-run. After blender_sweep the
               layer files are gone and this is the recovery path: each swept
               layer's manifest entry carries the script that built it. If the
               file is still on disk and no script is given, it is reused as-is.

    Everything else - placement, rotation, scale, binding, decal_on, which layer
    holds the rig, the root name - comes back off the manifest untouched. A
    layer put back at the origin unrotated is a different asset, which is why
    those arguments are recorded rather than re-typed.

    Refuses BEFORE spending time in Blender when another layer's source is
    missing, and names those layers: combine would otherwise assemble happily
    around the hole and hand back a character with no arms. Re-run those first.

    The re-assembled file is registered under the SAME logical name, so it is
    revision N+1 of the asset a reviewer already saw, not a new one. Returns the
    combine result plus `changed` - the layer's tri and object counts before and
    after - so "did that fix it" is a number rather than an impression.
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
def blender_sprites(base_script: str, poses: list[dict], name: str = "sprite",
                    width: int = 128, height: int = 128,
                    engine: str = "BLENDER_EEVEE_NEXT", fps: float = 8.0,
                    res_dir: str = "assets/sprites", out_dir: Optional[str] = None,
                    timeout: int = 420) -> dict:
    """Render a Blender-built character as a transparent 2D sprite set.

    THE 2D art path: build the model once in base_script (bpy; lights included - camera optional, an auto-framed ORTHO one is added if missing), then each
    pose in poses=[{"name","script"}] tweaks the scene and renders one frame.
    Output: per-pose PNGs + <name>_sheet.png + <name>_frames.tres (a Godot
    SpriteFrames with one animation per pose) ready for an AnimatedSprite2D via
    godot_import_asset into res_dir. Rendered sprites cannot drift between
    poses the way hand-drawn ones do - same rig, camera, light every frame.

    A pose script that errors fails only that pose; check `failed` in the result.
    The sheet is archived to the preview gallery.
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
def animation_contacts(model: str, feet: Optional[list[str]] = None,
                       gait: Optional[str] = None,
                       clip: Optional[str] = None,
                       ground_axis: int = 1,
                       band_fraction: float = 0.25,
                       max_slide: float = 0.02,
                       max_variation: float = 0.20,
                       floor: Optional[float] = None) -> dict:
    """Where a character's feet ACTUALLY are, frame by frame - the question
    animation_curves structurally cannot answer.

    Every metric in animation_curves reads a channel's raw local values, which
    is right for "is this curve smooth" and wrong for anything about where a
    body part IS. A foot bone on a skinned humanoid has no translation channel
    at all - it moves because a hip and a knee rotate above it - so the foot
    skate check over there has never run on a real character. It read a
    constant and honestly said so.

    This runs forward kinematics off the file, composing each joint onto its
    parent per frame, and then measures what only world positions can show:

      support      how many feet are down on each frame, and FLIGHT - frames
                   with nothing down at all. Judged only against a DECLARED
                   `gait`, because the identical number is correct for a run
                   and impossible for a walk; an undeclared gait returns the
                   measurement with a refusal rather than a pass.
      contact      the planted foot's ground-plane speed. Judged against the
                   clip's own convention, which the evaluator detects: a
                   ROOT-MOTION clip should hold its planted foot still, an
                   IN-PLACE clip must slide it at a STEADY speed, because
                   there the foot is the ground. Judging in-place clips
                   against zero would fail every correct locomotion loop in
                   the project.
      clearance    frames where a foot passes below the floor. Pass `floor`
                   (usually 0.0) for the real one; the default asks the weaker
                   question of whether it dips below its own resting contact.

    `feet` names the contact joints exactly (LeftFoot/RightFoot on this
    project's humanoids, the four paws on a quadruped). Without it, joints
    whose names look like feet are used and the guess is reported.

    `gait` is one of walk, run, stand, any. It is a declaration about what the
    clip was MEANT to be, and there is deliberately no default - see the
    support verdict.
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
