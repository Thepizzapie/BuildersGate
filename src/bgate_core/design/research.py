"""Pre-production research: what the games this one is like got right and wrong.

A project used to start from the pitch alone. The director settled a thesis,
wrote the pillars and laid out the board from one paragraph, and the question
every designer asks first - "which games is this like, and what did they learn
the hard way" - was answered from memory or not at all. This module is that
question as a step between the kickoff brief and the director's first turn:

  1. ``suggest`` asks a research agent for 5-8 comparable games, each with a
     reason and a relation (same loop, same setting, same audience, same look,
     or a cautionary tale). They land as ``proposed``; nothing further is spent
     until someone confirms them (``set_status``) or adds their own
     (``add_comp``).
  2. ``teardown`` researches one confirmed comparable, system by system, and
     stores a verdict per system - worked / failed / mixed - with a confidence
     and the sources behind it. One comparable per call, so a slow or failed
     search costs one row, not the whole pass.
  3. ``grid`` lines the systems up against the comparables.
  4. ``draft_plan`` combines the pitch, the brief and the findings into a
     proposal: pillars, core loop, setting, art direction, what not to build,
     the decisions still open, and optionally a mechanical thesis. It is saved
     as a draft and writes nothing else.
  5. ``adopt`` writes an approved plan into the bible, the not-building list,
     the decision log and the greenlight thesis. The MCP tool that reaches it
     is human-only, the same gate as brainstorm_deploy: a plan is read by the
     person whose game it is before it becomes canon.

THE RESEARCH AGENT IS THE ONE PROCESS HERE WITH THE WEB, AND ONLY THE WEB.
Every other session Builders Gate spawns is denied WebSearch and WebFetch. The
researcher is the deliberate exception and it is kept narrow: see
bgate_ui.agents.researcher, which builds it with those two tools and nothing
else - no file tools, no shell, no MCP server. What it returns is text, which
this module parses and validates before a row is written.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ..board import activity as _activity
from ..board import handoff as _handoff
from ..store import db, search
from ..store import project as _project
from ..store import workspace as _workspace
from . import bible as _bible
from . import decisions as _decisions
from . import greenlight as _greenlight

RELATIONS = ("loop", "setting", "audience", "art", "cautionary", "other")
STATUSES = ("proposed", "confirmed", "dropped", "researched")
VERDICTS = ("worked", "failed", "mixed")
CONFIDENCE = ("high", "medium", "low")
STANCES = ("keep", "avoid", "twist")

# The systems every teardown is asked about, so the grid's rows line up across
# comparables. The agent may add others; these are the vocabulary it is asked
# to reuse rather than inventing "progression (meta)" beside "meta-progression".
SYSTEMS = ("core loop", "moment-to-moment", "progression", "meta-progression",
           "economy", "difficulty", "onboarding", "content variety",
           "narrative delivery", "setting", "art style", "audio", "ui",
           "replayability", "monetization")

PLAN_SEAT = "director"
PLAN_KEY = "research/plan"
MAX_COMPS = 12
MAX_BRIEF_CONTEXT = 8_000
MAX_TEXT = 2_000
MAX_SOURCES = 8
RESEARCH_TITLE = "Comparables research"

Think = Callable[..., dict]


class AlreadyAdopted(ValueError):
    """The plan was adopted once already; adopting again needs ``again``."""


# ── helpers ────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(value: Any, limit: int = MAX_TEXT) -> str:
    return " ".join(str(value or "").split())[:limit]


def _system_name(value: Any) -> str:
    name = _clean(value, 60).lower().strip(" .:-")
    return re.sub(r"\s*[-_/]\s*", "-", name) if name else ""


def _choice(value: Any, allowed: tuple, fallback: str) -> str:
    got = _clean(value, 20).lower()
    return got if got in allowed else fallback


def _sources(raw: Any) -> list[dict]:
    out = []
    for entry in (raw or [])[:MAX_SOURCES] if isinstance(raw, list) else []:
        if isinstance(entry, str):
            entry = {"url": entry}
        if not isinstance(entry, dict):
            continue
        url = _clean(entry.get("url"), 500)
        if not url.startswith(("http://", "https://")):
            continue
        out.append({"url": url, "title": _clean(entry.get("title"), 200)})
    return out


def parse_json(text: str) -> dict:
    """The JSON object in an agent's answer. Tolerates a ```json fence and prose
    either side of it; raises ValueError when there is no object to find."""
    raw = str(text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
    if fence:
        raw = fence.group(1)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("the research agent's answer held no JSON object")
    try:
        got = json.loads(raw[start:end + 1])
    except ValueError as exc:
        raise ValueError(f"the research agent's JSON did not parse: {exc}")
    if not isinstance(got, dict):
        raise ValueError("the research agent's answer was not a JSON object")
    return got


def context(root) -> dict:
    """What the researcher is told about this game: name, pitch, brief."""
    root = Path(root)
    try:
        proj = _project.get(root)
    except Exception:
        proj = {}
    brief = ""
    path = root / "design" / "brief.md"
    try:
        brief = path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if len(brief) > MAX_BRIEF_CONTEXT:
        brief = brief[:MAX_BRIEF_CONTEXT] + " ..."
    return {"name": str(proj.get("name") or ""),
            "pitch": str(proj.get("pitch") or ""),
            "engine": str(proj.get("engine") or ""),
            "dimension": str(proj.get("dimension") or ""),
            "brief": brief}


def _context_text(ctx: dict) -> str:
    lines = [f"GAME: {ctx.get('name') or '(unnamed)'}"]
    if ctx.get("pitch"):
        lines.append(f"PITCH: {ctx['pitch']}")
    shape = " ".join(x for x in (ctx.get("dimension"), ctx.get("engine")) if x)
    if shape:
        lines.append(f"BUILT AS: {shape}")
    if ctx.get("brief"):
        lines += ["", "BRIEF:", ctx["brief"]]
    return "\n".join(lines)


def _default_think() -> Think:
    # Imported late for the same reason brainstorm imports brainsession late:
    # a project whose dashboard package is broken must still READ its research.
    from bgate_ui.agents import researcher

    return researcher.run


# ── comparables ────────────────────────────────────────────────────────────

def _comp_row(row) -> dict:
    return dict(row) if row is not None else {}


def get_comp(root, comp_id: int) -> dict:
    row = db.connect(root).execute(
        "SELECT * FROM research_comp WHERE id = ?", (int(comp_id),)).fetchone()
    if row is None:
        raise LookupError(f"no comparable {comp_id}")
    return _comp_row(row)


def list_comps(root, status: str = "") -> list[dict]:
    sql = ("SELECT c.*, (SELECT COUNT(*) FROM research_finding f "
           "WHERE f.comp_id = c.id) AS findings FROM research_comp c")
    args: tuple = ()
    if status:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        sql += " WHERE c.status = ?"
        args = (status,)
    sql += " ORDER BY c.status = 'dropped', c.id"
    return [dict(r) for r in db.connect(root).execute(sql, args).fetchall()]


def add_comp(root, title: str, why: str = "", relation: str = "loop", *,
             status: str = "confirmed", by: str = "") -> dict:
    """Add a comparable. A title already on the list comes back as it is, with
    ``existed`` set - suggesting a game twice is not an error, re-researching
    it by accident would be."""
    title = _clean(title, 120)
    if not title:
        raise ValueError("a comparable needs a title")
    if status not in STATUSES or status == "researched":
        raise ValueError("a new comparable is proposed or confirmed")
    conn = db.connect(root)
    row = conn.execute("SELECT * FROM research_comp WHERE title = ?",
                       (title,)).fetchone()
    if row is not None:
        return {**_comp_row(row), "existed": True}
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO research_comp (title, why, relation, status, by) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, _clean(why, 600), _choice(relation, RELATIONS, "other"),
             status, by or _activity.current_actor()))
        cid = int(cur.lastrowid)
    return {**get_comp(root, cid), "existed": False}


def set_status(root, comp_id: int, status: str) -> dict:
    """Confirm or drop a comparable. ``researched`` is set by teardown only."""
    if status not in ("proposed", "confirmed", "dropped"):
        raise ValueError("status must be proposed, confirmed or dropped")
    get_comp(root, comp_id)
    with db.tx(root) as conn:
        conn.execute("UPDATE research_comp SET status = ? WHERE id = ?",
                     (status, int(comp_id)))
    return get_comp(root, comp_id)


# ── findings ───────────────────────────────────────────────────────────────

def validate_findings(raw: Any) -> tuple[list[dict], list[dict]]:
    """``(kept, rejected)``. Agent output is filtered row by row: one malformed
    system must not throw away the nine good ones next to it, and each one
    thrown out is reported with why."""
    items = raw.get("findings") if isinstance(raw, dict) else raw
    kept, rejected = [], []
    for i, entry in enumerate(items if isinstance(items, list) else []):
        if not isinstance(entry, dict):
            rejected.append({"index": i, "why": "not an object"})
            continue
        system = _system_name(entry.get("system"))
        verdict = _clean(entry.get("verdict"), 20).lower()
        claim = _clean(entry.get("claim"))
        why = ("no system" if not system else
               f"verdict {verdict!r} is not one of {VERDICTS}"
               if verdict not in VERDICTS else "no claim" if not claim else "")
        if why:
            rejected.append({"index": i, "system": system, "why": why})
            continue
        sources = _sources(entry.get("sources"))
        confidence = _choice(entry.get("confidence"), CONFIDENCE, "medium")
        if not sources:
            # Unsourced is allowed - some verdicts are common knowledge - but
            # it may not claim to be well-founded.
            confidence = "low"
        kept.append({"system": system, "verdict": verdict,
                     "confidence": confidence, "claim": claim,
                     "evidence": _clean(entry.get("evidence")),
                     "sources": sources})
    return kept, rejected


def record(root, comp_id: int, raw: Any) -> dict:
    """Store a teardown. Replaces the comparable's earlier findings: a re-run
    is a correction, and two verdicts on one system from two runs would be a
    contradiction nobody asked the grid to show."""
    comp = get_comp(root, comp_id)
    kept, rejected = validate_findings(raw)
    if not kept:
        raise ValueError(f"the teardown of {comp['title']!r} produced no usable "
                         f"findings ({len(rejected)} rejected)")
    summary = _clean(raw.get("summary") if isinstance(raw, dict) else "")
    with db.tx(root) as conn:
        old = conn.execute("SELECT id FROM research_finding WHERE comp_id = ?",
                           (comp["id"],)).fetchall()
        for row in old:
            search.drop(conn, f"research:{row['id']}")
        conn.execute("DELETE FROM research_finding WHERE comp_id = ?",
                     (comp["id"],))
        for f in kept:
            cur = conn.execute(
                "INSERT INTO research_finding (comp_id, system, verdict, "
                "confidence, claim, evidence, sources_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (comp["id"], f["system"], f["verdict"], f["confidence"],
                 f["claim"], f["evidence"], json.dumps(f["sources"])))
            fid = int(cur.lastrowid)
            search.reindex(conn, f"research:{fid}", "research.finding",
                           f"{comp['title']}: {f['system']} ({f['verdict']})",
                           f"{f['claim']}\n{f['evidence']}")
        conn.execute(
            "UPDATE research_comp SET status = 'researched', summary = ?, "
            "researched_at = datetime('now') WHERE id = ?",
            (summary, comp["id"]))
    _activity.log(root, "research",
                  f"researched {comp['title']}: {len(kept)} system(s)",
                  seat=PLAN_SEAT, ref=f"research_comp:{comp['id']}")
    return {"comp": get_comp(root, comp["id"]), "kept": len(kept),
            "rejected": rejected}


def findings(root, *, system: str = "", comp_id: Optional[int] = None,
             verdict: str = "", query: str = "", limit: int = 100) -> list[dict]:
    """Findings, newest comparable last. ``system`` matches loosely
    ("progression" finds "meta-progression"); ``query`` is a substring over
    the claim and the evidence."""
    sql = ("SELECT f.*, c.title AS comp FROM research_finding f "
           "JOIN research_comp c ON c.id = f.comp_id WHERE c.status != 'dropped'")
    args: list = []
    if system:
        sql += " AND f.system LIKE ?"
        args.append(f"%{_system_name(system)}%")
    if comp_id:
        sql += " AND f.comp_id = ?"
        args.append(int(comp_id))
    if verdict:
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}")
        sql += " AND f.verdict = ?"
        args.append(verdict)
    if query:
        sql += " AND (f.claim LIKE ? OR f.evidence LIKE ?)"
        args += [f"%{query}%", f"%{query}%"]
    sql += " ORDER BY f.system, c.id LIMIT ?"
    args.append(max(1, min(int(limit or 100), 500)))
    out = []
    for row in db.connect(root).execute(sql, args).fetchall():
        item = dict(row)
        try:
            item["sources"] = json.loads(item.pop("sources_json") or "[]")
        except ValueError:
            item["sources"] = []
        out.append(item)
    return out


def grid(root) -> dict:
    """Systems down the side, comparables across the top, and - once a plan is
    drafted - this game's stance on each system as the last column."""
    rows: dict[str, dict] = {}
    comps = [c["title"] for c in list_comps(root) if c["status"] == "researched"]
    for f in findings(root, limit=500):
        cell = {"verdict": f["verdict"], "confidence": f["confidence"],
                "claim": f["claim"]}
        rows.setdefault(f["system"], {})[f["comp"]] = cell
    ours = {s["system"]: s for s in plan(root).get("stances", [])}
    order = {name: i for i, name in enumerate(SYSTEMS)}
    systems = sorted(rows, key=lambda s: (order.get(s, len(order)), s))
    return {"comps": comps,
            "rows": [{"system": s, "cells": rows[s],
                      "ours": ours.get(s)} for s in systems]}


