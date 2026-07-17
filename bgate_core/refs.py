"""Pinned reference anchors — the canonical images art derives from.

The problem this solves: an approved character reference or style anchor is the
single most valuable artifact in a generated-art pipeline, and it was living in
scratch output dirs, found by path guesswork, one cleanup away from gone. A pin
copies the file into ``.bgate/refs/`` (durable, travels with the project),
names it, and surfaces it in every seat brief — so every art agent starts from
the same anchors instead of re-deriving or, worse, re-generating them.

resolve() lets image tools accept a pin NAME anywhere they accept a path.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from . import activity, db
from .util import rows, slugify

REFS_DIRNAME = "refs"
KINDS = ("character", "style", "ui", "concept")


def _refs_dir(root: str | os.PathLike[str]) -> Path:
    return Path(root) / db.DB_DIRNAME / REFS_DIRNAME


def pin(root: str | os.PathLike[str], name: str, src_path: str, *,
        kind: str = "style", note: str = "") -> dict:
    """Pin a reference: copy it into .bgate/refs/ under a canonical name.

    Re-pinning an existing name replaces its file — that's the intended way to
    upgrade an anchor (the name stays stable, everything referencing it follows).
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    src = Path(src_path)
    if not src.is_file():
        raise FileNotFoundError(f"no file at {src_path}")
    slug = slugify(name)

    dest_dir = _refs_dir(root)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{slug}{src.suffix.lower()}"
    shutil.copy2(src, dest)

    with db.tx(root) as conn:
        conn.execute(
            """
            INSERT INTO ref_pin (name, path, kind, note) VALUES (?, ?, ?, ?)
            ON CONFLICT (name) DO UPDATE SET
                path = excluded.path, kind = excluded.kind, note = excluded.note
            """,
            (slug, str(dest), kind, note),
        )
    activity.log(root, "ref_pin", f"pinned reference {slug!r} ({kind})", ref=str(dest))
    return get(root, slug)


def get(root: str | os.PathLike[str], name: str) -> dict:
    row = db.connect(root).execute(
        "SELECT * FROM ref_pin WHERE name = ?", (slugify(name),)).fetchone()
    if row is None:
        raise LookupError(f"no pinned reference {name!r}")
    return dict(row)


def list_refs(root: str | os.PathLike[str], kind: Optional[str] = None) -> list[dict]:
    conn = db.connect(root)
    if kind:
        return rows(conn.execute(
            "SELECT * FROM ref_pin WHERE kind = ? ORDER BY name", (kind,)))
    return rows(conn.execute("SELECT * FROM ref_pin ORDER BY kind, name"))


def unpin(root: str | os.PathLike[str], name: str) -> dict:
    """Remove a pin (keeps the file — deleting canon art is a human's job)."""
    entry = get(root, name)
    with db.tx(root) as conn:
        conn.execute("DELETE FROM ref_pin WHERE name = ?", (entry["name"],))
    return entry


def resolve(root: str | os.PathLike[str], name_or_path: str) -> str:
    """A pin name -> its file path; an existing path passes through untouched.

    Missing on both counts raises — silently generating against a nonexistent
    reference produces an unconditioned image that LOOKS like a result.
    """
    try:
        return get(root, name_or_path)["path"]
    except LookupError:
        pass
    if Path(name_or_path).is_file():
        return str(name_or_path)
    raise LookupError(
        f"{name_or_path!r} is neither a pinned reference nor an existing file — "
        "pin it first (ref_pin) or pass a real path"
    )
