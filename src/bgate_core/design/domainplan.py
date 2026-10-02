"""Domain plans - what each discipline must deliver, and how done is judged.

The game plan (gameplan.py) answers "what does the game consist of" as one
flat list. It never answered, per discipline, the question execution depends
on: WHAT MUST THE FINISHED THING BE. An audio seat handed "add sfx for the
dash" built a sound; nothing said the dash is the core decision, that every
verb must answer within a tenth of a second, or that the shop is deliberately
silent. Audio, UI, tech and level had no plan artifact at all; the plans that
did exist (quests, the encounter roster, the storyboard) were never in any
seat's brief.

A domain plan is the missing layer, one per discipline:

  goal            the end state, as a sentence a reviewer can hold the build to
  done_when       1-8 binary checks; the domain is done when all of them pass
  leaves_dark     what this domain deliberately does NOT do (an unsaid no gets
                  built anyway)
  open_questions  what is still undecided; a planner states them rather than
                  guessing, and the director answers them
  entries         the typed deliverables. Each domain has its own entry shape
                  (a mechanic has a verb, feedback and tunables; a cue has a
                  type and a trigger) because a planner who must fill in
                  "feedback" for every verb has to decide it.

Entries compile into plan_row (tagged with the domain), so coverage, the
slice check and the morning digest all read one ledger. ``review`` reads the
plans AGAINST EACH OTHER - a mechanic no level teaches, a mechanic with no
sound, a clip for a character nobody draws - which is the understanding no
single seat has. ``brief_block`` puts the seat's own plan, with live state per
entry, into every seat brief.

Writes are seat-scoped: a seat writes its own domain(s), the director and the
human write any. Filing entries onto the board (``promote``) is the
director's call, the same rule as the rest of the board's scope.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from ..board import activity
from ..store import db

MIN_GOAL = 40
MIN_CHECK = 15
MIN_ACCEPTANCE = 15
MAX_CHECKS = 8
MAX_ENTRIES = 80
MAX_TEXT = 600
MAX_LIST = 12

# field spec: name -> (kind, help). kind is "text", "list", or a tuple of
# allowed values. Every listed field is REQUIRED: the point of a typed entry
# is that the planner has to decide each of these, and an optional field is
# one that never gets decided. Lists may be empty only where noted by "list?".
DOMAINS: dict[str, dict] = {
    "gameplay": {
        "seat": "gameplay", "kind": "system", "entry": "mechanic",
        "fields": {
            "verb": ("text", "what the player DOES, as an action ('dash through an enemy')"),
            "feedback": ("text", "how the game answers within a tenth of a second: picture, sound, number"),
            "tunables": ("list", "the numbers that set its feel, with starting values ('dash_distance=3.5m')"),
        },
        "asks": [
            "What is the one decision the player repeats (the thesis), and which mechanic carries it?",
            "For each verb: what does the player see and hear the instant it lands?",
            "Which numbers set the feel, and what are their starting values?",
            "What does the player do in minute one versus hour one - what deepens?",
            "Which mechanics are cut or deferred? Put them in leaves_dark.",
        ],
    },
    "level": {
        "seat": "level", "kind": "level", "entry": "space",
        "fields": {
            "purpose": ("text", "the beat this space delivers: teach, test, rest, reward, reveal"),
            "introduces": ("list?", "names of gameplay mechanics or threats it teaches or tests"),
            "connects_to": ("list?", "names of the spaces it leads to"),
        },
        "asks": [
            "What does each space teach, test or reward, and in what order?",
            "Where is the player safe, and where are they under pressure?",
            "How long is the critical path, and where does it branch?",
            "Which space is the vertical slice played in?",
        ],
    },
    "art": {
        "seat": "art", "kind": "asset", "entry": "asset",
        "fields": {
            "use": ("text", "where it appears in the game and what it must read as"),
            "view": ("text", "the camera it is seen through and its on-screen size"),
        },
        "asks": [
            "At the game's camera and resolution, what must read at a glance - player, threat, pickup, hazard?",
            "What is the style anchor (pinned ref) every asset is judged against?",
            "Which assets are in the slice, and which can stay graybox until production?",
            "What is the palette and value structure that keeps the player readable on every background?",
        ],
    },
    "animation": {
        "seat": "art", "kind": "asset", "entry": "clip",
        "fields": {
            "character": ("text", "who moves - an art entry or canon name"),
            "plays_on": ("text", "the gameplay event that triggers it ('dash pressed', 'hit taken')"),
        },
        "asks": [
            "Which gameplay events need a clip, and which can be a tween or a flash?",
            "For the core verb: anticipation, contact and recovery timings that make it feel right?",
            "Which clips loop, and which must be interruptible by input?",
        ],
    },
    "audio": {
        "seat": "audio", "kind": "sound", "entry": "cue",
        "fields": {
            "type": (("music", "sfx", "ambience", "voice", "ui"), "music | sfx | ambience | voice | ui"),
            "trigger": ("text", "the game event or state that plays it - name the mechanic or space"),
        },
        "asks": [
            "What does the player hear at the moment of the core decision, and on success versus failure?",
            "What plays in each space or state, and how does it transition?",
            "Which mechanics are silent, and is that deliberate?",
            "What is the loudness target, and what ducks under what?",
        ],
    },
    "narrative": {
        "seat": "narrative", "kind": "dialogue", "entry": "beat",
        "fields": {
            "reveals": ("text", "what the player learns or feels at this beat"),
            "delivered_by": (("play", "dialogue", "quest", "cinematic", "environment", "ui"),
                             "play | dialogue | quest | cinematic | environment | ui"),
        },
        "asks": [
            "What does the player know at the start, what do they learn, and in what order?",
            "Which beats are carried by play rather than text?",
            "What must never be said outright?",
        ],
    },
    "cinematic": {
        "seat": "cinematic", "kind": "scene", "entry": "sequence",
        "fields": {
            "trigger": ("text", "the moment control is taken, and what returns it"),
            "purpose": ("text", "what this sequence does that play cannot"),
        },
        "asks": [
            "Which moments justify taking control from the player, and for how long?",
            "Can every sequence be skipped, and what does the player lose by skipping?",
        ],
    },
    "ui": {
        "seat": "gameplay", "kind": "scene", "entry": "screen",
        "fields": {
            "purpose": ("text", "what the player needs from this screen"),
            "shows": ("list", "what is on it - every number, label and prompt"),
            "leads_to": ("list?", "screens reachable from it"),
        },
        "asks": [
            "What must the player know at a glance during play, and what can wait for a menu?",
            "What is the flow from boot to play to failure to retry, and how many presses is it?",
            "How does every screen work on the target input device?",
        ],
    },
    "tech": {
        "seat": "tech", "kind": "system", "entry": "system",
        "fields": {
            "owns": ("text", "the state or data this system is the ONE owner of"),
            "interfaces": ("list", "how other systems reach it: signals, autoload calls, resources"),
        },
        "asks": [
            "Which system owns each piece of game state - exactly one owner each?",
            "What crosses a scene change, and what is saved?",
            "What are the budgets: frame time, memory, load time, export size?",
            "Which risks are technical, and how is each retired?",
        ],
    },
    "qa": {
        "seat": "qa", "kind": None, "entry": "scenario",
        "fields": {
            "steps": ("list", "what the tester does, in order"),
            "covers": ("list", "the domains (or plan rows) whose done_when this scenario proves"),
        },
        "asks": [
            "What is the golden path from boot to the end of the slice, step by step?",
            "Which scenario proves each domain's done_when?",
            "What is the failure/retry path, and is it tested?",
        ],
    },
}

GENERAL_ASKS = [
    "goal: what must this domain's part of the FINISHED game be? A reviewer "
    "holds the build to this sentence.",
    "done_when: the binary checks that settle it - each one a test, not a "
    "restatement of the goal.",
    "leaves_dark: what this domain deliberately does not do.",
    "open_questions: what you could not decide. State them; do not guess.",
]


def template(domain: str) -> dict:
    """What a plan for this domain must decide, and the entry shape."""
    spec = _spec(domain)
    return {
        "domain": domain,
        "seat": spec["seat"],
        "entry": spec["entry"],
        "entry_fields": {k: {"type": (list(v[0]) if isinstance(v[0], tuple)
                                      else v[0]), "means": v[1]}
                         for k, v in spec["fields"].items()},
        "every_entry_also": {
            "name": "unique snake_case; how the row is matched forever",
            "acceptance": "the test that settles this ONE entry",
            "slice": "true if it is part of the vertical slice",
            "depends_on": "names of plan rows (any domain) it needs first",
            "checkpoint": "true to make the item that builds this a HUMAN "
                          "CHECKPOINT: it parks for the human's sign-off and "
                          "nothing behind it runs until they approve",
        },
        "answer_these": GENERAL_ASKS + spec["asks"],
        "compiles_to": (f"plan_row kind={spec['kind']!r}, seat={spec['seat']!r}"
                        if spec["kind"] else
                        "nothing on the board - QA scenarios are the tests "
                        "other domains are held to"),
    }


def _spec(domain: str) -> dict:
    key = str(domain or "").strip().lower()
    if key not in DOMAINS:
        raise ValueError(f"unknown domain {domain!r}; domains are {tuple(DOMAINS)}")
    return DOMAINS[key]


def may_write(seat: str, domain: str) -> bool:
    """A seat writes its own domains; the director (or no seat: the human)
    writes any."""
    seat = (seat or "").strip().lower()
    return not seat or seat == "director" or _spec(domain)["seat"] == seat


def _text_list(value: Any, what: str, min_len: int = 1,
               limit: int = MAX_LIST) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list of strings")
    out = [str(v).strip()[:MAX_TEXT] for v in value if str(v).strip()]
    short = [v for v in out if len(v) < min_len]
    if short:
        raise ValueError(f"{what}: {short[0]!r} is too short to check against "
                         f"(at least {min_len} characters)")
    if len(out) > limit:
        raise ValueError(f"{what} has {len(out)} entries; the limit is {limit}")
    return out


def validate(domain: str, plan: Any) -> dict:
    """Strict. Raises ValueError naming the field and, for entries, the row."""
    spec = _spec(domain)
    if not isinstance(plan, dict):
        raise ValueError("a domain plan is an object with goal, done_when, entries")
    goal = str(plan.get("goal") or "").strip()[:MAX_TEXT * 2]
    if len(goal) < MIN_GOAL:
        raise ValueError(f"goal must state the end state in at least {MIN_GOAL} "
                         "characters - what this domain's part of the finished "
                         "game IS, not a task")
    done_when = _text_list(plan.get("done_when"), "done_when", MIN_CHECK, MAX_CHECKS)
    if not done_when:
        raise ValueError("done_when needs at least one binary check")
    entries_in = plan.get("entries") or []
    if not isinstance(entries_in, list):
        raise ValueError("entries must be a list")
    if len(entries_in) > MAX_ENTRIES:
        raise ValueError(f"{len(entries_in)} entries; the limit is {MAX_ENTRIES}")
    names: set[str] = set()
    entries: list[dict] = []
    for i, raw in enumerate(entries_in, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"{spec['entry']} {i} is not an object")
        name = str(raw.get("name") or "").strip()[:120]
        if not name:
            raise ValueError(f"{spec['entry']} {i} has no name")
        if name in names:
            raise ValueError(f"{spec['entry']} {i} duplicates the name {name!r}")
        names.add(name)
        label = f"{spec['entry']} {i} ({name})"
        acceptance = str(raw.get("acceptance") or "").strip()[:2000]
        if len(acceptance) < MIN_ACCEPTANCE:
            raise ValueError(f"{label}: acceptance must be a test of at least "
                             f"{MIN_ACCEPTANCE} characters")
        entry: dict = {"name": name, "acceptance": acceptance,
                       "slice": bool(raw.get("slice")),
                       "checkpoint": bool(raw.get("checkpoint")),
                       "depends_on": _text_list(raw.get("depends_on"),
                                                f"{label} depends_on", 1, 20)}
        for field, (kind, means) in spec["fields"].items():
            value = raw.get(field)
            if isinstance(kind, tuple):
                value = str(value or "").strip().lower()
                if value not in kind:
                    raise ValueError(f"{label}: {field} must be one of {kind}")
                entry[field] = value
            elif kind.startswith("list"):
                items = _text_list(value, f"{label} {field}")
                if kind == "list" and not items:
                    raise ValueError(f"{label}: {field} is required - {means}")
                entry[field] = items
            else:
                value = str(value or "").strip()[:MAX_TEXT]
                if not value:
                    raise ValueError(f"{label}: {field} is required - {means}")
                entry[field] = value
        notes = str(raw.get("notes") or "").strip()[:MAX_TEXT]
        if notes:
            entry["notes"] = notes
        entries.append(entry)
    return {
        "goal": goal,
        "done_when": done_when,
        "leaves_dark": _text_list(plan.get("leaves_dark"), "leaves_dark"),
        "open_questions": _text_list(plan.get("open_questions"), "open_questions"),
        "entries": entries,
    }


def _load(row) -> dict:
    out = {"domain": row["domain"], "goal": row["goal"], "by": row["by"],
           "revision": int(row["revision"]), "updated_at": row["updated_at"]}
    for key in ("done_when", "leaves_dark", "open_questions", "entries",
                "replan"):
        try:
            out[key] = json.loads(row[key] or "[]")
        except (ValueError, IndexError, KeyError):
            out[key] = []
    try:
        out["check_results"] = json.loads(row["check_results"] or "{}")
    except (ValueError, IndexError, KeyError):
        out["check_results"] = {}
    return out


def get(root: str | os.PathLike[str], domain: str) -> Optional[dict]:
    _spec(domain)
    row = db.connect(root).execute("SELECT * FROM domain_plan WHERE domain = ?",
                                   (domain,)).fetchone()
    return _load(row) if row else None


def all_plans(root: str | os.PathLike[str]) -> dict[str, dict]:
    try:
        found = db.connect(root).execute("SELECT * FROM domain_plan").fetchall()
    except Exception:                                             # noqa: BLE001
        return {}
    return {r["domain"]: _load(r) for r in found}


def set_plan(root: str | os.PathLike[str], domain: str, plan: Any,
             by: str = "", merge: bool = True) -> dict:
    """Write a domain plan and compile its entries into plan_row.

    merge=True (the default) upserts entries by name and keeps the ones not
    mentioned, so a plan grows a piece at a time; goal/done_when/leaves_dark/
    open_questions are replaced when given. merge=False replaces the entry
    list: rows that left the plan are deleted while still 'spec', and kept
    (and reported) when the board already holds them - the need survived.
    """
    spec = _spec(domain)
    prior = get(root, domain)
    if merge and prior:
        combined = dict(prior)
        for key in ("goal", "done_when", "leaves_dark", "open_questions"):
            if plan.get(key) not in (None, "", []):
                combined[key] = plan[key]
        by_name = {e["name"]: e for e in prior["entries"]}
        for e in plan.get("entries") or []:
            if isinstance(e, dict) and e.get("name"):
                by_name[str(e["name"]).strip()] = e
        combined["entries"] = list(by_name.values())
        plan = combined
    clean = validate(domain, plan)

    conn = db.connect(root)
    existing = {r["name"]: r for r in conn.execute(
        "SELECT name, domain, work_item_id FROM plan_row").fetchall()}
    mine = {e["name"] for e in clean["entries"]}
    for e in clean["entries"]:
        other = existing.get(e["name"])
        if other is not None and other["domain"] not in ("", domain):
            raise ValueError(f"{e['name']!r} is already a {other['domain']} plan "
                             "row - names are unique across the whole game")
        for dep in e["depends_on"]:
            if dep not in mine and dep not in existing:
                raise ValueError(f"{e['name']!r} depends on {dep!r}, which is "
                                 "not a plan row in any domain")

    dropped_live: list[str] = []
    with db.tx(root) as tx:
        if prior:
            tx.execute(
                "UPDATE domain_plan SET goal=?, done_when=?, leaves_dark=?, "
                "open_questions=?, entries=?, by=?, revision=revision+1, "
                "replan=?, updated_at=datetime('now') WHERE domain=?",
                (clean["goal"], json.dumps(clean["done_when"]),
                 json.dumps(clean["leaves_dark"]),
                 json.dumps(clean["open_questions"]),
                 json.dumps(clean["entries"]), by,
                 # A REVISION ANSWERS THE FLAGS. Resolved rather than deleted:
                 # their sources must stay known or the next sweep re-raises
                 # the same failed check against the plan that answered it.
                 json.dumps([{**f, "resolved": True}
                             for f in prior.get("replan") or []]),
                 domain))
        else:
            tx.execute(
                "INSERT INTO domain_plan (domain, goal, done_when, leaves_dark, "
                "open_questions, entries, by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (domain, clean["goal"], json.dumps(clean["done_when"]),
                 json.dumps(clean["leaves_dark"]),
                 json.dumps(clean["open_questions"]),
                 json.dumps(clean["entries"]), by))
        if spec["kind"]:
            for e in clean["entries"]:
                if e["name"] in existing:
                    tx.execute(
                        "UPDATE plan_row SET kind=?, seat=?, acceptance=?, "
                        "slice=?, depends_on_names=?, domain=?, checkpoint=? "
                        "WHERE name=?",
                        (spec["kind"], spec["seat"], e["acceptance"],
                         1 if e["slice"] else 0, json.dumps(e["depends_on"]),
                         domain, 1 if e.get("checkpoint") else 0, e["name"]))
                else:
                    tx.execute(
                        "INSERT INTO plan_row (kind, name, seat, acceptance, "
                        "slice, depends_on_names, domain, checkpoint) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (spec["kind"], e["name"], spec["seat"], e["acceptance"],
                         1 if e["slice"] else 0, json.dumps(e["depends_on"]),
                         domain, 1 if e.get("checkpoint") else 0))
            for name, row in existing.items():
                if row["domain"] != domain or name in mine:
                    continue
                if row["work_item_id"]:
                    dropped_live.append(name)
                else:
                    tx.execute("DELETE FROM plan_row WHERE name = ?", (name,))
    activity.log(root, "domain-plan",
                 f"{domain} plan {'revised' if prior else 'written'}: "
                 f"{len(clean['entries'])} {spec['entry']}(s), "
                 f"{len(clean['open_questions'])} open question(s)",
                 seat=spec["seat"], ref=domain)
    out = {"ok": True, "domain": domain, "entries": len(clean["entries"]),
           "revision": (prior["revision"] + 1) if prior else 1,
           "findings": [f for f in review(root) if f["domain"] == domain]}
    if dropped_live:
        out["dropped_but_on_board"] = dropped_live
    return out


def _states(root) -> dict[str, dict]:
    from . import gameplan as _gameplan
    try:
        return {r["name"]: r for r in _gameplan.row_states(root)}
    except Exception:                                             # noqa: BLE001
        return {}


IN_GAME = ("wired", "verified")


def status(root: str | os.PathLike[str], domain: str = "") -> dict:
    """Per-domain coverage against each plan's own end state."""
    plans = all_plans(root)
    states = _states(root)
    if domain:
        _spec(domain)
    wanted = [domain] if domain else list(DOMAINS)
    out: dict[str, dict] = {}
    for d in wanted:
        plan = plans.get(d)
        if plan is None:
            out[d] = {"planned": False, "seat": DOMAINS[d]["seat"]}
            continue
        rows_ = []
        for e in plan["entries"]:
            st = states.get(e["name"], {}).get("state",
                                              "test" if d == "qa" else "spec")
            rows_.append({"name": e["name"], "state": st, "slice": e["slice"],
                          "item": states.get(e["name"], {}).get("work_item_id")})
        built = sum(1 for r in rows_ if r["state"] in IN_GAME)
        checks = check_state(root, d, plan)
        out[d] = {
            # DONE IS MEASURED: every deliverable in the game (QA's scenarios
            # are tests, not deliverables) and every done_when check passing
            # on evidence recorded after the domain's work last moved.
            "complete": (all(c["state"] == "pass" for c in checks)
                         and (d == "qa" or all(r["state"] in IN_GAME + ("cut",)
                                               for r in rows_))),
            "checks": checks,
            "replan": open_flags(plan),
            "planned": True, "seat": DOMAINS[d]["seat"],
            "goal": plan["goal"], "done_when": plan["done_when"],
            "leaves_dark": plan["leaves_dark"],
            "open_questions": plan["open_questions"],
            "revision": plan["revision"],
            "entries": len(rows_), "in_game": built,
            "slice": sum(1 for r in rows_ if r["slice"]),
            "rows": rows_ if domain else
                    [r for r in rows_
                     if r["state"] not in IN_GAME + ("cut",)][:15],
        }
    return {"domains": out, "findings": review(root, plans)}