# ── prompts ────────────────────────────────────────────────────────────────

SUGGEST_SYSTEM = """You are a game-design researcher doing pre-production for a \
new game. Use web search to find the games it is most usefully compared to, \
and to check each one is real and that your reason for picking it is \
accurate.

Pick 5 to 8. Mix them on purpose: games with the same core loop, games with \
the same setting or tone, the game this audience is already playing, a game \
with the look it is going for, and at least one cautionary tale - a game that \
tried something close and failed or disappointed, and why. Prefer games with \
enough written about them (reviews, postmortems, talks, player discussion) to \
research properly.

Answer with ONE JSON object and nothing else:
{"comps": [{"title": "...", "relation": "loop|setting|audience|art|cautionary|other", \
"why": "one or two sentences: what about it is comparable and what we can learn"}]}"""


def teardown_system() -> str:
    return f"""You are a game-design researcher tearing down one existing game \
for a team about to build something similar. Use web search and fetch real \
sources: reviews, postmortems, developer talks and interviews, store-page \
player reviews, patch notes, community discussion. Do not guess from memory \
where a source can be found.

For each system that matters in this game, say whether it WORKED, FAILED or \
was MIXED - judged by how players and critics received it, not by whether it \
exists - and why. Cover the systems relevant to the new game first. Use these \
system names where they fit, so teardowns line up: {", ".join(SYSTEMS)}. \
Add others only when none fits.

Confidence: "high" when several independent sources agree, "medium" when one \
good source supports it, "low" when it is thin or inferred. Every finding \
names the sources it rests on.

Answer with ONE JSON object and nothing else:
{{"summary": "two or three sentences: what this game teaches the new one",
 "findings": [{{"system": "...", "verdict": "worked|failed|mixed",
   "confidence": "high|medium|low", "claim": "one sentence verdict",
   "evidence": "what the sources say, briefly",
   "sources": [{{"url": "https://...", "title": "..."}}]}}]}}"""


