"""The machine-wide asset library: what one game made, another can take.

THE GAP THIS CLOSES. Every generated asset lands in ITS project's
``.bgate_out/`` and is tracked by content hash in THAT project's ``game.db``.
A second project on the same machine cannot see any of it; the paladin
walk cycle generated for one game is regenerated, at cost, for the next. The
one shared store that existed was ``~/.bgate/animlib``, a CC0 clip pack, and
it proved the shape: machine-wide, in no repository, following the person
rather than the game.

WHERE. ``~/.bgate/library/`` (``BGATE_HOME`` moves it, the same override the
key store and animlib honour). ``blobs/<sha256><ext>`` holds the bytes,
content-addressed so the same file published twice is stored once;
``index.json`` holds the entries: name, kind, tags, collection, size and
image dimensions, and where it came from (project, path, when).

SIDECARS TRAVEL. A sprite sheet's ``.rig.json`` (``art.rigmap``) is the
difference between a sheet the gear pipeline can use and one it has to guess
about, so publishing the sheet publishes the sidecar with it and importing
brings it back. Godot's ``.import`` files do NOT travel: the engine
regenerates them, and a stale one is worse than none.

WHAT IS DELIBERATELY NOT HERE. Deletion. ``forget`` is exposed to the CLI
only (``bgate library forget``), for the reason the animlib fetch is: a tool
an agent can call against a store every project on the machine shares must
not be able to empty it. Publish and import are per-project, reversible
copies; forgetting is not.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Iterable, Optional

from .assets import kind_of

INDEX = "index.json"
BLOBS = "blobs"

#: What ``publish`` collects when handed a DIRECTORY. A file named
#: explicitly is taken whatever its suffix.
COLLECTED = frozenset({
    ".png", ".webp", ".jpg", ".jpeg", ".svg",          # image
    ".ogg", ".wav", ".mp3",                            # audio
    ".glb", ".gltf", ".blend", ".fbx",                 # model
    ".tres", ".tscn",                                  # resource, scene
    ".gd", ".json", ".ase", ".aseprite",               # script, data, source
})
#: Never published, whatever asked. The engine rebuilds them.
SKIPPED_SUFFIXES = frozenset({".import", ".uid", ".tmp"})
SKIP_DIRS = frozenset({".git", ".godot", ".bgate", "__pycache__",
                       "node_modules", ".import"})
#: Sidecars that ride with a file: ``<stem>.rig.json`` beside ``<stem>.png``.
SIDECAR_SUFFIXES = (".rig.json",)

DIR_CAP = 2000


class LibraryError(ValueError):
    """A publish or import that cannot be honoured, and why."""


# ---------------------------------------------------------------------------
# Where
# ---------------------------------------------------------------------------
def home() -> Path:
    """``~/.bgate/library``, or under ``BGATE_HOME``."""
    base = os.environ.get("BGATE_HOME") or ""
    root = Path(base).expanduser() if base else Path.home() / ".bgate"
    return root / "library"


def _index_path() -> Path:
    return home() / INDEX


def _blob_dir() -> Path:
    return home() / BLOBS


def read_index() -> dict:
    path = _index_path()
    if not path.is_file():
        return {"entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"entries": {}}
    data.setdefault("entries", {})
    return data


def _write_index(data: dict) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _dims(path: Path) -> Optional[list[int]]:
    if path.suffix.lower() not in (".png", ".webp", ".jpg", ".jpeg"):
        return None
    try:
        from PIL import Image
        with Image.open(path) as im:
            return [im.size[0], im.size[1]]
    except Exception:                                             # noqa: BLE001
        return None


def _sidecars(path: Path) -> list[Path]:
    out = []
    for suffix in SIDECAR_SUFFIXES:
        side = path.with_suffix(suffix)
        if side.is_file() and side != path:
            out.append(side)
    return out


def _tags(tags: Optional[Iterable[str]]) -> list[str]:
    seen: list[str] = []
    for t in tags or ():
        t = str(t).strip().lower()
        if t and t not in seen:
            seen.append(t)
    return seen


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------
def _collect(root: Path, paths: Iterable[str]) -> list[Path]:
    """The files a publish call names, directories expanded."""
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        p = p.resolve()
        try:
            p.relative_to(root)
        except ValueError:
            raise LibraryError(f"{raw} is outside the project root {root}; "
                               "publish from inside the project") from None
        if p.is_dir():
            n = 0
            for f in sorted(p.rglob("*")):
                if not f.is_file() or SKIP_DIRS & set(f.relative_to(p).parts):
                    continue
                if f.suffix.lower() not in COLLECTED:
                    continue
                if any(f.name.endswith(s) for s in SIDECAR_SUFFIXES):
                    continue                       # travels with its sheet
                out.append(f)
                n += 1
                if n >= DIR_CAP:
                    raise LibraryError(f"{raw} holds more than {DIR_CAP} "
                                       "publishable files; name a subfolder")
        elif p.is_file():
            if p.suffix.lower() in SKIPPED_SUFFIXES:
                raise LibraryError(f"{p.name} is an engine cache file, not an "
                                   "asset")
            out.append(p)
        else:
            raise LibraryError(f"nothing on disk at {raw}")
    if not out:
        raise LibraryError("nothing to publish: no files matched")
    return out


def publish(root: str | os.PathLike[str], paths: Iterable[str], *,
            tags: Optional[Iterable[str]] = None, collection: str = "",
            note: str = "", project_name: str = "") -> dict:
    """Copy files out of a project into the library.

    Content-addressed: a file already in the library (same bytes, same
    name) gains the new tags and collection and is not stored again;
    ``existing`` names them so the caller can tell a re-publish from a
    first. Same bytes under a new name is a new entry over the same blob.
    """
    root = Path(root).resolve()
    files = _collect(root, paths)
    tags = _tags(tags)
    collection = (collection or "").strip()
    index = read_index()
    entries = index["entries"]
    blobs = _blob_dir()
    blobs.mkdir(parents=True, exist_ok=True)

    published, existing = [], []
    for f in files:
        digest = _sha(f)
        # An ENTRY is bytes under a name; a BLOB is the bytes. Two files
        # with the same content and different names are two entries (a
        # search for either name finds it) sharing one blob on disk.
        eid = hashlib.sha256(f"{digest}:{f.name}".encode()).hexdigest()[:12]
        blob = blobs / f"{digest}{f.suffix.lower()}"
        if not blob.is_file():
            shutil.copyfile(f, blob)
        sides = []
        for side in _sidecars(f):
            side_digest = _sha(side)
            side_blob = blobs / f"{side_digest}{''.join(side.suffixes[-2:])}"
            if not side_blob.is_file():
                shutil.copyfile(side, side_blob)
            sides.append({"name": side.name, "blob": side_blob.name})
        rel = f.relative_to(root).as_posix()
        entry = entries.get(eid)
        if entry:
            entry["tags"] = _tags([*entry.get("tags", []), *tags])
            if collection:
                entry["collection"] = collection
            if note:
                entry["note"] = note
            sources = entry.setdefault("sources", [])
            src = {"project": project_name, "path": rel, "root": str(root)}
            if src not in sources:
                sources.append(src)
            entry["updated_at"] = _stamp()
            if sides:
                entry["sidecars"] = sides
            existing.append(eid)
        else:
            entry = {
                "id": eid, "hash": digest, "name": f.name, "stem": f.stem,
                "ext": f.suffix.lower(), "kind": kind_of(f.name),
                "bytes": f.stat().st_size, "dims": _dims(f),
                "tags": tags, "collection": collection, "note": note,
                "blob": blob.name, "sidecars": sides,
                "sources": [{"project": project_name, "path": rel,
                             "root": str(root)}],
                "published_at": _stamp(), "updated_at": _stamp(),
            }
            entries[eid] = entry
            published.append(eid)
    _write_index(index)
    return {"ok": True, "home": str(home()),
            "published": [entries[e] for e in published],
            "existing": [entries[e] for e in existing],
            "count": len(published) + len(existing)}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def search(query: str = "", *, kind: str = "", tags: Optional[Iterable[str]] = None,
           collection: str = "", limit: int = 100) -> dict:
    """Entries matching every filter given. Empty filters match everything.

    ``query`` is a case-insensitive substring over name, tags, collection
    and note; ``tags`` must ALL be present. Newest first.
    """
    index = read_index()
    q = (query or "").strip().lower()
    want_tags = set(_tags(tags))
    kind = (kind or "").strip().lower()
    collection = (collection or "").strip().lower()
    hits = []
    for entry in index["entries"].values():
        if kind and entry.get("kind") != kind:
            continue
        if collection and (entry.get("collection") or "").lower() != collection:
            continue
        if want_tags and not want_tags <= set(entry.get("tags", [])):
            continue
        if q:
            hay = " ".join([entry.get("name", ""), entry.get("collection", ""),
                            entry.get("note", ""), *entry.get("tags", [])]).lower()
            if q not in hay:
                continue
        hits.append(entry)
    hits.sort(key=lambda e: e.get("updated_at", ""), reverse=True)
    total = len(hits)
    hits = hits[:max(1, int(limit))]
    kinds: dict[str, int] = {}
    collections: dict[str, int] = {}
    for entry in index["entries"].values():
        kinds[entry.get("kind", "unknown")] = kinds.get(entry.get("kind", "unknown"), 0) + 1
        c = entry.get("collection") or ""
        if c:
            collections[c] = collections.get(c, 0) + 1
    return {"home": str(home()), "entries": hits, "matched": total,
            "shown": len(hits), "library_size": len(index["entries"]),
            "kinds": kinds, "collections": collections}


def get(entry_id: str) -> dict:
    """One entry by id, or by an exact filename when the id is unknown."""
    index = read_index()
    entry = index["entries"].get(entry_id)
    if entry:
        return entry
    named = [e for e in index["entries"].values() if e.get("name") == entry_id]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise LibraryError(f"{entry_id!r} names {len(named)} entries; use an id: "
                           + ", ".join(e["id"] for e in named))
    raise LibraryError(f"no library entry {entry_id!r}; library_search lists them")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def import_entries(game_dir: str | os.PathLike[str], ids: Iterable[str], *,
                   dest: str = "assets/library", overwrite: bool = False,
                   rename: str = "") -> dict:
    """Copy entries into a project. Refuses to overwrite unless asked.

    ``dest`` is relative to the engine project (the directory holding
    project.godot, or the web root). Sidecars land beside the file. ``rename``
    names the file when importing exactly one entry.
    """
    game = Path(game_dir).resolve()
    if not game.is_dir():
        raise LibraryError(f"no project directory at {game}")
    dest_path = Path(dest)
    if dest_path.is_absolute() or ".." in dest_path.parts:
        raise LibraryError("dest must be a relative path inside the project")
    ids = list(ids)
    if rename and len(ids) != 1:
        raise LibraryError("rename applies to exactly one entry")
    target_dir = game / dest_path
    imported, refused = [], []
    for eid in ids:
        entry = get(eid)
        blob = _blob_dir() / entry["blob"]
        if not blob.is_file():
            refused.append({"id": entry["id"], "reason": "blob missing from the "
                            "library; publish it again"})
            continue
        name = rename if rename else entry["name"]
        if rename and not Path(rename).suffix:
            name = rename + entry["ext"]
        out = target_dir / name
        if out.is_file() and not overwrite:
            if _sha(out) == entry["hash"]:
                imported.append({"id": entry["id"], "path": out, "landed": False,
                                 "reason": "already present, identical"})
                continue
            refused.append({"id": entry["id"], "reason": f"{out.relative_to(game).as_posix()} "
                            "exists and differs; overwrite=True replaces it"})
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(blob, out)
        sides = []
        for side in entry.get("sidecars") or []:
            side_blob = _blob_dir() / side["blob"]
            if side_blob.is_file():
                side_out = out.with_suffix("".join(Path(side["name"]).suffixes[-2:]))
                shutil.copyfile(side_blob, side_out)
                sides.append(side_out)
        imported.append({"id": entry["id"], "path": out, "landed": True,
                         "sidecars": sides})
    rows = []
    for row in imported:
        rel = row["path"].relative_to(game).as_posix()
        rows.append({**row, "path": rel, "res": f"res://{rel}",
                     "sidecars": [s.relative_to(game).as_posix()
                                  for s in row.get("sidecars", [])]})
    return {"ok": not refused, "imported": rows, "refused": refused,
            "dest": dest_path.as_posix(), "game_dir": str(game)}


# ---------------------------------------------------------------------------
# Forget (CLI only)
# ---------------------------------------------------------------------------
def forget(entry_id: str) -> dict:
    """Drop an entry and its blob when no other entry shares the bytes."""
    index = read_index()
    entry = get(entry_id)
    index["entries"].pop(entry["id"], None)
    shared = any(e.get("blob") == entry["blob"] for e in index["entries"].values())
    removed_blob = False
    if not shared:
        blob = _blob_dir() / entry["blob"]
        if blob.is_file():
            blob.unlink()
            removed_blob = True
    for side in entry.get("sidecars") or []:
        if not any(side["blob"] == s.get("blob")
                   for e in index["entries"].values()
                   for s in e.get("sidecars") or []):
            p = _blob_dir() / side["blob"]
            if p.is_file():
                p.unlink()
    _write_index(index)
    return {"ok": True, "forgot": entry["id"], "name": entry["name"],
            "blob_removed": removed_blob}


def stats() -> dict:
    index = read_index()
    entries = index["entries"].values()
    return {"home": str(home()), "entries": len(index["entries"]),
            "bytes": sum(int(e.get("bytes", 0)) for e in entries),
            "kinds": sorted({e.get("kind", "unknown") for e in entries}),
            "collections": sorted({e.get("collection") for e in entries
                                   if e.get("collection")})}
