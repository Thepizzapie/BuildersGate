"""Headless Blender adapter — the agent's eyes on its own geometry.

An agent writes a bpy script, this runs it in ``blender --background``, and hands
back tri counts, UV warnings, materials, and optionally a render. That return
trip is the whole point: bpy is an unforgiving generation target, and an agent
that cannot see what it made will confidently produce nothing.

Blender is discovered from BGATE_BLENDER, then PATH, then the usual install dirs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from glob import glob
from pathlib import Path
from typing import Optional

from . import _blender_kit as _kit

# Windows: keep every subprocess from flashing a console window.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Every Blender launch MUST go through this.
#
# stdin=DEVNULL is load-bearing, not hygiene. When the MCP server runs over
# stdio, its stdin IS the client's protocol channel. A child that inherits it
# blocks reading a stream meant for the server — Blender then sits at ~0% CPU
# forever, which reads as "slow render" and gets misdiagnosed as a GPU stall.
# It can also steal bytes off the wire and corrupt the session.
#
# Symptom to remember: works standalone (stdin is a terminal), hangs under the
# server (stdin is a pipe nobody will ever write to).
def _spawn(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )

RUNNER = Path(__file__).with_name("_blender_runner.py")

ENGINES = ("BLENDER_WORKBENCH", "BLENDER_EEVEE_NEXT", "CYCLES")
DEFAULT_ENGINE = "BLENDER_WORKBENCH"  # fast, GPU-optional — a preview, not a beauty pass

# Measured on this machine (Blender 4.5, Windows): the FIRST EEVEE render after a
# cold boot blew past a 240s timeout; every run after it took 1-12s, and the same
# script that timed out later ran in 1.4s. Clearing Blender's own gl-shader-cache
# did NOT bring the stall back, so the warmup is below Blender — GPU driver shader
# cache or the OS first-loading its GPU DLLs. Cause unconfirmed; the cost is real.
#
# So: warmup() pays it once on purpose, and the first GPU-engine call gets a
# generous timeout. Never let an agent's first render be the one that eats this.
COLD_START_TIMEOUT = 420
GPU_ENGINES = ("BLENDER_EEVEE_NEXT", "CYCLES")

_warmed: set[str] = set()

# EEVEE_NEXT — the engine warmup() defaults to and the one every GPU path in
# this adapter names — landed in Blender 4.2 and does not exist before it, so a
# 4.1 install is not "old", it is unusable here. bgate_core.doctor declares the
# same floor in MIN_REQUIRED; the two must stay equal or the health check and
# the adapter will disagree about the same binary.
MIN_VERSION = (4, 2)

_SEARCH_GLOBS = (
    r"C:\Program Files\Blender Foundation\Blender *\blender.exe",
    r"C:\Program Files (x86)\Blender Foundation\Blender *\blender.exe",
    "/Applications/Blender.app/Contents/MacOS/Blender",
    "/usr/bin/blender",
    "/usr/local/bin/blender",
    "/snap/bin/blender",
)


class BlenderNotFound(RuntimeError):
    pass


# Blender's Windows layout puts the version in a directory name
# ("C:\Program Files\Blender Foundation\Blender 4.5\blender.exe"), and the Linux
# packages sometimes do too ("/opt/blender-4.5.1/blender").
_DIR_VERSION = re.compile(r"blender[ _\-]*(\d+)\.(\d+)(?:\.(\d+))?", re.I)


def _path_version(path: str) -> tuple[int, ...]:
    """Version read out of the install PATH, or () when it carries none.

    From the name, never from `blender --version`: discovery is called by health
    polls and pytest skipifs, and spawning a process per candidate just to rank
    the list would turn a stat() into seconds. Unversioned layouts (a PATH entry,
    a custom dir) come back () and sort lowest — a known-good 4.5 beats a guess.
    """
    for part in reversed(Path(path).parts):
        match = _DIR_VERSION.search(part)
        if match:
            return tuple(int(g) for g in match.groups() if g is not None)
    return ()


def _pretty(version: tuple[int, ...]) -> str:
    return ".".join(str(n) for n in version)


def find_blender() -> str:
    """Locate the Blender executable. Newest usable version wins.

    "Newest" is by PARSED version, not by string: sorted() on the raw paths puts
    "Blender 4.10" BEFORE "Blender 4.9" (and "3.6" after "4.5" the moment a
    layout stops padding), so the lexicographic sort this used to do would hand
    an agent the older install and then blame Blender for the missing feature.

    Anything below MIN_VERSION is not a candidate — but it is not silence
    either: a too-old install is reported AS too old, with its version, because
    "Blender not found" sends the user hunting for an install they already have.
    """
    override = os.environ.get("BGATE_BLENDER")
    if override:
        if not Path(override).exists():
            raise BlenderNotFound(f"BGATE_BLENDER points at a missing file: {override}")
        return override

    on_path = shutil.which("blender")
    if on_path:
        return on_path

    found: list[str] = []
    for pattern in _SEARCH_GLOBS:
        found.extend(glob(pattern) if "*" in pattern else
                     ([pattern] if Path(pattern).exists() else []))

    if found:
        ranked = sorted(found, key=_path_version)
        usable = [p for p in ranked
                  if not _path_version(p) or _path_version(p) >= MIN_VERSION]
        if usable:
            return usable[-1]
        newest = ranked[-1]
        raise BlenderNotFound(
            f"Blender {_pretty(_path_version(newest))} is installed at {newest}, "
            f"but this adapter needs {_pretty(MIN_VERSION)} or newer — "
            "BLENDER_EEVEE_NEXT (the render engine every GPU path here uses) "
            f"does not exist before {_pretty(MIN_VERSION)}. Upgrade Blender, or "
            "point BGATE_BLENDER at a newer build."
        )

    raise BlenderNotFound(
        "Blender not found. Install it, put it on PATH, or set BGATE_BLENDER "
        "to the executable path."
    )


def available() -> dict:
    """Probe without running anything heavy — for health checks and tool errors."""
    try:
        path = find_blender()
    except BlenderNotFound as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, "path": path}


def version() -> dict:
    exe = find_blender()
    proc = _spawn([exe, "--version"], timeout=60)
    first = (proc.stdout or "").strip().splitlines()
    return {"path": exe, "version": first[0] if first else "unknown"}


def run_script(script: str, *, blend_file: Optional[str] = None,
               render: bool = False, out_dir: Optional[str] = None,
               engine: str = DEFAULT_ENGINE, timeout: int = 180,
               factory_startup: bool = True, kit: bool = True,
               export_glb: Optional[str] = None) -> dict:
    """Execute a bpy script headless and report what came back.

    script         bpy source. `bpy` is pre-imported; importing it again is fine.
    kit            prepend the modelling kit (bg_clean, bg_mat, bg_unwrap,
                   bg_join, bg_mirror, bg_bone_chain, bg_finish...). ON by
                   default: measured on the first real character run, an agent
                   wrote 33 KB of these helpers itself before modelling
                   anything, and then lost twenty minutes to the mesh hygiene
                   `bg_clean` does in four lines. Pass kit=False for a script
                   that must run against bare bpy.
    blend_file     open this .blend first; None starts from an empty scene
                   (no default cube — scripts should build what they mean).
    render         also render the active camera to a PNG.
    export_glb     also export the scene to this .glb (modifiers applied).
    out_dir        where renders land. Defaults to <cwd>/.bgate_out.
    engine         BLENDER_WORKBENCH (default, fast) | BLENDER_EEVEE_NEXT | CYCLES.

    Returns {ok, error, traceback, print, scene:{objects,totals,materials,...},
             issues:[game-readiness problems], glb:{...}, render:{...},
             exit_code, seconds}. A failing SCRIPT is a normal result with
     ok=False — not an exception. A failing BLENDER (missing binary, timeout,
     unparseable result) raises or reports ok=False with a reason.
    """
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")

    # First GPU-engine render in this process may hit the cold-start stall (see
    # COLD_START_TIMEOUT). Give it room rather than reporting a bogus failure.
    if render and engine in GPU_ENGINES and engine not in _warmed:
        timeout = max(timeout, COLD_START_TIMEOUT)

    exe = find_blender()
    out = Path(out_dir or (Path.cwd() / ".bgate_out"))
    out.mkdir(parents=True, exist_ok=True)

    # Everything from here down lives inside the try: the rmtree used to sit on
    # the last line, so the ONLY path that cleaned up was total success. A
    # timeout, a crashed Blender, an unreadable result.json, a missing
    # blend_file — every failure mode left its scratch dir in %TEMP%, and
    # failures are exactly what an agent produces in bulk while iterating.
    tmp = Path(tempfile.mkdtemp(prefix="bgate_blender_"))
    try:
        script_path = tmp / "agent_script.py"
        result_path = tmp / "result.json"
        script_path.write_text((_kit.KIT + "\n" + script) if kit else script,
                               encoding="utf-8")

        render_path = str(out / "render.png") if render else "-"
        glb_path = "-"
        if export_glb:
            glb_path = str(Path(export_glb).resolve())
            Path(glb_path).parent.mkdir(parents=True, exist_ok=True)

        cmd = [exe, "--background"]
        if blend_file:
            if not Path(blend_file).exists():
                raise FileNotFoundError(f"blend_file not found: {blend_file}")
            cmd.append(str(blend_file))
        if factory_startup:
            # Ignore the user's startup file and addons: agent runs must be
            # reproducible, and a stray addon changing defaults is a nightmare to
            # diagnose from a tool result.
            cmd.append("--factory-startup")
        cmd += ["--python", str(RUNNER), "--",
                str(script_path), str(result_path), render_path, engine, glb_path]

        import time
        started = time.monotonic()
        try:
            proc = _spawn(cmd, timeout=timeout)
        except subprocess.TimeoutExpired:
            hint = "infinite loop, a modal operator waiting on a UI, or a heavy render"
            if engine in GPU_ENGINES:
                hint = (f"{engine}'s first render on a cold machine can take minutes "
                        "(GPU shader warmup). Call warmup() once, use BLENDER_WORKBENCH "
                        "for iteration, or raise timeout.")
            return {"ok": False, "error": f"Blender timed out after {timeout}s",
                    "hint": hint, "seconds": timeout}
        finally:
            elapsed = round(time.monotonic() - started, 2)

        if render and engine in GPU_ENGINES:
            _warmed.add(engine)

        if not result_path.exists():
            # Blender died before the runner could write anything — a crash, a bad
            # .blend, or a startup failure. Surface its own words.
            return {
                "ok": False,
                "error": "Blender exited without producing a result",
                "exit_code": proc.returncode,
                "stderr": (proc.stderr or "")[-2000:],
                "stdout": (proc.stdout or "")[-1000:],
                "seconds": elapsed,
            }

        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"unreadable result from Blender: {exc}",
                    "exit_code": proc.returncode, "seconds": elapsed}

        result["exit_code"] = proc.returncode
        result["seconds"] = elapsed
        return result
    finally:
        # ignore_errors: a Blender killed on timeout can still hold a handle for
        # a beat on Windows, and a failed cleanup must not mask the result.
        shutil.rmtree(tmp, ignore_errors=True)


def warmup(engine: str = "BLENDER_EEVEE_NEXT", out_dir: Optional[str] = None) -> dict:
    """Pay the GPU cold-start cost once, on purpose, at a time of your choosing.

    Renders a trivial 32px scene. Do this at pipeline start (or after a reboot)
    so no agent's real render is the one that eats a multi-minute stall.
    """
    if engine not in GPU_ENGINES:
        return {"ok": True, "warmed": False, "reason": f"{engine} needs no warmup"}

    script = """