def _norm(text: str) -> str:
    return str(text or "").lower().replace("_", " ").replace("-", " ")


def _mentions(name: str, texts: list[str]) -> bool:
    needle = _norm(name).strip()
    return bool(needle) and any(needle in _norm(t) for t in texts)


def review(root: str | os.PathLike[str],
           plans: Optional[dict] = None) -> list[dict]:
    """Read the plans AGAINST EACH OTHER. Findings, never a refusal.

    Each check is a question one discipline cannot answer alone: is every
    mechanic taught somewhere, does every mechanic make a sound, does every
    clip belong to a character somebody draws, does QA test every domain's
    done_when. A finding names the domain that has to act.
    """
    plans = all_plans(root) if plans is None else plans
    out: list[dict] = []

    def add(domain: str, severity: str, text: str) -> None:
        out.append({"domain": domain, "severity": severity, "text": text})

    try:
        from ..board import seats as _seats
        active = set(_seats.roles_for(root))
    except Exception:                                             # noqa: BLE001
        active = set(s["seat"] for s in DOMAINS.values())
    for d, spec in DOMAINS.items():
        if d not in plans and spec["seat"] in active:
            add(d, "gap", f"no {d} plan - {spec['seat']} is building without a "
                          "stated end state (domain_plan_template)")
    for d, plan in plans.items():
        if plan["open_questions"]:
            add(d, "decide", f"{len(plan['open_questions'])} open question(s): "
                             + " | ".join(q[:120] for q in plan["open_questions"][:3]))
        for flag in open_flags(plan)[-3:]:
            add(d, "replan", flag["reason"][:300])
        for c in check_state(root, d, plan):
            if c["state"] in ("fail", "stale"):
                add(d, "check", f"done_when {c['state'].upper()}: {c['check'][:160]}")

    mechanics = (plans.get("gameplay") or {}).get("entries") or []
    spaces = (plans.get("level") or {}).get("entries") or []
    cues = (plans.get("audio") or {}).get("entries") or []
    clips = (plans.get("animation") or {}).get("entries") or []
    assets = (plans.get("art") or {}).get("entries") or []
    scenarios = (plans.get("qa") or {}).get("entries") or []

    if mechanics and spaces:
        taught = [i for s in spaces for i in s.get("introduces") or []]
        for m in mechanics:
            if not _mentions(m["name"], taught):
                add("level", "gap", f"mechanic {m['name']!r} is never introduced "
                                    "by any space - where does the player learn it?")
        names = [m["name"] for m in mechanics]
        try:
            from ..level import encounter as _enc
            names += [r["name"] for r in _enc.roster(root)]
        except Exception:                                         # noqa: BLE001
            pass
        for s in spaces:
            for i in s.get("introduces") or []:
                if not any(_mentions(n, [i]) or _mentions(i, [n]) for n in names):
                    add("gameplay", "warn",
                        f"space {s['name']!r} introduces {i!r}, which no gameplay "
                        "mechanic or roster enemy defines")
    if mechanics and cues:
        triggers = [c["trigger"] + " " + c["name"] for c in cues]
        for m in mechanics:
            if not (_mentions(m["name"], triggers) or _mentions(m["verb"], triggers)):
                add("audio", "gap", f"mechanic {m['name']!r} has no cue - it is "
                                    "silent unless leaves_dark says so")
    if clips:
        known = [a["name"] for a in assets]
        try:
            from . import lore as _lore
            known += [e["name"] for e in _lore.list_entities(root, status="canon")]
        except Exception:                                         # noqa: BLE001
            pass
        for c in clips:
            if known and not any(_mentions(k, [c["character"]]) or
                                 _mentions(c["character"], [k]) for k in known):
                add("animation", "warn",
                    f"clip {c['name']!r} animates {c['character']!r}, which "
                    "neither the art plan nor canon names")
    if scenarios:
        covered = [_norm(c) for s in scenarios for c in s.get("covers") or []]
        for d in plans:
            if d != "qa" and not any(d in c or any(_norm(e["name"]) in c
                                                   for e in plans[d]["entries"])
                                     for c in covered):
                add("qa", "gap", f"no scenario covers the {d} plan's done_when")
    sliced = {d for d, p in plans.items() if any(e["slice"] for e in p["entries"])}
    if sliced:
        for d, p in plans.items():
            if d not in sliced and d != "qa" and p["entries"]:
                add(d, "warn", f"the {d} plan has no slice entry - the vertical "
                               "slice has nothing from this discipline")
    return out


