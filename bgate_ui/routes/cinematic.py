"""Cutscene endpoints for the cinematic seat.

The seat's other half. ``bgate_core.cinematic`` does the work; this exposes it
to the browser and makes the same decision routes/music.py makes and for a
stronger reason: GENERATION RUNS AS A BACKGROUND JOB. A Suno request is one to
three minutes; a video request is five to fifteen, every time. Holding an HTTP
request open for that burns a uvicorn worker, gives the browser a spinner, and
turns a dropped connection into a paid-for clip nobody can find.

TRANSCODING IS A JOB TOO, WHICH MUSIC NEEDED NO EQUIVALENT OF. Keeping a track
copies bytes and returns in milliseconds. Keeping a shot re-encodes it to Ogg
Theora, which for a 1080p clip is tens of seconds and for an assembled
two-minute cut is minutes — long enough that a synchronous keep would look like
a hung dashboard at exactly the moment a human is deciding to ship something.

Auto-registers via routes/__init__.py — no edit to app.py.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request

from bgate_core import cinematic as _cine
from bgate_core import jobs as _core_jobs
from bgate_ui import api
from bgate_ui.deps import root
from bgate_ui.routes import jobs as _jobs

router = APIRouter()

GENERATE_JOB = "cinematic_shot"
ASSEMBLE_JOB = "cinematic_assemble"
KEEP_JOB = "cinematic_keep"

# Same reason routes/music.py records it: a job row lives in the database and
# the thread advancing it lives in this process, so a non-terminal job created
# before this process started cannot still be running.
_STARTED_AT = time.time()


@router.get("/api/cinematic/options")
def cinematic_options() -> dict:
    """Models, style presets, per-model limits, and both availabilities.

    The form is built FROM this. Two independent things can be missing and they
    fail differently — a provider key is what BUYS a shot, an ffmpeg with
    libtheora is what makes a bought shot playable — so the panel can say which
    one is wrong instead of drawing a disabled button with no explanation.
    """
    return api.ok(_cine.options(root()))


@router.get("/api/cinematic/sequences")
def cinematic_sequences(name: str = "") -> dict:
    """Every shot list, or one sequence with each shot's state."""
    project = root()
    if name:
        return api.ok({"sequence": _cine.sequence(project, name)})
    return api.ok({"sequences": _cine.sequences(project)})


@router.post("/api/cinematic/plan")
def cinematic_plan(payload: dict) -> dict:
    """Write or rewrite a shot list. Synchronous, because it spends nothing.

    The one free step in the pipeline, and the only place a sequence can be
    argued with before it costs money. Warnings are returned rather than
    raised — an unanchored sequence is a real choice a human is allowed to
    make, and what they are not allowed to do is make it uninformed.
    """
    body = payload or {}
    name = str(body.get("name") or "").strip()
    if not name:
        raise api.bad_request("a sequence needs a name")
    shots = body.get("shots") or []
    if not isinstance(shots, list) or not shots:
        raise api.bad_request("a sequence with no shots is not a plan")
    try:
        return api.ok(_cine.plan(
            root(), name, shots,
            logline=str(body.get("logline") or ""),
            style=str(body.get("style") or ""),
            style_note=str(body.get("style_note") or ""),
            style_refs=list(body.get("style_refs") or []),
            model=str(body.get("model") or ""),
            aspect_ratio=str(body.get("aspect_ratio") or "16:9"),
            resolution=str(body.get("resolution") or "720p")))
    except _cine.CinematicError as exc:
        raise api.bad_request(str(exc)) from exc


@router.get("/api/cinematic/styles")
def cinematic_styles() -> dict:
    """The preset table, with each entry's `note` naming its trap."""
    return api.ok({"styles": _cine.styles(),
                   "fallback": _cine.STYLE_FALLBACK})