PLAN_SYSTEM = f"""You are the lead designer turning pre-production research \
into a plan for a new game. You have the creator's pitch and brief and a \
teardown of comparable games. The creator's idea leads: the research tells \
you what to keep, what to avoid and what to twist, not what game to make.

Answer with ONE JSON object and nothing else:
{{"summary": "the plan in three sentences",
 "pillars": [{{"title": "...", "body": "what it means and what it rules out"}}],
 "core_loop": "the loop, minute to minute and session to session",
 "setting": "world, tone, era, what the player is and why they care",
 "art_direction": "look, palette, camera/projection, references to comparables",
 "stances": [{{"system": "one of the teardown's system names",
   "stance": "{'|'.join(STANCES)}", "note": "what we do and which comparable taught us"}}],
 "not_building": [{{"text": "...", "reason": "the comparable or finding that argues against it"}}],
 "decisions": [{{"title": "an open question only the creator can answer",
   "acceptance": "how we would know the answer was right",
   "leaves_dark": "what choosing either way leaves unresolved"}}],
 "thesis": {{"sentence": "the decision the player repeatedly makes",
   "options": ["...", "..."], "stakes": "...", "tension": "...",
   "dominant_strategy": "...", "cadence": "..."}}}}

3 to 5 pillars. Every stance and every not-building entry names the \
comparable it came from. Put in `decisions` what the pitch genuinely leaves \
open rather than deciding it yourself. Omit `thesis` if the pitch does not yet \
support a real decision."""


