"""Kits: reusable systems a project can take one at a time.

THE GAP THIS CLOSES. The Godot templates ship ten well-shaped scripts - a
third-person controller, an interaction volume, a vehicle - and the only way
to get one into a game was ``bgate init`` (a new project) or
``godot_character_wire`` (one controller, on its way to doing something else).
A project that started as a platformer and wanted an inventory wrote one.
Measured on three shipped games: three inventories, three health components,
none of them the same shape, none of them tested.

A KIT IS A MANIFEST AND FILES. ``src/templates/kits/<engine>/<name>/kit.json``
says what it is, what it needs (input actions, autoloads, other kits) and
what it provides (signals, exports, groups); the scripts sit beside it, or
the manifest points at a script the scaffold already ships
(``template:godot/3d/scripts/interactable.gd``) so the two never drift.

INSTALL NEVER OVERWRITES. A file already in the project is reported, with
whether it matches the kit's copy, and left alone. ``force`` is the one way
past that and it is the caller's to say. Missing input actions are appended
to project.godot's ``[input]`` block with the manifest's default keys, never
rebound: an action that exists is the project's, whatever it is bound to.

THE LEDGER. ``.bgate/kits.json`` in the Builders Gate root records what was
installed and each file's hash at the time, so ``status`` can say
``installed`` / ``modified`` / ``missing`` per file without guessing, and
``remove`` can refuse to delete a file someone has since edited.

STUDIO MODULES. With a studio set (``bgate_core.store.studio``), the shelf
also reads ``<studio>/shared/modules/<engine>/<name>/<version>/``: modules a
person EXTRACTED from one of their own games. A version, once published, is
never rewritten. The ledger pins ``name@version`` for a studio module, and a
studio module named like a shipped kit is used only when it is pinned, so a
person's own ``health`` never silently replaces the one that ships.
``update`` moves a pin forward with the same rule install keeps: a file edited
since install is reported and left alone.

ENGINES. Godot kits are GDScript plus project.godot input actions. A ``web``
module is TypeScript under ``src/``; nothing edits an input map there, so its
``requires.actions`` are reported as needs rather than bound.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
KITS_DIR = TEMPLATES_DIR / "kits"
LEDGER = ".bgate/kits.json"
DIMENSIONS = ("2d", "3d", "any")
KIT_ENGINES = ("godot", "web")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")

# Godot's Key enum, the physical keycodes a manifest may name. Letters and
# digits are their ASCII codes; the rest are the constants from
# core/os/keyboard.h. Named here so a manifest says "Shift", not 4194325.
KEYCODES: dict[str, int] = {
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
    **{chr(c): c for c in range(ord("0"), ord("9") + 1)},
    "Space": 32, "Escape": 4194305, "Tab": 4194306, "Enter": 4194309,
    "Left": 4194319, "Up": 4194320, "Right": 4194321, "Down": 4194322,
    "Shift": 4194325, "Ctrl": 4194326, "Alt": 4194328,
}

_KEY_EVENT = (
    'Object(InputEventKey,"resource_local_to_scene":false,"resource_name":"",'
    '"device":-1,"window_id":0,"alt_pressed":false,"shift_pressed":false,'
    '"ctrl_pressed":false,"meta_pressed":false,"pressed":false,"keycode":0,'
    '"physical_keycode":{code},"key_label":0,"unicode":0,"location":0,'
    '"echo":false,"script":null)')


class KitError(ValueError):
    """A manifest that cannot be honoured, named so a caller can say why."""


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------
def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def source_path(kit_dir: Path, ref: str) -> Path:
    """Where a manifest's file comes from: beside the manifest, or a script
    the scaffold ships (``template:<path under src/templates>``)."""
    if ref.startswith("template:"):
        return TEMPLATES_DIR / ref[len("template:"):]
    return kit_dir / ref


def studio_modules_dir(engine: str) -> Path | None:
    """``<studio>/shared/modules/<engine>``, or None when no studio is set."""
    try:
        from bgate_core.store import studio
    except ImportError:
        return None
    if studio.root() is None:
        return None
    return Path(studio.modules_dir(engine))


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def module_versions(name: str, engine: str = "godot") -> list[str]:
    """Every published version of a studio module, oldest first."""
    base = studio_modules_dir(engine)
    if base is None or not (base / name).is_dir():
        return []
    found = [p.name for p in (base / name).iterdir()
             if p.is_dir() and _VERSION_RE.match(p.name)
             and (p / "kit.json").is_file()]
    return sorted(found, key=_version_key)


def _kit_dir(name: str, engine: str, version: str) -> tuple[Path, str]:
    """Where a kit lives, and whether it is ``shipped`` or ``studio``.

    A version names a studio module. Without one, the shipped kit wins; a
    name only the studio has resolves to its newest version.
    """
    if version:
        base = studio_modules_dir(engine)
        if base is None:
            raise KitError(f"{name}@{version} is a studio module and no studio "
                           "is set; `bgate studio init <dir>` sets one")
        return base / name / version, "studio"
    shipped = KITS_DIR / engine / name
    if (shipped / "kit.json").is_file():
        return shipped, "shipped"
    versions = module_versions(name, engine)
    if versions:
        return studio_modules_dir(engine) / name / versions[-1], "studio"
    return shipped, "shipped"


def _check_dest(dest: str, engine: str, where: str) -> Path:
    dest_path = Path(dest)
    if dest_path.is_absolute() or ".." in dest_path.parts:
        raise KitError(f"{where}: {dest!r} must be a relative path "
                       "inside the project")
    if engine == "web" and dest_path.parts[:1] != ("src",):
        raise KitError(f"{where}: {dest!r} is outside src/; a web module "
                       "installs under src/ only")
    return dest_path


def load(name: str, engine: str = "godot", version: str = "") -> dict:
    """One kit's manifest, validated, with every source path resolved."""
    kit_dir, origin = _kit_dir(name, engine, version)
    manifest = kit_dir / "kit.json"
    if not manifest.is_file():
        what = f"{name}@{version}" if version else repr(name)
        raise KitError(f"no kit named {what} for {engine}; kit_list names them")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise KitError(f"{manifest}: {exc}") from exc
    for key in ("name", "title", "engine", "dimension", "summary", "files"):
        if key not in data:
            raise KitError(f"{manifest}: missing {key!r}")
    if data["name"] != name:
        raise KitError(f"{manifest}: name {data['name']!r} does not match its "
                       f"directory {name!r}")
    if data["dimension"] not in DIMENSIONS:
        raise KitError(f"{manifest}: dimension must be one of {DIMENSIONS}")
    if not data["files"]:
        raise KitError(f"{manifest}: a kit with no files installs nothing")
    kit_version = ""
    if origin == "studio":
        kit_version = kit_dir.name
        if data.get("version") != kit_version:
            raise KitError(f"{manifest}: version {data.get('version')!r} does "
                           f"not match its directory {kit_version!r}")
    files = []
    for dest, ref in data["files"].items():
        dest_path = _check_dest(dest, engine, str(manifest))
        if origin == "studio" and ref.startswith("template:"):
            raise KitError(f"{manifest}: a studio module carries its own files, "
                           f"not {ref!r}")
        src = source_path(kit_dir, ref)
        if origin == "studio" and not src.resolve().is_relative_to(kit_dir.resolve()):
            raise KitError(f"{manifest}: {ref!r} points outside the module")
        if not src.is_file():
            raise KitError(f"{manifest}: {ref!r} is not a file ({src})")
        files.append({"dest": dest_path.as_posix(), "source": str(src),
                      "hash": _sha(src), "bytes": src.stat().st_size})
    requires = data.get("requires") or {}
    actions = requires.get("actions") or {}
    for action, keys in actions.items():
        for key in keys:
            if key not in KEYCODES:
                raise KitError(f"{manifest}: action {action!r} names key "
                               f"{key!r}, not in {sorted(KEYCODES)}")
    return {
        "name": data["name"], "title": data["title"], "engine": data["engine"],
        "dimension": data["dimension"], "summary": data["summary"],
        "usage": data.get("usage", ""),
        "files": files,
        "requires": {"actions": actions,
                     "autoloads": list(requires.get("autoloads") or []),
                     "kits": list(requires.get("kits") or [])},
        "provides": data.get("provides") or {},
        "dir": str(kit_dir),
        "origin": origin,
        "version": kit_version,
    }


