"""The same scripted drive run three times inside one item — item #19b.

MEASURED: item #6 re-ran a 5-minute headless bot drive four times after four
edits plus two full suites — an hour of wall clock — because nothing told
the agent it had already seen this exact run's output. Re-running an
IDENTICAL script against an IDENTICAL project with IDENTICAL args cannot
have produced new information the first two runs did not already contain;
the third run is either a habit or a way of avoiding reading the log that
is already sitting on disk.

So: the same (script text, args, project) run twice within one work item
(``BGATE_WORK_ITEM``) is allowed — the first proves it, the second confirms
it was not a fluke — and refused on the THIRD unless ``force=True``. The
record is a small per-project table under ``.bgate``, keyed by item id, so
it is scoped to the ONE item that is looping and does not follow the agent
into its next item or penalise a different item running the same script on
purpose (a regression suite run by two items in the same hour is not a
loop).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

DIRNAME = "rundedup"

#: How many identical runs are allowed before a refusal. The FIRST run and
#: one confirming re-run are both legitimate; a third is the loop.
MAX_IDENTICAL_RUNS = 2

#: A record older than this cannot be blocking anything real — the item that
#: made it is long over. Generous on purpose; this is a loop-breaker, not a
#: rate limiter, and a false refusal costs more than a stale record.
RECORD_TTL_S = 6 * 3600


def _dir(root: str | os.PathLike[str]) -> Path:
    return Path(root) / ".bgate" / DIRNAME


def fingerprint(*, script: str, args: object = None,
               project_dir: str = "") -> str:
    """One hash standing for "this exact run" — script text, args, project."""
    payload = json.dumps({
        "script": script, "args": args,
        "project": os.path.normpath(os.path.abspath(project_dir or ""))
                  .replace("\\", "/"),
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _path(root: str | os.PathLike[str], item_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(item_id))
    return _dir(root) / f"{safe or 'no_item'}.json"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(path: Path, doc: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def check(root: str | os.PathLike[str], *, item_id: str, fp: str,
         force: bool = False) -> dict:
    """Before spawning: ``{ok, count, refuse, log_path}``.

    ``ok=False`` (``refuse=True``) means this exact run has already
    happened :data:`MAX_IDENTICAL_RUNS` times inside this item and
    ``force`` was not passed. The caller is expected to return this
    straight back rather than spawn — see ``bgate_adapters.godot.run_script``.
    """
    if not item_id:
        # No BGATE_WORK_ITEM means nothing here can be scoped to an item, so
        # there is nothing to refuse — a bare interactive session running the
        # same script twice on purpose is not the loop this exists to stop.
        return {"ok": True, "count": 0, "refuse": False, "log_path": ""}
    doc = _read(_path(root, item_id))
    runs = doc.get("runs") if isinstance(doc.get("runs"), dict) else {}
    row = runs.get(fp) or {}
    count = int(row.get("count") or 0)
    now = time.time()
    if now - float(row.get("first_at") or now) > RECORD_TTL_S:
        count = 0
    refuse = count >= MAX_IDENTICAL_RUNS and not force
    return {"ok": not refuse, "count": count, "refuse": refuse,
            "log_path": str(row.get("log_path") or "")}


def record(root: str | os.PathLike[str], *, item_id: str, fp: str,
          log_path: str = "") -> dict:
    """After a run completes: bump this fingerprint's count for this item."""
    if not item_id:
        return {}
    path = _path(root, item_id)
    doc = _read(path)
    runs = doc.get("runs")
    doc["runs"] = runs if isinstance(runs, dict) else {}
    row = doc["runs"].get(fp) or {}
    now = time.time()
    first_at = float(row.get("first_at") or now)
    if now - first_at > RECORD_TTL_S:
        row = {}
        first_at = now
    count = int(row.get("count") or 0) + 1
    doc["runs"][fp] = {"count": count, "first_at": first_at, "last_at": now,
                       "log_path": log_path}
    _write(path, doc)
    return doc["runs"][fp]


def refusal_message(check_result: dict, *, script_kind: str = "script") -> str:
    log = check_result.get("log_path") or ""
    tail = f" — read the log at {log}" if log else ""
    return (f"you already ran this {script_kind} {check_result.get('count')} "
            f"times in this work item with the identical script and args"
            f"{tail}. Re-running it a third time cannot produce information "
            "the first two runs did not already contain; pass force=True if "
            "you genuinely need to run it again.")
