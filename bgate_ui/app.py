"""The dashboard backend — the local cockpit over the project's SQLite store.

One page, polled JSON, no build step, no node, no CDN. Mutations are deliberately
limited to user-facing orchestration and review: queue/dispatch, recording,
feedback disposition, and generated-artifact approval.

Run: bgate serve [--port 7788]   (from anywhere inside a project, or BGATE_ROOT)
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from bgate_core import (
    activity, artifacts, assets, bible, db, iterations, lore, playtest,
    project, seats,
)
from bgate_core import queue as _queue
from bgate_ui import dispatch as _dispatch

app = FastAPI(title="builders-gate-ui", docs_url=None, redoc_url=None)
_verify_cache: dict[str, tuple[float, dict]] = {}


def _asset_verification(root: Path, *, force: bool = False) -> dict:
    key = str(root.resolve())
    cached = _verify_cache.get(key)
    if not force and cached and time.monotonic() - cached[0] < 10:
        return cached[1]
    result = assets.verify(root)
    result["verified_at"] = time.time()
    _verify_cache[key] = (time.monotonic(), result)
    return result


@app.middleware("http")
async def _coi_headers(request, call_next):
    """Cross-origin isolation on every response — the embedded WASM game build
    (/play) needs SharedArrayBuffer, which needs these on the whole origin."""
    response = await call_next(request)
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
    return response

_STATIC = Path(__file__).with_name("static")

# Only ever serve images, and only from inside the project. The preview endpoint
# takes root-relative paths; anything that escapes the root is refused.
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".svg"}


def _root() -> Path:
    override = os.environ.get("BGATE_ROOT")
    if override:
        return Path(override)
    root = db.resolve_root()
    if root is None:
        raise HTTPException(503, "no .bgate project at or above the cwd — "
                                 "run the dashboard from inside a game project")
    return root


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (_STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/state")
def state() -> dict:
    """Everything the dashboard shows, one poll."""
    root = _root()
    conn = db.connect(root)

    try:
        proj = project.get(root)
    except LookupError:
        raise HTTPException(503, f"no project initialized at {root}")

    seat_table = seats.roles_for(root)
    locked = assets.list_assets(root, locked_only=True)
    locks_by_seat: dict[str, list] = {}
    for entry in locked:
        locks_by_seat.setdefault(entry["lock_seat"], []).append(
            {"path": entry["path"], "since": entry["lock_at"], "kind": entry["kind"]})

    latest_by_seat: dict[str, dict] = {}
    for event in activity.recent(root, limit=200):
        if event["seat"] and event["seat"] not in latest_by_seat:
            latest_by_seat[event["seat"]] = {
                "summary": event["summary"], "kind": event["kind"],
                "at": event["created_at"]}

    feedback_counts: dict[str, int] = {}
    for row in conn.execute(
            "SELECT seat, count(*) AS n FROM playtest_item "
            "WHERE status = 'promoted' "
            "AND NOT EXISTS (SELECT 1 FROM work_item w "
            "WHERE w.source = 'playtest' "
            "AND w.source_ref = CAST(playtest_item.id AS TEXT) "
            "AND w.status = 'done') GROUP BY seat"):
        feedback_counts[row["seat"]] = row["n"]

    previews_dir = root / ".bgate" / "previews"
    previews = []
    if previews_dir.is_dir():
        files = sorted(previews_dir.glob("*.png"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:24]
        previews = [{"rel": str(p.relative_to(root)).replace("\\", "/"),
                     "name": p.stem,
                     "mtime": int(p.stat().st_mtime)} for p in files]
    session_rows = playtest.list_sessions(root)[:10]
    for session in session_rows:
        if session["status"] == "processing":
            session["processing_worker"] = _pt_processing.get(
                session["id"], "stalled")

    return {
        "project": proj,
        "root": str(root),
        "seats": [
            {
                **cfg,
                "locks": locks_by_seat.get(role, []),
                "last_activity": latest_by_seat.get(role),
                "promoted_feedback": feedback_counts.get(role, 0),
            }
            for role, cfg in seat_table.items()
        ],
        "assets": assets.list_assets(root),
        "artifacts": artifacts.list_revisions(root, limit=100),
        "asset_groups": artifacts.workspace(root),
        "iterations": iterations.list_iterations(root, limit=12),
        "verify": _asset_verification(root),
        "bible": bible.overview(root),
        "lore": {
            "canon": lore.list_entities(root, status="canon"),
            "draft": lore.list_entities(root, status="draft"),
        },
        "sessions": session_rows,
        "notes": seats.read_notes(root, limit=15),
        "previews": previews,
    }


@app.get("/api/activity")
def activity_feed(after_id: int = 0, limit: int = 60) -> dict:
    """The ticker. Poll with the last seen id for cheap incremental reads."""
    return {"events": activity.recent(_root(), limit=limit, after_id=after_id)}


@app.get("/api/preview")
def preview(rel: str) -> FileResponse:
    """Serve one image from inside the project. Root-relative paths only."""
    root = _root().resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(403, "path escapes the project root")
    if target.suffix.lower() not in _IMAGE_SUFFIXES:
        raise HTTPException(415, "images only")
    if not target.is_file():
        raise HTTPException(404, f"no image at {rel}")
    return FileResponse(target)


# ---------------------------------------------------------------------------
# The queue + dispatch: orchestration lives here now
# ---------------------------------------------------------------------------
@app.get("/api/queue")
def queue_list(status: Optional[str] = None) -> dict:
    # NOTE: promoted playtest feedback does NOT auto-become work items. That
    # dumped raw transcript fragments ("[add] Jump velocity negative 172.A")
    # straight into the queue as dispatchable tasks -- garbage that spawned
    # agents on sentence fragments. The director SYNTHESIZES promoted feedback
    # into a few coherent work items (queue_add) instead; a fragment is not a task.
    return {"items": _queue.list_items(_root(), status=status)}


@app.post("/api/queue")
def queue_add(payload: dict) -> dict:
    return _queue.add(_root(), payload["seat"], payload["title"],
                      brief=payload.get("brief", ""),
                      priority=int(payload.get("priority", 0)))


@app.post("/api/queue/{item_id}/dispatch")
def queue_dispatch(item_id: int, payload: Optional[dict] = None) -> dict:
    payload = payload or {}
    return _dispatch.dispatch(str(_root()), item_id,
                              model=payload.get("model") or None)


@app.post("/api/queue/{item_id}/stop")
def queue_stop(item_id: int) -> dict:
    return _dispatch.stop(item_id)


@app.post("/api/queue/import-orbit")
def queue_import_orbit() -> dict:
    return _queue.import_orbit(_root())


@app.get("/api/agents")
def agents() -> dict:
    return {"agents": _dispatch.status(str(_root()))}


@app.get("/api/agent-log/{item_id}")
def agent_log(item_id: int, tail: int = 60) -> dict:
    path = _root() / ".bgate" / "agents" / f"item-{item_id}.log"
    if not path.is_file():
        return {"lines": []}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"lines": lines[-tail:]}


@app.get("/api/agent-activity/{item_id}")
def agent_activity(item_id: int) -> dict:
    """Readable live feed of what a dispatched agent is doing — parsed from its
    stream-json log into tool calls, messages, and the final result."""
    return _dispatch.read_activity(str(_root()), item_id)


@app.get("/api/artifacts")
def artifact_list(status: Optional[str] = None,
                  logical_name: Optional[str] = None) -> dict:
    return {"artifacts": artifacts.list_revisions(
        _root(), status=status, logical_name=logical_name)}


@app.post("/api/artifacts/{artifact_id}/review")
def artifact_review(artifact_id: int, payload: dict) -> dict:
    try:
        return artifacts.review(
            _root(), artifact_id, payload.get("status", ""), payload.get("note", ""))
    except (LookupError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/assets/workspace")
def asset_workspace() -> dict:
    return {"groups": artifacts.workspace(_root())}


@app.post("/api/artifacts/{artifact_id}/regenerate")
def artifact_regenerate(artifact_id: int, payload: Optional[dict] = None) -> dict:
    try:
        return artifacts.regenerate(
            _root(), artifact_id, (payload or {}).get("reason", ""))
    except (LookupError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/artifacts/{artifact_id}/feedback/{item_id}")
def artifact_link_feedback(artifact_id: int, item_id: int,
                           payload: Optional[dict] = None) -> dict:
    try:
        return artifacts.link_feedback(
            _root(), artifact_id, item_id,
            float((payload or {}).get("confidence", 1.0)))
    except (LookupError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/iterations")
def iteration_list(limit: int = 30) -> dict:
    return {"iterations": iterations.list_iterations(_root(), limit=limit)}


@app.get("/api/iterations/{iteration_id}")
def iteration_detail(iteration_id: int) -> dict:
    try:
        return iterations.get(_root(), iteration_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/assets/verify")
def asset_verify_full() -> dict:
    return _asset_verification(_root(), force=True)


# ---------------------------------------------------------------------------
# Playtest recording — start/stop from the app; triage flows to the director
# ---------------------------------------------------------------------------
_pt_processing: dict = {}


def _triage_exists(root: Path, session_id: int) -> bool:
    row = db.connect(root).execute(
        "SELECT 1 FROM work_item WHERE source = 'playtest-triage' "
        "AND source_ref = ? LIMIT 1", (str(session_id),)).fetchone()
    return row is not None


def _queue_playtest_triage(root: Path, session_id: int, item_count: int) -> None:
    if _triage_exists(root, session_id):
        return
    _queue.add(
        root, "director",
        title=f"Triage playtest session {session_id}",
        brief=(f"A playtest session (id {session_id}) was recorded on video "
               "with the player narrating. WATCH THE RECORDING -- do not review "
               "from the transcript alone.\n\n"
               f"Call playtest_brief with session_id={session_id} and "
               "include_transcript=true. It returns:\n"
               "- video_frames: an ORDERED list of stills sampled across the "
               "whole session ({i, t, path}). READ every frame with the Read "
               "tool, in order -- this is you watching the playtest. You will "
               "SEE the bug happen (who hit whom, which way a fighter faced, a "
               "jump arc, a slider value on the tuning overlay).\n"
               "- transcript: what the player SAID, timestamped. Line it up with "
               "the frames by t -- 'see how he hits me' means nothing until you "
               "look at the frame at that timestamp.\n"
               "- items: an auto keyword-index. IGNORE its grouping/seats; it "
               "blobs four issues into one and mis-routes on stray words.\n\n"
               "Ground every work item in what you SAW plus what was said, then "
               "author them:\n"
               "- ONE work item per DISTINCT issue; split a monologue that "
               "covers jump tuning + a facing bug + spam into separate items, "
               "and merge lines scattered across the session about one issue.\n"
               "- Title + brief that names the concrete change, cites the "
               "timestamp/frame where it's visible, and quotes the player's "
               "words and any exact numbers. Route each to the owning seat.\n"
               "- Drop thinking-aloud ('hopefully this is recording').\n"
               "queue_add each item; then queue_complete this triage summarizing "
               "what you filed and which frames you based it on. Do NOT "
               "playtest_promote as a substitute for authoring work."),
        priority=3, source="playtest-triage", source_ref=str(session_id))


def _finish_playtest(root: Path, session_id: int, *, resume: bool = False) -> None:
    """Finish or resume durable processing in a worker thread."""
    try:
        if resume:
            session = playtest.get(root, session_id)
            result = {
                "session_id": session_id,
                "transcript": playtest.transcribe_session(
                    root, session_id,
                    audio_offset_s=float(session["audio_offset_s"] or 0)),
            }
        else:
            result = playtest.stop(root, session_id)
        transcript = result.get("transcript") or {}
        if not transcript.get("ok"):
            reason = transcript.get("error", "transcription did not complete")
            with db.tx(root) as conn:
                conn.execute(
                    "UPDATE playtest_session SET status = 'failed', "
                    "processing_stage = 'failed', processing_error = ?, error = ? "
                    "WHERE id = ?", (reason, reason, session_id))
            _pt_processing[session_id] = f"failed: {reason}"
            return
        item_count = int(transcript.get("items", 0))
        _queue_playtest_triage(root, session_id, item_count)
        with db.tx(root) as conn:
            conn.execute(
                "UPDATE playtest_session SET status = 'ready', "
                "processing_stage = 'ready', processing_error = '' WHERE id = ?",
                (session_id,))
        _pt_processing[session_id] = "ready"
    except Exception as exc:
        with db.tx(root) as conn:
            conn.execute(
                "UPDATE playtest_session SET status = 'failed', "
                "processing_stage = 'failed', processing_error = ?, error = ? "
                "WHERE id = ?", (str(exc), str(exc), session_id))
        _pt_processing[session_id] = f"failed: {exc}"


@app.get("/api/playtest/preflight")
def pt_preflight(native: bool = False) -> dict:
    return playtest.preflight(root=_root(), native=native)


@app.post("/api/playtest/start")
def pt_start(payload: Optional[dict] = None) -> dict:
    payload = payload or {}
    try:
        return playtest.start(_root(), payload.get("name") or "app session",
                              window_title=payload.get("window_title"),
                              mic_device=payload.get("mic_device"),
                              game_cmd=payload.get("game_cmd", ""),
                              launch_native=bool(payload.get("launch_native")))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@app.post("/api/playtest/stop")
def pt_stop() -> dict:
    """Stop recording; transcription runs in a worker thread (it takes ~a
    minute per 10 of audio). When it finishes, a DIRECTOR TRIAGE work item is
    queued automatically — dispatch it and a director session reviews the
    brief, promotes/dismisses feedback, and queues work for the seats."""
    import threading

    root = _root()
    try:
        session = playtest._active(root, None)
    except LookupError as exc:
        return {"ok": False, "error": str(exc)}
    sid = session["id"]
    _pt_processing[sid] = "processing"
    with db.tx(root) as conn:
        conn.execute(
            "UPDATE playtest_session SET status = 'processing', "
            "processing_stage = 'stopping', processing_error = '' WHERE id = ?",
            (sid,))

    threading.Thread(
        target=_finish_playtest, args=(root, sid), daemon=True).start()
    return {"ok": True, "session_id": sid, "processing": True}


@app.get("/api/playtest/status")
def pt_status() -> dict:
    root = _root()
    recording = None
    try:
        recording = playtest._active(root, None)
    except LookupError:
        pass
    processing = playtest.list_sessions(root, status="processing")
    recording_state = None
    if recording:
        event_count = db.connect(root).execute(
            "SELECT count(*) FROM playtest_event WHERE session_id = ?",
            (recording["id"],)).fetchone()[0]
        recording_state = {
            "id": recording["id"], "name": recording["name"],
            "telemetry_events": event_count,
            "native": bool(recording["game_cmd"]),
        }
    return {
        "recording": recording_state,
        "processing": [
            {"id": s["id"], "stage": s["processing_stage"] or "processing",
             "error": s["processing_error"] or "",
             "worker": _pt_processing.get(s["id"], "stalled")}
            for s in processing
        ],
    }


@app.post("/api/playtest/{session_id}/retry")
def pt_retry(session_id: int) -> dict:
    import threading

    root = _root()
    if _pt_processing.get(session_id) == "processing":
        raise HTTPException(409, "session processing is already running")
    session = playtest.get(root, session_id)
    if not session["audio_path"] or not Path(session["audio_path"]).is_file():
        raise HTTPException(409, "session has no captured audio to transcribe")
    _pt_processing[session_id] = "processing"
    threading.Thread(
        target=_finish_playtest, args=(root, session_id),
        kwargs={"resume": True}, daemon=True).start()
    return {"ok": True, "session_id": session_id, "processing": True}


@app.post("/api/playtest/{session_id}/events")
def pt_event(session_id: int, payload: dict) -> dict:
    try:
        if isinstance(payload.get("events"), list):
            accepted = [
                playtest.ingest_web_event(_root(), session_id, event)
                for event in payload["events"]
            ]
            return {"ok": True, "accepted": len(accepted)}
        return playtest.ingest_web_event(_root(), session_id, payload)
    except (LookupError, RuntimeError, ValueError) as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/playtest/{session_id}")
def pt_review(session_id: int) -> dict:
    try:
        root = _root().resolve()
        result = playtest.brief(root, session_id, include_transcript=True)
        for item in result["items"]:
            frame = item.get("frame_path")
            if frame:
                try:
                    item["frame_rel"] = str(
                        Path(frame).resolve().relative_to(root)).replace("\\", "/")
                except ValueError:
                    item["frame_rel"] = ""
        result["has_video"] = bool(
            result["session"]["video_path"]
            and Path(result["session"]["video_path"]).is_file())
        result["asset_options"] = [
            {
                "logical_name": group["logical_name"],
                "artifact_id": (
                    group["approved"] or
                    (group["revisions"][0] if group["revisions"] else None)
                )["id"],
            }
            for group in artifacts.workspace(root)
            if group["approved"] or group["revisions"]
        ]
        return result
    except LookupError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/playtest/{session_id}/video")
def pt_video(session_id: int) -> FileResponse:
    root = _root().resolve()
    session = playtest.get(root, session_id)
    path = Path(session["video_path"] or "").resolve()
    try:
        path.relative_to(root / ".bgate" / "playtests")
    except ValueError:
        raise HTTPException(403, "video path escapes playtest storage")
    if not path.is_file():
        raise HTTPException(404, "session has no playable video")
    return FileResponse(path, media_type="video/mp4")


@app.post("/api/playtest/items/{item_id}/promote")
def pt_promote(item_id: int, payload: Optional[dict] = None) -> dict:
    payload = payload or {}
    try:
        root = _root()
        # Promotion marks a moment as noteworthy -- it does NOT create a work
        # item. Turning a raw feedback chunk verbatim into a task produced blob/
        # fragment work items; coherent work is authored by the director from
        # the full transcript, by meaning. (sync_promoted call removed.)
        return playtest.promote(
            root, item_id, seat=payload.get("seat"),
            kind=payload.get("kind"), ref=payload.get("ref", "app-review"))
    except (LookupError, ValueError) as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/playtest/items/{item_id}/dismiss")
def pt_dismiss(item_id: int) -> dict:
    try:
        return playtest.dismiss(_root(), item_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/playtest/items/{item_id}/merge")
def pt_merge(item_id: int, payload: dict) -> dict:
    try:
        return playtest.merge(_root(), item_id, int(payload["target_id"]))
    except (LookupError, ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc))


# ---------------------------------------------------------------------------
# Play the game inside the app — always the CURRENT build
# ---------------------------------------------------------------------------
@app.get("/api/play/status")
def play_status() -> dict:
    from bgate_ui import webbuild
    return webbuild.status(_root())


@app.post("/api/play/rebuild")
def play_rebuild() -> dict:
    from bgate_ui import webbuild
    return webbuild.rebuild(str(_root()))


@app.get("/play/{file_path:path}")
def play_files(file_path: str = "") -> FileResponse:
    """Serve the WASM build inside the dashboard origin (COI comes from the
    middleware). /play/ -> index.html."""
    root = _root().resolve()
    web = (root / "export" / "web").resolve()
    if not web.is_dir():
        raise HTTPException(404, "no web build — export it first (tech seat)")
    target = (web / (file_path or "index.html")).resolve()
    try:
        target.relative_to(web)
    except ValueError:
        raise HTTPException(403, "path escapes the build dir")
    if not target.is_file():
        raise HTTPException(404, file_path)
    return FileResponse(target)


def serve(port: int = 7788) -> None:
    import uvicorn

    # 127.0.0.1 on purpose: this is a local window into a local store.
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