@router.post("/api/cinematic/generate")
def cinematic_generate(payload: dict, request: Request) -> dict:
    """Buy one shot. 202 + {job_id}; poll /api/jobs/{id}.

    ONE SHOT PER REQUEST, matching the core module — there is no
    generate-the-sequence endpoint, because the thing a human must do between
    shots is watch the last one, and an endpoint that spends a sequence in one
    call is an endpoint built to skip that.
    """
    body = payload or {}
    project = str(root())
    name = str(body.get("name") or "").strip()
    idx = body.get("idx")
    if not name:
        raise api.bad_request("which sequence?")
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        raise api.bad_request("idx must be the shot number to generate") from None

    model = str(body.get("model") or "")
    audio = bool(body.get("generate_audio", False))

    def work(job_id: int) -> dict:
        def progress(fraction: float, words: str, _status: str = "") -> None:
            # A video generation reports little, so what this mostly buys is
            # the difference between "running" and "hung" — and what a person
            # does about an apparent hang is fire a second paid generation at
            # the same shot.
            if job_id:
                _core_jobs.progress(project, job_id, fraction=fraction,
                                    stage=words[:200])
        try:
            result = _cine.generate_shot(project, name, idx, model=model,
                                         generate_audio=audio,
                                         on_progress=progress)
        except Exception as exc:                                 # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if job_id:
            _core_jobs.progress(project, job_id, fraction=1.0,
                                stage="done" if result.get("ok") else "failed")
        return {**result, "sequence": name, "idx": idx}

    if str(body.get("async", "")).strip().lower() in {"0", "false", "no"}:
        return api.ok(work(0))
    return api.ok(_jobs.start(GENERATE_JOB, work,
                              request_body={"sequence": name, "idx": idx,
                                            "model": model},
                              request=request))


@router.post("/api/cinematic/assemble")
def cinematic_assemble(payload: dict, request: Request) -> dict:
    """Join the kept shots into one .ogv. 202 + {job_id}.

    A job because it is an ffmpeg pass over the whole runtime — minutes for a
    long sequence — not because it costs money. It costs nothing.
    """
    body = payload or {}
    project = str(root())
    name = str(body.get("name") or "").strip()
    if not name:
        raise api.bad_request("which sequence?")
    quality = int(body.get("quality") or _cine.DEFAULT_QUALITY)

    def work(job_id: int) -> dict:
        try:
            result = _cine.assemble(project, name, quality=quality)
        except Exception as exc:                                 # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if job_id:
            _core_jobs.progress(project, job_id, fraction=1.0, stage="done")
        return result

    if str(body.get("async", "")).strip().lower() in {"0", "false", "no"}:
        return api.ok(work(0))
    return api.ok(_jobs.start(ASSEMBLE_JOB, work,
                              request_body={"sequence": name},
                              request=request))


@router.get("/api/cinematic/candidates")
def cinematic_candidates(logical_name: str = "", limit: int = 100) -> dict:
    """Clips awaiting a decision, plus what has been kept and installed."""
    project = root()
    cap = max(1, min(int(limit), 500))
    return api.ok({
        "candidates": _cine.candidates(project, logical_name=logical_name,
                                       limit=cap),
        "kept": _cine.kept(project, limit=cap),
    })


@router.post("/api/cinematic/keep")
def cinematic_keep(payload: dict, request: Request) -> dict:
    """Transcode a clip into the engine project and approve it. 202 + {job_id}.

    ONLY A HUMAN MAY APPROVE, and that gate is not re-implemented here — it is
    inherited from artifacts.review, which is what reads the project's approval
    mode. What this layer adds is the actor: api.current_actor names who is
    asking, so an agent hitting this endpoint is refused by the same rule that
    refuses it everywhere else.
    """
    body = payload or {}
    project = str(root())
    try:
        artifact_id = int(body.get("artifact_id"))
    except (TypeError, ValueError):
        raise api.bad_request("artifact_id is required") from None
    note = str(body.get("note") or "")
    quality = int(body.get("quality") or _cine.DEFAULT_QUALITY)
    actor = api.current_actor(request)

    def work(job_id: int) -> dict:
        try:
            result = _cine.keep(project, artifact_id, note=note,
                                quality=quality, actor=actor)
        except Exception as exc:                                 # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if job_id:
            _core_jobs.progress(project, job_id, fraction=1.0, stage="done")
        return result

    if str(body.get("async", "")).strip().lower() in {"0", "false", "no"}:
        return api.ok(work(0))
    return api.ok(_jobs.start(KEEP_JOB, work,
                              request_body={"artifact_id": artifact_id},
                              request=request))