def suggest_prompt(ctx: dict, existing: list[str]) -> str:
    text = _context_text(ctx)
    if existing:
        text += ("\n\nALREADY ON THE LIST (do not repeat): "
                 + ", ".join(existing))
    return text + "\n\nFind the comparable games."


def teardown_prompt(ctx: dict, comp: dict) -> str:
    return (_context_text(ctx)
            + f"\n\nTEAR DOWN: {comp['title']}"
            + (f"\nWHY IT WAS PICKED ({comp['relation']}): {comp['why']}"
               if comp.get("why") else ""))


def plan_prompt(ctx: dict, digest: str, notes: str = "") -> str:
    return (_context_text(ctx) + "\n\n" + digest
            + (f"\n\nCREATOR'S NOTES: {notes}" if notes else ""))


def digest(root) -> str:
    """Every researched comparable and its verdicts, one line per system."""
    lines = ["RESEARCH:"]
    by_comp: dict[int, list[dict]] = {}
    for f in findings(root, limit=500):
        by_comp.setdefault(f["comp_id"], []).append(f)
    for comp in list_comps(root, status="researched"):
        lines.append(f"\n## {comp['title']} ({comp['relation']})")
        if comp.get("summary"):
            lines.append(comp["summary"])
        for f in by_comp.get(comp["id"], []):
            lines.append(f"- {f['system']}: {f['verdict'].upper()} "
                         f"[{f['confidence']}] {f['claim']}")
    return "\n".join(lines)


