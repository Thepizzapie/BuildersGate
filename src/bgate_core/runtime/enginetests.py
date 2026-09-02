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

from ..store import project as _project

LOG_FILE = "engine-tests.jsonl"

# The convention this project's .gd tests already print. Inventing a framework
# nobody's tests use would make the runner answer about nothing.
FAIL_MARKER = re.compile(r"\bFAIL(?:ED|URE|URES)?\b", re.I)
PASS_MARKER = re.compile(r"\bPASS(?:ES|ED)?\b", re.I)


def _log_path(root: str | os.PathLike[str]) -> Path:
    return Path(root) / ".bgate" / LOG_FILE


#: How a caller wants the result shaped. THE DEFAULT IS NOT `full`, AND THAT
#: IS THE POINT.
#:
#: MEASURED on one agent's log while it debugged a single failing assertion:
#: 68% of 8 MB was tool results echoed back — 366 entries averaging 15 KB
#: each. That, and not model quality, is what consumed the turn ceiling and
#: then the clock ceiling, and then bought a retry of work that was already
#: nearly done. A test runner that returns everything every time turns an
#: iterative debug into a context bonfire.
#:
#:   summary        counts and the failing script names. Nothing else.
#:   failures_only  DEFAULT. summary + the failing assertions and stderr,
#:                  excerpted, plus a path to the full log.
#:   changed        failures_only, restricted to scripts whose verdict MOVED
#:                  since the last recorded run — the iterative-debug shape.
#:   full           everything, including passing scripts' output.
MODES = ("summary", "failures_only", "changed", "full")
DEFAULT_MODE = "failures_only"

#: Per-script output kept inline in ``failures_only``. The whole of it is
#: always written to the run log, whose path rides in the result.
EXCERPT_CHARS = 1200

#: Where a run's complete output goes, so `full` is a file read rather than a
#: tool result. One file per run, under the project's own .bgate.
RUN_LOG_DIR = "engine-test-runs"

#: Names the test discovery skips. Agents leave scratch inside test directories
#: — `tests/.orig_player.gd`, temp probe scripts — and `Path.glob("*.gd")`
#: matches leading dots (unlike a shell glob), so the runner picked them up as
#: suite members and scored them. A backup of a broken file is not a test.
SKIP_PREFIXES = (".", "_", "~")