import bpy
bpy.ops.mesh.primitive_plane_add(size=1)
bpy.context.scene.render.resolution_x = 32
bpy.context.scene.render.resolution_y = 32
"""
    import time
    started = time.monotonic()
    got = run_script(script, render=True, engine=engine, out_dir=out_dir,
                     timeout=COLD_START_TIMEOUT)
    return {
        "ok": bool(got.get("ok")),
        "warmed": bool(got.get("render", {}).get("rendered")),
        "engine": engine,
        "seconds": round(time.monotonic() - started, 2),
        "error": got.get("error"),
    }


def scene_stats(blend_file: str, timeout: int = 120) -> dict:
    """Report a .blend without changing it — the read-only path."""
    return run_script("pass", blend_file=blend_file, timeout=timeout,
                      factory_startup=True)


def export_gltf(out_path: str, *, blend_file: Optional[str] = None,
                script: str = "pass", timeout: int = 240) -> dict:
    """Export a .blend (or a script-built scene) to .glb for the engine.

    Modifiers are applied — Blender's exporter defaults that OFF, which silently
    ships the base mesh and makes an asset look right in Blender and wrong in the
    engine. Returns the run result plus glb{exported, bytes} and the
    game-readiness issues worth fixing before this reaches a level.
    """
    return run_script(script, blend_file=blend_file, export_glb=out_path,
                      timeout=timeout)


# ---------------------------------------------------------------------------
# Assembly — the layers, bound into one asset
# ---------------------------------------------------------------------------
#
# WHY A CHARACTER IS NOT ONE GENERATION. Asked for a whole figure in one pass, a
# model spends one budget of attention across body, clothing, hard accessories
# and the text printed on them, and the parts that lose that competition come
# back deformed. Measured on a user's baseball player: the pose read fine, the
# hands and the cap did not, and the team logo on the cap was scrambled.
#
# Modelling each layer on its own fixes the first half. This function is the
# second half — without an assemble step, "layers" is just a pile of files, and
# the seat that split the work has made the human's job worse rather than better.

COMBINE_SUFFIXES = (".glb", ".gltf", ".blend")

# The planning ceiling, enforced as a WARNING rather than a refusal. The rule
# that stops a baseball player becoming a five-hour run over every lace lives in
# the art seat's doctrine, where it costs nothing; by the time parts reach this
# function the money is already spent, and refusing to assemble what was paid
# for would only add a lost asset to a lost afternoon.
MAX_LAYERS = 8

# How far proud of its surface a decal sits, in Blender units.
#
# THIS NUMBER IS THE GLITCHING LOGO. A logo modelled flush against a cap is two
# surfaces at the same depth, and which one draws is undefined per frame, per
# angle, per driver — the artefact reads as the logo tearing or flickering, and
# it is invisible in the modelling viewport where nobody moves the camera.
# Shrinkwrap conforms the decal to the curve; the offset decides the argument.
DECAL_OFFSET = 0.001

# The script prints its findings on one marked line. The runner already returns
# stdout, so this needs no change to the runner contract.
_PARTS_MARK = "BGATE_PARTS:"

_BIND_KINDS = ("deform", "none")   # plus "bone:<Name>", checked separately


def _bind_kind(value: str) -> str:
    """Validate one layer's binding, returning it normalised."""
    got = str(value or "none").strip()
    if got in _BIND_KINDS:
        return got
    if got.startswith("bone:") and got[5:].strip():
        return got
    raise ValueError(
        f"bind must be 'deform', 'none' or 'bone:<BoneName>', got {value!r}")


