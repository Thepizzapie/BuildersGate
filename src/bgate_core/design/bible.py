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
import re

from ..board import activity
from ..store import db, search
from ..store.util import rows

KINDS = ("pillar", "loop", "constraint", "reference")

# WHO STATED A SECTION. '' is the historical value (every row before 0047 and
# every row nobody labelled); 'human' is the one the gates read. A constraint
# the human stated is a RULING: the seats it binds see it at the top of their
# brief, the tools it forbids refuse to run for them, and dispatch refuses a
# brief that names a forbidden tool. EXIT 67 measured the alternative, where a
# night-one statement ("this game needs a 2D rig, frame sheets will not carry
# it") lived in a chat transcript and thirty-three agents built on frame sheets.
STATED_BY = ("", "human", "director", "agent")
HUMAN = "human"

_LIST_SPLIT = re.compile(r"[,\s]+")


class NotAConstraint(ValueError):
    """binds/forbids only mean something on a constraint."""


def _csv(value, field: str) -> str:
    """Normalise a list-ish field (list, tuple or comma string) to a sorted,
    de-duplicated, lower-cased comma string. '' stays ''."""
    if value is None:
        return ""
    if isinstance(value, str):
        items = _LIST_SPLIT.split(value.strip())
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = [str(v) for v in value]
    else:
        raise ValueError(f"{field} must be a list or a comma-separated string, "
                         f"got {type(value).__name__}")
    clean = sorted({i.strip().lower() for i in items if i and i.strip()})
    for item in clean:
        if not re.fullmatch(r"[a-z0-9_\-\.\*]+", item):
            raise ValueError(f"{field} entry {item!r} is not a seat or tool name")
    return ",".join(clean)


def _provenance(kind: str, stated_by, binds, forbids) -> dict:
    who = str(stated_by or "").strip().lower()
    if who not in STATED_BY:
        raise ValueError(f"stated_by must be one of {STATED_BY}, got {stated_by!r}")
    b = _csv(binds, "binds")
    f = _csv(forbids, "forbids")
    if (b or f) and kind != "constraint":
        raise NotAConstraint(
            f"binds/forbids only apply to a constraint, not a {kind}")
    return {"stated_by": who, "binds": b, "forbids": f}


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
        rank: int = 0, *, stated_by: str = "", binds=None, forbids=None) -> dict:
    """Add a section. ``stated_by``/``binds``/``forbids`` are the ruling fields,
    see STATED_BY; they are validated here and nowhere else."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    prov = _provenance(kind, stated_by, binds, forbids)
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO bible_section (kind, title, body, rank, stated_by, "
            "binds, forbids) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, title, body, rank, prov["stated_by"], prov["binds"],
             prov["forbids"]),
        )
        sid = int(cur.lastrowid)
        search.reindex(conn, _ref(sid), f"bible.{kind}", title, f"{title}\n{body}")
    if prov["stated_by"] == HUMAN:
        activity.log(root, "bible",
                     f"HUMAN RULING recorded: {title[:70]!r}"
                     + (f" binds {prov['binds']}" if prov["binds"] else "")
                     + (f" forbids {prov['forbids']}" if prov["forbids"] else ""),
                     ref=str(sid))
    return get(root, sid)


def update(root: str | os.PathLike[str], section_id: int, *, title: str | None = None,
           body: str | None = None, rank: int | None = None,
           expected_version: str | None = None, stated_by: str | None = None,
           binds=None, forbids=None) -> dict:
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
        prov = _provenance(
            current["kind"],
            current.get("stated_by", "") if stated_by is None else stated_by,
            current.get("binds", "") if binds is None else binds,
            current.get("forbids", "") if forbids is None else forbids)
        conn.execute(
            "UPDATE bible_section SET title = ?, body = ?, rank = ?, "
            "stated_by = ?, binds = ?, forbids = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (title, body, rank, prov["stated_by"], prov["binds"],
             prov["forbids"], section_id),
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


# ---------------------------------------------------------------------------
# Rulings: the constraints the human stated, and what they forbid
# ---------------------------------------------------------------------------

def _binds(section: dict, seat: str) -> bool:
    bound = [b for b in str(section.get("binds") or "").split(",") if b]
    return not bound or (seat or "").strip().lower() in bound


def rulings(root: str | os.PathLike[str], seat: str = "") -> list[dict]:
    """Constraints the HUMAN stated that reach ``seat`` ('' = all of them).

    Untruncated on purpose: a seat brief trims the bible to a page, and the one
    thing that must never fall off that page is the sentence the human said
    the game depends on.
    """
    out = []
    for section in list_sections(root, "constraint"):
        if str(section.get("stated_by") or "") != HUMAN:
            continue
        if seat and not _binds(section, seat):
            continue
        out.append({
            "id": section["id"], "title": section["title"],
            "body": section["body"],
            "binds": [b for b in str(section.get("binds") or "").split(",") if b],
            "forbids": [f for f in str(section.get("forbids") or "").split(",") if f],
        })
    return out


def forbidden_tools(root: str | os.PathLike[str], seat: str) -> dict[str, dict]:
    """tool name -> the ruling that forbids it, for one seat."""
    out: dict[str, dict] = {}
    for ruling in rulings(root, seat):
        for tool in ruling["forbids"]:
            out.setdefault(tool, ruling)
    return out


def tool_forbidden(root: str | os.PathLike[str], seat: str, tool: str) -> dict | None:
    """The ruling that forbids ``tool`` for ``seat``, or None. A pattern ending
    in ``*`` forbids every tool with that prefix (``image_sprites*``)."""
    name = (tool or "").strip().lower()
    if not name or not seat:
        return None
    for pattern, ruling in forbidden_tools(root, seat).items():
        if pattern == name or (pattern.endswith("*") and name.startswith(pattern[:-1])):
            return ruling
    return None


def brief_violations(root: str | os.PathLike[str], seat: str, text: str) -> list[dict]:
    """Forbidden tools a brief NAMES. A brief that says "use image_sprites"
    to a seat the human forbade it for is an item that will fail at its first
    tool call, after it has been briefed and billed - refuse it at dispatch.
    """
    hay = (text or "").lower()
    if not hay:
        return []
    out = []
    for pattern, ruling in forbidden_tools(root, seat).items():
        stem = pattern[:-1] if pattern.endswith("*") else pattern
        if stem and re.search(r"(?<![a-z0-9_])" + re.escape(stem), hay):
            out.append({"tool": pattern, "section_id": ruling["id"],
                        "title": ruling["title"]})
    return out


def describe_rulings(root: str | os.PathLike[str], seat: str,
                     body_chars: int = 700) -> str:
    """The block a dispatched agent reads under its item. '' when none."""
    got = rulings(root, seat)
    if not got:
        return ""
    lines = ["HUMAN RULINGS BINDING THIS SEAT. These are not preferences to "
             "test; the human stated them and the harness enforces them. Work "
             "that depends on the other choice does not start:"]
    for r in got:
        body = (r["body"] or "").strip()
        if len(body) > body_chars:
            body = body[:body_chars] + " ...[bible_read for the rest]"
        line = f"- [bible #{r['id']}] {r['title']}"
        if body:
            line += f": {body}"
        if r["forbids"]:
            line += (f" FORBIDDEN TOOLS for you: {', '.join(r['forbids'])} "
                     "(the call is refused; do not route around it).")
        lines.append(line)
    return "\n".join(lines)
