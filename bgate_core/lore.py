"""The lore graph — entities, edges, and the atomic facts canon_check reads.

Prose lives in ``lore_entity.body`` for humans. Machine-checkable assertions live
in ``canon_fact`` as one statement each. The split matters: you cannot diff a
paragraph for contradictions, but you can diff a sentence.
"""
from __future__ import annotations

import os

from . import db, search
from .util import rows, slugify

KINDS = ("faction", "character", "place", "event", "item", "concept", "species")
STATUSES = ("draft", "canon", "retired")


def _ref(slug: str) -> str:
    return f"entity:{slug}"


def _index_text(entity: dict, facts: list[dict]) -> str:
    parts = [entity["name"], entity["summary"], entity["body"]]
    parts.extend(f["statement"] for f in facts)
    return "\n".join(p for p in parts if p)


def _reindex_entity(conn, entity_id: int) -> None:
    row = conn.execute("SELECT * FROM lore_entity WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        return
    entity = dict(row)
    facts = rows(conn.execute(
        "SELECT statement FROM canon_fact WHERE entity_id = ?", (entity_id,)))
    search.reindex(conn, _ref(entity["slug"]), f"lore.{entity['kind']}",
                   entity["name"], _index_text(entity, facts))


def add_entity(root: str | os.PathLike[str], kind: str, name: str, summary: str = "",
               body: str = "", status: str = "draft") -> dict:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    slug = slugify(name)
    with db.tx(root) as conn:
        existing = conn.execute(
            "SELECT id FROM lore_entity WHERE slug = ?", (slug,)).fetchone()
        if existing:
            raise ValueError(
                f"entity {slug!r} already exists (id {existing['id']}) — "
                "update it instead of creating a duplicate"
            )
        cur = conn.execute(
            "INSERT INTO lore_entity (kind, name, slug, summary, body, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, name, slug, summary, body, status),
        )
        _reindex_entity(conn, int(cur.lastrowid))
    return get_entity(root, slug)


def update_entity(root: str | os.PathLike[str], ref: str | int, *,
                  summary: str | None = None, body: str | None = None,
                  status: str | None = None) -> dict:
    entity = get_entity(root, ref)
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE lore_entity SET summary = ?, body = ?, status = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (
                entity["summary"] if summary is None else summary,
                entity["body"] if body is None else body,
                entity["status"] if status is None else status,
                entity["id"],
            ),
        )
        _reindex_entity(conn, entity["id"])
    return get_entity(root, entity["id"])


def get_entity(root: str | os.PathLike[str], ref: str | int) -> dict:
    conn = db.connect(root)
    if isinstance(ref, int):
        row = conn.execute("SELECT * FROM lore_entity WHERE id = ?", (ref,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM lore_entity WHERE slug = ? OR name = ?", (ref, ref)).fetchone()
    if row is None:
        raise LookupError(f"no lore entity {ref!r}")
    return dict(row)


def list_entities(root: str | os.PathLike[str], kind: str | None = None,
                  status: str | None = None) -> list[dict]:
    conn = db.connect(root)
    sql = "SELECT * FROM lore_entity WHERE 1 = 1"
    params: list = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if status:
        sql += " AND status = ?"
        params.append(status)
    return rows(conn.execute(sql + " ORDER BY kind, name", params))


def link(root: str | os.PathLike[str], src: str | int, rel: str, dst: str | int,
         note: str = "") -> dict:
    a, b = get_entity(root, src), get_entity(root, dst)
    with db.tx(root) as conn:
        conn.execute(
            "INSERT INTO lore_link (src_id, dst_id, rel, note) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (src_id, dst_id, rel) DO UPDATE SET note = excluded.note",
            (a["id"], b["id"], rel, note),
        )
    return {"src": a["slug"], "rel": rel, "dst": b["slug"], "note": note}


def links_of(root: str | os.PathLike[str], ref: str | int) -> list[dict]:
    """Every edge touching this entity, in or out, resolved to slugs."""
    entity = get_entity(root, ref)
    conn = db.connect(root)
    return rows(conn.execute(
        """
        SELECT 'out' AS dir, l.rel, e.slug, e.name, e.kind, l.note
          FROM lore_link l JOIN lore_entity e ON e.id = l.dst_id
         WHERE l.src_id = ?
        UNION ALL
        SELECT 'in' AS dir, l.rel, e.slug, e.name, e.kind, l.note
          FROM lore_link l JOIN lore_entity e ON e.id = l.src_id
         WHERE l.dst_id = ?
        """,
        (entity["id"], entity["id"]),
    ))


def add_fact(root: str | os.PathLike[str], ref: str | int, statement: str,
             source: str = "", locked: bool = False) -> dict:
    """Assert one atomic fact. ``locked`` marks it as immovable canon."""
    entity = get_entity(root, ref)
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO canon_fact (entity_id, statement, source, locked) "
            "VALUES (?, ?, ?, ?)",
            (entity["id"], statement.strip(), source, 1 if locked else 0),
        )
        fid = int(cur.lastrowid)
        _reindex_entity(conn, entity["id"])
    conn = db.connect(root)
    return dict(conn.execute("SELECT * FROM canon_fact WHERE id = ?", (fid,)).fetchone())


def facts_of(root: str | os.PathLike[str], ref: str | int) -> list[dict]:
    entity = get_entity(root, ref)
    conn = db.connect(root)
    return rows(conn.execute(
        "SELECT * FROM canon_fact WHERE entity_id = ? ORDER BY id", (entity["id"],)))


def all_facts(root: str | os.PathLike[str]) -> list[dict]:
    conn = db.connect(root)
    return rows(conn.execute(
        """
        SELECT f.*, e.slug, e.name, e.status
          FROM canon_fact f LEFT JOIN lore_entity e ON e.id = f.entity_id
         ORDER BY f.id
        """
    ))


def brief(root: str | os.PathLike[str], ref: str | int) -> dict:
    """Everything a narrative agent needs about one entity, in one call."""
    entity = get_entity(root, ref)
    return {
        "entity": entity,
        "facts": facts_of(root, entity["id"]),
        "links": links_of(root, entity["id"]),
    }