def brief_block(root: str | os.PathLike[str], role: str) -> dict:
    """The seat's own plan(s), live, for seat_brief. Bounded; never raises."""
    try:
        plans = all_plans(root)
        states = _states(root)
        findings = review(root, plans)
        if role == "director":
            return {
                "domains": [{
                    "domain": d,
                    "planned": d in plans,
                    "goal": (plans[d]["goal"][:160] if d in plans else ""),
                    "in_game": (f"{sum(1 for e in plans[d]['entries'] if states.get(e['name'], {}).get('state') in IN_GAME)}"
                                f"/{len(plans[d]['entries'])}" if d in plans else ""),
                    "open_questions": len(plans[d]["open_questions"]) if d in plans else 0,
                    "replan": len(open_flags(plans[d])) if d in plans else 0,
                } for d in DOMAINS],
                "findings": findings[:10],
                "how": "domain_plan_status for detail; answer open questions "
                       "with domain_plan_set; plan_promote files spec rows",
            }
        mine = [d for d, s in DOMAINS.items() if s["seat"] == role]
        if not mine:
            return {}
        held = role in planning_held(root)
        out: dict = {"plans": [], "findings": [f for f in findings
                                               if f["domain"] in mine][:6]}
        for d in mine:
            plan = plans.get(d)
            if plan is None:
                out["plans"].append({
                    "domain": d,
                    "missing": f"no {d} plan. Before building {d} work, call "
                               f"domain_plan_template('{d}') and write one "
                               "with domain_plan_set - goal, done_when, and "
                               "every deliverable with its acceptance test."})
                continue
            out["plans"].append({
                "domain": d,
                "goal": plan["goal"],
                "done_when": plan["done_when"],
                "leaves_dark": plan["leaves_dark"],
                "open_questions": plan["open_questions"],
                "replan": [f["reason"][:240] for f in open_flags(plan)][-3:],
                "checks": [{"check": c["check"][:160], "state": c["state"]}
                           for c in check_state(root, d, plan)],
                "entries": [{"name": e["name"],
                             "state": states.get(e["name"], {}).get("state", "spec"),
                             "slice": e["slice"],
                             "acceptance": e["acceptance"][:160]}
                            for e in plan["entries"][:20]],
                "more": max(0, len(plan["entries"]) - 20),
            })
        out["rule"] = ("BUILD TOWARD THE GOAL. Your item is one entry of this "
                       "plan; meet its acceptance AND do not break the "
                       "domain's done_when. If the work shows the plan is "
                       "wrong, revise it with domain_plan_set and say so in "
                       "your result.")
        if held:
            out["held"] = ("YOUR SEAT IS HELD until your discipline's plan "
                           "exists: this project plans first. The planning "
                           "item on the board is the one that dispatches.")
        return out
    except Exception:                                             # noqa: BLE001
        return {}


