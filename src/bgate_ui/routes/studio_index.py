"""The studio index over HTTP: its games, shared modules and shared assets.

One read for the dashboard's Shared screen. Three sources, each the ground
truth for its own part:

  * ``studio.status()`` for the games, including a registered game whose
    folder has gone missing (``exists`` false), which is worth seeing.
  * The disk under ``shared/modules/<engine>/<name>/<version>/`` for modules.
    A version folder is what an install reads, so the page lists what is
    there rather than what an index claims.
  * Each game's kit ledger for pins: which game holds which version.

READ ONLY. Publishing a module is ``kit_extract`` and moving a game in is
``bgate studio adopt``; both are deliberate acts, not dashboard clicks.
No studio set is an ordinary answer (``set`` false), not an error. The
payload rides in the ``api.ok`` envelope: the studio status carries its own
``ok``, and a bare top-level ``ok: false`` reads as a failed request to the
shell's readJSON.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

from bgate_core.store import assetlib, kits, studio
from bgate_ui import api

router = APIRouter()


def _modules(root: Path) -> list[dict]:
    base = root / studio.SHARED_DIRNAME / studio.MODULES_DIRNAME
    out = []
    if not base.is_dir():
        return out
    for engine_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        for mod_dir in sorted(p for p in engine_dir.iterdir() if p.is_dir()):
            versions = []
            title = summary = ""
            for ver_dir in sorted((p for p in mod_dir.iterdir() if p.is_dir()),
                                  key=lambda p: p.name):
                manifest = ver_dir / "kit.json"
                if not manifest.is_file():
                    continue
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = {}
                title = data.get("title") or title
                summary = data.get("summary") or summary
                versions.append(ver_dir.name)
            if versions:
                out.append({"engine": engine_dir.name, "name": mod_dir.name,
                            "title": title, "summary": summary,
                            "versions": versions, "pinned_by": []})
    return out


def _attach_pins(modules: list[dict], games: list[dict]) -> None:
    by_name = {m["name"]: m for m in modules}
    for game in games:
        if not game.get("exists"):
            continue
        for name, rec in kits.read_ledger(game["path"]).items():
            mod = by_name.get(name)
            if mod is None or not isinstance(rec, dict) or not rec.get("version"):
                continue
            mod["pinned_by"].append({"game": game["name"],
                                     "version": str(rec["version"])})


@router.get("/api/studio-index")
def studio_index() -> dict:
    status = studio.status()
    if not status.get("ok"):
        return api.ok({**status, "games": [], "modules": [], "assets": None})
    root = Path(status["root"])
    games = status.get("games") or []
    modules = _modules(root)
    _attach_pins(modules, games)
    lib = assetlib.search(limit=50)
    assets = {"home": lib["home"], "count": lib["library_size"],
              "kinds": lib["kinds"], "collections": lib["collections"],
              "bytes": assetlib.stats()["bytes"],
              "recent": [{k: e.get(k) for k in ("id", "name", "kind", "collection",
                                                "tags", "bytes", "dims", "updated_at")}
                         for e in lib["entries"]]}
    return api.ok({**status, "games": games, "modules": modules, "assets": assets})
