"""The studio: one folder that holds a person's games and what they share.

Projects used to live wherever `bgate init` happened to run, so the parts one
game made (a controller, a sprite sheet) had no common place to be found by the
next. A studio is that place, chosen once:

    <studio>/games/<name>/                               projects made or adopted here
    <studio>/shared/modules/<engine>/<name>/<version>/   reusable modules
    <studio>/shared/assets/                              the shared asset store
    <studio>/studio.json                                 index: games, modules

WHICH FOLDER. ``BGATE_STUDIO`` wins, then the pointer `bgate studio init` writes
to ``~/.bgate/studio-dir.json`` (BGATE_HOME aware, like the registry). The
pointer is a separate file from the studio's own index for the same reason the
active pointer is separate from the registry: the index is a fact about the
folder, the pointer is this machine's preference, and losing one must not cost
the other.

NO STUDIO IS THE DEFAULT, and it changes nothing. :func:`root` returns None
and every caller keeps today's behaviour: init into the cwd, the library in
``~/.bgate/library``.

PATHS IN THE INDEX ARE RELATIVE when the game sits inside the studio, so the
whole folder can be moved, synced or copied to another machine and still read
correctly. A game registered in place, outside the studio, keeps its absolute
path; there is nothing else it could honestly say.

NOTHING HERE DELETES. `adopt --move` copies, checks the copy, repoints the
registry, and hands the original's path back for the person to remove.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import db
from . import project as _project

ENV = "BGATE_STUDIO"
POINTER_FILENAME = "studio-dir.json"
INDEX_FILENAME = "studio.json"
INDEX_VERSION = 1

GAMES_DIRNAME = "games"
SHARED_DIRNAME = "shared"
MODULES_DIRNAME = "modules"
ASSETS_DIRNAME = "assets"


class StudioError(LookupError):
    """No studio is set, or a studio operation cannot be honoured, and why."""


# ---------------------------------------------------------------------------
# Where
# ---------------------------------------------------------------------------
def _pointer_path() -> Path:
    return _project.user_dir() / POINTER_FILENAME


def configured() -> tuple[Optional[Path], str]:
    """(path, source) as configured, whether or not the folder exists.

    source is "env", "pointer" or "" (nothing set). :func:`status` reports a
    configured folder that has gone missing; :func:`root` treats it as unset.
    """
    raw = os.environ.get(ENV, "").strip()
    if raw:
        return Path(raw).expanduser(), "env"
    try:
        data = json.loads(_pointer_path().read_text(encoding="utf-8"))
        given = str(data.get("studio_dir") or "").strip()
    except Exception:
        return None, ""
    return (Path(given).expanduser(), "pointer") if given else (None, "")


def root() -> Optional[Path]:
    """The studio folder, or None when no studio is set or it is not there.

    "Not there" includes a folder without ``studio.json``: a typo in
    BGATE_STUDIO must not turn an arbitrary directory into the place new games
    are written.
    """
    path, _source = configured()
    if path is None:
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    return resolved if (resolved / INDEX_FILENAME).is_file() else None


def _require() -> Path:
    found = root()
    if found is None:
        path, source = configured()
        if path is None:
            raise StudioError("no studio is set. Run `bgate studio init <dir>` "
                              f"or set {ENV}.")
        raise StudioError(f"the studio at {path} (from {source}) is missing or "
                          f"has no {INDEX_FILENAME}. Run `bgate studio init "
                          f"{path}` to recreate it.")
    return found


def games_dir() -> Path:
    return _require() / GAMES_DIRNAME


def modules_dir(engine: str) -> Path:
    engine = str(engine or "").strip().lower()
    if not engine or any(ch in engine for ch in "/\\."):
        raise StudioError(f"not an engine name: {engine!r}")
    return _require() / SHARED_DIRNAME / MODULES_DIRNAME / engine


def assets_dir() -> Path:
    return _require() / SHARED_DIRNAME / ASSETS_DIRNAME


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------
def _empty_index() -> dict:
    return {"version": INDEX_VERSION, "games": {}, "modules": {}}


def index() -> dict:
    """The studio's index, with every key present. Raises without a studio.

    A file that exists but will not parse raises too, rather than reading as
    empty: a writer that took "unreadable" for "empty" and saved is how a
    registry lost every entry once (project._read_registry_raw).
    """
    path = _require() / INDEX_FILENAME
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise StudioError(f"{path} is not an index object")
    out = _empty_index()
    out.update(data)
    for key in ("games", "modules"):
        if not isinstance(out.get(key), dict):
            out[key] = {}
    return out


def save_index(data: dict) -> Path:
    """Write the index whole, through a temp file, so a crash mid-write leaves
    the previous index rather than half of a new one."""
    path = _require() / INDEX_FILENAME
    _write_json(path, data)
    return path


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _stored_path(studio: Path, game_root: Path) -> str:
    """Relative (posix) when inside the studio, absolute otherwise."""
    try:
        return game_root.relative_to(studio).as_posix()
    except ValueError:
        return str(game_root)


def game_path(entry: dict) -> Path:
    """An index entry's folder, resolved against the studio when relative."""
    raw = Path(str(entry.get("path") or ""))
    return raw if raw.is_absolute() else _require() / raw


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
def init(directory: str | os.PathLike[str]) -> dict:
    """Create (or re-point at) a studio. Idempotent; never overwrites an index.

    Refuses the Builders Gate checkout and anything inside an existing project:
    a studio nested in a game would be copied into itself by `adopt --move`.
    """
    target = Path(directory).expanduser().resolve()
    _project.refuse_harness(target, "make a studio")
    inside = db.resolve_root(target)
    if inside is not None:
        raise StudioError(f"refusing to make a studio at {target}: it is inside "
                          f"the project at {inside}. Pick a folder outside any game.")
    if target.exists() and not target.is_dir():
        raise StudioError(f"{target} exists and is not a folder")
    created = not (target / INDEX_FILENAME).is_file()
    for sub in ((GAMES_DIRNAME,), (SHARED_DIRNAME, MODULES_DIRNAME),
                (SHARED_DIRNAME, ASSETS_DIRNAME)):
        target.joinpath(*sub).mkdir(parents=True, exist_ok=True)
    if created:
        _write_json(target / INDEX_FILENAME, _empty_index())
    pointer = _pointer_path()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    _write_json(pointer, {"studio_dir": str(target)})
    out = {"root": str(target), "created": created, "pointer": str(pointer)}
    env_path, source = configured()
    if source == "env" and env_path is not None and env_path.resolve() != target:
        out["note"] = (f"{ENV} is set to {env_path} and wins over the pointer "
                       "just written; unset it to use this studio.")
    return out