def list_kits(engine: str = "godot", dimension: str = "") -> list[dict]:
    """Every kit for an engine, optionally only those fitting a dimension.

    Shipped kits, then studio modules (newest version, with every version
    under ``versions``). A studio module named like a shipped kit is listed
    on the shipped row as ``studio_versions``; it is used only when pinned.

    A kit whose manifest is broken is REPORTED, not dropped: a kit that
    silently vanishes from the list is a kit nobody will fix.
    """
    base = KITS_DIR / engine
    studio_base = studio_modules_dir(engine)
    shipped = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
    studio_names = (sorted(p.name for p in studio_base.iterdir() if p.is_dir())
                    if studio_base is not None and studio_base.is_dir() else [])
    out: list[dict] = []
    for name in shipped + [n for n in studio_names if n not in shipped]:
        try:
            kit = load(name, engine)
        except KitError as exc:
            out.append({"name": name, "engine": engine, "error": str(exc)})
            continue
        versions = module_versions(name, engine)
        if kit["origin"] == "studio":
            kit["versions"] = versions
        elif versions:
            kit["studio_versions"] = versions
        if dimension and kit["dimension"] not in ("any", dimension):
            continue
        out.append(kit)
    return out


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------
def _ledger_path(root: Path) -> Path:
    return root / LEDGER


