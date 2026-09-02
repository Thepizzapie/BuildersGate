"""Async jobs for operations too slow to hold an HTTP request open.

A Godot import or a headless run can take 90 seconds. Those endpoints blocked
the request (and a threadpool worker) for the duration, with a caller-supplied
timeout and no progress — so the UI could only show a spinner and hope.

The pattern is the one the playtest processing worker already uses: POST returns
202 ``{job_id}`` immediately, a daemon thread does the work, and the UI polls
``GET /api/jobs/{id}`` for ``{state, progress, stage, result}``.
"""
from __future__ import annotations

import json
import os
import threading
import traceback
from typing import Any, Callable, Optional

from ..store import db

TERMINAL = {"done", "failed", "cancelled"}


def create(root: str | os.PathLike[str], kind: str, *,
           request: Optional[dict] = None, actor: str = "") -> int:
    with db.tx(root) as conn:
        cur = conn.execute(
            "INSERT INTO job (kind, status, request_json, actor) "
            "VALUES (?, 'queued', ?, ?)",
            (kind, json.dumps(request or {}), actor))
    return int(cur.lastrowid)


def get(root: str | os.PathLike[str], job_id: int) -> Optional[dict]:
    row = db.connect(root).execute(
        "SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    for field in ("request_json", "result_json"):
        try:
            out[field[:-5]] = json.loads(out.pop(field) or "{}")
        except Exception:
            out[field[:-5]] = {}
    return out


def list_jobs(root: str | os.PathLike[str], *, kind: str = "",
              status: str = "", limit: int = 50) -> list[dict]:
    sql = "SELECT * FROM job WHERE 1=1"
    params: list = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 200)))
    return [dict(r) for r in db.connect(root).execute(sql, params)]


def progress(root: str | os.PathLike[str], job_id: int, *,
             fraction: Optional[float] = None, stage: str = "") -> None:
    """Report progress from inside a running job. Best-effort by design."""
    sets, params = ["updated_at = datetime('now')"], []
    if fraction is not None:
        sets.append("progress = ?")
        params.append(max(0.0, min(1.0, float(fraction))))
    if stage:
        sets.append("stage = ?")
        params.append(stage[:200])
    params.append(job_id)
    try:
        with db.tx(root) as conn:
            conn.execute(f"UPDATE job SET {', '.join(sets)} WHERE id = ?", params)
    except Exception:
        pass


def finish(root: str | os.PathLike[str], job_id: int, *,
           status: str = "done", result: Optional[dict] = None,
           error: str = "") -> None:
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE job SET status = ?, result_json = ?, error = ?, "
            "progress = CASE WHEN ? = 'done' THEN 1.0 ELSE progress END, "
            "updated_at = datetime('now') WHERE id = ?",
            (status, json.dumps(result or {}), error[:2000], status, job_id))


def run_in_background(root: str | os.PathLike[str], kind: str,
                      work: Callable[[int], Any], *,
                      request: Optional[dict] = None, actor: str = "") -> int:
    """Create a job and run ``work(job_id)`` on a daemon thread.

    Whatever ``work`` returns becomes the job result (wrapped if it is not a
    dict); a raised exception fails the job with its traceback rather than
    vanishing into a dead thread.
    """
    job_id = create(root, kind, request=request, actor=actor)

    def _run() -> None:
        try:
            with db.tx(root) as conn:
                conn.execute(
                    "UPDATE job SET status = 'running', updated_at = datetime('now') "
                    "WHERE id = ?", (job_id,))
            result = work(job_id)
            if not isinstance(result, dict):
                result = {"result": result}
            finish(root, job_id, status="done", result=result)
        except Exception as exc:
            finish(root, job_id, status="failed",
                   error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}")

    threading.Thread(target=_run, name=f"job-{kind}-{job_id}", daemon=True).start()
    return job_id
