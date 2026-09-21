"""Retarget a clip pack onto a character IN THE ENGINE, and keep the library.

blender_animate can already retarget a library clip - but by re-baking it
onto the rig in Blender, one export per clip set, and never a root-motion
clip (the pack's `root` bone is not a rig bone). Godot has its own retarget:
a BoneMap on the IMPORT of both files maps every bone to
SkeletonProfileHumanoid and the rest fixer rewrites both skeletons' rests
into the profile's frame, after which any clip from one drives the other.
This module drives that path and ends with a saved AnimationLibrary the
character's AnimationPlayer can add - no Blender, every pack clip, root
motion kept on the profile's Root.

  retarget_library(project, character_res, source, clips, name)
    1. the source pack (an animlib pack name, or a .gltf/.glb) is copied
       into assets/animlib/ and imported,
    2. a BoneMap .tres is written for each side (animlib.bone_map names the
       pack's bones; the character's are already profile names),
    3. both .import files get retarget/bone_map + the rest fixer, reimport,
    4. a Godot script copies the wanted clips out of the imported source,
       rewrites their track paths onto the character's Skeleton3D, drops
       tracks for bones the character lacks, saves the AnimationLibrary,
       ADDS IT TO THE CHARACTER IN MEMORY AND PLAYS EVERY CLIP: `drives`
       is whether the hands and feet left their rest - the only proof a
       retarget took, because a clip whose paths resolve to nothing plays
       happily and moves zero.

The library is `res://assets/anim/<name>.res` with a `<name>.res.json`
sidecar naming its clips, which godot_character_wire reads to resolve
roles without opening the engine again.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Optional

from bgate_adapters import animlib as _animlib
from bgate_adapters import godot as _godot

RETARGET_MARK = "BGATE_CLIPRETARGET:"

# The importer's retarget block. overwrite_axis is what makes two skeletons
# with different rests (a T-posed pack, an A-posed rig) share a frame;
# normalize_position_tracks scales Hips travel by each skeleton's own
# motion_scale so a 1.8 m clip walks a 1.2 m character at its own stride.
REST_FIXER = {
    "retarget/rest_fixer/apply_node_transforms": True,
    "retarget/rest_fixer/normalize_position_tracks": True,
    "retarget/rest_fixer/reset_all_bone_poses_after_import": True,
    "retarget/rest_fixer/overwrite_axis": True,
    "retarget/rest_fixer/keep_global_rest_on_leftovers": False,
    # THE SILHOUETTE FIX IS THE RETARGET. overwrite_axis only rewrites bone
    # axes; with the silhouette fix off, an A-posed rig played a T-posed
    # pack with its arms straight up - every clip "drove" the limbs and
    # the engine capture showed a Y-pose. On, both rests are bent to the
    # profile's reference silhouette before any clip is compared.
    "retarget/rest_fixer/fix_silhouette/enable": True,
    "retarget/rest_fixer/fix_silhouette/threshold": 15.0,
    "retarget/rest_fixer/fix_silhouette/base_height_adjustment": 0.0,
    "retarget/rest_fixer/remove_tracks/except_bone_transform": False,
    "retarget/rest_fixer/remove_tracks/unimportant_positions": True,
    "retarget/rest_fixer/remove_tracks/unmapped_bones": False,
}


def bone_map_tres(mapping: dict) -> str:
    """A BoneMap resource: profile bone -> this skeleton's bone name.

    `mapping` is {skeleton_bone: profile_bone} (animlib.bone_map's shape);
    a profile bone mapped twice keeps the first. The profile is embedded as
    a sub-resource so the file stands alone."""
    inverse: dict = {}
    for skel_bone, profile_bone in mapping.items():
        inverse.setdefault(profile_bone, skel_bone)
    lines = ['[gd_resource type="BoneMap" load_steps=2 format=3]', "",
             '[sub_resource type="SkeletonProfileHumanoid" id="SkeletonProfileHumanoid_1"]',
             "", "[resource]", 'profile = SubResource("SkeletonProfileHumanoid_1")']
    for profile_bone in sorted(inverse):
        lines.append(f'bone_map/{profile_bone} = {json.dumps(inverse[profile_bone])}')
    return "\n".join(lines) + "\n"


def profile_identity(bone_names) -> dict:
    """Bones that already carry profile names map to themselves; `root` in
    any case maps to Root so a root-motion track has somewhere to land."""
    names = set(_animlib._PROFILE)
    out = {}
    for b in bone_names or []:
        if b in names:
            out[b] = b
        elif b.lower() == "root":
            out[b] = "Root"
    return out


def gltf_bone_names(path: str | Path) -> list:
    """Every joint name in a .gltf/.glb, without the engine."""
    doc, _ = _animlib.read_gltf(path)
    joints = set()
    for skin in doc.get("skins") or []:
        joints.update(skin.get("joints") or [])
    nodes = doc.get("nodes") or []
    if not joints:
        return [n.get("name", "") for n in nodes if n.get("name")]
    return [nodes[i].get("name", "") for i in sorted(joints) if i < len(nodes)]


def _copy_source(project: Path, source: str, dest_rel: str) -> dict:
    """The pack (or file) into the project. A .gltf drags its .bin along."""
    packs = _animlib.status().get("packs") or {}
    if source in packs:
        info = packs[source]
        if not info.get("fetched"):
            return {"ok": False, "error": "pack %r is not fetched - the owner runs "
                                          "`%s`; no tool downloads" % (source, info.get("fetch"))}
        src = Path(info["path"])
        naming = (_animlib.PACKS.get(source) or {}).get("naming", "auto")
        stem = source
    else:
        src = Path(source)
        naming = "auto"
        stem = src.stem
    if not src.is_file():
        return {"ok": False, "error": f"no source at {src}"}
    if src.suffix.lower() not in (".gltf", ".glb"):
        return {"ok": False, "error": "the source must be .gltf or .glb (a .fbx is "
                                      "converted by Blender first: blender_run + "
                                      "export_gltf)"}
    dest_dir = project / dest_rel
    dest_dir.mkdir(parents=True, exist_ok=True)
    # import_asset copies the file itself and keys the destination on the
    # FILENAME, so only the .bin sidecars are copied here and the source
    # keeps its own name.
    dest = dest_dir / src.name
    copied = []
    if src.suffix.lower() == ".gltf":
        doc, _ = _animlib.read_gltf(src)
        for buf in doc.get("buffers") or []:
            uri = buf.get("uri") or ""
            if uri and not uri.startswith("data:"):
                side = src.parent / uri
                if side.is_file():
                    shutil.copyfile(side, dest_dir / Path(uri).name)
                    copied.append(str(dest_dir / Path(uri).name))
    return {"ok": True, "path": src, "naming": naming, "copied": copied, "stem": stem,
            "rel": str(dest.relative_to(project)).replace("\\", "/")}


_PATHS_GD = '''extends SceneTree
const MARK := "__MARK__"
func _walk(n: Node, root: Node, out: Dictionary) -> void:
	if n is Skeleton3D and not out.has("skeleton"):
		out["skeleton"] = String(root.get_path_to(n))
		out["first_bone"] = (n as Skeleton3D).get_bone_name(0)
	if n is AnimationPlayer:
		out["clips"] = Array((n as AnimationPlayer).get_animation_list())
	for c in n.get_children():
		_walk(c, root, out)
func _init() -> void:
	var out := {}
	for p in __PATHS__:
		var packed = load(p)
		var rec := {}
		if packed != null and packed is PackedScene:
			var s: Node = packed.instantiate()
			_walk(s, s, rec)
			s.free()
		out[p] = rec
	print(MARK + JSON.stringify(out))
	quit()
'''


def scene_paths(project_dir, res_paths: list, timeout: int = 120) -> dict:
    """{res: {skeleton: path-from-root, first_bone, clips}} for imported
    scenes - the Skeleton3D path is where the importer keeps its retarget
    options, and it is whatever the file's armature was called."""
    script = (_PATHS_GD.replace("__MARK__", RETARGET_MARK + "PATHS:")
              .replace("__PATHS__", json.dumps([str(p) for p in res_paths])))
    ran = _godot.run_script(script, project_dir=str(project_dir), timeout=timeout)
    text = (ran.get("stdout") or "") + "\n" + (ran.get("stderr") or "")
    for line in text.splitlines():
        if RETARGET_MARK + "PATHS:" in line:
            try:
                return json.loads(line.split(RETARGET_MARK + "PATHS:", 1)[1])
            except json.JSONDecodeError:
                return {}
    return {}


def _loopless(name: str) -> str:
    low = str(name).lower()
    for suffix in ("_loop", "-loop"):
        if low.endswith(suffix):
            return low[:-len(suffix)]
    return low


_EXTRACT_GD = '''extends SceneTree
## Copy the retargeted clips out of the imported source, aim their tracks at
## the character's skeleton, save the library, then PLAY EACH ONE on the
## character and measure whether the limbs moved.
const MARK := "__MARK__"
var out := {"ok": false, "clips": []}
var stage := 0
var char_root: Node = null
var skel: Skeleton3D = null
var player: AnimationPlayer = null
var lib: AnimationLibrary = null
var names: Array = []
var probes := ["LeftHand", "RightHand", "LeftFoot", "RightFoot", "Head"]
var rest := {}
var current := ""
var wait := 0
var all_dropped := {}

func _find(node: Node, cls: String) -> Node:
	if node.is_class(cls):
		return node
	for c in node.get_children():
		var f := _find(c, cls)
		if f != null:
			return f
	return null

func _say() -> void:
	print(MARK + JSON.stringify(out))
	quit()

## The importer strips a "_Loop"/"-loop" suffix into loop_mode, so names are
## compared without it, lower-case.
func _loopless(n: String) -> String:
	var low := n.to_lower()
	for suffix in ["_loop", "-loop"]:
		if low.ends_with(suffix):
			return low.substr(0, low.length() - suffix.length())
	return low

func _initialize() -> void:
	var src_path := "__SRC__"
	var char_path := "__CHAR__"
	var lib_path := "__LIB__"
	var wanted: Array = __WANTED__
	var packed_src = load(src_path)
	var packed_char = load(char_path)
	if packed_src == null or not (packed_src is PackedScene):
		out["error"] = "source did not load: %s" % src_path
		_say(); return
	if packed_char == null or not (packed_char is PackedScene):
		out["error"] = "character did not load: %s" % char_path
		_say(); return
	var src_root: Node = packed_src.instantiate()
	char_root = packed_char.instantiate()
	get_root().add_child(src_root)
	get_root().add_child(char_root)
	var src_player := _find(src_root, "AnimationPlayer") as AnimationPlayer
	var src_skel := _find(src_root, "Skeleton3D") as Skeleton3D
	skel = _find(char_root, "Skeleton3D") as Skeleton3D
	player = _find(char_root, "AnimationPlayer") as AnimationPlayer
	if src_player == null or src_skel == null:
		out["error"] = "the source has no AnimationPlayer/Skeleton3D after import"
		_say(); return
	if skel == null:
		out["error"] = "the character has no Skeleton3D"
		_say(); return
	if player == null:
		player = AnimationPlayer.new()
		char_root.add_child(player)
		player.root_node = player.get_path_to(char_root)
	out["character_skeleton"] = String(skel.name)
	out["source_bones"] = src_skel.get_bone_count()
	out["character_bones"] = skel.get_bone_count()
	var char_bones := {}
	for i in skel.get_bone_count():
		char_bones[skel.get_bone_name(i)] = true
	var skel_rel := String(player.get_node(player.root_node).get_path_to(skel))
	lib = AnimationLibrary.new()
	var src_list := src_player.get_animation_list()
	for anim_name in src_list:
		var short := String(anim_name)
		if "/" in short:
			short = short.get_slice("/", 1)
		if wanted.size() > 0 and not (_loopless(short) in wanted):
			continue
		var src_anim: Animation = src_player.get_animation(anim_name)
		var anim: Animation = src_anim.duplicate(true)
		var kept := 0
		var dropped := []
		var root_motion := false
		for t in range(anim.get_track_count() - 1, -1, -1):
			var path := anim.track_get_path(t)
			if path.get_subname_count() == 0:
				anim.remove_track(t)
				continue
			var bone := String(path.get_subname(0))
			if not char_bones.has(bone):
				dropped.append(bone)
				anim.remove_track(t)
				continue
			anim.track_set_path(t, NodePath("%s:%s" % [skel_rel, bone]))
			kept += 1
			if bone == "Root" and anim.track_get_type(t) == Animation.TYPE_POSITION_3D \\
					and anim.track_get_key_count(t) > 1:
				var a: Vector3 = anim.track_get_key_value(t, 0)
				var b: Vector3 = anim.track_get_key_value(t, anim.track_get_key_count(t) - 1)
				if a.distance_to(b) > 0.01:
					root_motion = true
		var loops := short.ends_with("_Loop") or short.ends_with("-loop")
		if loops and anim.loop_mode == Animation.LOOP_NONE:
			anim.loop_mode = Animation.LOOP_LINEAR
		lib.add_animation(StringName(short), anim)
		names.append(short)
		for d in dropped:
			all_dropped[d] = true
		out["clips"].append({"name": short, "source": String(anim_name),
			"tracks": kept, "dropped": dropped.size(), "length": snappedf(anim.length, 0.001),
			"loop": anim.loop_mode != Animation.LOOP_NONE, "root_motion": root_motion,
			"drives": false, "moved_m": 0.0})
	if names.is_empty():
		out["error"] = "no clip matched %s among %s" % [wanted, src_list]
		_say(); return
	out["dropped_bones"] = all_dropped.keys()
	var err := ResourceSaver.save(lib, lib_path)
	out["saved"] = err == OK
	out["save_error"] = int(err)
	out["library"] = lib_path
	player.add_animation_library("bgate_probe", lib)
	skel.reset_bone_poses()
	for p in probes:
		var i := skel.find_bone(p)
		if i != -1:
			rest[p] = skel.get_bone_global_pose(i).origin
	src_root.queue_free()
	stage = 0
	wait = 2

func _process(_delta: float) -> bool:
	if lib == null:
		return true
	if wait > 0:
		wait -= 1
		return false
	if current != "":
		var moved := 0.0
		for p in rest:
			var i := skel.find_bone(p)
			moved = maxf(moved, skel.get_bone_global_pose(i).origin.distance_to(rest[p]))
		for c in out["clips"]:
			if c["name"] == current:
				c["moved_m"] = snappedf(moved, 0.0001)
				c["drives"] = moved > 0.02
		player.stop()
		skel.reset_bone_poses()
		current = ""
	if stage >= names.size():
		out["ok"] = true
		_say()
		return true
	current = names[stage]
	stage += 1
	var anim: Animation = lib.get_animation(StringName(current))
	player.play("bgate_probe/" + current)
	player.seek(anim.length * 0.4, true)
	wait = 3
	return false
'''


def retarget_library(project_dir, character_res: str, source: str, *,
                     clips: Optional[list] = None, name: str = "",
                     dest_rel: str = "assets/animlib", lib_rel: str = "assets/anim",
                     timeout: int = 300) -> dict:
    """See the module docstring. Returns {ok, library, sidecar, clips:[...],
    bone_maps, steps, warnings, error}."""
    project = Path(project_dir)
    if not (project / "project.godot").is_file():
        return {"ok": False, "error": f"no project.godot in {project}"}
    if not str(character_res).startswith("res://"):
        return {"ok": False, "error": "character_res must be a res:// path to the "
                                      "imported character (godot_import_asset / "
                                      "godot_character_wire put it there)"}
    char_rel = str(character_res)[len("res://"):]
    char_file = project / char_rel
    if not char_file.is_file():
        return {"ok": False, "error": f"no file at {character_res}"}
    steps, warnings = [], []

    got = _copy_source(project, source, dest_rel)
    if not got.get("ok"):
        return {"ok": False, "error": got["error"]}
    steps.append({"step": "copy", "ok": True, "copied": got["copied"]})
    src_rel = got["rel"]

    # Bone maps, written before the first import so the importer sees them.
    src_names = gltf_bone_names(got["path"])
    src_map = _animlib.bone_map(src_names, got.get("naming", "auto"))
    for n in src_names:
        if n.lower() == "root" and n not in src_map:
            src_map[n] = "Root"
    char_names = gltf_bone_names(char_file)
    char_map = profile_identity(char_names)
    if len(src_map) < 12:
        return {"ok": False, "steps": steps,
                "error": "only %d of the source's %d bones map onto the humanoid "
                         "profile - not a humanoid pack this tool can read "
                         "(mapped: %s)" % (len(src_map), len(src_names),
                                           sorted(src_map.values())[:8])}
    if len(char_map) < 12:
        return {"ok": False, "steps": steps,
                "error": "the character carries %d profile-named bones; an engine "
                         "retarget needs the humanoid names blender_rig writes"
                         % len(char_map)}
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", name or got["stem"])
    maps_dir = project / lib_rel
    maps_dir.mkdir(parents=True, exist_ok=True)
    src_map_file = maps_dir / f"{stem}_source_bonemap.tres"
    char_map_file = maps_dir / (Path(char_rel).stem + "_bonemap.tres")
    src_map_file.write_text(bone_map_tres(src_map), encoding="utf-8")
    char_map_file.write_text(bone_map_tres(char_map), encoding="utf-8")
    src_map_res = "res://" + str(src_map_file.relative_to(project)).replace("\\", "/")
    char_map_res = "res://" + str(char_map_file.relative_to(project)).replace("\\", "/")
    steps.append({"step": "bone_maps", "ok": True,
                  "source": {"path": src_map_res, "mapped": len(src_map),
                             "unmapped": [n for n in src_names if n not in src_map][:20]},
                  "character": {"path": char_map_res, "mapped": len(char_map)}})

    # Import the source once so its .import exists, then set the retarget on
    # BOTH files and reimport.
    first = _godot.import_asset(str(project), str(got["path"]), dest_rel=dest_rel,
                                timeout=timeout)
    steps.append({"step": "import_source", "ok": bool(first.get("ok")),
                  "errors": (first.get("import") or {}).get("errors", [])})
    if not first.get("ok"):
        return {"ok": False, "steps": steps, "error": "the engine could not import "
                                                     "the source pack", "detail": first}
    src_res = first["res_path"]
    # THE RETARGET BLOCK LIVES ON THE SKELETON NODE, not in the flat params:
    # written flat, every key is silently ignored and the pack imports with
    # its own bone names (measured: 53 DEF-* bones, 0 tracks kept).
    paths = scene_paths(project, [src_res, character_res], timeout=timeout)
    # THE KEY IS THE PRE-IMPORT PATH. A retarget renames the skeleton node
    # to GeneralSkeleton in the imported scene, so reading the path back off
    # an already-retargeted import and writing it as the key matched nothing
    # on the next import and silently undid the retarget (measured: bones
    # back to DEF-*, 0 tracks). The importer's raw name is Skeleton3D.
    def _raw(path):
        if not path:
            return path
        parts = path.split("/")
        if parts[-1] == "GeneralSkeleton":
            parts[-1] = "Skeleton3D"
        return "/".join(parts)
    src_skel = _raw((paths.get(src_res) or {}).get("skeleton"))
    char_skel = _raw((paths.get(character_res) or {}).get("skeleton"))
    if not src_skel or not char_skel:
        return {"ok": False, "steps": steps,
                "error": "could not find a Skeleton3D in %s"
                         % (src_res if not src_skel else character_res)}
    steps.append({"step": "skeleton_paths", "ok": True,
                  "source": src_skel, "character": char_skel,
                  "source_clips": (paths.get(src_res) or {}).get("clips")})
    for rel, map_res, skel_path in ((src_rel, src_map_res, src_skel),
                                    (char_rel, char_map_res, char_skel)):
        block = {"retarget/bone_map": _godot.RawLiteral(f'Resource("{map_res}")'),
                 **REST_FIXER}
        wrote = _godot.write_import_settings(
            str(project), rel, params={"animation/import": True},
            node_params={skel_path: block})
        steps.append({"step": "retarget_settings", "ok": bool(wrote.get("ok")),
                      "asset": rel, "node": skel_path, "error": wrote.get("error"),
                      "merge_error": wrote.get("merge_error")})
        if not wrote.get("ok"):
            return {"ok": False, "steps": steps, "error": wrote.get("error")}
    reimport = _godot.check_project(str(project), timeout=timeout)
    steps.append({"step": "reimport", "ok": bool(reimport.get("ok")),
                  "errors": (reimport.get("errors") or [])[:8]})

    lib_file = maps_dir / f"{stem}.res"
    lib_res = "res://" + str(lib_file.relative_to(project)).replace("\\", "/")
    wanted = json.dumps([_loopless(c) for c in (clips or [])])
    script = (_EXTRACT_GD.replace("__MARK__", RETARGET_MARK)
              .replace("__SRC__", src_res).replace("__CHAR__", character_res)
              .replace("__LIB__", lib_res).replace("__WANTED__", wanted))
    ran = _godot.run_script(script, project_dir=str(project), timeout=timeout)
    text = (ran.get("stdout") or "") + "\n" + (ran.get("stderr") or "")
    report = None
    for line in text.splitlines():
        if RETARGET_MARK in line:
            try:
                report = json.loads(line.split(RETARGET_MARK, 1)[1])
            except json.JSONDecodeError:
                report = None
    if report is None:
        return {"ok": False, "steps": steps,
                "error": ran.get("error") or "the extract script printed no report",
                "output": text[-1500:]}
    steps.append({"step": "extract", "ok": bool(report.get("ok")),
                  "error": report.get("error")})
    if not report.get("ok"):
        return {"ok": False, "steps": steps, "error": report.get("error")}
    if not report.get("saved"):
        return {"ok": False, "steps": steps, "report": report,
                "output": text[-2000:],
                "error": "ResourceSaver refused %s (error %s)"
                         % (lib_res, report.get("save_error"))}
    clip_rows = report.get("clips") or []
    dead = [c["name"] for c in clip_rows if not c.get("drives")]
    if dead:
        warnings.append("clips that did not move the character's limbs when "
                        "played (their tracks resolve to nothing, or the clip "
                        "is a hold): %s" % ", ".join(dead))
    dropped = sorted(report.get("dropped_bones") or [])
    if dropped:
        warnings.append("tracks dropped for bones the character lacks: %s"
                        % ", ".join(dropped[:16]))
    sidecar = lib_file.with_suffix(".res.json")
    sidecar.write_text(json.dumps({
        "library": lib_res, "name": stem, "character": character_res,
        "source": src_res, "clips": [c["name"] for c in clip_rows],
        "root_motion": [c["name"] for c in clip_rows if c.get("root_motion")],
        "skeleton": report.get("character_skeleton")}, indent=1), encoding="utf-8")
    alive = [c["name"] for c in clip_rows if c.get("drives")]
    return {"ok": bool(alive), "library": lib_res, "sidecar": str(sidecar),
            "name": stem, "clips": clip_rows, "drives": alive, "dead": dead,
            "root_motion": [c["name"] for c in clip_rows if c.get("root_motion")],
            "skeleton": report.get("character_skeleton"),
            "bone_maps": {"source": src_map_res, "character": char_map_res},
            "engine_errors": [l for l in text.splitlines() if "ERROR" in l][:10],
            "steps": steps, "warnings": warnings,
            "error": "" if alive else "no retargeted clip moved the character"}
