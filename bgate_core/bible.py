"""The design bible — pillars, core loop, scope tiers, and the cut line.

Sections are typed rather than free prose so the Director seat can answer "is
this in scope?" mechanically. The important one is ``cut_line``: everything
ranked below it is explicitly NOT being built, which is the only thing that
reliably stops an agent fleet from gold-plating.
"""
from __future__ import annotations

import os

from . import activity, db, search
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


def dependents(root: str | os.PathLike[str], section_id: int) -> list[dict]:
    """Work items filed under this section. The FK is ON DELETE SET NULL, so a
    plain delete would quietly untier live work and every one of those items
    would stop being checkable against the cut line — silently in scope."""
    conn = db.connect(root)
    return rows(conn.execute(
        "SELECT id, seat, title, status FROM work_item WHERE scope_tier_id = ? "
        "ORDER BY id", (section_id,)))


def remove(root: str | os.PathLike[str], section_id: int, *,
           reassign_to: int | None = None, force: bool = False) -> dict:
    """Delete a section. Work items pointing at it must be dealt with first.

    Three ways, all explicit: nothing points at it and it just goes; pass
    ``reassign_to`` and the work moves to another tier; pass ``force`` and the
    work is untiered on purpose, which is recorded rather than assumed.
    """
    section = get(root, section_id)
    linked = dependents(root, section_id)
    if linked and reassign_to is None and not force:
        raise ValueError(
            f"{len(linked)} work item(s) are filed under {section['title']!r} "
            f"(ids {[i['id'] for i in linked]}) — pass reassign_to to move them "
            "to another tier, or force to untier them deliberately"
        )
    if reassign_to is not None:
        target = get(root, reassign_to)
        if target["kind"] != "scope_tier":
            raise ValueError(
                f"reassign_to must be a scope_tier, not a {target['kind']}")
        if target["id"] == section_id:
            raise ValueError("cannot reassign work to the section being deleted")
    with db.tx(root) as conn:
        if reassign_to is not None:
            conn.execute("UPDATE work_item SET scope_tier_id = ?, "
                         "updated_at = datetime('now') WHERE scope_tier_id = ?",
                         (reassign_to, section_id))
        conn.execute("DELETE FROM bible_section WHERE id = ?", (section_id,))
        search.drop(conn, _ref(section_id))
    if linked:
        where = f"moved to section {reassign_to}" if reassign_to is not None else "untiered"
        activity.log(root, "bible",
                     f"deleted {section['kind']} {section['title'][:60]!r}; "
                     f"{len(linked)} work item(s) {where}",
                     ref=str(section_id))
    return {"deleted": section, "work_items": linked,
            "reassigned_to": reassign_to, "untiered": bool(linked and reassign_to is None)}


def reorder(root: str | os.PathLike[str], kind: str, order: list[int]) -> list[dict]:
    """Rewrite the ranks of one kind to the given id order, 1..N, atomically.

    Rank order IS the scope decision for tiers and the cut line, so a half-applied
    reorder is a wrong cut line, not a cosmetic glitch. BEGIN IMMEDIATE takes the
    write lock before the read that validates the ids, so two concurrent reorders
    serialize instead of each rewriting from a stale view.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    ids = [int(i) for i in order]
    if len(set(ids)) != len(ids):
        raise ValueError("order contains duplicate section ids")
    with db.tx(root) as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        existing = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM bible_section WHERE kind = ? ORDER BY rank, id",
            (kind,)).fetchall()]
        missing = set(ids) - set(existing)
        if missing:
            raise ValueError(f"not {kind} sections of this project: {sorted(missing)}")
        # Anything the caller left out keeps its relative order after the listed
        # ids — a partial reorder must not drop sections off the end of the bible.
        final = ids + [i for i in existing if i not in set(ids)]
        for rank, section_id in enumerate(final, start=1):
            conn.execute("UPDATE bible_section SET rank = ?, "
                         "updated_at = datetime('now') WHERE id = ?",
                         (rank, section_id))
    return list_sections(root, kind)


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
