"""The audio lab's API — list, probe, write, loop, and remember the mix.

The editing happens in the browser: WebAudio decodes ogg/wav/mp3, resamples,
mixes and renders offline far better than a round-trip could. So these
endpoints are the parts a browser genuinely cannot do —

  * tell the truth about a file BEFORE it is decoded (rate, channels, length),
    so a listing is right on first paint;
  * put bytes on disk safely, with a backup, an mtime check, and an ffmpeg hop
    when the target is .ogg;
  * write the Godot loop settings, which live in a ``.import`` sidecar the
    engine owns and no amount of audio editing can reach;
  * keep the mix session next to the file so a mixdown stays editable.

Playback reuses ``/api/audio/file`` (routes/audio_ws.py) — one audio file
server is enough, and adding a second would be two places to get path safety
right.
"""
from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

from bgate_core.audio import audiolab, audiostems
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

SKIP_DIRS = {".git", ".godot", ".bgate", ".bgate_out", ".asset_work",
             "__pycache__", "node_modules", "export", "build"}
SCAN_CAP = 4000
BACKUP_DIRNAME = "audio_backups"
MAX_PAYLOAD_CHARS = 180 * 1024 * 1024      # base64 of the WAV cap, with room
_STEM_JOBS: dict[str, dict] = {}
_STEM_LOCK = threading.Lock()
_LOG = logging.getLogger(__name__)


def _backup_dir(project_root: Path) -> Path:
    return project_root / ".bgate_out" / BACKUP_DIRNAME


def _audio(rel: str, *, must_exist: bool = True) -> tuple[Path, Path]:
    """Resolve a project-relative audio path, refusing anything outside it."""
    base = root().resolve()
    target = (base / str(rel or "")).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise api.forbidden("path escapes the project root", rel=rel)
    if target.suffix.lower() not in audiolab.AUDIO_SUFFIXES:
        raise api.ApiError(415, "not an audio file",
                           detail={"rel": rel,
                                   "audio": sorted(audiolab.AUDIO_SUFFIXES)})
    if must_exist and not target.is_file():
        raise api.not_found(f"no audio at {rel}", rel=rel)
    return base, target


def _describe(project_root: Path, target: Path) -> dict:
    info = audiolab.probe(target)
    rel = target.relative_to(project_root).as_posix()
    session = None
    session_error = None
    beat = None
    try:
        session = audiolab.load_session(target)
    except audiolab.AudioError as exc:
        # AudioError here WRAPS the OSError/JSONDecodeError that broke the read,
        # so its text is exception-derived and cannot ride the response; the
        # file it is about is already `rel` below. See api.safe_error.
        session_error = api.safe_error(exc)
    try:
        beat = audiolab.load_beat(target)
    except audiolab.AudioError as exc:
        session_error = (session_error or "") + " · beat: " + api.safe_error(exc)
    return {
        "rel": rel,
        "name": target.name,
        "bytes": target.stat().st_size,
        "mtime": int(target.stat().st_mtime),
        "url": f"/api/audio/file?rel={rel}",
        "info": info,
        "loop": audiolab.loop_state(target, info=info),
        "session": session,
        "beat": beat,
        "session_error": session_error,
        "editable": target.suffix.lower() in audiolab.WRITABLE,
    }


@router.get("/api/audio/lab/status")
def lab_status() -> dict:
    """What this install can actually write, and what it cannot."""
    ffmpeg = audiolab.ffmpeg_path()
    return {
        "writable": sorted(audiolab.WRITABLE),
        "ogg": bool(ffmpeg),
        "ogg_reason": "" if ffmpeg else
            "ffmpeg is not on PATH — .wav saves work, .ogg needs ffmpeg",
        "ffmpeg": bool(ffmpeg),
        "max_seconds": audiolab.MAX_SECONDS,
        "loop_modes": sorted(audiolab.LOOP_MODES),
        "stems": audiostems.capability(),
    }


def _stem_view(job: dict) -> dict:
    return {key: job.get(key) for key in
            ("id", "state", "stage", "profile", "source", "output_dir",
             "stems", "error", "created_at")}


def _contained(project_root: Path, candidate: Path) -> Path:
    """Re-prove containment on every path DERIVED from a request value.

    `_audio` already refuses an escaping `rel`, but each path built from its
    result is a fresh path expression, and a guard that only ran on the input
    is one refactor away from being the wrong guard. This one is a plain
    realpath prefix test so it holds for the derived path itself.
    """
    base = os.path.realpath(str(project_root))
    real = os.path.realpath(str(candidate))
    if real != base and not real.startswith(base + os.sep):
        raise api.forbidden("path escapes the project root",
                            rel=candidate.name)
    return candidate


