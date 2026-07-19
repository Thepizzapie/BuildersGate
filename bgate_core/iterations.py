"""Causal iteration snapshots: goal -> build -> evidence -> decisions -> outcome."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from . import assets, db
from .util import rows

TELEMETRY_SCHEMA_VERSION = 1
_SKIP_DIRS = {".git", ".godot", ".bgate", ".bgate_out", "__pycache__", "export"}
_TUNABLE = re.compile(
    r"@export(?:_[a-z_]+)?(?:\([^)]*\))?\s+var\s+([A-Za-z_]\w*)"
    r"(?:\s*:[^=]+)?\s*=\s*([^\n#]+)")


def _sha(parts: list[bytes]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.hexdigest()


def _git_snapshot(root: Path) -> tuple[str, str]:
    commit = ""
    dirty = ""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, timeout=10, stdin=subprocess.DEVNULL)
        if head.returncode == 0:
            commit = head.stdout.strip()
        state = subprocess.run(
            ["git", "status", "--porcelain=v1", "-uall"], cwd=root,
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD"], cwd=root,
            capture_output=True, timeout=15, stdin=subprocess.DEVNULL)
        dirty_parts = [
            state.stdout.encode("utf-8", errors="replace"),
            diff.stdout or b"",
        ]
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=root, capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
        for raw in (untracked.stdout or b"").split(b"\0"):
            if not raw:
                continue
            path = root / raw.decode("utf-8", errors="surrogateescape")
            if path.is_file():
                dirty_parts.extend((raw, assets.file_hash(path).encode()))
        dirty = _sha(dirty_parts)
    except (OSError, subprocess.SubprocessError):
        pass
    return commit, dirty


def _source_fingerprint(root: Path) -> str:
    parts: list[bytes] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            rel = str(path.relative_to(root)).replace("\\", "/")
            parts.extend((rel.encode(), assets.file_hash(path).encode()))
        except OSError:
            continue
    return _sha(parts)


def _tunables(root: Path) -> dict:
    captured: dict[str, dict[str, str]] = {}
    game = root / "game"
    if game.is_dir():
        for script in sorted(game.rglob("*.gd")):
            try:
                found = {
                    name: value.strip()
                    for name, value in _TUNABLE.findall(
                        script.read_text(encoding="utf-8", errors="replace"))
                }
            except OSError:
                continue
            if found:
                captured[str(script.relative_to(root)).replace("\\", "/")] = found
    override = root / ".bgate" / "tunables.json"
    if override.is_file():
        try:
            captured["overrides"] = json.loads(override.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            captured["overrides"] = {"error": "invalid .bgate/tunables.json"}
    return captured


def snapshot(root: str | os.PathLike[str]) -> dict:
    project = Path(root).resolve()
    commit, dirty = _git_snapshot(project)
    export = project / "export" / "web" / "index.pck"
    artifact_ids = [
        int(row[0]) for row in db.connect(project).execute(
            "SELECT id FROM artifact_revision "
            "WHERE status IN ('approved','integrated') ORDER BY logical_name, revision")
    ]
    tests_path = project / ".bgate" / "test-results.json"
    tests = {"status": "not_captured"}
    if tests_path.is_file():
        try:
            tests = json.loads(tests_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            tests = {"status": "invalid", "path": str(tests_path)}
    return {
        "source_commit": commit,
        "dirty_fingerprint": dirty,
        "source_fingerprint": _source_fingerprint(project),
        "export_hash": assets.file_hash(export) if export.is_file() else "",
        "active_artifact_ids": artifact_ids,
        "tunables": _tunables(project),
        "tests": tests,
        "telemetry_schema_version": TELEMETRY_SCHEMA_VERSION,
    }


def create(root: str | os.PathLike[str], goal: str) -> dict:
    conn = db.connect(root)
    previous = conn.execute(
        "SELECT id, status FROM iteration ORDER BY id DESC LIMIT 1").fetchone()
    if previous and previous["status"] == "active":
        session = conn.execute(
            "SELECT id FROM playtest_session WHERE iteration_id = ? "
            "ORDER BY id DESC LIMIT 1", (previous["id"],)).fetchone()
        if session:
            complete_from_playtest(root, int(previous["id"]), int(session["id"]))
        else:
            with db.tx(root) as tx:
                tx.execute(
                    "UPDATE iteration SET status = 'abandoned', "
                    "completed_at = datetime('now') WHERE id = ?",
                    (previous["id"],))
    snap = snapshot(root)
    with db.tx(root) as tx:
        cur = tx.execute(
            """
            INSERT INTO iteration (
                goal, previous_id, source_commit, dirty_fingerprint,
                source_fingerprint, export_hash, active_artifact_ids_json,
                tunables_json, tests_json, telemetry_schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (goal.strip() or "Evaluate current build",
             int(previous["id"]) if previous else None,
             snap["source_commit"], snap["dirty_fingerprint"],
             snap["source_fingerprint"], snap["export_hash"],
             json.dumps(snap["active_artifact_ids"]),
             json.dumps(snap["tunables"]), json.dumps(snap["tests"]),
             snap["telemetry_schema_version"]),
        )
        iteration_id = int(cur.lastrowid)
    add_event(root, iteration_id, "snapshot", "iteration", str(iteration_id),
              "Captured source, build, assets, tunables, tests, and telemetry contract",
              snap)
    if previous:
        add_event(
            root, int(previous["id"]), "resulting_build", "iteration",
            str(iteration_id), f"Resulting build became iteration {iteration_id}",
            {"source_fingerprint": snap["source_fingerprint"],
             "export_hash": snap["export_hash"],
             "active_artifact_ids": snap["active_artifact_ids"]})
    return get(root, iteration_id)