def promote(root: str | os.PathLike[str], names: list[str],
            priority: int = 5) -> dict:
    """File spec rows onto the board, in dependency order, with real links.

    THE NEXT TRANCHE. Ingest files only the slice; every other row waited as
    'spec' with nothing that could ever move it. Each filed item carries the
    entry's acceptance and its domain's goal, so the agent building it knows
    what the piece is FOR. A dependency on a row that is still spec and not in
    this batch is refused - filing it would dispatch before its input exists.
    """
    from ..board import queue as _queue
    from . import gameplan as _gameplan

    wanted = [str(n).strip() for n in names or [] if str(n).strip()]
    if not wanted:
        raise ValueError("name at least one plan row to promote")
    states = _states(root)
    missing = [n for n in wanted if n not in states]
    if missing:
        raise LookupError(f"no plan row named {missing[0]!r}")
    conn = db.connect(root)
    rows_ = {r["name"]: r for r in conn.execute(
        "SELECT * FROM plan_row WHERE name IN (%s)" % ",".join("?" * len(wanted)),
        wanted).fetchall()}
    batch = []
    for n in wanted:
        if states[n]["state"] not in ("spec", "lost"):
            raise ValueError(f"{n!r} is {states[n]['state']} - only spec or "
                             "lost rows are promoted")
        deps = json.loads(rows_[n]["depends_on_names"] or "[]")
        for d in deps:
            if d not in wanted and states.get(d, {}).get("state") in ("spec", "lost"):
                raise ValueError(f"{n!r} needs {d!r}, which is not on the board - "
                                 "promote it in the same call")
        batch.append({"kind": rows_[n]["kind"], "name": n,
                      "seat": rows_[n]["seat"], "depends_on": deps,
                      "acceptance": rows_[n]["acceptance"],
                      "domain": rows_[n]["domain"],
                      "checkpoint": bool(rows_[n]["checkpoint"])})
    ordered = _gameplan._ordered(batch)
    plans = all_plans(root)
    filed = []
    item_for = {n: int(states[n]["work_item_id"]) for n in states
                if states[n].get("work_item_id")
                and states[n]["state"] not in ("spec", "lost")}
    for row in ordered:
        goal = (plans.get(row["domain"]) or {}).get("goal", "")
        brief = (f"[{row['domain'] or 'game'} plan: {row['kind']} '{row['name']}']\n"
                 + (f"DOMAIN GOAL: {goal}\n" if goal else "")
                 + f"ACCEPTANCE: {row['acceptance']}")
        deps = [item_for[d] for d in row["depends_on"] if d in item_for]
        item = _queue.add(root, row["seat"], f"{row['kind']}: {row['name']}",
                          brief=brief, priority=int(priority),
                          source="game-plan", source_ref=row["name"],
                          depends_on=deps[0] if deps else None,
                          acceptance=row["acceptance"][:500],
                          checkpoint=row.get("checkpoint", False),
                          checkpoint_note=(f"the {row['name']} checkpoint: "
                                           f"{row['acceptance'][:200]}"
                                           if row.get("checkpoint") else ""))
        for extra in deps[1:]:
            _queue.add_dependency(root, int(item["id"]), extra)
        item_for[row["name"]] = int(item["id"])
        with db.tx(root) as tx:
            tx.execute("UPDATE plan_row SET work_item_id = ? WHERE name = ?",
                       (int(item["id"]), row["name"]))
        filed.append({"id": int(item["id"]), "name": row["name"],
                      "seat": row["seat"], "depends_on": deps})
    activity.log(root, "domain-plan", f"promoted {len(filed)} plan row(s) to the board",
                 seat="director")
    return {"ok": True, "filed": filed}


