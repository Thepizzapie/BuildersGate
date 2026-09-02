"""Reference anchors hung off a bible section — the art a pillar MEANS.

The bible is prose. The pinned refs (bgate_core.art.refs / ref_pin) are the images
the project's look is actually defined by. Until now nothing connected the two:
a pillar could say "grimy corporate satire" and the four concept pieces that
settle what that looks like lived in a different view entirely, so every seat
that read the bible read the words and guessed the pictures.

This is bgate_core.store.task_refs with a different parent, deliberately: same
columns, same call shape, same layering rule. task_ref anchors art to ONE WORK
ITEM (a pose, a frame a variant must match); bible_ref anchors it to a piece of
the DESIGN (a pillar, a canon reference section, a constraint). A reader who
knows one module knows this one.

Kept as its own module rather than folded into refs.py because refs.py is about
the pins themselves — creating, versioning and resolving them. Which OTHER
record a pin is hung off is a different concern, and task_refs already
established that it gets its own file. Putting it in refs.py would give that
module two unrelated jobs and leave the two anchoring implementations in
different places.

WHAT IS STORED IS THE PIN NAME, NOT A PATH. refs.pin() versions a re-pin into a
new file and moves the pointer, so a stored path would freeze a section onto
revision 1 of art that has since been redrawn — the section would keep showing
the old image while the project moved on, and nobody would see a mismatch.
Resolution happens at read time.
"""
from __future__ import annotations

import os
from typing import Optional

from ..board import activity
from ..store import db
from ..art import refs
from ..store.util import rows

KINDS = refs.KINDS  # ('character','style','ui','concept')