def _stem_target(project_root: Path, source_target: Path) -> Path:
    parent = _contained(project_root, source_target).parent
    base = parent / f"{source_target.stem}_stems"
    candidate = _contained(project_root, base)
    n = 2
    while candidate.exists():
        candidate = _contained(project_root, base.with_name(f"{base.name}_{n}"))
        n += 1
    return candidate


def _run_stem_job(job_id: str, project_root: Path, source: Path,
                  work_dir: Path, target_dir: Path) -> None:
    def stage(text: str) -> None:
        with _STEM_LOCK:
            if job_id in _STEM_JOBS:
                _STEM_JOBS[job_id]["stage"] = text

    def cancelled() -> bool:
        with _STEM_LOCK:
            return bool(_STEM_JOBS.get(job_id, {}).get("cancel_requested"))

    with _STEM_LOCK:
        job = _STEM_JOBS[job_id]
        job.update(state="running", stage="analysing the clip")
        profile = job["profile"]
    try:
        outputs = audiostems.separate(source, work_dir, target_dir, profile, stage, cancelled)
        stems = []
        for output in outputs:
            rel = output.relative_to(project_root).as_posix()
            stems.append({"name": output.stem, "rel": rel})
            try:
                from bgate_core.store import assets
                assets.track(project_root, output)
            except Exception:
                pass
        with _STEM_LOCK:
            _STEM_JOBS[job_id].update(
                state="complete", stage="ready as mixer lanes", stems=stems)
    except audiostems.StemCancelled:
        with _STEM_LOCK:
            _STEM_JOBS[job_id].update(
                state="cancelled", stage="cancelled", error="")
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
    except Exception:
        _LOG.exception("Audio Lab stem job %s failed", job_id)
        with _STEM_LOCK:
            _STEM_JOBS[job_id].update(
                state="failed", stage="separation failed",
                error="The stem engine could not separate this clip. Check the server log.")
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@router.post("/api/audio/lab/stems")
def lab_stems_start(payload: dict) -> dict:
    """Start local model-backed source separation without blocking the UI."""
    cap = audiostems.capability()
    if not cap["available"]:
        raise api.unavailable(cap["reason"])
    profile = str(payload.get("profile") or "four")
    if profile not in audiostems.PROFILES:
        raise api.bad_request("unknown stem profile", profile=profile)

    raw = str(payload.get("wav") or "")
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    if not raw or len(raw) > MAX_PAYLOAD_CHARS:
        raise api.ApiError(413 if raw else 400, "invalid stem source payload")
    try:
        blob = base64.b64decode(raw, validate=True)
        audiolab.validate_wav(blob)
    except (binascii.Error, ValueError, audiolab.AudioError) as exc:
        raise api.bad_request(f"stem source is not valid WAV audio: {exc}")

    source_rel = str(payload.get("source_rel") or "game/assets/audio/untitled.wav")
    project_root, source_target = _audio(source_rel, must_exist=False)
    clean_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", source_target.stem).strip("_-")
    if not clean_name:
        clean_name = "audio"
    job_id = uuid.uuid4().hex[:12]
    work_dir = _contained(
        project_root, project_root / ".bgate_out" / "audio_stems" / job_id)
    work_dir.mkdir(parents=True, exist_ok=False)
    source = _contained(work_dir, work_dir / f"{clean_name}.wav")
    source.write_bytes(blob)
    target_dir = _stem_target(project_root, source_target)
    job = {
        "id": job_id, "state": "queued", "stage": "queued",
        "profile": profile, "source": source_rel,
        "output_dir": target_dir.relative_to(project_root).as_posix(),
        "stems": [], "error": "", "created_at": int(time.time()),
        "root": str(project_root.resolve()),
    }
    with _STEM_LOCK:
        active = [value for value in _STEM_JOBS.values()
                  if value.get("root") == str(project_root.resolve())
                  and value.get("state") in {"queued", "running"}]
        if active:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise api.conflict("a stem separation is already running",
                               job_id=active[0]["id"])
        # Bound an in-memory status list that otherwise survives for the server lifetime.
        finished = [key for key, value in _STEM_JOBS.items()
                    if value.get("state") in {"complete", "failed"}]
        for key in finished[:-24]:
            _STEM_JOBS.pop(key, None)
        _STEM_JOBS[job_id] = job
    threading.Thread(target=_run_stem_job,
                     args=(job_id, project_root, source, work_dir, target_dir),
                     daemon=True, name=f"audio-stems-{job_id}").start()
    return api.ok(_stem_view(job))