# ── the three agent steps ──────────────────────────────────────────────────

def _ask(think: Optional[Think], root, system: str, prompt: str,
         kind: str) -> tuple[dict, dict]:
    answer = (think or _default_think())(root, system=system, prompt=prompt,
                                         kind=kind)
    meta = {k: answer[k] for k in ("model", "seconds", "usd") if k in answer}
    if not answer.get("ok"):
        raise RuntimeError(str(answer.get("error")
                               or "the research agent did not answer"))
    return parse_json(answer.get("text", "")), meta


def suggest(root, *, think: Optional[Think] = None) -> dict:
    """Ask for comparables. They land as ``proposed``; nothing is researched
    until one is confirmed."""
    existing = [c["title"] for c in list_comps(root)]
    if len(existing) >= MAX_COMPS:
        raise ValueError(f"{len(existing)} comparables already; {MAX_COMPS} "
                         "is the cap - drop some before asking for more")
    raw, meta = _ask(think, root, SUGGEST_SYSTEM,
                     suggest_prompt(context(root), existing), "suggest")
    added, skipped = [], []
    for entry in raw.get("comps") or []:
        if not isinstance(entry, dict) or not _clean(entry.get("title")):
            skipped.append({"entry": entry, "why": "no title"})
            continue
        if len(existing) + len(added) >= MAX_COMPS:
            skipped.append({"entry": entry.get("title"), "why": "cap reached"})
            continue
        got = add_comp(root, entry["title"], entry.get("why", ""),
                       entry.get("relation", "other"), status="proposed")
        (skipped if got.pop("existed") else added).append(got)
    return {"proposed": added, "skipped": skipped, "model": meta}


def teardown(root, comp_id: int, *, think: Optional[Think] = None) -> dict:
    comp = get_comp(root, comp_id)
    if comp["status"] in ("proposed", "dropped"):
        raise ValueError(f"{comp['title']!r} is {comp['status']}; confirm it "
                         "before spending a research run on it")
    raw, meta = _ask(think, root, teardown_system(),
                     teardown_prompt(context(root), comp), "teardown")
    return {**record(root, comp_id, raw), "model": meta}


