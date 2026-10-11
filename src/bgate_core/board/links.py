"""What a work item builds on and what builds on it, as prompt text.

Shared by the dashboard's dispatch prompt and the MCP server's
queue_claim_next, so an agent that picks up its next ticket mid-session gets
the same foundation an agent spawned for it would.
"""
from __future__ import annotations

import os

from . import queue


def written_by(root: str | os.PathLike[str], item_id: int, cap: int = 12) -> list[str]:
    """Paths the hook saw this item's runs write, newest last, deduplicated."""
    try:
        from ..store import provenance as _prov, writelog as _wl
        seen: list[str] = []
        for rec in _wl.entries(root, f"item-{int(item_id)}"):
            rel = _prov._norm(rec.get("path"))
            if rel and rel not in seen:
                seen.append(rel)
        return seen[-cap:]
    except Exception:
        return []


def block(root: str | os.PathLike[str], item: dict) -> str:
    """What this item BUILDS ON and what BUILDS ON IT, printed under the item.

    MEASURED (adoore-island, 2026-10-03): 129 tickets done in dependency order
    and the game did not contain them. Every seat proved its piece in a side
    scene, because the prompt said only "wait for #N" - it never said what #N
    delivered, so "depends on" meant "starts after", not "builds on". The
    director ended up filing an eight-link integration chain to wire into the
    real game what the board had already paid for once. The fix is to hand the
    agent its upstream's result and files, and say plainly that they are the
    foundation, and to tell it who waits on its own output.
    """
    try:
        ups = queue.parents(root, int(item["id"]))
    except Exception:
        ups = []
    downs: list[dict] = []
    try:
        from ..store import db as _db
        conn = _db.connect(root)
        ids = {int(r[0]) for r in conn.execute(
            "SELECT id FROM work_item WHERE depends_on = ?", (int(item["id"]),))}
        try:
            ids |= {int(r[0]) for r in conn.execute(
                "SELECT item_id FROM work_item_dep WHERE depends_on = ? "
                "AND (cut_at IS NULL OR cut_at = '')", (int(item["id"]),))}
        except Exception:
            pass
        for i in sorted(ids):
            try:
                d = queue.get(root, i)
            except Exception:
                continue
            if d.get("status") not in ("cancelled",):
                downs.append(d)
    except Exception:
        downs = []
    if not ups and not downs:
        return ""
    out: list[str] = []
    if ups:
        out.append("BUILDS ON - this item depends on the work below. It is the "
                   "FOUNDATION, not background: use what it delivered IN PLACE "
                   "(extend its scene, call its script, instance its asset, read "
                   "its data). Do not rebuild a parallel version, and do not prove "
                   "your piece in a separate side scene - finish it wired into "
                   "what the game actually runs. If what it delivered does not fit "
                   "your item, ask_director before building around it.")
        for p in ups[:8]:
            try:
                d = queue.get(root, p)
            except Exception:
                continue
            note = " ".join(str(d.get("result") or "").split())
            if len(note) > 420:
                note = note[:419] + "…"
            line = (f"  #{p} [{d.get('seat')}] {str(d.get('title') or '')[:90]}"
                    f" - {d.get('status')}")
            out.append(line)
            if note:
                out.append(f"    delivered: {note}")
            files = written_by(root, p)
            if files:
                out.append("    files: " + ", ".join(files))
    if downs:
        out.append("WAITS ON YOU - these start from what you land. Land it in the "
                   "game they will build on, under names they can find:")
        for d in downs[:8]:
            out.append(f"  #{d['id']} [{d.get('seat')}] {str(d.get('title') or '')[:90]}")
    return "\n" + "\n".join(out) + "\n"
