"""What a killed run ALREADY DELIVERED — read before anybody pays for a retry.

THE MEASURED WASTE. One item ran three agents and $33 to deliver work that was
about 95% complete after the first. The first run was killed by a ceiling
AFTER the assets were written and before the item was closed; the router saw
`failed`, bought another agent, and that agent — handed a brief describing work
it could not tell had been done — generated the assets again. Then again.

Two independent ceilings made this worse than it had to be. ``max_runtime_s``
and the turn ceiling are separate numbers with no relationship, so an item can
survive one and die on the other, and raising the clock does nothing for a run
that then dies on turns. Raising ceilings is therefore NOT the fix, and this
module is not one: it is the question that has to be asked before a retry is
bought at all.

    WHAT IS ALREADY ON DISK, AND HOW MUCH OF THE DELIVERABLE IS IT?

The harness can answer that without asking the agent, and that distinction is
the whole reliability argument. ``writelog`` records writes the HOOK observed —
not what a run claimed — so a run that died mid-sentence still has an accurate
record of what it produced. Four reaped runs were recoverable on exactly this
evidence. ``provenance``/``artifacts`` add the paid side: an asset that a
provider was billed for and that landed is money that must not be spent twice.

WHAT THIS DOES NOT DO. It does not decide. It produces the evidence a decision
needs — files, artifacts, spend, and a verdict of ``resumable`` /
``regenerate`` / ``unknown`` — and the follow-up router turns that into a
brief. A module that both measured and decided would hide the measurement,
which is how the retry got bought without anybody looking in the first place.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: A run that wrote at least this many project files probably got somewhere. It
#: is a threshold for the WORDING of the recommendation, never for skipping the
#: inspection: the inspection is free and the retry is not.
SUBSTANTIAL_FILES = 1

#: Paid artifacts that landed. Regenerating one of these is paying twice for a
#: file that is already sitting there correct.
PAID_STATUSES = ("candidate", "approved", "integrated")


def owner_for(item_id: int) -> str:
    """The writelog owner key a dispatched run writes under."""
    return f"item-{int(item_id)}"


def inspect(root: str | os.PathLike[str], item_id: int) -> dict:
    """Everything a retry decision needs, measured rather than asked for.

    Returns ``{item, verdict, files, artifacts, spend_usd, why, brief_note}``.

    ``verdict``:
      resumable    real deliverables are on disk; a fresh agent should CONTINUE
      regenerate   nothing survived, so a retry starts where the last one did
      unknown      the evidence could not be read; say so rather than guess
    """
    from . import queue as _queue
    from ..store import writelog as _writelog

    try:
        item = _queue.get(root, int(item_id))
    except LookupError:
        return {"item": int(item_id), "verdict": "unknown", "files": {},
                "artifacts": [], "spend_usd": 0.0,
                "why": f"no item #{item_id} on this board"}

    owner = owner_for(item_id)
    try:
        files = _writelog.split(root, owner)
    except Exception as exc:                                      # noqa: BLE001
        return {"item": int(item_id), "verdict": "unknown", "files": {},
                "artifacts": [], "spend_usd": 0.0,
                "why": f"the write log could not be read ({exc})"}

    on_disk = [rel for rel in files.get("project", [])
               if (Path(root) / rel).exists()]
    vanished = [rel for rel in files.get("project", []) if rel not in on_disk]
    artifacts = _artifacts_for(root, item_id)
    spent = float(item.get("total_cost_usd") or 0.0)

    if on_disk or artifacts:
        verdict = "resumable"
        why = (f"{len(on_disk)} project file(s) and {len(artifacts)} delivered "
               f"artifact(s) from the killed run are STILL ON DISK, and "
               f"${spent:.2f} was already spent producing them. The harness "
               "observed these writes itself — this is not the previous "
               "agent's claim about its own work.")
    elif vanished:
        verdict = "unknown"
        why = (f"the harness observed {len(vanished)} write(s) that are no "
               "longer on disk. Something removed them (a worktree that was "
               "torn down, a revert). Look before regenerating.")
    else:
        verdict = "regenerate"
        why = ("nothing was observed written and no artifact landed, so a "
               "retry genuinely starts where the last one did")

    return {
        "item": int(item_id),
        "status": item.get("status"),
        "attempts": int(item.get("attempts") or 0),
        "auto_retries": int(item.get("auto_retries") or 0),
        "verdict": verdict,
        "files": {"on_disk": on_disk, "vanished": vanished,
                  "harness": files.get("harness", [])},
        "artifacts": artifacts,
        "spend_usd": round(spent, 4),
        "why": why,
        "brief_note": brief_note(root, item_id, on_disk, artifacts, spent),
    }


def _artifacts_for(root: str | os.PathLike[str], item_id: int) -> list[dict]:
    """Paid artifacts this item produced that are still recorded. Never raises."""
    try:
        from ..store import artifacts as _artifacts

        rows = []
        for status in PAID_STATUSES:
            for row in _artifacts.list_revisions(root, status=status, limit=200):
                if int(row.get("work_item_id") or 0) != int(item_id):
                    continue
                path = str(row.get("path") or "")
                rows.append({"path": path, "status": status,
                             "revision": row.get("revision"),
                             "on_disk": (Path(root) / path).exists()})
        return rows
    except Exception:                                             # noqa: BLE001
        return []


def brief_note(root: str | os.PathLike[str], item_id: int,
               on_disk: Optional[list] = None,
               artifacts: Optional[list] = None,
               spent: float = 0.0) -> str:
    """The block appended to a retry's brief. "" when there is nothing to say.

    THE SENTENCE THAT STOPS THE SECOND $11. An agent handed a brief describing
    work it cannot tell has been done will do it again — that is not a failure
    of the agent, it is a failure to hand it the evidence. So the retry brief
    says, in the first thing it reads: this exists, it was paid for, read it
    before you write anything.
    """
    if on_disk is None or artifacts is None:
        got = inspect(root, item_id)
        on_disk = got["files"]["on_disk"]
        artifacts = got["artifacts"]
        spent = got["spend_usd"]
    if not on_disk and not artifacts:
        return ""
    lines = [
        "ALREADY DELIVERED BY THE PREVIOUS RUN — DO NOT REGENERATE IT.",
        "",
        "The previous attempt was killed by a ceiling AFTER doing this work, "
        "not before. The harness OBSERVED these writes itself, so this is not "
        "a claim the dead agent made about itself:",
        "",
    ]
    for rel in (on_disk or [])[:40]:
        lines.append(f"  {rel}")
    if len(on_disk or []) > 40:
        lines.append(f"  ...and {len(on_disk) - 40} more")
    if artifacts:
        lines.append("")
        lines.append(f"PAID ARTIFACTS ({len(artifacts)}), already billed"
                     + (f" — ${spent:.2f} on this item so far:" if spent
                        else ":"))
        for row in artifacts[:20]:
            mark = "" if row.get("on_disk") else "  (MISSING FROM DISK)"
            lines.append(f"  {row['path']} [{row['status']}]{mark}")
    lines += [
        "",
        "READ THESE FIRST. Continue from what is there: finish what is "
        "unfinished, fix what is wrong, and leave what is correct alone. "
        "Regenerating a delivered asset because the run that produced it died "
        "afterwards is paying twice for the same file — measured at $33 across "
        "three agents for work that was ~95% complete after the first.",
        "",
        "If everything the brief asks for is already present and correct, say "
        "so and close the item. That is a legitimate and cheap outcome.",
    ]
    return "\n".join(lines)


def ceilings(item: dict) -> dict:
    """The two INDEPENDENT ceilings, side by side, and which one bit.

    ``max_runtime_s`` and the turn ceiling are unrelated numbers. An item can
    survive one and die on the other, so raising the clock buys nothing for a
    run that then dies on turns — and "raise the ceiling" was the reflex.
    Reporting them together is what makes that visible at the moment somebody
    is about to reach for it.
    """
    return {
        "max_runtime_s": item.get("max_runtime_s"),
        "num_turns": item.get("num_turns"),
        "max_cost_usd": item.get("max_cost_usd"),
        "total_cost_usd": item.get("total_cost_usd"),
        "note": ("these ceilings are independent of one another. Raising the "
                 "clock does nothing for a run that died on turns, and raising "
                 "turns does nothing for one that died on the clock. Before "
                 "raising either, inspect() what the run already delivered — a "
                 "retry that regenerates finished work costs more than the "
                 "ceiling did."),
    }
