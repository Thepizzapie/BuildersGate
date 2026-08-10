"""Music generation endpoints for the audio seat.

The seat's other half. ``bgate_core.music`` does the work; this exposes it to
the browser and makes one decision the core module does not: GENERATION RUNS AS
A BACKGROUND JOB. A Suno request takes one to three minutes, and holding the
HTTP request open for that would burn a uvicorn worker, give the browser nothing
but a spinner, and turn a dropped connection into a paid-for batch nobody can
find. So POST /api/music/generate answers 202 with a job id (routes/jobs.py) and
the seat polls it — the same shape the slow engine endpoints already use.

Auto-registers via routes/__init__.py — no edit to app.py.
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Request

from bgate_core import jobs as _core_jobs, music as _music
from bgate_ui import api
from bgate_ui.deps import root
from bgate_ui.routes import jobs as _jobs

router = APIRouter()

JOB_KIND = "music_generate"

# WHEN THIS PROCESS STARTED, and it is load-bearing rather than telemetry.
#
# A job row lives in the database; the thread that advances it lives in this
# process. Restart the dashboard and every job that was running becomes a row
# that says "running" forever, with a spinner in the UI attached to a thread
# that no longer exists — which is exactly the "old prompt still queued" the
# user reported. There is no column for "orphaned", and there cannot be a
# reliable one written at shutdown (a killed process writes nothing). But the
# inference is exact: a non-terminal job CREATED BEFORE THIS PROCESS DID cannot
# possibly still be running, because nothing here is advancing it.
_STARTED_AT = time.time()

# Everything build_music understands, so the form can grow a field without this
# module learning about it. Anything else in the body is REFUSED rather than
# dropped: a silently ignored `duration` is a track of the wrong length that
# was still charged for, which is the same argument kie.build_input makes.
SUNO_FIELDS = {
    "model", "custom", "instrumental", "style", "title", "negative_tags",
    "vocal_gender", "duration", "styleWeight", "weirdnessConstraint",
    "audioWeight", "personaId", "personaModel", "callback",
}


@router.get("/api/music/options")
def music_options() -> dict:
    """Models, per-model character limits, and whether kie is reachable at all.

    The form is built FROM this — see seats/audio.js. A limit typed into the
    HTML is a limit that goes stale silently; one read from here goes stale
    loudly, as a 422 the adapter already knows how to explain.
    """
    return api.ok(_music.options(root()))


@router.post("/api/music/generate")
def music_generate(payload: dict, request: Request) -> dict:
    """Start a Suno generation. 202 + {job_id}; poll /api/jobs/{id}.

    Synchronous only when the caller asks for it (``{"async": false}``), which
    exists for a script that wants one call and can wait three minutes.
    """
    project = str(root())
    prompt = str((payload or {}).get("prompt") or "").strip()
    if not prompt:
        raise api.bad_request("a music generation needs a prompt")
    name = str((payload or {}).get("name") or "").strip()

    unknown = sorted(set(payload or {}) - SUNO_FIELDS
                     - {"prompt", "name", "async", "work_item_id"})
    if unknown:
        raise api.bad_request(
            f"unknown field(s): {', '.join(unknown)} — Suno takes "
            f"{', '.join(sorted(SUNO_FIELDS))}. Passing one it does not know "
            "would be ignored and you would still be charged.",
            unknown=unknown)
    suno = {k: v for k, v in (payload or {}).items()
            if k in SUNO_FIELDS and v is not None and v != ""}
    work_item_id = (payload or {}).get("work_item_id")
    try:
        work_item_id = int(work_item_id) if work_item_id else None
    except (TypeError, ValueError):
        raise api.bad_request("work_item_id must be an integer")

    def work(job_id: int) -> dict:
        # EVERY STEP SUNO REPORTS BECOMES A WORD ON THE JOB ROW. Without this
        # the seat could only draw a spinner for one to three minutes, which is
        # indistinguishable from a hang — and what a person does about an
        # apparent hang is fire a second paid generation at the same prompt.
        #
        # This is also where cancellation is REAL rather than advisory: the
        # callback raises, which unwinds poll_music, and kie.generate_music
        # returns the task id it had reached. So a cancelled job hands back
        # something recoverable instead of just stopping.
        def progress(fraction: float, words: str, _status: str = "") -> None:
            if job_id and _jobs.is_cancelled(job_id):
                from bgate_adapters.kie import MusicCancelled

                raise MusicCancelled(
                    "cancelled from the dashboard — Suno was already asked for "
                    "this batch and it is charged for; recover it with the task "
                    "id on this result once it finishes")
            if job_id:
                _core_jobs.progress(project, job_id, fraction=fraction,
                                    stage=words[:200])

        try:
            result = _music.generate(project, prompt, name=name,
                                     work_item_id=work_item_id,
                                     on_progress=progress, **suno)
        except Exception as exc:                                 # noqa: BLE001
            # Returned, not raised: a failure here is a real answer the seat
            # renders ("Suno refused: prompt is 900 characters"), where a
            # raised one becomes a job error with a traceback in it.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if job_id:
            _core_jobs.progress(
                project, job_id, fraction=1.0,
                stage=("done" if result.get("ok")
                       else "cancelled" if result.get("cancelled") else "failed"))
        # The prompt travels WITH the result. A finished job arriving while the
        # form holds a different prompt has to be able to say which request it
        # was answering; the seat cannot infer it from a candidate list that
        # every batch writes into.
        return {**result, "prompt": prompt, "requested_name": name}

    # The default here is ASYNC, which inverts jobs.wants_async — that helper
    # reads an opt-IN, and every other slow endpoint in the product is
    # synchronous unless asked. A music request is minutes long every time, so
    # the inversion is stated rather than implied by a missing key.
    if str((payload or {}).get("async", "")).strip().lower() in {"0", "false", "no"}:
        return api.ok(work(0))
    return api.ok(_jobs.start("music_generate", work,
                              request_body={"prompt": prompt[:400],
                                            "name": name, **suno},
                              request=request))


@router.get("/api/music/jobs")
def music_jobs(limit: int = 12) -> dict:
    """Every music generation this project has run, newest first.

    THE ANSWER TO "old prompt still queued". Jobs were invisible: the seat
    tracked exactly one id in a JS variable, so a second generation, a reload,
    or a dashboard restart left rows nobody could see, cancel or clear — and a
    job number in the teens with no list behind it reads as a queue backing up.

    Each row says which PROMPT it was for, how far it got, and — for a
    non-terminal job older than this process — that it is ORPHANED: its thread
    died with the previous dashboard and it will never move again. That is a
    thing to dismiss, not a thing to wait for.
    """
    project = root()
    rows = _core_jobs.list_jobs(project, kind=JOB_KIND,
                                limit=max(1, min(int(limit), 50)))
    out = []
    for row in rows:
        # list_jobs hands back RAW rows — request_json and result_json are still
        # strings there, where jobs.get() decodes them. Re-reading each row
        # through the one decoder beats a second copy of the json.loads dance
        # (which is how this shipped with an empty prompt on every card), and at
        # a cap of fifty single-row lookups it costs nothing worth saving.
        job = _core_jobs.get(project, int(row["id"])) or row
        view = _jobs.view(project, job)
        request = view.get("request") or {}
        view["prompt"] = str(request.get("prompt") or "")
        view["name"] = str(request.get("name") or "")
        view["orphaned"] = bool(
            not view["terminal"]
            and api_stamp(view.get("created_at")) < _STARTED_AT)
        result = view.get("result") or {}
        view["task_id"] = str(result.get("task_id") or "")
        view["recoverable"] = bool(result.get("recoverable")
                                   or (result.get("task_id")
                                       and not result.get("ok")))
        out.append(view)
    return api.ok({"jobs": out, "kind": JOB_KIND,
                   "server_started_at": _STARTED_AT})


def api_stamp(when: Optional[str]) -> float:
    """A SQLite ``datetime('now')`` stamp as a UTC epoch. 0 when unreadable.

    Zero is the safe answer: it makes an unparseable timestamp look OLD, so the
    job is offered for dismissal rather than left spinning forever. Guessing the
    other way would hide the exact state this endpoint exists to surface.
    """
    text = str(when or "").strip()
    if not text:
        return 0.0
    from datetime import datetime, timezone

    for shape in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return (datetime.strptime(text[:19], shape)
                    .replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return 0.0


@router.post("/api/music/jobs/{job_id}/dismiss")
def music_job_dismiss(job_id: int, request: Request) -> dict:
    """Close out a job row the UI should stop watching.

    Distinct from ``POST /api/jobs/{id}/cancel``, which asks a LIVE job to stop
    at its next boundary and honestly reports that it may not. There is nothing
    to ask here: an orphaned job's thread is gone. This writes the terminal
    state the dead process never got to write, so the row stops claiming to be
    running. A job that IS live is cancelled first, so one button on a row can
    mean 'make this stop bothering me' whichever it turns out to be.
    """
    project = root()
    job = _core_jobs.get(project, job_id)
    if job is None:
        raise api.not_found(f"no job {job_id}", job_id=job_id)
    if job.get("kind") != JOB_KIND:
        raise api.bad_request(f"job {job_id} is a {job.get('kind')!r} job, not "
                              f"a music generation", job_id=job_id)
    if job["status"] in _core_jobs.TERMINAL:
        return api.ok({"dismissed": False, "already": job["status"],
                       "job": _jobs.view(project, job)})
    orphaned = api_stamp(job.get("created_at")) < _STARTED_AT
    _jobs.request_cancel(job_id)
    _core_jobs.finish(
        project, job_id, status="cancelled",
        result=(job.get("result") or {}),
        error=("orphaned by a dashboard restart — the thread that was running "
               "this died with the previous process" if orphaned else
               f"dismissed by {api.current_actor(request)}"))
    return api.ok({"dismissed": True, "orphaned": orphaned,
                   "job": _jobs.view(project, _core_jobs.get(project, job_id)),
                   "note": "if this job had already reached Suno, the batch was "
                           "charged for — its task id is on the job result and "
                           "POST /api/music/recover will collect it"})


@router.post("/api/music/recover")
def music_recover(payload: dict, request: Request) -> dict:
    """Collect the tracks of a task that was already paid for. Costs nothing.

    For the batch that submitted, rendered, was charged, and then died at the
    download — which is not hypothetical: kie's CDN 403'd every download this
    product made until the User-Agent was fixed. Idempotent by Suno track id.
    """
    task_id = str((payload or {}).get("task_id") or "").strip()
    if not task_id:
        raise api.bad_request("recover needs the task_id of a kie music task")
    try:
        return api.ok(_music.recover(root(), task_id,
                                     name=str((payload or {}).get("name") or "")))
    except _music.MusicError as exc:
        raise api.bad_request(str(exc), task_id=task_id)
    except Exception as exc:                                     # noqa: BLE001
        raise api.unavailable(f"{type(exc).__name__}: {exc}", task_id=task_id)


@router.get("/api/music/candidates")
def music_candidates(logical_name: Optional[str] = None,
                     limit: int = 200) -> dict:
    """The gallery: generated tracks awaiting keep-or-discard, plus what was kept."""
    project = root()
    return api.ok({
        "candidates": _music.candidates(project,
                                        logical_name=logical_name or "",
                                        limit=max(1, min(int(limit), 500))),
        "kept": _music.kept(project, limit=max(1, min(int(limit), 500))),
    })


@router.post("/api/music/keep")
def music_keep(payload: dict, request: Request) -> dict:
    """Install a candidate under the engine project and approve the revision.

    The human gate lives in ``artifacts.review`` and is not duplicated here —
    but the actor is passed explicitly so the decision is stamped with who at
    the dashboard made it, rather than re-derived one layer down.
    """
    artifact_id = _artifact_id(payload)
    actor = api.current_actor(request)
    try:
        return api.ok(_music.keep(root(), artifact_id, actor=actor,
                                  note=str((payload or {}).get("note") or "")))
    except PermissionError as exc:
        raise api.forbidden(str(exc), artifact_id=artifact_id, actor=actor)
    except LookupError as exc:
        raise api.not_found(str(exc), artifact_id=artifact_id)
    except (_music.MusicError, ValueError, OSError) as exc:
        raise api.bad_request(str(exc), artifact_id=artifact_id)


@router.post("/api/music/install")
def music_install(payload: dict, request: Request) -> dict:
    """Put an already-approved take where the game can load it. The repair door.

    Separate from keep() because the state it fixes is not a decision waiting to
    be made — it is a decision already made whose delivery did not happen. On a
    project with the approval gate off, ``artifacts.register`` approves each take
    as it is filed, so there was never a candidate and never a keep; the row said
    approved and ``game/assets/audio/music/`` did not exist. Also the honest
    button for an approved track whose file was later deleted.
    """
    artifact_id = _artifact_id(payload)
    try:
        return api.ok(_music.install(root(), artifact_id,
                                     actor=api.current_actor(request)))
    except LookupError as exc:
        raise api.not_found(str(exc), artifact_id=artifact_id)
    except (_music.MusicError, ValueError, OSError) as exc:
        raise api.bad_request(str(exc), artifact_id=artifact_id)


@router.post("/api/music/discard")
def music_discard(payload: dict, request: Request) -> dict:
    """Reject a candidate. The file stays under .bgate_out; the decision is kept."""
    artifact_id = _artifact_id(payload)
    try:
        return api.ok(_music.discard(root(), artifact_id,
                                     actor=api.current_actor(request),
                                     note=str((payload or {}).get("note") or "")))
    except LookupError as exc:
        raise api.not_found(str(exc), artifact_id=artifact_id)
    except ValueError as exc:
        raise api.bad_request(str(exc), artifact_id=artifact_id)


@router.get("/api/music/task/{task_id}")
def music_task(task_id: str) -> dict:
    """Where a Suno task got to, straight from kie. Costs nothing.

    For the batch whose download died: the charge happened, the tracks may be
    sitting there, and this says so before anyone pays for them twice.
    """
    try:
        return api.ok(_music.status(root(), task_id))
    except _music.MusicError as exc:
        raise api.bad_request(str(exc), task_id=task_id)
    except Exception as exc:                                     # noqa: BLE001
        raise api.unavailable(f"{type(exc).__name__}: {exc}", task_id=task_id)


def _artifact_id(payload: Optional[dict]) -> int:
    try:
        return int((payload or {}).get("artifact_id"))
    except (TypeError, ValueError):
        raise api.bad_request("artifact_id must be an integer")
