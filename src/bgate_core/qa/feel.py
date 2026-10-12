"""Is a scene FUN to play, or only clearable? Measured by two bots racing.

WHY THIS EXISTS. HARDCOVER (2026-10-11) passed every gate this harness had:
the slice check, a golden-path bot clearing every boss with 0 deaths, every
windup measured >= 0.6 s, traversal proofs, screenshots, look audits. The human
played it and said "it's a flat just hold click game". Every gate asked "can
this be cleared?" and none asked "do the player's decisions matter?".

THE METHOD. A decision-model bot (Jev, via :mod:`bgate_core.qa.agentplay`) plays
the scene. Then a bot that holds the Jev bot's single most-used option for the
whole run plays it again. If the dumb bot does about as well, the scene's
decisions do not matter, whatever the scene looks like. Alongside: how much
one input dominates, how often anything threatens the player, and whether the
deaths pile up on one spot.

WHAT THE GAME OWES. Its observer (``bgate_observe()``) reports ``progress``
(0..1 through the scene) and ``danger`` (something will hit the player soon) on
every decision. Without them the scene is reported UNMEASURED, which blocks the
same as a fail: a feel check that cannot measure is not a pass.

VERDICTS (one scene can earn several):
- ONE_BUTTON  the most-used option is over 60% of the smart bot's decisions
- FLAT        the hold-one-option bot reaches >= 85% of the smart bot's progress
- NO_PRESSURE the hold-one-option bot is threatened on under 20% of decisions
- TRIVIAL     neither bot dies
- UNFAIR      3+ smart-bot deaths inside the same 5% of the scene
- UNMEASURED  the observer reports no progress or no danger
PASS is none of them. Thresholds are module constants, measured on one game;
change them deliberately, with the run that justifies it.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from . import agentplay

ONE_BUTTON_SHARE = 0.60
FLAT_RATIO = 0.85
PRESSURE_MIN = 0.20
UNFAIR_DEATHS = 3
UNFAIR_BUCKET = 0.05
DEATH_KIND = re.compile(r"death|died|dead|knocked|fired|game_?over|lost|defeat", re.I)
#: A run-ending event whose status says the player lost counts as a death too:
#: measured on vibe-coder, losing was {"kind": "run_end", "data": {"status":
#: "fired"}}, and a name-only match called a fired bot's run TRIVIAL.
LOSS_STATUS = re.compile(r"fired|dead|died|lost|lose|game_?over|failed|defeat", re.I)
FAILS = ("ONE_BUTTON", "FLAT", "NO_PRESSURE", "TRIVIAL", "UNFAIR", "UNMEASURED")


def feel_dir(root: Any) -> Path:
    return Path(root) / ".bgate" / "feel"


#: Every spelling of "the page/scene the game boots into" is ONE scene.
#: Measured (vibe-coder): QA checked '' and then 'index.html', the gate kept
#: two records for one game, and two director items filed the same three
#: fixes twice.
_BOOT_ALIASES = {"", "/", "index.html", "/index.html", "./index.html"}


def _slug(scene: str) -> str:
    raw = str(scene or "").strip().lower()
    if raw in _BOOT_ALIASES:
        return "boot"
    return re.sub(r"[^a-z0-9]+", "_", raw).strip("_") or "boot"


def measure(run_dir: str | Path) -> dict:
    """Feel metrics for one finished agentplay run."""
    run_dir = Path(run_dir)
    rows = agentplay._jsonl(run_dir / "decisions.jsonl")
    events = agentplay._jsonl(run_dir / "telemetry.jsonl")
    choices = Counter(r.get("choice") for r in rows)
    top, top_n = (choices.most_common(1)[0] if choices else ("", 0))
    progress = [float(r["progress"]) for r in rows
                if isinstance(r.get("progress"), (int, float))]
    danger = [bool(r["danger"]) for r in rows if r.get("danger") is not None]
    deaths = []
    for e in events:
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        status = " ".join(str(data.get(k) or "") for k in
                          ("status", "result", "outcome", "reason"))
        ended = re.search(r"end|over|finish", str(e.get("kind") or ""), re.I)
        if not (DEATH_KIND.search(str(e.get("kind") or ""))
                or (ended and LOSS_STATUS.search(status))):
            continue
        ts = e.get("ts")
        at = None
        if isinstance(ts, (int, float)):
            before = [r for r in rows if isinstance(r.get("progress"), (int, float))
                      and isinstance(r.get("ts"), (int, float)) and r["ts"] <= ts]
            if before:
                at = float(before[-1]["progress"])
        deaths.append(at)
    return {
        "decisions": len(rows),
        "top_choice": top,
        "top_share": round(top_n / len(rows), 3) if rows else None,
        "choices": dict(choices.most_common(8)),
        "progress_max": round(max(progress), 3) if progress else None,
        "danger_share": round(sum(danger) / len(danger), 3) if danger else None,
        "deaths": len(deaths),
        "death_progress": [round(d, 3) if d is not None else None for d in deaths],
    }


def verdict(smart: dict, base: dict) -> dict:
    """The verdicts two runs earn, each with the number that earned it."""
    fails: dict[str, str] = {}
    # A progress that never leaves 0 over a real run is a broken measurement,
    # not a stationary player (measured: comment_section read 0.0 for a run that
    # reached x=4856 because its FightSession had no stage_length).
    if smart.get("progress_max") == 0 and (smart.get("decisions") or 0) >= 50:
        smart = {**smart, "progress_max": None}
    if smart.get("progress_max") is None or smart.get("danger_share") is None:
        missing = [k for k in ("progress", "danger")
                   if smart.get(f"{k}_max" if k == "progress" else f"{k}_share") is None]
        fails["UNMEASURED"] = ("the observer reports no " + " or ".join(missing)
                               + " - add them to bgate_observe()")
    share = smart.get("top_share")
    if share is not None and share > ONE_BUTTON_SHARE:
        fails["ONE_BUTTON"] = (f"'{smart['top_choice']}' was {share:.0%} of the "
                               f"smart bot's decisions (limit {ONE_BUTTON_SHARE:.0%})")
    sp, bp = smart.get("progress_max"), base.get("progress_max")
    if sp and bp is not None and bp >= FLAT_RATIO * sp:
        fails["FLAT"] = (f"holding '{smart['top_choice']}' the whole run reached "
                         f"{bp:.0%} of the scene against the smart bot's {sp:.0%}: "
                         "the decisions do not change the outcome")
    # PRESSURE IS READ OFF THE PASSIVE RUN. A good player keeps out of danger,
    # so the smart bot's own share undercounts it (measured, HARDCOVER The Feed:
    # 3% smart against 18.5% for the bot that only held 'shoot'). How often a
    # player who does nothing clever is threatened is the scene's pressure.
    ds = base.get("danger_share")
    if ds is None:
        ds = smart.get("danger_share")
    if ds is not None and ds < PRESSURE_MIN:
        fails["NO_PRESSURE"] = (f"a player who only holds '{smart.get('top_choice')}' "
                                f"is threatened on {ds:.0%} of decisions "
                                f"(minimum {PRESSURE_MIN:.0%})")
    if smart.get("deaths") == 0 and base.get("deaths") == 0:
        fails["TRIVIAL"] = "neither the smart bot nor the hold-one-option bot died"
    buckets = Counter(int(d / UNFAIR_BUCKET) for d in smart.get("death_progress", [])
                      if d is not None)
    bad = [(b, n) for b, n in buckets.items() if n >= UNFAIR_DEATHS]
    if bad:
        b, n = max(bad, key=lambda x: x[1])
        fails["UNFAIR"] = (f"{n} smart-bot deaths between {b * UNFAIR_BUCKET:.0%} "
                           f"and {(b + 1) * UNFAIR_BUCKET:.0%} of the scene")
    return {"verdict": "PASS" if not fails else "FAIL", "fails": fails}


def check(root: Any, *, scene: str, goal: str, seconds: float = 90.0,
          decide_every: int = 6, headless: bool = True,
          max_usd: float = 0.10) -> dict:
    """Race the smart bot against a hold-one-option bot on ``scene``."""
    smart = agentplay.run(root, goal=goal, seconds=seconds, scene=scene,
                          decide_every=decide_every, policy="jev",
                          headless=headless, max_usd=max_usd)
    smart_m = measure(smart["dir"])
    base = agentplay.run(root, goal=goal, seconds=seconds, scene=scene,
                         decide_every=decide_every,
                         policy=f"hold:{smart_m['top_choice'] or ''}",
                         headless=headless)
    base_m = measure(base["dir"])
    out = {"scene": scene, "goal": goal, "seconds": seconds,
           "at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "smart": smart_m, "baseline": base_m,
           "runs": {"smart": smart["dir"], "baseline": base["dir"]},
           "usd": smart.get("usd"), **verdict(smart_m, base_m)}
    d = feel_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{_slug(scene)}.json").write_text(json.dumps(out, indent=2),
                                           encoding="utf-8")
    try:
        out["board"] = escalate(root, out)
    except Exception as exc:                                     # noqa: BLE001
        out["board"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


FEEL_SOURCE = "feel-check"
#: What each verdict asks the director for. DESIGN, not polish: the fix for a
#: scene whose decisions do not matter is never a better texture.
REMEDY = {
    "ONE_BUTTON": "give the dominant input a cost or a reason to stop: heat or "
                  "overheat, ammo with a reload, a second weapon that trades "
                  "range for damage, enemies that must be ducked or aimed at",
    "FLAT": "make the hold-one-input strategy lose: attacks aimed at where the "
            "player stands, enemies that punish standing still, gaps and "
            "set pieces that force movement and timing",
    "NO_PRESSURE": "raise the threats per second a passive player faces: more "
                   "and faster attacks, attacks that track the player, "
                   "enemies arriving from behind and above",
    "TRIVIAL": "raise difficulty until a decent run dies sometimes: tighter "
               "telegraphs, more health on enemies, fewer safe spots",
    "UNFAIR": "fix the one spot where deaths pile up: geometry, an unreadable "
              "attack, or a hazard with no room to avoid it",
    "UNMEASURED": "fix the game's bgate_observe(): it must report progress "
                  "(0..1) and danger (bool) or the check cannot judge",
}


def escalate(root: Any, result: dict) -> dict:
    """Put a failing scene on the director's desk; close it when it passes.

    One open item per scene (source feel-check, source_ref the scene slug). A
    re-run that still fails files nothing new; a pass closes the open item
    with the passing numbers."""
    from ..board import queue as _queue
    from ..store import db
    from ..store.util import rows as _rows

    ref = _slug(result.get("scene") or "")
    open_items = _rows(db.connect(root).execute(
        "SELECT id FROM work_item WHERE source = ? AND source_ref = ? AND "
        "status IN ('queued','dispatched','review','parked')",
        (FEEL_SOURCE, ref)))
    if result.get("verdict") == "PASS":
        for it in open_items:
            _queue.complete(root, int(it["id"]), skip_gate=True, result=(
                f"feel check PASSED on {result.get('scene')} at "
                f"{result.get('at')}: smart bot progress "
                f"{(result.get('smart') or {}).get('progress_max')}, hold bot "
                f"{(result.get('baseline') or {}).get('progress_max')}"))
        return {"closed": [int(it["id"]) for it in open_items]}
    if open_items:
        return {"open": int(open_items[0]["id"])}
    fails = result.get("fails") or {}
    s, b = result.get("smart") or {}, result.get("baseline") or {}
    lines = [f"- {k}: {v}\n  change: {REMEDY.get(k, '')}"
             for k, v in fails.items()]
    brief = (
        f"The feel check FAILED on {result.get('scene') or 'the boot scene'} "
        f"({result.get('at')}). "
        "This is DESIGN work: the scene can be cleared but the player's "
        "decisions do not carry it. Do not answer it with art or polish; "
        "polish and content are held for this project until it passes.\n\n"
        + "\n".join(lines) + "\n\n"
        f"Numbers: smart bot {s.get('decisions')} decisions, choices "
        f"{s.get('choices')}, progress {s.get('progress_max')}, deaths "
        f"{s.get('deaths')} at {s.get('death_progress')}; hold-"
        f"'{s.get('top_choice')}' bot progress {b.get('progress_max')}, "
        f"threatened on {b.get('danger_share')} of decisions, deaths "
        f"{b.get('deaths')}. Runs: {result.get('runs')}. Read a few decision "
        "lines from the smart run's decisions.jsonl to see the scene the way "
        "the bot saw it.\n\n"
        "Do: decide the two or three changes that move these numbers, file "
        "each as kind=feature or fix to the gameplay or level seat with an "
        "acceptance naming the metric it should move, then file a qa item "
        "depending on them that re-runs playtest_feel_check on this scene. A "
        "pass closes this item by itself.")
    item = _queue.add(
        root, "director",
        f"FEEL FAIL: {result.get('scene') or 'the boot scene'} - "
        + ", ".join(fails)[:120],
        brief=brief, priority=10, source=FEEL_SOURCE, source_ref=ref,
        kind="fix", severity="major", size="small",
        acceptance="design changes filed to gameplay/level with a feel "
                   "re-check depending on them")
    return {"filed": int(item["id"])}


def results(root: Any) -> list[dict]:
    """The latest feel result per scene."""
    d = feel_dir(root)
    out = []
    for path in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def applies(root: Any) -> bool:
    """The check runs where a bot can play: a Godot project, or a web game
    (src/bgate/agent.ts, wired into the loop by the web templates)."""
    try:
        from .playtest import _godot_project_dir, _web_project_dir
        return ((_godot_project_dir(root) / "project.godot").is_file()
                or _web_project_dir(root) is not None)
    except Exception:                                            # noqa: BLE001
        return False


def blockers(root: Any) -> list[str]:
    """Why the feel gate holds, as sentences; empty when every checked scene
    passes. Nothing for a project no bot can play."""
    if not applies(root):
        return []
    got = results(root)
    if not got:
        return ["no scene has passed a feel check - playtest_feel_check(scene, "
                "goal) races a decision-model bot against a hold-one-option "
                "bot; a game that only proves it can be cleared has not proved "
                "its decisions matter"]
    out = []
    for r in got:
        if r.get("verdict") != "PASS":
            why = "; ".join(f"{k}: {v}" for k, v in (r.get("fails") or {}).items())
            out.append(f"feel check FAILED on {r.get('scene')} ({r.get('at')}): {why}")
    return out