def draft_plan(root, notes: str = "", *, think: Optional[Think] = None) -> dict:
    """Synthesise the plan and save it as a DRAFT. Writes nothing else."""
    if not list_comps(root, status="researched"):
        raise ValueError("nothing researched yet - tear down at least one "
                         "comparable before drafting a plan from the research")
    prompt = plan_prompt(context(root), digest(root), _clean(notes))
    raw, meta = _ask(think, root, PLAN_SYSTEM, prompt, "plan")
    clean = validate_plan(raw)
    saved = save_plan(root, clean)
    return {"plan": saved, "model": meta, "wrote_nothing_else": True}


# ── the plan ───────────────────────────────────────────────────────────────

def validate_plan(raw: Any) -> dict:
    """A plan, or a ValueError naming what is wrong. Strict, not repaired:
    this is what a human approves, and a silently-fixed plan is not the one
    they read."""
    if not isinstance(raw, dict):
        raise ValueError("a plan is a JSON object")
    pillars = []
    for p in raw.get("pillars") or []:
        if isinstance(p, dict) and _clean(p.get("title")):
            pillars.append({"title": _clean(p["title"], 120),
                            "body": _clean(p.get("body"))})
    if not 1 <= len(pillars) <= 7:
        raise ValueError(f"a plan needs 1 to 7 pillars; got {len(pillars)}")
    core_loop = _clean(raw.get("core_loop"))
    if len(core_loop) < 20:
        raise ValueError("a plan needs a core_loop of at least a sentence")
    stances = []
    for s in raw.get("stances") or []:
        if not isinstance(s, dict):
            continue
        stance = _clean(s.get("stance"), 10).lower()
        if stance not in STANCES:
            raise ValueError(f"stance {stance!r} is not one of {STANCES}")
        stances.append({"system": _system_name(s.get("system")),
                        "stance": stance, "note": _clean(s.get("note"))})
    not_building = []
    for n in raw.get("not_building") or []:
        if not isinstance(n, dict):
            continue
        text, reason = _clean(n.get("text")), _clean(n.get("reason"))
        if not text or not reason:
            raise ValueError("every not_building entry needs text and reason")
        not_building.append({"text": text, "reason": reason})
    decisions = []
    for d in raw.get("decisions") or []:
        if not isinstance(d, dict):
            continue
        row = {k: _clean(d.get(k)) for k in ("title", "acceptance",
                                             "leaves_dark")}
        missing = [k for k, v in row.items() if not v]
        if missing:
            raise ValueError(f"decision {row['title'] or '?'!r} is missing "
                             f"{', '.join(missing)}")
        decisions.append(row)
    thesis = raw.get("thesis") if isinstance(raw.get("thesis"), dict) else None
    return {"summary": _clean(raw.get("summary")),
            "pillars": pillars, "core_loop": core_loop,
            "setting": _clean(raw.get("setting")),
            "art_direction": _clean(raw.get("art_direction")),
            "stances": stances, "not_building": not_building,
            "decisions": decisions, "thesis": thesis}


def plan(root) -> dict:
    return _workspace.get(root, PLAN_SEAT, PLAN_KEY)


def save_plan(root, clean: dict) -> dict:
    """Store a validated plan as the current draft. A new draft clears the
    adopted mark: it is a different plan from the one that was adopted."""
    doc = {**clean, "adopted_at": None, "adopted_by": None,
           "drafted_by": _activity.current_actor()}
    _workspace.set(root, PLAN_SEAT, PLAN_KEY, doc, if_version="")
    return plan(root)


def _upsert(root, kind: str, title: str, body: str) -> tuple[int, bool]:
    for section in _bible.list_sections(root, kind=kind):
        if section.get("title") == title:
            return int(_bible.update(root, int(section["id"]), body=body)["id"]), False
    return int(_bible.add(root, kind, title, body=body)["id"]), True