def _check_parts(parts: list) -> list[dict]:
    """Normalise and validate the layer list before Blender is ever launched.

    Every one of these is a mistake that would otherwise surface as a confusing
    bpy traceback several minutes in, or — worse — as an asset that assembled
    "successfully" with a layer silently missing.
    """
    if not parts:
        raise ValueError("combine needs at least one part")

    out: list[dict] = []
    for i, raw in enumerate(parts):
        if isinstance(raw, (str, os.PathLike)):
            raw = {"path": str(raw)}
        if not isinstance(raw, dict):
            raise TypeError(f"part {i} must be a path or a dict, got {type(raw).__name__}")

        path = Path(str(raw.get("path") or "")).expanduser()
        if not str(path):
            raise ValueError(f"part {i} has no path")
        if path.suffix.lower() not in COMBINE_SUFFIXES:
            raise ValueError(
                f"part {i} ({path.name}): combine reads {', '.join(COMBINE_SUFFIXES)}, "
                f"not {path.suffix or 'a suffixless file'}")
        if not path.is_file():
            raise FileNotFoundError(f"part {i}: no such file: {path}")

        out.append({
            "name": str(raw.get("name") or path.stem),
            "path": str(path.resolve()),
            "at": [float(v) for v in (raw.get("at") or (0, 0, 0))],
            "rotate": [float(v) for v in (raw.get("rotate") or (0, 0, 0))],
            "scale": raw.get("scale", 1.0),
            "bind": _bind_kind(raw.get("bind")),
            "decal_on": str(raw.get("decal_on") or ""),
        })

    names = [p["name"] for p in out]
    duplicated = {n for n in names if names.count(n) > 1}
    if duplicated:
        # Names are how a decal finds its surface, how a bind is reported, and
        # how the human matches what shipped against what they approved. Two
        # layers called "logo" make every one of those a coin flip.
        raise ValueError(f"layer names must be unique; repeated: {sorted(duplicated)}")

    for part in out:
        target = part["decal_on"]
        if target and target not in names:
            raise ValueError(
                f"layer {part['name']!r} is a decal on {target!r}, which is not "
                f"one of the parts ({', '.join(names)})")
        if target == part["name"]:
            raise ValueError(f"layer {part['name']!r} cannot be a decal on itself")
    return out


