"""Dialogue trees as an engine-loadable resource, validated BEFORE they land.

The narrative seat owns ``design/**``, ``game/dialogue/**`` and ``content/**``,
and had no tool that produced a file in any of them: lore_* writes the graph in
the database and canon_check reads text back, so the one artifact the seat's
mission names — "the lore graph, quests, and dialogue" — could only be produced
by hand-writing JSON and hoping.

WHY VALIDATION IS THE POINT, not the paperwork. A dialogue tree fails in ways
that are invisible in the file and expensive in the game:

  * a choice whose target does not exist — the branch dead-ends at runtime, on
    a line most players never pick, weeks after it was written;
  * a node nothing reaches — content that was paid for, reviewed, and is not in
    the game, with nothing anywhere saying so;
  * a reachable node from which no ending is reachable — the player is stuck in
    a conversation loop with no way out, which reads as a hang.

None of the three is visible to a JSON schema and all three are cheap to prove
on a graph. So the graph is checked and the write is REFUSED, naming the
offending node — a warning attached to a landed file is a warning nobody reads.

CANON RUNS ON THE WAY IN. The seat's own mission says canon_check goes on every
narrative write, and a check the author has to remember is a check that happens
on the good days. A hard conflict refuses; a review-level flag rides along in
the result, because "this name is not in the lore graph yet" is normal for a
first draft and must not stop it.

The file is plain JSON under the seat's lane, one object Godot's ``JSON.parse``
loads directly. Not a .tres: a Resource script would have to exist in the game
before the writer could produce anything, and dialogue is data the engine reads,
not a class it instantiates.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from ..store.project import game_dir
from ..store.util import slugify

FORMAT_VERSION = 1
SUFFIX = ".dialogue.json"

MAX_NODES = 400
MAX_CHOICES = 12
MAX_TEXT = 4000
MAX_ID = 64


class DialogueError(ValueError):
    """A tree this module refuses to write, with the offending node named."""


# ---------------------------------------------------------------------------
# Where it lives
# ---------------------------------------------------------------------------
def dialogue_dir(root: str | os.PathLike[str]) -> Path:
    """``<godot project>/dialogue`` — inside the narrative lane for BOTH layouts.

    ``bgate init`` puts project.godot at the root while godot_scaffold puts it
    in ``<root>/game``, and seats.lanes_for_layout re-roots ``game/dialogue/**``
    to ``dialogue/**`` for the first. Resolving from game_dir() lands inside the
    lane either way; hardcoding ``game/dialogue`` would write outside it — and
    be refused by the write hook — on every CLI-created project.
    """
    base = game_dir(root) or (Path(root) / "game")
    return base / "dialogue"


def path_for(root: str | os.PathLike[str], name: str) -> Path:
    return dialogue_dir(root) / f"{safe_name(name)}{SUFFIX}"


def safe_name(name: str) -> str:
    slug = slugify(str(name or ""))
    if slug == "unnamed" and not str(name or "").strip():
        raise DialogueError("a dialogue needs a name — it becomes the filename")
    return slug[:MAX_ID]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _node(raw: dict, index: int) -> dict:
    where = f"nodes[{index}]"
    if not isinstance(raw, dict):
        raise DialogueError(f"{where} must be an object")
    node_id = str(raw.get("id") or "").strip()
    if not node_id:
        raise DialogueError(f"{where} has no id — every node needs one, it is "
                            "what choices point at")
    if len(node_id) > MAX_ID:
        raise DialogueError(f"{where} id {node_id!r} is longer than {MAX_ID} chars")

    choices = []
    raw_choices = raw.get("choices") or []
    if not isinstance(raw_choices, list):
        raise DialogueError(f"node {node_id!r}: choices must be a list")
    if len(raw_choices) > MAX_CHOICES:
        raise DialogueError(f"node {node_id!r} offers {len(raw_choices)} choices; "
                            f"the cap is {MAX_CHOICES}")
    for i, choice in enumerate(raw_choices):
        if not isinstance(choice, dict):
            raise DialogueError(f"node {node_id!r} choice {i} must be an object")
        text = str(choice.get("text") or "").strip()
        goto = str(choice.get("goto") or choice.get("to") or "").strip()
        if not text:
            raise DialogueError(f"node {node_id!r} choice {i} has no text — a "
                                "player cannot pick an unlabelled option")
        if not goto:
            raise DialogueError(f"node {node_id!r} choice {i} ({text!r}) has no "
                                "goto — say which node it leads to")
        choices.append({"text": text[:MAX_TEXT], "goto": goto,
                        "tag": str(choice.get("tag") or "")[:64],
                        "condition": str(choice.get("condition") or "")[:200]})

    return {
        "id": node_id,
        "speaker": str(raw.get("speaker") or "")[:80],
        "text": str(raw.get("text") or "")[:MAX_TEXT],
        "end": bool(raw.get("end")),
        "choices": choices,
        "tags": [str(t)[:40] for t in (raw.get("tags") or [])][:12],
        "note": str(raw.get("note") or "")[:400],
    }


def validate(nodes: Iterable[dict], start: str = "") -> dict:
    """Normalise and prove the graph. Raises :class:`DialogueError` naming the
    node at fault; returns ``{"nodes": [...], "start": id, "ends": [...]}``."""
    parsed = [_node(raw, i) for i, raw in enumerate(nodes or [])]
    if not parsed:
        raise DialogueError("a dialogue needs at least one node")
    if len(parsed) > MAX_NODES:
        raise DialogueError(f"{len(parsed)} nodes; the cap is {MAX_NODES}")

    seen: dict[str, int] = {}
    for i, node in enumerate(parsed):
        if node["id"] in seen:
            raise DialogueError(
                f"two nodes share the id {node['id']!r} (nodes[{seen[node['id']]}] "
                f"and nodes[{i}]) — a choice pointing at it would be ambiguous")
        seen[node["id"]] = i
    by_id = {node["id"]: node for node in parsed}

    entry = str(start or "").strip() or parsed[0]["id"]
    if entry not in by_id:
        raise DialogueError(f"start node {entry!r} is not one of the nodes "
                            f"({', '.join(sorted(by_id))})")

    # 1. Every choice target exists. THE failure this whole function is for:
    #    a typo'd goto is invisible in the file and dead-ends in the game.
    for node in parsed:
        for i, choice in enumerate(node["choices"]):
            if choice["goto"] not in by_id:
                raise DialogueError(
                    f"node {node['id']!r} choice {i} ({choice['text']!r}) points "
                    f"at {choice['goto']!r}, which is not a node in this "
                    f"dialogue — fix the target or add the node")
        if node["end"] and node["choices"]:
            raise DialogueError(
                f"node {node['id']!r} is marked end but still offers "
                f"{len(node['choices'])} choice(s) — an ending has nowhere to go")
        if not node["end"] and not node["choices"]:
            raise DialogueError(
                f"node {node['id']!r} has no choices and is not marked end — the "
                "conversation stops there with no way out. Add a choice, or set "
                "end: true if it IS an ending")

    # 2. Everything is reachable from the start.
    reachable = {entry}
    frontier = [entry]
    while frontier:
        node = by_id[frontier.pop()]
        for choice in node["choices"]:
            if choice["goto"] not in reachable:
                reachable.add(choice["goto"])
                frontier.append(choice["goto"])
    orphans = sorted(set(by_id) - reachable)
    if orphans:
        raise DialogueError(
            f"nothing reaches {', '.join(repr(o) for o in orphans)} from "
            f"{entry!r} — written, paid for, and not in the game. Link it or "
            "drop it")

    # 3. An ending is reachable from EVERY reachable node. A cycle with no exit
    #    is not a syntax error and not a crash; it is a conversation the player
    #    cannot leave, which reads as a hang.
    ends = sorted(n["id"] for n in parsed if n["end"])
    if not ends:
        raise DialogueError("no node is marked end — this conversation never "
                            "finishes. Mark the closing line(s) end: true")
    escapes = set(ends)
    changed = True
    while changed:
        changed = False
        for node in parsed:
            if node["id"] in escapes:
                continue
            if any(c["goto"] in escapes for c in node["choices"]):
                escapes.add(node["id"])
                changed = True
    trapped = sorted(reachable - escapes)
    if trapped:
        raise DialogueError(
            f"no ending is reachable from {', '.join(repr(t) for t in trapped)} "
            "— the player enters and cannot leave. Give one of those nodes a "
            "choice that leads to an ending")

    return {"nodes": parsed, "start": entry, "ends": ends}


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------
def dialogue_text(nodes: Iterable[dict]) -> str:
    """Every written word in the tree, for the canon check. Lines and choice
    labels both — a contradiction is as likely in an option as in a speech."""
    parts = []
    for node in nodes:
        if node.get("text"):
            parts.append(str(node["text"]))
        for choice in node.get("choices") or []:
            if choice.get("text"):
                parts.append(str(choice["text"]))
    return "\n".join(parts)


def write(root: str | os.PathLike[str], name: str, nodes: Iterable[dict], *,
          start: str = "", title: str = "", summary: str = "",
          allow_canon_conflict: bool = False) -> dict:
    """Validate, canon-check, then land the tree. In that order, deliberately.

    Nothing is written until the graph proves out and canon has had its say: a
    file on disk is a file somebody imports, and "it landed but it is broken"
    is the state this refuses to create.
    """
    slug = safe_name(name)
    graph = validate(nodes, start=start)

    canon = _canon_check(root, dialogue_text(graph["nodes"]))
    if (canon.get("verdict") == "conflict") and not allow_canon_conflict:
        hard = [f.get("message") or f.get("code")
                for f in canon.get("flags") or [] if f.get("level") == "conflict"]
        raise DialogueError(
            f"canon_check refuses {slug!r}: {'; '.join(str(h) for h in hard)}. "
            "Fix the line, update the fact it contradicts, or pass "
            "allow_canon_conflict=True if the canon is what changed")

    doc = {
        "format": "bgate.dialogue",
        "version": FORMAT_VERSION,
        "name": slug,
        "title": str(title or name)[:120],
        "summary": str(summary or "")[:600],
        "start": graph["start"],
        "ends": graph["ends"],
        "nodes": graph["nodes"],
    }
    target = path_for(root, slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    except OSError as exc:
        raise DialogueError(f"could not write {target.name}: {exc}") from exc

    return {
        "name": slug,
        "path": str(target),
        "rel_path": _relative(root, target),
        "res_path": _res_path(root, target),
        "nodes": len(doc["nodes"]),
        "start": doc["start"],
        "ends": doc["ends"],
        "choices": sum(len(n["choices"]) for n in doc["nodes"]),
        # Always present, conflict or not: a review-level flag is the normal
        # state of a first draft and the writer should see it without asking.
        "canon": {"verdict": canon.get("verdict"),
                  "flags": canon.get("flags") or []},
    }


def read(root: str | os.PathLike[str], name: str) -> dict:
    path = path_for(root, name)
    if not path.is_file():
        raise LookupError(
            f"no dialogue {safe_name(name)!r} in {dialogue_dir(root)} — "
            "dialogue_list shows what is there")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DialogueError(f"{path.name} is unreadable: {exc}") from exc
    doc["path"] = str(path)
    doc["rel_path"] = _relative(root, path)
    doc["res_path"] = _res_path(root, path)
    return doc


def list_dialogues(root: str | os.PathLike[str]) -> list[dict]:
    """Every tree in the lane. A file that no longer validates says so here —
    the listing is where a broken tree is cheapest to notice."""
    base = dialogue_dir(root)
    if not base.is_dir():
        return []
    out = []
    for path in sorted(base.glob(f"*{SUFFIX}")):
        entry = {"name": path.name[:-len(SUFFIX)], "path": str(path),
                 "rel_path": _relative(root, path), "nodes": 0, "start": "",
                 "title": "", "ok": False, "error": ""}
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            entry["title"] = str(doc.get("title") or "")
            entry["start"] = str(doc.get("start") or "")
            entry["nodes"] = len(doc.get("nodes") or [])
            validate(doc.get("nodes") or [], start=entry["start"])
            entry["ok"] = True
        except (OSError, ValueError, DialogueError) as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        out.append(entry)
    return out


def _canon_check(root: str | os.PathLike[str], text: str) -> dict:
    """canon.check, degraded to 'nothing to say' when it cannot run.

    A project with no lore graph, or a database this call cannot open, must not
    be a reason a writer cannot write. The verdict is reported either way, so a
    skipped check is visible rather than mistaken for a clean one.
    """
    if not text.strip():
        return {"verdict": "ok", "flags": [], "skipped": "no text"}
    try:
        from . import canon

        return canon.check(root, text)
    except Exception as exc:
        return {"verdict": "unchecked", "flags": [],
                "skipped": f"{type(exc).__name__}: {exc}"}


def _relative(root: str | os.PathLike[str], path: Path) -> str:
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path)


def _res_path(root: str | os.PathLike[str], path: Path) -> str:
    """The ``res://`` the game loads it by, or '' when there is no Godot project."""
    base = game_dir(root)
    if base is None:
        return ""
    try:
        return "res://" + path.resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return ""
