"""FTS5 recall over the bible, lore, and canon facts.

Lexical only, on purpose: it has no daemon, no model download, and no cold start.
Semantic recall (fastembed) can layer on top later behind the same ``find()``
signature — callers should not care which engine answered.
"""
from __future__ import annotations

import sqlite3

from .util import rows


def reindex(conn: sqlite3.Connection, ref: str, kind: str, title: str, text: str) -> None:
    """Upsert one document. ``ref`` is a stable id like 'entity:ashen-order'."""
    conn.execute("DELETE FROM search_idx WHERE ref = ?", (ref,))
    conn.execute(
        "INSERT INTO search_idx (ref, kind, title, text) VALUES (?, ?, ?, ?)",
        (ref, kind, title, text),
    )


def drop(conn: sqlite3.Connection, ref: str) -> None:
    conn.execute("DELETE FROM search_idx WHERE ref = ?", (ref,))


def find(conn: sqlite3.Connection, query: str, limit: int = 10,
         kind: str | None = None) -> list[dict]:
    """Search. Returns [{ref, kind, title, snippet}] ranked best-first."""
    match = _sanitize(query)
    if not match:
        return []
    sql = (
        "SELECT ref, kind, title, snippet(search_idx, 3, '[', ']', ' … ', 16) AS snippet "
        "FROM search_idx WHERE search_idx MATCH ?"
    )
    params: list = [match]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    try:
        return rows(conn.execute(sql, params))
    except sqlite3.OperationalError:
        # Malformed FTS expression despite sanitizing — treat as no results
        # rather than blowing up an agent's tool call.
        return []


def _sanitize(query: str) -> str:
    """FTS5 treats punctuation as syntax. Quote each bare word as a literal."""
    words = [w for w in query.replace('"', " ").split() if w.strip()]
    return " OR ".join(f'"{w}"' for w in words)
