"""Project identity — the single row every other table hangs off."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from . import db
from .util import slugify

ENGINES = ("godot", "none")
DIMENSIONS = ("2d", "3d", "2d+3d")

# Machine-wide registry of every project ever init'ed/selected, so a session
# whose cwd is NOWHERE NEAR the project (e.g. an MCP server spawned from a
# different repo) can still find and select it by name. Best-effort JSON —
# losing it only costs rediscovery, never data.
REGISTRY_PATH = Path.home() / ".bgate" / "projects.json"


def _read_registry() -> dict[str, str]:
    try:
        return {k: v for k, v in json.loads(
            REGISTRY_PATH.read_text(encoding="utf-8")).items()
            if (Path(v) / db.DB_DIRNAME / db.DB_FILENAME).exists()}
    except Exception:
        return {}


def register(root: str | os.PathLike[str], name: str = "") -> None:
    """Record root in the machine-wide registry (best-effort, never raises)."""
    try:
        resolved = str(Path(root).resolve())
        if not name:
            try:
                name = get(resolved)["slug"]
            except Exception:
                name = Path(resolved).name
        reg = _read_registry()
        reg[name] = resolved
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY_PATH.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    except Exception:
        pass


def known_projects() -> dict[str, str]:
    """{name: root} for every registered project that still exists on disk."""
    return _read_registry()


def init(root: str | os.PathLike[str], name: str, pitch: str = "",
         engine: str = "godot", dimension: str = "2d") -> dict:
    """Create ``<root>/.bgate/game.db``. Idempotent: re-init updates metadata."""
    if engine not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")
    if dimension not in DIMENSIONS:
        raise ValueError(f"dimension must be one of {DIMENSIONS}, got {dimension!r}")

    Path(root).mkdir(parents=True, exist_ok=True)
    with db.tx(root) as conn:
        conn.execute(
            """
            INSERT INTO project (id, name, slug, pitch, engine, dimension)
            VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name,
                slug = excluded.slug,
                pitch = excluded.pitch,
                engine = excluded.engine,
                dimension = excluded.dimension,
                updated_at = datetime('now')
            """,
            (name, slugify(name), pitch, engine, dimension),
        )
    register(root, slugify(name))
    return get(root)


def get(root: str | os.PathLike[str]) -> dict:
    conn = db.connect(root)
    row = conn.execute("SELECT * FROM project WHERE id = 1").fetchone()
    if row is None:
        raise LookupError(f"no Builders Gate project at {root} — run init first")
    return dict(row)


def game_dir(root: str | os.PathLike[str]) -> Optional[Path]:
    """Where this project's Godot project.godot actually lives, or None.

    Two entrypoints disagree about the layout: godot_scaffold (MCP) writes into
    ``<root>/game``, while ``bgate init`` and the dashboard's new-project route
    write straight into ``<root>``. Everything downstream that hardcoded one of
    the two silently did nothing for projects made the other way — the web
    export was unreachable for every CLI-created project for exactly that
    reason. Ask here instead of guessing.
    """
    base = Path(root)
    for candidate in (base / "game", base):
        if (candidate / "project.godot").is_file():
            return candidate
    return None


def require_root(start: Optional[str | os.PathLike[str]] = None) -> Path:
    """Find the enclosing project or explain how to make one."""
    root = db.resolve_root(start)
    if root is None:
        known = known_projects()
        hint = (f" Known projects (use project_select): {known}"
                if known else " Run project_init first.")
        raise LookupError(
            f"no .bgate project found at or above {Path(start or os.getcwd()).resolve()}."
            + hint
        )
    return root