_COMBINE_SCRIPT = r'''
import bpy, json, math

P = json.loads(r"""__PAYLOAD__""")
notes = []


def wipe():
    """Start from nothing. --factory-startup loads Cube/Camera/Light, and an
    assembled character shipping with a stray default cube inside it is a bug
    somebody finds in the engine."""
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    for block in list(bpy.data.meshes):
        if not block.users:
            bpy.data.meshes.remove(block)


def bring_in(path):
    """Import one part and return ONLY the objects it added."""
    before = set(bpy.data.objects.keys())
    if path.lower().endswith(".blend"):
        with bpy.data.libraries.load(path, link=False) as (src, dst):
            dst.objects = list(src.objects)
        for obj in dst.objects:
            if obj is not None:
                bpy.context.scene.collection.objects.link(obj)
    else:
        bpy.ops.import_scene.gltf(filepath=path)
    return [bpy.data.objects[n] for n in sorted(set(bpy.data.objects.keys()) - before)]


wipe()
root = bpy.data.objects.new(P["root"], None)
bpy.context.scene.collection.objects.link(root)

owned = {}
for part in P["parts"]:
    objects = bring_in(part["path"])
    owned[part["name"]] = [o.name for o in objects]
    if not objects:
        notes.append({"layer": part["name"], "note": "imported nothing"})
        continue
    for obj in [o for o in objects if o.parent is None]:
        obj.location = [a + b for a, b in zip(obj.location, part["at"])]
        if any(part["rotate"]):
            obj.rotation_euler = [math.radians(v) for v in part["rotate"]]
        scale = part["scale"]
        obj.scale = (scale, scale, scale) if isinstance(scale, (int, float)) else tuple(scale)
        obj.parent = root
        obj.matrix_parent_inverse = root.matrix_world.inverted()

# --- decals: conform to the surface, then sit just proud of it ---------------
for part in P["parts"]:
    if not part["decal_on"]:
        continue
    surfaces = [bpy.data.objects[n] for n in owned.get(part["decal_on"], [])
                if bpy.data.objects[n].type == "MESH"]
    if not surfaces:
        notes.append({"layer": part["name"],
                      "note": "decal target %r has no mesh to conform to" % part["decal_on"]})
        continue
    for name in owned[part["name"]]:
        obj = bpy.data.objects[name]
        if obj.type != "MESH":
            continue
        mod = obj.modifiers.new("BGateDecalFit", "SHRINKWRAP")
        mod.target = surfaces[0]
        mod.offset = P["decal_offset"]
        mod.wrap_method = "NEAREST_SURFACEPOINT"

def unweighted(meshes):
    """Vertices carrying no weight — the ones that stay at the rest pose."""
    total = 0
    for mesh in meshes:
        total += sum(1 for v in mesh.data.vertices
                     if not any(g.weight > 0 for g in v.groups))
    return total


def parent_to(meshes, armature, kind):
    bpy.ops.object.select_all(action="DESELECT")
    for mesh in meshes:
        mesh.select_set(True)
    armature.select_set(True)
    bpy.context.view_layer.objects.active = armature
    bpy.ops.object.parent_set(type=kind)


def unparent(meshes):
    for mesh in meshes:
        mesh.parent = None
        for mod in [m for m in mesh.modifiers if m.type == "ARMATURE"]:
            mesh.modifiers.remove(mod)
        mesh.vertex_groups.clear()


def weight_layer(name, meshes, armature):
    """Bind a deforming layer, stepping down until nothing is left unweighted.

    heat -> envelope -> nearest bone. The last one is rigid and crude, and it
    is still better than shipping a mesh that tears: a layer bound stiffly
    animates wrongly, a layer half-bound comes apart.
    """
    for kind, label in (("ARMATURE_AUTO", "heat"),
                        ("ARMATURE_ENVELOPE", "envelope")):
        try:
            parent_to(meshes, armature, kind)
        except Exception as exc:
            notes.append({"layer": name, "note": "%s weighting failed: %s" % (label, exc)})
            unparent(meshes)
            continue
        loose = unweighted(meshes)
        if not loose:
            return "deform:" + label
        notes.append({"layer": name,
                      "note": "%s weighting left %d vertices unweighted — stepping down"
                              % (label, loose)})
        unparent(meshes)

    # Last resort: every vertex to the closest bone, weight 1. Nothing deforms
    # smoothly, but nothing tears either, and the note says which layer settled.
    bones = [b for b in armature.data.bones]
    if not bones:
        return "none"
    for mesh in meshes:
        groups = {}
        for bone in bones:
            groups[bone.name] = (mesh.vertex_groups.get(bone.name)
                                 or mesh.vertex_groups.new(name=bone.name))
        world = mesh.matrix_world
        for vert in mesh.data.vertices:
            point = world @ vert.co
            closest = min(bones, key=lambda b: (
                (armature.matrix_world @ ((b.head_local + b.tail_local) / 2.0)) - point).length)
            groups[closest.name].add([vert.index], 1.0, "REPLACE")
        mod = mesh.modifiers.new("Armature", "ARMATURE")
        mod.object = armature
        mesh.parent = armature
    notes.append({"layer": name,
                  "note": "fell back to nearest-bone weights — rigid, but nothing tears"})
    return "deform:nearest"


# --- the rig: one armature, every layer bound to it --------------------------
armature = None
if P["rig"]:
    for name in owned.get(P["rig"], []):
        if bpy.data.objects[name].type == "ARMATURE":
            armature = bpy.data.objects[name]
            break
    if armature is None:
        notes.append({"layer": P["rig"],
                      "note": "named as the rig but contains no armature — nothing was bound"})
    else:
        armature.parent = root

bound = {}
if armature is not None:
    for part in P["parts"]:
        if part["bind"] == "none":
            continue
        meshes = [bpy.data.objects[n] for n in owned.get(part["name"], [])
                  if bpy.data.objects[n].type == "MESH"]
        if not meshes:
            continue
        if part["bind"] == "deform":
            # Automatic weights, and THEN THE FALLBACKS, because bone heat is
            # the single biggest time sink this pipeline has produced.
            #
            # MEASURED: on the first real character run an agent lost roughly
            # twenty minutes to "which authoring choice makes the glTF round
            # trip survivable for bone heat?" and "does cleaning the boolean
            # output fix weighting?" — then boolean-unioned every layer and
            # rebuilt them all. Bone heat refuses doubled verts, loose
            # geometry, zero-area faces and multiple islands, which is exactly
            # what a per-layer glTF round trip hands it. It does not raise; it
            # silently leaves vertices at the rest pose, and the mesh tears.
            #
            # So: clean first, try heat, VERIFY it took, and step down rather
            # than reporting a bind that did not happen.
            for mesh in meshes:
                bg_clean(mesh)
            bound[part["name"]] = weight_layer(part["name"], meshes, armature)
        else:
            # Rigid: a cap does not bend, it rides a bone. Weighting it would
            # only let it deform, which for hard geometry is a defect.
            bone = part["bind"].split(":", 1)[1]
            if bone not in [b.name for b in armature.data.bones]:
                notes.append({"layer": part["name"],
                              "note": "bone %r is not in the armature (have: %s)"
                                      % (bone, ", ".join(b.name for b in armature.data.bones[:12]))})
                continue
            for mesh in meshes:
                mesh.parent = armature
                mesh.parent_type = "BONE"
                mesh.parent_bone = bone
                mesh.matrix_parent_inverse = armature.matrix_world.inverted()
            bound[part["name"]] = part["bind"]

# --- the tests: what detaches in-engine, caught here -------------------------
checks = []
for part in P["parts"]:
    for name in owned.get(part["name"], []):
        obj = bpy.data.objects[name]
        if obj.type != "MESH":
            continue
        rigged = any(m.type == "ARMATURE" for m in obj.modifiers)
        if part["bind"] == "deform" and armature is not None and not rigged:
            checks.append({"layer": part["name"], "object": name, "check": "unbound",
                           "detail": "asked to deform but has no armature modifier",
                           "fix": "this layer will stand still while the body moves"})
        if rigged:
            loose = sum(1 for v in obj.data.vertices
                        if not any(g.weight > 0 for g in v.groups))
            if loose:
                checks.append({"layer": part["name"], "object": name,
                               "check": "unweighted_verts", "count": loose,
                               "detail": "%d of %d vertices carry no weight"
                                         % (loose, len(obj.data.vertices)),
                               "fix": "they stay at the rest pose and tear the mesh"})
        if part["decal_on"] and not any(m.type == "SHRINKWRAP" for m in obj.modifiers):
            checks.append({"layer": part["name"], "object": name, "check": "decal_not_fitted",
                           "detail": "decal layer with no shrinkwrap — it will z-fight",
                           "fix": "check the decal target has a mesh"})

print("BGATE_PARTS:" + json.dumps(
    {"owned": owned, "bound": bound, "checks": checks, "notes": notes,
     "armature": armature.name if armature is not None else ""}))
'''


