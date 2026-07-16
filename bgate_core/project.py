"""Project identity — the single row every other table hangs off."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from . import db
from .util import slugify

ENGINES = ("godot", "none")
DIMENSIONS = ("2d", "3d", "2d+3d")


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
    return get(root)


def get(root: str | os.PathLike[str]) -> dict:
    conn = db.connect(root)
    row = conn.execute("SELECT * FROM project WHERE id = 1").fetchone()
    if row is None:
        raise LookupError(f"no Builders Gate project at {root} — run init first")
    return dict(row)


def require_root(start: Optional[str | os.PathLike[str]] = None) -> Path:
    """Find the enclosing project or explain how to make one."""
    root = db.resolve_root(start)
    if root is None:
        raise LookupError(
            f"no .bgate project found at or above {Path(start or os.getcwd()).resolve()} "
            "— run project_init first"
        )
    return root