def add_event(root: str | os.PathLike[str], iteration_id: int, stage: str,
              ref_type: str = "", ref_id: str = "", summary: str = "",
              data: Optional[dict] = None) -> dict:
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO iteration_event "
            "(iteration_id, stage, ref_type, ref_id, summary, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (iteration_id, stage, ref_type, ref_id, summary,
             json.dumps(data or {})))
        event_id = int(cur.lastrowid)
    row = db.connect(root).execute(
        "SELECT * FROM iteration_event WHERE id = ?", (event_id,)).fetchone()
    return _decode_event(dict(row))


def active_id(root: str | os.PathLike[str]) -> Optional[int]:
    override = os.environ.get("BGATE_ITERATION", "")
    if override.isdigit():
        return int(override)
    row = db.connect(root).execute(
        "SELECT id FROM iteration WHERE status = 'active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return int(row["id"]) if row else None


def complete_from_playtest(root: str | os.PathLike[str], iteration_id: int,
                           session_id: int) -> dict:
    conn = db.connect(root)
    current_iteration = conn.execute(
        "SELECT * FROM iteration WHERE id = ?", (iteration_id,)).fetchone()
    session = conn.execute(
        "SELECT * FROM playtest_session WHERE id = ?", (session_id,)).fetchone()
    counts = {
        "feedback": conn.execute(
            "SELECT count(*) FROM playtest_item WHERE session_id = ?",
            (session_id,)).fetchone()[0],
        "telemetry_events": conn.execute(
            "SELECT count(*) FROM playtest_event WHERE session_id = ?",
            (session_id,)).fetchone()[0],
        "promoted": conn.execute(
            "SELECT count(*) FROM playtest_item "
            "WHERE session_id = ? AND status = 'promoted'",
            (session_id,)).fetchone()[0],
    }
    previous = conn.execute(
        "SELECT * FROM iteration "
        "WHERE id = (SELECT previous_id FROM iteration WHERE id = ?)",
        (iteration_id,)).fetchone()
    previous_outcome = {}
    if previous:
        try:
            previous_outcome = json.loads(previous["outcome_json"])
        except json.JSONDecodeError:
            pass
    outcome = {
        "session_id": session_id,
        "duration_s": float(session["duration_s"] or 0),
        **counts,
        "versus_previous": {
            key: counts[key] - int(previous_outcome.get(key, 0))
            for key in counts
        } if previous_outcome else {},
        "snapshot_delta": {
            "source_changed": bool(previous and current_iteration
                                   and previous["source_fingerprint"]
                                   != current_iteration["source_fingerprint"]),
            "build_changed": bool(previous and current_iteration
                                  and previous["export_hash"]
                                  != current_iteration["export_hash"]),
            "active_assets_changed": bool(
                previous and current_iteration
                and previous["active_artifact_ids_json"]
                != current_iteration["active_artifact_ids_json"]),
        } if previous else {},
    }
    with db.tx(root) as tx:
        tx.execute(
            "UPDATE iteration SET status = 'complete', outcome_json = ?, "
            "completed_at = datetime('now') WHERE id = ?",
            (json.dumps(outcome), iteration_id))
    add_event(root, iteration_id, "outcome", "playtest", str(session_id),
              "Iteration outcome captured", outcome)
    return get(root, iteration_id)


def get(root: str | os.PathLike[str], iteration_id: int) -> dict:
    row = db.connect(root).execute(
        "SELECT * FROM iteration WHERE id = ?", (iteration_id,)).fetchone()
    if row is None:
        raise LookupError(f"no iteration {iteration_id}")
    item = _decode(dict(row))
    item["events"] = [
        _decode_event(event) for event in rows(db.connect(root).execute(
            "SELECT * FROM iteration_event WHERE iteration_id = ? ORDER BY id",
            (iteration_id,)))
    ]
    item["sessions"] = rows(db.connect(root).execute(
        "SELECT id, name, status, duration_s, build_ref, started_at "
        "FROM playtest_session WHERE iteration_id = ? ORDER BY id",
        (iteration_id,)))
    return item


def list_iterations(root: str | os.PathLike[str], limit: int = 30) -> list[dict]:
    ids = [
        int(row[0]) for row in db.connect(root).execute(
            "SELECT id FROM iteration ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 100)),))
    ]
    return [get(root, iteration_id) for iteration_id in ids]


def record_checks(root: str | os.PathLike[str], result: dict) -> dict:
    """Persist the latest automated-check result for the next snapshot."""
    path = Path(root) / ".bgate" / "test-results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    iteration_id = active_id(root)
    if iteration_id:
        with db.tx(root) as conn:
            conn.execute(
                "UPDATE iteration SET tests_json = ? WHERE id = ?",
                (json.dumps(result), iteration_id))
        add_event(root, iteration_id, "automated_checks", "checks", "",
                  f"Automated checks: {result.get('status', 'recorded')}", result)
    return {"path": str(path), "result": result,
            "iteration_id": iteration_id}


def _decode(row: dict) -> dict:
    for source, target, fallback in (
        ("active_artifact_ids_json", "active_artifact_ids", []),
        ("tunables_json", "tunables", {}),
        ("tests_json", "tests", {}),
        ("outcome_json", "outcome", {}),
    ):
        try:
            row[target] = json.loads(row.pop(source))
        except (TypeError, json.JSONDecodeError):
            row.pop(source, None)
            row[target] = fallback
    return row


def _decode_event(row: dict) -> dict:
    try:
        row["data"] = json.loads(row.pop("data_json"))
    except (TypeError, json.JSONDecodeError):
        row.pop("data_json", None)
        row["data"] = {}
    return row