_TEXTURE_SCRIPT = r'''
import bpy, os

P = __PAYLOAD__
bg_wipe()
path = P["model"]
if path.lower().endswith(".blend"):
    with bpy.data.libraries.load(path, link=False) as (src, dst):
        dst.objects = list(src.objects)
    for obj in dst.objects:
        if obj is not None:
            bpy.context.scene.collection.objects.link(obj)
else:
    bpy.ops.import_scene.gltf(filepath=path)

image = bpy.data.images.load(P["image"], check_existing=True)
wanted = P["material"]
touched, unwrapped = [], []
for obj in [o for o in bpy.context.scene.objects if o.type == "MESH"]:
    # NO UVs MEANS NO TEXTURE. A layer modelled without an unwrap silently
    # ignores every map you give it, which looks exactly like the texture
    # having failed to generate.
    if not obj.data.uv_layers:
        bg_unwrap(obj)
        unwrapped.append(obj.name)
    if not obj.data.materials:
        bg_mat(obj, obj.name + "_mat", (0.8, 0.8, 0.8))
    for slot in obj.data.materials:
        if slot is None or (wanted and slot.name != wanted):
            continue
        slot.use_nodes = True
        tree = slot.node_tree
        bsdf = tree.nodes.get("Principled BSDF")
        if bsdf is None:
            continue
        node = None
        for existing in tree.nodes:
            if existing.type == "TEX_IMAGE" and existing.label == "BGateBaseColor":
                node = existing
                break
        if node is None:
            node = tree.nodes.new("ShaderNodeTexImage")
            node.label = "BGateBaseColor"
            node.location = (bsdf.location.x - 400, bsdf.location.y)
        node.image = image
        tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
        touched.append(slot.name)

print("BGATE_TEXTURE:" + json.dumps(
    {"materials": sorted(set(touched)), "unwrapped": unwrapped,
     "image": os.path.basename(P["image"])}))
'''


