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


# ---------------------------------------------------------------------------
# The rig, across a glTF round trip
# ---------------------------------------------------------------------------
#
# glTF STORES BONE HEADS AND NOT BONE TAILS. A glTF joint is a node with a
# translation; where the bone POINTS is Blender's own idea, and nothing in the
# format carries it. So io_scene_gltf2 guesses on import — child head where a
# bone has exactly one child, and an invented stub for a leaf — and the guess is
# not the rig that was authored.
#
# MEASURED (Blender 4.5, the 940-vert bg_human base, 22 deform bones):
#
#   authored, weighted in the scene that built it   0 of 940 unweighted
#   exported to .glb, re-imported, bone heat        200 of 940 unweighted
#   ...the same after a merge-by-distance clean     200 of 940 unweighted
#   ...envelope weighting instead                     0 of 940 unweighted
#
# ALL 200 SAT BETWEEN z=1.546 AND z=1.800 — the top of the skull, and nothing
# else. The Head bone is a LEAF, its authored tail reached z=1.786, and the
# importer's stub stopped at z=1.532: heat had no bone above the crown, so the
# crown got no weight. That is why the damage survives cleaning the mesh — the
# mesh was never the problem — and why combine() then stepped the whole body
# layer down to deform:nearest, which is rigid.
#
# TWO THINGS FIX IT, both measured back to 0 of 940:
#
#   * RESTORE. The exporting run writes every armature's rest pose into the
#     layer's .bgate.json record (see LAYER_RECORD_SUFFIX), and the importing
#     run puts the tails back. Exact, and it also restores roll and the
#     use_deform flags — the importer marks every bone deforming, including a
#     Root that was authored not to. Measured on the base above: 0 of 23 heads
#     moved, 7 of 23 tails did, and putting those 7 back is the whole fix.
#   * GROW. With no record to restore from — a .glb from somewhere else — each
#     LEAF bone is extended along its own axis until it spans the geometry
#     standing around that axis, capped at BGATE_LEAF_CAP times its imported
#     length. Measured 0 of 940 (adult), 0 of 940 (chibi) and 0 of 1350 (A-pose,
#     dense) against 200/0/362 unrepaired.
#
# The standard reconstruction — "a bone's tail is its child's head" — is NOT
# one of them. Measured, it changed nothing at all (200 before, 200 after),
# because the importer already applies exactly that rule wherever it can. The
# bones it gets wrong are the ones with no child to ask.
_RIG_SOURCE = r'''
import bpy as _bpy
from mathutils import Vector as _Vec

# How far a leaf bone may be grown, as a multiple of its imported length. The
# cap binds on the Head bone of every base mesh measured, and it still reached.
BGATE_LEAF_CAP = 4.0


def bgate_rig_enter(rig):
    """Into edit mode on one armature, remembering what was selected."""
    previous = _bpy.context.view_layer.objects.active
    selected = [o for o in _bpy.context.view_layer.objects if o.select_get()]
    for obj in selected:
        obj.select_set(False)
    rig.select_set(True)
    _bpy.context.view_layer.objects.active = rig
    _bpy.ops.object.mode_set(mode="EDIT")
    return (rig, previous, selected)


def bgate_rig_leave(state):
    rig, previous, selected = state
    _bpy.ops.object.mode_set(mode="OBJECT")
    rig.select_set(False)
    for obj in selected:
        try:
            obj.select_set(True)
        except Exception:
            pass
    if previous is not None:
        _bpy.context.view_layer.objects.active = previous


def bgate_drop_shapes(objects):
    """Delete the glTF importer's bone custom shapes. Returns (kept, dropped).

    MEASURED (Blender 4.5): importing ANY .glb that carries an armature makes
    io_scene_gltf2 build a 42-vertex "Icosphere" at the world ORIGIN, link it
    into the scene and leave it PARENTLESS. It is not the asset. Left in, it is
    the one object that fails envelope weighting — which drags the whole layer
    down to rigid nearest-bone weights — it trips a no_material check that
    names a layer nobody authored, and it re-exports into the assembled .glb so
    the next import invents another one.
    """
    shapes = set()
    for rig in [o for o in objects if o.type == "ARMATURE"]:
        for posed in rig.pose.bones:
            if posed.custom_shape is not None:
                shapes.add(posed.custom_shape.name)
                posed.custom_shape = None
    kept = [o for o in objects if o.name not in shapes]
    for name in sorted(shapes):
        obj = _bpy.data.objects.get(name)
        if obj is not None:
            _bpy.data.objects.remove(obj, do_unlink=True)
    return kept, sorted(shapes)


def bgate_rig_dump(objects=None):
    """Every armature's REST pose, in ARMATURE space, ready to be written down.

    Armature space, not world: it is what edit_bone.head/.tail read and write,
    and it is preserved across the glTF round trip (measured — 0 of 23 bone
    HEADS moved), which is what makes putting the tails back meaningful.
    """
    pool = objects if objects is not None else list(_bpy.context.scene.objects)
    out = {}
    for rig in [o for o in pool if o.type == "ARMATURE"]:
        deform = {b.name: bool(b.use_deform) for b in rig.data.bones}
        bones = {}
        state = bgate_rig_enter(rig)
        try:
            for bone in rig.data.edit_bones:
                bones[bone.name] = {
                    "head": [round(v, 6) for v in bone.head],
                    "tail": [round(v, 6) for v in bone.tail],
                    "roll": round(bone.roll, 6),
                    "connect": bool(bone.use_connect),
                    "deform": deform.get(bone.name, True),
                }
        finally:
            bgate_rig_leave(state)
        if bones:
            out[rig.name] = {"bones": bones}
    return out


def bgate_rig_restore(rig, rest):
    """Put the authored tails, rolls and deform flags back. Returns how many
    tails actually moved — zero means the importer happened to guess right."""
    bones = (rest or {}).get("bones") or {}
    if not bones:
        return 0
    moved = 0
    state = bgate_rig_enter(rig)
    try:
        for name, spec in bones.items():
            bone = rig.data.edit_bones.get(name)
            if bone is None:
                continue
            tail = spec.get("tail")
            if tail and (bone.tail - _Vec(tail)).length > 1e-6:
                bone.tail = _Vec(tail)
                moved += 1
            roll = spec.get("roll")
            if roll is not None:
                bone.roll = float(roll)
    finally:
        bgate_rig_leave(state)
    for bone in rig.data.bones:
        spec = bones.get(bone.name)
        if spec is not None and "deform" in spec:
            bone.use_deform = bool(spec["deform"])
    return moved


def bgate_rig_repair(rig, meshes, cap=BGATE_LEAF_CAP):
    """No record: grow each LEAF bone until it spans the geometry around it.

    A leaf is the only bone the importer has nothing to guess FROM, and a leaf
    that stops short of the geometry it is supposed to move is exactly the
    unweighted skull cap. Only leaves are touched — every other bone's tail is
    already its child's head, which is both the importer's rule and the right
    answer. Returns {bone: growth factor} for what moved.
    """
    into = rig.matrix_world.inverted()
    points = []
    for mesh in meshes:
        if mesh.type != "MESH":
            continue
        onto = into @ mesh.matrix_world
        points.extend(onto @ v.co for v in mesh.data.vertices)
    if not points:
        return {}
    # A dense layer does not need every vertex to find its far edge.
    points = points[::max(1, len(points) // 4000)]

    grown = {}
    state = bgate_rig_enter(rig)
    try:
        bones = rig.data.edit_bones
        parents = {b.parent.name for b in bones if b.parent is not None}
        for bone in bones:
            if bone.name in parents:
                continue
            head = bone.head.copy()
            axis = bone.tail - head
            length = axis.length
            if length < 1e-9:
                continue
            axis = axis.normalized()
            far = length
            for point in points:
                offset = point - head
                along = offset.dot(axis)
                # Only geometry BEYOND the current tail can extend it, and only
                # geometry standing near the bone's own axis is its to move.
                if along <= far:
                    continue
                if (offset - axis * along).length <= length:
                    far = along
            far = min(far, length * cap)
            if far > length * 1.01:
                bone.tail = head + axis * far
                grown[bone.name] = round(far / length, 3)
    finally:
        bgate_rig_leave(state)
    return grown


def bgate_rig_fix(objects, rest, grow=True):
    """Undo the importer's tail guess across everything just imported."""
    report = {"restored": {}, "grown": {}}
    rigs = [o for o in objects if o.type == "ARMATURE"]
    meshes = [o for o in objects if o.type == "MESH"]
    rest = rest or {}
    for rig in rigs:
        spec = rest.get(rig.name)
        # One armature, one record, a name the exporter renamed: still its rig.
        if spec is None and len(rest) == 1 and len(rigs) == 1:
            spec = list(rest.values())[0]
        if spec:
            report["restored"][rig.name] = bgate_rig_restore(rig, spec)
        elif grow:
            report["grown"][rig.name] = bgate_rig_repair(rig, meshes)
    return report
'''


# Appended to any script that exports a .glb, so the rest pose the exporter is
# about to throw away is written down beside the file. Wrapped whole in a
# try/except: this runs after an agent's own script, and a capture that fails
# must not fail an export that worked.
_RIG_CAPTURE = r'''

try:
    import json as _bgate_json
    _bgate_rest = bgate_rig_dump()
    if _bgate_rest:
        with open(__RIG_OUT__, "w", encoding="utf-8") as _bgate_fh:
            _bgate_json.dump(_bgate_rest, _bgate_fh)
except Exception:
    pass
'''