def read_ledger(root: str | os.PathLike[str]) -> dict:
    path = _ledger_path(Path(root))
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_ledger(root: Path, ledger: dict) -> None:
    path = _ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# project.godot [input] and [autoload]
# ---------------------------------------------------------------------------
def _section(text: str, name: str) -> str:
    m = re.search(rf"^\[{name}\]\s*$(.*?)(?=^\[|\Z)", text, re.S | re.M)
    return m.group(1) if m else ""


def declared_actions(project_godot: Path) -> set[str]:
    """Every action project.godot's [input] section declares."""
    if not project_godot.is_file():
        return set()
    body = _section(project_godot.read_text(encoding="utf-8"), "input")
    return set(re.findall(r"^(\w+)=\{", body, re.M))


def declared_autoloads(project_godot: Path) -> set[str]:
    if not project_godot.is_file():
        return set()
    body = _section(project_godot.read_text(encoding="utf-8"), "autoload")
    return set(re.findall(r"^(\w+)=", body, re.M))


def _action_block(action: str, keys: list[str]) -> str:
    events = "\n, ".join(_KEY_EVENT.format(code=KEYCODES[k]) for k in keys)
    return f"{action}={{\n\"deadzone\": 0.5,\n\"events\": [{events}\n]\n}}\n"


def bind_actions(project_godot: Path, actions: dict[str, list[str]]) -> list[str]:
    """Append the MISSING actions to [input] with their default keys.

    Never rebinds one that exists: an action a project declares is the
    project's, whatever it is bound to. Returns the names added.
    """
    have = declared_actions(project_godot)
    missing = {a: k for a, k in actions.items() if a not in have}
    if not missing:
        return []
    text = project_godot.read_text(encoding="utf-8")
    blocks = "".join(_action_block(a, k) for a, k in missing.items())
    m = re.search(r"^\[input\]\s*$", text, re.M)
    if m:
        # At the END of the [input] section, before the next header.
        nxt = re.search(r"^\[", text[m.end():], re.M)
        at = m.end() + nxt.start() if nxt else len(text)
        head = text[:at].rstrip("\n") + "\n\n"
        tail = text[at:]
        text = head + blocks + ("\n" if tail else "") + tail
    else:
        text = text.rstrip("\n") + "\n\n[input]\n\n" + blocks
    project_godot.write_text(text, encoding="utf-8")
    return sorted(missing)


# ---------------------------------------------------------------------------
# Status, install, remove
# ---------------------------------------------------------------------------
def _file_state(dest: Path, expected_hash: str) -> str:
    if not dest.is_file():
        return "missing"
    return "installed" if _sha(dest) == expected_hash else "modified"