def apply_texture(model: str | os.PathLike[str], image: str | os.PathLike[str],
                  out_path: str | os.PathLike[str], *, material: str = "",
                  timeout: int = 240) -> dict:
    """Put a generated image on a layer's material and re-export it.

    THE MISSING HALF OF THE LAYERED PATH. Measured on the first real character
    run: the assembled asset carried 21 materials and ZERO images — every
    surface a flat colour an agent typed, because nothing connected the image
    adapter to the 3D layers. A flat colour is a blocking-in tool; the shipped
    surface is a generated texture, conditioned on the same pinned references
    every 2D asset in the project is conditioned on.

    ``material`` narrows it to one slot by name; empty means every slot on
    every mesh. Meshes without UVs are unwrapped first — otherwise the map is
    attached and silently ignored, which reads as the texture having failed.
    """
    src = Path(model)
    if not src.is_file():
        raise FileNotFoundError(f"no such model: {src}")
    img = Path(image)
    if not img.is_file():
        raise FileNotFoundError(f"no such texture image: {img}")

    payload = {"model": str(src.resolve()), "image": str(img.resolve()),
               "material": material}
    script = ("import json\n" +
              _TEXTURE_SCRIPT.replace("__PAYLOAD__", json.dumps(payload)))
    result = run_script(script, export_glb=str(out_path), timeout=timeout)

    report = _marked(result, "BGATE_TEXTURE:")
    return {**result,
            "textured": report.get("materials") or [],
            "unwrapped": report.get("unwrapped") or [],
            "out_path": str(Path(out_path).resolve())}


# How much of a render may be pure white before it is called blown out, and how
# dark the mean may go before it is called black.
#
# THIS IS THE LOOK-BACK MADE MECHANICAL. Measured: four turnaround renders of a
# correctly-coloured model (purple 0.40/0.11/0.64, teal 0.04/0.72/0.74) came out
# white and pastel because the lights were far too hot, and were reported as
# finished without anybody opening them. The model was fine. The render was not,
# and nothing in the pipeline could tell the difference.
BLOWN_FRACTION = 0.35
DARK_MEAN = 24.0

_TURNAROUND_SCRIPT = r'''
import bpy, math, os

P = __PAYLOAD__
bg_wipe()
path = P["model"]
if path.lower().endswith(".blend"):
    with bpy.data.libraries.load(path, link=False) as (src, dst):
        dst.objects = list(src.objects)
    for obj in dst.objects:
        if obj is not None:
            bpy.context.scene.collection.objects.link(obj)
else:
    bpy.ops.import_scene.gltf(filepath=path)

meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
if not meshes:
    raise RuntimeError("nothing to render — the model imported no meshes")

# Frame the subject from its own bounds. A fixed camera distance renders a
# thumbnail of a giant or a crop of a doll depending on the export scale.
lo = [1e9, 1e9, 1e9]
hi = [-1e9, -1e9, -1e9]
for obj in meshes:
    for corner in obj.bound_box:
        world = obj.matrix_world @ Vector(corner)
        for i in range(3):
            lo[i] = min(lo[i], world[i])
            hi[i] = max(hi[i], world[i])
centre = [(lo[i] + hi[i]) / 2.0 for i in range(3)]
height = max(hi[2] - lo[2], 1e-3)
width = max(hi[0] - lo[0], hi[1] - lo[1], 1e-3)
reach = max(height, width)

pivot = bpy.data.objects.new("BGatePivot", None)
bpy.context.scene.collection.objects.link(pivot)
pivot.location = centre
for obj in meshes:
    if obj.parent is None:
        obj.parent = pivot
        obj.matrix_parent_inverse = pivot.matrix_world.inverted()

cam_data = bpy.data.cameras.new("BGateCam")
cam = bpy.data.objects.new("BGateCam", cam_data)
bpy.context.scene.collection.objects.link(cam)
cam.location = (0, -reach * 2.4, centre[2])
cam.rotation_euler = (math.radians(90), 0, 0)
bpy.context.scene.camera = cam

# Three-point lighting at MODEST energy, scaled to the subject. The default
# instinct is a single hot sun, which is what turned a black uniform white.
def lamp(name, at, energy, size):
    data = bpy.data.lights.new(name, type="AREA")
    data.energy = energy
    data.size = size
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = at
    direction = (Vector(centre) - Vector(at)).normalized()
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    return obj

scale = max(reach, 1.0)
lamp("Key",  (-reach * 1.6, -reach * 1.8, centre[2] + reach), 220 * scale, reach)
lamp("Fill", (reach * 1.8, -reach * 1.2, centre[2]), 90 * scale, reach * 1.4)
lamp("Rim",  (0, reach * 2.0, centre[2] + reach * 0.8), 140 * scale, reach)

world = bpy.context.scene.world or bpy.data.worlds.new("BGateWorld")
bpy.context.scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes.get("Background")
if bg:
    bg.inputs[0].default_value = (0.28, 0.29, 0.32, 1.0)
    bg.inputs[1].default_value = 0.6

scene = bpy.context.scene
scene.render.engine = P["engine"]
scene.render.resolution_x, scene.render.resolution_y = P["size"]
scene.render.image_settings.file_format = "PNG"
scene.render.film_transparent = False
try:
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.exposure = P["exposure"]
except Exception:
    pass

written = []
for index, (label, degrees) in enumerate(P["angles"]):
    pivot.rotation_euler = (0, 0, math.radians(degrees))
    out = os.path.join(P["out_dir"], "%s_%s.png" % (P["stem"], label))
    scene.render.filepath = out
    bpy.ops.render.render(write_still=True)
    written.append({"label": label, "degrees": degrees, "path": out})

print("BGATE_TURNAROUND:" + json.dumps({"renders": written, "reach": reach}))
'''

