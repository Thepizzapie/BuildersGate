"""The design bible — pillars, core loop, scope tiers, and the cut line.

Sections are typed rather than free prose so the Director seat can answer "is
this in scope?" mechanically. The important one is ``cut_line``: everything
ranked below it is explicitly NOT being built, which is the only thing that
reliably stops an agent fleet from gold-plating.
"""
from __future__ import annotations

import os

from . import db, search
from .util import rows

KINDS = ("pillar", "loop", "scope_tier", "cut_line", "constraint", "reference")


def _ref(section_id: int) -> str:
    return f"bible:{section_id}"


def add(root: str | os.PathLike[str], kind: str, title: str, body: str = "",
        rank: int = 0) -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO bible_section (kind, title, body, rank) VALUES (?, ?, ?, ?)",
            (kind, title, body, rank),
        )
        sid = int(cur.lastrowid)
        search.reindex(conn, _ref(sid), f"bible.{kind}", title, f"{title}\n{body}")
    return get(root, sid)


def update(root: str | os.PathLike[str], section_id: int, *, title: str | None = None,
           body: str | None = None, rank: int | None = None) -> dict:
    current = get(root, section_id)
    title = current["title"] if title is None else title
    body = current["body"] if body is None else body
    rank = current["rank"] if rank is None else rank
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE bible_section SET title = ?, body = ?, rank = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (title, body, rank, section_id),
        )
        search.reindex(conn, _ref(section_id), f"bible.{current['kind']}",
                       title, f"{title}\n{body}")
    return get(root, section_id)


def remove(root: str | os.PathLike[str], section_id: int) -> None:
    with db.tx(root) as conn:
        conn.execute("DELETE FROM bible_section WHERE id = ?", (section_id,))
        search.drop(conn, _ref(section_id))


def get(root: str | os.PathLike[str], section_id: int) -> dict:
    conn = db.connect(root)
    row = conn.execute("SELECT * FROM bible_section WHERE id = ?", (section_id,)).fetchone()
    if row is None:
        raise LookupError(f"no bible section {section_id}")
    return dict(row)


def list_sections(root: str | os.PathLike[str], kind: str | None = None) -> list[dict]:
    conn = db.connect(root)
    if kind:
        return rows(conn.execute(
            "SELECT * FROM bible_section WHERE kind = ? ORDER BY rank, id", (kind,)))
    return rows(conn.execute("SELECT * FROM bible_section ORDER BY kind, rank, id"))


def in_scope(root: str | os.PathLike[str], rank: int) -> bool:
    """True when ``rank`` sits above the cut line (lower rank = higher priority).

    With no cut line set, everything is in scope — an unset cut line means the
    team hasn't made the scope call yet, not that the scope is infinite.
    """
    line = cut_line(root)
    return True if line is None else rank < line["rank"]


def cut_line(root: str | os.PathLike[str]) -> dict | None:
    conn = db.connect(root)
    row = conn.execute(
        "SELECT * FROM bible_section WHERE kind = 'cut_line' ORDER BY rank LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def overview(root: str | os.PathLike[str]) -> dict:
    """The whole bible, grouped — what a seat reads before starting work."""
    grouped: dict[str, list[dict]] = {k: [] for k in KINDS}
    for section in list_sections(root):
        grouped[section["kind"]].append(section)
    line = cut_line(root)
    scope = grouped["scope_tier"]
    return {
        "pillars": grouped["pillar"],
        "loop": grouped["loop"],
        "constraints": grouped["constraint"],
        "references": grouped["reference"],
        "cut_line": line,
        "in_scope": [s for s in scope if line is None or s["rank"] < line["rank"]],
        "cut": [] if line is None else [s for s in scope if s["rank"] >= line["rank"]],
    }