def _read_json(path: Path) -> dict:
    """A dict off disk, or {}. Never raises — every caller here is a side
    channel whose absence must not fail the run it decorated."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def run_script(script: str, *, blend_file: Optional[str] = None,
               render: bool = False, out_dir: Optional[str] = None,
               engine: str = DEFAULT_ENGINE, timeout: int = 180,
               factory_startup: bool = True, kit: bool = True,
               export_glb: Optional[str] = None, record: bool = True) -> dict:
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
    record         when a .glb is exported, write THIS SCRIPT into a sidecar
                   record beside it (see LAYER_RECORD_SUFFIX). That record is
                   what makes "re-run one layer later" true rather than a
                   promise: combine() folds it into the assembled asset's
                   manifest, so six months on the manifest names not just which
                   file a layer came from but the source that produced it.
                   Internal callers that build their own script (combine,
                   apply_texture, turnaround) pass record=False — the script
                   they run is this module's, not the layer's.
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
        rig_path = tmp / "rig.json"
        source = (_kit.KIT + "\n" + script) if kit else script
        if export_glb:
            # The rest pose, captured to a FILE rather than printed. The runner
            # truncates stdout to its last 4000 characters, and a 23-bone dump
            # is bigger than that — printing it would evict the agent's own
            # output and still arrive cut in half.
            source += ("\n\n" + _RIG_SOURCE + "\n" +
                       _RIG_CAPTURE.replace("__RIG_OUT__", json.dumps(str(rig_path))))
        script_path.write_text(source, encoding="utf-8")

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
        if export_glb and result.get("ok"):
            fields: dict = {}
            # ARMATURES ARE RECORDED EVEN WHEN record=False. `record` governs
            # whose SCRIPT the sidecar claims, and combine/apply_texture run
            # this module's rather than the layer's. The rest pose is not
            # authorship, it is the thing glTF cannot carry — see _RIG_SOURCE —
            # and dropping it here is how the assembled asset would lose its
            # tails the moment anything re-imported it.
            armatures = _read_json(rig_path)
            if armatures:
                fields["armatures"] = armatures
            if record:
                fields.update({
                    "script": script,
                    "kit": bool(kit),
                    "blend_file": str(blend_file or ""),
                    "built_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                })
            if fields:
                merge_layer_record(glb_path, fields)
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

# How far proud of its surface a decal sits, in Blender units — the FLOOR only.
#
# THIS NUMBER IS THE GLITCHING LOGO. A logo modelled flush against a cap is two
# surfaces at the same depth, and which one draws is undefined per frame, per
# angle, per driver — the artefact reads as the logo tearing or flickering, and
# it is invisible in the modelling viewport where nobody moves the camera.
# Shrinkwrap conforms the decal to the curve; the offset decides the argument.
#
# AN ABSOLUTE CONSTANT IS THE WRONG SHAPE FOR THIS. It is applied in the decal
# object's LOCAL space, after the layer's own scale — so the same 0.001 means a
# visible float on a 0.3 m prop and nothing at all on a 10 m one, and a layer
# scaled 0.1x divides it by ten again. The offset is derived from the TARGET's
# bounding-box diagonal and divided back out by the decal's world scale; this
# constant survives only as the floor for a degenerate target.
DECAL_OFFSET = 0.001

# Offset as a fraction of the target's bbox diagonal. 0.2% is under a pixel at
# any sane framing and still a decisive winner in the depth test.
DECAL_OFFSET_RATIO = 0.002

# SHRINKWRAP ONLY MOVES VERTICES IT ALREADY HAS. MEASURED (Blender 4.5): a
# 4-vertex plane of size 0.4 shrinkwrapped onto a sphere of radius 1.2 put its
# four corners on the surface and its face centre 0.030 INSIDE it — minimum
# radius 1.1696 against the sphere's 1.2. The decal reads as a hole. Subdivided
# to a max edge of 4% of the target's diagonal (25 verts here) the worst
# deviation dropped to 0.0083, exactly the requested offset, and every point sat
# proud of the surface. The subdivision is not a nicety; it is what makes the
# shrinkwrap mean anything.
DECAL_EDGE_RATIO = 0.04

# How far off the offset surface a decal may sit before it is called unfitted,
# as a fraction of the target's diagonal, on top of twice the offset itself.
# Sized from the measurement above: it passes the subdivided fit (0.0083 against
# a 0.0249 budget) and fails the sagging one (0.0301).
DECAL_TOLERANCE_RATIO = 0.002

# How many subdivision passes a decal gets before we stop. Each pass quadruples
# the face count; five is 1024x on a single quad, which is far past any decal
# worth conforming and keeps a mistaken decal_on from exploding the run.
DECAL_MAX_SUBDIVISIONS = 5

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
import bpy, bmesh, json, math
from mathutils import Euler, Matrix, Vector

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


def bring_in(part):
    """Import one part and return ONLY the objects it added — the importer's
    own junk removed, and the rig it guessed at put back."""
    path = part["path"]
    blended = path.lower().endswith(".blend")
    before = set(bpy.data.objects.keys())
    if blended:
        with bpy.data.libraries.load(path, link=False) as (src, dst):
            dst.objects = list(src.objects)
        for obj in dst.objects:
            if obj is not None:
                bpy.context.scene.collection.objects.link(obj)
    else:
        bpy.ops.import_scene.gltf(filepath=path)
    added = [bpy.data.objects[n]
             for n in sorted(set(bpy.data.objects.keys()) - before)]

    # THE IMPORTER'S BONE CUSTOM SHAPE IS NOT A LAYER. See bgate_drop_shapes:
    # left in, this 42-vertex Icosphere is the one object that fails envelope
    # weighting, and one failure steps the WHOLE layer down to rigid
    # nearest-bone weights. turnaround has filtered it since the day it was
    # measured; this import path had not.
    #
    # ONLY ON A glTF IMPORT. A .blend's custom shapes are a rigger's control
    # widgets — somebody put them there — and deleting an author's widgets
    # because a different importer invents lookalikes is not the same fix.
    dropped = []
    if not blended:
        added, dropped = bgate_drop_shapes(added)
    if dropped:
        notes.append({"layer": part["name"],
                      "note": "dropped the glTF importer's bone custom shape(s) %s — "
                              "not part of this layer" % ", ".join(dropped)})

    # A .blend carries its bone tails. Only glTF loses them, so only a glTF
    # import is repaired — growing a leaf on a rig that was loaded intact would
    # be inventing bone length nobody lost.
    fixed = {} if blended else bgate_rig_fix(added, part.get("rest") or {})
    for name, moved in (fixed.get("restored") or {}).items():
        if moved:
            notes.append({"layer": part["name"],
                          "note": "restored %d authored bone tail(s) on %s from the "
                                  "layer record — glTF does not carry them"
                                  % (moved, name)})
    for name, grown in (fixed.get("grown") or {}).items():
        if grown:
            notes.append({"layer": part["name"],
                          "note": "no rest record for %s; grew leaf bone(s) %s to span "
                                  "the geometry the importer's stub stopped short of"
                                  % (name, ", ".join(sorted(grown)))})
    return added


wipe()
root = bpy.data.objects.new(P["root"], None)
bpy.context.scene.collection.objects.link(root)

owned = {}
for part in P["parts"]:
    objects = bring_in(part)
    owned[part["name"]] = [o.name for o in objects]
    if not objects:
        notes.append({"layer": part["name"], "note": "imported nothing"})
        continue
    # matrix_world is a CACHE. MEASURED: setting obj.scale = (2,2,3) and reading
    # obj.matrix_world back on the next line still returns the old scale until
    # the depsgraph runs, so anything that composes onto the import's transform
    # has to ask for the update first or it composes onto a stale identity.
    bpy.context.view_layer.update()
    for obj in [o for o in objects if o.parent is None]:
        # COMPOSE, NEVER ASSIGN. Two measured failures lived on these lines:
        #   obj.scale = (s, s, s) CLOBBERED the imported node's own scale — a
        #     layer authored at (2, 2, 3) came out (1, 1, 1) the moment anyone
        #     passed a scale, and (1,1,1) is exactly what "scale: 1.0" means.
        #   obj.rotation_euler = ... was a SILENT NO-OP, because the glTF
        #     importer leaves rotation_mode == 'QUATERNION' and Blender reads
        #     the quaternion in that mode. rotate:[0,0,90] moved nothing.
        obj.rotation_mode = "XYZ"
        factors = part["scale"]
        if isinstance(factors, (int, float)):
            factors = (factors, factors, factors)
        location, rotation, scaling = obj.matrix_world.decompose()
        turn = Euler([math.radians(v) for v in part["rotate"]], "XYZ").to_quaternion()
        obj.matrix_world = Matrix.LocRotScale(
            Vector(location) + Vector(part["at"]),
            turn @ rotation,
            Vector([a * b for a, b in zip(scaling, factors)]))
        obj.parent = root
        obj.matrix_parent_inverse = root.matrix_world.inverted()

bpy.context.view_layer.update()


# --- decals: conform to the surface, then sit just proud of it ---------------
#
# See DECAL_EDGE_RATIO in blender.py for the measurement behind the subdivide:
# shrinkwrap moves the vertices a mesh HAS, and a 4-vertex plane has none in the
# middle to move, so its face sinks into the surface it was supposed to sit on.

def world_corners(obj):
    return [obj.matrix_world @ Vector(c) for c in obj.bound_box]


def world_diagonal(obj):
    corners = world_corners(obj)
    lo = Vector((min(c.x for c in corners), min(c.y for c in corners),
                 min(c.z for c in corners)))
    hi = Vector((max(c.x for c in corners), max(c.y for c in corners),
                 max(c.z for c in corners)))
    return max((hi - lo).length, 1e-6)


def world_scale(obj):
    return max(max(abs(v) for v in obj.matrix_world.to_scale()), 1e-6)


def subdivide_to(obj, target_edge):
    """Cut the decal down to a max WORLD edge length, so shrinkwrap has grip."""
    factor = world_scale(obj)
    for _ in range(P["decal_max_subdivisions"]):
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        longest = max([e.calc_length() for e in bm.edges] or [0.0]) * factor
        if longest <= target_edge:
            bm.free()
            return
        bmesh.ops.subdivide_edges(bm, edges=list(bm.edges), cuts=1,
                                  use_grid_fill=True)
        bm.to_mesh(obj.data)
        bm.free()
    obj.data.update()


decal_fit = {}
for part in P["parts"]:
    if not part["decal_on"]:
        continue
    surfaces = [bpy.data.objects[n] for n in owned.get(part["decal_on"], [])
                if bpy.data.objects[n].type == "MESH"]
    if not surfaces:
        notes.append({"layer": part["name"],
                      "note": "decal target %r has no mesh to conform to" % part["decal_on"]})
        continue
    target = surfaces[0]
    diagonal = world_diagonal(target)
    offset = max(diagonal * P["decal_offset_ratio"], P["decal_offset"])
    for name in owned[part["name"]]:
        obj = bpy.data.objects[name]
        if obj.type != "MESH":
            continue
        subdivide_to(obj, diagonal * P["decal_edge_ratio"])
        mod = obj.modifiers.new("BGateDecalFit", "SHRINKWRAP")
        mod.target = target
        mod.wrap_method = "NEAREST_SURFACEPOINT"
        # The modifier's offset is in the DECAL's local space, which is why an
        # absolute constant here broke under any layer scale.
        mod.offset = offset / world_scale(obj)
        decal_fit[name] = {
            "target": target.name,
            "offset": offset,
            "tolerance": offset * 2.0 + diagonal * P["decal_tolerance_ratio"],
        }

bpy.context.view_layer.update()


def deform_bones(armature):
    """The bones that can actually MOVE a vertex.

    A vertex group named for anything else — a control bone, a leftover named
    'NotABone' — carries weight that no bone reads. Counting those as "bound"
    is how a mesh reports zero unweighted vertices and still tears in-engine.
    """
    if armature is None:
        return set()
    return {b.name for b in armature.data.bones if b.use_deform}


def unweighted(meshes, bone_names):
    """Vertices carrying no DEFORM weight — the ones that stay at the rest pose."""
    total = 0
    for mesh in meshes:
        groups = mesh.vertex_groups
        for vert in mesh.data.vertices:
            if not any(g.weight > 0 and groups[g.group].name in bone_names
                       for g in vert.groups):
                total += 1
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
    bone_names = deform_bones(armature)
    for kind, label in (("ARMATURE_AUTO", "heat"),
                        ("ARMATURE_ENVELOPE", "envelope")):
        try:
            parent_to(meshes, armature, kind)
        except Exception as exc:
            notes.append({"layer": name, "note": "%s weighting failed: %s" % (label, exc)})
            unparent(meshes)
            continue
        loose = unweighted(meshes, bone_names)
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
            #
            # KEEP THE AUTHORED POSITION. parent_type='BONE' positions a child
            # relative to the bone's TAIL, in BONE-LOCAL space where the bone's
            # +Y runs along its own length — nothing like the armature object's
            # own space. MEASURED (Blender 4.5): a cap authored at (0, 0, 2),
            # bound to the default bone (head (0,0,0), tail (0,0,1)) with
            # matrix_parent_inverse = armature.matrix_world.inverted(), landed
            # at (0, -2, 1) rotated 90 degrees. Same probe with the world matrix
            # captured and restored: (0, 0, 2), rotation zero.
            #
            # Restoring matrix_world is also robust to whatever bone axis
            # convention the rig was authored with, which no hand-derived
            # correction matrix is.
            keep = [(mesh, mesh.matrix_world.copy()) for mesh in meshes]
            for mesh, _ in keep:
                mesh.parent = armature
                mesh.parent_type = "BONE"
                mesh.parent_bone = bone
                mesh.matrix_parent_inverse.identity()
            # The parent's effective matrix comes off the pose bone; it has to
            # be evaluated before matrix_world can be solved back through it.
            bpy.context.view_layer.update()
            for mesh, authored in keep:
                mesh.matrix_world = authored
            bpy.context.view_layer.update()
            bound[part["name"]] = part["bind"]

# --- the tests: what detaches in-engine, caught here -------------------------

def decal_deviation(obj, target):
    """Worst distance from the SHRINKWRAPPED result to the target's surface.

    Measured on the EVALUATED mesh, and on face centres as well as vertices —
    the sag that a decal_not_fitted check exists to catch happens BETWEEN the
    vertices, so a vertex-only sample reports a perfect fit on the exact mesh
    that has a hole in the middle of it.
    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    try:
        mesh = evaluated.to_mesh()
    except Exception:
        return None
    try:
        into_target = target.matrix_world.inverted()
        points = [evaluated.matrix_world @ v.co for v in mesh.vertices]
        points += [evaluated.matrix_world @ p.center for p in mesh.polygons]
        if not points:
            return None
        # A dense decal does not need every point sampled to find its worst one.
        step = max(1, len(points) // 400)
        worst = 0.0
        for point in points[::step]:
            hit, spot, _normal, _index = target.closest_point_on_mesh(
                into_target @ point)
            if not hit:
                continue
            worst = max(worst, ((target.matrix_world @ spot) - point).length)
        return worst
    finally:
        try:
            evaluated.to_mesh_clear()
        except Exception:
            pass


def carries_image(material):
    """Does this material reach an actual image, or is it a colour somebody typed?"""
    if not material.use_nodes or material.node_tree is None:
        return False
    return any(node.type == "TEX_IMAGE" and node.image is not None
               for node in material.node_tree.nodes)


def layer_materials(names):
    found = {}
    for name in names:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "MESH":
            continue
        for slot in obj.data.materials:
            if slot is not None:
                found[slot.name] = slot
    return found


checks = []
bone_names = deform_bones(armature)
for part in P["parts"]:
    # WHICH LAYER TO RE-RUN. The asset-level no_textures gate below says the
    # assembly carries no image at all, which is true and useless: on a
    # six-layer character it names none of the six. An agent told "this asset
    # is untextured" re-runs everything or guesses. Per layer, it re-runs one.
    mats = layer_materials(owned.get(part["name"], []))
    if mats and not any(carries_image(m) for m in mats.values()):
        checks.append({"layer": part["name"],
                       "object": (owned.get(part["name"]) or [""])[0],
                       "check": "untextured",
                       "materials": sorted(mats),
                       "detail": "layer %r carries %d material(s) (%s) and not one "
                                 "image texture — it ships as a flat colour"
                                 % (part["name"], len(mats), ", ".join(sorted(mats))),
                       "fix": "generate a texture for %r and apply_texture it onto "
                              "that layer before combining" % part["name"]})
    # ANY bind that did not happen is a layer that stands still while the body
    # moves — a bone: bind that named a bone the armature does not have used to
    # leave nothing but a note, and notes are not checks.
    if part["bind"] != "none" and bound.get(part["name"], "none") == "none":
        checks.append({"layer": part["name"],
                       "object": (owned.get(part["name"]) or [""])[0],
                       "check": "unbound", "asked": part["bind"],
                       "detail": "asked for bind %r and nothing bound it" % part["bind"],
                       "fix": "this layer will stand still while the body moves"})
    for name in owned.get(part["name"], []):
        obj = bpy.data.objects[name]
        if obj.type != "MESH":
            continue
        rigged = any(m.type == "ARMATURE" for m in obj.modifiers)
        if rigged:
            # Against the DEFORM BONES, not against any vertex group that
            # happens to exist. MEASURED: a cube carrying one group named
            # "NotABone" reported 0 unweighted and tore in-engine.
            loose = unweighted([obj], bone_names)
            if loose:
                checks.append({"layer": part["name"], "object": name,
                               "check": "unweighted_verts", "count": loose,
                               "detail": "%d of %d vertices carry no deform weight"
                                         % (loose, len(obj.data.vertices)),
                               "fix": "they stay at the rest pose and tear the mesh"})
        if not part["decal_on"]:
            continue
        fit = decal_fit.get(name)
        if fit is None:
            checks.append({"layer": part["name"], "object": name, "check": "decal_not_fitted",
                           "detail": "decal layer with no shrinkwrap — it will z-fight",
                           "fix": "check the decal target has a mesh"})
            continue
        gap = decal_deviation(obj, bpy.data.objects[fit["target"]])
        if gap is None:
            continue
        if gap > fit["tolerance"]:
            checks.append({"layer": part["name"], "object": name,
                           "check": "decal_not_fitted",
                           "gap": round(gap, 5), "tolerance": round(fit["tolerance"], 5),
                           "detail": "decal sits up to %.4f from %s (budget %.4f) — it "
                                     "sinks into the surface between its vertices"
                                     % (gap, fit["target"], fit["tolerance"]),
                           "fix": "give the decal more geometry, or model it closer "
                                  "to the shape it has to lie on"})

# What the exporter will actually ship. A layer pile that assembled cleanly and
# carries no material and no image is a grey blob, and it used to report ok.
materials = sorted({m.name for m in bpy.data.materials if m.users})
images = set()
for material in bpy.data.materials:
    if not material.use_nodes or material.node_tree is None:
        continue
    for node in material.node_tree.nodes:
        if node.type == "TEX_IMAGE" and node.image is not None:
            images.add(node.image.name)

print("BGATE_PARTS:" + json.dumps(
    {"owned": owned, "bound": bound, "checks": checks, "notes": notes,
     "materials": materials, "images": sorted(images),
     "armature": armature.name if armature is not None else ""}))
'''


# Which BSDF input each map drives, and the LABEL its image node carries so a
# second pass replaces the map instead of stacking another node behind the same
# socket. "normal" is the odd one: it cannot reach the socket directly, it has
# to go through a Normal Map node or the tangent-space vectors are read as
# colours and the surface lights as if it were painted with the map.
_MAP_SOCKETS = {
    "base_color": ("Base Color", "BGateBaseColor"),
    "roughness": ("Roughness", "BGateRoughness"),
    "metallic": ("Metallic", "BGateMetallic"),
    "normal": ("Normal", "BGateNormal"),
    "emission": ("Emission Color", "BGateEmission"),
}

_TEXTURE_SCRIPT = r'''
import bpy, os

# json.loads, not a bare literal: this payload carries booleans, and JSON's
# true/false are not Python's. Pasted straight in they are a NameError several
# minutes into a Blender launch.
P = json.loads(r"""__PAYLOAD__""")
bg_wipe()
path = P["model"]
before = set(bpy.data.objects.keys())
blended = path.lower().endswith(".blend")
if blended:
    with bpy.data.libraries.load(path, link=False) as (src, dst):
        dst.objects = list(src.objects)
    for obj in dst.objects:
        if obj is not None:
            bpy.context.scene.collection.objects.link(obj)
else:
    bpy.ops.import_scene.gltf(filepath=path)

# THIS PASS RE-EXPORTS, SO IT INHERITS THE ROUND TRIP'S DAMAGE. A texture run
# that imports a rigged layer, wires an image and writes it back out would ship
# the importer's GUESSED bone tails as if they were authored — and its own
# capture would then record the guess, so the record combine() later restores
# from would be wrong. Put the tails back before anything is written.
#
# Restore only, never grow: inventing bone length is a weighting repair, and a
# texture pass has no business changing the rig it was handed.
imported = [bpy.data.objects[n]
            for n in sorted(set(bpy.data.objects.keys()) - before)]
dropped_shapes = []
rig_fixed = {}
if not blended:
    # Only glTF invents the widget and only glTF loses the tails; a .blend
    # arrives with both intact and is left exactly as its author saved it.
    imported, dropped_shapes = bgate_drop_shapes(imported)
    rig_fixed = bgate_rig_fix(imported, P.get("rest") or {}, grow=False)

report = {"materials": [], "unwrapped": [], "slots": [], "wired": {},
          "dropped_shapes": dropped_shapes,
          "rig_restored": rig_fixed.get("restored") or {},
          "colorspaces": {}, "alpha": P["alpha"], "refused": "",
          "maps": {k: os.path.basename(v["path"]) for k, v in P["maps"].items()}}


def say(detail):
    """Print the findings BEFORE raising. The runner captures stdout even when
    the script dies, and it skips the .glb export when it does — so a refusal
    reports what it saw and writes no half-textured asset."""
    report["detail"] = detail
    print("BGATE_TEXTURE:" + json.dumps(report))


def load(kind):
    """One map, in the colour space its DATA needs.

    THIS IS THE HALF EVERYONE FORGETS. A roughness or normal map is measurement,
    not colour: read as sRGB it gets the display transfer curve applied on the
    way in, so every value is wrong — a 0.5 roughness lands near 0.21 and the
    surface reads far glossier than it was authored. Only the maps that feed a
    COLOUR socket stay sRGB.
    """
    spec = P["maps"][kind]
    image = bpy.data.images.load(spec["path"], check_existing=True)
    try:
        image.colorspace_settings.name = spec["colorspace"]
    except Exception as exc:
        report.setdefault("colorspace_errors", []).append("%s: %s" % (kind, exc))
    report["colorspaces"][kind] = image.colorspace_settings.name
    return image


images = {kind: load(kind) for kind in P["maps"]}


def node_for(tree, bsdf, label, row):
    for existing in tree.nodes:
        if existing.type == "TEX_IMAGE" and existing.label == label:
            return existing
    node = tree.nodes.new("ShaderNodeTexImage")
    node.label = label
    node.location = (bsdf.location.x - 600, bsdf.location.y - row * 300)
    return node


def principled(tree):
    """The glTF importer does not always leave the node called 'Principled
    BSDF' — an imported material can carry a renamed one, and looking it up by
    name only is how a texture run reports success having wired nothing."""
    node = tree.nodes.get("Principled BSDF")
    if node is not None and node.type == "BSDF_PRINCIPLED":
        return node
    for candidate in tree.nodes:
        if candidate.type == "BSDF_PRINCIPLED":
            return candidate
    return None


def wire(material):
    material.use_nodes = True
    tree = material.node_tree
    bsdf = principled(tree)
    if bsdf is None:
        return []
    done = []
    for row, (kind, image) in enumerate(sorted(images.items())):
        socket, label = P["sockets"][kind]
        if socket not in bsdf.inputs:
            continue
        node = node_for(tree, bsdf, label, row)
        node.image = image
        if kind == "normal":
            mapper = None
            for existing in tree.nodes:
                if existing.type == "NORMAL_MAP" and existing.label == "BGateNormalMap":
                    mapper = existing
                    break
            if mapper is None:
                mapper = tree.nodes.new("ShaderNodeNormalMap")
                mapper.label = "BGateNormalMap"
                mapper.location = (node.location.x + 300, node.location.y)
            mapper.inputs["Strength"].default_value = P["normal_strength"]
            tree.links.new(node.outputs["Color"], mapper.inputs["Color"])
            tree.links.new(mapper.outputs["Normal"], bsdf.inputs[socket])
        else:
            tree.links.new(node.outputs["Color"], bsdf.inputs[socket])
        if kind == "emission" and "Emission Strength" in bsdf.inputs:
            if not bsdf.inputs["Emission Strength"].default_value:
                bsdf.inputs["Emission Strength"].default_value = 1.0
        done.append(kind)

    # --- alpha: the link that decides whether glTF says OPAQUE ---------------
    #
    # MEASURED (Blender 4.5, io_scene_gltf2): the exporter reads the ALPHA
    # SOCKET'S GRAPH, not blend_method or surface_render_method. Both of those
    # were set every which way and the exported alphaMode never moved.
    #   Alpha unlinked                              -> (no alphaMode) = OPAQUE
    #   image Alpha -> BSDF Alpha                   -> alphaMode BLEND
    #   image Alpha -> Math GREATER_THAN -> Alpha    -> alphaMode MASK,
    #                                                   alphaCutoff = threshold
    # A keyed decal wants MASK: BLEND is order-dependent and makes a logo on a
    # cap sort against the cap it is glued to.
    base = None
    for existing in tree.nodes:
        if existing.type == "TEX_IMAGE" and existing.label == P["sockets"]["base_color"][1]:
            base = existing
            break
    if "Alpha" in bsdf.inputs:
        for link in list(bsdf.inputs["Alpha"].links):
            tree.links.remove(link)
        for existing in [n for n in tree.nodes if n.label == "BGateAlphaClip"]:
            tree.nodes.remove(existing)
        bsdf.inputs["Alpha"].default_value = 1.0
        if base is not None and P["alpha"] in ("clip", "blend"):
            if P["alpha"] == "clip":
                clip = tree.nodes.new("ShaderNodeMath")
                clip.label = "BGateAlphaClip"
                clip.operation = "GREATER_THAN"
                clip.inputs[1].default_value = P["alpha_cutoff"]
                clip.location = (bsdf.location.x - 250, bsdf.location.y - 400)
                tree.links.new(base.outputs["Alpha"], clip.inputs[0])
                tree.links.new(clip.outputs[0], bsdf.inputs["Alpha"])
            else:
                tree.links.new(base.outputs["Alpha"], bsdf.inputs["Alpha"])
            done.append("alpha:" + P["alpha"])

    # Viewport/EEVEE only — the export does not read these (see above), but a
    # material that looks opaque when a human opens the .blend to check is its
    # own bug report.
    for attr, value in (("surface_render_method",
                         "BLENDED" if P["alpha"] == "blend" else "DITHERED"),
                        ("blend_method",
                         {"blend": "BLEND", "clip": "CLIP"}.get(P["alpha"], "OPAQUE"))):
        try:
            setattr(material, attr, value)
        except Exception:
            pass
    try:
        material.use_backface_culling = bool(P["backface_cull"])
    except Exception:
        pass
    return done


meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]