@router.get("/api/audio/lab/stems/{job_id}")
def lab_stems_job(job_id: str) -> dict:
    with _STEM_LOCK:
        job = _STEM_JOBS.get(job_id)
        if not job or job.get("root") != str(root().resolve()):
            raise api.not_found("no such stem job", job_id=job_id)
        return _stem_view(dict(job))


@router.post("/api/audio/lab/stems/{job_id}/cancel")
def lab_stems_cancel(job_id: str) -> dict:
    with _STEM_LOCK:
        job = _STEM_JOBS.get(job_id)
        if not job or job.get("root") != str(root().resolve()):
            raise api.not_found("no such stem job", job_id=job_id)
        if job.get("state") in {"queued", "running"}:
            job["cancel_requested"] = True
            job["stage"] = "stopping the stem engine"
        return api.ok(_stem_view(dict(job)))


@router.get("/api/audio/lab/list")
def lab_list(q: Optional[str] = None, limit: int = 300) -> dict:
    """Every audio file in the project, newest first, probed.

    Walks the whole tree rather than the two directories ``/api/audio/list``
    knows about: a sound being edited is often one that has not been filed into
    assets/audio yet, and a picker that cannot see it is a picker you work
    around.
    """
    project_root = root()
    limit = max(1, min(int(limit), 2000))
    needle = (q or "").strip().lower()
    found, scanned = [], 0
    for path in project_root.rglob("*"):
        if scanned >= SCAN_CAP:
            break
        if path.suffix.lower() not in audiolab.AUDIO_SUFFIXES or not path.is_file():
            continue
        if SKIP_DIRS & set(path.parts):
            continue
        scanned += 1
        rel = path.relative_to(project_root).as_posix()
        if needle and needle not in rel.lower():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        info = audiolab.probe(path)
        loop = audiolab.loop_state(path, info=info)
        found.append({
            "rel": rel, "name": path.name, "bytes": stat.st_size,
            "mtime": int(stat.st_mtime),
            "seconds": info.get("seconds"),
            "sample_rate": info.get("sample_rate"),
            "channels": info.get("channels"),
            "loops": bool(loop.get("enabled")),
            "editable": path.suffix.lower() in audiolab.WRITABLE,
            "has_session": audiolab.existing_session_path(path) is not None,
        })
    found.sort(key=lambda d: d["mtime"], reverse=True)
    return {"sounds": found[:limit], "count": len(found[:limit]),
            "total": len(found), "truncated": len(found) > limit}


@router.get("/api/audio/lab/open")
def lab_open(rel: str) -> dict:
    """Everything the editor needs on load. The samples come from /api/audio/file."""
    project_root, target = _audio(rel)
    return _describe(project_root, target)


@router.post("/api/audio/lab/save")
def lab_save(payload: dict) -> dict:
    """Write rendered audio back, keeping the previous bytes.

    The browser always sends 16-bit PCM WAV — it is the only thing WebAudio can
    encode without a library, and it is lossless, so the .ogg hop encodes ONCE
    from the master rather than transcoding something already lossy.

    ``rel`` may name a file that does not exist yet: "save as" is how a new
    sound effect is born, and forcing a placeholder file to exist first would
    be a worse ritual than checking the parent directory is inside the project.
    """
    project_root, target = _audio(str(payload.get("rel") or ""), must_exist=False)
    if target.suffix.lower() not in audiolab.WRITABLE:
        raise api.ApiError(415, f"cannot write {target.suffix} — "
                                f"writable: {sorted(audiolab.WRITABLE)}",
                           detail={"rel": payload.get("rel")})

    raw = str(payload.get("wav") or "")
    if raw.startswith("data:"):
        raw = raw.split(",", 1)[-1]
    if not raw:
        raise api.bad_request("no audio data in the payload")
    if len(raw) > MAX_PAYLOAD_CHARS:
        raise api.ApiError(413, "audio payload too large",
                           detail={"chars": len(raw)})
    try:
        blob = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise api.bad_request(f"audio data is not valid base64: {exc}")
    try:
        info = audiolab.validate_wav(blob)
    except audiolab.AudioError as exc:
        raise api.bad_request(str(exc), rel=payload.get("rel"))

    existed = target.is_file()
    expect = payload.get("mtime")
    overwrite = bool(payload.get("overwrite"))
    if existed and expect is not None and int(expect) != int(target.stat().st_mtime) \
            and not overwrite:
        raise api.conflict("the file changed on disk since you opened it",
                           rel=payload.get("rel"),
                           on_disk=int(target.stat().st_mtime),
                           expected=int(expect))
    # A "save as" onto a different path sends no mtime (JSON.stringify drops the
    # undefined key), so the check above short-circuits and an unrelated sound
    # would be replaced without a word. Make the caller say so out loud.
    if existed and expect is None and not overwrite:
        raise api.ApiError(409, f"{payload.get('rel')} already exists — "
                                "pass overwrite:true to replace it",
                           code="exists", detail={"rel": payload.get("rel")})

    backup = None
    if existed:
        bdir = _backup_dir(project_root)
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        copy = bdir / f"{target.stem}.{stamp}{target.suffix}"
        shutil.copy2(target, copy)
        backup = copy.relative_to(project_root).as_posix()

    if target.suffix.lower() == ".ogg":
        try:
            audiolab.encode_ogg(blob, target,
                                quality=int(payload.get("ogg_quality", 6)))
        except audiolab.AudioError as exc:
            raise api.unavailable(str(exc), rel=payload.get("rel"))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, target)

    try:
        from bgate_core.store import assets
        assets.track(project_root, target)
    except Exception:
        pass          # a project with no database is still one you can mix in

    return api.ok({
        **_describe(project_root, target),
        "backup": backup,
        "created": not existed,
        "source_info": info,
        # A new file has no .import until Godot sees it, and the loop settings
        # live there — say so once, here, instead of failing later.
        "needs_godot_import": not existed,
    })