TURNAROUND_ANGLES = (("front", 0), ("threequarter", 45), ("side", 90), ("back", 180))


def _exposure_report(path: Path) -> dict:
    """Is this render legible, or is it a white sheet nobody looked at?"""
    try:
        from PIL import Image
    except ImportError:
        return {"checked": False}
    try:
        with Image.open(path) as im:
            grey = im.convert("L")
            pixels = list(grey.getdata())
    except Exception as exc:                                   # noqa: BLE001
        return {"checked": False, "error": str(exc)}
    if not pixels:
        return {"checked": False}
    total = len(pixels)
    blown = sum(1 for p in pixels if p >= 250) / total
    mean = sum(pixels) / total
    verdict = ""
    if blown >= BLOWN_FRACTION:
        verdict = (f"blown out — {blown:.0%} of the frame is pure white; the "
                   "lights are too hot and the colours in the model are not "
                   "what you are looking at")
    elif mean <= DARK_MEAN:
        verdict = f"too dark to read — mean luminance {mean:.0f}/255"
    return {"checked": True, "blown": round(blown, 3), "mean": round(mean, 1),
            "ok": not verdict, "verdict": verdict}


def turnaround(model: str | os.PathLike[str], out_dir: str | os.PathLike[str], *,
               stem: str = "turnaround", angles=TURNAROUND_ANGLES,
               size=(640, 960), engine: str = "BLENDER_EEVEE_NEXT",
               exposure: float = 0.0, timeout: int = 480) -> dict:
    """Render a model from N angles under a fixed three-point rig, and JUDGE it.

    Every agent that renders a turnaround invents its own camera and lights, and
    the failure mode is always the same direction: too much light, a white
    figure, and a report of success written without opening the file. The rig
    here is scaled to the subject's own bounding box, and every frame comes back
    with a blown-out/too-dark verdict attached.

    ``ok`` is False when any frame fails its exposure check — which is the whole
    point: a render nobody can read must not pass as a finished one.
    """
    src = Path(model)
    if not src.is_file():
        raise FileNotFoundError(f"no such model: {src}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    payload = {"model": str(src.resolve()), "out_dir": str(out.resolve()),
               "stem": stem, "engine": engine, "exposure": float(exposure),
               "size": [int(size[0]), int(size[1])],
               "angles": [[str(a[0]), float(a[1])] for a in angles]}
    script = ("import json\nfrom mathutils import Vector\n" +
              _TURNAROUND_SCRIPT.replace("__PAYLOAD__", json.dumps(payload)))
    if engine in GPU_ENGINES:
        timeout = max(timeout, COLD_START_TIMEOUT)
    result = run_script(script, engine=engine, out_dir=str(out), timeout=timeout)

    report = _marked(result, "BGATE_TURNAROUND:")
    renders = []
    for entry in report.get("renders") or []:
        path = Path(entry["path"])
        renders.append({**entry, "exists": path.is_file(),
                        **_exposure_report(path)})
    unreadable = [r for r in renders if r.get("checked") and not r.get("ok")]
    return {**result, "renders": renders, "unreadable": unreadable,
            "ok": bool(result.get("ok")) and not unreadable and bool(renders)}


def _marked(result: dict, mark: str) -> dict:
    """Pull one marked JSON line out of a run's stdout."""
    for line in (result.get("print") or "").splitlines():
        if line.startswith(mark):
            try:
                return json.loads(line[len(mark):])
            except ValueError:
                return {}
    return {}