def status(root: str | os.PathLike[str], game_dir: str | os.PathLike[str],
           engine: str = "godot", dimension: str = "") -> dict:
    """Every kit, with what the ledger and the disk say about it here."""
    root, game = Path(root), Path(game_dir)
    ledger = read_ledger(root)
    kits = []
    for kit in list_kits(engine, dimension):
        if kit.get("error"):
            kits.append(kit)
            continue
        rec = ledger.get(kit["name"])
        pinned = (rec or {}).get("version") or ""
        if pinned and pinned != kit["version"]:
            # The ledger's pin, not the shelf's newest or the shipped copy,
            # is what this game holds.
            try:
                kit = {**load(kit["name"], engine, pinned),
                       "versions": module_versions(kit["name"], engine)}
            except KitError as exc:
                kits.append({"name": kit["name"], "engine": engine,
                             "error": f"pinned {kit['name']}@{pinned}: {exc}"})
                continue
        if pinned:
            versions = kit.get("versions") or []
            kit["newer"] = (versions[-1] if versions and
                            _version_key(versions[-1]) > _version_key(pinned) else "")
        files = []
        for f in kit["files"]:
            dest = game / f["dest"]
            # Against the LEDGER's hash when installed, so a kit updated
            # upstream reads as installed-but-stale rather than modified;
            # against the kit's own copy otherwise.
            expected = (rec or {}).get("files", {}).get(f["dest"], f["hash"])
            state = _file_state(dest, expected)
            files.append({"dest": f["dest"], "state": state,
                          "stale": bool(rec) and state == "installed"
                          and expected != f["hash"]})
        states = {f["state"] for f in files}
        kit_state = ("installed" if states == {"installed"} else
                     "absent" if states == {"missing"} else
                     "partial" if "missing" in states else "modified")
        kits.append({**kit, "state": kit_state, "files_state": files,
                     "installed_at": (rec or {}).get("installed_at"),
                     "stale": any(f["stale"] for f in files)})
    return {"kits": kits, "engine": engine, "dimension": dimension or "any",
            "ledger": str(_ledger_path(root))}


def install(root: str | os.PathLike[str], game_dir: str | os.PathLike[str],
            name: str, *, engine: str = "godot", force: bool = False,
            bind: bool = True, dimension: str = "", version: str = "") -> dict:
    """Copy a kit into the game. Never overwrites without ``force``.

    ``version`` installs that studio module version and pins it; without
    one, a game that already pins a version keeps it (``update`` moves it).

    Returns what landed, what was left alone and why, which input actions
    were added, and what the kit still needs that this project lacks
    (autoloads, other kits). ``ok`` is False only when a file the kit
    needs is absent afterwards.
    """
    root, game = Path(root).resolve(), Path(game_dir).resolve()
    if not version:
        version = (read_ledger(root).get(name) or {}).get("version") or ""
    kit = load(name, engine, version)
    if dimension and kit["dimension"] not in ("any", dimension):
        raise KitError(f"{name} is a {kit['dimension']} kit; this project is "
                       f"{dimension}. kit_install(any_dimension=True) or "
                       "`bgate kit install --all` installs it anyway.")
    if not game.is_dir():
        raise KitError(f"no engine project at {game}")

    ledger = read_ledger(root)
    written, kept, replaced = [], [], []
    for f in kit["files"]:
        dest = game / f["dest"]
        if dest.is_file():
            if _sha(dest) == f["hash"]:
                kept.append({"dest": f["dest"],
                             "reason": "already present, identical"})
                continue
            if not force:
                kept.append({"dest": f["dest"],
                             "reason": "already present and DIFFERENT; "
                                       "force=True replaces it (a .bak is kept)"})
                continue
            bak = dest.with_suffix(dest.suffix + f".bak.{int(time.time())}")
            shutil.copyfile(dest, bak)
            replaced.append({"dest": f["dest"], "backup": bak.name})
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f["source"], dest)
        written.append(f["dest"])

    project_godot = game / "project.godot"
    actions = kit["requires"]["actions"]
    added_actions: list[str] = []
    missing_actions: list[str] = []
    if engine != "godot":
        # No input map to edit: the needs are listed for the person to wire.
        missing_actions = sorted(actions)
        missing_autoloads = list(kit["requires"]["autoloads"])
    else:
        if actions:
            have = declared_actions(project_godot)
            missing_actions = sorted(a for a in actions if a not in have)
            if missing_actions and bind and project_godot.is_file():
                added_actions = bind_actions(project_godot, actions)
                missing_actions = []
        autoloads = declared_autoloads(project_godot)
        missing_autoloads = [a for a in kit["requires"]["autoloads"]
                             if a not in autoloads]
    missing_kits = [k for k in kit["requires"]["kits"] if k not in ledger]

    # The ledger records the kit's hash for every file the kit OWNS here:
    # one it wrote, or one that was already identical. A file kept because
    # it differs is the project's, not the kit's, and stays out, so status
    # reads it as modified against the kit rather than as installed.
    rec = ledger.get(name) or {}
    files_rec = dict(rec.get("files") or {})
    different = {k["dest"] for k in kept if "DIFFERENT" in k["reason"]}
    for f in kit["files"]:
        if f["dest"] in different:
            files_rec.pop(f["dest"], None)
        elif (game / f["dest"]).is_file():
            files_rec[f["dest"]] = f["hash"]
    ledger[name] = {"files": files_rec,
                    "installed_at": rec.get("installed_at") or _stamp(),
                    "updated_at": _stamp()}
    if kit["version"]:
        ledger[name]["version"] = kit["version"]
    _write_ledger(root, ledger)

    all_present = all((game / f["dest"]).is_file() for f in kit["files"])
    did_something = bool(written or added_actions)
    return {
        "ok": all_present,
        "kit": name, "title": kit["title"], "dimension": kit["dimension"],
        "version": kit["version"], "origin": kit["origin"],
        "written": written, "kept": kept, "replaced": replaced,
        "actions_added": added_actions,
        "actions_missing": missing_actions,
        "autoloads_missing": missing_autoloads,
        "kits_missing": missing_kits,
        "usage": kit["usage"], "provides": kit["provides"],
        "game_dir": str(game),
        "note": ("nothing to do: every file is already present"
                 if not did_something and all_present else ""),
    }