def add(root: str | os.PathLike[str], section_id: int, ref: str, *,
        kind: str = "style", note: str = "", rank: int = 0) -> dict:
    """Anchor a reference (a global pin name OR a project-relative path) to a
    bible section. Validates that it resolves to a real image now, so the bible
    never carries a dangling anchor — a section that points at nothing is worse
    than one that points at nothing yet, because it looks answered."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    _section_title(root, section_id)  # LookupError before we write a child row
    refs.resolve(root, ref)  # raises LookupError if it points at nothing
    with db.tx(root) as conn:
        conn.execute(
            "INSERT INTO bible_ref (section_id, ref, kind, note, rank) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(section_id, ref) DO UPDATE SET "
            "kind=excluded.kind, note=excluded.note, rank=excluded.rank",
            (section_id, ref, kind, note, rank))
    activity.log(root, "ref", f"anchored '{ref}' to bible section {section_id}",
                 ref=f"bible:{section_id}")
    return {"ok": True, "section_id": section_id, "ref": ref, "kind": kind}


def remove(root: str | os.PathLike[str], section_id: int, ref: str) -> dict:
    with db.tx(root) as conn:
        cur = conn.execute(
            "DELETE FROM bible_ref WHERE section_id = ? AND ref = ?",
            (section_id, ref))
        removed = cur.rowcount
    return {"ok": True, "removed": removed, "ref": ref}


def list_for_section(root: str | os.PathLike[str], section_id: int) -> list[dict]:
    """The section's anchored refs, each with its resolved path + existence flag.

    ``exists`` is reported rather than filtered out: a pin whose file was
    deleted under it is something the user has to SEE, not something a list
    quietly shortens.
    """
    out = rows(db.connect(root).execute(
        "SELECT id, section_id, ref, kind, note, rank, created_at "
        "FROM bible_ref WHERE section_id = ? ORDER BY rank, id",
        (section_id,)))
    for r in out:
        try:
            r["resolved_path"] = refs.resolve(root, r["ref"])
            r["exists"] = os.path.isfile(r["resolved_path"])
        except LookupError:
            r["resolved_path"] = None
            r["exists"] = False
    return out


def list_all(root: str | os.PathLike[str]) -> dict[int, list[dict]]:
    """Every anchored ref in the bible, keyed by section id.

    One query for the whole view. The World bible panel draws N sections at
    once, and a per-section fetch there is N round trips to render one page.
    """
    grouped: dict[int, list[dict]] = {}
    for r in rows(db.connect(root).execute(
            "SELECT id, section_id, ref, kind, note, rank, created_at "
            "FROM bible_ref ORDER BY section_id, rank, id")):
        try:
            r["resolved_path"] = refs.resolve(root, r["ref"])
            r["exists"] = os.path.isfile(r["resolved_path"])
        except LookupError:
            r["resolved_path"] = None
            r["exists"] = False
        grouped.setdefault(int(r["section_id"]), []).append(r)
    return grouped


def resolve_for_section(root: str | os.PathLike[str], section_id: Optional[int],
                        *, kind: Optional[str] = None) -> list[dict]:
    """The LAYERED reference set work under this section should condition on:
    the section's own anchors first (highest priority), then the global pins not
    already covered. Each entry: {ref, kind, note, path, scope: 'bible'|'global'}.

    Same contract as task_refs.resolve_for_task, so a caller that already knows
    how to feed a generator a layered set does not learn a second shape.
    """
    seen: set[str] = set()
    layered: list[dict] = []
    if section_id is not None:
        for r in list_for_section(root, section_id):
            if kind and r["kind"] != kind:
                continue
            if not r.get("resolved_path"):
                continue
            seen.add(r["resolved_path"])
            layered.append({"ref": r["ref"], "kind": r["kind"], "note": r["note"],
                            "path": r["resolved_path"], "scope": "bible"})
    # Same restraint as task_refs.resolve_for_task, same reason: a section
    # with anchors keeps its identity set, and only the style pins — the
    # project's look — ride along uninvited.
    anchored = bool(layered)
    for g in refs.list_refs(root, kind=kind):
        if anchored and not kind and g.get("kind") != "style":
            continue
        path = g.get("path")
        if not path or path in seen:
            continue
        seen.add(path)
        layered.append({"ref": g["name"], "kind": g["kind"], "note": g.get("note", ""),
                        "path": path, "scope": "global"})
    return layered


def suggest_from_titles(root: str | os.PathLike[str]) -> list[dict]:
    """Find pin names people hand-encoded into bible PROSE, and propose anchors.

    THIS IS THE WORKAROUND THIS MODULE REPLACES, and it is already in the live
    data: sections titled "Battle-screen concept (pinned: concept-battle /
    concept-battle-dark)" and "Game shape (concept pack, 8 pinned refs)". Typed
    by hand because there was nowhere structured to put them, and therefore
    unresolvable, unrenderable, and blind to a re-pin.

    It REPORTS, it does not write. Nothing here rewrites a title or creates an
    anchor: the text is something a human typed, the match is a guess made by
    string search, and a migration that quietly edited both would be this
    feature destroying the evidence it was built from. The caller shows the
    proposal and a human accepts it one section at a time (POST the anchor).

    Each entry: {section_id, kind, title, propose: [pin names], already:
    [already anchored], unresolved: [names the prose claims that no pin has]}.
    Sections with nothing to say are left out.
    """
    import re

    from . import bible

    pins = {p["name"]: p for p in refs.list_refs(root)}
    anchored = {sid: {r["ref"] for r in rows_}
                for sid, rows_ in list_all(root).items()}
    out: list[dict] = []
    for section in bible.list_sections(root):
        sid = int(section["id"])
        text = f"{section.get('title') or ''}\n{section.get('body') or ''}"
        low = text.lower()
        have = anchored.get(sid, set())
        propose, already = [], []
        for name in pins:
            # Word-bounded so 'concept-battle' does not also match inside
            # 'concept-battle-dark' and claim an anchor nobody wrote.
            if not re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", low):
                continue
            (already if name in have else propose).append(name)
        # A parenthetical that says "pinned" is a deliberate claim about art. Any
        # name-shaped token in there that is NOT a real pin is worth surfacing:
        # it is either a typo or a pin that was never made, and both look
        # identical to a reader of the title.
        unresolved = []
        for chunk in re.findall(r"\(([^)]*pin[^)]*)\)", low):
            for token in re.findall(r"[a-z0-9][a-z0-9-]{2,}", chunk):
                if token in pins or token in {"pinned", "pins", "pin", "refs",
                                              "ref", "concept", "pack"}:
                    continue
                if token.isdigit() or token in unresolved:
                    continue
                unresolved.append(token)
        if propose or unresolved:
            out.append({"section_id": sid, "kind": section["kind"],
                        "title": section["title"], "propose": sorted(propose),
                        "already": sorted(already), "unresolved": unresolved})
    return out


def _section_title(root: str | os.PathLike[str], section_id: int) -> str:
    """Assert the section exists, and hand back its title for log lines.

    Imported here rather than at module scope: bible.py is a heavier module and
    this is the only thing needed from it — a top-level import would make every
    importer of refs-adjacent code pull in the search index too.
    """
    from . import bible

    return str(bible.get(root, section_id).get("title") or "")