@router.post("/api/audio/lab/loop")
def lab_loop(payload: dict) -> dict:
    """Set the loop points Godot reads. This is the setting no editor can hear.

    A music track whose .import says ``loop=false`` plays once and stops, and
    nothing about the audio file itself reveals that — which is why it is worth
    an endpoint of its own rather than a checkbox buried in a save dialog.
    """
    _, target = _audio(str(payload.get("rel") or ""))
    end = payload.get("end_s")
    try:
        result = audiolab.write_loop(
            target,
            enabled=bool(payload.get("enabled", True)),
            begin_s=float(payload.get("begin_s") or 0.0),
            end_s=float(end) if end is not None else None,
            mode=str(payload.get("mode") or "forward"))
    except (audiolab.AudioError, TypeError, ValueError) as exc:
        raise api.bad_request(str(exc), rel=payload.get("rel"))
    return api.ok({"rel": payload.get("rel"), **result})


@router.get("/api/audio/lab/beat")
def lab_beat_get(rel: str) -> dict:
    """The beat session for a clip, if it has one."""
    _, target = _audio(rel, must_exist=False)
    try:
        session = audiolab.load_beat(target)
    except audiolab.AudioError as exc:
        raise api.bad_request(str(exc), rel=rel)
    return {"rel": rel, "beat": session,
            "voices": {"drum": list(audiolab.DRUM_VOICES),
                       "synth": list(audiolab.SYNTH_VOICES),
                       "kinds": list(audiolab.TRACK_KINDS)},
            "limits": {"steps": audiolab.MAX_STEPS,
                       "tracks": audiolab.MAX_TRACKS,
                       "patterns": audiolab.MAX_PATTERNS,
                       "song": audiolab.MAX_SONG}}


@router.post("/api/audio/lab/beat")
def lab_beat_save(payload: dict) -> dict:
    """Save the pattern, not just the render.

    A rendered beat is a WAV; the session is the thing you want back tomorrow —
    tempo, grid, which steps are on, which voice each row is. A project that
    keeps only renders accumulates loops nobody can change.
    """
    project_root, target = _audio(str(payload.get("rel") or ""), must_exist=False)
    try:
        saved = audiolab.save_beat(target, payload.get("beat") or {})
    except audiolab.AudioError as exc:
        raise api.bad_request(str(exc), rel=payload.get("rel"))
    return api.ok({
        "rel": payload.get("rel"), "beat": saved,
        "seconds": round(audiolab.beat_seconds(saved), 3),
        "path": audiolab.beat_path(target).relative_to(project_root).as_posix(),
    })


@router.post("/api/audio/lab/session")
def lab_session(payload: dict) -> dict:
    """Save the mixer session beside the file it renders to."""
    project_root, target = _audio(str(payload.get("rel") or ""), must_exist=False)
    try:
        saved = audiolab.save_session(target, payload.get("session") or {})
    except audiolab.AudioError as exc:
        raise api.bad_request(str(exc), rel=payload.get("rel"))
    return api.ok({
        "rel": payload.get("rel"), "session": saved,
        "path": audiolab.session_path(target)
                .relative_to(project_root).as_posix(),
    })