def _is_test_script(path: Path) -> bool:
    return path.suffix == ".gd" and not path.name.startswith(SKIP_PREFIXES)


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
        scripts = sorted(p.name for p in d.glob("*.gd") if _is_test_script(p))
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
        "passed": int(result.get("assertions_passed")
                      or result.get("passed") or 0),
        "failures": int(result.get("assertions_failed") or 0),
        "seconds": result.get("seconds"),
        "by": str(result.get("by") or ""),
        "engine_errors": int(result.get("engine_error_scripts") or 0),
        # Kept in the history too: a week where seven scripts were refused
        # every night is a fact about the harness, and a stored 0/0 hid it.
        "refused": int(result.get("scripts_refused") or 0),
        "scripts": [{"script": s.get("script"), "ok": bool(s.get("ok")),
                     "ran": bool(s.get("ran", True)),
                     "refused": str(s.get("refused") or ""),
                     "assertions_ok": s.get("assertions_ok"),
                     "process_ok": s.get("process_ok"),
                     "passed": s.get("passed"),
                     "failed": s.get("failed"),
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
        timeout: int = 180, godot_project: str = "", actor: str = "",
        mode: str = DEFAULT_MODE) -> dict:
    """Run the suite headless, score it, and RECORD the score.

    TWO SIGNALS, KEPT APART. Exit code alone is not the verdict: Godot prints
    SCRIPT ERROR and still exits 0. But collapsing "assertions failed" into
    "the process reported errors" produced a result that read as nonsense —
    ``ok: false`` with ``0`` failed assertions — and it was dismissed as
    harness noise by two separate readers for a full session.

    IT WAS NOT NOISE. The engine error in question was
    ``N resources still in use at exit``, and it was a genuine leak in the
    project's own tests: a still-parented node being freed, and ``quit()``
    racing the RenderingServer's RID release. So the two signals are now
    DISTINGUISHABLE (``assertions_ok`` vs ``process_ok``) and the second is
    NOT made easier to ignore — it gets its own count, its own top-level
    field and its own sentence, because "the engine complained" is the shape a
    real defect arrives in.

    ``mode`` bounds what comes back. See :data:`MODES`; the default is
    ``failures_only`` and the full output always lands in a file whose path is
    in the result.
    """
    from bgate_adapters import godot as _godot

    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    started = time.monotonic()
    base = Path(godot_project) if godot_project else _project.game_dir(root)
    if base is None or not (Path(base) / "project.godot").is_file():
        out = {"ok": False, "no_tests": True, "scripts": [], "scripts_run": 0,
               "godot_project": "", "failures": [], "assertions_passed": 0,
               "assertions_failed": 0,
               "error": "no project.godot found at or under "
                        f"{root} - run godot_scaffold, or pass godot_project"}
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
            scripts = sorted(p for p in (base / "tests").glob("*.gd")
                             if _is_test_script(p))
        except OSError:
            scripts = []

    if not scripts:
        out = {
            "ok": False, "no_tests": True, "scripts": [], "scripts_run": 0,
            "tests_dir": str(base / "tests"), "godot_project": str(base),
            "missing": missing, "failures": [], "assertions_passed": 0,
            "assertions_failed": 0,
            "error": (f"none of {missing} exist" if missing else
                      f"no test scripts in {base / 'tests'} — this project has "
                      "no regression baseline to check. Write one (extends "
                      "SceneTree, print PASS/FAIL per assertion, call quit())"),
        }
        return {**out, "recorded": record(root, {**out, "by": actor})}

    previous = {row["script"]: row
                for row in (history(root, limit=1) or [{}])[0].get("scripts", [])
                if isinstance(row, dict) and row.get("script")}

    results: list[dict[str, Any]] = []
    transcript: list[str] = []
    failed = total_pass = total_fail = engine_bad = refused = 0
    for script in scripts:
        try:
            rel = script.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            rel = str(script)
        try:
            source = script.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            results.append({"script": rel, "ok": False, "assertions_ok": False,
                            "process_ok": False, "passed": 0, "failed": 0,
                            "error": f"unreadable: {exc}"})
            failed += 1
            continue
        got = _godot.run_script(source, project_dir=str(base), timeout=timeout)
        # A REFUSAL IS NOT A RESULT. The runner declined to spawn the engine at
        # all, so this script has no assertions and no engine behaviour to
        # report. Scoring it 0 passed / 0 failed WAS THE BUG: the autoload gate
        # refused seven healthy scripts and each came back zero-and-zero, which
        # reads as "this file contains nothing" rather than "I declined to look
        # at it" — and behind those empty scores sat 441 passing assertions and
        # 2 that genuinely failed.
        #
        # None, not 0, for the counts. A number invites arithmetic; there is no
        # honest number here, and null is the only value that cannot be summed
        # into somebody's total by accident.
        if got.get("refused"):
            transcript.append(f"===== {rel} =====\nNOT RUN "
                              f"({got['refused']}): {got.get('error', '')}\n")
            results.append({
                "script": rel, "ok": False, "ran": False,
                "refused": str(got["refused"]),
                "assertions_ok": None, "process_ok": None,
                "passed": None, "failed": None,
                "error": str(got.get("error") or ""),
                "hint": str(got.get("hint") or ""),
                "why_not_run": ("the harness refused to run this script — it "
                                "has NOT been checked. This is not a pass and "
                                "not a failure, and no count in this result "
                                "includes it."),
            })
            refused += 1
            continue
        output = (got.get("stdout") or "") + (got.get("stderr") or "")
        transcript.append(f"===== {rel} =====\n{output}\n")
        passes = len(PASS_MARKER.findall(output))
        fails = len(FAIL_MARKER.findall(output))
        errors = got.get("errors") or []
        # THE TWO QUESTIONS, ASKED SEPARATELY.
        assertions_ok = fails == 0
        process_ok = bool(got.get("ok")) and not errors
        ok = assertions_ok and process_ok
        if not process_ok:
            engine_bad += 1
        entry = {
            "script": rel, "ok": ok, "ran": True,
            "assertions_ok": assertions_ok, "process_ok": process_ok,
            "passed": passes, "failed": fails,
            "exit_code": got.get("exit_code"), "seconds": got.get("seconds"),
            "errors": errors,
            "changed": (rel in previous
                        and previous[rel].get("ran", True)
                        and bool(previous[rel].get("ok")) != ok),
        }
        entry["_output"] = output
        if not ok:
            entry["error"] = _why(assertions_ok, process_ok, fails, errors,
                                  str(got.get("error") or ""))
            failed += 1
        total_pass += passes
        total_fail += fails
        results.append(entry)

    log = _write_run_log(root, transcript)
    out = {
        # A SUITE WITH A REFUSED SCRIPT IN IT IS NOT GREEN. Whatever that
        # script would have proved is unproven, and unproven is not passing.
        "ok": failed == 0 and refused == 0,
        "no_tests": False, "scripts": results,
        # scripts_run counts what actually RAN. It counted what was ATTEMPTED,
        # so a suite of fifteen with seven refused reported fifteen run.
        "scripts_run": len(results) - refused,
        "scripts_attempted": len(results),
        "scripts_refused": refused,
        "refused_note": ("" if not refused else
                         f"{refused} of {len(results)} script(s) were NOT RUN "
                         "— the harness refused them before the engine "
                         "started. They are UNCHECKED: not passing, not "
                         "failing, and absent from every count above. Read "
                         "`error` and `hint` on each; the fix is usually the "
                         "script's own shape, not the game."),
        "scripts_failed": failed,
        "godot_project": str(base),
        # `failures` NAMES the failing scripts; the assertion tallies are their
        # own two numbers. One key meaning "a count of assertions" in the core
        # and "a list of script names" in the MCP tool is exactly the drift
        # that two copies of this runner had already produced.
        "failures": [r["script"] for r in results if not r["ok"]],
        "assertions_passed": total_pass, "assertions_failed": total_fail,
        "passed": total_pass,
        # NOT FOLDED INTO `failures`. An engine error with zero failed
        # assertions is the shape a REAL leak arrived in and was waved away as
        # harness noise. It gets a number of its own so it cannot read as a
        # rounding error on somebody else's count.
        "engine_error_scripts": engine_bad,
        "engine_errors_note": ("" if not engine_bad else
                               f"{engine_bad} script(s) had ASSERTIONS that "
                               "may be fine and a PROCESS that reported "
                               "errors. Do not dismiss this as harness noise: "
                               "the last time it was, `N resources still in "
                               "use at exit` was a real leak in the project's "
                               "own tests (a still-parented node freed, and "
                               "quit() racing the RenderingServer). Read "
                               "`errors` on each script below."),
        "tests_dir": str(base / "tests"), "missing": missing,
        "full_log": log,
        "seconds": round(time.monotonic() - started, 2),
        "error": (f"{failed} of {len(results)} test script(s) failed: "
                  + ", ".join(r["script"] for r in results if not r["ok"])
                  if failed else ""),
    }
    recorded = record(root, {**out, "by": actor})
    return {**shape(out, mode), "recorded": recorded}


def _why(assertions_ok: bool, process_ok: bool, fails: int,
         errors: list, reported: str) -> str:
    """One sentence naming WHICH signal failed. They are not the same fault."""
    if not assertions_ok and not process_ok:
        return (f"{fails} FAIL marker(s) AND the engine reported "
                f"{len(errors)} error(s) — two independent faults")
    if not assertions_ok:
        return f"{fails} FAIL marker(s): assertions in this script did not hold"
    return ("ASSERTIONS PASSED; the ENGINE reported "
            + (f"{len(errors)} error(s): " + "; ".join(str(e)[:160]
                                                       for e in errors[:3])
               if errors else (reported or "a nonzero exit"))
            + ". This is a separate fault from a failed assertion and it is "
              "not automatically noise.")


def _write_run_log(root: str | os.PathLike[str],
                   transcript: list[str]) -> str:
    """The whole output, on disk, so a concise result can still be complete."""
    try:
        directory = Path(root) / ".bgate" / RUN_LOG_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (time.strftime("%Y%m%d-%H%M%S", time.gmtime())
                            + f"-{os.getpid()}.log")
        path.write_text("".join(transcript), encoding="utf-8")
        # Bounded: keep the last 20 runs. A debug loop produces one per minute
        # and nobody reads the fortieth.
        old = sorted(directory.glob("*.log"))[:-20]
        for stale in old:
            stale.unlink(missing_ok=True)
        return str(path)
    except OSError:
        return ""


def shape(result: dict, mode: str) -> dict:
    """Trim a full result to what ``mode`` asked for. Pure.

    Separate from :func:`run` so the recorded history is always the complete
    one — a concise ANSWER must not become a concise RECORD.
    """
    scripts = result.get("scripts") or []
    if mode == "full":
        # THE WHOLE OF IT, every script. The one mode where a passing script's
        # engine boot chatter is what you asked for.
        return {**result, "mode": "full", "scripts_omitted": 0,
                "scripts": [{**{k: v for k, v in s.items() if k != "_output"},
                             "output": s.get("_output", "")} for s in scripts]}
    if mode == "summary":
        kept = [{"script": s["script"], "ok": s["ok"],
                 "assertions_ok": s.get("assertions_ok"),
                 "process_ok": s.get("process_ok"),
                 "passed": s.get("passed"), "failed": s.get("failed"),
                 "error": s.get("error", "")}
                for s in scripts if not s["ok"]]
    elif mode == "changed":
        kept = [_trim(s) for s in scripts if s.get("changed")]
    else:                                             # failures_only
        kept = [_trim(s) for s in scripts if not s["ok"]]
    return {
        **result,
        "scripts": kept,
        "mode": mode,
        "scripts_omitted": len(scripts) - len(kept),
        "failures": result.get("failures"),
        "note": (f"mode={mode}: {len(scripts) - len(kept)} script(s) not "
                 "shown. The complete output of every script is at "
                 f"{result.get('full_log') or '(no log written)'}; pass "
                 "mode='full' to get it inline, which is rarely what you "
                 "want mid-debug."),
    }


def _trim(script: dict) -> dict:
    """One script, with its output cut to an excerpt and the rest cited."""
    out = {k: v for k, v in script.items() if k != "_output"}
    whole = str(script.get("_output") or "")
    if not script.get("ok") and whole:
        # THE TAIL, not the head: a test script prints its assertions in order
        # and the failing one is near the end, behind the engine's boot banner.
        out["output"] = whole[-EXCERPT_CHARS:]
        if len(whole) > EXCERPT_CHARS:
            out["output_clipped"] = len(whole) - EXCERPT_CHARS
    return out
