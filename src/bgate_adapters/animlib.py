"""Animation libraries — hand-keyed CC0 clip packs, fetched once, retargeted
onto any humanoid this pipeline rigs.

WHY. humanpose.py's procedural clips are CORRECT — feet planted, weight
right, every gate green — and they will always read as engine-generic: no
anticipation, no weight shift, nobody home. A pack keyed by an animator is
the difference between the character walking and the character being someone.
The retarget itself lives in blender.py's animate script (world-space rotation
deltas onto an aligned rest, through humanpose's own FK); this module is the
part that has to be right BEFORE Blender starts: where the pack is, whether it
is the bytes we pinned, what it contains, and what its bones are called.

THE CACHE IS GLOBAL, NOT PER PROJECT. ~/.bgate/animlib/<pack>/ — the same
reasoning as the global key store: a 4 MB pack follows the person, not the
game, and nothing in it belongs in anyone's repository. BGATE_HOME moves it.

FETCHING IS A CLI/OWNER ACTION, NOT AN AGENT TOOL. `bgate animlib fetch` is
the one download this module ever makes: a commit-pinned zip whose SHA-256 is
checked before a byte is unpacked. An MCP tool reports status and names the
command; it does not download, for the same reason no tool writes a key.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import struct
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
# Every entry names a COMMIT, never a branch: a branch is whatever it is today,
# and a pack whose bytes can change under a pinned hash is a pack whose hash
# means nothing. `file` is the one glTF (with its sibling .bin) that carries
# every clip; `naming` selects the bone map below.
PACKS: dict[str, dict] = {
    "quaternius-ual": {
        "title": "Quaternius — Universal Animation Library (Standard, glTF)",
        "license": "CC0-1.0",
        "author": "Quaternius (quaternius.com); glTF mirror by J-Ponzo",
        "source": "https://github.com/J-Ponzo/gltf-universal-animation-library",
        "commit": "e24c23cf2a1323488a3faa226ea7ea21f644b73e",
        "url": ("https://codeload.github.com/J-Ponzo/gltf-universal-animation-"
                "library/zip/e24c23cf2a1323488a3faa226ea7ea21f644b73e"),
        # Measured on the zip fetched 2026-09-03. A mismatch is a refusal.
        "sha256": "813d53f689f90dbef64645c06feb48f9cbf78ffe758cf68fc04a2a720868debd",
        "file": "glTF/AnimationLibrary_Godot_Standard.gltf",
        "naming": "rigify",
        "notes": ("46 clips: locomotion, crouch, jump, sit, pistol, sword, spell, "
                  "punch, hit, death, dance, swim, push. Names ending _Loop cycle; "
                  "_RM variants carry root motion on the `root` bone."),
    },
}

# ---------------------------------------------------------------------------
# Bone maps: a pack's names -> Godot's SkeletonProfileHumanoid names
# ---------------------------------------------------------------------------
# Exact tables, not fuzzy matching. Fingers are absent on purpose: no rig out
# of this pipeline has them, and a finger mapped onto nothing is noise in the
# report. Anything a table does not name is ignored at retarget time and
# listed under `unmapped` so a missing limb is a fact and not a mystery.
_RIGIFY_CORE = {
    "hips": "Hips", "spine.001": "Spine", "spine.002": "Chest",
    "spine.003": "UpperChest", "neck": "Neck", "head": "Head",
}
_RIGIFY_SIDE = {
    "shoulder": "Shoulder", "upper_arm": "UpperArm", "forearm": "LowerArm",
    "hand": "Hand", "thigh": "UpperLeg", "shin": "LowerLeg", "foot": "Foot",
    "toe": "Toes",
}
_MIXAMO_CORE = {
    "Hips": "Hips", "Spine": "Spine", "Spine1": "Chest", "Spine2": "UpperChest",
    "Neck": "Neck", "Head": "Head",
}
_MIXAMO_SIDE = {
    "Shoulder": "Shoulder", "Arm": "UpperArm", "ForeArm": "LowerArm",
    "Hand": "Hand", "UpLeg": "UpperLeg", "Leg": "LowerLeg", "Foot": "Foot",
    "ToeBase": "Toes",
}


def bone_map(names: list, naming: str = "auto") -> dict:
    """{source bone: profile bone} for the names a pack's skeleton carries.

    `naming` rigify (DEF-thigh.L), mixamo (mixamorig:LeftUpLeg / LeftUpLeg),
    godot (already profile names), or auto — which tries each and keeps the
    one that maps the most bones, so a pack that renames itself between
    versions (Quaternius did, at v2.0) still lands.
    """
    if naming == "auto":
        best: dict = {}
        for candidate in ("godot", "rigify", "mixamo"):
            got = bone_map(names, candidate)
            if len(got) > len(best):
                best = got
        return best
    out = {}
    for raw in names:
        name = str(raw)
        if naming == "godot":
            if name in _PROFILE:
                out[name] = name
            continue
        if naming == "rigify":
            stem = name[4:] if name.startswith("DEF-") else name
            if stem in _RIGIFY_CORE:
                out[name] = _RIGIFY_CORE[stem]
            elif stem[-2:] in (".L", ".R") and stem[:-2] in _RIGIFY_SIDE:
                side = "Left" if stem.endswith(".L") else "Right"
                out[name] = side + _RIGIFY_SIDE[stem[:-2]]
            continue
        if naming == "mixamo":
            stem = name.split(":", 1)[1] if ":" in name else name
            if stem in _MIXAMO_CORE:
                out[name] = _MIXAMO_CORE[stem]
            else:
                for side in ("Left", "Right"):
                    if stem.startswith(side) and stem[len(side):] in _MIXAMO_SIDE:
                        out[name] = side + _MIXAMO_SIDE[stem[len(side):]]
            continue
        raise ValueError(f"unknown bone naming {naming!r}")
    return out


_PROFILE = frozenset((
    "Root", "Hips", "Spine", "Chest", "UpperChest", "Neck", "Head",
    "LeftShoulder", "LeftUpperArm", "LeftLowerArm", "LeftHand",
    "RightShoulder", "RightUpperArm", "RightLowerArm", "RightHand",
    "LeftUpperLeg", "LeftLowerLeg", "LeftFoot", "LeftToes",
    "RightUpperLeg", "RightLowerLeg", "RightFoot", "RightToes",
))


# ---------------------------------------------------------------------------
# Where packs live
# ---------------------------------------------------------------------------

def home() -> Path:
    """~/.bgate/animlib, or under BGATE_HOME — the same override the aegis
    allowlist honours, so the cache is where the rest of ~/.bgate is."""
    base = os.environ.get("BGATE_HOME") or ""
    root = Path(base).expanduser() if base else Path.home() / ".bgate"
    return root / "animlib"


def pack_dir(name: str) -> Path:
    return home() / name


def pack_file(name: str) -> Optional[Path]:
    """The clip file on disk, or None when the pack is not fetched."""
    spec = PACKS.get(name)
    if not spec:
        return None
    root = pack_dir(name)
    if not root.is_dir():
        return None
    wanted = spec["file"].replace("\\", "/")
    # The zip unpacks to <repo>-<sha>/ (codeload) or <repo>-main/ (a branch
    # zip somebody fetched by hand); either is fine, the file is what matters.
    for hit in sorted(root.glob("*/" + wanted)):
        if hit.is_file():
            return hit
    direct = root / wanted
    return direct if direct.is_file() else None


def status(name: Optional[str] = None) -> dict:
    """What is fetched, what is not, and how to fetch it. Never downloads."""
    rows = {}
    for key, spec in PACKS.items():
        if name and key != name:
            continue
        path = pack_file(key)
        row = {"title": spec["title"], "license": spec["license"],
               "source": spec["source"], "commit": spec["commit"][:12],
               "fetched": path is not None,
               "path": str(path) if path else "",
               "fetch": f"bgate animlib fetch {key}"}
        if path is not None:
            try:
                row["clips"] = len(clips(key))
            except Exception as exc:  # a half-unpacked pack is a fact to show
                row["clips"] = 0
                row["error"] = f"{type(exc).__name__}: {exc}"
        rows[key] = row
    return {"home": str(home()), "packs": rows}


def fetch(name: str, *, force: bool = False, timeout: int = 120) -> dict:
    """Download the pinned zip, verify its SHA-256, unpack. THE ONE DOWNLOAD.

    A hash mismatch leaves nothing behind: a pack whose bytes are not the
    bytes that were reviewed is not a pack, it is an unknown file with a
    trusted name.
    """
    spec = PACKS.get(name)
    if not spec:
        return {"ok": False, "error": f"no such pack {name!r}; known: "
                                      f"{', '.join(sorted(PACKS))}"}
    existing = pack_file(name)
    if existing is not None and not force:
        return {"ok": True, "pack": name, "path": str(existing),
                "fetched": False, "note": "already fetched; force=True re-fetches"}
    root = pack_dir(name)
    try:
        with urllib.request.urlopen(spec["url"], timeout=timeout) as resp:
            data = resp.read()
    except Exception as exc:
        return {"ok": False, "error": f"download failed: {type(exc).__name__}: {exc}",
                "url": spec["url"]}
    digest = hashlib.sha256(data).hexdigest()
    if digest != spec["sha256"]:
        return {"ok": False, "error": "SHA-256 mismatch — the download is not "
                                      "the reviewed pack, nothing was unpacked",
                "expected": spec["sha256"], "got": digest, "url": spec["url"]}
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "pack.zip").write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.namelist():
            # No absolute paths, no traversal: a zip is a list of names
            # somebody else wrote.
            target = (root / member).resolve()
            if not str(target).startswith(str(root.resolve())):
                continue
            if member.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(member))
    (root / "PROVENANCE.json").write_text(json.dumps({
        "pack": name, "url": spec["url"], "sha256": digest,
        "commit": spec["commit"], "license": spec["license"],
        "source": spec["source"]}, indent=2), encoding="utf-8")
    path = pack_file(name)
    if path is None:
        return {"ok": False, "error": f"unpacked, but {spec['file']} is not in it"}
    return {"ok": True, "pack": name, "path": str(path), "fetched": True,
            "bytes": len(data), "sha256": digest, "clips": len(clips(name))}


# ---------------------------------------------------------------------------
# Reading a pack without Blender
# ---------------------------------------------------------------------------

def read_gltf(path: str | Path) -> tuple[dict, bytes]:
    """JSON + the first buffer, for a .glb OR a .gltf with a sibling .bin.

    animcurves._read_glb is GLB-only, and the packs ship as .gltf + .bin.
    """
    src = Path(path)
    data = src.read_bytes()
    if data[:4] == b"glTF":
        magic, _version, length = struct.unpack_from("<4sII", data, 0)
        offset, doc, blob = 12, None, b""
        while offset + 8 <= min(length, len(data)):
            chunk_len, chunk_type = struct.unpack_from("<I4s", data, offset)
            offset += 8
            chunk = data[offset:offset + chunk_len]
            offset += chunk_len
            if chunk_type == b"JSON":
                doc = json.loads(chunk.decode("utf-8"))
            elif chunk_type == b"BIN\x00":
                blob = chunk
        if doc is None:
            raise ValueError(f"no JSON chunk in {src}")
        return doc, blob
    doc = json.loads(data.decode("utf-8"))
    blob = b""
    buffers = doc.get("buffers") or []
    if buffers:
        uri = buffers[0].get("uri") or ""
        if uri.startswith("data:"):
            import base64
            blob = base64.b64decode(uri.split(",", 1)[1])
        elif uri:
            blob = (src.parent / uri).read_bytes()
    return doc, blob


def clips(name: str) -> list:
    """[{name, seconds, loop, root_motion}] for a fetched pack."""
    path = pack_file(name)
    if path is None:
        raise FileNotFoundError(f"pack {name!r} is not fetched — "
                                f"bgate animlib fetch {name}")
    doc, _ = read_gltf(path)
    accessors = doc.get("accessors") or []
    out = []
    for anim in doc.get("animations") or []:
        seconds = 0.0
        for sampler in anim.get("samplers") or []:
            acc = accessors[sampler["input"]]
            hi = (acc.get("max") or [0.0])[0]
            seconds = max(seconds, float(hi))
        label = anim.get("name") or ""
        out.append({"name": label, "seconds": round(seconds, 3),
                    "loop": label.endswith("_Loop") or label.endswith("-loop"),
                    "root_motion": label.endswith("_RM")})
    return out


def skeleton_names(name: str) -> list:
    """The joint names a pack's skeleton carries, in skin order."""
    path = pack_file(name)
    if path is None:
        return []
    doc, _ = read_gltf(path)
    nodes = doc.get("nodes") or []
    skins = doc.get("skins") or []
    if not skins:
        return [n.get("name", "") for n in nodes]
    return [nodes[i].get("name", "") for i in skins[0].get("joints") or []]