# COUNTED BEFORE THE FALLBACK MATERIALS ARE INVENTED. The ambiguity that
# matters is skin/eye/mouth — surfaces somebody AUTHORED as different. Grey
# placeholders this call created a line later are not a distinction anybody
# made, and refusing over them would break every untextured layer.
authored = set()
for obj in meshes:
    for slot in obj.data.materials:
        if slot is not None:
            authored.add(slot.name)

for obj in meshes:
    # NO UVs MEANS NO TEXTURE. A layer modelled without an unwrap silently
    # ignores every map you give it, which looks exactly like the texture
    # having failed to generate.
    if not obj.data.uv_layers:
        bg_unwrap(obj)
        report["unwrapped"].append(obj.name)
    if not obj.data.materials:
        bg_mat(obj, obj.name + "_mat", (0.8, 0.8, 0.8))

slots = {}
for obj in meshes:
    for slot in obj.data.materials:
        if slot is not None:
            slots[slot.name] = slot
report["slots"] = sorted(slots)
report["authored"] = sorted(authored)

wanted = P["material"]
if wanted:
    chosen = [slots[wanted]] if wanted in slots else []
    if not chosen:
        # ONE NAMED MATERIAL THAT MATCHED NOTHING IS A FAILED RUN. It used to
        # texture nothing, export a copy of the input and report ok=True, which
        # is the most expensive kind of success there is.
        report["refused"] = "no_such_material"
        say("material %r is not on this model (slots: %s)"
            % (wanted, ", ".join(report["slots"]) or "none"))
        raise RuntimeError("material %r is not on this model; it has %s"
                           % (wanted, ", ".join(report["slots"]) or "no materials"))