def _closure(root: str | os.PathLike[str], names: list[str]) -> list[str]:
    """names plus every spec/lost row they transitively depend on."""
    conn = db.connect(root)
    deps = {r["name"]: json.loads(r["depends_on_names"] or "[]")
            for r in conn.execute("SELECT name, depends_on_names FROM plan_row")}
    states = _states(root)
    out: list[str] = []
    todo = list(names)
    while todo:
        n = todo.pop()
        if n in out or n not in deps:
            continue
        if states.get(n, {}).get("state") not in ("spec", "lost"):
            continue
        out.append(n)
        todo.extend(deps[n])
    return out


def promote_slice(root: str | os.PathLike[str]) -> list[dict]:
    """File every domain plan's spec slice entries, with what they need.

    The deploy path when a brainstorm returned domain plans and no manifest:
    the plans' own slice entries are the slice. A slice entry needing a
    non-slice row pulls that row in - the slice cannot play without it.
    """
    states = _states(root)
    names = [n for n, r in states.items()
             if r.get("slice") and r.get("domain") and r["state"] == "spec"]
    if not names:
        return []
    return promote(root, _closure(root, names))["filed"]


# ---------------------------------------------------------------------------
# Plan first. A seat whose discipline has no plan does not build.
# ---------------------------------------------------------------------------
#: The discipline a seat must have planned before its work dispatches. ui and
#: animation are not required: they ride on seats whose primary plan is, and
#: review() reports them missing.
SEAT_DOMAIN = {"gameplay": "gameplay", "level": "level", "art": "art",
               "audio": "audio",
               "narrative": "narrative", "cinematic": "cinematic",
               "tech": "tech", "qa": "qa"}
