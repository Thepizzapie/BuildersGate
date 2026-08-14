"""What the engine itself was asked to prove, and when — kept, not thrown away.

``godot_test_run`` runs this project's ``tests/*.gd`` headless and scores them,
and then returns the score TO ITS CALLER AND NOWHERE ELSE. That is fine for an
agent answering "are tests green right now" and useless for the QA seat's actual
question, which is comparative: what was green last week, which script started
failing, and whether anybody has run the suite since the change that broke it. A
verdict with no history cannot answer any of those, so the dashboard's Tests tab
had nothing to draw and said so.

THE RUN LIVES HERE, NOT IN THE MCP TOOL. Both callers want the same thing, and
the recording has to happen wherever the run happens or it does not happen at
all — a tool that returns a result and a dashboard that re-runs it separately
would produce two histories that disagree. ``run()`` is the whole runner;
``godot_test_run`` can delegate to it in one line.

HISTORY IS A JSONL FILE, not a table. Same reason cinecheck's watch log is:
``.bgate/`` already holds causal_specs.json, tunables.json and notify.jsonl, and
appending a line is the one write that cannot lose earlier lines to a crash.

A PROJECT WITH NO TEST SCRIPTS IS NOT A PASS, and that judgement is inherited
from the tool verbatim — `no_tests` is a distinct outcome from `ok`, because
zero failures out of nothing run is the most misleading number available here.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from bgate_core import project as _project

LOG_FILE = "engine-tests.jsonl"

# The convention this project's .gd tests already print. Inventing a framework
# nobody's tests use would make the runner answer about nothing.
FAIL_MARKER = re.compile(r"\bFAIL(?:ED|URE|URES)?\b", re.I)
PASS_MARKER = re.compile(r"\bPASS(?:ES|ED)?\b", re.I)


def _log_path(root: str | os.PathLike[str]) -> Path:
    return Path(root) / ".bgate" / LOG_FILE


def tests_dir(root: str | os.PathLike[str]) -> Optional[Path]:
    """``<godot project>/tests``, resolved through the real layout, or None.

    Two entrypoints put project.godot in different places; asking project.game_dir
    is the difference between working on every project and working on half.
    """
    base = _project.game_dir(root)
    return None if base is None else Path(base) / "tests"


def discover(root: str | os.PathLike[str]) -> dict:
    """The test scripts that exist on disk, before anybody runs anything.

    This is what makes the empty state truthful: "no runs recorded" and "this
    project has no tests" are different sentences and the screen has to be able
    to say which one it is looking at.
    """
    base = _project.game_dir(root)
    if base is None:
        return {"tests_dir": "", "scripts": [], "godot_project": "",
                "why": "no project.godot was found, so there is no test suite to run"}
    d = Path(base) / "tests"
    try:
        scripts = sorted(p.name for p in d.glob("*.gd"))
    except OSError:
        scripts = []
    return {
        "tests_dir": str(d), "godot_project": str(base), "scripts": scripts,
        "why": ("" if scripts else
                f"no *.gd in {d} — a regression gate with nothing in it looks "
                "exactly like a green one"),
    }


def history(root: str | os.PathLike[str], *, limit: int = 20) -> list[dict]:
    """Recorded runs, newest first. Corrupt lines are skipped, not fatal."""
    try:
        lines = _log_path(root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
        if len(out) >= max(1, int(limit)):
            break
    return out


def record(root: str | os.PathLike[str], result: dict) -> dict:
    """Append one run to the history. Best-effort — never raises at a caller.

    Only the summary is kept. A green suite's stdout is thousands of lines of
    engine boot chatter and storing it per run turns the log into a liability
    rather than a record.
    """
    row = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ok": bool(result.get("ok")),
        "no_tests": bool(result.get("no_tests")),
        "scripts_run": int(result.get("scripts_run") or 0),
        "scripts_failed": int(result.get("scripts_failed") or 0),
        "passed": int(result.get("passed") or 0),
        "failures": int(result.get("failures") or 0),
        "seconds": result.get("seconds"),
        "by": str(result.get("by") or ""),
        "scripts": [{"script": s.get("script"), "ok": bool(s.get("ok")),
                     "passed": int(s.get("passed") or 0),
                     "failed": int(s.get("failed") or 0),
                     "error": str(s.get("error") or "")[:400]}
                    for s in (result.get("scripts") or [])],
        "error": str(result.get("error") or "")[:400],
    }
    try:
        path = _log_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass
    return row


def run(root: str | os.PathLike[str], *, paths: Optional[list[str]] = None,
        timeout: int = 180, godot_project: str = "", actor: str = "") -> dict:
    """Run the suite headless, score it, and RECORD the score.

    Exit code alone is not the verdict: Godot prints SCRIPT ERROR and still
    exits 0, so a script that errored is failed here however many PASS lines it
    managed first.
    """
    from bgate_adapters import godot as _godot

    started = time.monotonic()
    base = Path(godot_project) if godot_project else _project.game_dir(root)
    if base is None or not (Path(base) / "project.godot").is_file():
        out = {"ok": False, "no_tests": True, "scripts": [], "scripts_run": 0,
               "error": f"no Godot project found at or under {root}"}
        return {**out, "recorded": record(root, {**out, "by": actor})}
    base = Path(base)

    scripts: list[Path] = []
    missing: list[str] = []
    if paths:
        for raw in paths:
            for candidate in (Path(raw), base / raw, Path(root) / raw):
                if candidate.is_file():
                    scripts.append(candidate)
                    break
            else:
                missing.append(str(raw))
    else:
        try:
            scripts = sorted((base / "tests").glob("*.gd"))
        except OSError:
            scripts = []

    if not scripts:
        out = {
            "ok": False, "no_tests": True, "scripts": [], "scripts_run": 0,
            "tests_dir": str(base / "tests"), "missing": missing,
            "error": (f"none of {missing} exist" if missing else
                      f"no test scripts in {base / 'tests'} — this project has "
                      "no regression baseline to check"),
        }
        return {**out, "recorded": record(root, {**out, "by": actor})}

    results: list[dict[str, Any]] = []
    failed = total_pass = total_fail = 0
    for script in scripts:
        try:
            rel = script.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            rel = str(script)
        try:
            source = script.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            results.append({"script": rel, "ok": False, "passed": 0,
                            "failed": 0, "error": f"unreadable: {exc}"})
            failed += 1
            continue
        got = _godot.run_script(source, project_dir=str(base), timeout=timeout)
        output = (got.get("stdout") or "") + (got.get("stderr") or "")
        passes = len(PASS_MARKER.findall(output))
        fails = len(FAIL_MARKER.findall(output))
        errors = got.get("errors") or []
        ok = bool(got.get("ok")) and fails == 0 and not errors
        entry = {"script": rel, "ok": ok, "passed": passes, "failed": fails,
                 "exit_code": got.get("exit_code"), "seconds": got.get("seconds"),
                 "errors": errors}
        if not ok:
            entry["error"] = str(got.get("error") or "") or (
                f"{fails} FAIL marker(s)" if fails else "engine errors")
            failed += 1
        total_pass += passes
        total_fail += fails
        results.append(entry)

    out = {
        "ok": failed == 0, "no_tests": False, "scripts": results,
        "scripts_run": len(results), "scripts_failed": failed,
        "passed": total_pass, "failures": total_fail,
        "tests_dir": str(base / "tests"), "missing": missing,
        "seconds": round(time.monotonic() - started, 2),
    }
    return {**out, "recorded": record(root, {**out, "by": actor})}