def remove(root: str | os.PathLike[str], game_dir: str | os.PathLike[str],
           name: str, *, engine: str = "godot", force: bool = False) -> dict:
    """Delete a kit's files, refusing any the ledger says have been edited.

    Input actions are NOT removed: another script may read them by now, and
    an unbound action is a silent no-op in the engine, not an error.
    """
    root, game = Path(root).resolve(), Path(game_dir).resolve()
    ledger = read_ledger(root)
    rec = ledger.get(name)
    if rec is None:
        raise KitError(f"{name} is not in the ledger; nothing to remove")
    kit = load(name, engine, rec.get("version") or "")
    removed, refused, absent = [], [], []
    for f in kit["files"]:
        dest = game / f["dest"]
        if not dest.is_file():
            absent.append(f["dest"])
            continue
        expected = rec.get("files", {}).get(f["dest"], f["hash"])
        if _sha(dest) not in (expected, f["hash"]) and not force:
            refused.append({"dest": f["dest"],
                            "reason": "edited since install; force=True deletes it"})
            continue
        dest.unlink()
        removed.append(f["dest"])
    if not refused:
        ledger.pop(name, None)
        _write_ledger(root, ledger)
    return {"ok": not refused, "kit": name, "removed": removed,
            "refused": refused, "absent": absent,
            "ledger_cleared": not refused}