def adopt(root, raw: Optional[dict] = None, *, by: str = "human",
          again: bool = False) -> dict:
    """Write an approved plan into the design database. HUMAN-ONLY at the tool.

    ``raw`` is the plan as the human edited it; None adopts the saved draft.
    Bible sections upsert by title, so adopting a revised plan updates the
    pillars rather than doubling them; not-building entries and decisions do
    not upsert, so a second adopt of the same plan is refused unless
    ``again``.
    """
    current = plan(root)
    if raw is None:
        if not current.get("pillars"):
            raise ValueError("there is no drafted plan to adopt")
        clean = validate_plan(current)
    else:
        clean = validate_plan(raw)
    if current.get("adopted_at") and not again and (
            raw is None or clean == validate_plan(current)):
        raise AlreadyAdopted(
            f"this plan was adopted {current['adopted_at']} by "
            f"{current.get('adopted_by') or '?'}; pass again=True to write its "
            "not-building entries and decisions a second time")

    out: dict = {"bible": [], "not_building": [], "decisions": [],
                 "thesis": None, "thesis_skipped": None}
    for p in clean["pillars"]:
        sid, new = _upsert(root, "pillar", p["title"], p["body"])
        out["bible"].append({"id": sid, "kind": "pillar", "new": new})
    for kind, title, body in (
            ("loop", "Core loop", clean["core_loop"]),
            ("constraint", "Setting", clean["setting"]),
            ("constraint", "Art direction", clean["art_direction"])):
        if body:
            sid, new = _upsert(root, kind, title, body)
            out["bible"].append({"id": sid, "kind": kind, "title": title,
                                 "new": new})
    sid, new = _upsert(root, "reference", RESEARCH_TITLE, _reference_body(root, clean))
    out["bible"].append({"id": sid, "kind": "reference", "title": RESEARCH_TITLE,
                         "new": new})
    for n in clean["not_building"]:
        row = _decisions.refuse(root, n["text"], n["reason"], tag="research",
                                actor=by)
        out["not_building"].append(row.get("id"))
    for d in clean["decisions"]:
        row = _decisions.add(root, d["title"], d["acceptance"], d["leaves_dark"],
                             state="open", actor=by)
        out["decisions"].append(row.get("id"))
    if clean.get("thesis"):
        try:
            out["thesis"] = _greenlight.set_thesis(root, clean["thesis"], by=by)
        except ValueError as exc:
            # A plan whose thesis the greenlight gate rejects is still a good
            # plan; the director settles the thesis at kickoff instead.
            out["thesis_skipped"] = str(exc)

    doc = {**clean, "adopted_at": _now(), "adopted_by": by,
           "drafted_by": current.get("drafted_by")}
    _workspace.set(root, PLAN_SEAT, PLAN_KEY, doc, if_version="")
    _activity.log(root, "research",
                  f"research plan adopted: {len(clean['pillars'])} pillar(s), "
                  f"{len(out['not_building'])} not-building, "
                  f"{len(out['decisions'])} open decision(s)",
                  seat=PLAN_SEAT, actor=by)
    try:
        _handoff.note(root, "next",
                      "Pre-production research adopted into the bible "
                      f"(section #{sid}); build the thesis and board on it.",
                      actor=by)
    except Exception:
        pass
    return out


def _reference_body(root, clean: dict) -> str:
    lines = ["Written by research_plan_adopt from the pre-production research. "
             "research_findings has every verdict with its sources; "
             "research_grid lines them up."]
    if clean.get("summary"):
        lines += ["", clean["summary"]]
    comps = list_comps(root, status="researched")
    if comps:
        lines += ["", "Comparables:"]
        lines += [f"- {c['title']} ({c['relation']}): {c['summary'] or c['why']}"
                  for c in comps]
    if clean["stances"]:
        lines += ["", "Our stance per system:"]
        lines += [f"- {s['system']}: {s['stance']} - {s['note']}"
                  for s in clean["stances"]]
    return "\n".join(lines)


def status(root) -> dict:
    comps = list_comps(root)
    counts = {s: sum(1 for c in comps if c["status"] == s) for s in STATUSES}
    doc = plan(root)
    return {"comps": comps, "counts": counts,
            "plan": ("adopted" if doc.get("adopted_at") else
                     "drafted" if doc.get("pillars") else "none"),
            "next": _next_step(counts, doc)}


def _next_step(counts: dict, doc: dict) -> str:
    if not any(counts.values()):
        return "research_suggest to propose comparable games"
    if counts["proposed"] and not (counts["confirmed"] or counts["researched"]):
        return "confirm or drop the proposed comparables (research_comp_set)"
    if counts["confirmed"]:
        return "research_teardown each confirmed comparable"
    if not doc.get("pillars"):
        return "research_plan_draft to turn the findings into a plan"
    if not doc.get("adopted_at"):
        return "the human reviews the draft and adopts it (research_plan_adopt)"
    return "adopted - the director builds the thesis and board on it"