PLANNING_SOURCE = "domain-plan"
_LIVE = ("queued", "dispatched", "review", "parked")


def planning_held(root: str | os.PathLike[str]) -> set[str]:
    """Seats whose work waits for their discipline's plan.

    ON ONLY ONCE THE PROJECT PLANS. A project with no domain plan at all is
    a project that never adopted the layer (every board before 2026-09-30),
    and holding all of its seats would stop a running game for a document it
    was never asked for. The first plan written - by kickoff, a deployed
    brainstorm, or any seat - turns it on for every seat.
    """
    plans = all_plans(root)
    if not plans:
        return set()
    try:
        from ..board import seats as _seats
        active = set(_seats.roles_for(root))
    except Exception:                                             # noqa: BLE001
        return set()
    return {seat for seat, d in SEAT_DOMAIN.items()
            if seat in active and d not in plans}


def ensure_planning_items(root: str | os.PathLike[str]) -> list[int]:
    """File ONE planning item per held seat that has work waiting.

    Idempotent: a seat with an open planning item gets no second one, and a
    seat with nothing queued gets none - planning nobody is waiting on is a
    document, not a step.
    """
    held = planning_held(root)
    if not held:
        return []
    from ..board import queue as _queue

    conn = db.connect(root)
    waiting = {r["seat"] for r in conn.execute(
        "SELECT DISTINCT seat FROM work_item WHERE status = 'queued' "
        "AND source != ?", (PLANNING_SOURCE,))}
    marks = ",".join("?" * len(_LIVE))
    open_ = {r["source_ref"] for r in conn.execute(
        f"SELECT source_ref FROM work_item WHERE source = ? "
        f"AND status IN ({marks})", (PLANNING_SOURCE, *_LIVE))}
    plans = all_plans(root)
    filed: list[int] = []
    for seat in sorted(held & waiting):
        domain = SEAT_DOMAIN[seat]
        if domain in open_:
            continue
        titles = [r["title"] for r in conn.execute(
            "SELECT title FROM work_item WHERE seat = ? AND status = 'queued' "
            "AND source != ? ORDER BY priority DESC, id LIMIT 12",
            (seat, PLANNING_SOURCE))]
        item = _queue.add(root, seat, f"PLAN: write the {domain} plan",
                          brief=_planning_brief(domain, titles, plans),
                          priority=9, source=PLANNING_SOURCE, source_ref=domain,
                          size="small",
                          acceptance=(f"domain_plan_status('{domain}') shows "
                                      "planned: true, with done_when checks and "
                                      "an entry for every queued deliverable"))
        filed.append(int(item["id"]))
        activity.log(root, "domain-plan",
                     f"{seat} work is held until the {domain} plan exists - "
                     f"planning item #{item['id']} filed", seat=seat,
                     ref=str(item["id"]))
    return filed


def _planning_brief(domain: str, waiting: list[str], plans: dict) -> str:
    tpl = template(domain)
    lines = [
        f"PLAN THE {domain.upper()} DISCIPLINE BEFORE BUILDING IT.",
        "",
        "Your seat's work is held until this plan exists. Waiting on it: "
        + ("; ".join(waiting) if waiting else "(nothing named yet)") + ".",
        "",
        f"1. Start from domain_plan_draft('{domain}'): the template, entries "
        "drafted from what the project already holds (quests, storyboards, "
        "audio hooks in code, the encounter roster) and the other "
        "disciplines' plans. Read the bible and the thesis "
        "(greenlight_status) too - plan AGAINST them: use other plans' entry "
        "names in depends_on, introduces, trigger, character.",
        "2. Answer these, in the plan, not in prose: "
        + " | ".join(tpl["answer_these"]),
        f"3. domain_plan_set('{domain}', goal=..., done_when=[...], "
        f"entries=[...], leaves_dark=[...], open_questions=[...]). Each "
        f"{tpl['entry']} needs: name, acceptance, slice, depends_on, "
        + ", ".join(tpl["entry_fields"]) + ".",
        "4. Every waiting item above must map to an entry. Anything you cannot "
        "decide goes in open_questions - do not guess, and do not build.",
        "5. Read the findings domain_plan_set returns and fix the ones that are "
        "yours. Do NOT build anything in this item.",
    ]
    others = [f"{d}: {p['goal'][:140]}" for d, p in plans.items() if d != domain]
    if others:
        lines += ["", "OTHER PLANS ALREADY WRITTEN:"] + others
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# done_when, measured. A check is passed by evidence somebody recorded, and
# goes stale when the domain's work moves after it.
# ---------------------------------------------------------------------------
CHECKERS = ("qa", "director")