# ---------------------------------------------------------------------------
# Studio modules: extract one from a game, move a pin forward
# ---------------------------------------------------------------------------
def extract(game_dir: str | os.PathLike[str], name: str, version: str,
            files: list[str], *, engine: str = "godot", title: str = "",
            summary: str = "", dimension: str = "any", usage: str = "",
            requires: dict | None = None, provides: dict | None = None,
            confirm: bool = False) -> dict:
    """Publish files from a game as ``<name>@<version>`` in the studio.

    Two calls by design. Without ``confirm`` nothing is written: the result
    is the manifest it WOULD publish, so the person can read what it claims
    to provide and need and correct it. With ``confirm`` the version is
    written, and from then on it is immutable: publishing the same version
    twice is refused, whatever the bytes.
    """
    game = Path(game_dir).resolve()
    if engine not in KIT_ENGINES:
        raise KitError(f"modules are for {KIT_ENGINES}, not {engine!r}")
    if not _NAME_RE.match(name):
        raise KitError(f"module name {name!r}: lowercase letters, digits and "
                       "_, starting with a letter")
    if not _VERSION_RE.match(version):
        raise KitError(f"version {version!r} must be MAJOR.MINOR.PATCH")
    if dimension not in DIMENSIONS:
        raise KitError(f"dimension must be one of {DIMENSIONS}")
    if not files:
        raise KitError("a module with no files installs nothing")
    base = studio_modules_dir(engine)
    if base is None:
        raise KitError("no studio is set; `bgate studio init <dir>` sets one")
    target = base / name / version
    if target.exists():
        raise KitError(f"{name}@{version} is already published and versions "
                       "are immutable; publish a new version")

    picked: dict[str, Path] = {}
    for rel in files:
        dest = _check_dest(Path(rel).as_posix(), engine, "files")
        src = (game / dest).resolve()
        if not src.is_relative_to(game):
            raise KitError(f"{rel!r} is outside {game}")
        if not src.is_file():
            raise KitError(f"{rel!r} is not a file in {game}")
        picked[dest.as_posix()] = src
    req = requires or {}
    manifest = {
        "name": name, "version": version, "engine": engine,
        "title": title or name.replace("_", " ").capitalize(),
        "dimension": dimension,
        "summary": summary,
        "files": {dest: f"files/{dest}" for dest in sorted(picked)},
        "requires": {"actions": dict(req.get("actions") or {}),
                     "autoloads": list(req.get("autoloads") or []),
                     "kits": list(req.get("kits") or [])},
        "provides": dict(provides or {}),
        "usage": usage,
        "extracted_from": str(game),
        "extracted_at": _stamp(),
    }
    for action, keys in manifest["requires"]["actions"].items():
        for key in keys:
            if engine == "godot" and key not in KEYCODES:
                raise KitError(f"action {action!r} names key {key!r}, not in "
                               f"{sorted(KEYCODES)}")
    previous = module_versions(name, engine)
    out = {"ok": True, "module": name, "version": version, "engine": engine,
           "manifest": manifest, "dir": str(target),
           "previous_versions": previous}
    if not confirm:
        out.update(confirmed=False,
                   note="nothing written; check provides and requires, then "
                        "call again with confirm")
        return out
    if previous and _version_key(version) < _version_key(previous[-1]):
        raise KitError(f"{name}@{version} is older than the newest published "
                       f"version {previous[-1]}")
    if not summary:
        raise KitError("a published module needs a summary saying what it is")

    staging = base / name / f".{version}.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    for dest, src in picked.items():
        out_path = staging / "files" / dest
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out_path)
    (staging / "kit.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                      encoding="utf-8")
    staging.rename(target)
    load(name, engine, version)
    _index_module(engine, name, version, manifest)
    out.update(confirmed=True, note="")
    return out


def _index_module(engine: str, name: str, version: str, manifest: dict) -> None:
    """Record a published version in studio.json. The folder is the truth;
    the index is what the studio page reads without walking every module."""
    try:
        from bgate_core.store import studio
        data = studio.index()
    except Exception:                                             # noqa: BLE001
        return
    key = f"{engine}/{name}"
    entry = data["modules"].get(key) if isinstance(data["modules"].get(key), dict) else {}
    versions = sorted(set(entry.get("versions") or []) | {version}, key=_version_key)
    data["modules"][key] = {"engine": engine, "name": name, "versions": versions,
                            "title": manifest["title"],
                            "summary": manifest["summary"],
                            "dimension": manifest["dimension"]}
    studio.save_index(data)


def update(root: str | os.PathLike[str], game_dir: str | os.PathLike[str],
           name: str, *, engine: str = "godot", version: str = "") -> dict:
    """Move a pinned module to ``version`` (default: the newest).

    A file the game has not edited since install is replaced by the new
    version's copy; an edited one is kept and named, never overwritten.
    Files the new version dropped stay in the game and are reported.
    """
    root, game = Path(root).resolve(), Path(game_dir).resolve()
    ledger = read_ledger(root)
    rec = ledger.get(name)
    if rec is None:
        raise KitError(f"{name} is not installed here; kit_install it first")
    pinned = rec.get("version") or ""
    if not pinned:
        raise KitError(f"{name} is a shipped kit, not a pinned studio module; "
                       "there is no version to move")
    versions = module_versions(name, engine)
    target = version or (versions[-1] if versions else "")
    if not target:
        raise KitError(f"no published versions of {name} for {engine}")
    if _version_key(target) < _version_key(pinned):
        raise KitError(f"{name}@{target} is older than the pinned {pinned}")
    if target == pinned:
        return {"ok": True, "kit": name, "from": pinned, "to": target,
                "written": [], "kept": [], "dropped": [],
                "note": f"already at {pinned}"}
    old = {f["dest"]: f for f in load(name, engine, pinned)["files"]}
    new = load(name, engine, target)
    files_rec = dict(rec.get("files") or {})
    written, kept = [], []
    for f in new["files"]:
        dest = game / f["dest"]
        if dest.is_file():
            current = _sha(dest)
            if current == f["hash"]:
                files_rec[f["dest"]] = f["hash"]
                kept.append({"dest": f["dest"], "reason": "already identical"})
                continue
            owned = files_rec.get(f["dest"]) or (old.get(f["dest"]) or {}).get("hash")
            if current != owned:
                files_rec.pop(f["dest"], None)
                kept.append({"dest": f["dest"],
                             "reason": "edited since install; left alone"})
                continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f["source"], dest)
        files_rec[f["dest"]] = f["hash"]
        written.append(f["dest"])
    new_dests = {f["dest"] for f in new["files"]}
    dropped = sorted(d for d in old if d not in new_dests)
    for d in dropped:
        files_rec.pop(d, None)
    ledger[name] = {**rec, "files": files_rec, "version": target,
                    "updated_at": _stamp()}
    _write_ledger(root, ledger)
    return {"ok": True, "kit": name, "from": pinned, "to": target,
            "written": written, "kept": kept, "dropped": dropped,
            "usage": new["usage"],
            "note": (f"{len(dropped)} file(s) the new version no longer ships "
                     "were left in the game" if dropped else "")}