def combine(parts: list, out_path: str | os.PathLike[str], *,
            root_name: str = "Assembled", rig: str = "",
            timeout: int = 300) -> dict:
    """Assemble separately-modelled layers into one rigged, exported asset.

    ``parts`` is the layer list, in assembly order. Each entry is a path, or a
    dict::

        {"path": "out/uniform.glb",     # .glb / .gltf / .blend
         "name": "uniform",             # how it is reported and referenced
         "at": [0, 0, 0],               # offset applied to the part's roots
         "rotate": [0, 0, 0],           # degrees
         "scale": 1.0,                  # number, or [x, y, z]
         "bind": "deform",              # deform | bone:<Name> | none
         "decal_on": "cap"}             # conform to that layer's surface

    ``rig`` names the layer holding the armature everything binds to. Without
    it nothing is bound and the result is a static assembly, which is a
    legitimate answer for a prop and the wrong one for a character.

    Returns the run result plus ``parts`` (per layer: its objects, tris, and how
    it bound), ``checks`` (the failures worth catching before the engine does),
    and ``warnings``. A layer that imported nothing, failed to bind, or carries
    unweighted vertices is NAMED — the whole reason to model in layers is that
    a bad one can be re-run alone, which needs knowing which one it was.
    """
    checked = _check_parts(parts)
    warnings: list[str] = []
    if len(checked) > MAX_LAYERS:
        warnings.append(
            f"{len(checked)} layers is above the planning ceiling of {MAX_LAYERS} — "
            "assembling anyway, but a subject that needs this many is usually two "
            "assets, and every extra layer is another generation somebody paid for")
    if rig and rig not in [p["name"] for p in checked]:
        raise ValueError(
            f"rig={rig!r} is not one of the parts "
            f"({', '.join(p['name'] for p in checked)})")
    if not rig and any(p["bind"] != "none" for p in checked):
        # Binding without a rig is the caller believing the asset animates when
        # it cannot. Say so here rather than shipping a static character.
        warnings.append("layers ask to bind but no rig was named — nothing will "
                        "deform; pass rig=<the layer holding the armature>")

    payload = {"root": root_name, "parts": checked, "rig": rig,
               "decal_offset": DECAL_OFFSET}
    script = _COMBINE_SCRIPT.replace("__PAYLOAD__", json.dumps(payload))

    result = run_script(script, export_glb=str(out_path), timeout=timeout)

    report = _marked(result, _PARTS_MARK)

    # Join the script's ownership map to the runner's per-object stats, so a
    # layer reports its own tri count rather than the caller reading a scene
    # total and guessing which layer blew the budget.
    stats = {o["name"]: o for o in (result.get("scene") or {}).get("objects") or []}
    owned = report.get("owned") or {}
    bound = report.get("bound") or {}
    layers = []
    for part in checked:
        objects = owned.get(part["name"], [])
        layers.append({
            "name": part["name"],
            # The file this layer came from, kept so a sweep knows exactly
            # which intermediates belong to THIS run and a re-run knows which
            # single layer to rebuild.
            "source": part["path"],
            "objects": objects,
            "tris": sum(int(stats.get(n, {}).get("tris") or 0) for n in objects),
            "meshes": sum(1 for n in objects if stats.get(n, {}).get("type") == "MESH"),
            "bound": bound.get(part["name"], "none"),
            "decal_on": part["decal_on"],
            "imported": bool(objects),
        })

    missing = [layer["name"] for layer in layers if not layer["imported"]]
    if missing:
        warnings.append(f"layer(s) imported nothing: {', '.join(missing)}")

    got = {**result,
           "parts": layers,
           "armature": report.get("armature") or "",
           "checks": report.get("checks") or [],
           "notes": report.get("notes") or [],
           "warnings": warnings,
           "layers": len(layers)}
    got["manifest"] = write_manifest(out_path, got)
    return got


# ---------------------------------------------------------------------------
# The record, and cleaning up without erasing it
# ---------------------------------------------------------------------------
#
# A character run leaves a per-layer .glb, a .blend rig, the assembled asset and
# a set of renders — fourteen files for one request, and nothing on disk says
# which of them is the deliverable. Deleting the intermediates by hand means
# reading the directory and guessing; leaving them means every asset ships with
# its own scratch heap.
#
# The manifest answers "which of these is the thing" — and it OUTLIVES the
# sweep, keeping what was built, from what, and what was removed. A cleanup that
# erases the record of the run is not cleanup, it is amnesia: the layer list is
# how you re-run one layer six months later.

MANIFEST_SUFFIX = ".manifest.json"


def manifest_path(out_path: str | os.PathLike[str]) -> Path:
    target = Path(out_path)
    return target.with_name(target.name + MANIFEST_SUFFIX)


def write_manifest(out_path: str | os.PathLike[str], result: dict) -> str:
    """Record the run beside its output. Never raises — a manifest that fails
    to write must not fail an asset that assembled."""
    target = Path(out_path)
    doc = {
        "asset": target.name,
        "assembled_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "armature": result.get("armature") or "",
        "ok": bool(result.get("ok")),
        "layers": [{"name": p["name"], "objects": p["objects"], "tris": p["tris"],
                    "bound": p["bound"], "decal_on": p["decal_on"],
                    "source": p.get("source", "")}
                   for p in result.get("parts") or []],
        "checks": result.get("checks") or [],
        "warnings": result.get("warnings") or [],
        "kept": [target.name],
        "removed": [],
    }
    try:
        path = manifest_path(target)
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return str(path)
    except OSError:
        return ""


def sweep(out_path: str | os.PathLike[str], *, dry_run: bool = False,
          keep_renders: bool = True) -> dict:
    """Remove a run's intermediate layer files, keeping the asset and the record.

    Reads the manifest written beside the assembled asset, so it deletes what
    THAT RUN produced and nothing else — a sweep that globs a directory takes
    the neighbouring asset's layers with it.

    Kept always: the assembled file, its manifest, and (by default) renders.
    Removed: the per-layer sources listed in the manifest. What was removed is
    written back into the manifest, so the run's history survives its files.
    """
    target = Path(out_path)
    path = manifest_path(target)
    if not path.is_file():
        raise FileNotFoundError(
            f"no manifest beside {target.name} — sweep only removes what a "
            f"recorded run produced, so there is nothing safe to do here")
    doc = json.loads(path.read_text(encoding="utf-8"))

    protected = {target.resolve(), path.resolve()}
    removable, kept = [], []
    for layer in doc.get("layers") or []:
        source = layer.get("source") or ""
        if not source:
            continue
        candidate = Path(source)
        if not candidate.is_file() or candidate.resolve() in protected:
            continue
        if keep_renders and candidate.suffix.lower() in (".png", ".jpg", ".jpeg"):
            kept.append(str(candidate))
            continue
        removable.append(candidate)

    freed = 0
    removed = []
    if not dry_run:
        for candidate in removable:
            size = candidate.stat().st_size
            try:
                candidate.unlink()
            except OSError:
                kept.append(str(candidate))
                continue
            removed.append(str(candidate))
            freed += size
        doc["removed"] = sorted(set((doc.get("removed") or []) + removed))
        doc["swept_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        try:
            path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        except OSError:
            pass
    else:
        freed = sum(c.stat().st_size for c in removable)

    return {"ok": True, "manifest": str(path), "dry_run": dry_run,
            "removed": removed if not dry_run else [str(c) for c in removable],
            "kept": sorted(set(kept + [target.name, path.name])),
            "bytes_freed": freed}