def add_game(game_root: str | os.PathLike[str], name: str = "") -> dict:
    """Record a project in the index (no copy, no move). Returns the entry."""
    studio = _require()
    resolved = Path(game_root).expanduser().resolve()
    if not (resolved / db.DB_DIRNAME / db.DB_FILENAME).exists():
        raise StudioError(f"{resolved} is not a Builders Gate project (no "
                          f"{db.DB_DIRNAME}/{db.DB_FILENAME}). Run `bgate adopt` "
                          "there first.")
    if not name:
        try:
            name = _project.get(resolved)["slug"]
        except Exception:
            name = resolved.name
    data = index()
    entry = {"path": _stored_path(studio, resolved),
             "added": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    previous = data["games"].get(name)
    if isinstance(previous, dict) and previous.get("added"):
        entry["added"] = previous["added"]
    data["games"][name] = entry
    save_index(data)
    return {"name": name, **entry}


def _inventory(base: Path) -> dict[str, int]:
    """{relative posix path: size} for every file under base."""
    out: dict[str, int] = {}
    for path in base.rglob("*"):
        if path.is_file() and not path.is_symlink():
            out[path.relative_to(base).as_posix()] = path.stat().st_size
    return out


def adopt(project_dir: str | os.PathLike[str], move: bool = False) -> dict:
    """Bring an existing project into the studio.

    Without ``move``: index it where it is. With ``move``: copy it to
    ``games/<name>/``, check the copy file for file, and only then point the
    registry (and the active pointer, if it named the original) at the copy.
    The original is never deleted; its path comes back as ``original`` for the
    person to remove when they are satisfied.
    """
    studio = _require()
    source = Path(project_dir).expanduser().resolve()
    if not (source / db.DB_DIRNAME / db.DB_FILENAME).exists():
        raise StudioError(f"{source} is not a Builders Gate project (no "
                          f"{db.DB_DIRNAME}/{db.DB_FILENAME}). Run `bgate adopt "
                          f"{source}` first.")
    try:
        name = _project.get(source)["slug"]
    except Exception:
        name = source.name
    if not move:
        return {"name": name, "root": str(source), "moved": False,
                "entry": add_game(source, name)}

    dest = studio / GAMES_DIRNAME / name
    if source == dest:
        return {"name": name, "root": str(dest), "moved": False,
                "entry": add_game(dest, name),
                "note": "already in the studio"}
    if studio == source or source in studio.parents:
        raise StudioError(f"refusing to move {source}: the studio ({studio}) is "
                          "inside it, so the copy would contain itself.")
    if dest.exists():
        raise StudioError(f"{dest} already exists. Remove or rename it, or adopt "
                          "without --move to index the project where it is.")

    db.close_all()
    shutil.copytree(source, dest, symlinks=True)
    want, got = _inventory(source), _inventory(dest)
    if want != got:
        missing = sorted(set(want) - set(got))
        differ = sorted(k for k in set(want) & set(got) if want[k] != got[k])
        raise StudioError(
            f"the copy at {dest} does not match {source} "
            f"({len(missing)} missing, {len(differ)} different, e.g. "
            f"{(missing or differ)[:3]}). The registry still points at the "
            f"original; delete {dest} and retry.")
    try:
        _project.get(dest)
    except Exception as exc:
        raise StudioError(f"the copied project store at {dest} does not open "
                          f"({exc}). The registry still points at the original; "
                          f"delete {dest} and retry.") from exc

    was_active = _project.active_root() == source
    _project.register(dest, name)
    if was_active:
        _project.set_active(dest)
    entry = add_game(dest, name)
    return {"name": name, "root": str(dest), "moved": True, "original": str(source),
            "files": len(got), "active_repointed": was_active, "entry": entry}


def status() -> dict:
    """What a person or an agent needs to know about the studio, never raising.

    ``set`` is False with no studio; ``ok`` is False when one is configured but
    missing, which is the case worth saying out loud.
    """
    path, source = configured()
    if path is None:
        return {"set": False, "ok": False, "root": None, "source": ""}
    found = root()
    if found is None:
        return {"set": True, "ok": False, "root": str(path), "source": source,
                "error": f"missing, or no {INDEX_FILENAME}"}
    try:
        data = index()
    except Exception as exc:                                     # noqa: BLE001
        return {"set": True, "ok": False, "root": str(found), "source": source,
                "error": f"{INDEX_FILENAME} unreadable: {exc}"}
    games = []
    for name, entry in sorted(data["games"].items()):
        if not isinstance(entry, dict):
            continue
        where = game_path(entry)
        games.append({"name": name, "path": str(where),
                      "exists": (where / db.DB_DIRNAME / db.DB_FILENAME).exists()})
    return {"set": True, "ok": True, "root": str(found), "source": source,
            "games": games, "modules": len(data["modules"])}