# ---------------------------------------------------------------------------
# Proof: does the engine compile what landed?
# ---------------------------------------------------------------------------
PROVE_MARK = "BGATE_KITPROVE:"

# `extends Node`, so it runs as the project's main scene and the autoloads
# every kit script references (BGateTelemetry) resolve exactly as in the game.
# `--check-only` cannot do this: it parses one file with no autoloads, and
# every shipped controller fails it on `BGateTelemetry` alone. load() then
# can_instantiate() is the engine's own answer to "does this compile".
_PROVE_GD = '''extends Node
const MARK := "__MARK__"


func _ready() -> void:
	var out := {"ok": true, "scripts": []}
	for path in __PATHS__:
		var row := {"path": path, "compiled": false, "error": ""}
		var res = load(path)
		if res == null or not (res is GDScript):
			row["error"] = "load() returned nothing"
			out["ok"] = false
		elif not res.can_instantiate():
			row["error"] = "does not compile"
			out["ok"] = false
		else:
			row["compiled"] = true
		out["scripts"].append(row)
	print(MARK + JSON.stringify(out))
	get_tree().quit()
'''


def prove(game_dir: str | os.PathLike[str], rel_paths: list[str],
          timeout: int = 180) -> dict:
    """Load each script inside the running project and report whether the
    engine compiled it. Needs Godot; the caller decides what a miss means."""
    from bgate_adapters import godot as _godot

    paths = ", ".join(json.dumps(f"res://{p}") for p in rel_paths)
    script = _PROVE_GD.replace("__MARK__", PROVE_MARK).replace("__PATHS__", f"[{paths}]")
    ran = _godot.run_script(script, project_dir=str(game_dir), timeout=timeout)
    text = (ran.get("stdout") or "") + "\n" + (ran.get("stderr") or "")
    found = None
    for line in text.splitlines():
        if PROVE_MARK in line:
            try:
                found = json.loads(line.split(PROVE_MARK, 1)[1])
            except json.JSONDecodeError:
                found = None
    errors = [l for l in text.splitlines()
              if "SCRIPT ERROR" in l or "Parse Error" in l or "Compile Error" in l][:12]
    if found is None:
        return {"ok": False, "measured": False,
                "error": ran.get("error") or "the probe printed no report",
                "engine_errors": errors,
                "stderr": (ran.get("stderr") or "")[-1200:]}
    found["measured"] = True
    found["engine_errors"] = errors
    return found