def record_check(root: str | os.PathLike[str], domain: str, check: Any,
                 verdict: str, evidence: str, by: str = "") -> dict:
    """Record a verdict on one done_when check, with the evidence behind it.

    `check` is the check's index (0-based) or its exact text. Evidence is
    what was run and what it showed - a tool result, a screenshot path, a
    test count. Keyed by the check's TEXT, so a reworded check starts
    unverified: the verdict was about the old sentence.
    """
    plan = get(root, domain)
    if plan is None:
        raise LookupError(f"no {domain} plan - nothing to check")
    checks = plan["done_when"]
    if isinstance(check, int) or str(check).strip().isdigit():
        i = int(check)
        if not 0 <= i < len(checks):
            raise ValueError(f"the {domain} plan has {len(checks)} check(s); "
                             f"{i} is out of range")
        text = checks[i]
    else:
        text = str(check).strip()
        if text not in checks:
            raise ValueError(f"{text!r} is not one of the {domain} plan's checks")
    verdict = str(verdict or "").strip().lower()
    if verdict not in ("pass", "fail"):
        raise ValueError("verdict is pass or fail")
    evidence = str(evidence or "").strip()
    if len(evidence) < 20:
        raise ValueError("evidence must say what was run and what it showed "
                         "(at least 20 characters)")
    at = db.connect(root).execute("SELECT datetime('now')").fetchone()[0]
    results = dict(plan["check_results"])
    results[text] = {"verdict": verdict, "evidence": evidence[:1500],
                     "by": by, "at": at}
    with db.tx(root) as tx:
        tx.execute("UPDATE domain_plan SET check_results = ? WHERE domain = ?",
                   (json.dumps(results), domain))
    activity.log(root, "domain-plan",
                 f"{domain} check {verdict.upper()}: {text[:120]}",
                 seat=_spec(domain)["seat"], ref=domain)
    if verdict == "fail":
        flag_replan(root, domain, f"done_when failed: {text[:200]} - "
                                  f"{evidence[:200]}", source=f"check:{at}:{text[:60]}")
    return {"ok": True, "domain": domain, "check": text, "verdict": verdict,
            "checks": check_state(root, domain)}


def _last_done(root: str | os.PathLike[str], domain: str) -> str:
    row = db.connect(root).execute(
        "SELECT MAX(w.updated_at) FROM plan_row p JOIN work_item w "
        "ON w.id = p.work_item_id WHERE p.domain = ? AND w.status = 'done'",
        (domain,)).fetchone()
    return (row[0] if row else "") or ""


def check_state(root: str | os.PathLike[str], domain: str,
                plan: Optional[dict] = None) -> list[dict]:
    """Every done_when check with its verdict: pass, fail, stale or open.

    STALE is a pass recorded before the domain's latest finished item: the
    build moved after the check, so the check no longer describes it.
    """
    plan = plan or get(root, domain)
    if plan is None:
        return []
    moved = _last_done(root, domain)
    out = []
    for text in plan["done_when"]:
        got = plan["check_results"].get(text)
        if not got:
            state = "open"
        elif got["verdict"] == "pass" and moved and got["at"] < moved:
            state = "stale"
        else:
            state = got["verdict"]
        out.append({"check": text, "state": state,
                    "at": (got or {}).get("at"),
                    "evidence": ((got or {}).get("evidence") or "")[:200]})
    return out


# ---------------------------------------------------------------------------
# Replan. A plan that the build proved wrong says so where its seat reads it.
# ---------------------------------------------------------------------------
MAX_FLAGS = 40


def flag_replan(root: str | os.PathLike[str], domain: str, reason: str,
                source: str) -> bool:
    """Mark a plan as needing revision. False when `source` was seen before."""
    plan = get(root, domain)
    if plan is None:
        return False
    flags = list(plan["replan"])
    if any(f.get("source") == source for f in flags):
        return False
    at = db.connect(root).execute("SELECT datetime('now')").fetchone()[0]
    flags.append({"reason": str(reason)[:500], "source": source, "at": at,
                  "resolved": False})
    with db.tx(root) as tx:
        tx.execute("UPDATE domain_plan SET replan = ? WHERE domain = ?",
                   (json.dumps(flags[-MAX_FLAGS:]), domain))
    activity.log(root, "domain-plan", f"{domain} plan needs revision: "
                                      f"{str(reason)[:160]}",
                 seat=_spec(domain)["seat"], ref=domain)
    return True


def open_flags(plan: dict) -> list[dict]:
    return [f for f in plan.get("replan") or [] if not f.get("resolved")]


def _domain_of_item(root, item_id: int) -> str:
    conn = db.connect(root)
    row = conn.execute("SELECT domain FROM plan_row WHERE work_item_id = ?",
                       (int(item_id),)).fetchone()
    if row and row["domain"]:
        return row["domain"]
    seat = conn.execute("SELECT seat FROM work_item WHERE id = ?",
                        (int(item_id),)).fetchone()
    return SEAT_DOMAIN.get(seat["seat"], "") if seat else ""


