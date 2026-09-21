"""GRIPE 38 (EXIT 67 postmortem, 2026-09-21): agents had no sense of what is
CURRENT. The human repeatedly left the board and used a CLI agent directly to
make a specific edit, because a board agent was underperforming or too slow —
and that edit then happened OUTSIDE the harness, with no agent any the wiser.
A dispatched agent that reads a file the human just hand-edited an hour ago,
with no signal that happened, is the exact defect this module answers.

``current_activity`` is the read: files changed in the last N hours, grouped
by lane, with WHO changed them (a harness auto-commit — "item #N ..." — vs
anything else, which is a human or an outside agent), the items completed or
failed in that window, and the last few handoff notes. Bounded to roughly
1.5 KB so it is cheap to print under every dispatched item's brief (see
dispatch.py's ``{current_block}``) and inside seats.brief()'s ``current``
field.

Nothing here raises: a project with no git or no commits gets an honest
"nothing to report", the same convention gitwork.py already uses, because a
missing audit trail must never take down dispatch.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from . import gitwork as _git

# The auto-commit message format ("item #N <title>") — see gitwork/dispatch's
# commit_paths. Anything NOT matching this is, by definition, outside the
# board: a human's own commit, or an agent working straight in a shell.
_ITEM_COMMIT_RE = re.compile(r"^item #(\d+)\b")

# Keep the block printable under a dispatched item's brief without ballooning
# it — this is a "since you last looked" summary, not a full audit.
MAX_CHARS = 1500
MAX_FILES_LISTED = 12


def _lane_for(path: str) -> str:
    """Best-effort lane name for a path, for grouping only — never a security
    boundary (that is seats.can_write)."""
    try:
        from . import seats as _seats
        for role, cfg in _seats.DEFAULT_SEATS.items():
            for g in cfg.get("write_globs", []):
                prefix = str(g).split("*", 1)[0]
                if prefix and path.startswith(prefix):
                    return role
    except Exception:
        pass
    return "other"


def _recent_commits(root, hours: int) -> list[dict]:
    ok, out, _err = _git._run(
        root, ["log", f"--since={int(hours)} hours ago",
               "--name-only", "--pretty=format:%x01%H%x02%s"],
        timeout=15)
    if not ok or not out.strip():
        return []
    commits: list[dict] = []
    cur: Optional[dict] = None
    for line in out.splitlines():
        if line.startswith("\x01"):
            if cur is not None:
                commits.append(cur)
            _, rest = line[1:].split("\x02", 1)
            cur = {"subject": rest, "paths": []}
        elif line.strip() and cur is not None:
            cur["paths"].append(line.strip())
    if cur is not None:
        commits.append(cur)
    return commits


def current_activity(root: str | os.PathLike[str], hours: int = 6) -> dict:
    """Files changed in the last ``hours``, grouped by lane, with WHO changed
    them and the board activity in that window. Bounded, never raises."""
    state = _git.probe(root)
    if not state["available"]:
        return {"available": False, "reason": state["reason"],
                "hours": int(hours), "outside_the_board": [],
                "by_lane": {}, "items": {"completed": [], "failed": []},
                "handoff_notes": []}

    outside: list[str] = []
    by_lane: dict[str, set] = {}

    for commit in _recent_commits(root, hours):
        is_board = bool(_ITEM_COMMIT_RE.match(commit["subject"]))
        for path in commit["paths"]:
            by_lane.setdefault(_lane_for(path), set()).add(path)
            if not is_board:
                outside.append(path)

    try:
        dirty = _git.dirty(root)
        for path in dirty.get("paths", []):
            by_lane.setdefault(_lane_for(path), set()).add(path)
            outside.append(path)          # uncommitted: nobody's item claimed it yet
    except Exception:
        pass

    items = {"completed": [], "failed": []}
    try:
        import time as _time

        from . import queue as _q
        cutoff = _time.strftime(
            "%Y-%m-%d %H:%M:%S",
            _time.gmtime(_time.time() - int(hours) * 3600))
        for row in _q.list_items(root):
            if row.get("status") not in ("done", "failed"):
                continue
            updated = str(row.get("updated_at") or "")
            if not updated or updated < cutoff:
                continue
            items_key = "completed" if row["status"] == "done" else "failed"
            items[items_key].append(
                {"id": row["id"], "title": str(row.get("title") or "")[:80]})
        items["completed"] = items["completed"][-8:]
        items["failed"] = items["failed"][-8:]
    except Exception:
        pass

    notes = []
    try:
        from . import handoff as _handoff
        notes = [str(n.get("text", ""))[:200] for n in
                 _handoff.read(root, limit=3)]
    except Exception:
        notes = []

    outside_dedup = sorted(set(outside))[:MAX_FILES_LISTED]
    out = {
        "available": True, "hours": int(hours),
        "outside_the_board": outside_dedup,
        "by_lane": {lane: sorted(paths)[:MAX_FILES_LISTED]
                    for lane, paths in by_lane.items()},
        "items": items,
        "handoff_notes": notes,
    }
    return out


def summary_text(root: str | os.PathLike[str], hours: int = 6) -> str:
    """The one-paragraph form printed under a dispatched item — this is what
    ``{current_block}`` in prompts/dispatch.txt renders."""
    data = current_activity(root, hours=hours)
    if not data["available"]:
        return ""
    outside = data["outside_the_board"]
    if not outside:
        return ""
    text = (f"SINCE {hours}h ago: {len(outside)} file(s) changed outside "
            f"the board: {', '.join(outside)}")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS - 1].rsplit(",", 1)[0] + " …"
    return text
