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
import os
import shutil
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

from bgate_core.audio import audiolab
from bgate_ui import api
from bgate_ui.deps import root

router = APIRouter()

SKIP_DIRS = {".git", ".godot", ".bgate", ".bgate_out", ".asset_work",
             "__pycache__", "node_modules", "export", "build"}
SCAN_CAP = 4000
BACKUP_DIRNAME = "audio_backups"
MAX_PAYLOAD_CHARS = 180 * 1024 * 1024      # base64 of the WAV cap, with room


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
        session_error = str(exc)
    try:
        beat = audiolab.load_beat(target)
    except audiolab.AudioError as exc:
        session_error = (session_error or "") + f" · beat: {exc}"
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
    }


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