def replan_sweep(root: str | os.PathLike[str]) -> list[dict]:
    """Turn the board's evidence that a plan is wrong into replan flags.

    Three signals, each flagged once by source:
      * a SLICE CHECK that failed - every domain with a slice entry;
      * a REFUTED PREMISE - the domain of the item whose brief was wrong;
      * a QA gate that FAILED an item building a plan row - that row's domain.
    """
    plans = all_plans(root)
    if not plans:
        return []
    from ..board import queue as _queue
    from . import gameplan as _gameplan

    conn = db.connect(root)
    raised: list[dict] = []

    def raise_(domain: str, reason: str, source: str) -> None:
        if domain in plans and flag_replan(root, domain, reason, source):
            raised.append({"domain": domain, "source": source})

    for row in conn.execute(
            "SELECT id, result FROM work_item WHERE source = ? AND status = 'done' "
            "AND result LIKE '%VERDICT: FAIL%' ORDER BY id DESC LIMIT 5",
            (_gameplan.SLICE_CHECK_SOURCE,)):
        for d, p in plans.items():
            if any(e.get("slice") for e in p["entries"]):
                raise_(d, f"slice check #{row['id']} FAILED: "
                          f"{(row['result'] or '')[:240]}", f"slice-check:{row['id']}")
    for ref in _queue.refutations(root, limit=20):
        item = str(ref.get("item") or "")
        if item.isdigit():
            raise_(_domain_of_item(root, int(item)),
                   f"premise refuted on #{item}: {str(ref.get('claim') or '')[:160]}"
                   f" - measured {str(ref.get('measured') or '')[:160]}",
                   f"refuted:{item}")
    for row in conn.execute(
            "SELECT id, source_ref, result FROM work_item WHERE source = 'qa-gate' "
            "AND status = 'done' AND result LIKE '%VERDICT: FAIL%' "
            "ORDER BY id DESC LIMIT 20"):
        ref = str(row["source_ref"] or "")
        if not ref.isdigit():
            continue
        planned = conn.execute("SELECT domain, name FROM plan_row "
                               "WHERE work_item_id = ?", (int(ref),)).fetchone()
        if planned and planned["domain"]:
            raise_(planned["domain"],
                   f"QA failed {planned['name']!r} (#{ref}): "
                   f"{(row['result'] or '')[:200]}", f"qa-fail:{row['id']}")
    return raised


def sweep(root: str | os.PathLike[str]) -> dict:
    """The follow-up sweep's one entry point. Never raises."""
    out: dict = {}
    for name, fn in (("planning_items", ensure_planning_items),
                     ("replan", replan_sweep)):
        try:
            out[name] = fn(root)
        except Exception as exc:                                  # noqa: BLE001
            out[name] = f"error: {exc}"
    return out


# ---------------------------------------------------------------------------
# Drafts from what the project already has. Quests, storyboards, the audio
# hooks the code calls and the encounter roster were each a plan in their own
# silo; a planner starting from a blank page re-invents them or ignores them.
# ---------------------------------------------------------------------------
def _snake(text: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "_" for c in str(text or ""))
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")[:80] or "unnamed"


def _draft_narrative(root) -> tuple[list[dict], dict]:
    from . import quests as _quests
    entries = []
    for q in _quests.list_quests(root):
        if q.get("state") == "cut":
            continue
        entries.append({
            "name": f"quest_{_snake(q['slug'])}",
            "reveals": (q.get("premise") or q["title"])[:MAX_TEXT],
            "delivered_by": "quest",
            "acceptance": f"quest '{q['slug']}' validates (quest_read) and "
                          "every step's done_when is reachable in game",
            "slice": False,
        })
    return entries, {"quests": len(entries)}


def _draft_cinematic(root) -> tuple[list[dict], dict]:
    from ..cine import storyboard as _sb
    entries = []
    for b in _sb.boards(root):
        entries.append({
            "name": f"seq_{_snake(b['name'])}",
            "trigger": "",
            "purpose": (b.get("logline") or b.get("premise") or "")[:MAX_TEXT],
            "acceptance": f"storyboard '{b['name']}' is promoted, plays in "
                          "game, and can be skipped",
            "slice": False,
        })
    return entries, {"storyboards": len(entries),
                     "decide": "each sequence's trigger: the moment control "
                               "is taken and what returns it"}


def _draft_audio(root) -> tuple[list[dict], dict]:
    from ..audio import audiohooks as _hooks
    got = _hooks.scan(root)
    entries = []
    for ev in got.get("events") or []:
        site = (ev.get("sites") or [{}])[0]
        family = str(ev.get("family") or "")
        kind = ("music" if "music" in family.lower() else
                "ambience" if "amb" in family.lower() else "sfx")
        entries.append({
            "name": f"cue_{_snake(ev.get('event') or ev.get('name'))}",
            "type": kind,
            "trigger": f"{ev.get('event') or ev.get('name')} - called at "
                       f"{site.get('file', '?')}:{site.get('line', '?')}",
            "acceptance": "a file answers this hook and it was heard in game "
                          "(audio_listen_record)",
            "slice": False,
            "notes": f"currently {ev.get('state', 'unknown')}",
        })
    return entries, {"hooks_in_code": len(entries),
                     "unbound": sum(1 for e in got.get("events") or []
                                    if e.get("state") == "unbound")}


def _draft_gameplay(root) -> tuple[list[dict], dict]:
    from ..level import encounter as _enc
    return [], {
        "roster": [{"name": r["name"], "pressure": r.get("pressure", "")}
                   for r in _enc.roster(root)][:20],
        "objectives": [{"name": o["name"], "shape": o.get("shape", "")}
                       for o in _enc.objectives(root)][:20],
        "note": "the roster and objectives are threats and goals, not "
                "mechanics: write the player's verbs that answer them, and "
                "name roster entries in the level plan's introduces",
    }


_DRAFTERS = {"narrative": _draft_narrative, "cinematic": _draft_cinematic,
             "audio": _draft_audio, "gameplay": _draft_gameplay}


def draft(root: str | os.PathLike[str], domain: str) -> dict:
    """A starting point for a plan: the template, the plan so far, and entries
    drafted from what the project already holds. WRITES NOTHING.

    Drafted entries can carry empty fields - those are the decisions the
    silo never made, and validation will refuse them until the planner
    makes them. That is on purpose: a placeholder that passes validation is a
    decision nobody made.
    """
    _spec(domain)
    entries, context = [], {}
    fn = _DRAFTERS.get(domain)
    if fn:
        try:
            entries, context = fn(root)
        except Exception as exc:                                  # noqa: BLE001
            context = {"error": f"could not read the existing {domain} "
                                f"material: {exc}"}
    current = get(root, domain)
    have = {e["name"] for e in (current or {}).get("entries") or []}
    others = {d: {"goal": p["goal"][:200],
                  "entries": [e["name"] for e in p["entries"]][:40]}
              for d, p in all_plans(root).items() if d != domain}
    return {
        "template": template(domain),
        "current": current,
        "drafted_entries": [e for e in entries if e["name"] not in have],
        "context": context,
        "other_plans": others,
        "next": f"edit the drafted entries (fill every empty field), add what "
                f"is missing, then domain_plan_set('{domain}', ...)",
    }