@router.post("/api/cinematic/install")
def cinematic_install(payload: dict, request: Request) -> dict:
    """Re-transcode an already-approved clip into the engine project.

    The repair button. Synchronous because it is pressed for one clip whose
    install went missing, not in a loop.
    """
    body = payload or {}
    try:
        artifact_id = int(body.get("artifact_id"))
    except (TypeError, ValueError):
        raise api.bad_request("artifact_id is required") from None
    try:
        return api.ok(_cine.install(root(), artifact_id,
                                    quality=int(body.get("quality")
                                                or _cine.DEFAULT_QUALITY),
                                    actor=api.current_actor(request)))
    except _cine.CinematicError as exc:
        raise api.bad_request(str(exc)) from exc


@router.post("/api/cinematic/discard")
def cinematic_discard(payload: dict, request: Request) -> dict:
    """Reject a clip and return its shot to planned. No human needed —
    refusing to ship something is a decision an agent may make."""
    body = payload or {}
    try:
        artifact_id = int(body.get("artifact_id"))
    except (TypeError, ValueError):
        raise api.bad_request("artifact_id is required") from None
    return api.ok(_cine.discard(root(), artifact_id,
                                note=str(body.get("note") or ""),
                                actor=api.current_actor(request)))


@router.post("/api/cinematic/recover")
def cinematic_recover(payload: dict) -> dict:
    """Download a shot that was already paid for. The anti-double-charge door.

    Synchronous: the generation is finished at the provider, so this is a
    download rather than a wait.
    """
    body = payload or {}
    name = str(body.get("name") or "").strip()
    if not name:
        raise api.bad_request("which sequence?")
    try:
        idx = int(body.get("idx"))
    except (TypeError, ValueError):
        raise api.bad_request("idx must be the shot number") from None
    try:
        return api.ok(_cine.recover_shot(root(), name, idx,
                                         str(body.get("task_id") or "")))
    except _cine.CinematicError as exc:
        raise api.bad_request(str(exc)) from exc


@router.get("/api/cinematic/jobs")
def cinematic_jobs(limit: int = 12) -> dict:
    """Every cutscene job this project has run, newest first.

    All three kinds in one list, because a human watching a sequence come
    together does not think of "generate", "keep" and "assemble" as separate
    queues — they are the stages of one thing, and a shot generating while an
    earlier one transcodes is the normal state.

    A non-terminal job older than this process is reported ORPHANED: its thread
    died with the previous dashboard and it will never move again. That is a
    thing to dismiss, not a thing to wait for.
    """
    project = root()
    cap = max(1, min(int(limit), 50))
    out = []
    for kind in (GENERATE_JOB, ASSEMBLE_JOB, KEEP_JOB):
        for row in _core_jobs.list_jobs(project, kind=kind, limit=cap):
            job = _core_jobs.get(project, int(row["id"])) or row
            view = _jobs.view(project, job)
            request = view.get("request") or {}
            view["sequence"] = str(request.get("sequence") or "")
            view["idx"] = request.get("idx")
            view["orphaned"] = bool(
                view.get("status") not in ("done", "failed", "cancelled")
                and _job_started_before_process(job))
            out.append(view)
    out.sort(key=lambda j: int(j.get("id") or 0), reverse=True)
    return api.ok({"jobs": out[:cap]})


def _job_started_before_process(job: dict) -> bool:
    """Whether this row predates the process that would have to be advancing it.

    Best-effort: an unparseable timestamp reports False, because calling a live
    job orphaned is worse than missing a dead one — the first makes a human
    dismiss work that is still running.
    """
    import datetime as _dt

    stamp = str(job.get("created_at") or "").strip()
    if not stamp:
        return False
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            when = _dt.datetime.strptime(stamp[:19], fmt)
        except ValueError:
            continue
        return when.replace(tzinfo=_dt.timezone.utc).timestamp() < _STARTED_AT
    return False