elif len(authored) > 1 and not P["all_slots"]:
    # ONE IMAGE ON EVERY SLOT IS NOT TEXTURING, IT IS A STAMP. A body layer
    # with skin/eye/mouth slots got the identical map on all three and shipped.
    report["refused"] = "ambiguous_material"
    say("%d authored material slots and no material named" % len(authored))
    raise RuntimeError(
        "this model has %d material slots (%s) — name one with material=, or "
        "pass all_slots=True if the same maps really belong on every surface"
        % (len(authored), ", ".join(report["authored"])))
else:
    chosen = list(slots.values())

for material in chosen:
    kinds = wire(material)
    if kinds:
        report["materials"].append(material.name)
        report["wired"][material.name] = kinds

report["materials"] = sorted(set(report["materials"]))
say("wired %d material(s)" % len(report["materials"]))
'''


ALPHA_MODES = ("auto", "opaque", "clip", "blend")


def _has_transparency(path: Path) -> Optional[bool]:
    """Does this image actually carry transparent pixels? None = cannot tell.

    An RGBA file is not the same thing as a transparent one, and the difference
    decides whether the exported material claims MASK or OPAQUE. Guessing from
    the channel count marks every fully-opaque PNG as cut-out, which costs a
    render pass in the engine for nothing.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            if im.mode not in ("RGBA", "LA", "PA") and "transparency" not in im.info:
                return False
            alpha = im.convert("RGBA").getchannel("A")
            return alpha.getextrema()[0] < 255
    except Exception:                                          # noqa: BLE001
        return None


def apply_texture(model: str | os.PathLike[str],
                  image: str | os.PathLike[str] | None,
                  out_path: str | os.PathLike[str], *, material: str = "",
                  all_slots: bool = False,
                  roughness: str | os.PathLike[str] | None = None,
                  metallic: str | os.PathLike[str] | None = None,
                  normal: str | os.PathLike[str] | None = None,
                  emission: str | os.PathLike[str] | None = None,
                  normal_strength: float = 1.0,
                  alpha: str = "auto", alpha_cutoff: float = 0.5,
                  backface_cull: Optional[bool] = None, decal: bool = False,
                  timeout: int = 240) -> dict:
    """Put generated maps on a layer's material and re-export it.

    THE MISSING HALF OF THE LAYERED PATH. Measured on the first real character
    run: the assembled asset carried 21 materials and ZERO images — every
    surface a flat colour an agent typed, because nothing connected the image
    adapter to the 3D layers. A flat colour is a blocking-in tool; the shipped
    surface is a generated texture, conditioned on the same pinned references
    every 2D asset in the project is conditioned on.

    ``image`` is the albedo/base colour map and is what the one-argument call
    has always meant. The rest are optional and each drives its own BSDF input:

        roughness   how glossy, per texel. WITHOUT IT EVERY SURFACE IS THE
                    SAME PLASTIC: the kit types rough=0.6, metal=0.0, so cloth,
                    leather, skin and steel all ship as one 0.6 dielectric and
                    the only thing that varies across an asset is its colour.
        metallic    0 for dielectrics, 1 for bare metal. Rarely a gradient.
        normal      tangent-space normals, through a Normal Map node.
        emission    what glows. Feeds Emission Color and lifts strength to 1.

    Every one of those is DATA, not colour, and is loaded Non-Color — see
    load() in the script for what sRGB does to a roughness value. ``image`` and
    ``emission`` feed colour sockets and stay sRGB.

    ``alpha``   auto | opaque | clip | blend. ``auto`` inspects the base image
                and picks clip when it actually has transparent pixels — which
                is what makes a keyed decal export as alphaMode MASK instead of
                a solid rectangle of key colour glued over the cap.
    ``decal``   shorthand for a conformed logo: implies backface culling.

    ``material`` names ONE slot. It is required whenever the model carries more
    than one AUTHORED material, because the same map on every slot of a body
    layer paints skin, eyes and mouth with one image and calls it textured;
    pass all_slots=True to say you meant it. (Grey placeholder materials this
    call invents for a mesh that had none do not count — nobody authored that
    distinction.) A named material that matches no slot is a FAILURE
    (ok=False), not an empty ``textured`` list beside a cheerful ok=True.

    Meshes without UVs are unwrapped first — otherwise the map is attached and
    silently ignored, which reads as the texture having failed.
    """
    src = Path(model)
    if not src.is_file():
        raise FileNotFoundError(f"no such model: {src}")
    if alpha not in ALPHA_MODES:
        raise ValueError(f"alpha must be one of {ALPHA_MODES}, got {alpha!r}")

    supplied = {"base_color": image, "roughness": roughness,
                "metallic": metallic, "normal": normal, "emission": emission}
    maps: dict[str, dict] = {}
    for kind, given in supplied.items():
        if given in (None, ""):
            continue
        path = Path(given)
        if not path.is_file():
            label = "texture" if kind == "base_color" else kind
            raise FileNotFoundError(f"no such {label} image: {path}")
        maps[kind] = {
            "path": str(path.resolve()),
            # sRGB ONLY WHERE THE DATA IS A COLOUR. Emission is a colour too;
            # loading it Non-Color would strip the transfer curve off pixels
            # that were authored with one and darken the glow.
            "colorspace": "sRGB" if kind in ("base_color", "emission") else "Non-Color",
        }
    if not maps:
        raise ValueError("apply_texture needs at least one map — pass an image, "
                         "or a roughness/metallic/normal/emission map")

    mode = alpha
    if mode == "auto":
        keyed = _has_transparency(Path(maps["base_color"]["path"])) if "base_color" in maps else False
        mode = "clip" if keyed else "opaque"

    inherited = read_layer_record(src)
    payload = {
        "model": str(src.resolve()),
        "material": material,
        "all_slots": bool(all_slots),
        "maps": maps,
        "sockets": {k: list(v) for k, v in _MAP_SOCKETS.items()},
        "alpha": mode,
        "alpha_cutoff": float(alpha_cutoff),
        "normal_strength": float(normal_strength),
        "backface_cull": bool(decal if backface_cull is None else backface_cull),
        # What glTF could not carry about this layer's rig, if the run that
        # exported it wrote the record down. See _RIG_SOURCE.
        "rest": inherited.get("armatures") or {},
    }
    script = ("import json\n" + _RIG_SOURCE + "\n" +
              _TEXTURE_SCRIPT.replace("__PAYLOAD__", json.dumps(payload)))
    result = run_script(script, export_glb=str(out_path), timeout=timeout,
                        record=False)

    report = _marked(result, "BGATE_TEXTURE:")
    textured = report.get("materials") or []
    refused = report.get("refused") or ""
    ok = bool(result.get("ok")) and not refused and bool(textured)
    error = result.get("error")
    if not ok and not error:
        error = ("no material was textured — the model's slots are %s"
                 % (", ".join(report.get("slots") or []) or "empty"))

    got = {**result,
           "ok": ok,
           "error": error,
           "textured": textured,
           "unwrapped": report.get("unwrapped") or [],
           "slots": report.get("slots") or [],
           "authored": report.get("authored") or [],
           "wired": report.get("wired") or {},
           "maps": report.get("maps") or {},
           "colorspaces": report.get("colorspaces") or {},
           "alpha": mode,
           "refused": refused,
           "rig_restored": report.get("rig_restored") or {},
           "dropped_shapes": report.get("dropped_shapes") or [],
           "out_path": str(Path(out_path).resolve())}
    note = _artifact_note(out_path)
    if note:
        got["artifact_note"] = note
    if ok:
        # Carry the layer's own record forward. The textured .glb is what
        # combine() is handed, so without this the assembled manifest would
        # know the surface and forget the script that built the geometry.
        merge_layer_record(out_path, {
            "script": inherited.get("script", ""),
            "kit": inherited.get("kit", True),
            "textured_from": str(src.resolve()),
            "material": material,
            "textures": {kind: spec["path"] for kind, spec in maps.items()},
            "colorspaces": got["colorspaces"],
            "alpha": mode,
            "textured_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        })
    return got


# How much of a render may be pure white before it is called blown out, and how
# dark the mean may go before it is called black.
#
# THIS IS THE LOOK-BACK MADE MECHANICAL. Measured: four turnaround renders of a
# correctly-coloured model (purple 0.40/0.11/0.64, teal 0.04/0.72/0.74) came out
# white and pastel because the lights were far too hot, and were reported as
# finished without anybody opening them. The model was fine. The render was not,
# and nothing in the pipeline could tell the difference.
#
# THESE FRACTIONS ARE OF THE SUBJECT, NOT OF THE FRAME. Run over the whole
# image they measure the BACKGROUND: this rig paints an opaque linear
# (0.28, 0.29, 0.32) x 0.6, which encodes to about 114/255, and a humanoid
# covers 15-25% of a portrait frame. A figure blown to solid white therefore
# scored blown ~0.20 against a 0.35 threshold and PASSED, and mean could never
# fall to 24 because a pure-black render still scored ~97. So the verdict is
# taken from a matte pass rendered with film_transparent, over the pixels with
# alpha > 0 and nothing else.
BLOWN_FRACTION = 0.35
DARK_MEAN = 24.0

# Luma hides a railed channel: saturated (255, 0, 0) scores 76 and reads as
# perfectly exposed while every bit of red detail is gone. So a second term
# counts subject pixels with ANY channel at the ceiling.
CLIPPED_FRACTION = 0.5
CLIP_LEVEL = 250

# Below this share of the frame there is effectively nothing in shot. The
# failure it catches: a subject whose centre is metres off the world origin,
# framed by a camera that only ever centred Z, rendering an empty background
# that then passed every exposure test there was.
MIN_SUBJECT_FRACTION = 0.002

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

# THE IMPORTER'S OWN JUNK IS NOT THE SUBJECT. MEASURED (Blender 4.5): importing
# any .glb that carries an armature makes io_scene_gltf2 build a bone custom
# shape — a 2x2x2 "Icosphere" at the world ORIGIN, linked into the scene with
# hide_render False and NO PARENT. On a combine() product that is the only
# parentless mesh in the file, so the old pivot rotated it and nothing else; it
# also dragged the subject's bounding box back to the origin and rendered a grey
# ball over the top of the character.
shapes = set()
for rig in [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]:
    for posed in rig.pose.bones:
        if posed.custom_shape is not None:
            shapes.add(posed.custom_shape.name)
            posed.custom_shape.hide_render = True

meshes = [o for o in bpy.context.scene.objects
          if o.type == "MESH" and o.name not in shapes]
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
centre = Vector([(lo[i] + hi[i]) / 2.0 for i in range(3)])
height = max(hi[2] - lo[2], 1e-3)
width = max(hi[0] - lo[0], hi[1] - lo[1], 1e-3)
reach = max(height, width)

# The eight bbox corners RELATIVE TO THE CENTRE — the frustum fit below needs
# them rotated per angle, and only the offsets rotate.
corners = [Vector((x, y, z)) - centre
           for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]

pivot = bpy.data.objects.new("BGatePivot", None)
bpy.context.scene.collection.objects.link(pivot)
pivot.location = centre
# matrix_world is a cache: without this the inverse taken below is the identity
# of a pivot that has not moved yet, and every child gets displaced by the whole
# of `centre`. MEASURED on a subject centred at (3, -5): three of four frames
# came back empty because the model had been shoved to (6, -10).
bpy.context.view_layer.update()

# PARENT THE ANCESTOR, NOT THE MESH. Only parentless meshes used to join the
# pivot — and a combine() product re-imports under an "Assembled" root, with its
# deforming layers parented to an armature, so NO mesh is parentless, nothing
# joined the pivot, and all four "angles" came back byte-identical fronts. Walk
# up to whatever sits at the top, whatever type it is.
tops = []
for obj in meshes:
    top = obj
    while top.parent is not None:
        top = top.parent
    if top is not pivot and top.name not in [t.name for t in tops]:
        tops.append(top)
for obj in tops:
    obj.parent = pivot
    obj.matrix_parent_inverse = pivot.matrix_world.inverted()
bpy.context.view_layer.update()

res_x, res_y = P["size"]
cam_data = bpy.data.cameras.new("BGateCam")
# Pin the sensor fit: with AUTO, cam.angle means the horizontal FOV on a
# landscape render and the vertical one on a portrait render, and the frustum
# maths below would silently mean a different thing per resolution.
cam_data.sensor_fit = "HORIZONTAL"
cam_data.angle = math.radians(P["fov"])
fov_h = cam_data.angle
fov_v = 2.0 * math.atan(math.tan(fov_h / 2.0) * float(res_y) / float(res_x))
cam = bpy.data.objects.new("BGateCam", cam_data)
bpy.context.scene.collection.objects.link(cam)
cam.rotation_euler = (math.radians(90), 0, 0)
bpy.context.scene.camera = cam


def frame_at(degrees):
    """Camera distance that FITS the subject at this angle, from the centre.

    A rotated bounding box is wider than an axis-aligned one — up to 1.41x
    against the 1.15x of visible width the old fixed 2.4x reach bought — which
    is why the 45 degree view was the one that came back cropped.
    """
    turn = math.radians(degrees)
    cos_t, sin_t = math.cos(turn), math.sin(turn)
    half_w = max(abs(c.x * cos_t - c.y * sin_t) for c in corners)
    half_h = max(abs(c.z) for c in corners)
    depth = max(abs(c.x * sin_t + c.y * cos_t) for c in corners)
    fit = max(half_w / math.tan(fov_h / 2.0), half_h / math.tan(fov_v / 2.0))
    return fit * P["margin"] + depth


# Three-point lighting at MODEST energy, scaled to the subject, PLACED AROUND
# THE SUBJECT. Everything here used to be measured from the world origin with X
# hardcoded to 0, so a model exported centred on (3, -5, 0) — which is what any
# asset authored beside its neighbours looks like — was lit and framed several
# metres from where it actually was.
def lamp(name, offset, energy, size):
    data = bpy.data.lights.new(name, type="AREA")
    data.energy = energy
    data.size = size
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = centre + Vector(offset)
    direction = (centre - obj.location).normalized()
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    return obj


# ENERGY GOES AS REACH SQUARED. The lamps stand off at a distance proportional
# to reach, so irradiance falls as 1/reach**2 and a power that rose only
# linearly made exposure a function of how big the subject was: a 0.3 m prop
# took ~360 W/m2 where a 2 m character took ~16, a 20x swing, and the old
# max(reach, 1.0) clamp froze the power outright below 1 m while the lights
# kept closing in. Squared holds irradiance constant at every scale.
power = reach * reach
lamp("Key",  (-reach * 1.6, -reach * 1.8, reach), 220 * power, reach)
lamp("Fill", (reach * 1.8, -reach * 1.2, 0.0), 90 * power, reach * 1.4)
lamp("Rim",  (0.0, reach * 2.0, reach * 0.8), 140 * power, reach)

world = bpy.context.scene.world or bpy.data.worlds.new("BGateWorld")
bpy.context.scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes.get("Background")
if bg:
    bg.inputs[0].default_value = (0.28, 0.29, 0.32, 1.0)
    bg.inputs[1].default_value = 0.6

scene = bpy.context.scene
scene.render.engine = P["engine"]
scene.render.resolution_x, scene.render.resolution_y = res_x, res_y
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA"
try:
    # AgX, not Standard. Standard clips: anything over 1.0 lands on 255 and the
    # whole highlight range collapses into one flat white, which is exactly the
    # artefact these renders exist to catch. AgX rolls it off, so a hot light
    # costs contrast rather than erasing the model.
    scene.view_settings.view_transform = "AgX"
except Exception:
    try:
        scene.view_settings.view_transform = "Standard"
    except Exception:
        pass
try:
    scene.view_settings.exposure = P["exposure"]
except Exception:
    pass

written = []
for index, (label, degrees) in enumerate(P["angles"]):
    pivot.rotation_euler = (0, 0, math.radians(degrees))
    bpy.context.view_layer.update()
    distance = frame_at(degrees)
    cam.location = (centre.x, centre.y - distance, centre.z)
    bpy.context.view_layer.update()

    out = os.path.join(P["out_dir"], "%s_%s.png" % (P["stem"], label))
    scene.render.film_transparent = False
    scene.render.filepath = out
    bpy.ops.render.render(write_still=True)

    # The MATTE PASS: the same frame with no background, so the exposure
    # verdict is computed over the subject's own pixels. Judging the composite
    # measures the backdrop, which is a constant nobody is worried about.
    matte = os.path.join(P["out_dir"], "%s_%s_matte.png" % (P["stem"], label))
    scene.render.film_transparent = True
    scene.render.filepath = matte
    bpy.ops.render.render(write_still=True)
    scene.render.film_transparent = False

    written.append({"label": label, "degrees": degrees, "path": out,
                    "matte": matte, "distance": round(distance, 4)})

print("BGATE_TURNAROUND:" + json.dumps(
    {"renders": written, "reach": reach,
     "centre": [round(v, 4) for v in centre]}))
'''

