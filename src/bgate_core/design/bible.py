"""The design bible — pillars, core loop, constraints, references.

Sections are typed rather than free prose so a reader can find the pillars
without reading the whole document, and so the seat brief can quote the parts
that bound the work.

SCOPE TIERS AND THE CUT LINE USED TO LIVE HERE, as two more kinds plus a
``cut_line()`` lookup and a reassign-or-untier cascade on delete, all of it in
service of a queue gate that in practice never refused anything. It was three
panels of the World view and a chunk of every brief for a mechanism nobody
filed work against. Removed 2026-08-10. Sections of the old kinds may still
exist in an older project's database — nothing here creates or lists them, and
the readers below tolerate them rather than crashing on a kind they no longer
know.
"""
from __future__ import annotations

import hashlib
import os

from ..board import activity
from ..store import db, search
from ..store.util import rows

KINDS = ("pillar", "loop", "constraint", "reference")


class StaleWrite(ValueError):
    """The section changed since the caller read it. Refuse, do not merge."""

    def __init__(self, section_id: int, expected: str, actual: str) -> None:
        super().__init__(
            f"bible section {section_id} changed since you loaded it "
            f"(you had {expected}, stored is {actual}) — reload and reapply; "
            "saving would erase the other edit")
        self.section_id, self.expected, self.actual = section_id, expected, actual


def _ref(section_id: int) -> str:
    return f"bible:{section_id}"


def version_of(section: dict) -> str:
    """Content version of a section — what an editor holds while it edits.

    A hash rather than updated_at: SQLite stores whole seconds, and two saves in
    the same second are exactly the collision this is meant to catch.
    """
    blob = "\x00".join((str(section.get("title") or ""),
                        str(section.get("body") or ""),
                        str(section.get("rank") or 0)))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


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
           body: str | None = None, rank: int | None = None,
           expected_version: str | None = None) -> dict:
    """Edit a section. A partial edit is a read-modify-write, so it is done
    under the write lock and, when the caller says what it was editing
    (``expected_version``, from :func:`version_of`), refused if the section has
    moved since. Without that the second of two editors silently wins, and the
    pillars are the last place in the product where that is acceptable.
    """
    with db.tx(root) as conn:
        # The lock must be taken BEFORE the read the merge is based on.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM bible_section WHERE id = ?",
                           (section_id,)).fetchone()
        if row is None:
            raise LookupError(f"no bible section {section_id}")
        current = dict(row)
        actual = version_of(current)
        if expected_version is not None and expected_version != actual:
            raise StaleWrite(section_id, expected_version, actual)
        title = current["title"] if title is None else title
        body = current["body"] if body is None else body
        rank = current["rank"] if rank is None else rank
        conn.execute(
            "UPDATE bible_section SET title = ?, body = ?, rank = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (title, body, rank, section_id),
        )
        search.reindex(conn, _ref(section_id), f"bible.{current['kind']}",
                       title, f"{title}\n{body}")
    return get(root, section_id)


def remove(root: str | os.PathLike[str], section_id: int) -> dict:
    """Delete a section, and the search row that pointed at it.

    THIS USED TO BE A NEGOTIATION. work_item carried a scope_tier_id pointing
    here, so deleting a tier had to first move that work somewhere
    (``reassign_to``) or untier it on the record (``force``), and refuse until
    the caller picked. The column is gone with the cut line, nothing references
    a section any more, and a delete is a delete.
    """
    section = get(root, section_id)
    with db.tx(root) as conn:
        conn.execute("DELETE FROM bible_section WHERE id = ?", (section_id,))
        search.drop(conn, _ref(section_id))
    activity.log(root, "bible",
                 f"deleted {section['kind']} {section['title'][:60]!r}",
                 ref=str(section_id))
    return {"deleted": section}


def reorder(root: str | os.PathLike[str], kind: str, order: list[int]) -> list[dict]:
    """Rewrite the ranks of one kind to the given id order, 1..N, atomically.

    Rank order is the reading order of a design document, so a half-applied
    reorder is a bible that argues with itself. BEGIN IMMEDIATE takes the write
    lock before the read that validates the ids, so two concurrent reorders
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
    section = dict(row)
    # Handed out with every read so an editor can pass it back to update() and
    # find out it was beaten, instead of overwriting the winner.
    section["version"] = version_of(section)
    return section


def list_sections(root: str | os.PathLike[str], kind: str | None = None) -> list[dict]:
    conn = db.connect(root)
    if kind:
        listed = rows(conn.execute(
            "SELECT * FROM bible_section WHERE kind = ? ORDER BY rank, id", (kind,)))
    else:
        listed = rows(conn.execute("SELECT * FROM bible_section ORDER BY kind, rank, id"))
    for section in listed:  # the editor edits from this list; it needs the version
        section["version"] = version_of(section)
    return listed


def overview(root: str | os.PathLike[str]) -> dict:
    """The whole bible, grouped — what a seat reads before starting work.

    setdefault rather than a fixed four keys: a project that authored scope
    tiers before the cut line was removed still has rows of a kind KINDS no
    longer names, and reading the bible must not be the thing that breaks on
    them. They land in their own bucket, which no caller asks for.
    """
    grouped: dict[str, list[dict]] = {k: [] for k in KINDS}
    for section in list_sections(root):
        grouped.setdefault(section["kind"], []).append(section)
    return {
        "pillars": grouped["pillar"],
        "loop": grouped["loop"],
        "constraints": grouped["constraint"],
        "references": grouped["reference"],
    }
