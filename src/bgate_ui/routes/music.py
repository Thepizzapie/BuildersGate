"""Music generation endpoints for the audio seat.

The seat's other half. ``bgate_core.audio.music`` does the work; this exposes it to
the browser and makes one decision the core module does not: GENERATION RUNS AS
A BACKGROUND JOB. A Suno request takes one to three minutes, and holding the
HTTP request open for that would burn a uvicorn worker, give the browser nothing
but a spinner, and turn a dropped connection into a paid-for batch nobody can
find. So POST /api/music/generate answers 202 with a job id (routes/jobs.py) and
the seat polls it — the same shape the slow engine endpoints already use.

Auto-registers via routes/__init__.py — no edit to app.py.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request

from bgate_adapters import kie as _kie
from bgate_core.board import jobs as _core_jobs
from bgate_core.audio import music as _music
from bgate_ui import api
from bgate_ui.deps import root
from bgate_ui.routes import jobs as _jobs

router = APIRouter()

JOB_KIND = "music_generate"

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
            return {"ok": False, "error": api.safe_error(exc)}
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
        raise api.unavailable(api.safe_error(exc), task_id=task_id)


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
    except _kie.KieError as exc:
        # NOT 503. An unset KIE_API_KEY is a configuration state the caller can
        # fix in the panel, and 503 says the upstream is down — which sends
        # someone to kie's status page over a key they never entered. It also
        # tripped the route smoke test, whose whole rule is that a GET must not
        # answer in the server-error class for a condition the server is fine
        # about.
        raise api.bad_request(str(exc), task_id=task_id)
    except Exception as exc:                                     # noqa: BLE001
        raise api.unavailable(api.safe_error(exc), task_id=task_id)


def _artifact_id(payload: Optional[dict]) -> int:
    try:
        return int((payload or {}).get("artifact_id"))
    except (TypeError, ValueError):
        raise api.bad_request("artifact_id must be an integer")