# THE LABEL IS A CLAIM ABOUT THE FACE, NOT ABOUT THE PIVOT.
#
# The rig above is fixed: the camera stands at -Y looking toward +Y, and the
# PIVOT turns the subject by `degrees` about Z. So the label that renders the
# face is whichever rotation brings the base's forward axis round to -Y.
#
# The base faces +Y (see BG_FORWARD in _blender_base — Blender +Y is glTF -Z,
# which is what Godot calls forward), so that rotation is 180, not 0. These
# used to be 0/45/90/180 against a base that faced -Y; every angle here is the
# old one plus 180, which is exactly what a base turned to face the other way
# needs and nothing else. frame_at() is unchanged and cannot notice: the bbox
# offsets it fits to are symmetric about the centre, so frame_at(d + 180)
# returns the same distance frame_at(d) did.
#
# LEFT UNLABELLED ON PURPOSE: "side" is one side, the same one it always was —
# 270 shows the figure's right, because 90 did.
TURNAROUND_ANGLES = (("front", 180), ("threequarter", 225), ("side", 270),
                     ("back", 0))


def _exposure_report(path: Path) -> dict:
    """Is this render legible, or is it a white sheet nobody looked at?

    MEASURED OVER THE SUBJECT. Given a matte pass (film_transparent), the
    statistics run only over pixels with alpha > 0; given an image with no
    transparency at all, every pixel is the subject and the numbers mean what
    they always did. See BLOWN_FRACTION for why the difference decides the
    verdict rather than shading it.
    """
    try:
        from PIL import Image
    except ImportError:
        return {"checked": False}
    try:
        with Image.open(path) as im:
            pixels = list(im.convert("RGBA").getdata())
    except Exception as exc:                                   # noqa: BLE001
        return {"checked": False, "error": str(exc)}
    if not pixels:
        return {"checked": False}

    frame = len(pixels)
    subject = [(r, g, b) for r, g, b, a in pixels if a > 0]
    covered = len(subject) / frame
    if not subject:
        return {"checked": True, "blown": 0.0, "clipped": 0.0, "mean": 0.0,
                "subject": 0.0, "ok": False,
                "verdict": "empty — nothing is in frame; the camera is not "
                           "looking at the subject"}

    # ITU-R 601, which is what PIL's own convert('L') uses — the thresholds
    # here were calibrated against those numbers.
    luma = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in subject]
    total = len(subject)
    blown = sum(1 for v in luma if v >= CLIP_LEVEL) / total
    clipped = sum(1 for r, g, b in subject if max(r, g, b) >= CLIP_LEVEL) / total
    mean = sum(luma) / total

    verdict = ""
    if covered < MIN_SUBJECT_FRACTION:
        verdict = (f"almost nothing in frame — the subject covers {covered:.2%} "
                   "of it; the camera is framing empty space")
    elif blown >= BLOWN_FRACTION:
        verdict = (f"blown out — {blown:.0%} of the SUBJECT is pure white; the "
                   "lights are too hot and the colours in the model are not "
                   "what you are looking at")
    elif clipped >= CLIPPED_FRACTION:
        verdict = (f"clipped — {clipped:.0%} of the subject has a colour channel "
                   "railed at full; that detail is gone even where the overall "
                   "brightness looks reasonable")
    elif mean <= DARK_MEAN:
        verdict = f"too dark to read — mean subject luminance {mean:.0f}/255"
    return {"checked": True, "blown": round(blown, 3), "clipped": round(clipped, 3),
            "mean": round(mean, 1), "subject": round(covered, 4),
            "ok": not verdict, "verdict": verdict}


