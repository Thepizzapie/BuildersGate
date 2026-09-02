"""What is publishable, and what do we know about it.

Everything here reads. Nothing here exports, copies, or writes — so `bgate
publish --dry-run` can answer "what would ship" without touching Godot or the
output directory.

The per-game facts come from three places, in increasing authority:

  1. the project store (name, pitch, dimension) — always there
  2. the game itself (input map -> the controls list, icon -> the fallback art)
  3. ``<root>/.bgate/site.json`` — the human's overrides, which win

That order matters. A pitch written for the design bible is a fine tagline for
a first publish, but it is not a store page, and the human must be able to say
so without editing the design bible to fix the website.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

from bgate_core.level import controls
from bgate_core.store import db, project

# Where a per-project site override lives. Inside .bgate/ because it is metadata
# about the project, not part of the game the engine loads.
OVERRIDE_NAME = "site.json"

# Site-wide config, looked for in the cwd then the user's ~/.bgate.
CONFIG_NAMES = ("arcade.json",)

COVER_SUFFIXES = (".png", ".webp", ".jpg", ".jpeg", ".gif")

# Where a cover is looked for, relative to the project root. First hit wins.
COVER_CANDIDATES = (
    "cover", "art/cover", "assets/cover", ".bgate/cover", "game/cover",
)

DEFAULT_CONFIG = {
    "title": "Arcade",
    "tagline": "Games built in Builders Gate.",
    "about": "",
    "author": "",
    "url": "",
    "accent": "#ff6a3d",
}


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def site_config(explicit: str | os.PathLike[str] | None = None) -> dict:
    """Site-wide settings: an explicit --config, else ./arcade.json, else ~/.bgate.

    Never raises on a malformed file — a typo'd JSON should degrade to defaults
    with the rest of the publish intact, not abort a build that is otherwise
    fine. The caller reports which file (if any) was used.
    """
    config = dict(DEFAULT_CONFIG)
    config["source"] = ""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    else:
        for name in CONFIG_NAMES:
            candidates.append(Path.cwd() / name)
            candidates.append(Path.home() / ".bgate" / name)
    for path in candidates:
        if path.is_file():
            found = _read_json(path)
            if found:
                config.update(found)
            config["source"] = str(path)
            break
    return config


def overrides(root: str | os.PathLike[str]) -> dict:
    return _read_json(Path(root) / db.DB_DIRNAME / OVERRIDE_NAME)


def _find_cover(root: Path, game: Optional[Path], override: str = "") -> Optional[Path]:
    """A real image for the tile, or None — never a stand-in that looks like art.

    A generated placeholder screenshot would be a lie about a game nobody has
    seen yet. When there is nothing, the card falls back to the game's own icon
    on a flat plate, which reads as "no cover" at a glance.
    """
    if override:
        explicit = (root / override).resolve()
        try:  # never let a cover path escape the project
            explicit.relative_to(root.resolve())
        except ValueError:
            return None
        return explicit if explicit.is_file() else None
    for stem in COVER_CANDIDATES:
        for suffix in COVER_SUFFIXES:
            candidate = root / f"{stem}{suffix}"
            if candidate.is_file():
                return candidate
    if game is not None:
        icon = game / "icon.svg"
        if icon.is_file():
            return icon
    return None


def _build_facts(root: Path) -> dict:
    from bgate_ui import webbuild
    status = webbuild.status(root)
    web = root / "export" / "web"
    total = 0
    if web.is_dir():
        total = sum(p.stat().st_size for p in web.rglob("*") if p.is_file())
    return {
        "built": bool(status.get("built")),
        "stale": bool(status.get("stale")),
        "build_reason": status.get("reason", ""),
        "build_dir": str(web),
        "build_bytes": total,
        "build_mtime": status.get("build_mtime", 0.0),
    }


def describe(root: str | os.PathLike[str]) -> dict:
    """Everything the site needs about one project. Raises LookupError if the
    path is not a Builders Gate project at all."""
    root = Path(root).resolve()
    meta = project.get(root)          # LookupError if this is not a project
    game = project.game_dir(root)
    over = overrides(root)

    slug = str(over.get("slug") or meta.get("slug") or root.name)
    card = {
        "slug": slug,
        "root": str(root),
        "game_dir": str(game) if game else "",
        "title": str(over.get("title") or meta.get("name") or slug),
        "tagline": str(over.get("tagline") or meta.get("pitch") or ""),
        "description": str(over.get("description") or ""),
        "credits": str(over.get("credits") or ""),
        "dimension": str(meta.get("dimension") or ""),
        "engine": str(meta.get("engine") or ""),
        "tags": [str(t) for t in over.get("tags", []) if str(t).strip()],
        "hidden": bool(over.get("hidden", False)),
        "order": over.get("order"),
        "controls": controls.for_project(root) if game else [],
    }
    card.update(_build_facts(root))
    cover = _find_cover(root, game, str(over.get("cover") or ""))
    card["cover"] = str(cover) if cover else ""
    card["publishable"] = bool(game) and not card["hidden"]
    if not game:
        card["skip_reason"] = "no Godot project (nothing to export)"
    elif card["hidden"]:
        card["skip_reason"] = "hidden: true in .bgate/site.json"
    else:
        card["skip_reason"] = ""
    return card


def discover(roots: Optional[Iterable[str | os.PathLike[str]]] = None) -> list[dict]:
    """Describe every candidate project, publishable or not.

    roots=None means the machine-wide registry plus the enclosing project of the
    cwd — the registry alone misses a project you just cloned and have never
    opened, and the cwd alone misses every other game you have ever made.

    Cards come back sorted: explicit `order` first, then title. Unpublishable
    ones are INCLUDED, carrying skip_reason — a publish that silently drops a
    game the human expected to see is the failure mode worth designing against.
    """
    paths: list[Path] = []
    if roots is not None:
        paths = [Path(r) for r in roots]
    else:
        paths = [Path(p) for p in project.known_projects().values()]
        here = db.resolve_root()
        if here is not None:
            paths.append(Path(here))

    seen: set[str] = set()
    cards: list[dict] = []
    for path in paths:
        try:
            resolved = str(path.resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            cards.append(describe(path))
        except LookupError:
            continue          # a registry entry whose store is gone
        except Exception as exc:   # a broken project must not sink the publish
            cards.append({
                "slug": path.name, "root": resolved, "title": path.name,
                "publishable": False, "hidden": False, "built": False,
                "skip_reason": f"could not read project ({type(exc).__name__}: {exc})",
                "tagline": "", "description": "", "credits": "", "tags": [],
                "controls": [], "cover": "", "dimension": "", "engine": "",
                "game_dir": "", "build_dir": "", "build_bytes": 0,
                "build_mtime": 0.0, "stale": True, "build_reason": "", "order": None,
            })

    def rank(card: dict) -> tuple:
        order = card.get("order")
        pinned = isinstance(order, (int, float)) and not isinstance(order, bool)
        return (0 if pinned else 1, float(order) if pinned else 0.0,
                str(card.get("title", "")).lower())

    cards.sort(key=rank)
    return cards