def resolve(name: str) -> dict:
    """Everything the Blender script needs for one pack, or a refusal."""
    spec = PACKS.get(name)
    if not spec:
        return {"ok": False, "error": f"no such pack {name!r}; known: "
                                      f"{', '.join(sorted(PACKS))}"}
    path = pack_file(name)
    if path is None:
        return {"ok": False, "error": f"pack {name!r} is not fetched — run "
                                      f"`bgate animlib fetch {name}` (a "
                                      f"{spec['license']} download, "
                                      f"SHA-256 pinned)"}
    names = skeleton_names(name)
    mapping = bone_map(names, spec.get("naming", "auto"))
    return {"ok": True, "pack": name, "path": str(path).replace("\\", "/"),
            "bone_map": mapping, "unmapped": [n for n in names if n not in mapping],
            "clips": {c["name"]: c for c in clips(name)}, "license": spec["license"]}


def doctor_row() -> dict:
    """One line for `bgate doctor`: how many packs are fetched, and the
    command for the rest. Never a failure — a pack is optional."""
    st = status()
    fetched = [k for k, r in st["packs"].items() if r["fetched"]]
    missing = [k for k, r in st["packs"].items() if not r["fetched"]]
    return {"available": bool(fetched), "path": st["home"] if fetched else "",
            "version": f"{len(fetched)} of {len(PACKS)} packs",
            "reason": "" if not missing else
                      "not fetched: " + ", ".join(f"bgate animlib fetch {m}"
                                                  for m in missing)}