def turnaround(model: str | os.PathLike[str], out_dir: str | os.PathLike[str], *,
               stem: str = "turnaround", angles=TURNAROUND_ANGLES,
               size=(640, 960), engine: str = "BLENDER_EEVEE_NEXT",
               exposure: float = 0.0, fov: float = 40.0, margin: float = 1.25,
               timeout: int = 480) -> dict:
    """Render a model from N angles under a fixed three-point rig, and JUDGE it.

    Every agent that renders a turnaround invents its own camera and lights, and
    the failure mode is always the same direction: too much light, a white
    figure, and a report of success written without opening the file. The rig
    here is scaled to the subject's own bounding box, PLACED AROUND ITS CENTRE,
    and every frame comes back with a blown-out/too-dark verdict attached,
    measured over the subject rather than over the backdrop.

    Each angle writes two files: ``path``, the frame to look at, and ``matte``,
    the same frame on transparent film that the verdict is computed from.

    ``fov``      horizontal field of view in degrees; the distance per angle is
                 fitted to it, so the rotated three-quarter view does not crop.
    ``margin``   headroom on that fit. 1.0 is edge-to-edge.
    ``angles``   ``[(label, degrees), ...]``. The degrees turn the SUBJECT, not
                 the camera, and "front" is 180 because the base faces +Y — see
                 TURNAROUND_ANGLES. Pass your own only for a model that does
                 not follow the base's facing.

    ``ok`` is False when any frame fails its exposure check — which is the whole
    point: a render nobody can read must not pass as a finished one. When it is
    False, ``error`` SAYS WHICH FRAMES AND WHY: an ok=False carrying error=None
    is rewritten downstream into "the call failed without stating a reason",
    which turns the one mechanical stop-signal this function has into what looks
    like a broken tool.
    """
    src = Path(model)
    if not src.is_file():
        raise FileNotFoundError(f"no such model: {src}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    payload = {"model": str(src.resolve()), "out_dir": str(out.resolve()),
               "stem": stem, "engine": engine, "exposure": float(exposure),
               "fov": float(fov), "margin": float(margin),
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
        matte = Path(entry.get("matte") or entry["path"])
        renders.append({**entry, "exists": path.is_file(),
                        **_exposure_report(matte if matte.is_file() else path)})
    unreadable = [r for r in renders if r.get("checked") and not r.get("ok")]
    ok = bool(result.get("ok")) and not unreadable and bool(renders)

    error = result.get("error")
    if not ok and not error:
        if not renders:
            error = ("no frames came back — the render script produced no "
                     "turnaround report")
        else:
            error = "%d of %d frames unreadable: %s" % (
                len(unreadable), len(renders),
                "; ".join("%s: %s" % (r.get("label") or "?",
                                      r.get("verdict") or "no verdict")
                          for r in unreadable))
    return {**result, "renders": renders, "unreadable": unreadable,
            "centre": report.get("centre") or [], "ok": ok, "error": error}


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

    # Each layer's own record, read BEFORE the run: it carries the bone tails
    # glTF threw away, and the import inside the script needs them in hand.
    records = {part["name"]: read_layer_record(part["path"]) for part in checked}
    sent = [{**part, "rest": records[part["name"]].get("armatures") or {}}
            for part in checked]

    payload = {"root": root_name, "parts": sent, "rig": rig,
               "decal_offset": DECAL_OFFSET,
               "decal_offset_ratio": DECAL_OFFSET_RATIO,
               "decal_edge_ratio": DECAL_EDGE_RATIO,
               "decal_tolerance_ratio": DECAL_TOLERANCE_RATIO,
               "decal_max_subdivisions": DECAL_MAX_SUBDIVISIONS}
    script = _RIG_SOURCE + "\n" + _COMBINE_SCRIPT.replace("__PAYLOAD__",
                                                          json.dumps(payload))

    result = run_script(script, export_glb=str(out_path), timeout=timeout,
                        record=False)

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
        # Whatever the layer's own file remembers about how it was made: the
        # bpy script run_script exported it from, and the maps apply_texture
        # put on it. Folded in HERE, because after a sweep the layer file is
        # gone and this manifest is the only surviving copy.
        record = records[part["name"]]
        layers.append({
            "name": part["name"],
            # The file this layer came from, kept so a sweep knows exactly
            # which intermediates belong to THIS run and a re-run knows which
            # single layer to rebuild.
            "source": part["path"],
            # THE PLACEMENT ARGUMENTS, not just the file. Without these the
            # manifest cannot rebuild the asset: a layer put back at the origin,
            # unrotated and unscaled, is a different asset.
            "at": part["at"],
            "rotate": part["rotate"],
            "scale": part["scale"],
            "bind": part["bind"],
            "objects": objects,
            "tris": sum(int(stats.get(n, {}).get("tris") or 0) for n in objects),
            "meshes": sum(1 for n in objects if stats.get(n, {}).get("type") == "MESH"),
            "bound": bound.get(part["name"], "none"),
            "decal_on": part["decal_on"],
            "imported": bool(objects),
            "is_rig": part["name"] == rig,
            "textures": record.get("textures") or {},
            "textured_from": record.get("textured_from") or "",
            "script": record.get("script") or "",
        })

    missing = [layer["name"] for layer in layers if not layer["imported"]]
    if missing:
        warnings.append(f"layer(s) imported nothing: {', '.join(missing)}")

    checks = list(report.get("checks") or [])
    checks += _promoted_issues(result, owned, warnings)
    ok, blocking = _shippable(report, checks, warnings)

    got = {**result,
           "parts": layers,
           "armature": report.get("armature") or "",
           "checks": checks,
           "notes": report.get("notes") or [],
           "warnings": warnings,
           "materials": report.get("materials") or [],
           "images": report.get("images") or [],
           "rig": rig,
           "root_name": root_name,
           "ok": bool(result.get("ok")) and ok,
           "layers": len(layers)}
    if blocking and not got.get("error"):
        got["error"] = ("assembled, but the result is not an asset: "
                        + " and ".join(blocking))
    note = _artifact_note(out_path)
    if note:
        got["artifact_note"] = note
    got["manifest"] = write_manifest(out_path, got, recipe=checked)
    return got


# The wording turnaround's caller already uses when frames land outside the
# project. SAME SENTENCE ON PURPOSE: an assembled .glb and a textured layer are
# registered by exactly the same machinery, fail to register for exactly the
# same reason, and used to say nothing at all about it — so an agent got a
# cheerful ok=True over an asset no reviewer would ever be shown.
ARTIFACT_NOTE = (
    "no artifact can be registered for this file — out_path is outside the "
    "project root, so art QA and the dashboard cannot see it; write it into "
    "the project to put it on the ledger")


def _artifact_note(out_path: str | os.PathLike[str]) -> str:
    """``ARTIFACT_NOTE`` when this output cannot be put on the ledger, else "".

    Containment is decided the way the rest of Builders Gate decides it —
    BGATE_ROOT if the caller set one, otherwise the nearest ``.bgate`` walking
    up from the file's own directory. Never raises and never guesses: with
    bgate_core absent the adapter is being used standalone, there is no ledger
    to miss, and a note about one would be noise.
    """
    try:
        from bgate_core.db import resolve_root
    except Exception:                                          # noqa: BLE001
        return ""
    try:
        target = Path(out_path).resolve()
        hint = os.environ.get("BGATE_ROOT") or ""
        root = Path(hint).resolve() if hint else resolve_root(target.parent)
        if root is not None and (root == target.parent or root in target.parents):
            return ""
    except (OSError, ValueError):                              # noqa: BLE001
        return ""
    return ARTIFACT_NOTE


# The runner already measures these on every run (_blender_runner._game_readiness)
# and combine() used to drop them on the floor: `result` was merged wholesale so
# `issues` was PRESENT in the return, but nothing put them in `checks` and
# nothing put them in `warnings`, which are the two keys a caller reads. MEASURED:
# an end-to-end run reported ok=True, checks=[], warnings=[] over a .glb whose
# materials were None and whose image and texture counts were both zero.
_PROMOTED = {
    "no_material": "every surface will import as default grey",
    "no_uv": "cannot be textured at all — the map attaches and is ignored",
    "unapplied_scale": "the engine sees the wrong dimensions",
    "non_uniform_scale": "shears children and normals",
    "ngons": "triangulates unpredictably per exporter",
}


def _promoted_issues(result: dict, owned: dict, warnings: list[str]) -> list[dict]:
    """Lift the runner's game-readiness issues into checks, named by layer."""
    layer_of = {name: layer for layer, names in (owned or {}).items() for name in names}
    promoted: list[dict] = []
    counts: dict[str, int] = {}
    for issue in result.get("issues") or []:
        kind = issue.get("issue")
        if kind not in _PROMOTED:
            continue
        obj = issue.get("object") or ""
        counts[kind] = counts.get(kind, 0) + 1
        promoted.append({
            "layer": layer_of.get(obj, ""),
            "object": obj,
            "check": kind,
            "count": issue.get("count"),
            "detail": issue.get("detail") or _PROMOTED[kind],
            "fix": issue.get("fix") or "",
        })
    for kind, count in sorted(counts.items()):
        warnings.append(f"{count} object(s): {kind} — {_PROMOTED[kind]}")
    return promoted


def _shippable(report: dict, checks: list[dict], warnings: list[str]):
    """A grey untextured blob is not a finished asset. Refuse to call it one."""
    materials = report.get("materials") or []
    images = report.get("images") or []
    blocking: list[str] = []
    if not materials:
        blocking.append("the assembled scene has no materials at all")
        checks.append({"layer": "", "object": "", "check": "no_materials",
                       "detail": "nothing in the assembly carries a material",
                       "fix": "assign materials per layer, or run apply_texture"})
    if not images:
        blocking.append("no image texture reaches any material")
        checks.append({"layer": "", "object": "", "check": "no_textures",
                       "detail": "every surface is a flat colour somebody typed",
                       "fix": "generate a texture and apply_texture it onto the layer"})
    warnings.extend(blocking)
    return (not blocking), blocking


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

# What ONE layer file remembers about itself, written beside it. Distinct from
# the assembled asset's manifest on purpose: this sits next to an intermediate,
# survives being read into the manifest, and is what lets the texture step hand
# the geometry step's script forward instead of ending the chain.
LAYER_RECORD_SUFFIX = ".bgate.json"


def manifest_path(out_path: str | os.PathLike[str]) -> Path:
    target = Path(out_path)
    return target.with_name(target.name + MANIFEST_SUFFIX)


def layer_record_path(out_path: str | os.PathLike[str]) -> Path:
    target = Path(out_path)
    return target.with_name(target.name + LAYER_RECORD_SUFFIX)


def read_layer_record(out_path: str | os.PathLike[str]) -> dict:
    """What this layer file remembers, or {}. Never raises."""
    try:
        path = layer_record_path(out_path)
        if not path.is_file():
            return {}
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def merge_layer_record(out_path: str | os.PathLike[str], fields: dict) -> str:
    """Update a layer's record in place. Never raises — a record that fails to
    write must not fail an export that succeeded."""
    doc = read_layer_record(out_path)
    doc.update({k: v for k, v in fields.items() if v not in (None, "")})
    doc["file"] = Path(out_path).name
    try:
        path = layer_record_path(out_path)
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return str(path)
    except OSError:
        return ""


def write_manifest(out_path: str | os.PathLike[str], result: dict,
                   *, recipe: Optional[list] = None) -> str:
    """Record the run beside its output. Never raises — a manifest that fails
    to write must not fail an asset that assembled.

    THIS IS THE RE-RUN, NOT A RECEIPT. The old manifest recorded a layer's
    name, objects, tri count and source file and nothing else — so the sentence
    "re-run one layer later" was false in both directions. The placement
    arguments (``at``, ``rotate``, ``scale``, ``bind``), which layer held the
    rig, what the assembly root was called and which images went onto which
    surface were all discarded, and then sweep() deleted the sources. What
    survived could not rebuild the asset and could not even say what it had
    been. ``combine`` is now recoverable from ``recipe`` alone — see
    manifest_recipe().
    """
    target = Path(out_path)
    layers = result.get("parts") or []
    doc = {
        "asset": target.name,
        "assembled_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "armature": result.get("armature") or "",
        "rig": result.get("rig") or "",
        "root_name": result.get("root_name") or "",
        "ok": bool(result.get("ok")),
        # The exact normalised argument list combine() ran. Feed it back with
        # rig/root_name and you get this asset again.
        "recipe": {
            "root_name": result.get("root_name") or "",
            "rig": result.get("rig") or "",
            "parts": list(recipe if recipe is not None else []),
        },
        "layers": [{"name": p["name"], "objects": p["objects"], "tris": p["tris"],
                    "bound": p["bound"], "decal_on": p["decal_on"],
                    "source": p.get("source", ""),
                    "at": p.get("at") or [0.0, 0.0, 0.0],
                    "rotate": p.get("rotate") or [0.0, 0.0, 0.0],
                    "scale": p.get("scale", 1.0),
                    "bind": p.get("bind", "none"),
                    "is_rig": bool(p.get("is_rig")),
                    "textures": p.get("textures") or {},
                    "textured_from": p.get("textured_from") or "",
                    "script": p.get("script") or ""}
                   for p in layers],
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


def manifest_recipe(out_path: str | os.PathLike[str]) -> dict:
    """The combine() call that produced this asset, read back off its manifest.

    Returns ``{parts, rig, root_name, missing}``. ``missing`` names the layer
    sources that are no longer on disk — after a sweep that is most of them,
    which is the point: each entry carries the script that built it, so a layer
    can be rebuilt and the assembly re-run rather than guessed at.
    """
    path = manifest_path(out_path)
    if not path.is_file():
        raise FileNotFoundError(f"no manifest beside {Path(out_path).name}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    recipe = doc.get("recipe") or {}
    parts = list(recipe.get("parts") or [])
    if not parts:
        # Pre-recipe manifests, and any manifest whose recipe was lost: rebuild
        # the argument list from the layer records instead of returning nothing.
        parts = [{"name": layer.get("name", ""), "path": layer.get("source", ""),
                  "at": layer.get("at") or [0.0, 0.0, 0.0],
                  "rotate": layer.get("rotate") or [0.0, 0.0, 0.0],
                  "scale": layer.get("scale", 1.0),
                  "bind": layer.get("bind", "none"),
                  "decal_on": layer.get("decal_on", "")}
                 for layer in doc.get("layers") or []]
    by_name = {layer.get("name", ""): layer for layer in doc.get("layers") or []}
    missing = []
    for part in parts:
        source = part.get("path") or ""
        if source and not Path(source).is_file():
            layer = by_name.get(part.get("name", ""), {})
            missing.append({"name": part.get("name", ""), "source": source,
                            "script": layer.get("script") or "",
                            "textures": layer.get("textures") or {}})
    return {"parts": parts,
            "rig": recipe.get("rig") or doc.get("rig") or "",
            "root_name": recipe.get("root_name") or doc.get("root_name") or "",
            "missing": missing}


def _confine(root: Path, candidate: Path) -> str:
    """Refuse a path that is not inside ``root``. Raises ValueError if it is not.

    Uses the registry's own normaliser so "inside the tree" means exactly what
    it means everywhere else in Builders Gate — including the resolution of
    ``..``, symlinks and drive-relative Windows paths, which is precisely where
    a hand-rolled ``str.startswith`` check leaks.
    """
    try:
        from bgate_core.assets import normalize_path
    except Exception:                                          # noqa: BLE001
        # bgate_core absent (adapter used standalone): fall back to the same
        # rule, resolved. Never fall back to no rule at all.
        resolved = candidate.resolve()
        if root not in resolved.parents:
            raise ValueError(f"{candidate} is outside the asset's tree {root}")
        return str(resolved.relative_to(root)).replace("\\", "/")
    return normalize_path(root, candidate)


def sweep(out_path: str | os.PathLike[str], *, dry_run: bool = False,
          keep_renders: bool = True) -> dict:
    """Remove a run's intermediate layer files, keeping the asset and the record.

    Reads the manifest written beside the assembled asset, so it deletes what
    THAT RUN produced and nothing else — a sweep that globs a directory takes
    the neighbouring asset's layers with it.

    Kept always: the assembled file, its manifest, the RIG layer, and (by
    default) renders. Removed: the per-layer sources listed in the manifest,
    and only those that sit INSIDE the assembled asset's own directory tree and
    carry a layer suffix. Everything else comes back under ``refused`` with the
    reason and is left alone — a manifest is an ordinary JSON file, and an
    unconfined unlink() of absolute paths read out of one is an arbitrary-delete
    primitive pointed wherever its last editor liked.

    What was removed, and what was refused, is written back into the manifest,
    so the run's history survives its files.
    """
    target = Path(out_path)
    path = manifest_path(target)
    if not path.is_file():
        raise FileNotFoundError(
            f"no manifest beside {target.name} — sweep only removes what a "
            f"recorded run produced, so there is nothing safe to do here")
    doc = json.loads(path.read_text(encoding="utf-8"))

    # THE TREE THIS SWEEP MAY TOUCH. Every candidate below is an absolute path
    # read back off a JSON file, and a manifest is an ordinary file an agent can
    # write. Unconfined, that made sweep an arbitrary-file-delete primitive
    # aimed by whoever last edited the manifest, with dry_run the only guard.
    root = target.resolve().parent
    protected = {target.resolve(), path.resolve()}
    rig = (doc.get("recipe") or {}).get("rig") or doc.get("rig") or ""

    removable, kept, refused = [], [], []

    def refuse(source: str, why: str) -> None:
        refused.append({"source": source, "reason": why})

    for layer in doc.get("layers") or []:
        source = layer.get("source") or ""
        if not source:
            continue
        candidate = Path(source)
        name = layer.get("name") or ""
        # THE RIG STAYS. It is the one layer that is not an intermediate: every
        # other layer is bound TO it, so a rebuilt layer with a deleted armature
        # can never be re-combined and the character is finished forever.
        if name and (name == rig or layer.get("is_rig")):
            kept.append(str(candidate))
            continue
        if not candidate.is_file():
            continue
        if candidate.resolve() in protected:
            continue
        if keep_renders and candidate.suffix.lower() in (".png", ".jpg", ".jpeg"):
            kept.append(str(candidate))
            continue
        # OUT OF TREE IS OUT OF BOUNDS. A shared models/base_human.blend passed
        # as a layer is not this asset's intermediate — swept once, it is gone
        # for every other asset that was built on it.
        try:
            _confine(root, candidate)
        except ValueError as exc:
            refuse(str(candidate), str(exc))
            continue
        if candidate.suffix.lower() not in COMBINE_SUFFIXES:
            # combine() only ever accepts these, so anything else in a `source`
            # field was put there by hand and is not a layer.
            refuse(str(candidate),
                   f"{candidate.suffix or 'a suffixless file'} is not a layer "
                   f"source ({', '.join(COMBINE_SUFFIXES)})")
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
        doc["refused"] = refused
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
            "refused": refused,
            "root": str(root),
            "bytes_freed": freed}
